"""Independent health supervisor — runs on its own asyncio task, not game_time.

The problem (B8): the existing watchdog (_check_silent_agents) runs inside
game_time's tick. If game_time stops (off-duty, crash, etc.), the watchdog
stops too — they share the same fault domain.

This supervisor runs on a separate asyncio task with its own timer, so it
continues monitoring agent health even when game_time is stopped.

Checks:
- Game time stopped: detect if game_time hasn't ticked recently
- Silent agents: ONLY when game_time is stale (fallback). When game_time
  is healthy, leave silent detection to game_time._check_silent_agents to
  avoid double-wake / missing exemptions (TEST6 audit).
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
WAKE_COOLDOWN_S = 600           # don't re-wake same agent every tick
NOTIFY_COOLDOWN_S = 1800        # don't re-notify superior every tick

_WAITING_DISPOSITIONS = frozenset({
    "waiting_human", "waiting_agent", "waiting_timer", "blocked",
})


class HealthSupervisor:
    """Independent health monitor — runs on its own asyncio task."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_check = 0.0
        # agent_id → last wake / notify monotonic timestamps
        self._wake_ts: dict[str, float] = {}
        self._notify_ts: dict[str, float] = {}

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
            rows = await meta_db.query(
                "SELECT id, name, workspace_path, is_started, created_at "
                "FROM projects ORDER BY created_at DESC"
            )
            projects = [dict(r) for r in (rows or [])]
            for project in projects:
                try:
                    await self._check_project(project)
                except Exception as e:
                    log.warning(
                        "health_supervisor_project_error",
                        project_id=project.get("id"),
                        error=str(e),
                    )
        except Exception as e:
            log.warning("health_supervisor_list_projects_failed", error=str(e))

    async def _check_project(self, project: dict):
        """Check health for a single project."""
        project_id = project.get("id")
        if not project_id:
            return

        # Skip off-duty projects
        if not project.get("is_started"):
            return

        try:
            conn = await get_project_db_by_project_id(project_id)
            now_ms = int(time.time() * 1000)
            now_mono = time.monotonic()

            # 1. Check game_time staleness
            cursor = await conn.execute(
                "SELECT game_seconds, updated_at FROM game_time_state "
                "WHERE project_id = ?",
                [project_id],
            )
            gt_row = await cursor.fetchone()
            await cursor.close()

            gt_stale = True
            if gt_row:
                gt_updated = gt_row["updated_at"] or 0
                gt_stale_s = (now_ms - gt_updated) / 1000
                gt_stale = gt_stale_s > GAME_TIME_STALE_S
                if gt_stale:
                    log.warning(
                        "health_supervisor_game_time_stale",
                        project_id=project_id,
                        stale_seconds=int(gt_stale_s),
                    )

            # When game_time is healthy, its _check_silent_agents owns silence
            # detection (full exemptions). Only fall back when game_time is dead.
            if not gt_stale:
                return

            # 2. Fallback silent/stuck check (game_time domain failed)
            cursor = await conn.execute(
                "SELECT id, short_id, name, role, status, last_active_at, "
                "parent_id, created_at "
                "FROM agents WHERE status = 'active'",
                [],
            )
            agents = await cursor.fetchall()
            await cursor.close()

            last_output = await self._last_output_map(conn, agents)

            for agent in agents:
                aid = agent["id"]
                if await self._should_skip_agent(aid, project_id, now_ms):
                    continue

                baseline = last_output.get(aid) or int(
                    agent["last_active_at"] or agent["created_at"] or 0
                )
                if not baseline:
                    continue
                silent_s = (now_ms - baseline) / 1000

                if silent_s > STUCK_THRESHOLD_S:
                    log.warning(
                        "health_supervisor_agent_stuck",
                        agent_id=aid,
                        short_id=agent["short_id"],
                        name=agent["name"],
                        silent_seconds=int(silent_s),
                        project_id=project_id,
                    )
                    await self._wake_agent(aid, project_id, now_ms, now_mono)
                    await self._notify_superior(
                        agent, project_id, int(silent_s), now_mono
                    )
                elif silent_s > SILENT_THRESHOLD_S:
                    log.warning(
                        "health_supervisor_agent_silent",
                        agent_id=aid,
                        short_id=agent["short_id"],
                        name=agent["name"],
                        silent_seconds=int(silent_s),
                        project_id=project_id,
                    )
                    await self._wake_agent(aid, project_id, now_ms, now_mono)

        except Exception as e:
            log.warning(
                "health_supervisor_check_project_failed",
                project_id=project_id,
                error=str(e),
            )

    async def _last_output_map(
        self, conn: object, agents: list
    ) -> dict[str, int]:
        """Prefer assistant / work_logs over last_active_at (game_time口径)."""
        out: dict[str, int] = {}
        for a in agents:
            la = a["last_active_at"]
            if la:
                out[a["id"]] = int(la)
        try:
            cur = await conn.execute(  # type: ignore[attr-defined]
                "SELECT agent_id, MAX(created_at) AS last_ts FROM chat_messages "
                "WHERE role = 'assistant' GROUP BY agent_id",
                [],
            )
            rows = await cur.fetchall()
            await cur.close()
            for r in rows:
                if r["last_ts"]:
                    aid = r["agent_id"]
                    out[aid] = max(out.get(aid, 0), int(r["last_ts"]))
            cur = await conn.execute(  # type: ignore[attr-defined]
                "SELECT agent_id, MAX(created_at) AS last_ts FROM work_logs "
                "GROUP BY agent_id",
                [],
            )
            rows = await cur.fetchall()
            await cur.close()
            for r in rows:
                if r["last_ts"]:
                    aid = r["agent_id"]
                    out[aid] = max(out.get(aid, 0), int(r["last_ts"]))
        except Exception as e:
            log.debug("health_supervisor_last_output_failed", error=str(e))
        return out

    async def _should_skip_agent(
        self, agent_id: str, project_id: str, now_ms: int
    ) -> bool:
        """Mirror game_time silent-watchdog exemptions."""
        try:
            from hiveweave.agents.supervisor import agent_manager

            for aid, pid in agent_manager.list_processing():
                if aid == agent_id and pid == project_id:
                    return True
        except Exception:
            pass

        inst = None
        try:
            from hiveweave.agents.supervisor import agent_manager

            inst = agent_manager.get_agent(agent_id)
        except Exception:
            inst = None
        disp = getattr(inst, "disposition", None) if inst else None
        if disp == "complete":
            return True

        waits: list[dict] = []
        try:
            from hiveweave.services.wait_contract import wait_contract_service

            all_waits = await wait_contract_service.list_all_active(project_id)
            for w in all_waits or []:
                if (w.get("agentId") or "") != agent_id:
                    continue
                exp = w.get("expiresAt")
                if exp is None or int(exp) > now_ms:
                    waits.append(w)
        except Exception:
            waits = []

        if waits and (disp is None or disp in _WAITING_DISPOSITIONS):
            return True

        # Legal idle: no actionable obligations and no unreplied ask
        try:
            from hiveweave.services.task import TaskService

            obligations = await TaskService().get_actionable_obligations(
                project_id, agent_id
            )
        except Exception:
            obligations = ["?"]  # fail closed: don't skip if unknown

        inbound_ask = False
        try:
            conn = await get_project_db_by_project_id(project_id)
            cur = await conn.execute(
                "SELECT COUNT(*) AS c FROM inbox "
                "WHERE to_agent_id = ? AND expect_report = 1 "
                "AND COALESCE(read, 0) = 0",
                [agent_id],
            )
            row = await cur.fetchone()
            await cur.close()
            inbound_ask = bool(row and int(row["c"] or 0) > 0)
        except Exception:
            inbound_ask = True  # fail closed

        if not obligations and not inbound_ask and not waits:
            return True
        return False

    async def _wake_agent(
        self,
        agent_id: str,
        project_id: str,
        now_ms: int,
        now_mono: float,
    ) -> None:
        """Wake silent agent + broadcast agent_health error (TEST6 P1-3)."""
        last = self._wake_ts.get(agent_id, 0.0)
        if now_mono - last < WAKE_COOLDOWN_S:
            return
        self._wake_ts[agent_id] = now_mono

        # Red-box on org tree
        try:
            from hiveweave.realtime.event_bus import status_event_bus

            await status_event_bus.publish_stream_event(agent_id, {
                "type": "agent_health",
                "agentId": agent_id,
                "projectId": project_id,
                "health": "error",
                "message": "silent_watchdog",
                "at": now_ms,
            })
        except Exception as e:
            log.warning(
                "health_supervisor_broadcast_failed",
                agent_id=agent_id,
                error=str(e),
            )

        # Trigger LLM turn
        try:
            from hiveweave.agents.trigger import (
                is_coordinator,
                trigger_coordinator,
                trigger_subordinate,
            )
            from hiveweave.services.org import OrgService

            agent = await OrgService().get_agent(agent_id)
            role = (agent or {}).get("role") or ""
            if is_coordinator(role):
                await trigger_coordinator(agent_id)
            else:
                await trigger_subordinate(agent_id)
            log.info(
                "health_supervisor_woke_agent",
                agent_id=agent_id,
                project_id=project_id,
            )
        except Exception as e:
            log.warning(
                "health_supervisor_wake_failed",
                agent_id=agent_id,
                error=str(e),
            )

    async def _notify_superior(
        self,
        agent: object,
        project_id: str,
        silent_s: int,
        now_mono: float,
    ) -> None:
        """Notify org parent when agent is stuck >30 min."""
        try:
            aid = agent["id"]  # type: ignore[index]
            parent_id = agent["parent_id"]  # type: ignore[index]
            name = agent["name"]  # type: ignore[index]
            short_id = agent["short_id"]  # type: ignore[index]
        except Exception:
            return
        if not parent_id:
            return
        last = self._notify_ts.get(aid, 0.0)
        if now_mono - last < NOTIFY_COOLDOWN_S:
            return
        self._notify_ts[aid] = now_mono
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().send_message(
                from_agent_id="system",
                to_agent_id=str(parent_id),
                message=(
                    f"[AGENT STUCK] {name} ({short_id}) has produced no "
                    f"output for {silent_s // 60} min. Check obligations / "
                    f"wake them, or escalate."
                ),
                message_type="escalation",
                priority="urgent",
                wake=True,
                idempotency_key=(
                    f"health_stuck:{aid}:{int(now_mono) // NOTIFY_COOLDOWN_S}"
                ),
            )
            from hiveweave.agents.trigger import (
                is_coordinator,
                trigger_coordinator,
                trigger_subordinate,
            )
            from hiveweave.services.org import OrgService

            parent = await OrgService().get_agent(str(parent_id))
            role = (parent or {}).get("role") or ""
            if is_coordinator(role):
                await trigger_coordinator(str(parent_id))
            else:
                await trigger_subordinate(str(parent_id))
            log.info(
                "health_supervisor_notified_superior",
                agent_id=aid,
                parent_id=parent_id,
                project_id=project_id,
            )
        except Exception as e:
            log.warning(
                "health_supervisor_notify_failed",
                agent_id=aid,
                error=str(e),
            )


# Singleton
health_supervisor = HealthSupervisor()
