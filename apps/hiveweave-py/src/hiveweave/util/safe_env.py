"""Whitelist filter for subprocess environments (no API keys / secrets)."""

from __future__ import annotations

import os

# Shared by MCP stdio spawn and alarm scripts (audit T1#9).
SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "USERNAME", "USERPROFILE",
    "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
    "LANG", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING",
    "NODE_PATH",
    "PROJECT_NAME", "PROJECT_ID",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
})


def filtered_environ(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Parent env ∩ SAFE_ENV_KEYS, then overlay *extra* (server-specific)."""
    out = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in SAFE_ENV_KEYS or k in SAFE_ENV_KEYS
    }
    if extra:
        out.update({str(k): str(v) for k, v in extra.items()})
    return out
