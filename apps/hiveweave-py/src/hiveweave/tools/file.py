"""File tool — read_file / write_file / list_files with sandbox.

契约 02: 工具执行器 — file 子模块
- read_file: 行号格式输出，支持 offset+limit，二进制检测拒绝
- write_file: 自动创建父目录，覆盖写入
- list_files: 列出目录条目，标 [DIR]/[FILE] + 大小
- 路径沙箱：所有路径必须解析到 workspace_path 内
- 敏感文件保护：拒绝 .env / *.pem / id_rsa / credentials 等（写入和读取）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ── Constants ───────────────────────────────────────────────

MAX_READ_LINES = 2000
MAX_LINE_LENGTH = 2000
BINARY_PROBE_SIZE = 8192
MAX_LIST_FILES = 1000
MAX_READ_BYTES = 10 * 1024 * 1024  # 10MB — 大文件读取上限（R5），防止 OOM

# Directories to skip in list_files (common build/dep dirs)
IGNORED_DIRS = frozenset({
    "node_modules", ".git", ".svn", ".hg", "__pycache__",
    "dist", "build", "target", ".next", ".nuxt", ".turbo",
    ".cache", "coverage", ".idea", ".vscode",
})

# HiveWeave system directory — agents must never touch it
HIVEWEAVE_DIR = ".hiveweave"

# Sensitive file patterns — blocked from read/write
SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\.env(\..+)?$", re.I),
    re.compile(r"^id_rsa(\.pub)?$", re.I),
    re.compile(r"^id_ed25519(\.pub)?$", re.I),
    re.compile(r"^.*\.pem$", re.I),
    re.compile(r"^.*\.p12$", re.I),
    re.compile(r"^.*\.pfx$", re.I),
    re.compile(r"^credentials(\.json)?$", re.I),
    re.compile(r"^.*\.key$", re.I),
    re.compile(r"^\.htpasswd$", re.I),
    re.compile(r"^shadow$", re.I),
    re.compile(r"^.*\.keystore$", re.I),
    re.compile(r"^token(\.json)?$", re.I),
    re.compile(r"^secrets?(\.json|\.ya?ml)?$", re.I),
    re.compile(r"^\.npmrc$", re.I),
    re.compile(r"^\.pypirc$", re.I),
    re.compile(r"^netrc$", re.I),
    re.compile(r"^\.aws[\\/]credentials$", re.I),
]


# ── Path security ──────────────────────────────────────────

def _resolve_safe(workspace_path: str, file_path: str) -> str | None:
    """Resolve file_path against workspace and ensure it stays inside.

    Returns the absolute path string, or None if the path escapes the sandbox.
    """
    if not file_path:
        return None
    try:
        ws = Path(workspace_path).resolve()
        # Disallow absolute paths that point outside workspace
        candidate = Path(file_path)
        if candidate.is_absolute():
            try:
                rel = candidate.relative_to(ws)
                full = ws / rel
            except ValueError:
                return None
        else:
            full = (ws / file_path).resolve()
        # Re-check via relative_to
        if full != ws:
            try:
                full.relative_to(ws)
            except ValueError:
                return None
        return str(full)
    except (OSError, ValueError):
        return None


def _check_hiveweave_dir(abs_path: str, workspace_path: str) -> bool:
    """Return True if the path targets protected .hiveweave internals.

    Agent work files (.hiveweave/reports/, .hiveweave/drafts/, etc.) are
    ALLOWED. Only system internals (data.db, tool_outputs/) are blocked.
    """
    try:
        ws = Path(workspace_path).resolve()
        hw_root = ws / HIVEWEAVE_DIR
        target = Path(abs_path).resolve()
        try:
            target.relative_to(hw_root)
        except ValueError:
            return False  # Not in .hiveweave — allowed
        # Block only system-critical internals
        protected = {
            hw_root / "data.db",
            hw_root / "data.db-shm",
            hw_root / "data.db-wal",
            hw_root / "tool_outputs",
        }
        if target in protected:
            return True
        try:
            target.relative_to(hw_root / "tool_outputs")
            return True  # Inside tool_outputs/ — system-managed
        except ValueError:
            pass
        return False  # Inside .hiveweave but not protected — allowed
    except (OSError, ValueError):
        return False


def _is_sensitive(file_path: str) -> bool:
    """Return True if the basename matches a sensitive file pattern."""
    base = Path(file_path).name
    return any(p.search(base) for p in SENSITIVE_PATTERNS)


def _is_binary(abs_path: str) -> bool:
    """Probe the file for null bytes (binary detection)."""
    try:
        with open(abs_path, "rb") as fh:
            chunk = fh.read(BINARY_PROBE_SIZE)
        return b"\x00" in chunk
    except OSError:
        return False


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1_048_576:
        return f"{size / 1024:.1f}KB"
    return f"{size / 1_048_576:.1f}MB"


# ── Public tool functions ──────────────────────────────────

async def read_file(
    file_path: str,
    offset: int,
    limit: int,
    workspace_path: str,
) -> dict[str, Any]:
    """Read a file with line numbers. Refuses binary files.

    Returns {success, output, error} where output is line-numbered text.
    """
    if not file_path:
        return {"success": False, "output": "",
                "error": "Error: filePath is required"}

    full = _resolve_safe(workspace_path, file_path)
    if full is None:
        return {"success": False, "output": "",
                "error": f'Error: Sandbox violation — "{file_path}" '
                         "outside workspace"}

    if _check_hiveweave_dir(full, workspace_path):
        return {"success": False, "output": "",
                "error": 'Error: Access denied: ".hiveweave" is the '
                         "HiveWeave system directory."}

    if _is_sensitive(file_path):
        return {"success": False, "output": "",
                "error": f'Error: Access denied: "{file_path}" matches a '
                         "sensitive file pattern."}

    p = Path(full)
    if not p.exists():
        return {"success": False, "output": "",
                "error": f"Error: File not found: {file_path}"}
    if p.is_dir():
        return {"success": False, "output": "",
                "error": f"Error: Path is a directory, not a file: {file_path}"}

    if _is_binary(full):
        size = p.stat().st_size
        return {"success": False, "output": "",
                "error": f"Error: Cannot display binary file ({size} bytes). "
                         "Use list_files to see metadata instead."}

    # R5: 大文件保护 — 超过 MAX_READ_BYTES 只读首块，防止一次性 read_text 导致 OOM
    size = p.stat().st_size
    truncated_note = ""
    try:
        if size > MAX_READ_BYTES:
            with open(full, "rb") as fh:
                raw = fh.read(MAX_READ_BYTES)
            content = raw.decode("utf-8", errors="replace")
            truncated_note = (
                f"\n\n⚠️ 文件较大（{_format_size(size)}），仅读取前 "
                f"{_format_size(MAX_READ_BYTES)}。请使用 offset 参数读取后续部分。"
            )
        else:
            content = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"success": False, "output": "",
                "error": f"Error: {exc}"}

    # Split keeping line semantics
    lines = content.split("\n")
    total = len(lines)

    start = max(int(offset or 0), 0)
    lim = int(limit or MAX_READ_LINES)
    if lim <= 0:
        lim = MAX_READ_LINES
    end = min(start + lim, total)

    selected = lines[start:end]

    formatted_lines: list[str] = []
    for idx, line in enumerate(selected, start=start):
        # Truncate overly long lines
        if len(line) > MAX_LINE_LENGTH:
            line = line[:MAX_LINE_LENGTH] + " ... [line truncated]"
        formatted_lines.append(f"{idx + 1}: {line}")

    body = "\n".join(formatted_lines)
    suffix = f"\n\n(Showing lines {start + 1}-{end} of {total})"
    if truncated_note:
        suffix = truncated_note + suffix
    return {"success": True, "output": body + suffix, "error": None}


async def write_file(
    file_path: str,
    content: str,
    workspace_path: str,
) -> dict[str, Any]:
    """Write a file (overwrite). Auto-creates parent directories."""
    if not file_path:
        return {"success": False, "output": "",
                "error": "Error: filePath is required"}
    if content is None:
        return {"success": False, "output": "",
                "error": "Error: content is required"}

    full = _resolve_safe(workspace_path, file_path)
    if full is None:
        return {"success": False, "output": "",
                "error": f'Error: Sandbox violation — "{file_path}" '
                         "outside workspace"}

    if _check_hiveweave_dir(full, workspace_path):
        return {"success": False, "output": "",
                "error": 'Error: Access denied: ".hiveweave" is the '
                         "HiveWeave system directory."}

    if _is_sensitive(file_path):
        return {"success": False, "output": "",
                "error": f'Error: Access denied: "{file_path}" matches a '
                         "sensitive file pattern."}

    p = Path(full)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Normalize to LF — prevents CRLF/LF mismatch breaking grep
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        p.write_text(normalized, encoding="utf-8", newline="")
    except OSError as exc:
        return {"success": False, "output": "",
                "error": f"Error: {exc}"}

    size = len(content.encode("utf-8"))
    log.info("file.write", path=file_path, bytes=size)
    return {"success": True,
            "output": f"Wrote {file_path} ({size} bytes)", "error": None}


async def list_files(
    path: str,
    workspace_path: str,
    recursive: bool = False,
    maxdepth: int = 1,
) -> dict[str, Any]:
    """List directory contents with [DIR]/[FILE] tags and sizes.

    BUG-019 修复：支持 recursive + maxdepth 参数，让 CEO 一次看多层目录，
    避免反复调 list_files 探索不同目录导致首次 chat 30s+。
    """
    ws = workspace_path or "."
    depth = max(1, min(maxdepth, 3)) if recursive else 1

    if path:
        full = _resolve_safe(workspace_path, path)
        if full is None:
            return {"success": False, "output": "",
                    "error": "Error: Sandbox violation - "
                             "path must be within workspace"}
    else:
        full = str(Path(ws).resolve())

    p = Path(full)
    if not p.exists():
        return {"success": False, "output": "",
                "error": f"Error: Directory not found: {path}"}
    if not p.is_dir():
        return {"success": False, "output": "",
                "error": f"Error: Not a directory: {path}"}

    lines: list[str] = []
    count = 0

    def _walk(d: Path, prefix: str, current_depth: int):
        nonlocal count
        try:
            entries = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except OSError as exc:
            lines.append(f"{prefix}(error: {exc})")
            return
        for entry in entries:
            if count >= MAX_LIST_FILES:
                if not any("truncated" in l for l in lines[-1:]):
                    lines.append(f"... (truncated at {MAX_LIST_FILES} entries)")
                return
            if entry.is_dir() and entry.name in IGNORED_DIRS:
                continue
            try:
                rel = entry.relative_to(p)
                indent = "  " * (current_depth - 1)
                if entry.is_dir():
                    lines.append(f"{indent}[DIR]  {rel}/")
                    count += 1
                    if current_depth < depth:
                        _walk(entry, prefix, current_depth + 1)
                elif entry.is_file():
                    size = entry.stat().st_size
                    lines.append(f"{indent}[FILE] {rel} ({_format_size(size)})")
                    count += 1
                else:
                    lines.append(f"{indent}[???]  {rel}")
                    count += 1
            except OSError:
                continue

    _walk(p, "", 1)

    body = "\n".join(lines) if lines else "(empty directory)"
    return {"success": True, "output": body, "error": None}
