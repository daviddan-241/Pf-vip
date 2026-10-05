"""Redaction for anything headed to logs or API responses — never leak secrets."""
from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"(sk-arena-[A-Za-z0-9]{8})[A-Za-z0-9]*"),
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{6})[A-Za-z0-9]*"),
    re.compile(r"(?i)(password[\"' =:]+)(\S{4})\S*"),
    re.compile(r"(?i)(bearer\s+\S{6})\S*"),
    re.compile(r"(eyJ[A-Za-z0-9_-]{6})[A-Za-z0-9_.-]*"),
]


def redact(text: str) -> str:
    """Mask API keys, tokens, passwords in free text."""
    out = text
    for pat in _PATTERNS:
        out = pat.sub(lambda m: m.group(1) + "…", out)
    return out
