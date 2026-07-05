"""Dispatch service — task dispatch from superior to subordinate (contract 04).

契约 04: 多 Agent 编排 (dispatch 部分)
- Coordinator dispatches task to subordinate
- 写 work_log (type=discussion) 到 per-project DB (日志读取协议)
- 通过 InboxService 发送 inbox 消息给 subordinate
- 通过 HandoffService 创建 handoff 记录 (生命周期追踪)
- approve_work / reject_work 记录审查决策 (coordinator 侧)
- get_subordinate_logs / get_subordinate_logs_since: 日志读取协议

路由: 所有 work_log 查询通过 project_id 解析到 per-project DB
(镜像 Elixir ProjectFactory.query, 与 handoff.py 一致)。
"""

import json
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services.handoff import HandoffService
from hiveweave.services.inbox import InboxService

log = structlog.get_logger(__name__)


async def _conn(project_id: str):
    """Resolve project_id to per-project DB connection."""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ValueError(f"Workspace not found for project {project_id}")
    return await ensure_project_db(workspace)


async def _query(project_id: str, sql: str,
                 params: list | None = None) -> list:
    """Execute a SELECT on the per-project DB (routed by project_id)."""
    conn = await _conn(project_id)
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def _execute(project_id: str, sql: str,
                   params: list | None = None) -> None:
    """Execute an INSERT/UPDATE/DELETE on the per-project DB."""
    conn = await _conn(project_id)
    await conn.execute(sql, params or [])
    await conn.commit()


class DispatchService:
    """Task dispatch → execute → review workflow.

    Flow:
        Coordinator dispatch_task → Executor works → Executor reports
        → Coordinator reviews (reads code + logs) → approve_work / reject_work

    All work_log queries route to per-project DB via project_id
    (mirrors Elixir ``ProjectFactory.query``).
    """

    def __init__(self) -> None:
        self.inbox = InboxService()
        self.handoff = HandoffService()

    # ── DISPATCH ─────────────────────────────────────────────

    async def dispatch_task(self, project_id: str, from_agent_id: str,
                            to_agent_id: str, description: str,
                            session_id: str | None = None,
                            expect_report: bool = False,
                            create_handoff: bool = True) -> dict:
        """Coordinator dispatches a task to a subordinate.

        1. Write ``work_log`` (type=discussion) on coordinator's side —
           traceable in the session timeline (日志读取协议).
        2. Send inbox message to subordinate via :class:`InboxService`.
        3. Create handoff record via :class:`HandoffService` (if
           ``create_handoff``) — enables lifecycle tracking (pending →
           accepted → completed → approved).

        Returns ``{task_id, handoff_id, from_agent_id, to_agent_id, description}``.
        """
        log_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)

        details = json.dumps({
            "type": "dispatch",
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "description": description,
        })

        await _execute(
            project_id,
            "INSERT INTO work_logs (id, agent_id, project_id, session_id, "
            "type, summary, details, created_at) "
            "VALUES (?, ?, ?, ?, 'discussion', ?, ?, ?)",
            [log_id, from_agent_id, project_id, session_id,
             description, details, now_ms],
        )

        # Send inbox message to subordinate (triggers their processing)
        await self.inbox.send_message(
            from_agent_id, to_agent_id, description,
            message_type="task",
            expect_report=expect_report,
        )

        # Create handoff record for lifecycle tracking
        handoff_id: str | None = None
        if create_handoff:
            handoff_id = await self.handoff.create_handoff(
                project_id, from_agent_id, to_agent_id, description,
                expect_report=expect_report,
            )

        log.info("dispatch.task", from_agent_id=from_agent_id,
                 to_agent_id=to_agent_id, task_id=log_id,
                 handoff_id=handoff_id, preview=description[:80])

        return {
            "task_id": log_id,
            "handoff_id": handoff_id,
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "description": description,
        }

    # ── WORK LOG ─────────────────────────────────────────────

    async def write_work_log(self, project_id: str, agent_id: str,
                             session_id: str | None, log_type: str,
                             summary: str,
                             details: dict | None = None) -> str:
        """Write a work log entry for an agent.

        Returns the UUID of the newly created log entry.
        """
        log_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        details_json = json.dumps(details) if details else "{}"

        await _execute(
            project_id,
            "INSERT INTO work_logs (id, agent_id, project_id, session_id, "
            "type, summary, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [log_id, agent_id, project_id, session_id,
             log_type or "discussion", summary, details_json, now_ms],
        )
        return log_id

    async def get_subordinate_logs(self, project_id: str,
                                   subordinate_agent_id: str,
                                   limit: int = 10) -> list[dict]:
        """Get subordinate's recent work logs (newest first).

        Implements the 日志读取协议 — coordinator reads subordinate progress
        before each conversation turn.
        """
        rows = await _query(
            project_id,
            "SELECT id, agent_id, type, summary, details, created_at "
            "FROM work_logs WHERE agent_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            [subordinate_agent_id, limit],
        )
        return [self._row(r) for r in rows]

    async def get_agent_logs(self, project_id: str, agent_id: str,
                             limit: int = 20) -> list[dict]:
        """Get agent's own work logs (newest first)."""
        return await self.get_subordinate_logs(project_id, agent_id, limit)

    async def get_subordinate_logs_since(self, project_id: str,
                                         subordinate_agent_id: str,
                                         since_timestamp: int) -> list[dict]:
        """Get logs since a timestamp (oldest first, for incremental reads)."""
        rows = await _query(
            project_id,
            "SELECT id, agent_id, type, summary, details, created_at "
            "FROM work_logs WHERE agent_id = ? AND created_at > ? "
            "ORDER BY created_at ASC",
            [subordinate_agent_id, since_timestamp],
        )
        return [self._row(r) for r in rows]

    # ── REVIEW ───────────────────────────────────────────────

    async def approve_work(self, project_id: str, coordinator_id: str,
                           session_id: str | None, subordinate_id: str,
                           review: str | None = None) -> str:
        """Coordinator approves subordinate's completed work.

        Writes a ``completion``-type work log on the coordinator's side.
        Returns the work log UUID.
        """
        summary = f"Approved work from {subordinate_id}"
        if review:
            summary = f"{summary}: {review}"
        return await self.write_work_log(
            project_id, coordinator_id, session_id, "completion", summary,
            {"action": "approve", "subordinate_id": subordinate_id,
             "review": review},
        )

    async def reject_work(self, project_id: str, coordinator_id: str,
                          session_id: str | None, subordinate_id: str,
                          feedback: str) -> str:
        """Coordinator rejects subordinate's work with feedback.

        Writes an ``error``-type work log on the coordinator's side.
        Returns the work log UUID.
        """
        summary = f"Rejected work from {subordinate_id}: {feedback}"
        return await self.write_work_log(
            project_id, coordinator_id, session_id, "error", summary,
            {"action": "reject", "subordinate_id": subordinate_id,
             "feedback": feedback},
        )

    # ── HELPERS ──────────────────────────────────────────────

    @staticmethod
    def _row(row) -> dict:
        """Convert a work_log DB row to a dict, decoding details JSON."""
        d = dict(row)
        raw = d.get("details")
        if isinstance(raw, str):
            try:
                d["details"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d["details"] = {}
        elif raw is None:
            d["details"] = {}
        return d
