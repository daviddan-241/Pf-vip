"""Arena.ai Web Browser Session Provider.

Drives a real headless Chromium browser session (via Playwright) against the
arena.ai web chat UI as an additive model backend transport in ArenaOS.

RESOURCE & MEMORY CAVEAT:
Headless Chromium requires significant memory (typically 300-500MB RAM per
active browser process). Render's FREE web service tier provides only 512MB RAM
total, which is tight alongside FastAPI, Uvicorn, SQLAlchemy, and the rest of ArenaOS.
If deployed on Render's Free Tier, running browser automation may trigger Out-Of-Memory
(OOM) process kills. It is strongly recommended to either upgrade to Render's paid
$7/mo tier (1GB+ RAM) or run this web provider on a separate lightweight always-on
machine and point ArenaOS at it if the free tier proves insufficient.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Optional, Sequence

from pydantic import BaseModel, Field

from .compat import get_settings

try:
    from playwright.async_api import (
        BrowserContext,
        ElementHandle,
        Page,
        Playwright,
        async_playwright,
    )
except ImportError:
    BrowserContext = Any  # type: ignore
    ElementHandle = Any  # type: ignore
    Page = Any  # type: ignore
    Playwright = Any  # type: ignore
    async_playwright = None  # type: ignore

from .base import (
    ArenaEndpoint,
    ArenaProvider,
    ChatMessage,
    CompleteRequest,
    ModelResponse,
    StreamEvent,
)

logger = logging.getLogger(__name__)


# ==============================================================================
# USER CONFIGURATION & DOM SELECTOR CONTRACT NOTICE:
# ------------------------------------------------------------------------------
# Because arena.ai's live DOM markup and layout can change or may differ from
# generic defaults, ALL CSS selectors in WebSessionConfig are fully configurable.
#
# Once you inspect the real arena.ai chat web page markup, you must confirm or
# adjust the following selectors in your WebSessionConfig:
#   1. input_selectors: CSS selectors targeting the main text chat input area
#      (e.g. 'textarea#chat-input', 'textarea[placeholder*="message" i]').
#   2. send_button_selectors: CSS selectors for the message submit/send button
#      (e.g. 'button#send', 'button[type="submit"]').
#   3. response_selectors: CSS selectors for assistant message content blocks
#      (e.g. 'div[role="assistant"]', '.assistant-message', '.response-container').
#   4. stop_button_selectors: CSS selectors for the stop-generation button if present.
#   5. login_url / chat_url: Specific URLs for logging in and chatting on arena.ai.
# ==============================================================================


if TYPE_CHECKING:  # shared-browser wiring only; no runtime import cycle
    from .browser_session import PersistentBrowser

class WebmailSession:
    """Standalone build has no agent webmail module — honest failure if the
    arena.ai verification-code login flow ever triggers. Password login
    (ARENA_EMAIL/ARENA_PASSWORD) is the supported path here."""
    def __init__(self, *a, **k):
        raise RuntimeError(
            "arena.ai asked for an email verification code; this standalone "
            "build has no webmail module. Log in once in a real browser or "
            "set ARENA_SESSION_COOKIE to reuse a logged-in cookie.")

CODE_INPUT_SELECTORS: list[str] = []
DEFAULT_WEBMAIL_URL = "about:blank" 


class ArenaWebLoginRequired(RuntimeError):
    """Raised when login is required for arena.ai but no credentials are available or login failed."""


class ArenaWebScrapeError(RuntimeError):
    """Raised when chat response elements could not be found or scraped from the page DOM."""

    def __init__(self, message: str, visible_snippet: str = "") -> None:
        super().__init__(message)
        self.visible_snippet = visible_snippet


class ArenaWebCaptchaRequired(RuntimeError):
    """Raised when arena.ai shows a reCAPTCHA 'Security Verification' challenge.

    Observed live (2026-09-07): anonymous/datacenter sessions trigger a reCAPTCHA
    on first submit. It cannot be solved programmatically. Resolution: log in with
    a real account (persistent browser profile) and/or solve the challenge once
    manually in the platform's browser-visibility page, after which the session
    cookie persists.
    """


class WebSessionConfig(BaseModel):
    """Configuration for ArenaWebSession and ArenaWebSessionProvider."""

    chat_url: str = "https://arena.ai"
    login_url: str = ""
    # The agent's own webmail (logged in once via the in-app browser, then
    # automatic): used to fetch email verification codes for ANY login flow.
    webmail_url: str = DEFAULT_WEBMAIL_URL
    # Selectors below were verified against the LIVE arena.ai site (2026-09-07).
    terms_accept_selectors: list[str] = Field(
        default_factory=lambda: [
            "button:has-text('Accept')",
            "button:has-text('I agree')",
            "button:has-text('Agree')",
            "button:has-text('Continue')",
            "button:has-text('Got it')",
            "[role='dialog'] button",
        ]
    )
    captcha_text_indicators: list[str] = Field(
        default_factory=lambda: [
            "Security Verification",
            "I'm not a robot",
            "not a robot",
        ]
    )
    captcha_iframe_selectors: list[str] = Field(
        default_factory=lambda: [
            "iframe[src*='recaptcha']",
            "iframe[title*='recaptcha' i]",
        ]
    )
    input_selectors: list[str] = Field(
        default_factory=lambda: [
            "form textarea",
            "textarea[placeholder*='Ask anything' i]",
            "textarea[placeholder*='Ask followup' i]",
            "textarea[placeholder*='Ask' i]",
            "textarea#chat-input",
            "textarea",
            "div[contenteditable='true']",
            "input[type='text']",
        ]
    )
    send_button_selectors: list[str] = Field(
        default_factory=lambda: [
            "form button[type='submit']",
            "form button:has(svg)",
            "button[aria-label*='send' i]",
            "button:has-text('Send')",
            "button#send",
            "button[type='submit']",
        ]
    )
    response_selectors: list[str] = Field(
        default_factory=lambda: [
            "div#responses > div",
            "div[role='assistant']",
            ".assistant-message",
            ".response-container",
            "div[data-message-author='assistant']",
            ".markdown-body",
        ]
    )
    stop_button_selectors: list[str] = Field(
        default_factory=lambda: [
            "button:has-text('Stop')",
            "button[aria-label*='Stop' i]",
            "button.stop-generating",
        ]
    )
    # Real, best-effort "use Arena's full potential" automation: if arena.ai
    # exposes a model/mode picker, find it and pick the strongest, least-
    # filtered option available — same generic-selector-with-fallback pattern
    # as everything else in this config. Danny: once you tell me the exact
    # model names in your account's picker, add them to the FRONT of
    # preferred_model_labels (checked in order, first match wins) and the
    # generic labels below stop being needed. Until then this silently no-ops
    # if the picker isn't found — never a fake "done", exactly like terms/
    # captcha detection above.
    model_picker_trigger_selectors: list[str] = Field(
        default_factory=lambda: [
            "[data-testid*='model' i]",
            "button[aria-haspopup='listbox']",
            "button[aria-label*='model' i]",
            "button:has-text('Model')",
            "[role='combobox']",
        ]
    )
    model_picker_option_selectors: list[str] = Field(
        default_factory=lambda: [
            "[role='option']",
            "[role='menuitem']",
            "li[role='option']",
            "ul li",
        ]
    )
    preferred_model_labels: list[str] = Field(
        default_factory=lambda: [
            # Danny: put your account's actual "most capable / unrestricted"
            # model name(s) here first, exact substrings from the picker text.
            "unrestricted", "uncensored", "unfiltered", "pro", "ultra", "max",
            "extended", "advanced", "large", "opus", "flagship",
        ]
    )
    browser_profile_dir: str = Field(
        default_factory=lambda: str(get_settings().data_dir / "browser_profile"))
    headless: bool = True
    name: str = "arena-web"
    timeout_s: float = 120.0
    poll_interval_s: float = 0.5


def _redact_credentials(text: str, extra_secrets: Optional[list[str]] = None) -> str:
    """Redact passwords, emails, cookies, and secret tokens from raw text before logging or reporting."""
    if not text:
        return text

    redacted = text
    if extra_secrets:
        for secret in extra_secrets:
            if secret and len(secret) > 1:
                redacted = redacted.replace(secret, "[REDACTED]")

    # Redact email addresses
    redacted = re.sub(
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "[REDACTED_EMAIL]", redacted
    )
    # Redact password/token key-value patterns
    redacted = re.sub(
        r"(?i)(password|passwd|secret|cookie|auth_token|session)\s*[:=]\s*['\"]?[^\s'\"]+['\"]?",
        r"\1=[REDACTED]",
        redacted,
    )
    return redacted


class ArenaWebSession:
    """Manages persistent Playwright browser context, page lifecycle, login, and DOM interactions."""

    def __init__(
        self,
        config: WebSessionConfig,
        get_credential: Optional[Callable[[str], Optional[str]]] = None,
        browser: Optional["PersistentBrowser"] = None,
    ) -> None:
        """`browser`: the app's shared PersistentBrowser. When given, this
        session uses ITS Chromium context (one process for the whole app —
        the arena page, webmail tab, and operator free-browsing all live in
        ONE launch). That halves RAM vs. the old two-launch setup (critical
        on the 512MB Render free tier — two Chromium processes were the
        OOM-kill that wiped the logged-in arena session on restart) and the
        arena.ai login persists in the same profile the live view drives."""
        self.config = config
        self.get_credential = get_credential
        self.browser = browser
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        # Set true while a human is driving this exact page via the live-login
        # WebSocket view (Settings > Connect arena.ai). Automated queries defer
        # rather than fighting the user for control of the same page.
        self.live_login_active = False
        # The agent's own webmail tab, opened in the SAME persistent context.
        self._webmail_page: Optional[Page] = None
        # True once we've tried the model-picker automation for this page's
        # lifetime — avoids re-opening the picker on every single message.
        self._model_confirmed = False

    async def get_page(self) -> Page:
        """Retrieve or initialize the active Playwright Page in a persistent browser context."""
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page

            if self.browser is not None:
                # Shared Chromium: the named "arena" page IS the model tab and
                # the live-login view tab — one process, one profile, logins
                # survive restarts because the profile dir is never reaped.
                self._page = await self.browser.get_page("arena")
                self._context = self._page.context
                return self._page

            if async_playwright is None:
                raise RuntimeError("Playwright package is not installed.")

            profile_dir = Path(self.config.browser_profile_dir).resolve()
            profile_dir.mkdir(parents=True, exist_ok=True)

            if self._playwright is None:
                self._playwright = await async_playwright().start()

            if self._context is None or self._context.is_closed():
                self._context = await self._playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=self.config.headless,
                    viewport={"width": 1280, "height": 800},
                    # Realistic desktop Chrome fingerprint (arena.ai + webmail
                    # sessions) — default headless UA gets flagged by
                    # Google/Microsoft bot detection.
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
                    locale="en-US", timezone_id="America/New_York",
                )

            pages = self._context.pages
            if pages:
                self._page = pages[0]
            else:
                self._page = await self._context.new_page()

            return self._page

    async def webmail_page(self) -> Page:
        """The agent's own email tab — same profile as arena.ai, so the ONE
        webmail login done via the in-app browser works for everything."""
        if self.browser is not None:
            # Shared Chromium: the named "webmail" tab stays alive between
            # sessions in the one persistent profile.
            return await self.browser.get_page("webmail")
        await self.get_page()  # ensures the persistent context is launched
        assert self._context is not None
        if self._webmail_page is None or self._webmail_page.is_closed():
            self._webmail_page = await self._context.new_page()
        return self._webmail_page

    async def _complete_code_login(self, page: Page, code_el) -> None:
        """A verification-code prompt is on the arena page — the agent reads
        the code from its own email and finishes the login by itself."""
        wm = WebmailSession(self.webmail_page, self.config.webmail_url)
        try:
            from arenaos.email.webmail import extract_codes
            baseline_text = await wm.inbox_text()
            baseline = {c for c, _ in extract_codes(baseline_text)}
            logger.info("arena login needs a code — polling the agent's email…")
            code = await wm.latest_code(baseline=baseline, timeout_s=100.0, poll_s=8.0)
        finally:
            await wm.close()

        await code_el.fill(code)  # type: ignore[attr-defined]
        submit = await self._find_first_visible(page, [
            "button[type='submit']",
            "button:has-text('Verify')",
            "button:has-text('Continue')",
            "button:has-text('Submit')",
        ])
        if submit:
            await submit.click()
        else:
            await code_el.press("Enter")  # type: ignore[attr-defined]
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            await asyncio.sleep(2.0)

        if await self._find_first_visible(page, self.config.input_selectors) is not None:
            logger.info("arena email-code login completed automatically.")
            return
        raise ArenaWebLoginRequired(
            "The emailed code was submitted, but the chat interface did not appear."
        )

    async def ensure_logged_in(self, page: Page) -> None:
        """Verify session is logged in, inject cookies or fill login form if needed."""
        # Inject cookie if available
        cookie_val = (
            self.get_credential("arena_web_session_cookie") if self.get_credential else None
        )
        if cookie_val and self._context:
            try:
                cookies_data = json.loads(cookie_val)
                if isinstance(cookies_data, dict):
                    cookies_data = [cookies_data]
                if isinstance(cookies_data, list):
                    for c in cookies_data:
                        if "url" not in c and "domain" not in c:
                            c["url"] = self.config.chat_url
                    await self._context.add_cookies(cookies_data)
                    logger.info("Injected session cookies into browser context (count: %d)", len(cookies_data))
            except Exception as exc:
                logger.warning("Failed to parse or set session cookies: %s", exc)

        target_url = self.config.login_url or self.config.chat_url
        current_url = page.url
        if current_url in ("about:blank", "", None) or not current_url.startswith("http"):
            await page.goto(target_url, wait_until="domcontentloaded")

        # Check if chat input is visible
        input_el = await self._find_first_visible(page, self.config.input_selectors)
        if input_el is not None:
            logger.info("Chat input interface found on page. Session is logged in.")
            return

        # Look for login form elements
        login_form_selectors = [
            "input[type='email']",
            "input[name*='email' i]",
            "input[name*='user' i]",
            "input[type='password']",
            "input[name*='password' i]",
            "form[action*='login' i]",
        ]
        login_el = await self._find_first_visible(page, login_form_selectors)

        email = self.get_credential("arena_web_email") if self.get_credential else None
        password = self.get_credential("arena_web_password") if self.get_credential else None
        has_creds = bool(email and password)

        logger.info(
            "Login check: login_form_detected=%s, email_password_provided=%s",
            login_el is not None,
            has_creds,
        )

        # Arena sometimes logs in by EMAILING A CODE — no password at all.
        # The agent reads that code from its own webmail automatically.
        code_el = await self._find_first_visible(page, CODE_INPUT_SELECTORS)
        if code_el is not None:
            logger.info("Verification-code field detected — auto-completing from the agent's email.")
            await self._complete_code_login(page, code_el)
            return

        if login_el is not None or input_el is None:
            if not has_creds:
                raise ArenaWebLoginRequired(
                    "Login is required to access the arena.ai chat interface, but no "
                    "credentials ('arena_web_email'/'arena_web_password' or 'arena_web_session_cookie') "
                    "were configured."
                )

            email_field = await self._find_first_visible(
                page,
                [
                    "input[type='email']",
                    "input[name*='email' i]",
                    "input[name*='user' i]",
                    "input[type='text']",
                ],
            )
            pass_field = await self._find_first_visible(
                page,
                [
                    "input[type='password']",
                    "input[name*='password' i]",
                ],
            )

            if email_field and pass_field:
                await email_field.fill(email)  # type: ignore
                await pass_field.fill(password)  # type: ignore

                submit_btn = await self._find_first_visible(
                    page,
                    [
                        "button[type='submit']",
                        "button:has-text('Log in')",
                        "button:has-text('Sign in')",
                        "input[type='submit']",
                    ],
                )
                if submit_btn:
                    await submit_btn.click()
                else:
                    await pass_field.press("Enter")

                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    await asyncio.sleep(2.0)

                input_el_after = await self._find_first_visible(
                    page, self.config.input_selectors
                )
                if input_el_after is not None:
                    logger.info("Login submitted successfully. Chat input is visible.")
                    return

                # Credentials accepted, then a code prompt appeared — auto-read it.
                code_el_after = await self._find_first_visible(page, CODE_INPUT_SELECTORS)
                if code_el_after is not None:
                    await self._complete_code_login(page, code_el_after)
                    return

                raise ArenaWebLoginRequired(
                    "Credentials were submitted, but chat input interface was not found on the page afterwards."
                )

            raise ArenaWebLoginRequired(
                "Login form was detected, but email/password input fields could not be found."
            )

    async def ensure_preferred_model(self, page: Page) -> None:
        """Real, best-effort attempt to put arena.ai on its strongest, least-
        filtered model/mode. Runs once per page lifetime; NEVER raises — a
        picker that isn't found (wrong selectors for this account, or no
        picker at all) is a silent no-op, not a fake success."""
        if self._model_confirmed:
            return
        self._model_confirmed = True  # try once; don't retry every message
        try:
            trigger = await self._find_first_visible(page, self.config.model_picker_trigger_selectors)
            if trigger is None:
                return
            await trigger.click()
            await asyncio.sleep(0.3)
            for label in self.config.preferred_model_labels:
                for opt_selector in self.config.model_picker_option_selectors:
                    try:
                        option = page.locator(opt_selector, has_text=re.compile(label, re.I)).first
                        if await option.count() > 0 and await option.is_visible():
                            await option.click()
                            logger.info("arena web: selected model matching %r", label)
                            return
                    except Exception:
                        continue
            # nothing matched — close the picker rather than leaving it open
            await page.keyboard.press("Escape")
        except Exception as exc:
            logger.debug("model-picker automation skipped: %s", exc)

    async def send_and_wait(self, message: str, timeout_s: Optional[float] = None) -> str:
        """Type message into chat input, submit, wait for assistant response to settle, and return scraped text."""
        if self.live_login_active:
            raise ArenaWebLoginRequired(
                "A manual arena.ai login session is open in Settings right now — "
                "finish or close it there, then retry."
            )
        page = await self.get_page()
        await self.ensure_logged_in(page)
        await self._accept_terms_if_present(page)
        await self.ensure_preferred_model(page)

        input_el = await self._find_first_visible(page, self.config.input_selectors)
        if input_el is None:
            snippet = await self._get_redacted_body(page)
            raise ArenaWebScrapeError(
                "Chat input element not found on page.", visible_snippet=snippet
            )

        await input_el.fill(message)

        send_btn = await self._find_first_visible(page, self.config.send_button_selectors)
        if send_btn is not None:
            await send_btn.click()
        else:
            await input_el.press("Enter")

        max_timeout = timeout_s or self.config.timeout_s
        poll_interval = self.config.poll_interval_s
        start_time = time.monotonic()

        last_text = ""
        unchanged_count = 0

        while time.monotonic() - start_time < max_timeout:
            await asyncio.sleep(poll_interval)

            if await self._captcha_detected(page):
                raise ArenaWebCaptchaRequired(
                    "arena.ai raised a reCAPTCHA 'Security Verification' challenge. "
                    "Log in with a real arena.ai account (persistent profile) or "
                    "solve the challenge once in the platform browser session; "
                    "this cannot be solved programmatically."
                )

            response_el = await self._find_last_response_element(page)
            if response_el is not None:
                raw_text = await response_el.text_content() or ""
                current_text = raw_text.strip()

                stop_btn = await self._find_first_visible(
                    page, self.config.stop_button_selectors
                )
                stop_visible = stop_btn is not None

                if current_text and current_text == last_text:
                    unchanged_count += 1
                    if unchanged_count >= 2 and not stop_visible:
                        return current_text
                else:
                    last_text = current_text
                    unchanged_count = 0

        if last_text:
            return last_text

        snippet = await self._get_redacted_body(page)
        raise ArenaWebScrapeError(
            f"Timed out ({max_timeout}s) waiting for assistant response element.",
            visible_snippet=snippet,
        )

    async def _accept_terms_if_present(self, page: Page) -> None:
        """Accept the first-run Terms of Use & Privacy Policy dialog (seen live on arena.ai)."""
        btn = await self._find_first_visible(page, self.config.terms_accept_selectors)
        if btn is not None:
            try:
                await btn.click()
                await asyncio.sleep(1.0)
            except Exception:
                pass

    async def _captcha_detected(self, page: Page) -> bool:
        """Return True when a reCAPTCHA challenge is visible on the page."""
        for selector in self.config.captcha_iframe_selectors:
            try:
                el = await page.query_selector(selector)
                if el is not None:
                    return True
            except Exception:
                continue
        try:
            body_text = (await page.inner_text("body"))[:4000]
        except Exception:
            return False
        return any(indicator in body_text for indicator in self.config.captcha_text_indicators)

    async def _find_first_visible(
        self, page: Page, selectors: list[str]
    ) -> Optional[ElementHandle]:
        """Try selectors in order and return the first matching visible element."""
        for selector in selectors:
            try:
                el = await page.query_selector(selector)
                if el is not None and await el.is_visible():
                    return el
            except Exception:
                continue
        return None

    async def _find_last_response_element(self, page: Page) -> Optional[ElementHandle]:
        """Find the last assistant message container element on the page."""
        for selector in self.config.response_selectors:
            try:
                elements = await page.query_selector_all(selector)
                if elements:
                    for el in reversed(elements):
                        if await el.is_visible():
                            return el
                    return elements[-1]
            except Exception:
                continue
        return None

    async def _get_redacted_body(self, page: Page, max_len: int = 1000) -> str:
        """Fetch visible innerText from page body and redact credentials."""
        try:
            raw_text = await page.inner_text("body")
            raw_text = raw_text[:max_len]
            secrets: list[str] = []
            if self.get_credential:
                for k in (
                    "arena_web_email",
                    "arena_web_password",
                    "arena_web_session_cookie",
                ):
                    val = self.get_credential(k)
                    if val:
                        secrets.append(val)
            return _redact_credentials(raw_text, extra_secrets=secrets)
        except Exception:
            return "[Unable to read page body]"

    async def close(self) -> None:
        """Close page, persistent context, and Playwright process cleanly.

        With a shared PersistentBrowser this is a no-op: the browser owns
        the context/pages and must outlive this session (the arena login
        lives in its profile) — only the standalone path tears down.
        """
        if self.browser is not None:
            return
        async with self._lock:
            if self._page and not self._page.is_closed():
                try:
                    await self._page.close()
                except Exception:
                    pass
                self._page = None

            if self._context and not self._context.is_closed():
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None

            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None


class ArenaWebSessionProvider(ArenaProvider):
    """ArenaProvider implementation driving a headless browser session against arena.ai chat UI."""

    def __init__(
        self,
        config: Optional[WebSessionConfig] = None,
        get_credential: Optional[Callable[[str], Optional[str]]] = None,
        browser: Optional["PersistentBrowser"] = None,
    ) -> None:
        self.config = config or WebSessionConfig()
        self.get_credential = get_credential
        self.session = ArenaWebSession(self.config, get_credential=self.get_credential,
                                       browser=browser)

    def _build_prompt(self, messages: Sequence[ChatMessage]) -> str:
        """Extract or compose the user prompt string from chat message history."""
        if not messages:
            return ""

        system_msgs = [m.content for m in messages if m.role == "system"]
        user_msgs = [m.content for m in messages if m.role == "user"]

        if user_msgs:
            last_user = user_msgs[-1]
            if system_msgs:
                return f"[System Instructions: {' '.join(system_msgs)}]\n\n{last_user}"
            return last_user

        return messages[-1].content

    async def complete(self, request: CompleteRequest) -> ModelResponse:
        """Perform a blocking completion by sending prompt through the browser session."""
        prompt = self._build_prompt(request.messages)
        scraped_text = await self.session.send_and_wait(
            prompt, timeout_s=self.config.timeout_s
        )
        model_name = f"arena-web:{self.config.name}"
        return ModelResponse(
            content=scraped_text,
            model=model_name,
            usage=None,
            raw={"transport": "playwright_browser", "chat_url": self.config.chat_url},
        )

    async def stream(self, request: CompleteRequest) -> AsyncIterator[StreamEvent]:
        """Stream token deltas by polling the page response container as text is rendered."""
        prompt = self._build_prompt(request.messages)
        page = await self.session.get_page()
        await self.session.ensure_logged_in(page)

        input_el = await self.session._find_first_visible(
            page, self.config.input_selectors
        )
        if input_el is None:
            snippet = await self.session._get_redacted_body(page)
            yield StreamEvent(
                kind="error",
                delta=f"Chat input element not found: {snippet}",
            )
            return

        await input_el.fill(prompt)

        send_btn = await self.session._find_first_visible(
            page, self.config.send_button_selectors
        )
        if send_btn is not None:
            await send_btn.click()
        else:
            await input_el.press("Enter")

        start_time = time.monotonic()
        max_timeout = self.config.timeout_s
        poll_interval = self.config.poll_interval_s

        last_len = 0
        last_text = ""
        unchanged_count = 0

        while time.monotonic() - start_time < max_timeout:
            await asyncio.sleep(poll_interval)

            if await self._captcha_detected(page):
                raise ArenaWebCaptchaRequired(
                    "arena.ai raised a reCAPTCHA 'Security Verification' challenge. "
                    "Log in with a real arena.ai account (persistent profile) or "
                    "solve the challenge once in the platform browser session; "
                    "this cannot be solved programmatically."
                )

            response_el = await self.session._find_last_response_element(page)
            if response_el is not None:
                raw_text = await response_el.text_content() or ""
                current_text = raw_text.strip()

                if len(current_text) > last_len:
                    delta = current_text[last_len:]
                    yield StreamEvent(kind="token", delta=delta)
                    last_len = len(current_text)
                    last_text = current_text
                    unchanged_count = 0
                elif current_text == last_text and current_text != "":
                    unchanged_count += 1
                    stop_btn = await self.session._find_first_visible(
                        page, self.config.stop_button_selectors
                    )
                    if unchanged_count >= 2 and stop_btn is None:
                        break

        yield StreamEvent(
            kind="done", delta="", data={"model": f"arena-web:{self.config.name}"}
        )

    async def close(self) -> None:
        """Clean up browser context and Playwright resources."""
        await self.session.close()

    async def __aenter__(self) -> ArenaWebSessionProvider:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
