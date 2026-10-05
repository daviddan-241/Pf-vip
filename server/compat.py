"""Minimal settings/logger shim so the vendored arena modules stay untouched."""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path


class _Settings:
    """Standalone config from env — no ArenaOS dependency."""

    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tor_proxy = os.environ.get("TOR_PROXY") or None


@lru_cache(maxsize=1)
def get_settings() -> _Settings:
    return _Settings()


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    return logging.getLogger(name)
