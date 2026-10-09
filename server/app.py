"""arena-vip: a self-contained, free-hostable OpenAI-compatible API over the
real arena.ai web session.

Deploy anywhere Python runs (Render free tier, a $0 HF Space, a Raspberry Pi)
— it keeps working regardless of any other platform's credits. One shared
headless Chromium drives YOUR logged-in arena.ai account (persistent profile +
ARENA_EMAIL/ARENA_PASSWORD auto re-login), and the whole thing is exposed as:

    POST /v1/chat/completions     (OpenAI body + SSE streaming, sk-arena- keys)
    GET  /v1/models
    GET  /api/sessions            (saved transcripts — every message persisted)
    POST /api/keys                (operator password → mint an sk-arena- key)

"Knows how to auto get new ones": when an arena thread goes stale or hits a
scrape/session error mid-turn, the service starts a FRESH arena thread and
retries the turn once. Captcha challenges are never auto-solved — surfaced
honestly as 503 arena_captcha_required.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
import json
import os
import secrets
import time
import uuid
from typing import Any, AsyncIterator, Optional, List

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import store
from .compat import get_logger
from .security import redact

logger = get_logger(__name__)

OPERATOR_PASSWORD = os.environ.get("OPERATOR_PASSWORD", "")
ARENA_EMAIL = os.environ.get("ARENA_EMAIL", "")
ARENA_PASSWORD = os.environ.get("ARENA_PASSWORD", "")

app = FastAPI(title="arena-vip", docs_url="/docs")

_chat_lock = asyncio.Lock()   # one arena tab = one conversation at a time


# --------------------------------------------------------------------------- #
# Arena provider construction (lazy: Chromium launches on first completion)
# --------------------------------------------------------------------------- #

def _build_provider():
    """Real web-session provider over the logged-in arena.ai account."""
    from .arena_web import ArenaWebSessionProvider, WebSessionConfig
    from .browser_session import PersistentBrowser
    from .compat import get_settings

    def get_credential(name: str) -> Optional[str]:
        if name == "arena_web_email":
            return ARENA_EMAIL or None
        if name == "arena_web_password":
            return ARENA_PASSWORD or None
        if name == "arena_web_session_cookie":
            return os.environ.get("ARENA_SESSION_COOKIE") or None
        return None

    browser = PersistentBrowser(
        profile_dir=get_settings().data_dir / "browser_profile", headless=True)
    provider = ArenaWebSessionProvider(WebSessionConfig(), get_credential, browser=browser)
    return provider


_PROVIDER = None


def get_provider():
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = _build_provider()
    return _PROVIDER


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def _operator_auth(x_operator_password: Optional[str] = Header(None)) -> None:
    """Key-mint endpoint auth: header X-Operator-Password (or none if unset)."""
    if not OPERATOR_PASSWORD:
        raise HTTPException(503, "OPERATOR_PASSWORD not set — configure the service "
                                 "env before managing keys")
    if not x_operator_password or hashlib.sha256(
            x_operator_password.encode()).hexdigest() != hashlib.sha256(
            OPERATOR_PASSWORD.encode()).hexdigest():
        raise HTTPException(401, "wrong operator password")


def _bearer_auth(request: Request) -> str:
    """OpenAI-style Bearer key auth for /v1 routes."""
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "missing API key: send 'Authorization: Bearer sk-arena-…'")
    token = header[7:].strip()
    if not store.verify_key(token):
        raise HTTPException(401, "invalid API key")
    return token


# --------------------------------------------------------------------------- #
# OpenAI shapes
# --------------------------------------------------------------------------- #

class OAIMessage(BaseModel):
    role: str = "user"
    content: str = ""


class OAIChatRequest(BaseModel):
    model: str = "arena-web"
    messages: list[OAIMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    n: Optional[int] = None
    stop: Optional[Any] = None
    user: Optional[str] = None


def _oai_error(status: int, message: str, etype: str, code: Optional[str] = None):
    return JSONResponse(status_code=status,
                        content={"error": {"message": redact(message),
                                           "type": etype, "code": code}})


def _short_reason(exc: Exception) -> str:
    first = (str(exc).strip().splitlines() or [""])[0][:300]
    return redact(f"{type(exc).__name__}: {first}" if first else type(exc).__name__)


def _map_error(exc: Exception):
    name = type(exc).__name__
    if name == "ArenaWebCaptchaRequired":
        return _oai_error(503, str(exc), "arena_captcha_required", "arena_captcha_required")
    if name == "ArenaWebLoginRequired":
        return _oai_error(503, str(exc), "arena_login_required", "arena_login_required")
    if name == "ArenaWebScrapeError":
        return _oai_error(502, str(exc), "arena_scrape_failed", "arena_scrape_failed")
    logger.exception("arena transport failed")
    reason = _short_reason(exc)
    hint = ""
    low = reason.lower()
    if "shared libraries" in low or "libnspr" in low or "targetclosed" in low or "launch" in low:
        hint = (" Chromium could not launch — install Playwright deps: "
                "`python -m playwright install --with-deps chromium`")
    return _oai_error(503, f"arena transport failed: {reason}{hint}",
                      "arena_unavailable", "arena_unavailable")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


async def _new_arena_thread() -> None:
    """'Auto get new ones': open a fresh arena.ai chat thread."""
    provider = get_provider()
    from .arena_web import WebSessionConfig
    cfg: WebSessionConfig = provider.config
    page = await provider.session.get_page()
    await page.goto(cfg.chat_url, wait_until="domcontentloaded")
    await asyncio.sleep(1.0)  # let the composer mount


async def _complete_with_retry(messages, temperature, max_tokens):
    """One completion; on a stale/scraped thread error, start a fresh arena
    thread once and retry. Captcha/login errors are never retried blindly."""
    from .base import ArenaEndpoint, ChatMessage, CompleteRequest
    provider = get_provider()
    request = CompleteRequest(
        endpoint=ArenaEndpoint(name="arena-web", base_url="https://arena.ai"),
        messages=[ChatMessage(role=m.role, content=m.content) for m in messages],
        temperature=temperature, max_tokens=max_tokens)

    try:
        async with _chat_lock:
            return await provider.complete(request)
    except Exception as exc:
        name = type(exc).__name__
        if name in ("ArenaWebCaptchaRequired", "ArenaWebLoginRequired"):
            raise
        logger.warning("arena turn failed (%s) — starting a fresh thread and "
                       "retrying once", name)
        async with _chat_lock:
            await _new_arena_thread()
            return await provider.complete(request)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "arena_email": bool(ARENA_EMAIL),
            "operator_set": bool(OPERATOR_PASSWORD)}


@app.post("/api/keys")
def mint_key(body: dict, x_operator_password: Optional[str] = Header(None)):
    """Operator mints an sk-arena-… key. Plaintext returned exactly once."""
    _operator_auth(x_operator_password)
    name = str(body.get("name", "key"))[:64]
    token = store.mint_key(name)
    return {"name": name, "token": token}


@app.get("/api/keys")
def keys(x_operator_password: Optional[str] = Header(None)):
    _operator_auth(x_operator_password)
    return store.list_keys()


@app.delete("/api/keys/{key_id}")
def revoke(key_id: int, x_operator_password: Optional[str] = Header(None)):
    _operator_auth(x_operator_password)
    return {"revoked": key_id} if store.revoke_key(key_id) else (
        _oai_error(404, "no such key", "invalid_request_error", "key_not_found"))


@app.get("/v1/models")
def models(request: Request):
    _bearer_auth(request)
    return {"object": "list", "data": [{
        "id": "arena-web", "object": "model", "created": 0,
        "owned_by": "arena-vip-web-session"}]}


class DriverStep(BaseModel):
    """One DOM-precise action executed on the live arena.ai tab."""
    op: str
    text: Optional[str] = None
    selector: Optional[str] = None
    value: Optional[str] = None
    key: Optional[str] = None
    ms: int = 1500
    url: Optional[str] = None


async def run_driver(steps: list) -> dict:
    """Execute locator-based steps on the provider's arena page (DOM-precise).

    ops: goto, click_text, click_selector, fill (by placeholder/label/selector),
    press, wait, read (text content), eval_ready.
    """
    from playwright.async_api import TimeoutError as PWTimeout
    page = await get_provider().session.get_page()
    results = []
    for i, st in enumerate(steps):
        try:
            if st.op == "goto":
                await page.goto(st.url or st.text or "", wait_until="domcontentloaded")
                res = {"ok": True, "url": page.url}
            elif st.op == "click_text":
                loc = page.get_by_text(st.text, exact=False).first
                await loc.click(timeout=8000)
                res = {"ok": True}
            elif st.op == "click_role":
                loc = page.get_by_role("button", name=st.text).first
                await loc.click(timeout=8000)
                res = {"ok": True}
            elif st.op == "click_selector":
                await page.locator(st.selector).first.click(timeout=8000)
                res = {"ok": True}
            elif st.op == "fill_placeholder":
                loc = page.get_by_placeholder(st.text).first
                await loc.fill(st.value or "", timeout=8000)
                res = {"ok": True}
            elif st.op == "fill_selector":
                await page.locator(st.selector).first.fill(st.value or "", timeout=8000)
                res = {"ok": True}
            elif st.op == "press":
                await page.keyboard.press(st.key or "Enter")
                res = {"ok": True}
            elif st.op == "wait":
                await asyncio.sleep(max(0.1, st.ms / 1000))
                res = {"ok": True}
            elif st.op == "buttons":
                els = await page.query_selector_all("button, [role=button], a")
                out = []
                for el in els[:60]:
                    info = await el.evaluate(
                        "e => ({tag: e.tagName, aria: e.getAttribute('aria-label'), "
                        "id: e.id || null, cls: (e.className || '').toString().slice(0, 80), "
                        "txt: (e.innerText || '').slice(0, 40).replace(/\\s+/g, ' ').trim(), "
                        "vis: !!(e.offsetWidth || e.offsetHeight)})")
                    if info.get("vis"):
                        out.append(info)
                res = {"ok": True, "buttons": out}
            elif st.op == "shot":
                buf = await page.screenshot(type="jpeg", quality=55,
                                           clip={"x": 0, "y": 0, "width": 640, "height": 480}
                                           ) if page.viewport_size and page.viewport_size["width"] > 640 \
                    else await page.screenshot(type="jpeg", quality=55)
                res = {"ok": True, "jpeg_len": len(buf),
                       "jpeg_b64": base64.b64encode(buf).decode()[:60000]}
            elif st.op == "fill_eval":
                ok = await page.evaluate(
                    "([sel, val]) => { const el = document.querySelector(sel);"
                    "if (!el) return false; const proto = el instanceof HTMLTextAreaElement"
                    " ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
                    "Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, val);"
                    "el.dispatchEvent(new Event('input', {bubbles: true}));"
                    "el.dispatchEvent(new Event('change', {bubbles: true})); return true; }",
                    [st.selector, st.value or ""])
                res = {"ok": bool(ok)}
            elif st.op == "click_eval":
                ok = await page.evaluate(
                    "arg => { const [sel, txt] = arg; if (sel) { const el ="
                    " document.querySelector(sel); if (el) { el.click(); return true; } return false; }"
                    "const btn = Array.from(document.querySelectorAll('button'))"
                    ".find(b => (b.innerText || '').trim() === txt);"
                    "if (btn) { btn.click(); return true; } return false; }",
                    [st.selector, st.text])
                res = {"ok": bool(ok)}
            elif st.op == "dialog_click":
                ok = await page.evaluate(
                    "txt => { const dlg = document.querySelector('[role=dialog]') || document.body;"
                    "const btn = Array.from(dlg.querySelectorAll('button'))"
                    ".find(b => (b.innerText || '').trim() === txt);"
                    "if (btn) { btn.click(); return true; } return false; }", st.text)
                res = {"ok": bool(ok)}
            elif st.op == "submit_form":
                ok = await page.evaluate(
                    "sel => { const el = document.querySelector(sel);"
                    "if (!el) return false; el.requestSubmit ? el.requestSubmit() : el.submit();"
                    "return true; }", st.selector or "form")
                res = {"ok": bool(ok)}
            elif st.op == "input_values":
                vals = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('input, textarea'))"
                    ".map(e => ({name: e.name || e.id || e.type, type: e.type,"
                    " val: e.value ? e.value.slice(0, 30) : '', vis: !!(e.offsetWidth || e.offsetHeight)}))")
                res = {"ok": True, "values": vals}
            elif st.op == "inputs":
                els = await page.query_selector_all("input, textarea, select, [contenteditable=true]")
                out = []
                for el in els[:40]:
                    info = await el.evaluate(
                        "e => ({tag: e.tagName, type: e.type || null, name: e.name || null, "
                        "ph: e.getAttribute('placeholder'), aria: e.getAttribute('aria-label'), "
                        "id: e.id || null, vis: !!(e.offsetWidth || e.offsetHeight)})")
                    out.append(info)
                res = {"ok": True, "inputs": out}
            elif st.op == "read":
                content = await page.inner_text("body", timeout=8000)
                res = {"ok": True, "text": content[:4000]}
            elif st.op == "read_selector":
                content = await page.locator(st.selector).first.inner_text(timeout=8000)
                res = {"ok": True, "text": content[:2000]}
            else:
                res = {"ok": False, "error": f"unknown op {st.op}"}
        except PWTimeout:
            res = {"ok": False, "error": "timeout", "op": st.op, "arg": st.text or st.selector}
        except Exception as exc:
            res = {"ok": False, "error": str(exc)[:200], "op": st.op}
        res["url"] = page.url
        results.append(res)
        if not res.get("ok") and st.op != "read":
            break
    return {"steps": results, "url": page.url}


@app.post("/api/driver")
async def driver(body: List[DriverStep], x_operator_password: Optional[str] = Header(None)):
    """Operator-gated DOM driver for the arena tab: find by text/placeholder, click, fill."""
    _operator_auth(x_operator_password)
    return await run_driver(body)


@app.get("/api/sessions")
def sessions(request: Request):
    """Saved transcripts (one entry per turn). Key-authenticated."""
    _bearer_auth(request)
    return {"threads": store.list_threads()}


@app.get("/api/sessions/{thread_id}")
def session_detail(thread_id: str, request: Request):
    _bearer_auth(request)
    return {"thread_id": thread_id, "messages": store.thread_history(thread_id)}


@app.post("/v1/chat/completions")
async def chat_completions(body: OAIChatRequest, request: Request):
    """OpenAI-compatible completion through the real arena.ai web session."""
    _bearer_auth(request)
    if not body.messages:
        return _oai_error(400, "at least one message is required",
                          "invalid_request_error", "empty_messages")

    thread_id = body.user or uuid.uuid4().hex[:12]
    chunk_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    model_label = body.model or "arena-web"

    # persist the request side of every turn (sessions survive restarts)
    store.save_message(thread_id, "user",
                       redact("\n".join(m.content for m in body.messages)))

    if not body.stream:
        try:
            resp = await _complete_with_retry(body.messages, body.temperature,
                                              body.max_tokens)
        except Exception as exc:
            return _map_error(exc)
        store.save_message(thread_id, "assistant", redact(resp.content), resp.model)
        ptok = sum(_estimate_tokens(m.content) for m in body.messages)
        ctok = _estimate_tokens(resp.content)
        return {
            "id": chunk_id, "object": "chat.completion", "created": int(time.time()),
            "model": resp.model or model_label,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": resp.content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": ptok, "completion_tokens": ctok,
                      "total_tokens": ptok + ctok},
        }

    async def event_stream() -> AsyncIterator[str]:
        full = []
        try:
            async with _chat_lock:
                async for event in get_provider().stream(_req(body)):
                    if event.kind == "token" and event.delta:
                        full.append(event.delta)
                        yield _sse_chunk(chunk_id, model_label, {"content": event.delta})
                    elif event.kind == "error":
                        yield _sse({"error": {"message": _short_reason(RuntimeError(event.delta)),
                                              "type": "arena_transport_error",
                                              "code": "arena_transport_error"}})
                        break
                    elif event.kind == "done":
                        yield _sse_chunk(chunk_id, model_label, {}, "stop",
                                         extra={"arena_model": event.data.get("model")})
                        break
        except Exception as exc:
            yield _sse({"error": {"message": _short_reason(exc),
                                  "type": type(exc).__name__,
                                  "code": "arena_transport_error"}})
        if full:
            store.save_message(thread_id, "assistant", redact("".join(full)))
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _req(body: OAIChatRequest):
    from .base import ArenaEndpoint, ChatMessage, CompleteRequest
    return CompleteRequest(
        endpoint=ArenaEndpoint(name="arena-web", base_url="https://arena.ai"),
        messages=[ChatMessage(role=m.role, content=m.content) for m in body.messages],
        temperature=body.temperature, max_tokens=body.max_tokens)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_chunk(cid: str, model: str, delta: dict, finish: Optional[str] = None,
               extra: Optional[dict] = None) -> str:
    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
             "model": model,
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if extra:
        chunk.update({k: v for k, v in extra.items() if v is not None})
    return _sse(chunk)


# --------------------------------------------------------------------------- #
# Mobile web app (PWA) + live captcha-solving view
# --------------------------------------------------------------------------- #

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/manifest.json", include_in_schema=False)
def manifest() -> dict:
    """PWA manifest with real icon images (home-screen install shows the icon)."""
    return {"name": "arena-vip", "short_name": "arena-vip", "start_url": "/",
            "display": "standalone", "background_color": "#FFFFFF",
            "theme_color": "#007AFF",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
                 "purpose": "any maskable"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "any maskable"},
            ]}


@app.get("/icon-{size}.png", include_in_schema=False)
def icon(size: int) -> FileResponse:
    """Serve the real PNG app icons (192/512). Unknown sizes 404 honestly."""
    path = Path(__file__).parent / "static" / f"icon-{size}.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="no such icon")
    return FileResponse(path, media_type="image/png")


@app.get("/apple-touch-icon.png", include_in_schema=False)
def apple_touch_icon() -> FileResponse:
    """iOS home-screen icon (Apple ignores manifest icons for Add to Home Screen)."""
    return FileResponse(Path(__file__).parent / "static" / "apple-touch-icon.png",
                        media_type="image/png")


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    """Stream THE arena tab the API sends on. A reCAPTCHA raised during an
    API turn appears here live; the operator taps it through like a human."""
    from .live import run_live_login
    provider = get_provider()
    await run_live_login(websocket, provider.session)


@app.on_event("startup")
def _startup() -> None:
    store.init_db()
    logger.info("arena-vip up — arena_email=%s operator_password=%s keys persisted in %s",
                bool(ARENA_EMAIL), bool(OPERATOR_PASSWORD),
                get_settings_data_dir())


def get_settings_data_dir():
    from .compat import get_settings
    return get_settings().data_dir
