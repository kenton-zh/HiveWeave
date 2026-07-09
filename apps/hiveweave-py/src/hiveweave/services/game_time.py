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
    # BUG-020 修复：切项目后 DB 可能尚未创建/迁移，强制 ensure schema 后再返回连接
    try:
        return await ensure_project_db(workspace)
    except Exception as e:
        log.error("game_time.ensure_project_db_failed",
                  project_id=project_id, workspace=workspace, error=str(e))
        raise


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
        # BUG-005 修复：返回 real_started_at 让前端能做纯本地时间计算，
        # 不再需要每秒 HTTP poll 拉 formatted 字符串。
        return {
            "game_seconds": gs,
            "formatted": self._format(gs),
            "real_started_at": state.get("real_started_at", int(time.time())),
            "real_seconds_per_game_day": REAL_SECONDS_PER_GAME_DAY,
        }

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
                             fire_at_game_seconds: int,
                             repeat_interval_seconds: int = 0,
                             script_command: str = "") -> str:
        alarm_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "INSERT INTO scheduled_alarms (id, project_id, from_agent_id, to_agent_id, "
            "purpose, fire_at_game_seconds, repeat_interval_seconds, script_command, "
            "status, fired, run_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, ?)",
            [alarm_id, project_id, from_agent_id, to_agent_id, purpose,
             fire_at_game_seconds, repeat_interval_seconds, script_command, now_ms])
        _alarm_project[alarm_id] = project_id
        state = _states.get(project_id)
        if state:
            state["alarms"].append({
                "id": alarm_id, "project_id": project_id,
                "from_agent_id": from_agent_id, "to_agent_id": to_agent_id,
                "purpose": purpose, "fire_at_game_seconds": fire_at_game_seconds,
                "repeat_interval_seconds": repeat_interval_seconds,
                "script_command": script_command,
                "fired": False, "run_count": 0})
        log.info("alarm_scheduled", alarm_id=alarm_id, fire_at=fire_at_game_seconds,
                 recurring=repeat_interval_seconds > 0)
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
        # Fire due alarms (OpenClaw ordering: run BEFORE recompute, not after)
        due = [a for a in state["alarms"]
               if not a["fired"] and a["fire_at_game_seconds"] <= new_gs]
        for alarm in due:
            try:
                result = await self._fire_alarm(alarm)
                if result:
                    # Recurring: update in-memory state with new fire_at
                    for i, a in enumerate(state["alarms"]):
                        if a["id"] == alarm["id"]:
                            state["alarms"][i] = result
                            break
                else:
                    # One-shot: mark fired, remove from active list
                    alarm["fired"] = True
            except Exception as e:
                log.error("alarm_fire_failed", alarm_id=alarm["id"], error=str(e))
        state["alarms"] = [a for a in state["alarms"] if not a["fired"]]
        # idle escalation 已禁用 — 原 _check_stalled 每 10 分钟给 idle agent 的
        # superior 发 escalation，导致 CEO 陷入循环（HR/QA idle → escalation →
        # CEO 回复"待命" → 10min 后再次 escalation）。如果需要恢复，取消下行注释。
        # if state["tick_count"] % STALL_CHECK_TICKS == 0:
        #     await self._check_stalled(project_id)
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

    async def _fire_alarm(self, alarm: dict) -> dict | None:
        """Fire an alarm. Returns updated alarm dict if recurring, None if one-shot.

        C4 fix ordering preserved: send message/execute script BEFORE marking DB,
        so failures don't lose the alarm.
        """
        # 1. Execute script if bound
        script = alarm.get("script_command", "")
        if script:
            try:
                import asyncio
                proc = await asyncio.create_subprocess_shell(
                    script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=120
                )
                if proc.returncode != 0:
                    log.warning("alarm_script_failed", alarm_id=alarm["id"],
                                rc=proc.returncode, stderr=stderr.decode()[:200])
            except Exception as e:
                log.error("alarm_script_error", alarm_id=alarm["id"], error=str(e))

        # 2. Send inbox notification — JSON format, sender identifies the creator
        to_agent = alarm.get("to_agent_id")
        if to_agent:
            from hiveweave.services.inbox import InboxService
            from hiveweave.agents.trigger import _agent_name as _alarm_agent_name
            import json
            from_id = alarm.get("from_agent_id") or ""
            repeat = alarm.get("repeat_interval_seconds", 0) or 0
            # Resolve creator name: self-alarm → "你自己的闹钟", other → "XXX 的闹钟"
            if from_id == to_agent:
                sender = "你自己的闹钟"
            elif from_id:
                creator_name = await _alarm_agent_name(from_id)
                sender = f"{creator_name}的闹钟"
            else:
                sender = "闹钟"
            entry = {"from": sender, "content": alarm.get("purpose", "")}
            if repeat > 0:
                entry["content"] = f"[每{repeat}游戏秒] {entry['content']}"
            msg = json.dumps(entry, ensure_ascii=False)
            await InboxService().send_message(
                from_agent_id=from_id or to_agent, to_agent_id=to_agent, message=msg,
                message_type="alarm", priority="normal")

        # 3. Update DB
        now_ms = int(time.time() * 1000)
        repeat = alarm.get("repeat_interval_seconds", 0) or 0
        if repeat > 0:
            # Recurring: advance fire_at, increment run_count
            new_fire_at = alarm["fire_at_game_seconds"] + repeat
            await _execute(_alarm_project.get(alarm["id"], ""),
                "UPDATE scheduled_alarms SET fired_at = ?, last_fired_at = ?, "
                "fire_at_game_seconds = ?, run_count = run_count + 1 "
                "WHERE id = ?",
                [now_ms, now_ms, new_fire_at, alarm["id"]])
            alarm["fire_at_game_seconds"] = new_fire_at
            alarm["run_count"] = (alarm.get("run_count", 0) or 0) + 1
            alarm["fired"] = False  # re-arm
            log.info("alarm_recurring_fired", alarm_id=alarm["id"],
                     next_fire=new_fire_at, run_count=alarm["run_count"])
            return alarm
        else:
            # One-shot: mark fired
            await _execute(_alarm_project.get(alarm["id"], ""),
                "UPDATE scheduled_alarms SET fired = 1, fired_at = ?, status = 'fired' "
                "WHERE id = ?", [now_ms, alarm["id"]])
            log.info("alarm_fired", alarm_id=alarm["id"], purpose=alarm.get("purpose"))
            return None

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
