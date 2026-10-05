"""SQLite persistence: API keys, session transcripts. Stdlib-only, WAL mode.

This is what makes the service survive restarts and redeploys for free:
keys and every exchanged message live in one small file (mount it on a
persistent volume if the host has one; Render free tier is ephemeral — the
arena.ai login survives via ARENA_EMAIL/ARENA_PASSWORD env, not cookies).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from .compat import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_used_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_thread ON sessions(thread_id, id);
"""


def _db() -> sqlite3.Connection:
    path = Path(get_settings().data_dir) / "service.db"
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _db() as conn:
        conn.executescript(_SCHEMA)


def mint_key(name: str) -> str:
    """Create an OpenAI-style key (sk-arena-…). Plaintext returned exactly once."""
    token = "sk-arena-" + secrets.token_hex(24)
    with _db() as conn:
        conn.execute("INSERT INTO api_keys (name, key_hash, created_at) VALUES (?,?,?)",
                     (name, hashlib.sha256(token.encode()).hexdigest(), time.time()))
    return token


def verify_key(token: str) -> bool:
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _db() as conn:
        row = conn.execute("SELECT id FROM api_keys WHERE key_hash=?", (digest,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time(), row["id"]))
    return True


def list_keys() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT id, name, created_at, last_used_at FROM api_keys "
                            "ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def revoke_key(key_id: int) -> bool:
    with _db() as conn:
        cur = conn.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
    return cur.rowcount > 0


def save_message(thread_id: str, role: str, content: str, model: str = "") -> None:
    with _db() as conn:
        conn.execute("INSERT INTO sessions (thread_id, role, content, model, ts) "
                     "VALUES (?,?,?,?,?)",
                     (thread_id, role, content, model, time.time()))


def thread_history(thread_id: str) -> list[dict]:
    """Saved transcript for one logical session (thread)."""
    with _db() as conn:
        rows = conn.execute("SELECT role, content, model, ts FROM sessions "
                            "WHERE thread_id=? ORDER BY id", (thread_id,)).fetchall()
    return [dict(r) for r in rows]


def list_threads() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT thread_id, COUNT(*) AS turns, MAX(ts) AS last_ts FROM sessions "
            "GROUP BY thread_id ORDER BY last_ts DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]
