"""Game time service — per-project simulated clock (契约 07).

1 real hour = 1 game day (3600s real = 86400s game). 5s tick: advance time,
fire alarms, detect stalls. Absolute time model. Cooldown in-memory (A5).
"""

import asyncio
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ensure_project_db

log = structlog.get_logger(__name__)

REAL_SECONDS_PER_GAME_DAY = 3600
GAME_SECONDS_PER_DAY = 86400
TICK_INTERVAL = 5
STALL_CHECK_TICKS = 12        # 12 * 5s = 60s
STALL_IDLE_MS = 10 * 60 * 1000        # 10 min
STALL_COOLDOWN_MS = 10 * 60 * 1000    # 10 min (in-memory, A5)

_states: dict[str, dict] = {}          # project_id → state
_alarm_project: dict[str, str] = {}    # alarm_id → project_id


async def _conn(project_id: str):
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ValueError(f"Workspace not found for project {project_id}")
    return await ensure_project_db(workspace)


async def _query(project_id, sql, params=None):
    conn = await _conn(project_id)
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def _execute(project_id, sql, params=None):
    conn = await _conn(project_id)
    await conn.execute(sql, params or [])
    await conn.commit()


class GameTimeService:
    """Per-project simulated clock with alarms and stall detection.

    R12: 构造函数接受可选 project_id，供 main.py lifespan 等场景按项目实例化。
    各方法仍接受 project_id 参数（向后兼容），未传时回退到 self._project_id。
    """

    def __init__(self, project_id: str | None = None) -> None:
        self._project_id = project_id

    async def get_current_time(self, project_id: str) -> dict:
        state = _states.get(project_id) or await self._load_state(project_id)
        _states[project_id] = state
        gs = state["current_game_seconds"]
        return {"game_seconds": gs, "formatted": self._format(gs)}

    async def start(self, project_id: str) -> None:
        state = await self._load_state(project_id)
        _states[project_id] = state
        state["task"] = asyncio.create_task(self._tick_loop(project_id))
        log.info("game_time_start", project_id=project_id,
                 game_seconds=state["current_game_seconds"])

    async def stop(self, project_id: str) -> None:
        state = _states.get(project_id)
        if state and state.get("task"):
            state["task"].cancel()
            try:
                await state["task"]
            except asyncio.CancelledError:
                pass
            state["task"] = None
        await self._persist_time(project_id)
        log.info("game_time_stop", project_id=project_id)

    async def schedule_alarm(self, project_id: str, from_agent_id: str,
                             to_agent_id: str, purpose: str,
                             fire_at_game_seconds: int) -> str:
        alarm_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "INSERT INTO scheduled_alarms (id, project_id, from_agent_id, to_agent_id, "
            "purpose, fire_at_game_seconds, status, fired, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            [alarm_id, project_id, from_agent_id, to_agent_id, purpose,
             fire_at_game_seconds, now_ms])
        _alarm_project[alarm_id] = project_id
        state = _states.get(project_id)
        if state:
            state["alarms"].append({
                "id": alarm_id, "project_id": project_id,
                "from_agent_id": from_agent_id, "to_agent_id": to_agent_id,
                "purpose": purpose, "fire_at_game_seconds": fire_at_game_seconds,
                "fired": False})
        log.info("alarm_scheduled", alarm_id=alarm_id, fire_at=fire_at_game_seconds)
        return alarm_id

    async def cancel_alarm(self, alarm_id: str) -> None:
        project_id = _alarm_project.get(alarm_id)
        if not project_id:
            log.warning("alarm_cancel_no_project", alarm_id=alarm_id)
            return
        await _execute(project_id,
            "UPDATE scheduled_alarms SET status = 'cancelled' WHERE id = ?", [alarm_id])
        state = _states.get(project_id)
        if state:
            state["alarms"] = [a for a in state["alarms"] if a["id"] != alarm_id]
        _alarm_project.pop(alarm_id, None)

    async def get_alarms(self, project_id: str) -> list[dict]:
        rows = await _query(project_id,
            "SELECT id, project_id, from_agent_id, to_agent_id, purpose, "
            "fire_at_game_seconds, status, fired, fired_at, created_at "
            "FROM scheduled_alarms WHERE project_id = ? ORDER BY fire_at_game_seconds ASC",
            [project_id])
        return [dict(r) for r in rows]

    async def tick(self, project_id: str) -> None:
        state = _states.get(project_id) or await self._load_state(project_id)
        _states[project_id] = state
        now = int(time.time())
        elapsed = now - state["real_started_at"]
        new_gs = int(elapsed * GAME_SECONDS_PER_DAY / REAL_SECONDS_PER_GAME_DAY)
        state["current_game_seconds"] = new_gs
        state["tick_count"] += 1
        await self._persist_time(project_id)
        # Fire due alarms
        # C4 fix: _fire_alarm 失败时不标记 fired、不移出内存，下次 tick 重试
        due = [a for a in state["alarms"]
               if not a["fired"] and a["fire_at_game_seconds"] <= new_gs]
        fired_ok = []
        for alarm in due:
            try:
                await self._fire_alarm(alarm)
                alarm["fired"] = True
                fired_ok.append(alarm)
            except Exception as e:
                log.error("alarm_fire_failed", alarm_id=alarm["id"], error=str(e))
                # 不标记 fired，不移出内存，下次 tick 重试
        state["alarms"] = [a for a in state["alarms"] if a not in fired_ok]
        if state["tick_count"] % STALL_CHECK_TICKS == 0:
            await self._check_stalled(project_id)
        log.debug("game_time_tick", project_id=project_id, game_seconds=new_gs)

    # ── Internal ──────────────────────────────────────────────

    async def _tick_loop(self, project_id: str) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            try:
                await self.tick(project_id)
            except Exception as e:
                log.error("game_time_tick_error", project_id=project_id, error=str(e))

    async def _load_state(self, project_id: str) -> dict:
        rows = await _query(project_id,
            "SELECT game_seconds FROM game_time_state WHERE id = 'singleton' LIMIT 1")
        if rows and rows[0]["game_seconds"]:
            gs = rows[0]["game_seconds"]
            real_started = int(time.time()) - int(
                gs * REAL_SECONDS_PER_GAME_DAY / GAME_SECONDS_PER_DAY)
        else:
            gs, real_started = 0, int(time.time())
        alarm_rows = await _query(project_id,
            "SELECT id, project_id, from_agent_id, to_agent_id, purpose, "
            "fire_at_game_seconds FROM scheduled_alarms "
            "WHERE fired = 0 AND status = 'pending' ORDER BY fire_at_game_seconds ASC")
        alarms = []
        for r in alarm_rows:
            _alarm_project[r["id"]] = project_id
            alarms.append({"id": r["id"], "project_id": r["project_id"],
                "from_agent_id": r["from_agent_id"], "to_agent_id": r["to_agent_id"],
                "purpose": r["purpose"],
                "fire_at_game_seconds": r["fire_at_game_seconds"] or 0, "fired": False})
        return {"project_id": project_id, "current_game_seconds": gs,
                "real_started_at": real_started, "alarms": alarms,
                "tick_count": 0, "task": None, "stall_cooldowns": {}}

    async def _persist_time(self, project_id: str) -> None:
        state = _states.get(project_id)
        if not state:
            return
        await _execute(project_id,
            "INSERT OR REPLACE INTO game_time_state (id, project_id, game_seconds, "
            "updated_at) VALUES ('singleton', ?, ?, ?)",
            [project_id, state["current_game_seconds"], int(time.time() * 1000)])

    async def _fire_alarm(self, alarm: dict) -> None:
        # C4 fix: 先发 inbox 消息，成功后再 UPDATE DB 标记 fired
        # 原顺序是先标记 fired 再发消息，inbox 失败则告警永久丢失
        to_agent = alarm.get("to_agent_id")
        if to_agent:
            from hiveweave.services.inbox import InboxService
            msg = f"[ALARM] {alarm.get('purpose', '')}"
            await InboxService().send_message(
                alarm.get("from_agent_id") or to_agent, to_agent, msg,
                message_type="alarm", priority="urgent")
        await _execute(_alarm_project.get(alarm["id"], ""),
            "UPDATE scheduled_alarms SET fired = 1, fired_at = ?, status = 'fired' "
            "WHERE id = ?", [int(time.time() * 1000), alarm["id"]])
        log.info("alarm_fired", alarm_id=alarm["id"], purpose=alarm.get("purpose"))

    async def _check_stalled(self, project_id: str) -> None:
        """Detect stalled agents and escalate (every 60s). Uses updated_at as heartbeat."""
        state = _states.get(project_id)
        if not state:
            return
        agents = await meta_db.query(
            "SELECT id, name, parent_id, updated_at FROM agents "
            "WHERE project_id = ? AND status = 'active'", [project_id])
        now_ms = int(time.time() * 1000)
        for agent in agents:
            aid = agent["id"]
            idle_ms = now_ms - (agent["updated_at"] or now_ms)
            if idle_ms < STALL_IDLE_MS:
                continue
            last = state["stall_cooldowns"].get(aid, 0)
            if now_ms - last < STALL_COOLDOWN_MS:
                continue  # A5: in-memory cooldown, restart loses
            state["stall_cooldowns"][aid] = now_ms
            reason = f"idle for {idle_ms // 60000}min"
            # R13 fix: agent 是 aiosqlite.Row，不支持 .get()，改用 [] 索引
            # （列均由 SELECT 显式查询：id, name, parent_id, updated_at）
            parent_id = agent["parent_id"]
            log.warning("agent_stalled", agent_id=aid, name=agent["name"], reason=reason)
            try:
                if parent_id:
                    from hiveweave.services.inbox import InboxService
                    msg = (f"[ESCALATION] Your subordinate {agent['name'] or aid} "
                           f"appears stalled: {reason}. Please check on them.")
                    await InboxService().send_message(
                        aid, parent_id, msg, message_type="escalation", priority="urgent")
                else:
                    log.warning("ceo_stalled", agent_id=aid, name=agent["name"])
            except Exception as e:
                log.error("escalate_failed", agent_id=aid, error=str(e))

    @staticmethod
    def _format(game_seconds: int) -> str:
        day = game_seconds // GAME_SECONDS_PER_DAY
        rem = game_seconds % GAME_SECONDS_PER_DAY
        return f"Day {day} {rem // 3600:02d}:{(rem % 3600) // 60:02d}"
