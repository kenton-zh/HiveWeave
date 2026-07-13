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

from hiveweave.tools.security import check_sensitive_access

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


# ── P1 容错匹配 — 参考 OpenCode edit.ts 的多策略匹配 ──
# 当精确匹配失败时，依次尝试以下策略，找到第一个匹配即返回：
# 1. 行首尾空白归一化：忽略每行前后多余空白
# 2. 全局空白归一化：连续空白折叠为单个空格
# 3. 缩进无关匹配：忽略行首缩进差异

def _fuzzy_match(content: str, old_str: str) -> tuple[str, int, int] | None:
    """Try fuzzy matching strategies when exact match fails.

    Returns (matched_text, start_offset, end_offset) or None.
    """
    # 策略 1：行首尾空白归一化
    result = _match_line_trimmed(content, old_str)
    if result is not None:
        return result

    # 策略 2：全局空白归一化
    result = _match_whitespace_normalized(content, old_str)
    if result is not None:
        return result

    # 策略 3：缩进无关匹配
    result = _match_indent_flexible(content, old_str)
    if result is not None:
        return result

    return None


def _match_line_trimmed(content: str, old_str: str) -> tuple[str, int, int] | None:
    """Match by trimming each line's leading/trailing whitespace."""
    content_lines = content.split("\n")
    old_lines = old_str.split("\n")
    if not old_lines:
        return None

    old_trimmed = [ln.strip() for ln in old_lines]
    n = len(old_trimmed)

    for i in range(len(content_lines) - n + 1):
        chunk = content_lines[i:i + n]
        if [ln.strip() for ln in chunk] == old_trimmed:
            start = sum(len(content_lines[j]) + 1 for j in range(i))
            end = start + len("\n".join(content_lines[i:i + n]))
            return ("\n".join(chunk), start, end)
    return None


def _match_whitespace_normalized(content: str, old_str: str) -> tuple[str, int, int] | None:
    """Match by normalizing all consecutive whitespace to single space."""
    import re
    old_norm = re.sub(r"\s+", " ", old_str).strip()
    if not old_norm:
        return None

    # 在内容中搜索归一化后匹配的原始区间
    content_norm = re.sub(r"\s+", " ", content)
    idx = content_norm.find(old_norm)
    if idx == -1:
        return None

    # 尝试在原始内容中找到对应的区间（通过字符映射）
    # 简化：直接在原文中找第一个非空白字符和最后一个非空白字符
    old_first_word = old_norm.split(" ")[0]
    old_last_word = old_norm.split(" ")[-1] if old_norm.split(" ") else old_first_word

    # 在原文中找到包含这些词的区间
    search_start = 0
    while True:
        start = content.find(old_first_word, search_start)
        if start == -1:
            return None
        # 从 start 开始，向后找 old_last_word
        # 计算归一化后匹配需要的字符数（近似）
        end = content.find(old_last_word, start + len(old_first_word))
        if end == -1:
            search_start = start + 1
            continue
        end += len(old_last_word)
        # 检查这个区间归一化后是否匹配
        candidate = content[start:end]
        if re.sub(r"\s+", " ", candidate).strip() == old_norm:
            return (candidate, start, end)
        search_start = start + 1


def _match_indent_flexible(content: str, old_str: str) -> tuple[str, int, int] | None:
    """Match ignoring leading indentation differences."""
    import re
    content_lines = content.split("\n")
    old_lines = old_str.split("\n")
    if not old_lines:
        return None

    old_stripped = [re.sub(r"^\s*", "", ln) for ln in old_lines]
    n = len(old_stripped)

    for i in range(len(content_lines) - n + 1):
        chunk = content_lines[i:i + n]
        chunk_stripped = [re.sub(r"^\s*", "", ln) for ln in chunk]
        if chunk_stripped == old_stripped:
            start = sum(len(content_lines[j]) + 1 for j in range(i))
            end = start + len("\n".join(content_lines[i:i + n]))
            return ("\n".join(chunk), start, end)
    return None


def _apply_single(patch: dict[str, Any], workspace_path: str) -> str:
    """Apply a single patch entry; return a status string."""
    op = (patch.get("op") or "").strip().lower()
    # LLMs sometimes use "replace" — treat it as "update"
    if op == "replace":
        op = "update"
    file_path = patch.get("filePath") or patch.get("file_path") or ""

    # 敏感文件保护（C6）— 在路径解析前检查，阻止对 .env / *.pem / credentials 等的写入/删除
    check_sensitive_access(file_path, op=op or "write")

    full = _resolve_safe(workspace_path, file_path)
    if full is None:
        return f"ERROR: Sandbox violation: {file_path}"

    # .hiveweave 系统目录保护 — 阻止 patch 修改/删除 data.db 等系统文件
    from hiveweave.tools.file import _check_hiveweave_dir
    if _check_hiveweave_dir(full, workspace_path):
        return (f"ERROR: `.hiveweave` is the HiveWeave system directory. "
                f"NEVER patch files inside .hiveweave (data.db, "
                f"tool_outputs/, etc.). System files are managed by "
                f"HiveWeave internals.")

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
        replace_all = patch.get("replace_all", patch.get("replaceAll", False))
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

        # 精确匹配（原逻辑）
        count = content.count(old_str) if old_str else 0

        # replace_all: skip uniqueness check, replace all occurrences
        if replace_all and count > 0:
            new_content = content.replace(old_str, new_str)
            try:
                p.write_text(new_content, encoding="utf-8")
            except OSError as exc:
                return f"ERROR: {exc}"
            return (f"Updated {file_path} ({count} occurrences replaced, "
                    f"replace_all=True)")

        if count == 0 and old_str:
            # P1 容错匹配 — 参考 OpenCode edit.ts 的多策略匹配
            match_result = _fuzzy_match(content, old_str)
            if match_result is not None:
                matched_text, start, end = match_result
                # 检查匹配唯一性
                second = _fuzzy_match(content[:start] + content[end:], old_str)
                if second is not None:
                    return (f"ERROR: oldString matches {2}+ times in {file_path} "
                            "(fuzzy match). Add more context to make it unique.")
                new_content = content[:start] + new_str + content[end:]
                try:
                    p.write_text(new_content, encoding="utf-8")
                except OSError as exc:
                    return f"ERROR: {exc}"
                old_lines = old_str.count("\n") + 1
                new_lines = new_str.count("\n") + 1
                line_diff = new_lines - old_lines
                sign = "+" if line_diff >= 0 else ""
                return (f"Updated {file_path} ({sign}{line_diff} lines, fuzzy match)")
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
            status = f"ERROR: {type(exc).__name__}: {exc}"
            has_error = True
        results.append(status)
        if status.startswith("ERROR"):
            has_error = True

    body = "\n".join(results)
    total = len(patches)
    failed = sum(1 for r in results if r.startswith("ERROR"))
    return {
        "success": not has_error,
        "output": body,
        "error": None if not has_error else f"{failed}/{total} patches failed (see output for details)",
    }


# ── Pydantic models + @tool registration (Phase 2 migration) ──────

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

from .base import tool
from .helpers import coerce_to_list
from .result import ToolResult


class PatchItem(BaseModel):
    """Single patch operation."""
    model_config = ConfigDict(populate_by_name=True)

    op: str = Field(
        description="Operation: 'add' (create), 'update' (replace), or 'delete'.",
    )
    file_path: str = Field(
        alias="filePath",
        description="Path to the file (relative to workspace).",
        json_schema_extra={"aliases": ["file_path", "file", "path"]},
    )
    old_string: str | None = Field(
        default=None,
        alias="oldString",
        description="For update: text to find in the file.",
        json_schema_extra={"aliases": ["old_string", "old_str", "oldText", "search"]},
    )
    new_string: str | None = Field(
        default=None,
        alias="newString",
        description="For update: replacement text.",
        json_schema_extra={"aliases": ["new_string", "new_str", "newText", "replace"]},
    )
    content: str | None = Field(
        default=None,
        description="For add: full file content.",
    )
    replace_all: bool = Field(
        default=False,
        description="If true, replace all occurrences (skip uniqueness check).",
        json_schema_extra={"aliases": ["replaceAll"]},
    )


class ApplyPatchParams(BaseModel):
    """Parameters for apply_patch tool."""
    model_config = ConfigDict(populate_by_name=True)

    patches: list[PatchItem] = Field(
        default_factory=list,
        description="Array of patch operations.",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_direct_params(cls, data: Any) -> Any:
        """Handle LLM passing direct single-patch params instead of patches[] array.

        LLMs often call apply_patch with:
            {"filePath": "...", "oldString": "...", "newString": "..."}
        instead of:
            {"patches": [{"op": "update", "filePath": "...", ...}]}

        This validator detects direct params and wraps them into a patches array,
        mirroring the legacy _normalize_patches() logic.
        """
        if not isinstance(data, dict):
            return data
        # Already has patches — let field_validator handle coercion
        if "patches" in data and data["patches"]:
            return data
        # Check for direct single-patch params
        direct_keys = {"filePath", "file_path", "file", "path",
                       "oldString", "old_string", "old_str", "oldText", "search",
                       "newString", "new_string", "new_str", "newText", "replace",
                       "content", "op", "replaceAll", "replace_all"}
        found_keys = direct_keys & data.keys()
        if not found_keys:
            return data
        # Build a single patch entry from direct params
        patch: dict[str, Any] = {}
        for k, v in data.items():
            if k in direct_keys:
                patch[k] = v
        # Infer op if not provided
        if "op" not in patch:
            if any(k in patch for k in ("oldString", "old_string", "old_str", "oldText", "search")):
                patch["op"] = "update"
            elif "content" in patch:
                patch["op"] = "add"
            else:
                patch["op"] = "add"
        return {"patches": [patch]}

    @field_validator("patches", mode="before")
    @classmethod
    def _coerce_patches(cls, v: Any) -> Any:
        """Coerce JSON string to list if LLM passes a string instead of array."""
        if isinstance(v, str):
            import json
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return v


class EditFileParams(BaseModel):
    """Parameters for edit_file tool (single-patch shortcut)."""
    model_config = ConfigDict(populate_by_name=True)

    file_path: str = Field(
        alias="filePath",
        description="Path to the file to edit.",
        json_schema_extra={"aliases": ["file_path", "file", "path"]},
    )
    old_string: str = Field(
        alias="oldString",
        description="Text to find in the file.",
        json_schema_extra={"aliases": ["old_string", "old_str", "oldText", "search"]},
    )
    new_string: str = Field(
        alias="newString",
        description="Replacement text.",
        json_schema_extra={"aliases": ["new_string", "new_str", "newText", "replace"]},
    )
    replace_all: bool = Field(
        default=False,
        description="If true, replace all occurrences.",
        json_schema_extra={"aliases": ["replaceAll"]},
    )


@tool(
    "apply_patch",
    "Apply file patch operations (create/update/delete files). Each patch specifies a file path, operation type, and content.",
    requires_workspace=True,
    security_level="file_op",
)
async def apply_patch_tool(params: ApplyPatchParams, agent_id: str, workspace: str) -> ToolResult:
    """Apply a list of patch operations."""
    # Convert Pydantic models back to dicts for the existing implementation
    patches_raw = [p.model_dump(by_alias=True, exclude_none=True) for p in params.patches]
    result = await apply_patch(
        patches=patches_raw,
        workspace_path=workspace,
    )
    if result.get("success"):
        return ToolResult.ok(result["output"])
    # Include detailed output in error so LLM can understand WHY a patch failed
    # (e.g., "File already exists" vs "oldString not found" vs "Sandbox violation")
    error_msg = result.get("error", "Unknown error")
    output = result.get("output", "")
    if output:
        error_msg = f"{error_msg}\nDetails:\n{output}"
    return ToolResult.err(error_msg)


@tool(
    "edit_file",
    "Targeted text replacement in a file. Finds old_string and replaces with new_string. Use apply_patch for multi-file operations.",
    requires_workspace=True,
    security_level="file_op",
)
async def edit_file_tool(params: EditFileParams, agent_id: str, workspace: str) -> ToolResult:
    """Single-file edit via apply_patch."""
    patch_dict = {
        "op": "update",
        "filePath": params.file_path,
        "oldString": params.old_string,
        "newString": params.new_string,
        "replace_all": params.replace_all,
    }
    result = await apply_patch(
        patches=[patch_dict],
        workspace_path=workspace,
    )
    if result.get("success"):
        return ToolResult.ok(result["output"])
    error_msg = result.get("error", "Unknown error")
    output = result.get("output", "")
    if output:
        error_msg = f"{error_msg}\nDetails:\n{output}"
    return ToolResult.err(error_msg)
