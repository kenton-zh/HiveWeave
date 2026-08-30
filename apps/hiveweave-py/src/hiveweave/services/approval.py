"""Approval service — async approval flow for tool permission requests.

契约 08: 权限与审批
- Flow: request → wait (120s timeout) → resolve/cancel
- asyncio.Future for async waiting (replaces Elixir receive/ETS)
- permission_requests stored in per-project DB
- remember=True saves tool pattern to agent's allowed/denied_tools JSON field
- cleanup_orphaned_requests: pending → 'timeout' (startup cleanup)
"""

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

logger = structlog.get_logger()

APPROVAL_TIMEOUT_S = 120

# F5（平台修复计划 2026-08-30）：审批超时是**能力缺失**（没有门铃），不是
# 操作被否决。平台观测不到用户是否在线，却在文案里断言「用户暂不可达」把
# 能力缺失记成操作被否决 —— 凌晨无人值守时段 6 次「用户暂不可达」全部是
# 项目的既定运行方式，不是用户失职。拆出：
#   - `approval_channel_unavailable`（通道无人应答/无审批通道）→ 走替代方案
#   - `operation_denied`（用户实际拒绝）→ 才记被拒绝
# 同一审批指纹（tool + args hash）超时一次后，同 run 内拒绝再次发起——
# 「不要重试」从文案变成机制（详见 pipeline/command_guard 的
# _approval_fingerprint_seen，消费方在工具调用前置检查里拒绝重发）。
APPROVAL_TIMEOUT_HINT = (
    "[approval_channel_unavailable] 审批请求超时：审批通道无应答 "
    "（超时 %ds，未批准也未拒绝；项目的无人值守运行方式是既定方式，"
    "不是审核人失职）。平台无法观测到审核人是否在线。"
    "请勿原地空转或反复重试同一审批 —— 同一请求在本回合内已被平台"
    "禁止重发。改走可审计的替代方案：说明原因、替代步骤与产出，"
    "走 review/汇报；若该操作是平台不可绕过的硬门，请把目标拆小或"
    "调整参数后再发起一次不同的请求。"
) % APPROVAL_TIMEOUT_S


class PermissionRejected(Exception):
    """Raised when a permission request is rejected."""


class PermissionTimeout(Exception):
    """Raised when a permission request times out."""


# ── F5：审批超时的指纹去重 + 无人值守模式 ─────────────────────────
# 同一审批指纹（tool + args hash）超时一次后，同 run 内拒绝再次发起——
# 「不要重试」从文案变成机制（r4：文案已写"不要重试"，仍重试 6 次）。
# 进程内 per-(agent, tool, args_hash) 记一次超时，TTL 内同指纹再次 request
# 直接短路返回超时（不再等 120s）。周期惰性清理，防内存泄漏。
# 语义更正（P3 边界审计 2026-08-30）：实现是「10min 进程窗口」而非严格
# 「同 run」——标记不随 turn/run 边界清除。10min 窗口比单 run 更保守
# （跨 run 的合法重发也会被挡），对 r4 的「同一审批反复空等」打击更彻底；
# 若未来需要严格 run 作用域，在 tool_loop 首轮清空该 agent marks。
_APPROVAL_TIMEOUT_MARK: dict[str, int] = {}  # key -> first_timeout_ms
_APPROVAL_TIMEOUT_TTL_MS = 10 * 60 * 1000  # 10min 记忆窗口（一次 turn 内足够）
_FINGERPRINT_CACHE_MAX = 512


def approval_fingerprint(tool_name: str, tool_args: dict | None) -> str:
    """(tool_name, canonical args) 的稳定指纹 —— F5 同指纹拒重试的键。"""
    try:
        args_repr = json.dumps(
            tool_args or {}, sort_keys=True, ensure_ascii=False, default=str
        )
    except Exception:
        args_repr = repr(tool_args or {})
    return hashlib.sha256(
        f"{tool_name}\n{args_repr}".encode("utf-8", errors="replace")
    ).hexdigest()[:24]


def _prune_approval_marks(now_ms: int | None = None) -> None:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if len(_APPROVAL_TIMEOUT_MARK) < _FINGERPRINT_CACHE_MAX:
        stale = [
            k for k, ts in _APPROVAL_TIMEOUT_MARK.items()
            if now_ms - ts > _APPROVAL_TIMEOUT_TTL_MS
        ]
        for k in stale:
            _APPROVAL_TIMEOUT_MARK.pop(k, None)
    else:
        _APPROVAL_TIMEOUT_MARK.clear()  # 满上限保守清空，避免膨胀


def approval_timeout_marked(agent_id: str, tool_name: str, tool_args: dict | None) -> bool:
    """True = 同指纹最近已超时一次，本 run 内拒绝再次发起（F5）。"""
    key = f"{agent_id}:{approval_fingerprint(tool_name, tool_args)}"
    ts = _APPROVAL_TIMEOUT_MARK.get(key)
    if ts is None:
        return False
    now_ms = int(time.time() * 1000)
    if now_ms - ts > _APPROVAL_TIMEOUT_TTL_MS:
        _APPROVAL_TIMEOUT_MARK.pop(key, None)
        return False
    return True


def mark_approval_timeout(agent_id: str, tool_name: str, tool_args: dict | None) -> None:
    """记录一次审批超时（供同 run 内拒重试）。"""
    _prune_approval_marks()
    key = f"{agent_id}:{approval_fingerprint(tool_name, tool_args)}"
    _APPROVAL_TIMEOUT_MARK.setdefault(key, int(time.time() * 1000))


# 无人值守模式开关：项目级 global_settings key。开启后审批类请求直接走
# 可审计的替代方案通道（返回超时语义），不再等待 120s —— 凌晨无人值守是
# 项目的既定运行方式，不是用户失职（r4 F5）。
UNATTENDED_MODE_SETTING = "unattended_mode"


async def is_unattended_mode(project_id: str | None) -> bool:
    """True = 项目开启无人值守模式（审批请求不再等通道）。

    读取 Meta DB ``global_settings`` 的 ``unattended_mode:<project_id>``
    与全局 ``unattended_mode``。best-effort：读失败返回 False（视为有人值守，
    不因配置读取失败而放行审批）。
    """
    if not project_id:
        return False
    try:
        from hiveweave.services.settings import SettingsService
        svc = SettingsService()
        for key in (
            f"{UNATTENDED_MODE_SETTING}:{project_id}",
            UNATTENDED_MODE_SETTING,
        ):
            v = await svc.get(key)
            if v and str(v).strip().lower() in ("1", "true", "yes", "on"):
                return True
        return False
    except Exception:
        return False


class _PendingEntry:
    """In-memory tracking for a pending approval request."""
    __slots__ = ("agent_id", "project_id", "future")

    def __init__(self, agent_id: str, project_id: str, future: asyncio.Future):
        self.agent_id = agent_id
        self.project_id = project_id
        self.future = future


class ApprovalService:
    """Manages async approval flow for tool permission requests."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingEntry] = {}

    async def request_permission(
        self,
        agent_id: str,
        tool_name: str,
        tool_args: dict | None = None,
        description: str = "",
    ) -> str:
        """Create a permission request and wait for resolution (120s timeout).

        Returns request_id on approval.
        Raises PermissionRejected on rejection, PermissionTimeout on timeout.
        """
        project_id = await meta_db.get_agent_project_id(agent_id) or ""
        request_id = str(uuid.uuid4())
        now = int(time.time() * 1000)

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = _PendingEntry(agent_id, project_id, future)

        args_json = json.dumps(tool_args or {})
        await project_db.execute(
            agent_id,
            """INSERT INTO permission_requests
               (id, agent_id, project_id, tool_name, tool_arguments,
                description, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            [request_id, agent_id, project_id, tool_name, args_json,
             description, now, now],
        )
        logger.info("approval.request_created", request_id=request_id,
                     agent_id=agent_id, tool=tool_name)

        try:
            result = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            await project_db.execute(
                agent_id,
                "UPDATE permission_requests SET status = 'timeout', "
                "updated_at = ? WHERE id = ?",
                [int(time.time() * 1000), request_id],
            )
            self._pending.pop(request_id, None)
            logger.warning("approval.timeout", request_id=request_id)
            # F5：同指纹超时留痕 —— 同 run 内拒重试（「不要重试」机制化）。
            mark_approval_timeout(agent_id, tool_name, tool_args)
            raise PermissionTimeout(
                f"Approval request {request_id} timed out"
            )

        if result.get("approved"):
            if result.get("remember"):
                await self._remember_rule(agent_id, tool_name, approved=True)
            return request_id
        if result.get("remember"):
            await self._remember_rule(agent_id, tool_name, approved=False)
        raise PermissionRejected(result.get("note", "rejected"))

    async def resolve_request(
        self,
        request_id: str,
        approved: bool,
        remember: bool = False,
        user_note: str | None = None,
    ) -> None:
        """Resolve a pending permission request (called by API controller)."""
        entry = self._pending.get(request_id)
        now = int(time.time() * 1000)
        status = "approved" if approved else "rejected"

        if entry is not None:
            await project_db.execute(
                entry.agent_id,
                "UPDATE permission_requests SET status = ?, user_note = ?, "
                "updated_at = ? WHERE id = ?",
                [status, user_note, now, request_id],
            )
            if not entry.future.done():
                entry.future.set_result(
                    {"approved": approved, "remember": remember,
                     "note": user_note or ""}
                )
            self._pending.pop(request_id, None)
            logger.info("approval.resolved", request_id=request_id, status=status)
        else:
            logger.warning("approval.not_in_pending", request_id=request_id)

    async def get_pending_requests(self, agent_id: str) -> list[dict]:
        """Get pending permission requests for an agent."""
        try:
            rows = await project_db.query(
                agent_id,
                """SELECT id, agent_id, tool_name, tool_arguments, description,
                          status, created_at
                   FROM permission_requests
                   WHERE agent_id = ? AND status = 'pending'
                   ORDER BY created_at DESC""",
                [agent_id],
            )
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def get_project_pending(self, project_id: str) -> list[dict]:
        """Get pending permission requests for a project."""
        workspace = await meta_db.get_project_workspace(project_id)
        if not workspace:
            return []
        try:
            conn = await project_db.ensure_project_db(workspace)
            cursor = await conn.execute(
                """SELECT id, agent_id, tool_name, tool_arguments, description,
                          status, created_at
                   FROM permission_requests
                   WHERE project_id = ? AND status = 'pending'
                   ORDER BY created_at DESC""",
                [project_id],
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def cleanup_orphaned_requests(self) -> None:
        """Restore/reject pending requests on startup (R4).

        服务器重启后 _pending 内存 dict 丢失，DB 中 status='pending' 的请求永远不会
        被 resolve（resolve_request 只查 _pending，静默 warning）。

        修复：启动时从 DB 加载所有 pending 请求：
        - 过期的（created_at 早于 APPROVAL_TIMEOUT_S 前）→ 自动 reject（status='timeout'）
        - 未过期的 → 重建 _PendingEntry 加入 _pending，使 resolve_request 仍可处理
        """
        try:
            projects = await meta_db.query(
                "SELECT id, workspace_path FROM projects"
            )
        except Exception:
            return
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - APPROVAL_TIMEOUT_S * 1000
        loop = asyncio.get_event_loop()
        restored = 0
        rejected = 0
        for row in projects:
            p = dict(row)
            ws = p.get("workspace_path")
            if not ws:
                continue
            try:
                conn = await project_db.ensure_project_db(ws)

                # 1. 自动 reject 过期的 pending 请求
                cursor = await conn.execute(
                    "UPDATE permission_requests SET status = 'timeout', "
                    "updated_at = ? WHERE status = 'pending' AND created_at < ?",
                    [now_ms, cutoff],
                )
                rejected += max(cursor.rowcount, 0)
                await cursor.close()
                await conn.commit()

                # 2. 重建未过期 pending 请求到 _pending（resolve_request 仍可处理）
                cursor = await conn.execute(
                    "SELECT id, agent_id, project_id FROM permission_requests "
                    "WHERE status = 'pending'",
                )
                rows = await cursor.fetchall()
                await cursor.close()
                for r in rows:
                    req_id = r["id"]
                    if req_id not in self._pending:
                        future: asyncio.Future = loop.create_future()
                        self._pending[req_id] = _PendingEntry(
                            r["agent_id"], r["project_id"] or p.get("id", ""),
                            future)
                        restored += 1
            except Exception as e:
                logger.warning("approval.restore_failed",
                               project=p.get("id"), error=str(e))
        logger.info("approval.restore_done",
                    restored=restored, rejected=rejected,
                    pending_in_memory=len(self._pending))

    def cleanup_project(self, project_id: str) -> None:
        """清理指定项目的所有 pending 审批请求（项目删除时调用）。

        取消所有属于该项目的 pending Future，使等待审批的 agent task
        收到 CancelledError 而非永久阻塞。
        """
        stale_ids = [
            rid for rid, entry in self._pending.items()
            if entry.project_id == project_id
        ]
        for rid in stale_ids:
            entry = self._pending.pop(rid, None)
            if entry and not entry.future.done():
                entry.future.cancel()
        if stale_ids:
            logger.info("approval.cleanup_project",
                        project_id=project_id, cleaned=len(stale_ids))

    async def _remember_rule(
        self, agent_id: str, tool_pattern: str, approved: bool
    ) -> None:
        """Save a permanent allow/deny rule to agent's JSON field."""
        agent = await meta_db.get_agent_by_id(agent_id)
        if agent is None:
            return
        field_name = "allowed_tools" if approved else "denied_tools"
        raw = agent.get(field_name, "[]")
        try:
            tools = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            tools = []
        if tool_pattern not in tools:
            tools.append(tool_pattern)
            await project_db.execute(
                agent_id,
                f"UPDATE agents SET {field_name} = ? WHERE id = ?",
                [json.dumps(tools), agent_id],
            )
            logger.info("approval.rule_saved", agent_id=agent_id,
                        tool=tool_pattern, action=field_name)


approval_service = ApprovalService()
