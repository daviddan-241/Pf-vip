"""General-purpose persistent browser session — the agent's own browser.

Separate from arenaos/arena/web_provider.py (which drives arena.ai specifically
for model access). This one is for the agent to browse, click, type, extract,
and screenshot on ANY site as part of doing real work. Same persistent-profile
pattern: cookies/logins survive restarts, Chromium launches lazily on first use.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from .compat import get_settings
from .compat import get_logger

logger = get_logger(__name__)

try:
    from playwright.async_api import BrowserContext, Page, async_playwright
except ImportError:
    BrowserContext = Any  # type: ignore
    Page = Any  # type: ignore
    async_playwright = None  # type: ignore


class BrowserUnavailable(RuntimeError):
    """Raised when Playwright/Chromium isn't installed in this environment."""


class PersistentBrowser:
    """Lazy-launched, persistent-profile Chromium the agent drives directly.

    One shared context for the whole process; pages are opened per-tool-call
    and closed after, but cookies/localStorage persist in the profile dir.
    """

    def __init__(self, profile_dir: Optional[Path] = None, headless: bool = True) -> None:
        settings = get_settings()
        self.profile_dir = Path(profile_dir) if profile_dir else settings.data_dir / "agent_browser_profile"
        self.headless = headless
        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._lock = asyncio.Lock()
        self._pages: dict[str, Page] = {}

    async def _ensure_context(self) -> BrowserContext:
        if async_playwright is None:
            raise BrowserUnavailable(
                "Playwright is not installed. Install with: pip install '.[browser]' "
                "&& python -m playwright install chromium")
        async with self._lock:
            if self._context is None:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self._playwright = await async_playwright().start()
                args = ["--disable-blink-features=AutomationControlled"]
                # Optional Tor routing (set ARENA_TOR_PROXY, e.g.
                # socks5://127.0.0.1:9050) — the whole browser then exits Tor.
                if get_settings().tor_proxy:
                    args.append(f"--proxy-server={get_settings().tor_proxy}")
                    logger.info("browser routed through tor proxy %s", get_settings().tor_proxy)
                self._context = await self._playwright.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=self.headless, args=args,
                    # Realistic desktop Chrome fingerprint so Google/Microsoft
                    # don't flag the agent's automated sessions as bots —
                    # default headless UA advertises "HeadlessChrome".
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
                    locale="en-US", timezone_id="America/New_York",
                )
                logger.info("agent browser context launched (profile=%s)", self.profile_dir)
        return self._context

    async def get_page(self, session_id: str = "default") -> Page:
        """Return (creating if needed) a named page — lets a task keep one tab open."""
        context = await self._ensure_context()
        if session_id not in self._pages or self._pages[session_id].is_closed():
            self._pages[session_id] = await context.new_page()
        return self._pages[session_id]

    async def close_page(self, session_id: str = "default") -> None:
        page = self._pages.pop(session_id, None)
        if page and not page.is_closed():
            await page.close()

    async def shutdown(self) -> None:
        async with self._lock:
            for page in self._pages.values():
                if not page.is_closed():
                    await page.close()
            self._pages.clear()
            if self._context is not None:
                await self._context.close()
                self._context = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
