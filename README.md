# arena-vip — arena.ai as a permanent, free OpenAI-compatible API

A self-contained FastAPI service that drives the **real arena.ai web UI**
with your own logged-in account (headless Chromium, persistent profile) and
exposes it as a standard OpenAI-style API with `sk-arena-…` keys.

- **Works with any OpenAI client**: point `base_url` at this service, put the
  minted key in the `Authorization: Bearer` header.
- **Survives anything**: keys + full session transcripts in one SQLite file;
  the arena login survives redeploys via `ARENA_EMAIL`/`ARENA_PASSWORD`
  (auto re-login). Host it free on Render / HuggingFace Spaces / any box —
  it has no dependency on any other platform's credits.
- **Auto new sessions**: when an arena thread goes stale mid-turn, the
  service opens a fresh thread and retries the turn once. Captcha gates are
  never auto-solved — they surface as honest 503 `arena_captcha_required`.
- **Every turn is saved**: `GET /api/sessions` lists threads,
  `GET /api/sessions/{id}` replays the transcript.

## Env
| var | purpose |
|---|---|
| `OPERATOR_PASSWORD` | password for minting/managing keys |
| `ARENA_EMAIL` / `ARENA_PASSWORD` | your arena.ai login (auto re-login) |
| `ARENA_SESSION_COOKIE` | optional: reuse a logged-in cookie instead |
| `DATA_DIR` | SQLite + browser profile location (default `./data`) |

## Use
```
# mint a key (once)
curl -X POST $BASE/api/keys -H "X-Operator-Password: $OP" -d '{"name":"main"}'
# then, like OpenAI:
curl $BASE/v1/chat/completions -H "Authorization: Bearer sk-arena-…" \
  -H "Content-Type: application/json" \
  -d '{"model":"arena-web","messages":[{"role":"user","content":"hello"}]}'
```
`stream: true` gives OpenAI SSE chunks ending in `data: [DONE]`.

## Tests
`python -m pytest tests/ -q` (stubbed provider, hermetic tmp DB)
