"""Game time service — per-project simulated clock (契约 07).

1 real hour = 1 game day (3600s real = 86400s game). 5s tick: advance time,
fire alarms, detect stalls. Absolute time model. Cooldown in-memory (A5).
"""

import asyncio
import os
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ensure_project_db

# ── Constants ────────────────────────────────────────────────

# Minimal environment variable whitelist for alarm script execution.
# Filters out API keys, DB credentials, and other sensitive env vars.
_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "USERNAME", "USERPROFILE",
    "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
    "LANG", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING",
    "NODE_PATH", "NODE_OPTIONS",
    "PROJECT_NAME", "PROJECT_ID",
})

log = structlog.get_logger(__name__)

REAL_SECONDS_PER_GAME_DAY = 3600
GAME_SECONDS_PER_DAY = 86400
TICK_INTERVAL = 5
STALL_CHECK_TICKS = 24        # 24 * 5s = 120s = 2min
STREAMING_SWEEP_TICKS = 6     # 6 * 5s = 30s — auto-heal orphan is_streaming=1
STALL_IDLE_MS = 10 * 60 * 1000        # 10 min idle threshold
STALL_COOLDOWN_MS = 15 * 60 * 1000    # 15 min cooldown (避免重复触发)
STALL_ESCALATION_THRESHOLD = 3        # 同一对未回复触发 3 次后升级到上级
# Sender asked for a reply but got silence — wake waiter sooner than recipient stall
AWAITING_REPLY_MS = 3 * 60 * 1000     # 3 min

# Bug K: task 状态停留超时阈值（毫秒）
# 每个 task 状态有一个"合理停留时间"，超过则催办负责人
TASK_STALL_THRESHOLDS = {
    "running":   20 * 60 * 1000,   # 20 min: assignee 该提交或更新进度
    "submitted": 10 * 60 * 1000,   # 10 min: creator 该审查
    "reviewing": 10 * 60 * 1000,   # 10 min: reviewer 该审批
    "rework":    10 * 60 * 1000,   # 10 min: assignee 该返工
    "created":    5 * 60 * 1000,   # 5 min: assignee 该认领
    "claimed":    5 * 60 * 1000,   # 5 min: assignee 该开始
}
TASK_STALL_COOLDOWN_MS = 15 * 60 * 1000  # 15 min: 同一 task 不重复催

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
        """Start the game-time tick loop (idempotent).

        If a tick loop is already running for this project, keep it and return.
        This prevents duplicate activate() from stacking orphaned tick tasks
        that would double-fire alarms and stall watchdogs.
        """
        existing = _states.get(project_id)
        old_task = existing.get("task") if existing else None
        if old_task is not None and not old_task.done():
            log.info("game_time_start_idempotent_skip", project_id=project_id)
            return

        state = await self._load_state(project_id)
        # Preserve in-memory trackers across restart of a finished task
        if existing:
            for key in ("stall_trackers", "task_stall_trackers", "stall_cooldowns"):
                if key in existing and key not in state:
                    state[key] = existing[key]
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

    async def cancel_alarms_for_agent(self, project_id: str, agent_id: str) -> int:
        """Cancel all pending alarms for an agent (A4 fix).

        Called when an agent is dismissed to prevent alarms from firing
        on an archived agent. Cancels alarms where to_agent_id or
        from_agent_id matches.

        Returns number of alarms cancelled.
        """
        await _execute(project_id,
            "UPDATE scheduled_alarms SET status = 'cancelled' "
            "WHERE (to_agent_id = ? OR from_agent_id = ?) AND status = 'pending'",
            [agent_id, agent_id])
        state = _states.get(project_id)
        cancelled = 0
        if state:
            before = len(state["alarms"])
            state["alarms"] = [
                a for a in state["alarms"]
                if a.get("to_agent_id") != agent_id
                and a.get("from_agent_id") != agent_id
            ]
            cancelled = before - len(state["alarms"])
        log.info("alarms_cancelled_for_agent",
                 agent_id=agent_id, project_id=project_id, count=cancelled)
        return cancelled

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
        # Auto-heal: clear orphan streaming messages (agent idle but is_streaming=1)
        if state["tick_count"] % STREAMING_SWEEP_TICKS == 0:
            await self._sweep_orphan_streaming(project_id)
        # Watchdog: 每 2 分钟检查停滞 agent，直接触发（不经过上级）
        if state["tick_count"] % STALL_CHECK_TICKS == 0:
            await self._check_stalled(project_id)
            await self._nudge_stale_verify(project_id)
        log.debug("game_time_tick", project_id=project_id, game_seconds=new_gs)

    # ── Internal ──────────────────────────────────────────────

    async def _tick_loop(self, project_id: str) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            try:
                await self.tick(project_id)
            except Exception as e:
                log.error("game_time_tick_error", project_id=project_id, error=str(e))

    async def _nudge_stale_verify(self, project_id: str) -> None:
        """Wake assignees of VERIFY children stuck under verifying parents."""
        try:
            from hiveweave.tools.task_tools import nudge_stale_verify_tasks

            await nudge_stale_verify_tasks(project_id)
        except Exception as e:
            log.error(
                "stale_verify_nudge_failed",
                project_id=project_id,
                error=str(e),
            )

    async def _sweep_orphan_streaming(self, project_id: str) -> None:
        """Clear is_streaming=1 rows whose agent is not actively PROCESSING.

        Users must never need a manual/AI 'zombie clear'. Boot clears crash
        leftovers; this runtime sweep catches mid-session orphans within ~30s.
        """
        try:
            from hiveweave.agents.supervisor import agent_manager
            from hiveweave.services.chat_message import ChatMessageService

            protect = {
                aid
                for aid, pid in agent_manager.list_processing()
                if pid == project_id
            }
            await ChatMessageService().clear_orphan_streaming(
                project_id,
                protect_agent_ids=protect,
            )
        except Exception as e:
            log.warning(
                "streaming_sweep_failed",
                project_id=project_id,
                error=str(e),
            )

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
                from hiveweave.tools.bash import _validate_command_safety
                blocked, reason = _validate_command_safety(script)
                if blocked:
                    log.warning("alarm_script_blocked", alarm_id=alarm["id"], reason=reason)
                    # 跳过脚本执行，继续后续 inbox 通知
                else:
                    import asyncio
                    safe_env = {
                        k: v for k, v in os.environ.items()
                        if k.upper() in _SAFE_ENV_KEYS
                    }
                    proc = await asyncio.create_subprocess_shell(
                        script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=safe_env,
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
        """Watchdog: 精准检测"该回复但没回复"的 agent（每 2 分钟）。

        只触发以下情况的 agent:
        1. 有未读的 expect_report=1 inbox 消息（inbox watcher 兜底）
        2. 有已读但未回复的 expect_report=1 inbox 消息（agent 处理了但忘了回复）
        3. 有 accepted handoff 且 expect_report=1 且 reported_up=0（接收了任务上下文但没提交）

        不会触发:
        - 阶段性完成工作、等待新任务的 agent
        - 没有待回复消息的 idle agent
        """
        state = _states.get(project_id)
        if not state:
            return

        now_ms = int(time.time() * 1000)

        # ── Case 1: 未读的 expect_report / 文案要求回复（inbox watcher 兜底）──
        unread_candidates = await _query(project_id,
            "SELECT i.to_agent_id, i.from_agent_id, i.message, i.created_at, i.id, "
            "i.expect_report "
            "FROM inbox i "
            "WHERE i.read = 0 "
            "AND (i.expect_report = 1 OR i.message LIKE '%回复%' "
            "     OR lower(i.message) LIKE '%reply%' "
            "     OR lower(i.message) LIKE '%report back%') "
            f"AND i.created_at < {now_ms - STALL_IDLE_MS} "
            "AND i.to_agent_id IN ("
            "  SELECT id FROM agents WHERE status = 'active')",
            [])

        # ── Case 2: 已读但未回复 ──
        read_candidates = await _query(project_id,
            "SELECT i.to_agent_id, i.from_agent_id, i.message, i.created_at, i.id, "
            "i.expect_report "
            "FROM inbox i "
            "WHERE i.read = 1 "
            "AND (i.expect_report = 1 OR i.message LIKE '%回复%' "
            "     OR lower(i.message) LIKE '%reply%' "
            "     OR lower(i.message) LIKE '%report back%') "
            f"AND i.created_at < {now_ms - STALL_IDLE_MS} "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM inbox r "
            "  WHERE r.from_agent_id = i.to_agent_id "
            "  AND r.to_agent_id = i.from_agent_id "
            "  AND r.created_at > i.created_at"
            ") "
            "AND i.to_agent_id IN ("
            "  SELECT id FROM agents WHERE status = 'active')",
            [])

        from hiveweave.services.reply_policy import message_requests_reply

        def _needs_reply(row: dict) -> bool:
            return bool(row.get("expect_report")) or message_requests_reply(
                row.get("message")
            )

        unread_reply = [m for m in unread_candidates if _needs_reply(m)]
        read_unreplied = [m for m in read_candidates if _needs_reply(m)]

        # ── Case 3: accepted handoff 且 expect_report=1 且 reported_up=0 ──
        unreported_handoffs = await _query(project_id,
            "SELECT h.to_agent_id, h.from_agent_id, h.summary, h.created_at, h.id, h.task_id "
            "FROM handoffs h "
            "WHERE h.status = 'accepted' AND h.expect_report = 1 "
            "AND h.reported_up = 0 "
            f"AND h.created_at < {now_ms - STALL_IDLE_MS} "
            "AND h.to_agent_id IN ("
            "  SELECT id FROM agents WHERE status = 'active')",
            [])

        # 汇总需要触发的 agent → 结构化待办列表
        # trigger_map: agent_id → list of {type, from_id, from_name, message_preview, msg_created_at}
        trigger_map: dict[str, list[dict]] = {}

        # 批量获取所有涉及的 agent name 和 parent_id
        all_agent_ids = set()
        for m in unread_reply + read_unreplied:
            all_agent_ids.add(m["to_agent_id"])
            all_agent_ids.add(m["from_agent_id"])
        for h in unreported_handoffs:
            all_agent_ids.add(h["to_agent_id"])
            all_agent_ids.add(h["from_agent_id"])

        name_map: dict[str, str] = {}
        parent_map: dict[str, str | None] = {}
        if all_agent_ids:
            id_placeholders = ", ".join(["?"] * len(all_agent_ids))
            name_rows = await _query(project_id,
                f"SELECT id, name, parent_id FROM agents WHERE id IN ({id_placeholders})",
                list(all_agent_ids))
            name_map = {r["id"]: r["name"] for r in name_rows}
            parent_map = {r["id"]: r["parent_id"] for r in name_rows}

        def _name(aid: str) -> str:
            return name_map.get(aid, aid[:8])

        # Case 1: 未读的 expect_report 消息
        for m in unread_reply:
            aid = m["to_agent_id"]
            trigger_map.setdefault(aid, []).append({
                "type": "unread",
                "from_id": m["from_agent_id"],
                "from_name": _name(m["from_agent_id"]),
                "preview": (m["message"] or "")[:80],
                "msg_created_at": m["created_at"],
            })

        # Case 2: 已读但未回复的 expect_report 消息
        # 注意：不跳过已出现在 trigger_map 中的 agent —
        # A 可能同时有未读消息（from X）和已读未回复消息（from D, E）
        for m in read_unreplied:
            aid = m["to_agent_id"]
            # 避免重复：如果同一条消息已在 unread_reply 中则跳过
            existing_ids = {e["from_id"] for e in trigger_map.get(aid, []) if e["type"] == "unread"}
            if m["from_agent_id"] in existing_ids:
                continue
            trigger_map.setdefault(aid, []).append({
                "type": "unreplied",
                "from_id": m["from_agent_id"],
                "from_name": _name(m["from_agent_id"]),
                "preview": (m["message"] or "")[:80],
                "msg_created_at": m["created_at"],
            })

        # Case 3: 未报告的 handoff
        for h in unreported_handoffs:
            aid = h["to_agent_id"]
            trigger_map.setdefault(aid, []).append({
                "type": "unreported",
                "from_id": h["from_agent_id"],
                "from_name": _name(h["from_agent_id"]),
                "preview": (h["summary"] or "")[:80],
                "msg_created_at": h["created_at"],
            })

        # ── Case 4: task 状态停留超时（Bug K）──
        # 只检查已"上班"的项目
        from hiveweave.db import meta as _meta_db
        _proj = await _meta_db.query_one(
            "SELECT is_started FROM projects WHERE id = ?", [project_id]
        )
        if not _proj or not dict(_proj).get("is_started"):
            return

        # 扫描所有非终态 task，检查在当前状态停留是否超过阈值
        # 按 assignee/creator 分组合并消息，每个 agent 只 trigger 一次
        # blocked 故意不入选：合法等待不按沉默催办
        for col, coldef in (("wait_kind", "TEXT"), ("wake_at", "INTEGER")):
            try:
                await _execute(
                    project_id,
                    f"ALTER TABLE tasks ADD COLUMN {col} {coldef}",
                )
            except Exception:
                pass

        try:
            stalled_tasks = await _query(project_id,
                "SELECT id, title, status, assignee_id, creator_id, "
                "updated_at, submitted_at, claimed_at, wake_at "
                "FROM tasks WHERE status IN ('created','claimed','running',"
                "'submitted','reviewing','rework') "
                "AND is_archived = 0",
                [])
        except Exception:
            stalled_tasks = await _query(project_id,
                "SELECT id, title, status, assignee_id, creator_id, "
                "updated_at, submitted_at, claimed_at "
                "FROM tasks WHERE status IN ('created','claimed','running',"
                "'submitted','reviewing','rework') "
                "AND is_archived = 0",
                [])

        # handoff 存在 ⇒ 已 dispatch；纯 create 入队无 handoff
        handoff_task_ids: set[str] = set()
        try:
            hrows = await _query(
                project_id,
                "SELECT DISTINCT task_id FROM handoffs WHERE task_id IS NOT NULL",
                [],
            )
            handoff_task_ids = {
                r["task_id"] for r in hrows if r["task_id"]
            }
        except Exception:
            handoff_task_ids = set()

        # 有未来 alarm 的 agent：跳过 task stall（主动等待）
        agents_with_future_alarms: set[str] = set()
        try:
            state_gs = int(
                (_states.get(project_id) or {}).get("current_game_seconds") or 0
            )
            arows = await _query(
                project_id,
                "SELECT DISTINCT to_agent_id FROM scheduled_alarms "
                "WHERE fired = 0 AND status = 'pending' "
                "AND fire_at_game_seconds > ?",
                [state_gs],
            )
            agents_with_future_alarms = {
                r["to_agent_id"] for r in arows if r["to_agent_id"]
            }
        except Exception:
            agents_with_future_alarms = set()

        # 按 agent 分组超时 task
        # key: agent_id, value: list of {task_id, title, status, stall_ms}
        task_trigger_map: dict[str, list[dict]] = {}
        for t in stalled_tasks:
            status = t["status"]
            threshold = TASK_STALL_THRESHOLDS.get(status)
            if not threshold:
                continue
            # Future wake_at → still legally waiting on a timer
            try:
                wake_at = t["wake_at"]
            except (KeyError, IndexError, TypeError):
                wake_at = None
            if wake_at is not None and int(wake_at) > now_ms:
                continue
            # 确定状态进入时间
            if status == "submitted":
                entered_at = t["submitted_at"] or t["updated_at"]
            elif status == "claimed":
                entered_at = t["claimed_at"] or t["updated_at"]
            else:
                entered_at = t["updated_at"]
            if not entered_at:
                continue
            stall_ms = now_ms - entered_at
            # Activity stretch: recent updates buy more silence budget
            updated_at = t["updated_at"] or entered_at
            activity_age = now_ms - int(updated_at)
            if activity_age < 3 * 60 * 1000:
                effective = int(threshold * 2.0)
            elif activity_age < 10 * 60 * 1000:
                effective = int(threshold * 1.5)
            else:
                effective = threshold
            if stall_ms < effective:
                continue
            # 确定负责人：
            # - submitted/reviewing → creator
            # - created 且从未 dispatch（无 handoff）→ creator（队列入账，催派发）
            # - 其他 → assignee
            if status in ("submitted", "reviewing"):
                responsible = t["creator_id"]
            elif status == "created" and t["id"] not in handoff_task_ids:
                responsible = t["creator_id"]
            else:
                responsible = t["assignee_id"]
            if not responsible:
                continue
            if responsible in agents_with_future_alarms:
                continue
            task_trigger_map.setdefault(responsible, []).append({
                "task_id": t["id"],
                "title": t["title"] or "(untitled)",
                "status": status,
                "stall_ms": stall_ms,
                "queued_undispatched": (
                    status == "created" and t["id"] not in handoff_task_ids
                ),
            })

        # 处理超时 task：按 agent 合并消息 + trigger
        if task_trigger_map:
            # 补充 name_map
            for aid in task_trigger_map:
                if aid not in name_map:
                    all_agent_ids.add(aid)
            if all_agent_ids:
                id_placeholders = ", ".join(["?"] * len(all_agent_ids))
                name_rows = await _query(project_id,
                    f"SELECT id, name, parent_id FROM agents WHERE id IN ({id_placeholders})",
                    list(all_agent_ids))
                name_map = {r["id"]: r["name"] for r in name_rows}
                parent_map = {r["id"]: r["parent_id"] for r in name_rows}

            # task stall 冷却追踪器（独立于 inbox stall）
            if "task_stall_trackers" not in state:
                state["task_stall_trackers"] = {}
            task_trackers = state["task_stall_trackers"]

            for aid, tasks in task_trigger_map.items():
                # Skip archived / non-active agents (dead mailbox)
                agent_row = None
                try:
                    arows = await _query(
                        project_id,
                        "SELECT id, name, status FROM agents WHERE id = ? LIMIT 1",
                        [aid],
                    )
                    agent_row = arows[0] if arows else None
                except Exception:
                    agent_row = None
                if not agent_row or (agent_row.get("status") or "") != "active":
                    continue

                # 冷却检查：跳过最近催过的 task
                pending_tasks = []
                for t in tasks:
                    tracker = task_trackers.get(t["task_id"], {"ts": 0})
                    if now_ms - tracker["ts"] < TASK_STALL_COOLDOWN_MS:
                        continue
                    task_trackers[t["task_id"]] = {"ts": now_ms}
                    pending_tasks.append(t)

                if not pending_tasks:
                    continue

                agent_name = _name(aid)

                # 构建合并消息
                status_labels = {
                    "running": "进行中，请提交成果或更新进度",
                    "submitted": "已提交，等待你的审查",
                    "reviewing": "审查中，请完成审批",
                    "rework": "需返工，请尽快处理",
                    "created": "待认领，请开始处理",
                    "claimed": "已认领，请开始执行",
                }
                lines = []
                for t in pending_tasks:
                    minutes = int(t["stall_ms"] / 60000)
                    if t.get("queued_undispatched"):
                        label = "仅入队未派发，请 dispatch_task(taskId=...) 叫醒执行者"
                    else:
                        label = status_labels.get(t["status"], "需推进")
                    lines.append(
                        f"  - [{t['title']}] 状态：{t['status']}（{label}），"
                        f"已停留 {minutes} 分钟"
                    )
                msg = (
                    "[TASK WATCHDOG] 以下任务需要你推进：\n"
                    + "\n".join(lines)
                    + "\n请尽快处理：提交成果(submit_task)、审查(review_task)、"
                    "派发(dispatch_task)或更新进度。"
                )

                log.warning("task_stall_trigger",
                            project_id=project_id,
                            agent_id=aid, name=agent_name,
                            stalled_tasks=len(pending_tasks),
                            statuses=[t["status"] for t in pending_tasks])

                try:
                    from hiveweave.services.inbox import InboxService
                    inbox = InboxService()
                    # Upsert semantics: clear prior watchdog nudges first
                    await inbox.supersede_watchdog_messages(aid)
                    await inbox.send_message(
                        "system", aid, msg,
                        message_type="system", priority="urgent")

                    from hiveweave.agents.trigger import trigger_subordinate
                    await trigger_subordinate(aid)
                except Exception as e:
                    log.error("task_stall_trigger_failed",
                              agent_id=aid, error=str(e))

        # ── Case 5: 等待方自救 — 发出「请回复」后对方一直没回 ──
        # 覆盖 expect_report=1 与文案要求回复但漏标 flag 的情况
        await self._nudge_awaiting_replies(project_id, now_ms, name_map)

        if not trigger_map:
            return

        log.info("watchdog_scan",
                 project_id=project_id,
                 agents_to_trigger=len(trigger_map),
                 unread=len(unread_reply),
                 unreplied=len(read_unreplied),
                 unreported=len(unreported_handoffs))

        # 初始化 stall_trackers: {agent_id: {sender_id: {ts, count}}}
        if "stall_trackers" not in state:
            state["stall_trackers"] = {}

        # 清理已回复的 tracker：
        # 如果 A 当前不再出现在 trigger_map 中，说明所有消息已回复，重置计数
        for aid in list(state["stall_trackers"].keys()):
            if aid not in trigger_map:
                del state["stall_trackers"][aid]

        # 对于仍在 trigger_map 中的 agent，清理已回复的 sender
        for aid, items in trigger_map.items():
            trackers = state["stall_trackers"].get(aid, {})
            current_senders = {it["from_id"] for it in items}
            # 删除不再 pending 的 sender
            for sid in list(trackers.keys()):
                if sid not in current_senders:
                    del trackers[sid]

        # 只触发需要回复的 agent
        for aid, items in trigger_map.items():
            trackers = state["stall_trackers"].setdefault(aid, {})

            # 按 sender 分组，检查每个 sender 的触发次数
            # 过滤掉已在 cooldown 内的 sender
            pending_items = []
            escalated_senders = []
            for it in items:
                sender_id = it["from_id"]
                tracker = trackers.get(sender_id, {"ts": 0, "count": 0})

                # cooldown 检查（按 sender 粒度）
                if now_ms - tracker["ts"] < STALL_COOLDOWN_MS:
                    continue

                # 更新计数
                tracker["ts"] = now_ms
                tracker["count"] += 1
                trackers[sender_id] = tracker

                if tracker["count"] >= STALL_ESCALATION_THRESHOLD:
                    escalated_senders.append(it)
                else:
                    pending_items.append(it)

            if not pending_items and not escalated_senders:
                continue

            agent_name = _name(aid)

            log.warning("watchdog_trigger",
                        agent_id=aid, name=agent_name,
                        pending_count=len(pending_items),
                        escalated_count=len(escalated_senders),
                        senders=[it["from_name"] for it in items])

            try:
                from hiveweave.services.inbox import InboxService
                inbox = InboxService()

                # ── 1. 给 A 发精准通知（列出已回复和未回复的人）──
                if pending_items:
                    # 查询 A 在最近已经回复了哪些人（用于对比显示）
                    replied_senders = set()
                    all_expect_senders = {it["from_id"] for it in items}
                    for sid in all_expect_senders:
                        # 检查 A 是否在 D 的消息之后给 D 发过消息
                        replied_rows = await _query(project_id,
                            "SELECT 1 FROM inbox r "
                            "WHERE r.from_agent_id = ? AND r.to_agent_id = ? "
                            "AND r.created_at > ? LIMIT 1",
                            [aid, sid, items[0]["msg_created_at"]])
                        if replied_rows:
                            replied_senders.add(sid)

                    lines = []
                    for it in pending_items:
                        tag = {"unread": "未读", "unreplied": "已读未回复", "unreported": "未报告"}[it["type"]]
                        lines.append(
                            f"  ❌ [{tag}] {it['from_name']}：{it['preview']}"
                        )

                    replied_names = [_name(sid) for sid in replied_senders if sid not in {i["from_id"] for i in pending_items}]
                    header = "[WATCHDOG] 以下人员正在等待你的回复，你尚未回复：\n"
                    if replied_names:
                        header = f"[WATCHDOG] 你已回复：{', '.join(replied_names)}，但以下人员仍未收到你的回复：\n"

                    msg = (
                        header
                        + "\n".join(lines)
                        + "\n请调用 send_message(recipients=['花名'], message='...') "
                        "回复上述每一位。注意：回复给其他人不算回复给这些人。"
                    )
                    # Upsert: clear prior [WATCHDOG] nudges before inserting
                    await inbox.supersede_watchdog_messages(
                        aid, prefixes=["[WATCHDOG]"]
                    )
                    await inbox.send_message(
                        "system", aid, msg,
                        message_type="system", priority="urgent")

                    from hiveweave.agents.trigger import trigger_subordinate
                    await trigger_subordinate(aid)

                # ── 2. 升级到 A 的上级 ──
                if escalated_senders:
                    parent_id = parent_map.get(aid)
                    if parent_id:
                        esc_lines = []
                        for it in escalated_senders:
                            esc_lines.append(
                                f"  - {agent_name} 已 {STALL_ESCALATION_THRESHOLD} 次未回复 "
                                f"{it['from_name']} 的消息：{it['preview']}"
                            )
                        esc_msg = (
                            f"[WATCHDOG ESCALATION] 你的下属 {agent_name} "
                            f"多次未能回复以下人员，请直接介入协调：\n"
                            + "\n".join(esc_lines)
                        )
                        await inbox.send_message(
                            "system", parent_id, esc_msg,
                            message_type="system", priority="urgent")

                        from hiveweave.agents.trigger import trigger_subordinate
                        await trigger_subordinate(parent_id)

                        log.warning("watchdog_escalated",
                                    agent_id=aid, name=agent_name,
                                    parent_id=parent_id,
                                    escalated_senders=[it["from_name"] for it in escalated_senders])
                    else:
                        # A 没有上级（CEO），直接再触发一次
                        log.warning("watchdog_cea_unesclatable",
                                    agent_id=aid, name=agent_name)

            except Exception as e:
                log.error("watchdog_trigger_failed",
                          agent_id=aid, error=str(e))

    async def _nudge_awaiting_replies(
        self,
        project_id: str,
        now_ms: int,
        name_map: dict[str, str],
    ) -> None:
        """Wake senders who asked for a reply but got silence (Case 5).

        Recipient-side stall (Cases 1–2) may also fire; this wakes the *waiter*
        so they can follow up or proceed (e.g. dispatch_task) instead of
        hanging forever on \"等待回复\".
        """
        state = _states.get(project_id)
        if not state:
            return

        from hiveweave.services.reply_policy import message_requests_reply

        candidates = await _query(
            project_id,
            "SELECT i.id, i.from_agent_id, i.to_agent_id, i.message, "
            "i.created_at, i.expect_report "
            "FROM inbox i "
            "WHERE i.from_agent_id IN ("
            "  SELECT id FROM agents WHERE status = 'active') "
            "AND i.to_agent_id IN ("
            "  SELECT id FROM agents WHERE status = 'active') "
            "AND (i.expect_report = 1 OR i.message LIKE '%回复%' "
            "     OR lower(i.message) LIKE '%reply%' "
            "     OR lower(i.message) LIKE '%report back%') "
            f"AND i.created_at < {now_ms - AWAITING_REPLY_MS} "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM inbox r "
            "  WHERE r.from_agent_id = i.to_agent_id "
            "  AND r.to_agent_id = i.from_agent_id "
            "  AND r.created_at > i.created_at"
            ")",
            [],
        )

        by_waiter: dict[str, list[dict]] = {}
        for m in candidates:
            if not (
                m.get("expect_report") or message_requests_reply(m.get("message"))
            ):
                continue
            by_waiter.setdefault(m["from_agent_id"], []).append(m)

        if not by_waiter:
            return

        # Fill missing names
        missing = [aid for aid in by_waiter if aid not in name_map]
        for mlist in by_waiter.values():
            for m in mlist:
                tid = m["to_agent_id"]
                if tid not in name_map:
                    missing.append(tid)
        if missing:
            ph = ", ".join(["?"] * len(set(missing)))
            rows = await _query(
                project_id,
                f"SELECT id, name FROM agents WHERE id IN ({ph})",
                list(set(missing)),
            )
            for r in rows:
                name_map[r["id"]] = r["name"]

        if "await_reply_trackers" not in state:
            state["await_reply_trackers"] = {}
        trackers: dict = state["await_reply_trackers"]

        for waiter_id, msgs in by_waiter.items():
            tracker = trackers.get(waiter_id, {"ts": 0})
            if now_ms - tracker["ts"] < STALL_COOLDOWN_MS:
                continue
            trackers[waiter_id] = {"ts": now_ms}

            # Dedupe by recipient
            seen: set[str] = set()
            lines: list[str] = []
            for m in msgs:
                tid = m["to_agent_id"]
                if tid in seen:
                    continue
                seen.add(tid)
                tname = name_map.get(tid, tid[:8])
                minutes = max(1, int((now_ms - int(m["created_at"])) / 60000))
                preview = (m.get("message") or "")[:40].replace("\n", " ")
                lines.append(f"  - {tname}（已等待 {minutes} 分钟）：{preview}")

            if not lines:
                continue

            msg = (
                "[AWAITING REPLIES] 你发出的消息要求对方回复，但以下人员尚未"
                "通过 send_message 回复你：\n"
                + "\n".join(lines)
                + "\n不要无限空等。请二选一：\n"
                "1) send_message 催促未回复者（expectReport=true）\n"
                "2) 若已足够推进（如工具验证可视为完成）→ 立刻用 "
                "dispatch_task / create_task 进入下一阶段"
            )

            log.warning(
                "awaiting_reply_nudge",
                project_id=project_id,
                waiter_id=waiter_id,
                waiter=name_map.get(waiter_id, waiter_id[:8]),
                pending=len(lines),
            )
            try:
                from hiveweave.services.inbox import InboxService
                from hiveweave.agents.trigger import trigger_subordinate

                await InboxService().send_message(
                    "system",
                    waiter_id,
                    msg,
                    message_type="system",
                    priority="urgent",
                )
                await trigger_subordinate(waiter_id)
            except Exception as e:
                log.error(
                    "awaiting_reply_nudge_failed",
                    waiter_id=waiter_id,
                    error=str(e),
                )

    @staticmethod
    def _format(game_seconds: int) -> str:
        day = game_seconds // GAME_SECONDS_PER_DAY
        rem = game_seconds % GAME_SECONDS_PER_DAY
        return f"Day {day} {rem // 3600:02d}:{(rem % 3600) // 60:02d}"
