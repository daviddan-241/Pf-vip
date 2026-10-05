"""arena-vip service tests: auth, OpenAI shapes, streaming, session
persistence, auto-new-thread retry. Provider is stubbed; the honest 503
paths (no credentials / captcha) assert real behavior, not fakes."""
import importlib
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="arenvip-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["OPERATOR_PASSWORD"] = "op-pass-1"
os.environ["ARENA_EMAIL"] = "a@b.co"
os.environ["ARENA_PASSWORD"] = "pw-123"

from fastapi.testclient import TestClient

from server import store


class _StubProvider:
    def __init__(self, fail_first=None):
        self.calls = []
        self.fail_first = fail_first

    async def complete(self, request):
        self.calls.append(request)
        if self.fail_first:
            exc, self.fail_first = self.fail_first, None
            raise exc
        from server.base import ModelResponse
        return ModelResponse(content="arena says hi", model="arena-web:stub")


@pytest.fixture
def client(monkeypatch):
    store.init_db()
    import server.app as appmod
    appmod._PROVIDER = _StubProvider()
    with TestClient(appmod.app) as c:
        yield c, appmod


def _key(client, name="t"):
    c, _ = client
    r = c.post("/api/keys", json={"name": name},
               headers={"X-Operator-Password": "op-pass-1"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_healthz(client):
    c, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["arena_email"] is True


def test_key_mint_requires_operator_password(client):
    c, _ = client
    assert c.post("/api/keys", json={"name": "x"}).status_code == 401
    assert c.post("/api/keys", json={"name": "x"},
                  headers={"X-Operator-Password": "wrong"}).status_code == 401


def test_key_prefix_and_auth(client):
    token = _key(client)
    assert token.startswith("sk-arena-")
    c, _ = client
    body = {"model": "arena-web",
            "messages": [{"role": "user", "content": "hi"}]}
    assert c.post("/v1/chat/completions", json=body).status_code == 401
    bad = c.post("/v1/chat/completions", json=body,
                 headers={"Authorization": "Bearer sk-arena-fake"})
    assert bad.status_code == 401
    ok = c.post("/v1/chat/completions", json=body,
                headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200, ok.text


def test_models_openai_shape(client):
    token = _key(client)
    c, _ = client
    r = c.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "arena-web"


def test_completion_persists_session(client):
    token = _key(client)
    c, _ = client
    body = {"model": "arena-web",
            "messages": [{"role": "user", "content": "remember this"}],
            "user": "thread-abc"}
    r = c.post("/v1/chat/completions", json=body,
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "arena says hi"
    detail = c.get("/api/sessions/thread-abc",
                   headers={"Authorization": f"Bearer {token}"})
    msgs = detail.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "arena says hi"


def test_auto_new_thread_retry(client):
    """A stale/scrape error mid-turn triggers ONE fresh-thread retry."""
    token = _key(client)
    c, appmod = client
    from server.arena_web import ArenaWebScrapeError
    appmod._PROVIDER = _StubProvider(fail_first=ArenaWebScrapeError("stale thread"))
    appmod._new_arena_thread = AsyncMock()
    body = {"model": "arena-web",
            "messages": [{"role": "user", "content": "hi"}]}
    r = c.post("/v1/chat/completions", json=body,
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "arena says hi"
    assert appmod._new_arena_thread.await_count == 1
    assert len(appmod._PROVIDER.calls) == 2  # failed attempt + retry


def test_captcha_not_retried(client):
    """Captcha challenges surface honestly — never retried, never solved."""
    token = _key(client)
    c, appmod = client
    from server.arena_web import ArenaWebCaptchaRequired
    appmod._PROVIDER = _StubProvider(fail_first=ArenaWebCaptchaRequired("gated"))
    appmod._new_arena_thread = AsyncMock()
    r = c.post("/v1/chat/completions",
               json={"model": "arena-web",
                     "messages": [{"role": "user", "content": "hi"}]},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "arena_captcha_required"
    assert appmod._new_arena_thread.await_count == 0


def test_key_revocation_kills_access(client):
    token = _key(client)
    c, _ = client
    kid = c.get("/api/keys", headers={"X-Operator-Password": "op-pass-1"}).json()[0]["id"]
    assert c.delete(f"/api/keys/{kid}",
                    headers={"X-Operator-Password": "op-pass-1"}).status_code == 200
    r = c.post("/v1/chat/completions",
               json={"model": "arena-web",
                     "messages": [{"role": "user", "content": "hi"}]},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_streaming_sse(client):
    token = _key(client)
    c, _ = client

    class _StreamStub:
        async def stream(self, request):
            from server.base import StreamEvent
            for tok in ("a", "b", "c"):
                yield StreamEvent(kind="token", delta=tok)
            yield StreamEvent(kind="done", data={"model": "arena-web:stub"})

    import server.app as appmod
    appmod._PROVIDER = _StreamStub()
    r = c.post("/v1/chat/completions",
               json={"model": "arena-web",
                     "messages": [{"role": "user", "content": "hi"}],
                     "stream": True},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    payload = [l[6:] for l in r.text.splitlines() if l.startswith("data: ")]
    assert payload[-1] == "[DONE]"
    deltas = "".join(json.loads(p)["choices"][0]["delta"].get("content", "")
                     for p in payload[:-1] if "choices" in json.loads(p))
    assert deltas == "abc"
    assert payload[-2] and json.loads(payload[-2])["choices"][0]["finish_reason"] == "stop"


import json
import time  # used above


def _fake_live_session():
    from unittest.mock import MagicMock
    from server.browser_session import PersistentBrowser
    sess = MagicMock()
    sess.config.chat_url = "https://arena.ai/agent"
    sess.live_login_active = False
    page = MagicMock()
    page.url = "https://arena.ai/agent"
    page.set_viewport_size = AsyncMock()
    page.mouse.click = AsyncMock()
    page.mouse.move = AsyncMock()
    page.mouse.down = AsyncMock()
    page.mouse.up = AsyncMock()
    page.mouse.wheel = AsyncMock()
    page.keyboard.type = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.goto = AsyncMock()
    page.reload = AsyncMock()
    page.go_back = AsyncMock()
    cdp = MagicMock(); cdp.send = AsyncMock()
    page.context.new_cdp_session = AsyncMock(return_value=cdp)
    sess.get_page = AsyncMock(return_value=page)
    sess._live_page = page
    return sess, page


def test_index_and_manifest_served(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "arena-vip" in r.text
    assert "/manifest.json" in r.text and "007AFF" in r.text
    m = c.get("/manifest.json")
    assert m.status_code == 200 and m.json()["display"] == "standalone"


def test_ws_live_streams_and_taps(client):
    """The live view attaches to the SAME page the API uses and relays taps."""
    c, appmod = client
    sess, page = _fake_live_session()
    appmod._PROVIDER = MagicMock()
    appmod._PROVIDER.session = sess
    with c.websocket_connect("/ws/live") as ws:
        assert ws.receive_json()["type"] == "status"
        assert ws.receive_json()["type"] == "url"
        ws.send_json({"type": "click", "x": 240, "y": 400})
        deadline = time.monotonic() + 2.0
        while page.mouse.click.await_count < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        page.mouse.click.assert_awaited_once_with(240, 400)


def test_live_guard_flag_set_during_session(client):
    """While the operator drives the arena tab, live_login_active flips on
    (automated turns defer instead of fighting over the tab)."""
    c, appmod = client
    sess, page = _fake_live_session()
    appmod._PROVIDER = MagicMock()
    appmod._PROVIDER.session = sess
    with c.websocket_connect("/ws/live") as ws:
        ws.receive_json(); ws.receive_json()
        assert sess.live_login_active is True
    time.sleep(0.1)
    assert sess.live_login_active is False
