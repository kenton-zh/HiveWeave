"""Staffing demand service — structured hiring needs.

When the system detects a staffing gap (e.g., VERIFY blocked because no
QA exists), it creates a staffing demand. HR can query open demands to
know exactly what to hire, rather than relying on inbox notifications
that might be missed.
"""

from __future__ import annotations

import json
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

# ── Locked writes（per-workspace 写锁纪律，TEST18 审计 S1）─────────────
from hiveweave.db.project import (  # noqa: E402
    ProjectDbError,
    ensure_project_db,
    execute_by_project,
    get_workspace_write_lock,
)


async def _execute_rowcount(
    project_id: str, sql: str, params: list | None = None
) -> int:
    """同纪律单语句写 + 返回 rowcount（execute_by_project 不返回 rowcount）。"""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
    lock = await get_workspace_write_lock(workspace)
    async with lock:
        conn = await ensure_project_db(workspace)
        try:
            cur = await conn.execute(sql, params or [])
            n = cur.rowcount or 0
            await conn.commit()
            await cur.close()
            return n
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


class StaffingDemandService:
    """Manages staffing demands — structured hiring signals."""

    async def create_demand(
        self,
        project_id: str,
        role_needed: str,
        reason: str = "",
        task_id: str | None = None,
        priority: str = "normal",
    ) -> str | None:
        """Create a staffing demand."""
        demand_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        try:
            await execute_by_project(
                project_id,
                "INSERT INTO staffing_demands "
                "(id, project_id, role_needed, reason, task_id, priority, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
                [demand_id, project_id, role_needed, reason[:500],
                 task_id, priority, now_ms],
            )
            log.info(
                "staffing_demand_created",
                demand_id=demand_id,
                role=role_needed,
                project_id=project_id,
            )
            return demand_id
        except Exception as e:
            log.warning("staffing_demand_create_failed", error=str(e))
            return None

    async def get_open_demands(
        self, project_id: str, role_needed: str | None = None
    ) -> list[dict]:
        """Get open staffing demands, optionally filtered by role."""
        try:
            conn = await project_db.get_project_db_by_project_id(project_id)
            if role_needed:
                cursor = await conn.execute(
                    "SELECT * FROM staffing_demands "
                    "WHERE project_id = ? AND status = 'open' "
                    "AND role_needed = ? ORDER BY created_at ASC",
                    [project_id, role_needed],
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM staffing_demands "
                    "WHERE project_id = ? AND status = 'open' "
                    "ORDER BY created_at ASC",
                    [project_id],
                )
            rows = await cursor.fetchall()
            await cursor.close()
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("staffing_demands_query_failed", error=str(e))
            return []

    async def fulfill_demand(
        self, project_id: str, demand_id: str,
        fulfilled_by: str,
    ) -> None:
        """Mark a staffing demand as fulfilled."""
        now_ms = int(time.time() * 1000)
        try:
            await execute_by_project(
                project_id,
                "UPDATE staffing_demands SET status = 'fulfilled', "
                "fulfilled_by = ?, fulfilled_at = ? WHERE id = ?",
                [fulfilled_by, now_ms, demand_id],
            )
            log.info(
                "staffing_demand_fulfilled",
                demand_id=demand_id,
                fulfilled_by=fulfilled_by[:12],
            )
        except Exception as e:
            log.warning("staffing_demand_fulfill_failed", error=str(e))

    async def cancel_demand(
        self, project_id: str, demand_id: str,
        reason: str = "",
    ) -> None:
        """Cancel a staffing demand (e.g., task was abandoned)."""
        now_ms = int(time.time() * 1000)
        suffix = f" [cancelled: {reason[:200]}]" if reason else ""
        try:
            await execute_by_project(
                project_id,
                "UPDATE staffing_demands SET status = 'cancelled', "
                "fulfilled_at = ?, reason = COALESCE(reason, '') || ? "
                "WHERE id = ?",
                [now_ms, suffix, demand_id],
            )
            log.info(
                "staffing_demand_cancelled",
                demand_id=demand_id,
                reason=reason[:120],
            )
        except Exception as e:
            log.warning("staffing_demand_cancel_failed", error=str(e))

    async def cancel_open_demands_for_task(
        self, project_id: str, task_id: str,
        reason: str = "task_closed",
    ) -> int:
        """Cancel all open demands tied to a task (closed/archived cleanup).

        Fulfilled demands (an actual hire) are left untouched.
        """
        now_ms = int(time.time() * 1000)
        suffix = f" [cancelled: {reason[:200]}]" if reason else ""
        try:
            n = await _execute_rowcount(
                project_id,
                "UPDATE staffing_demands SET status = 'cancelled', "
                "fulfilled_at = ?, reason = COALESCE(reason, '') || ? "
                "WHERE task_id = ? AND status = 'open'",
                [now_ms, suffix, task_id],
            )
            if n:
                log.info(
                    "staffing_demands_cancelled_for_task",
                    task_id=task_id,
                    count=n,
                    reason=reason[:120],
                )
            return n
        except Exception as e:
            log.warning(
                "staffing_demand_cancel_for_task_failed",
                task_id=task_id,
                error=str(e),
            )
            return 0


# Singleton
staffing_demand_service = StaffingDemandService()
