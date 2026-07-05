"""Approval service — async approval flow for tool permission requests.

契约 08: 权限与审批
- Flow: request → wait (120s timeout) → resolve/cancel
- asyncio.Future for async waiting (replaces Elixir receive/ETS)
- permission_requests stored in per-project DB
- remember=True saves tool pattern to agent's allowed/denied_tools JSON field
- cleanup_orphaned_requests: pending → 'timeout' (startup cleanup)
"""

import asyncio
import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

logger = structlog.get_logger()

APPROVAL_TIMEOUT_S = 120


class PermissionRejected(Exception):
    """Raised when a permission request is rejected."""


class PermissionTimeout(Exception):
    """Raised when a permission request times out."""


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
            await meta_db.execute(
                f"UPDATE agents SET {field_name} = ? WHERE id = ?",
                [json.dumps(tools), agent_id],
            )
            logger.info("approval.rule_saved", agent_id=agent_id,
                        tool=tool_pattern, action=field_name)


approval_service = ApprovalService()
