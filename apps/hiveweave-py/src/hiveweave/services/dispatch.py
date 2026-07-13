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
from hiveweave.services.task import TaskService

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


# Columns missing from older work_logs tables
_MISSING_COLUMNS = [
    ("task_id", "TEXT"),
]
_migrated: set[str] = set()


async def _ensure_schema(project_id: str) -> None:
    """Add missing columns to work_logs table (idempotent)."""
    if project_id in _migrated:
        return
    for col_name, col_def in _MISSING_COLUMNS:
        try:
            await _execute(project_id,
                           f"ALTER TABLE work_logs ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass  # Column already exists
    _migrated.add(project_id)


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
        self.task_service = TaskService()

    # ── DISPATCH ─────────────────────────────────────────────

    async def dispatch_task(self, project_id: str, from_agent_id: str,
                            to_agent_id: str, description: str,
                            session_id: str | None = None,
                            expect_report: bool = False,
                            create_handoff: bool = True,
                            existing_task_id: str | None = None) -> dict:
        """Coordinator dispatches a task to a subordinate.

        1. Create a Task Ledger entry via :class:`TaskService` — obtains
           the canonical ``task_id`` that threads through inbox / work_log /
           handoff (Task Ledger 全链路串联). If ``existing_task_id`` is
           provided, reuse it instead of creating a new one.
        2. Write ``work_log`` (type=discussion) on coordinator's side —
           traceable in the session timeline (日志读取协议), 携带 task_id.
        3. Send inbox message to subordinate via :class:`InboxService`,
           携带 task_id.
        4. Create handoff record via :class:`HandoffService` (if
           ``create_handoff``) — enables lifecycle tracking (pending →
           accepted → completed → approved), 携带 task_id.

        Returns ``{success, task_id, handoff_id, from_agent_id, to_agent_id,
        description}``.
        """
        await _ensure_schema(project_id)

        # 1) Task Ledger: 创建 task 记录，获得真正的 task_id（全链路主键）
        #    如果传入了 existing_task_id，复用已有 task 而不是创建新的
        if existing_task_id:
            task_id = existing_task_id
            # 更新 assignee 为实际接收者
            await self.task_service.update_task(
                project_id, task_id, assignee_id=to_agent_id
            )
        else:
            task_id = await self.task_service.create_task(
                project_id=project_id,
                title=description[:100],
                description=description,
                creator_id=from_agent_id,
                assignee_id=to_agent_id,
                source="agent",
            )

        log_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)

        details = json.dumps({
            "type": "dispatch",
            "task_id": task_id,
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "description": description,
        })

        # 2) work_log 携带 task_id
        await _execute(
            project_id,
            "INSERT INTO work_logs (id, agent_id, project_id, session_id, "
            "type, summary, details, created_at, task_id) "
            "VALUES (?, ?, ?, ?, 'discussion', ?, ?, ?, ?)",
            [log_id, from_agent_id, project_id, session_id,
             description, details, now_ms, task_id],
        )

        # 3) inbox 消息携带 task_id
        await self.inbox.send_message(
            from_agent_id, to_agent_id, description,
            message_type="task",
            expect_report=expect_report,
            task_id=task_id,
        )

        # 4) handoff 携带 task_id（如启用）
        handoff_id: str | None = None
        if create_handoff:
            handoff_id = await self.handoff.create_handoff(
                project_id, from_agent_id, to_agent_id, description,
                expect_report=expect_report,
                task_id=task_id,
            )

        log.info("dispatch.task", from_agent_id=from_agent_id,
                 to_agent_id=to_agent_id, task_id=task_id,
                 log_id=log_id, handoff_id=handoff_id, preview=description[:80])

        return {
            "success": True,
            "task_id": task_id,
            "handoff_id": handoff_id,
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "description": description,
        }

    # ── WORK LOG ─────────────────────────────────────────────

    async def write_work_log(self, project_id: str, agent_id: str,
                             session_id: str | None, log_type: str,
                             summary: str,
                             details: dict | None = None,
                             task_id: str | None = None) -> str:
        """Write a work log entry for an agent.

        Returns the UUID of the newly created log entry.
        """
        await _ensure_schema(project_id)
        log_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        details_json = json.dumps(details) if details else "{}"

        await _execute(
            project_id,
            "INSERT INTO work_logs (id, agent_id, project_id, session_id, "
            "type, summary, details, created_at, task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [log_id, agent_id, project_id, session_id,
             log_type or "discussion", summary, details_json, now_ms, task_id],
        )
        return log_id

    async def get_subordinate_logs(self, project_id: str,
                                   subordinate_agent_id: str,
                                   limit: int = 10) -> list[dict]:
        """Get subordinate's recent work logs (newest first).

        Implements the 日志读取协议 — coordinator reads subordinate progress
        before each conversation turn.
        """
        await _ensure_schema(project_id)
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
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            "SELECT id, agent_id, type, summary, details, created_at "
            "FROM work_logs WHERE agent_id = ? AND created_at > ? "
            "ORDER BY created_at ASC",
            [subordinate_agent_id, since_timestamp],
        )
        return [self._row(r) for r in rows]

    async def get_work_logs_for_task(self, project_id: str,
                                     task_id: str) -> list[dict]:
        """Get all work logs associated with a task (oldest first).

        Public API for querying work_logs by task_id — used by the Task Ledger
        API to include related logs in task detail responses.
        """
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            "SELECT id, agent_id, type, summary, details, created_at, task_id "
            "FROM work_logs WHERE task_id = ? ORDER BY created_at ASC",
            [task_id],
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
