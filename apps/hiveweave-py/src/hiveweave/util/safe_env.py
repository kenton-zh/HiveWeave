"""Whitelist filter for subprocess environments (no API keys / secrets)."""

from __future__ import annotations

import os

# Shared by MCP stdio spawn, alarm scripts, bash, and start_dev_server.
# Match is case-insensitive via _SAFE_UPPER (Windows Path vs PATH).
SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "USERNAME", "USERPROFILE",
    "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING", "PYTHONUTF8",
    "NODE_PATH", "NODE_ENV", "PNPM_HOME",
    # SDK 根目录（目录指针，非密钥）——dev server 白名单 spawn 后发现 SDK 用；
    # 不透传会让 Java/Go/Android/.NET 类项目起不来（审计 P1）
    "JAVA_HOME", "GOROOT", "ANDROID_HOME", "ANDROID_SDK_ROOT", "DOTNET_ROOT",
    "PROJECT_NAME", "PROJECT_ID",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "USERDOMAIN", "COMPUTERNAME", "OS",
    "HOMEDRIVE", "HOMEPATH",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "SYSTEMDRIVE", "PROGRAMFILES", "PROGRAMW6432",
    "PROGRAMFILES(X86)",
})

_SAFE_UPPER = frozenset(k.upper() for k in SAFE_ENV_KEYS)
# NODE_OPTIONS can inject --require / inspect — bash/dev-server only, not MCP.
_CHILD_ONLY_UPPER = frozenset({"NODE_OPTIONS"})


def _is_safe_env_key(name: str) -> bool:
    return name.upper() in _SAFE_UPPER


def filtered_environ(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Parent env ∩ SAFE_ENV_KEYS, then overlay *extra* (server-specific)."""
    out = {k: v for k, v in os.environ.items() if _is_safe_env_key(k)}
    if extra:
        out.update({str(k): str(v) for k, v in extra.items()})
    return out


def build_child_env(cwd: str, *, bash_markers: bool = False) -> dict[str, str]:
    """Whitelist child env. Never copies *KEY* / *SECRET* parent vars.

    ``bash_markers=True`` is for the bash tool only (HIVEWEAVE_BASH + forced
    UTF-8 locale). Dev-server spawn must not stamp bash markers.
    """
    env = filtered_environ()
    for k, v in os.environ.items():
        if k.upper() in _CHILD_ONLY_UPPER:
            env[k] = v
    env["HIVEWEAVE_WORKSPACE"] = cwd
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if bash_markers:
        env["HIVEWEAVE_BASH"] = "1"
        env["LANG"] = "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"
    return env
