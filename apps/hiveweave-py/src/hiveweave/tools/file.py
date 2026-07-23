"""File tool — read_file / write_file / list_files with sandbox.

契约 02: 工具执行器 — file 子模块
- read_file: 行号格式输出，支持 offset+limit，二进制检测拒绝
- write_file: 自动创建父目录，覆盖写入
- list_files: 列出目录条目，标 [DIR]/[FILE] + 大小
- 路径沙箱（读写分离）:
  - 写：必须落在 agent 自己的 workspace（executor = worktree）
  - 读：相对路径仍相对 workspace 解析，但允许落到整个项目根内
    （可读 main / 同伴 worktree / shared，不可逃出项目）
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

# Directories to skip in list_files (common build/dep dirs + HiveWeave system dir)
IGNORED_DIRS = frozenset({
    "node_modules", ".git", ".svn", ".hg", "__pycache__",
    "dist", "build", "target", ".next", ".nuxt", ".turbo",
    ".cache", "coverage", ".idea", ".vscode",
    ".hiveweave",  # HiveWeave 系统目录 — agent 不应遍历
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

# Tools that may resolve paths against the project root (read-only).
READ_PATH_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "list_files",
    "grep",
    "search_files",
})

# Git Bash / MSYS2 drive paths: /d/PC_AI/... → D:/PC_AI/...
# Agents copy these from bash output into read_file/write_file; Path() on
# Windows does not map them to the real drive, so sandbox checks false-deny.
_MSYS_DRIVE = re.compile(r"^/([a-zA-Z])(/|$)")


def normalize_input_path(p: str) -> str:
    """Normalize agent-supplied paths (MSYS2 / Git Bash → Windows-friendly).

    ``/d/PC_AI/Project/X`` → ``D:/PC_AI/Project/X``. Backslashes become
    forward slashes for consistent prefix checks. Relative paths unchanged.
    """
    if not p:
        return p
    s = p.replace("\\", "/")
    if _MSYS_DRIVE.match(s):
        return _MSYS_DRIVE.sub(
            lambda m: m.group(1).upper() + ":/", s, count=1
        )
    return s


def infer_project_root(workspace_path: str) -> str:
    """Derive the project root from an agent workspace.

    Executor worktrees live at ``<project>/.hiveweave/worktrees/<short_id>``.
    Coordinator/HR workspaces are already the project root.
    """
    p = Path(workspace_path).resolve()
    parts = p.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".hiveweave" and parts[i + 1] == "worktrees":
            if i == 0:
                return str(p)
            return str(Path(parts[0]).joinpath(*parts[1:i]))
    return str(p)


def _resolve_safe(workspace_path: str, file_path: str) -> str | None:
    """Resolve file_path against workspace and ensure it stays inside (WRITE sandbox).

    Returns the absolute path string, or None if the path escapes the sandbox.
    """
    if not file_path:
        return None
    file_path = normalize_input_path(file_path)
    try:
        ws = Path(workspace_path).resolve()
        # Disallow absolute paths that point outside workspace
        candidate = Path(file_path)
        if candidate.is_absolute():
            try:
                # Resolve absolute first so D:/… and /d/… (normalized) compare
                abs_cand = candidate.resolve()
                rel = abs_cand.relative_to(ws)
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


def resolve_for_read(
    write_workspace: str,
    file_path: str,
    project_root: str | None = None,
) -> str | None:
    """Resolve a path for READ access.

    Relative paths are resolved against the agent's write workspace (cwd),
    but the final path may land anywhere under the project root — so an
    executor can read main / sibling worktrees via ``../…`` while keeping
    ``src/foo`` pointing at their own worktree copy.
    """
    root = Path(project_root or infer_project_root(write_workspace)).resolve()
    write_ws = Path(write_workspace).resolve()
    if not file_path:
        return str(write_ws)
    file_path = normalize_input_path(file_path)
    try:
        candidate = Path(file_path)
        if candidate.is_absolute():
            full = candidate.resolve()
        else:
            full = (write_ws / file_path).resolve()
        if full != root:
            full.relative_to(root)
        return str(full)
    except (OSError, ValueError):
        return None


def _check_hiveweave_dir(abs_path: str, workspace_path: str) -> bool:
    """Return True if the path targets protected .hiveweave internals.

    保护策略（分层）:
    - `.hiveweave` 根目录下的直接文件（data.db, env.sh, *.db-* 等）→ 保护
    - `.hiveweave/tool_outputs/` → 保护（系统管理的工具输出）
    - `.hiveweave/shared/` → 放行（团队共享空间，所有 agent 可读可写）
    - `.hiveweave/reports/`, `.hiveweave/drafts/`, `.hiveweave/worktrees/` → 放行（agent 工作文件）
    - 其他 `.hiveweave/<subdir>/` → 保护（未知子目录默认保护）
    """
    try:
        ws = Path(workspace_path).resolve()
        hw_root = ws / HIVEWEAVE_DIR
        target = Path(abs_path).resolve()
        try:
            target.relative_to(hw_root)
        except ValueError:
            return False  # Not in .hiveweave — allowed

        # 放行的 agent 工作子目录（shared = 团队共享空间）
        allowed_subdirs = {"shared", "reports", "drafts", "worktrees"}
        for sub in allowed_subdirs:
            try:
                target.relative_to(hw_root / sub)
                return False  # 在允许的工作子目录内
            except ValueError:
                pass

        # tool_outputs/ 保护
        try:
            target.relative_to(hw_root / "tool_outputs")
            return True
        except ValueError:
            pass

        # .hiveweave 根目录下的直接文件或其他未知子目录 → 保护
        return True
    except (OSError, ValueError):
        return False


def _is_sensitive(file_path: str) -> bool:
    """Return True if the file path matches any sensitive file pattern.

    Uses two complementary checks:
    1. Basename-anchored patterns (exact match on filename)
    2. Full-path substring patterns from security.py (broader, catches paths like .ssh/id_rsa)
    """
    base = Path(file_path).name
    if any(p.search(base) for p in SENSITIVE_PATTERNS):
        return True
    # Also check via security.py for path-level patterns (.ssh/, .aws/, etc.)
    from hiveweave.tools.security import is_sensitive_path
    return is_sensitive_path(file_path)


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
    project_root: str | None = None,
) -> dict[str, Any]:
    """Read a file with line numbers. Refuses binary files.

    Returns {success, output, error} where output is line-numbered text.
    Reads may resolve anywhere under the project root; writes stay sandboxed
    to workspace_path (see write_file).
    """
    if not file_path:
        return {"success": False, "output": "",
                "error": "Error: filePath is required"}

    root = project_root or infer_project_root(workspace_path)
    full = resolve_for_read(workspace_path, file_path, root)
    if full is None:
        return {"success": False, "output": "",
                "error": f'Error: Sandbox violation — "{file_path}" '
                         "outside project"}

    if _check_hiveweave_dir(full, root):
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
                "error": f"Error: {type(exc).__name__}: {exc}"}

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
                "error": f"Error: {type(exc).__name__}: {exc}"}

    size = len(content.encode("utf-8"))
    log.info("file.write", path=file_path, bytes=size)
    return {"success": True,
            "output": f"Wrote {file_path} ({size} bytes)", "error": None}


async def list_files(
    path: str,
    workspace_path: str,
    recursive: bool = False,
    maxdepth: int = 1,
    include_ignored: bool = False,
    project_root: str | None = None,
) -> dict[str, Any]:
    """List directory contents with [DIR]/[FILE] tags and sizes.

    BUG-019 修复：支持 recursive + maxdepth 参数，让 CEO 一次看多层目录，
    避免反复调 list_files 探索不同目录导致首次 chat 30s+。

    include_ignored: 列出被 IGNORED_DIRS 屏蔽的 .hiveweave 子目录——
    coordinator 审查 worktree 代码时需要（默认 .hiveweave 整目录被跳过）。

    Reads may list anywhere under the project root; empty path lists the
    agent's own write workspace.
    """
    ws = workspace_path or "."
    root = project_root or infer_project_root(workspace_path)
    depth = max(1, min(maxdepth, 3)) if recursive else 1

    if path:
        full = resolve_for_read(workspace_path, path, root)
        if full is None:
            return {"success": False, "output": "",
                    "error": "Error: Sandbox violation - "
                             "path must be within project"}
        # .hiveweave 系统目录保护 — 对 protected 区域返回错误，
        # 但允许列出 .hiveweave 根目录（只显示 agent 可用子目录）
        if Path(full).name == HIVEWEAVE_DIR and \
           _check_hiveweave_dir(full, root):
            # Listing .hiveweave root — show only accessible subdirectories
            hw_root = Path(full)
            allowed_subs = ["shared", "worktrees", "reports", "drafts"]
            hw_lines: list[str] = []
            for sub in allowed_subs:
                sub_path = hw_root / sub
                if sub_path.exists() and sub_path.is_dir():
                    hw_lines.append(f"[DIR]  {sub}/")
            msg = "Accessible agent work directories in .hiveweave/:" if hw_lines \
                  else "No agent work directories found in .hiveweave/."
            return {
                "success": True,
                "output": msg + ("\n" + "\n".join(hw_lines) if hw_lines else ""),
                "error": None,
            }
        if _check_hiveweave_dir(full, root):
            return {"success": False, "output": "",
                    "error": "Error: This part of `.hiveweave/` is a protected "
                             "system area. Accessible subdirectories: "
                             "`.hiveweave/shared/`, `.hiveweave/worktrees/`, "
                             "`.hiveweave/reports/`, `.hiveweave/drafts/`."}
    else:
        full = str(Path(ws).resolve())

    p = Path(full)
    # worktrees 内的列表自动放开 ignore 过滤（审查场景）
    try:
        rel = p.resolve().relative_to(Path(root).resolve())
        if len(rel.parts) >= 2 and rel.parts[0] == HIVEWEAVE_DIR \
           and rel.parts[1] == "worktrees":
            include_ignored = True
    except ValueError:
        pass
    if not p.exists():
        return {"success": False, "output": "",
                "error": f"Error: Directory not found: {path}"}
    if not p.is_dir():
        return {"success": False, "output": "",
                "error": f"Error: Not a directory: {path}"}

    lines: list[str] = []
    count = 0
    # include_ignored（审查 worktree 场景）：放开 .hiveweave 目录过滤
    ignored = IGNORED_DIRS - {HIVEWEAVE_DIR} if include_ignored else IGNORED_DIRS

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
            if entry.is_dir() and entry.name in ignored:
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


# ── Pydantic models + @tool registration (Phase 2 migration) ──────

from pydantic import BaseModel, Field, ConfigDict

from .base import tool
from .result import ToolResult


class ReadFileParams(BaseModel):
    """Parameters for read_file tool."""
    model_config = ConfigDict(populate_by_name=True)

    file_path: str = Field(
        alias="filePath",
        description="Path to the file to read (relative to workspace).",
        json_schema_extra={"aliases": ["path", "file_path", "file"]},
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Starting line number (0-based, default: 0).",
    )
    limit: int = Field(
        default=2000,
        ge=1,
        description="Max lines to read (default: 2000).",
    )


class WriteFileParams(BaseModel):
    """Parameters for write_file tool."""
    model_config = ConfigDict(populate_by_name=True)

    file_path: str = Field(
        alias="filePath",
        description="Path to the file to write (relative to workspace).",
        json_schema_extra={"aliases": ["path", "file_path", "file"]},
    )
    content: str = Field(
        description="Full file content to write. Overwrites existing file.",
    )


class ListFilesParams(BaseModel):
    """Parameters for list_files tool."""
    model_config = ConfigDict(populate_by_name=True)

    dir_path: str | None = Field(
        default=None,
        alias="dirPath",
        description="Directory path to list (relative to workspace). Default: workspace root.",
        json_schema_extra={"aliases": ["path", "directory", "dir", "dir_path"]},
    )
    recursive: bool = Field(
        default=False,
        description="If true, list recursively. Default: false.",
    )
    maxdepth: int = Field(
        default=1,
        ge=1,
        le=3,
        description="Max depth when recursive (1-3). Default: 1.",
    )
    include_ignored: bool = Field(
        default=False,
        description="Also list .hiveweave subdirectories (e.g. worktrees when "
        "reviewing executor code). Default: false.",
        json_schema_extra={"aliases": ["include_ignored", "no_ignore"]},
    )


@tool(
    "read_file",
    "Reads file contents with line numbers. Relative paths resolve from your "
    "workspace; you may also read anywhere under the project root (e.g. main "
    "or a peer's worktree via ../…). Writes stay confined to your workspace.",
    requires_workspace=True,
    security_level="file_op",
)
async def read_file_tool(params: ReadFileParams, agent_id: str, workspace: str) -> ToolResult:
    """Read a file with line numbers. Refuses binary files."""
    result = await read_file(
        file_path=params.file_path,
        offset=params.offset,
        limit=params.limit,
        workspace_path=workspace,
    )
    if result.get("success"):
        return ToolResult.ok(result["output"])
    return ToolResult.err(result.get("error", "Unknown error"))


@tool(
    "write_file",
    "Writes content to a file (overwrite) inside your own workspace only. "
    "Cannot write outside your worktree / workspace. Auto-creates parent dirs.",
    requires_workspace=True,
    security_level="file_op",
)
async def write_file_tool(params: WriteFileParams, agent_id: str, workspace: str) -> ToolResult:
    """Write a file (overwrite). Auto-creates parent directories."""
    result = await write_file(
        file_path=params.file_path,
        content=params.content,
        workspace_path=workspace,
    )
    if result.get("success"):
        return ToolResult.ok(result["output"])
    return ToolResult.err(result.get("error", "Unknown error"))


@tool(
    "list_files",
    "Lists files and directories. Empty path lists your workspace; other paths "
    "may point anywhere under the project root (e.g. ../ for main, "
    "../peerId/ for a sibling worktree).",
    requires_workspace=True,
    security_level="file_op",
)
async def list_files_tool(params: ListFilesParams, agent_id: str, workspace: str) -> ToolResult:
    """List directory contents with [DIR]/[FILE] tags and sizes."""
    result = await list_files(
        path=params.dir_path or "",
        workspace_path=workspace,
        recursive=params.recursive,
        maxdepth=params.maxdepth,
        include_ignored=params.include_ignored,
    )
    if result.get("success"):
        return ToolResult.ok(result["output"])
    return ToolResult.err(result.get("error", "Unknown error"))
