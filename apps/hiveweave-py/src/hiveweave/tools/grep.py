"""grep tool — regex search across files in the workspace.

契约 02: 工具执行器 — grep 子模块
- ripgrep 兼容的正则搜索（优先调用 rg，回退到 Python 内置扫描）
- 支持 glob 过滤（include 参数）、上下文行（context）、多行模式（multiline）
- 路径沙箱：search_path 必须在 workspace_path 内
- 输出按文件分组，前缀 "<file>:<line>: <content>"
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

import structlog

from hiveweave.tools.security import is_sensitive_path

log = structlog.get_logger(__name__)

# ── Constants ───────────────────────────────────────────────

MAX_RESULTS = 500
"""默认结果上限（R4）。大目录搜索结果封顶，防止海量匹配导致输出过大。"""
MAX_CHARS_PER_LINE = 500
MAX_FILE_SIZE = 1_048_576  # 1MB — skip larger files

# Directories to skip during fallback scan
IGNORED_DIRS = frozenset({
    "node_modules", ".git", ".svn", ".hg", "__pycache__",
    "dist", "build", "target", ".next", ".nuxt", ".turbo",
    ".cache", "coverage", ".idea", ".vscode", ".hiveweave",
})


def _resolve_safe(workspace_path: str, path: str) -> str | None:
    """Resolve a search path and ensure it stays inside the workspace."""
    if not path:
        return str(Path(workspace_path).resolve())
    try:
        ws = Path(workspace_path).resolve()
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                rel = candidate.relative_to(ws)
                full = ws / rel
            except ValueError:
                return None
        else:
            full = (ws / path).resolve()
        if full != ws:
            try:
                full.relative_to(ws)
            except ValueError:
                return None
        return str(full)
    except (OSError, ValueError):
        return None


def _format_results(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return "No matches found."
    groups: dict[str, list[dict[str, Any]]] = {}
    for m in matches:
        groups.setdefault(m["file"], []).append(m)
    parts: list[str] = []
    for file, ms in groups.items():
        parts.append(f"\n{file}:")
        for m in ms:
            parts.append(f'  {m["line"]}: {m["content"]}')
    return "\n".join(parts).strip()


async def _try_ripgrep(
    cwd: str, pattern: str, include: str | None,
    limit: int, context: int, multiline: bool,
) -> list[dict[str, Any]] | None:
    """Try ripgrep; return None if rg is unavailable.

    R4: 流式逐行读取 stdout，达到 limit 上限即终止 rg 进程，
    避免大目录扫描全量输出载入内存。
    """
    args = ["rg", "--line-number", "--no-heading", "--color=never"]
    if multiline:
        args.append("-U")
    if context > 0:
        args.append(f"-C{context}")
    if include:
        args.extend(["--glob", include])
    args.extend(["-e", pattern, cwd])

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None
    except OSError:
        return None

    matches: list[dict[str, Any]] = []
    assert proc.stdout is not None
    try:
        # R4: 逐行流式读取，达到上限即 break（不再 consume 全部输出）
        while len(matches) < limit:
            try:
                raw_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=30
                )
            except asyncio.TimeoutError:
                break
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            # Format: <file>:<line>:<content>
            idx1 = line.find(":")
            if idx1 == -1:
                continue
            file = line[:idx1]
            rest = line[idx1 + 1:]
            idx2 = rest.find(":")
            if idx2 == -1:
                continue
            try:
                line_num = int(rest[:idx2])
            except ValueError:
                continue
            content = rest[idx2 + 1:][:MAX_CHARS_PER_LINE]
            # Normalize path separators
            file = file.replace("\\", "/")
            matches.append({"file": file, "line": line_num, "content": content})
    finally:
        # 达到上限或读取完毕后，若 rg 仍在运行则终止，释放资源
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass

    return matches


def _walk_files(root: Path, include: str | None) -> list[Path]:
    """Walk the tree, returning files that match the include glob (if any)."""
    out: list[Path] = []
    if include:
        try:
            glob_re = re.compile(
                "^" + re.escape(include).replace(r"\*", ".*").replace(r"\?", ".") + "$"
            )
        except re.error:
            glob_re = None
    else:
        glob_re = None

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Skip ignored dirs
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.stat().st_size > MAX_FILE_SIZE:
            continue
        if glob_re is not None and not glob_re.search(path.name):
            continue
        out.append(path)
    return out


def _scan_python(
    root: Path, pattern: str, include: str | None,
    head_limit: int, context: int, multiline: bool,
) -> list[dict[str, Any]]:
    """Fallback grep using Python's re module."""
    try:
        if multiline:
            flags = re.MULTILINE | re.DOTALL
        else:
            flags = re.MULTILINE
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc

    matches: list[dict[str, Any]] = []
    files = _walk_files(root, include)
    root_str = str(root)
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            # Normalize CRLF → LF so grep works on Windows-written files
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        except OSError:
            continue
        if multiline:
            # For multiline mode, scan whole text and report line of first match
            for m in regex.finditer(text):
                if len(matches) >= head_limit:
                    return matches
                line_num = text.count("\n", 0, m.start()) + 1
                content = m.group(0)
                # Truncate long content / newlines
                content = content.replace("\n", "\\n")[:MAX_CHARS_PER_LINE]
                rel = str(fp).replace(root_str + os_sep(), "").replace("\\", "/")
                matches.append({"file": rel, "line": line_num, "content": content})
        else:
            for line_idx, line in enumerate(text.split("\n"), start=1):
                if len(matches) >= head_limit:
                    return matches
                m = regex.search(line)
                if m:
                    content = line[:MAX_CHARS_PER_LINE]
                    rel = str(fp).replace(root_str + os_sep(), "").replace("\\", "/")
                    matches.append({"file": rel, "line": line_idx,
                                    "content": content})
                # context lines not implemented for fallback (ripgrep handles it)
    return matches


def os_sep() -> str:
    """Return OS path separator (helper for testability)."""
    return "\\" if sys.platform.startswith("win") else "/"


async def execute_grep(
    pattern: str,
    path: str,
    include: str | None,
    workspace_path: str,
    head_limit: int | None = None,
    context: int = 0,
    multiline: bool = False,
    max_results: int = 500,
) -> dict[str, Any]:
    """Search files for a regex pattern. Returns {success, output, error}.

    R4: max_results 控制结果上限（默认 500），达到上限后停止搜索。
    head_limit（若提供）与 max_results 取较小值作为有效上限。
    """
    if not pattern:
        return {"success": False, "output": "",
                "error": "Error: pattern is required"}

    search_path = _resolve_safe(workspace_path, path)
    if search_path is None:
        return {"success": False, "output": "",
                "error": "Error: Sandbox violation - "
                         "path must be within workspace"}

    if not Path(search_path).exists():
        return {"success": False, "output": "",
                "error": f"Error: Path not found: {path}"}

    # R4: 有效上限 = min(head_limit, max_results)，未提供 head_limit 时用 max_results
    if head_limit is not None and head_limit > 0:
        limit = min(head_limit, max_results)
    else:
        limit = max_results

    # 1. Try ripgrep first
    rg_matches = await _try_ripgrep(
        search_path, pattern, include, limit, context, multiline
    )
    if rg_matches is not None:
        # 过滤敏感文件路径的匹配（C6）— 不暴露 .env / *.pem / credentials 等内容
        rg_matches = [m for m in rg_matches if not is_sensitive_path(m["file"])]
        return {"success": True, "output": _format_results(rg_matches),
                "error": None}

    # 2. Fallback to Python scan
    try:
        matches = _scan_python(
            Path(search_path), pattern, include, limit, context, multiline
        )
    except ValueError as exc:
        return {"success": False, "output": "",
                "error": f"Error: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "output": "",
                "error": f"Error: {exc}"}

    # 过滤敏感文件路径的匹配（C6）
    matches = [m for m in matches if not is_sensitive_path(m["file"])]
    return {"success": True, "output": _format_results(matches), "error": None}
