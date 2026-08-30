"""deliver_patch tool — 跨仓补丁交付通道（F12 · 平台修复计划 2026-08-30）。

背景（r4 #9）：沙箱禁止直写平台仓（动机正当），但平台没有「确需改动平台
仓」的正式通道，Agent 只能退化成「写补丁文件放那儿」（青岚用了 8+ 次，
全项目无人复用）。本工具把人肉通路变成一等公民：

- Agent 提交 patch 工件（diff 文本或文件相对路径）+ 目标仓标识 + 理由
- 平台落盘到项目共享区 ``.hiveweave/patch-deliveries/<ts>-<agent>_<n>.patch``
  （属于 _ALLOWED_HW_SUBDIRS 放行清单，不会被 .hiveweave 禁写拦掉）
- 落一条 work_log + inbox 通知有权限方（项目 CEO / 履行审批方）去应用
- 返回工件路径与通知回执 —— 「补丁交付」从此有正式通道、有留存、有人管

与 apply_patch 的区别：apply_patch 是**在自己的工作区直接改文件**；
deliver_patch 是**跨仓交付**（目标不在 Agent 可写范围内，如平台仓）。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import structlog

from pydantic import BaseModel, Field, ConfigDict

from .base import tool
from .helpers import get_project_id
from .result import ToolResult

log = structlog.get_logger(__name__)

#: 交付工件落盘根（项目共享区，agent 对 .hiveweave/patch-deliveries 可写）
DELIVERY_DIR = ".hiveweave/patch-deliveries"


class DeliverPatchParams(BaseModel):
    """Parameters for deliver_patch tool."""

    model_config = ConfigDict(populate_by_name=True)

    diff: str = Field(
        description=(
            "The patch/diff content (unified diff) being delivered. "
            "Required if filePath is not provided."
        ),
        default="",
        json_schema_extra={"aliases": ["patch", "diff_content", "content"]},
    )
    file_path: str = Field(
        default="",
        description=(
            "Path (relative to YOUR workspace) of an existing patch file to "
            "deliver. Alternative to inline diff."
        ),
        json_schema_extra={"aliases": ["patch_file", "file", "path"]},
    )
    target_repo: str = Field(
        default="",
        description=(
            "Which repo the patch targets (e.g. 'hiveweave-platform', "
            "'project-<name>'). The sandbox forbids writing platform repos "
            "directly — this channel hands the patch to an authorized party."
        ),
    )
    reason: str = Field(
        default="",
        description="Why this change is needed and what it does.",
    )


def _delivery_root(workspace: str) -> Path:
    return Path(workspace) / DELIVERY_DIR


async def _notify_authorized_party(
    project_id: str | None, agent_id: str, artifact_path: str, reason: str
) -> str:
    """Inbox 通知项目 CEO（有权方代理），请其应用/委派应用补丁。best-effort。"""
    try:
        from hiveweave.services.inbox import InboxService
        from hiveweave.services.org import OrgService

        recipient = None
        try:
            ceo = await OrgService().get_agent_by_role(project_id, "ceo")
            recipient = (ceo or {}).get("id")
        except Exception:
            recipient = None
        if not recipient:
            return ("no authorized party recipient resolved (project CEO "
                    "missing) — artifact persisted for manual pickup")

        note = (
            f"[PATCH DELIVERY] {agent_id} 提交了跨仓补丁，等待有权方应用。\n"
            f"工件：{artifact_path}\n"
            f"理由：{(reason or '')[:300]}\n"
            f"应用后请回执；若无 SOURCE_WRITE 权限，委派有权限的方应用。"
        )
        await InboxService().send_message(
            from_agent_id=agent_id,
            to_agent_id=recipient,
            message=note,
            message_type="task",
            priority="high",
            wake=True,
        )
        return "project CEO notified via inbox"
    except Exception as e:  # noqa: BLE001
        log.warning("deliver_patch_notify_failed", error=str(e))
        return f"notification failed (best-effort): {e}"


async def _record_work_log(
    project_id: str, agent_id: str, artifact_path: str, reason: str
) -> None:
    """落一条 work_log 供审计/追溯（歪招典藏变正式留痕）。"""
    try:
        from hiveweave.services.work_log import WorkLogService

        await WorkLogService().write_work_log(
            project_id, agent_id, None, "delivery",
            f"跨仓补丁交付: {artifact_path}",
            details={"reason": reason[:500], "kind": "patch_delivery"},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("deliver_patch_work_log_failed", error=str(e))


@tool(
    "deliver_patch",
    "Deliver a cross-repo patch through the official channel. The sandbox "
    "forbids writing platform repos directly; this tool hands your patch "
    "(inline diff or file path) to an authorized party with reason and "
    "persists it under .hiveweave/patch-deliveries/ for review and "
    "application. Use it when you must change a repo outside your write "
    "sandbox (e.g. the platform repo) — do NOT fall back to leaving loose "
    "patch files in random places.",
    requires_workspace=True,
    security_level="file_op",
)
async def deliver_patch_tool(
    params: DeliverPatchParams, agent_id: str, workspace: str
) -> ToolResult:
    """Deliver a patch artifact through the official cross-repo channel."""
    from hiveweave.tools.file import _resolve_safe_detail

    project_id = await get_project_id(agent_id)
    diff = (params.diff or "").strip()
    file_path = (params.file_path or "").strip()

    if not diff and not file_path:
        return ToolResult.err(
            "deliver_patch requires either 'diff' content or an existing "
            "file_path to deliver."
        )

    # 组装工件内容（inline diff 或从 workspace 内读取既有 patch 文件）
    artifact: str
    if diff:
        artifact = diff
    else:
        full, hint = _resolve_safe_detail(workspace, file_path)
        if hint or full is None:
            return ToolResult.err(
                f"deliver_patch: cannot resolve patch file: {hint or file_path}"
            )
        try:
            artifact = Path(full).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult.err(f"deliver_patch: read failed: {e}")

    if not artifact.strip():
        return ToolResult.err("deliver_patch: patch content is empty.")

    # 落盘（共享交付区允许写；文件用唯一名防并行冲突）
    try:
        root = _delivery_root(workspace)
        root.mkdir(parents=True, exist_ok=True)
        fname = f"{int(time.time() * 1000)}-{agent_id[:8]}_{uuid.uuid4().hex[:6]}.patch"
        target = root / fname
        target.write_text(artifact, encoding="utf-8")
    except OSError as e:
        return ToolResult.err(f"deliver_patch: persist failed: {e}")

    rel_path = f"{DELIVERY_DIR}/{fname}"
    notify = await _notify_authorized_party(project_id, agent_id, rel_path, params.reason)
    if project_id:
        await _record_work_log(project_id, agent_id, rel_path, params.reason)

    return ToolResult.ok(
        f"[PATCH DELIVERY] 跨仓补丁已通过正式通道交付。\n"
        f"  工件：{rel_path}（项目共享区，团队可见）\n"
        f"  目标仓：{params.target_repo or '(未指定)'}\n"
        f"  理由：{(params.reason or '')[:200]}\n"
        f"  状态：{notify}\n"
        f"后续：等待有权方（人或 CEO 代理）应用并回执 —— 这是标准流程，"
        f"不要再地把补丁文件丢在别的目录。"
    )