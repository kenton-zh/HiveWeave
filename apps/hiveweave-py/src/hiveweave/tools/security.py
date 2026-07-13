"""Shared security utilities for tool implementations.

契约 02: 工具执行器 — 安全共享模块
- 统一敏感文件路径检查（.env / *.pem / id_rsa / credentials / *.key 等）
- 供 file.py / patch / grep / review 等所有工具复用
- file.py 的 _is_sensitive 调用本模块作为 path-level 补充检查
"""

from __future__ import annotations

import re

# ── 敏感文件模式 ────────────────────────────────────────────
# 子串匹配（非全路径锚定），刻意偏宽：宁可误拦不可漏放。
# 合并了 file.py 原有 basename 模式中缺失的项（.pypirc, shadow, .keystore, token 等）。

SENSITIVE_PATTERNS: list[str] = [
    r"\.env$", r"\.env\.", r"id_rsa", r"id_dsa", r"id_ecdsa",
    r"id_ed25519", r"\.pem$", r"\.key$", r"\.pfx$", r"\.p12$",
    r"credentials", r"\.htpasswd", r"\.gitconfig$", r"\.npmrc$",
    r"\.netrc$", r"\.pypirc$", r"\.ssh/", r"known_hosts",
    r"authorized_keys", r"\.aws/credentials", r"\.aws/config",
    r"\.docker/config", r"settings\.py$", r"local_settings\.py$",
    r"secret", r"password", r"api[_-]?key",
    r"^shadow$", r"\.keystore$", r"^token(\.json)?$",
]

_COMPILED: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in SENSITIVE_PATTERNS
]


def is_sensitive_path(file_path: str) -> bool:
    """Check if a file path matches sensitive patterns.

    Uses case-insensitive substring search on the full path string.
    Returns True if any sensitive pattern matches.
    """
    p = str(file_path).lower()
    return any(pat.search(p) for pat in _COMPILED)


def check_sensitive_access(file_path: str, op: str = "read") -> None:
    """Raise PermissionError if path is sensitive.

    Args:
        file_path: The file path to check.
        op: Operation name for the error message (read/write/delete/...).

    Raises:
        PermissionError: If the path matches a sensitive file pattern.
    """
    if is_sensitive_path(file_path):
        raise PermissionError(
            f"Access denied: {file_path} is a sensitive file "
            f"(operation: {op})"
        )
