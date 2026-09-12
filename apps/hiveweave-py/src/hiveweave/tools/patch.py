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

from hiveweave.tools.file import (
    _resolve_safe_detail,
    check_file_version,
    record_file_version,
)
from hiveweave.util.tree_label import write_tree_suffix
from hiveweave.tools.security import check_sensitive_access

log = structlog.get_logger(__name__)


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

    full, hint = _resolve_safe_detail(workspace_path, file_path)
    if hint is not None:
        return f"ERROR: {hint}"
    if full is None:
        return f"ERROR: Sandbox violation: {file_path}"

    # .hiveweave 系统目录保护 — 阻止 patch 修改/删除 data.db 等系统文件
    # write=True：patch 是写路径；`merge-quarantine/` 只对**读**放行
    # （report TEST_DSH_54 #5：读放行/写保护，不是整目录并入白名单）。
    from hiveweave.tools.file import _check_hiveweave_dir
    if _check_hiveweave_dir(full, workspace_path, write=True):
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
        record_file_version(p)
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
        # 45 轮 P1「拒绝无记忆」③：edit 前版本戳——文件在最近一次读/写
        # 访问后被外部改动 → 陈旧视图早拒（逼重读），而不是烧在
        # oldString not found 上。
        #
        # #10（2026-09-11）：只给**动作**，不给版本证据（见
        # `check_file_version` 的 docstring —— 印两个相等的 size 是伪造
        # 证据）。typed code `FS_NOT_OBSERVED` 保留在文案里，便于机检/路由。
        stale = check_file_version(p)
        if stale:
            return (
                f"ERROR: stale view: {file_path} changed since your last "
                f"read. Re-read the file, then re-apply. "
                f"RETRY[action=reread_file_then_reapply] ({stale})"
            )
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
            record_file_version(p)
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
                record_file_version(p)
                old_lines = old_str.count("\n") + 1
                new_lines = new_str.count("\n") + 1
                line_diff = new_lines - old_lines
                sign = "+" if line_diff >= 0 else ""
                return (f"Updated {file_path} ({sign}{line_diff} lines, fuzzy match)")
            # 45 轮 P1：machine-readable 出路标记 + 同因连拒计数（s3c10
            # 同文件 4 败夹 3 成的陈旧视图形态）。
            #
            # ⚠️ 本处**有意保留** `annotate_repeat_rejection` 的就地拼接
            # （批次 4 附项 2026-09-11 的例外）：`_apply_edit` 的签名里没有
            # agent_id，返回的是**裸字符串**（调用方只能当消息文本用），
            # 拿不到投递通道所需的收件人。此处的连拒提示因此退化为进程级
            # 共享 + 与文案同处 —— 已知代价，不是遗漏。
            # 迁移前提：把 agent_id 穿过 `edit_file` 的调用链（5+ 处签名），
            # 那属于工具契约改动，与本批次（知识共享）不同源，故留待后续。
            from hiveweave.services.rejection_memory import (
                annotate_repeat_rejection,
            )

            msg = (
                f"ERROR: oldString not found in {file_path}. "
                "Please read the file first."
                " RETRY[action=reread_file_then_reapply|alt=use_write_file]"
            )
            msg += annotate_repeat_rejection("edit_file", msg)
            return msg
        if count > 1:
            return (f"ERROR: oldString found {count} times in {file_path}. "
                    "Add more context to make it unique.")

        new_content = content.replace(old_str, new_str)
        try:
            p.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: {exc}"
        record_file_version(p)

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
    """Accept both standard 'patches' array and LLM-direct single-patch form.

    op 推断走唯一实现 `_infer_op`（此前这里有**第三份**内联副本，
    且数组项形态完全不被推断 —— report TEST_DSH_54 #11）。
    """
    patches = raw.get("patches")
    if isinstance(patches, list):
        out: list[dict[str, Any]] = []
        for item in patches:
            if isinstance(item, dict) and not item.get("op"):
                item = {**item, "op": _infer_op(item)}
            out.append(item)
        return out

    # LLM passed direct parameters
    file_path = raw.get("filePath") or raw.get("file_path")
    if isinstance(file_path, str):
        merged = dict(raw)
        merged["op"] = merged.get("op") or _infer_op(merged)
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
        if not status.startswith("ERROR"):
            status = status + write_tree_suffix(workspace_path)
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

# ── op 推断：唯一实现（report TEST_DSH_54 #11）────────────────────
# `op` 曾只在**顶层直传**形态被推断（见 `_normalize_direct_params`），
# `patches[]` 数组项缺 `op` 则直落 pydantic 必填错误
# `'patches.<N>.op': Field required` —— 实测 13 次 / 4 个 Agent 全撞后者。
# 两处推断逻辑收敛到这里：同一语义只有一个实现，新形态接入不会各写一份。
_OLD_STRING_KEYS = (
    "oldString", "old_string", "old_str", "oldText", "search",
)


def _infer_op(patch: dict) -> str:
    """从可见字段推断缺失的 `op`（与模型从 edit_file 学来的分布一致）。

    规则（刻意保守，与既有顶层直传行为逐字一致）：
    - 提供 oldString 族字段 ⇒ ``update``
    - 提供 content ⇒ ``add``
    - 都没有 ⇒ ``add``（删除**无法**从"缺字段"推断，必须显式传 op='delete'）
    """
    if any(k in patch for k in _OLD_STRING_KEYS):
        return "update"
    return "add"


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
        if not patch.get("op"):
            patch["op"] = _infer_op(patch)
        return {"patches": [patch]}

    @field_validator("patches", mode="before")
    @classmethod
    def _coerce_patches(cls, v: Any) -> Any:
        """Coerce JSON string to list, and infer a missing per-item `op`.

        report TEST_DSH_54 #11：模型看到的契约（`tools/executor.py` 手写
        schema 表）明确鼓励两种形态 ——
        「Either pass patches[] (array of ops) or a single change as direct
        filePath + oldString/newString/content」—— 但数组项的 `op` 在
        pydantic 侧是**必填**，而旧的 op 推断只覆盖顶层直传形态。
        结果：模型照可见契约传 ⇒ 必然被拒（13 次 / 4 个 Agent）。
        契约的模型可见面与代码强制面必须一致：这里让数组项缺 `op` 也走
        同一套推断（唯一实现 `_infer_op`），而不是让模型为平台的字段
        设计付往返成本。
        """
        if isinstance(v, str):
            import json
            try:
                parsed = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return v
            v = parsed
        if not isinstance(v, list):
            return v
        out: list[Any] = []
        for item in v:
            if isinstance(item, dict) and not item.get("op"):
                item = dict(item)
                item["op"] = _infer_op(item)
            out.append(item)
        return out


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
        description="Literal text to replace. Must match exactly.",
        json_schema_extra={"aliases": ["old_string", "old_str", "oldText", "search"]},
    )
    new_string: str = Field(
        alias="newString",
        description="Literal replacement. Empty string deletes the match.",
        json_schema_extra={"aliases": ["new_string", "new_str", "newText", "replace"]},
    )
    replace_all: bool = Field(
        default=False,
        description="Replace all matches. Default false: old_string must appear exactly once.",
        json_schema_extra={"aliases": ["replaceAll"]},
    )


@tool(
    "apply_patch",
    "Apply file patch operations (add/update/delete). Prefer this or "
    "edit_file for a small change; write_file fully replaces a file.",
    requires_workspace=True,
    security_level="file_op",
)
async def apply_patch_tool(params: ApplyPatchParams, agent_id: str, workspace: str) -> ToolResult:
    """Apply a list of patch operations."""
    from hiveweave.tools import write_gate

    # 写路径闸（46/11 #6）：多文件 patch 全部路径**全有或全无**获取，防
    # 半持有状态；同路径并发写（共享 worktree 的父/子代理）advisory 冲突。
    # 去重（审计 P1-3）：同一 patch 多处改同一文件是合法输入，不去重会
    # 自己撞自己的闸被误拒。
    targets = list(dict.fromkeys(p.file_path for p in params.patches))
    acquired: list[str] = []
    blocked_path: str | None = None
    for fp in targets:
        if write_gate.try_acquire(fp, workspace):
            acquired.append(fp)
        else:
            blocked_path = fp
            break
    if blocked_path is not None:
        for fp in acquired:
            write_gate.release(fp, workspace)
        return ToolResult.err(write_gate.conflict_message(blocked_path))
    try:
        # Convert Pydantic models back to dicts for the existing implementation
        patches_raw = [p.model_dump(by_alias=True, exclude_none=True) for p in params.patches]
        result = await apply_patch(
            patches=patches_raw,
            workspace_path=workspace,
        )
    finally:
        for fp in acquired:
            write_gate.release(fp, workspace)
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
    "Edit an existing UTF-8 text file by replacing literal text. "
    "old_string must match exactly. Default: it must appear exactly once. "
    "Empty new_string deletes the match. Prefer apply_patch for multi-file.",
    requires_workspace=True,
    security_level="file_op",
)
async def edit_file_tool(params: EditFileParams, agent_id: str, workspace: str) -> ToolResult:
    """Single-file edit via apply_patch."""
    from hiveweave.tools import write_gate

    # 写路径闸（46/11 #6）：同路径并发写 advisory 冲突（共享 worktree）。
    if not write_gate.try_acquire(params.file_path, workspace):
        return ToolResult.err(write_gate.conflict_message(params.file_path))
    try:
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
    finally:
        write_gate.release(params.file_path, workspace)
    if result.get("success"):
        return ToolResult.ok(result["output"])
    error_msg = result.get("error", "Unknown error")
    output = result.get("output", "")
    if output:
        error_msg = f"{error_msg}\nDetails:\n{output}"
    return ToolResult.err(error_msg)
