"""apply_patch tool — structured search/replace patch operations.

契约 02: 工具执行器 — apply_patch 子模块
- 三种操作: add / update / delete
- update 走 SEARCH/REPLACE 规则：唯一匹配（多次匹配报错）、连续块、不修改未改变部分
- 路径沙箱：所有 filePath 必须解析到 workspace_path 内
- 兼容 LLM 直传参数（filePath + op + oldString/newString）和标准 patches 数组格式
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def _resolve_safe(workspace_path: str, file_path: str) -> str | None:
    """Resolve file_path against workspace; return None if escapes sandbox."""
    if not file_path:
        return None
    try:
        ws = Path(workspace_path).resolve()
        candidate = Path(file_path)
        if candidate.is_absolute():
            try:
                rel = candidate.relative_to(ws)
                full = ws / rel
            except ValueError:
                return None
        else:
            full = (ws / file_path).resolve()
        if full != ws:
            try:
                full.relative_to(ws)
            except ValueError:
                return None
        return str(full)
    except (OSError, ValueError):
        return None


def _apply_single(patch: dict[str, Any], workspace_path: str) -> str:
    """Apply a single patch entry; return a status string."""
    op = (patch.get("op") or "").strip().lower()
    file_path = patch.get("filePath") or patch.get("file_path") or ""

    full = _resolve_safe(workspace_path, file_path)
    if full is None:
        return f"ERROR: Sandbox violation: {file_path}"

    p = Path(full)

    if op == "add":
        content = patch.get("content")
        if content is None:
            return 'ERROR: add requires "content"'
        if p.exists():
            return f"ERROR: File already exists: {file_path}"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: {exc}"
        size = len(content.encode("utf-8"))
        return f"Created {file_path} ({size} bytes)"

    if op == "update":
        old_str = patch.get("oldString", patch.get("old_string"))
        new_str = patch.get("newString", patch.get("new_string"))
        if old_str is None or new_str is None:
            return 'ERROR: update requires "oldString" and "newString"'
        if not p.exists():
            return f"ERROR: File not found: {file_path}"
        if not p.is_file():
            return f"ERROR: Not a file: {file_path}"
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"ERROR: {exc}"

        # Count non-overlapping occurrences (mirrors :binary.matches/1)
        count = content.count(old_str) if old_str else 0

        if count == 0:
            return (f"ERROR: oldString not found in {file_path}. "
                    "Please read the file first.")
        if count > 1:
            return (f"ERROR: oldString found {count} times in {file_path}. "
                    "Add more context to make it unique.")

        new_content = content.replace(old_str, new_str)
        try:
            p.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: {exc}"

        old_lines = old_str.count("\n") + 1
        new_lines = new_str.count("\n") + 1
        line_diff = new_lines - old_lines
        sign = "+" if line_diff >= 0 else ""
        return (f"Updated {file_path} ({sign}{line_diff} lines)")

    if op == "delete":
        if not p.exists():
            return f"ERROR: File not found: {file_path}"
        try:
            p.unlink()
        except OSError as exc:
            return f"ERROR: {exc}"
        return f"Deleted {file_path}"

    return f"ERROR: Unknown op: {op}"


def _normalize_patches(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept both standard 'patches' array and LLM-direct single-patch form."""
    patches = raw.get("patches")
    if isinstance(patches, list):
        return patches

    # LLM passed direct parameters
    file_path = raw.get("filePath") or raw.get("file_path")
    if isinstance(file_path, str):
        op = raw.get("op")
        if not op:
            if raw.get("oldString") is not None or raw.get("old_string") is not None:
                op = "update"
            elif raw.get("content") is not None:
                op = "add"
            else:
                op = "add"
        merged = dict(raw)
        merged["op"] = op
        return [merged]

    return []


async def apply_patch(
    patches: list[dict[str, Any]] | None,
    workspace_path: str,
    raw_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a list of patch operations.

    Can be called either with an explicit `patches` list, or with the full
    `raw_input` dict (which may carry either patches[] or single-patch fields).
    """
    if raw_input is not None and not patches:
        patches = _normalize_patches(raw_input)
    elif patches is None:
        patches = []

    if not patches:
        return {
            "success": False, "output": "",
            "error": "Error: No patches provided. Use the 'patches' array "
                     "with 'op', 'filePath', and 'content'/'oldString'/"
                     "'newString' fields.",
        }

    results: list[str] = []
    has_error = False
    for entry in patches:
        if not isinstance(entry, dict):
            results.append("ERROR: patch entry must be an object")
            has_error = True
            continue
        try:
            status = _apply_single(entry, workspace_path)
        except Exception as exc:  # noqa: BLE001
            status = f"ERROR: {exc}"
            has_error = True
        results.append(status)
        if status.startswith("ERROR"):
            has_error = True

    body = "\n".join(results)
    return {
        "success": not has_error,
        "output": body,
        "error": None if not has_error else "One or more patches failed",
    }
