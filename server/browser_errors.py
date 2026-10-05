"""One honest, human-readable translation for browser/Playwright failures.

Playwright's own exceptions are useful in logs (full traceback, the ASCII-art
"looks like Playwright was just installed" box) but are never fit to show a
user directly — they're multi-paragraph, dev-facing, and leak local paths.
This module keeps the FULL truth (never hides that something is broken,
never fakes success) while making the *message shown to the user* one clean
sentence that says what's actually wrong and, where knowable, how to fix it.
"""
from __future__ import annotations

import re


class BrowserUnavailable(RuntimeError):
    """Raised when Playwright/Chromium isn't installed in this environment."""


_EXECUTABLE_MISSING = re.compile(r"Executable doesn't exist at", re.IGNORECASE)
_TARGET_CLOSED = re.compile(r"Target (page|context|browser) closed", re.IGNORECASE)
_TIMEOUT = re.compile(r"Timeout \d+ms exceeded", re.IGNORECASE)
_CONN_REFUSED = re.compile(r"(ERR_CONNECTION_REFUSED|ECONNREFUSED)", re.IGNORECASE)
_DNS = re.compile(r"(ERR_NAME_NOT_RESOLVED|ENOTFOUND)", re.IGNORECASE)
_NAV_ABORTED = re.compile(r"ERR_ABORTED", re.IGNORECASE)


def friendly_browser_error(exc: BaseException) -> str:
    """One clean, honest sentence for the UI. Never a stack trace, never the
    Playwright ASCII-art box, never a lie about what happened."""
    raw = str(exc)

    if _EXECUTABLE_MISSING.search(raw):
        return (
            "The server's browser isn't installed in this environment yet — "
            "Chromium is missing. This is a deployment issue, not something "
            "wrong with your login: ping the operator to run "
            "`playwright install chromium` (or redeploy — the Dockerfile now "
            "installs it at build time)."
        )
    if _TARGET_CLOSED.search(raw):
        return "The browser tab closed unexpectedly. Reopen it and try again."
    if _TIMEOUT.search(raw):
        return "The page took too long to respond. Try again — sites can be slow to load on a cold server."
    if _CONN_REFUSED.search(raw):
        return "That site refused the connection. Double-check the address and try again."
    if _DNS.search(raw):
        return "That address couldn't be found — check for a typo in the URL."
    if _NAV_ABORTED.search(raw):
        return "Navigation was interrupted — try again."

    # Unknown failure: stay honest, but trim it to one readable line instead
    # of dumping a full traceback / multi-line Playwright box into the chat.
    first_line = raw.strip().splitlines()[0] if raw.strip() else "unknown error"
    first_line = first_line[:180]
    return f"Browser error: {first_line}"
