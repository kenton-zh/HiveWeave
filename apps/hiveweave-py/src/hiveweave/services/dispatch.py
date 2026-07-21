"""Dispatch service — task dispatch from superior to subordinate (contract 04).

契约 04: 多 Agent 编排 (dispatch 部分)
- Coordinator dispatches task to subordinate
- 写 work_log (type=discussion) 到 per-project DB (日志读取协议)
- 通过 InboxService 发送 inbox 消息给 subordinate
- 通过 HandoffService 创建 handoff 记录 (生命周期追踪)

注意: work_log 读写 / approve / reject 由 WorkLogService + HandoffService 接管
(dispatch_task 内部仍写 dispatch 日志, get_work_logs_for_task 查询按 task 关联日志).

路由: 所有 work_log 查询通过 project_id 解析到 per-project DB
(镜像 Elixir ProjectFactory.query, 与 handoff.py 一致)。
"""

import json
import time
import uuid

import aiosqlite
import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ProjectDbError, ensure_project_db
from hiveweave.services.handoff import HandoffService
from hiveweave.services.inbox import InboxService
from hiveweave.services.task import TaskService

log = structlog.get_logger(__name__)


async def _conn(project_id: str) -> aiosqlite.Connection:
    """Resolve project_id to per-project DB connection."""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
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
        2. Ensure executor worktree (branch hw/<shortId>/t-<taskid8>,
           P0 稳定命名) and pin the worktree path into the delivery
           message; the pinned description is synced back to the task row.
        3. Write ``work_log`` (type=discussion) on coordinator's side —
           traceable in the session timeline (日志读取协议), 携带 task_id.
        4. Send inbox message to subordinate via :class:`InboxService`,
           携带 task_id.
        5. Create handoff record via :class:`HandoffService` (if
           ``create_handoff``) — enables lifecycle tracking (pending →
           accepted → completed → approved), 携带 task_id.

        Returns ``{success, task_id, handoff_id, from_agent_id, to_agent_id,
        description}``.
        """
        await _ensure_schema(project_id)

        # Hard gates: direct-report span + CEO 只派直属中层 coordinator +
        # assignee 须具备 SOURCE_WRITE（executor/qa/builder coordinator）
        from hiveweave.services.org_span import (
            validate_ceo_dispatch_target,
            validate_dispatch_span,
            validate_executor_assignee,
        )

        span_err = await validate_dispatch_span(from_agent_id, to_agent_id)
        if span_err:
            return {
                "success": False,
                "message": span_err,
                "from_agent_id": from_agent_id,
                "to_agent_id": to_agent_id,
            }
        ceo_err = await validate_ceo_dispatch_target(from_agent_id, to_agent_id)
        if ceo_err:
            return {
                "success": False,
                "message": ceo_err,
                "from_agent_id": from_agent_id,
                "to_agent_id": to_agent_id,
            }
        coord_err = await validate_executor_assignee(to_agent_id)
        if coord_err:
            return {
                "success": False,
                "message": coord_err,
                "from_agent_id": from_agent_id,
                "to_agent_id": to_agent_id,
            }

        # 1) Task Ledger: 先创建 task 记录，获得真正的 task_id（全链路主键）。
        #    worktree 分支命名契约 hw/<shortId>/t-<taskid8> 依赖它（P0 分支
        #    命名稳定化），所以建账必须先于 ensure_executor_worktree。
        #    如果传入了 existing_task_id，复用已有 task 而不是创建新的
        if existing_task_id:
            task_id = existing_task_id
            # 更新 assignee 为实际接收者；指派即认领（VERIFY 除外）
            await self.task_service.update_task(
                project_id, task_id, assignee_id=to_agent_id
            )
            try:
                await self.task_service.ensure_assignee_claimed(
                    project_id, task_id
                )
            except Exception as e:
                log.warning(
                    "dispatch_ensure_claimed_failed",
                    task_id=task_id,
                    error=str(e),
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

        # Ensure executor/builder-coordinator worktree + pin paths in the message
        description_out = description
        wt_meta: dict = {}
        try:
            from hiveweave.services.git_worktree import (
                agent_gets_write_worktree,
                ensure_executor_worktree,
                pin_dispatch_message_to_worktree,
            )
            from hiveweave.services.org import OrgService

            assignee = await OrgService().resolve_agent(to_agent_id)
            if assignee and agent_gets_write_worktree(assignee):
                ensured = await ensure_executor_worktree(
                    project_id,
                    to_agent_id,
                    task_name=(description[:40] if description else None),
                    task_id=task_id,
                )
                wt_meta = ensured
                if ensured.get("success") and ensured.get("path"):
                    description_out = pin_dispatch_message_to_worktree(
                        description,
                        short_id=ensured.get("short_id") or assignee.get("short_id") or "",
                        worktree_path=ensured["path"],
                    )
                    # task 行先行创建时存的是原始 description —— 钉上 worktree
                    # 路径后回写，保持 Task Ledger 里可见路径（与原行为一致）。
                    if description_out != description:
                        try:
                            await self.task_service.update_task(
                                project_id, task_id,
                                description=description_out,
                            )
                        except Exception as e:
                            log.warning(
                                "dispatch_task_desc_pin_failed",
                                task_id=task_id, error=str(e),
                            )
                else:
                    log.warning(
                        "dispatch_worktree_ensure_failed",
                        to_agent_id=to_agent_id,
                        error=ensured.get("message"),
                    )
            elif assignee and not agent_gets_write_worktree(assignee):
                log.warning(
                    "dispatch_to_non_writer",
                    to_agent_id=to_agent_id,
                    permission_type=assignee.get("permission_type"),
                )
        except Exception as e:
            log.warning("dispatch_worktree_pin_failed", error=str(e))

        log_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)

        details = json.dumps({
            "type": "dispatch",
            "task_id": task_id,
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "description": description_out,
            "worktree_path": wt_meta.get("path"),
            "worktree_short_id": wt_meta.get("short_id"),
        })

        # 3) work_log 携带 task_id
        await _execute(
            project_id,
            "INSERT INTO work_logs (id, agent_id, project_id, session_id, "
            "type, summary, details, created_at, task_id) "
            "VALUES (?, ?, ?, ?, 'discussion', ?, ?, ?, ?)",
            [log_id, from_agent_id, project_id, session_id,
             description_out, details, now_ms, task_id],
        )

        # 4) inbox 消息携带 task_id（路径已钉到 assignee worktree）
        await self.inbox.send_message(
            from_agent_id, to_agent_id, description_out,
            message_type="task",
            expect_report=expect_report,
            task_id=task_id,
        )

        # 5) handoff 携带 task_id（如启用）
        handoff_id: str | None = None
        if create_handoff:
            handoff_id = await self.handoff.create_handoff(
                project_id, from_agent_id, to_agent_id, description_out,
                expect_report=expect_report,
                task_id=task_id,
            )

        log.info("dispatch.task", from_agent_id=from_agent_id,
                 to_agent_id=to_agent_id, task_id=task_id,
                 log_id=log_id, handoff_id=handoff_id,
                 worktree=wt_meta.get("path"),
                 preview=description_out[:80])

        return {
            "success": True,
            "task_id": task_id,
            "handoff_id": handoff_id,
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "description": description_out,
            "worktree_path": wt_meta.get("path"),
            "worktree_short_id": wt_meta.get("short_id"),
        }

    # ── WORK LOG ─────────────────────────────────────────────

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
