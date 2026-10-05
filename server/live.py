"""Live browser view — real CDP screencast + real input relay over a WebSocket.

No demo, no simulated frames. Three session kinds, all REAL:

This is the SAME arena tab the API sends on (provider.session.get_page()):
when arena.ai raises a reCAPTCHA during an API turn, it appears here in real
time and the operator taps it through — human solves the challenge, API keeps
flowing. Same persistent profile, so logins done here are logins for the API.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

from .browser_errors import friendly_browser_error
from .compat import get_logger

if TYPE_CHECKING:
    from .arena_web import ArenaWebSession
    from .browser_session import PersistentBrowser

logger = get_logger(__name__)

FRAME_WIDTH = 480
FRAME_HEIGHT = 854
FRAME_QUALITY = 82  # was 65 — too blurry to read login forms; 82 stays fast and sharp


async def run_live_login(websocket: WebSocket, session: "ArenaWebSession",
                         start_url: str | None = None,
                         page: Any | None = None) -> None:
    """Stream the arena page (or the agent's webmail tab) and relay input.

    The arena page is `guarded`: while the operator is driving it, automated
    model queries defer instead of fighting over the same tab.
    """
    guarded = page is None
    if guarded:
        session.live_login_active = True

    async def _get_target() -> Any:
        # Resolved AFTER the socket is already accepted (see _stream_page) —
        # a slow cold-start Chromium launch now shows the operator a live
        # "waking up" status instead of looking like a dead connection.
        return page if page is not None else await session.get_page()

    try:
        start_url_eff = start_url or session.config.chat_url
        await _stream_page(websocket, _get_target, navigate_if_blank=start_url_eff)
    finally:
        if guarded:
            session.live_login_active = False


async def run_free_browser(websocket: WebSocket, browser: "PersistentBrowser",
                           start_url: str | None) -> None:
    """Stream ANY website on the operator's persistent profile.

    The named "operator" page stays alive between sessions, so the site is
    where the operator left it and logins persist in the profile dir.
    """
    async def _get_target() -> Any:
        return await browser.get_page("operator")

    await _stream_page(websocket, _get_target, navigate_if_blank=start_url,
                       force_goto=start_url)


async def page_set_viewport(page: Any) -> None:
    try:
        await page.set_viewport_size({"width": FRAME_WIDTH, "height": FRAME_HEIGHT})
    except Exception:
        pass  # viewport is nice-to-have; never block the stream on it


async def _stream_page(websocket: WebSocket, page_getter: Callable[[], Awaitable[Any]],
                       navigate_if_blank: str | None = None,
                       force_goto: str | None = None) -> None:
    """Drive one live WebSocket connection: accept FIRST — before the (possibly
    slow, cold-start) real page is even resolved — so a first-launch Chromium
    spin-up never looks like a dropped connection to the client. A "connecting"
    status goes out immediately; the frontend shows a live waking-up state
    instead of the old false "connection closed" error. Screencast starts
    second, navigate last, so the operator sees every loading frame."""
    await websocket.accept()
    cdp = None
    url_task = None
    try:
        await websocket.send_json({"type": "status", "status": "connecting"})
        page = await page_getter()
        await page_set_viewport(page)

        cdp = await page.context.new_cdp_session(page)
        cdp.on("Page.screencastFrame", lambda params: asyncio.ensure_future(
            _relay_frame(websocket, cdp, params)))
        await cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": FRAME_QUALITY,
            "maxWidth": FRAME_WIDTH, "maxHeight": FRAME_HEIGHT, "everyNthFrame": 1,
        })

        # NOW navigate (after the stream is live) — every loading frame streams.
        if force_goto and page.url != force_goto:
            await page.goto(force_goto, wait_until="commit")
        elif navigate_if_blank and page.url in ("about:blank", "", None):
            await page.goto(navigate_if_blank, wait_until="commit")

        await websocket.send_json({"type": "url", "url": page.url})
        url_task = asyncio.ensure_future(_push_url_changes(websocket, page))

        while True:
            msg = await websocket.receive_json()
            await _apply_input(page, msg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # Full detail stays in the server log; the user only ever sees one
        # clean, honest sentence — never Playwright's raw ASCII-art error box.
        logger.warning("live browser session error: %s", exc)
        try:
            await websocket.send_json({"type": "error", "error": friendly_browser_error(exc)})
        except Exception:
            pass
    finally:
        if url_task:
            url_task.cancel()
        if cdp is not None:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
        logger.info("live browser session ended")


async def _relay_frame(websocket: WebSocket, cdp: Any, params: dict) -> None:
    """Forward one screencast frame to the browser and ack it (required by CDP)."""
    try:
        await websocket.send_json({"type": "frame", "data": params["data"]})
    except Exception:
        return
    finally:
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass


async def _push_url_changes(websocket: WebSocket, page: Any) -> None:
    """Poll the real page URL and push it whenever navigation happens."""
    last = page.url
    while True:
        await asyncio.sleep(1.0)
        if page.url != last:
            last = page.url
            try:
                await websocket.send_json({"type": "url", "url": last})
            except Exception:
                return


async def _apply_input(page: Any, msg: dict) -> None:
    """Dispatch one real input event from the client into the live page."""
    kind = msg.get("type")
    try:
        if kind == "mousemove":
            await page.mouse.move(msg["x"], msg["y"])
        elif kind == "mousedown":
            await page.mouse.move(msg["x"], msg["y"])
            await page.mouse.down()
        elif kind == "mouseup":
            await page.mouse.move(msg["x"], msg["y"])
            await page.mouse.up()
        elif kind == "click":
            await page.mouse.click(msg["x"], msg["y"])
        elif kind == "type":
            await page.keyboard.type(msg.get("text", ""))
        elif kind == "key":
            await page.keyboard.press(msg.get("key", ""))
        elif kind == "scroll":
            await page.mouse.wheel(msg.get("dx", 0), msg.get("dy", 0))
        elif kind == "goto":
            await page.goto(msg.get("url", ""), wait_until="commit")
        elif kind == "back":
            await page.go_back()
        elif kind == "forward":
            await page.go_forward()
        elif kind == "reload":
            await page.reload()
    except Exception as exc:
        logger.debug("live-browser input %r failed: %s", kind, exc)
