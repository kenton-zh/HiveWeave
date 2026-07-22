"""Independent health supervisor — runs on its own asyncio task, not game_time.

The problem (B8): the existing watchdog (_check_silent_agents) runs inside
game_time's tick. If game_time stops (off-duty, crash, etc.), the watchdog
stops too — they share the same fault domain.

This supervisor runs on a separate asyncio task with its own timer, so it
continues monitoring agent health even when game_time is stopped.

Checks:
- Silent agents: no output for 10 min → wake + red flag
- Stuck agents: no output for 30 min → notify superior
- Game time stopped: detect if game_time hasn't ticked recently
"""

from __future__ import annotations

import asyncio
import time

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import get_project_db_by_project_id

log = structlog.get_logger(__name__)

# Check interval (seconds)
CHECK_INTERVAL_S = 60

# Thresholds
SILENT_THRESHOLD_S = 600       # 10 min — wake + red flag
STUCK_THRESHOLD_S = 1800        # 30 min — notify superior
GAME_TIME_STALE_S = 120         # 2 min — game_time hasn't ticked


class HealthSupervisor:
    """Independent health monitor — runs on its own asyncio task."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_check = 0.0

    def start(self):
        """Start the health supervisor as a background asyncio task."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        log.info("health_supervisor_started")

    def stop(self):
        """Stop the health supervisor."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        log.info("health_supervisor_stopped")

    async def _run_loop(self):
        """Main check loop — runs until stopped."""
        while self._running:
            try:
                await asyncio.sleep(CHECK_INTERVAL_S)
                await self._check_all_projects()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("health_supervisor_loop_error", error=str(e))
                await asyncio.sleep(10)  # back off before retrying

    async def _check_all_projects(self):
        """Check health for all active projects."""
        try:
            projects = await meta_db.list_projects()
            for project in projects:
                try:
                    await self._check_project(project)
                except Exception as e:
                    log.debug(
                        "health_supervisor_project_error",
                        project_id=project.get("id"),
                        error=str(e),
                    )
        except Exception as e:
            log.debug("health_supervisor_list_projects_failed", error=str(e))

    async def _check_project(self, project: dict):
        """Check health for a single project."""
        project_id = project.get("id")
        if not project_id:
            return

        try:
            conn = await get_project_db_by_project_id(project_id)
            now_ms = int(time.time() * 1000)

            # 1. Check game_time staleness
            cursor = await conn.execute(
                "SELECT game_seconds, updated_at FROM game_time_state "
                "WHERE project_id = ?",
                [project_id],
            )
            gt_row = await cursor.fetchone()
            await cursor.close()

            if gt_row:
                gt_updated = gt_row["updated_at"] or 0
                gt_stale_s = (now_ms - gt_updated) / 1000
                if gt_stale_s > GAME_TIME_STALE_S:
                    log.warning(
                        "health_supervisor_game_time_stale",
                        project_id=project_id,
                        stale_seconds=int(gt_stale_s),
                    )

            # 2. Check for silent/stuck agents
            cursor = await conn.execute(
                "SELECT id, short_id, name, role, status, last_active_at "
                "FROM agents WHERE status = 'active'",
                [],
            )
            agents = await cursor.fetchall()
            await cursor.close()

            for agent in agents:
                last_active = agent["last_active_at"]
                if not last_active:
                    continue
                silent_s = (now_ms - last_active) / 1000

                if silent_s > STUCK_THRESHOLD_S:
                    log.warning(
                        "health_supervisor_agent_stuck",
                        agent_id=agent["id"],
                        short_id=agent["short_id"],
                        name=agent["name"],
                        silent_seconds=int(silent_s),
                        project_id=project_id,
                    )
                    # TODO: notify superior via inbox
                elif silent_s > SILENT_THRESHOLD_S:
                    log.warning(
                        "health_supervisor_agent_silent",
                        agent_id=agent["id"],
                        short_id=agent["short_id"],
                        name=agent["name"],
                        silent_seconds=int(silent_s),
                        project_id=project_id,
                    )
                    # TODO: wake agent + broadcast health error

        except Exception as e:
            log.debug(
                "health_supervisor_check_project_failed",
                project_id=project_id,
                error=str(e),
            )


# Singleton
health_supervisor = HealthSupervisor()
