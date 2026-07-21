"""Game time service — per-project simulated clock (契约 07).

1 real hour = 1 game day (3600s real = 86400s game). 5s tick: advance time,
fire alarms, detect stalls. Absolute time model. Cooldown in-memory (A5).
"""

import asyncio
import os
import shlex
import time
import uuid

import aiosqlite
import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ProjectDbError, ensure_project_db

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
WORKTREE_RECONCILE_TICKS = 72  # 72 * 5s = 6min — retry orphan worktree cleanup
STALL_IDLE_MS = 10 * 60 * 1000        # 10 min idle threshold
STALL_COOLDOWN_MS = 15 * 60 * 1000    # 15 min cooldown (避免重复触发)
STALL_ESCALATION_THRESHOLD = 3        # 同一对未回复触发 3 次后升级到上级
# Sender asked for a reply but got silence — wake waiter sooner than recipient stall
AWAITING_REPLY_MS = 3 * 60 * 1000     # 3 min

# 潮汐事故: agent 沉默观测 — 无任何产出的失联检测（消息轴/任务轴看门狗的盲区）
SILENCE_THRESHOLD_MS = 10 * 60 * 1000        # 10 min 无任何产出判定失联
SILENCE_NOTIFY_MS = 30 * 60 * 1000           # 失联持续 30 min 升级通知上级
SILENCE_NOTIFY_COOLDOWN_MS = 30 * 60 * 1000  # 同一 agent 上级通知 30 min 冷却
# 合法等待 disposition（commit_turn 落盘 wait contract 时的内存映射，见 turn_exit）
_WAITING_DISPOSITIONS = frozenset({
    "waiting_human", "waiting_agent", "waiting_timer", "blocked",
})

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

# Ledger re-nudge (replaces disabled stall NL nudges) — structured fields only
LEDGER_REVIEW_STALE_MS = 10 * 60 * 1000   # submitted|reviewing
MERGE_PENDING_STALE_MS = 5 * 60 * 1000    # approved awaiting merge
LEDGER_NUDGE_COOLDOWN_MS = 15 * 60 * 1000
PEER_REVIEW_DEADLOCK_MS = 10 * 60 * 1000
PEER_REVIEW_DEADLOCK_COOLDOWN_MS = 30 * 60 * 1000
MERGE_PROXY_STALE_MS = 15 * 60 * 1000     # escalate to parent with MERGE
MERGE_PROXY_COOLDOWN_MS = 30 * 60 * 1000

_states: dict[str, dict] = {}          # project_id → state
_alarm_project: dict[str, str] = {}    # alarm_id → project_id


async def _conn(project_id: str) -> aiosqlite.Connection:
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
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

    async def _watchdog_trigger(
        self, agent_id: str, *, role: str | None = None
    ) -> None:
        """Wake an agent after a watchdog nudge.

        Skips ``complete`` agents with no open obligations (quota burn).
        Uses ``trigger_coordinator`` for coordinator roles so empty-pending
        backgrounds do not start an LLM turn.
        """
        from hiveweave.agents.supervisor import agent_manager
        from hiveweave.agents.trigger import (
            is_coordinator,
            trigger_coordinator,
            trigger_subordinate,
        )

        inst = agent_manager.get_agent(agent_id)
        if inst is not None and getattr(inst, "disposition", None) == "complete":
            try:
                from hiveweave.services.task import TaskService

                obs = await TaskService().get_actionable_obligations(
                    inst.project_id, agent_id
                )
                if not obs:
                    log.info("watchdog_skip_complete", agent_id=agent_id)
                    return
            except Exception as e:
                log.debug("watchdog_complete_check_failed", error=str(e))

        r = role
        if r is None and inst is not None:
            r = (getattr(inst, "config", None) or {}).get("role", "")
        if is_coordinator(r or ""):
            await trigger_coordinator(agent_id)
        else:
            await trigger_subordinate(agent_id)

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
            for key in ("stall_trackers", "task_stall_trackers", "stall_cooldowns",
                        "silence_trackers", "ledger_nudge_cooldowns"):
                if key in existing and key not in state:
                    state[key] = existing[key]
        # Duty session baseline — overnight wall-clock must not stampede nudges
        state["duty_session_started_at_ms"] = int(time.time() * 1000)
        _states[project_id] = state
        state["task"] = asyncio.create_task(self._tick_loop(project_id))
        log.info("game_time_start", project_id=project_id,
                 game_seconds=state["current_game_seconds"],
                 duty_session_started_at_ms=state["duty_session_started_at_ms"])
        # 重启/激活恢复：立即处理已到期 wait 并武装未到期 wait 定时器
        # （tick 运行时幂等；off-duty 期间也保证超时唤醒可达）
        try:
            await self.recover_wait_timeouts(project_id)
        except Exception as e:
            log.warning("wait_recovery_on_start_failed",
                        project_id=project_id, error=str(e))

    async def stop(self, project_id: str) -> None:
        state = _states.get(project_id)
        self.cancel_wait_recovery_timers(project_id)
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
        # P0 Phase 2: Wait TTL expiry + SCC cycle break (every tick)
        try:
            await self._process_wait_contracts(project_id)
        except Exception as e:
            log.error("wait_contract_tick_failed", project_id=project_id, error=str(e))
        # Auto-heal: clear orphan streaming messages (agent idle but is_streaming=1)
        if state["tick_count"] % STREAMING_SWEEP_TICKS == 0:
            await self._sweep_orphan_streaming(project_id)
        # D5: periodic worktree reconcile (merge cleanup failures / orphans)
        if state["tick_count"] % WORKTREE_RECONCILE_TICKS == 0:
            try:
                await self._reconcile_worktrees(project_id)
            except Exception as e:
                log.error(
                    "worktree_reconcile_tick_failed",
                    project_id=project_id,
                    error=str(e),
                )
        # Watchdog: 每 2 分钟检查停滞 agent，直接触发（不经过上级）
        if state["tick_count"] % STALL_CHECK_TICKS == 0:
            await self._check_stalled(project_id)
            await self._nudge_stale_verify(project_id)
            try:
                await self._nudge_stale_ledger(project_id)
            except Exception as e:
                log.error(
                    "stale_ledger_nudge_failed",
                    project_id=project_id,
                    error=str(e),
                )
            # 沉默观测（失联检测）：独立 try/except，故障不拖累既有看门狗
            try:
                await self._check_silent_agents(project_id)
            except Exception as e:
                log.error("silence_watchdog_failed",
                          project_id=project_id, error=str(e))
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

    async def _reconcile_worktrees(self, project_id: str) -> None:
        """Retry orphan worktree cleanup after merge soft-failures.

        Order matches supervisor activate: heal missing executor worktrees
        first, then reconcile orphans (TEST3 — avoid deleting active dirs).
        """
        from hiveweave.services.git_worktree import (
            heal_project_executor_worktrees,
            reconcile_worktrees,
        )
        from hiveweave.services.worktree_review import project_main_workspace

        try:
            healed = await heal_project_executor_worktrees(project_id)
            if healed and not healed.get("skipped"):
                log.info(
                    "worktree_heal_tick",
                    project_id=project_id,
                    recovered=healed.get("recovered"),
                    failed=healed.get("failed"),
                )
        except Exception as e:
            log.warning(
                "worktree_heal_tick_failed",
                project_id=project_id,
                error=str(e),
            )

        workspace_path = await project_main_workspace(project_id)
        if not workspace_path:
            return
        result = await reconcile_worktrees(workspace_path)
        if result and (
            result.get("removed_dirs")
            or result.get("skipped_active_dirs")
            or result.get("reattached_dirs")
            or result.get("preserved_branches")
            or result.get("deleted_branches")
        ):
            log.info(
                "worktree_reconcile_tick",
                project_id=project_id,
                result=result,
            )

    async def _process_wait_contracts(self, project_id: str) -> None:
        """Expire waits (WAIT_TIMEOUT) and break agent wait cycles."""
        from hiveweave.services.inbox import InboxService
        from hiveweave.services.org import OrgService
        from hiveweave.services.wait_contract import wait_contract_service

        await wait_contract_service.backfill_null_expires(project_id)
        cleared = await wait_contract_service.clear_expired(project_id)
        inbox = InboxService()
        for w in cleared:
            aid = w.get("agentId") or ""
            if not aid:
                continue
            kind = w.get("kind") or "?"
            ref = w.get("ref") or "?"
            try:
                await inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=aid,
                    message=(
                        f"[WAIT_TIMEOUT] Your wait ({kind}:{ref}) expired. "
                        "Resume work or re-establish a wait."
                    ),
                    message_type="system",
                    priority="urgent",
                )
                await self._watchdog_trigger(aid)
            except Exception as e:
                log.warning(
                    "wait_timeout_notify_failed",
                    agent_id=aid,
                    error=str(e),
                )

        agents = await OrgService().list_agents(project_id)
        by_key: dict[str, str] = {}
        for a in agents:
            aid = a.get("id") or ""
            if not aid:
                continue
            by_key[aid.lower()] = aid
            if a.get("short_id"):
                by_key[str(a["short_id"]).lower()] = aid
            if a.get("name"):
                by_key[str(a["name"]).lower()] = aid

        def resolve_ref(ref: str) -> str | None:
            r = (ref or "").strip().lower()
            if not r:
                return None
            if r in by_key:
                return by_key[r]
            for k, v in by_key.items():
                if len(r) >= 4 and (k.startswith(r) or r.startswith(k)):
                    return v
            return None

        breaks = await wait_contract_service.break_wait_cycles(
            project_id, resolve_ref
        )
        for b in breaks:
            members = list(b.get("memberIds") or b.get("cycle") or [])
            cycle = b.get("cycle") or members
            if not members and b.get("breakerId"):
                members = [b["breakerId"]]
            for member in members:
                if not member:
                    continue
                try:
                    await inbox.send_message(
                        from_agent_id="system",
                        to_agent_id=member,
                        message=(
                            f"[WAIT_CYCLE] Deadlock broken among {cycle}. "
                            "Your waits were cleared — resume or re-wait."
                        ),
                        message_type="system",
                        priority="urgent",
                        wake=True,
                    )
                    await self._watchdog_trigger(member)
                except Exception as e:
                    log.warning(
                        "wait_cycle_notify_failed",
                        member=member,
                        error=str(e),
                    )
            # Ping common superior (parent of first member) if outside cycle
            breaker = members[0] if members else ""
            parent_id = None
            for a in agents:
                if a.get("id") == breaker:
                    parent_id = a.get("parent_id")
                    break
            if parent_id and parent_id not in cycle:
                try:
                    await inbox.send_message(
                        from_agent_id="system",
                        to_agent_id=parent_id,
                        message=(
                            f"[WAIT_CYCLE] Subordinate wait deadlock broken "
                            f"(cycle={cycle})."
                        ),
                        message_type="system",
                        priority="normal",
                    )
                except Exception:
                    pass

    async def recover_wait_timeouts(self, project_id: str) -> dict:
        """重启恢复：重建 wait 超时的"闹钟"语义（P1 — TEST9 停摆）。

        背景：agent_waits 表持久化了 parked wait 及 expires_at，但超时
        唤醒依赖 tick loop 的 _process_wait_contracts；后端重启后项目
        一律 off-duty（is_started=0，Bug K），tick 不跑，超时永不触发，
        parked agent 永久停摆。

        恢复策略（幂等，可在 lifespan / activate 重复调用）：
        1. 已到期 wait → 立即走 _process_wait_contracts（清除 +
           [WAIT_TIMEOUT] + 唤醒），与 tick 路径完全一致。
        2. 未到期 wait → 为每个 wait 武装一次性 asyncio 定时器，到期即
           触发一次 _process_wait_contracts。tick loop 若已运行，两者
           幂等共存（clear_expired 只清一次）；项目 stop 时统一取消。

        Returns: {"expired_processed": bool, "armed": int}
        """
        from hiveweave.services.wait_contract import wait_contract_service

        now_ms = int(time.time() * 1000)
        try:
            active = await wait_contract_service.list_all_active(project_id)
        except Exception as e:
            log.warning("wait_recovery_list_failed", project_id=project_id,
                        error=str(e))
            return {"expired_processed": False, "armed": 0}

        expired = [
            w for w in active
            if w.get("expiresAt") is not None and int(w["expiresAt"]) <= now_ms
        ]
        expired_processed = False
        if expired:
            # 与 tick 路径一致：clear_expired + [WAIT_TIMEOUT] + 唤醒
            await self._process_wait_contracts(project_id)
            expired_processed = True
            log.info("wait_recovery_expired_processed",
                     project_id=project_id, count=len(expired))

        state = _states.get(project_id) or await self._load_state(project_id)
        _states[project_id] = state
        armed_ids: set[str] = state.setdefault("armed_wait_ids", set())
        timers: list = state.setdefault("wait_recovery_timers", [])
        armed = 0
        loop = asyncio.get_running_loop()
        for w in active:
            wid = w.get("id")
            exp = w.get("expiresAt")
            if not wid or exp is None or int(exp) <= now_ms or wid in armed_ids:
                continue
            delay = max(0.0, (int(exp) - now_ms) / 1000.0)

            def _make_cb(pid: str, wait_id: str):
                def _cb() -> None:
                    st = _states.get(pid)
                    if st is not None:
                        st.get("armed_wait_ids", set()).discard(wait_id)
                    asyncio.create_task(self._on_wait_timer(pid, wait_id))
                return _cb

            timers.append(loop.call_later(delay, _make_cb(project_id, wid)))
            armed_ids.add(wid)
            armed += 1
        if armed:
            log.info("wait_recovery_armed", project_id=project_id, armed=armed)
        return {"expired_processed": expired_processed, "armed": armed}

    async def _on_wait_timer(self, project_id: str, wait_id: str) -> None:
        """一次性 wait 定时器回调：到期处理（幂等，已被 tick 清除则无操作）。"""
        try:
            await self._process_wait_contracts(project_id)
        except Exception as e:
            log.error("wait_recovery_timer_failed", project_id=project_id,
                      wait_id=wait_id, error=str(e))

    def cancel_wait_recovery_timers(self, project_id: str) -> int:
        """取消项目所有 wait 恢复定时器（项目 stop 时调用）。返回取消数。"""
        state = _states.get(project_id)
        if not state:
            return 0
        n = 0
        for h in state.get("wait_recovery_timers", []) or []:
            try:
                h.cancel()
                n += 1
            except Exception:
                pass
        state["wait_recovery_timers"] = []
        state["armed_wait_ids"] = set()
        return n

    async def _break_peer_review_deadlocks(
        self,
        project_id: str,
        agents: list[dict],
        inbox,
    ) -> None:
        """Peer-review mutual-wait breaker — now via ledger scan only.

        Kept for call-site compatibility; logic lives in ``_nudge_stale_ledger``.
        """
        await self._nudge_peer_review_deadlocks(project_id, agents=agents)

    async def _nudge_stale_ledger(self, project_id: str) -> None:
        """Ledger-only re-nudge: stale review / merge / peer_review cross-wait.

        Does **not** restore NL stall nudges. Uses tasks.status/tags/updated_at
        and org parent_id only. Age is capped to the current duty session so
        overnight off-duty wall-clock does not stampede on activate.
        """
        from hiveweave.services.inbox import InboxService
        from hiveweave.services.org import OrgService
        from hiveweave.services.task import TaskService

        # Hard gates: off-duty / paused → no nudges
        proj = await meta_db.query_one(
            "SELECT is_started FROM projects WHERE id = ?", [project_id]
        )
        if not proj or not dict(proj).get("is_started"):
            return
        from hiveweave.services.system_state import system_state

        if system_state.paused():
            return

        now_ms = int(time.time() * 1000)
        state = _states.get(project_id)
        if not state:
            return
        cooldowns: dict[str, int] = state.setdefault("ledger_nudge_cooldowns", {})
        session_start = int(
            state.get("duty_session_started_at_ms") or now_ms
        )

        ts = TaskService()
        tasks = await ts.list_tasks(project_id)
        inbox = InboxService()
        agents = await OrgService().list_agents(project_id)
        by_id = {a.get("id"): a for a in agents if a.get("id")}

        def _cooled(key: str, window: int) -> bool:
            last = cooldowns.get(key) or 0
            if now_ms - last < window:
                return False
            cooldowns[key] = now_ms
            return True

        def _effective_age_ms(t: dict) -> int:
            """Wall age capped by duty session (overnight does not count)."""
            updated = int(t.get("updated_at") or t.get("created_at") or 0)
            if not updated:
                return 0
            wall = max(0, now_ms - updated)
            since_duty = max(0, now_ms - session_start)
            return min(wall, since_duty)

        def _creator_unavailable(creator_id: str) -> bool:
            from hiveweave.agents.supervisor import agent_manager

            inst = agent_manager.get_agent(creator_id)
            if inst is None:
                return False
            if getattr(inst, "_resume_suppressed", False):
                return True
            disp = getattr(inst, "disposition", None) or ""
            if disp == "blocked":
                return True
            silence = (state.get("silence_trackers") or {}).get(creator_id) or {}
            if silence.get("flagged"):
                return True
            return False

        # ── submitted|reviewing → [LEDGER REVIEW] ──
        for t in tasks:
            status = t.get("status")
            if status not in ("submitted", "reviewing"):
                continue
            if _effective_age_ms(t) < LEDGER_REVIEW_STALE_MS:
                continue
            creator = t.get("creator_id")
            tid = str(t.get("id") or "")
            if not creator or not tid:
                continue
            if not _cooled(f"review:{tid}", LEDGER_NUDGE_COOLDOWN_MS):
                continue
            title = (t.get("title") or "(untitled)").split("\n")[0][:50]
            body = (
                f"[LEDGER REVIEW] Task '{title}' ({tid[:8]}) has been "
                f"{status} for >{LEDGER_REVIEW_STALE_MS // 60000}min "
                f"(on-duty). Use review_task(taskId='{tid}', "
                f"decision='approve'/'rework')."
            )
            try:
                await inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=creator,
                    message=body,
                    message_type="task",
                    priority="urgent",
                    task_id=tid,
                    wake=True,
                )
                await self._watchdog_trigger(creator)
                log.info(
                    "ledger_stale_review_nudge",
                    project_id=project_id,
                    task_id=tid,
                    creator=creator,
                    status=status,
                )
            except Exception as e:
                log.warning(
                    "ledger_stale_review_nudge_failed",
                    task_id=tid,
                    error=str(e),
                )

        # ── approved (non-VERIFY) → [MERGE PENDING] and/or [MERGE PROXY] ──
        for t in tasks:
            if t.get("status") != "approved":
                continue
            if TaskService._is_verify_task(t):
                continue
            age = _effective_age_ms(t)
            creator = t.get("creator_id")
            tid = str(t.get("id") or "")
            if not creator or not tid:
                continue

            unavailable = _creator_unavailable(str(creator))
            need_proxy = age >= MERGE_PROXY_STALE_MS or unavailable
            need_pending = age >= MERGE_PENDING_STALE_MS

            if need_pending and not unavailable:
                if _cooled(f"merge:{tid}", LEDGER_NUDGE_COOLDOWN_MS):
                    title = (t.get("title") or "(untitled)").split("\n")[0][:50]
                    short = ""
                    asg = t.get("assignee_id")
                    if asg and asg in by_id:
                        short = (by_id[asg] or {}).get("short_id") or ""
                    branch = short or "hw/<short_id>/..."
                    body = (
                        f"[MERGE PENDING] Task '{title}' ({tid[:8]}) approved for "
                        f">{MERGE_PENDING_STALE_MS // 60000}min without merge "
                        f"(on-duty). Call git_worktree_merge(branchName='{branch}') now."
                    )
                    try:
                        await inbox.send_message(
                            from_agent_id="system",
                            to_agent_id=creator,
                            message=body,
                            message_type="task",
                            priority="urgent",
                            task_id=tid,
                            wake=True,
                        )
                        await self._watchdog_trigger(creator)
                        log.info(
                            "ledger_stale_merge_nudge",
                            project_id=project_id,
                            task_id=tid,
                            creator=creator,
                        )
                    except Exception as e:
                        log.warning(
                            "ledger_stale_merge_nudge_failed",
                            task_id=tid,
                            error=str(e),
                        )

            if need_proxy and _cooled(f"proxy:{tid}", MERGE_PROXY_COOLDOWN_MS):
                from hiveweave.services.merge_proxy import escalate_merge_proxy

                reason = "creator_unavailable" if unavailable else "overdue"
                try:
                    await escalate_merge_proxy(
                        project_id,
                        t,
                        reason=reason,
                        agents_by_id=by_id,
                        trigger=True,
                    )
                except Exception as e:
                    log.warning(
                        "merge_proxy_escalate_failed",
                        task_id=tid,
                        error=str(e),
                    )

        await self._nudge_peer_review_deadlocks(
            project_id, agents=agents, tasks=tasks, now_ms=now_ms
        )

    async def _nudge_peer_review_deadlocks(
        self,
        project_id: str,
        *,
        agents: list[dict] | None = None,
        tasks: list[dict] | None = None,
        now_ms: int | None = None,
    ) -> None:
        """Cross peer_review submitted pairs → wake both + common superior."""
        from hiveweave.services.inbox import InboxService
        from hiveweave.services.org import OrgService
        from hiveweave.services.task import TaskService

        now = now_ms if now_ms is not None else int(time.time() * 1000)
        state = _states.get(project_id)
        if not state:
            return
        cooldowns: dict[str, int] = state.setdefault("ledger_nudge_cooldowns", {})
        session_start = int(state.get("duty_session_started_at_ms") or now)

        if agents is None:
            agents = await OrgService().list_agents(project_id)
        if tasks is None:
            tasks = await TaskService().list_tasks(project_id)

        by_id = {a.get("id"): a for a in agents if a.get("id")}
        inbox = InboxService()

        def _tags(t: dict) -> list[str]:
            raw = t.get("tags") or []
            if isinstance(raw, list):
                return [str(x).lower() for x in raw]
            if isinstance(raw, str):
                return [raw.lower()]
            return []

        def _effective_age_ms(t: dict) -> int:
            updated = int(t.get("updated_at") or t.get("created_at") or 0)
            if not updated:
                return 0
            wall = max(0, now - updated)
            since_duty = max(0, now - session_start)
            return min(wall, since_duty)

        # Edges: creator must review assignee's submitted peer_review task
        edges: dict[tuple[str, str], dict] = {}
        for t in tasks:
            if t.get("status") not in ("submitted", "reviewing"):
                continue
            if "peer_review" not in _tags(t):
                continue
            if _effective_age_ms(t) < PEER_REVIEW_DEADLOCK_MS:
                continue
            c, a = t.get("creator_id"), t.get("assignee_id")
            if not c or not a or c == a:
                continue
            edges[(c, a)] = t

        seen_pairs: set[frozenset[str]] = set()
        for (a, b), t_ab in list(edges.items()):
            t_ba = edges.get((b, a))
            if not t_ba:
                continue
            pair = frozenset({a, b})
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            key = f"peer:{':'.join(sorted(pair))}"
            last = cooldowns.get(key) or 0
            if now - last < PEER_REVIEW_DEADLOCK_COOLDOWN_MS:
                continue
            cooldowns[key] = now

            parent_a = (by_id.get(a) or {}).get("parent_id")
            parent_b = (by_id.get(b) or {}).get("parent_id")
            superior = parent_a if parent_a == parent_b else (parent_a or parent_b)

            tid_ab = str(t_ab.get("id") or "")[:8]
            tid_ba = str(t_ba.get("id") or "")[:8]
            body_pair = (
                f"[PEER_REVIEW_DEADLOCK] Cross peer_review stuck: "
                f"{a[:8]}↔{b[:8]} (tasks {tid_ab}/{tid_ba}). "
                f"Break the wait: review_task one side or escalate."
            )
            for member in (a, b):
                try:
                    await inbox.send_message(
                        from_agent_id="system",
                        to_agent_id=member,
                        message=body_pair,
                        message_type="escalation",
                        priority="urgent",
                        wake=True,
                    )
                    await self._watchdog_trigger(member)
                except Exception as e:
                    log.warning(
                        "peer_review_deadlock_wake_failed",
                        member=member,
                        error=str(e),
                    )
            if superior and superior not in (a, b):
                try:
                    await inbox.send_message(
                        from_agent_id="system",
                        to_agent_id=superior,
                        message=(
                            f"[PEER_REVIEW_DEADLOCK] Subordinates {a[:8]}↔{b[:8]} "
                            f"cross-waiting on peer_review tasks "
                            f"{tid_ab}/{tid_ba}. Please break the deadlock."
                        ),
                        message_type="escalation",
                        priority="urgent",
                        wake=True,
                    )
                    await self._watchdog_trigger(superior)
                except Exception as e:
                    log.warning(
                        "peer_review_deadlock_superior_failed",
                        superior=superior,
                        error=str(e),
                    )
            log.info(
                "peer_review_deadlock_broken",
                project_id=project_id,
                pair=sorted(pair),
                superior=superior,
            )

    async def _sweep_orphan_streaming(self, project_id: str) -> None:
        """Clear is_streaming=1 rows whose agent is not actively PROCESSING.

        Users must never need a manual/AI 'zombie clear'. Boot clears crash
        leftovers; this runtime sweep catches mid-session orphans within ~30s.
        """
        try:
            from hiveweave.agents.supervisor import agent_manager
            from hiveweave.agents.agent import SAFETY_TIMEOUT_MS
            from hiveweave.services.chat_message import ChatMessageService

            protect = {
                aid
                for aid, pid in agent_manager.list_processing()
                if pid == project_id
            }
            await ChatMessageService().clear_orphan_streaming(
                project_id,
                protect_agent_ids=protect,
                soft_age_ms=SAFETY_TIMEOUT_MS,
                hard_age_ms=SAFETY_TIMEOUT_MS + 60_000,
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
                    safe_env = {
                        k: v for k, v in os.environ.items()
                        if k.upper() in _SAFE_ENV_KEYS
                    }
                    from hiveweave.util.win_subprocess import windows_no_window_kwargs

                    # Security: 用 create_subprocess_exec + shlex.split 取代
                    # create_subprocess_shell，避免 shell=True 的 prompt injection
                    # 风险（agent 可控的 script_command 不再经 shell 解释）。
                    # Windows 下 shlex.split(posix=True) 对含空格/反斜杠的路径
                    # 可能解析不当 — 解析失败时 ValueError 被捕获并跳过执行。
                    try:
                        cmd_parts = shlex.split(script, posix=True)
                    except ValueError as ve:
                        log.warning(
                            "alarm_script_parse_failed",
                            alarm_id=alarm["id"],
                            error=str(ve),
                        )
                        cmd_parts = []

                    if not cmd_parts:
                        log.warning(
                            "alarm_script_empty_or_unparseable",
                            alarm_id=alarm["id"],
                        )
                    else:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd_parts,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            env=safe_env,
                            **windows_no_window_kwargs(),
                        )
                        stdout, stderr = await asyncio.wait_for(
                            proc.communicate(), timeout=120
                        )
                        if proc.returncode != 0:
                            log.warning(
                                "alarm_script_failed",
                                alarm_id=alarm["id"],
                                rc=proc.returncode,
                                stderr=stderr.decode()[:200],
                            )
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
        """Stall / unreplied inbox watchdog — disabled.

        Reply obligations are enforced at turn exit via structured
        ``expect_report`` / ``ask`` + recipient ID check. Task advance uses
        the ``agent.turn.after`` hook. Periodic stall nudges were removed.
        Peer-review deadlock break and silent-agent watch remain elsewhere.
        """
        return

    async def _nudge_awaiting_replies(
        self,
        project_id: str,
        now_ms: int,
        name_map: dict[str, str],
    ) -> None:
        """Awaiting-reply sender nudge — disabled (see ``_check_stalled``)."""
        return

    async def _check_silent_agents(self, project_id: str) -> None:
        """Watchdog: agent 沉默观测 — 检测"无任何产出"的失联 agent（每 2 分钟）。

        与 _check_stalled（消息轴）/ task stall（任务轴）互补：覆盖"接活后
        当场死亡、名下无任何待回复消息"的盲区（潮汐事故）。

        判定：active agent 最后产出（chat_messages 的 assistant 消息或
        work_logs，取较新者；均无则以 agents.created_at 为基线）距今超
        SILENCE_THRESHOLD_MS。

        动作：① trigger_subordinate 唤醒（STALL_COOLDOWN_MS 冷却，不每次扫都触发）
              ② 经 event_bus 广播 agent_health error 红框（同构 agent.py
                _broadcast_agent_health 的事件结构，前端组织树节点变红）
              ③ 失联持续超 SILENCE_NOTIFY_MS 通知上级（30 min 冷却）
        恢复：重新产出后广播 agent_health ok 解除红框。

        豁免（复用既有判断）：processing 中 / waiting_*、blocked disposition
        且有未过期 wait contract / 项目 is_started=0 / 系统 paused。
        """
        state = _states.get(project_id)
        if not state:
            return

        # 豁免 1: 项目未"上班" — 与 _check_stalled Case 4 同一判断
        proj = await meta_db.query_one(
            "SELECT is_started FROM projects WHERE id = ?", [project_id]
        )
        if not proj or not dict(proj).get("is_started"):
            return

        # 豁免 2: 系统全局暂停
        from hiveweave.services.system_state import system_state
        if system_state.paused():
            return

        now_ms = int(time.time() * 1000)

        agents = await _query(project_id,
            "SELECT id, name, parent_id, created_at, last_active_at FROM agents "
            "WHERE status = 'active'", [])
        if not agents:
            return

        # 豁免 3: processing 中的 agent（复用 supervisor 既有判断）
        from hiveweave.agents.supervisor import agent_manager
        processing_ids = {
            aid for aid, pid in agent_manager.list_processing()
            if pid == project_id
        }

        # 豁免 4: waiting_*/blocked disposition + 未过期 wait contract。
        # wait contract 是 commit_turn 落盘的合法等待凭证（P0 Hard Gates）；
        # 无活实例（进程重启/agent 已死）时以落盘 contract 为准——死亡 agent
        # 死于 turn 中途、没有 contract，不会被误豁免。
        from hiveweave.services.wait_contract import wait_contract_service
        try:
            all_waits = await wait_contract_service.list_all_active(project_id)
        except Exception:
            all_waits = []
        live_waits: dict[str, list[dict]] = {}
        for w in all_waits:
            exp = w.get("expiresAt")
            if exp is None or int(exp) > now_ms:
                live_waits.setdefault(w.get("agentId") or "", []).append(w)

        # 最后活跃：优先 last_active_at，再 assistant/work_logs，再 created_at
        last_output: dict[str, int] = {}
        for a in agents:
            la = a["last_active_at"]
            if la:
                last_output[a["id"]] = int(la)
        rows = await _query(project_id,
            "SELECT agent_id, MAX(created_at) AS last_ts FROM chat_messages "
            "WHERE role = 'assistant' GROUP BY agent_id", [])
        for r in rows:
            if r["last_ts"]:
                aid = r["agent_id"]
                last_output[aid] = max(last_output.get(aid, 0), int(r["last_ts"]))
        rows = await _query(project_id,
            "SELECT agent_id, MAX(created_at) AS last_ts FROM work_logs "
            "GROUP BY agent_id", [])
        for r in rows:
            if r["last_ts"]:
                aid = r["agent_id"]
                last_output[aid] = max(last_output.get(aid, 0), int(r["last_ts"]))

        # {agent_id: {"flagged": bool, "wake_ts": int, "notify_ts": int}}
        trackers: dict = state.setdefault("silence_trackers", {})

        from hiveweave.realtime.event_bus import status_event_bus

        for a in agents:
            aid = a["id"]
            if aid in processing_ids:
                continue
            waits = live_waits.get(aid) or []
            if waits:
                inst = agent_manager.get_agent(aid)
                disp = getattr(inst, "disposition", None) if inst else None
                if disp is None or disp in _WAITING_DISPOSITIONS:
                    continue
            else:
                # No active wait contracts — still skip complete (idle-by-design)
                inst = agent_manager.get_agent(aid)
                disp = getattr(inst, "disposition", None) if inst else None
                if disp == "complete":
                    continue

            baseline = last_output.get(aid) or int(a["created_at"] or now_ms)
            silent_ms = now_ms - baseline
            tracker = trackers.get(aid) or {
                "flagged": False, "wake_ts": 0, "notify_ts": 0,
            }

            if silent_ms < SILENCE_THRESHOLD_MS:
                # 有产出 → 若此前举过红框则广播 ok 解除
                if tracker["flagged"]:
                    tracker["flagged"] = False
                    try:
                        await status_event_bus.publish_stream_event(aid, {
                            "type": "agent_health",
                            "agentId": aid,
                            "projectId": project_id,
                            "health": "ok",
                            "message": "",
                            "at": now_ms,
                        })
                    except Exception as e:
                        log.warning("silence_health_broadcast_failed",
                                    agent_id=aid, error=str(e))
                    log.info("silence_recovered",
                             project_id=project_id, agent_id=aid)
                trackers[aid] = tracker
                continue

            minutes = int(silent_ms / 60000)

            # ① 唤醒 + ② 红框（冷却内不重复，对齐 stall 看门狗的冷却语义）
            if now_ms - tracker["wake_ts"] >= STALL_COOLDOWN_MS:
                tracker["wake_ts"] = now_ms
                tracker["flagged"] = True
                log.warning("silence_watchdog_trigger",
                            project_id=project_id, agent_id=aid,
                            name=a["name"], silent_minutes=minutes)
                try:
                    await status_event_bus.publish_stream_event(aid, {
                        "type": "agent_health",
                        "agentId": aid,
                        "projectId": project_id,
                        "health": "error",
                        "message": (
                            f"[SILENCE WATCHDOG] 已 {minutes} 分钟无产出，疑似失联"
                        )[:200],
                        "at": now_ms,
                    })
                except Exception as e:
                    log.warning("silence_health_broadcast_failed",
                                agent_id=aid, error=str(e))
                try:
                    await self._watchdog_trigger(aid)
                except Exception as e:
                    log.error("silence_wake_failed",
                              agent_id=aid, error=str(e))

            # ③ 失联持续超 30 min → 通知上级（同一 agent 30 min 冷却）
            if (silent_ms >= SILENCE_NOTIFY_MS
                    and now_ms - tracker["notify_ts"] >= SILENCE_NOTIFY_COOLDOWN_MS):
                tracker["notify_ts"] = now_ms
                parent_id = a["parent_id"]
                if parent_id:
                    log.warning("silence_escalated",
                                project_id=project_id, agent_id=aid,
                                parent_id=parent_id, silent_minutes=minutes)
                    try:
                        from hiveweave.services.inbox import InboxService
                        await InboxService().send_message(
                            "system", parent_id,
                            f"[SILENCE WATCHDOG] 你的下属 {a['name']} 已 "
                            f"{minutes} 分钟无任何产出（chat/work_log），"
                            "唤醒尝试未恢复，请介入检查。",
                            message_type="system", priority="urgent")
                        await self._watchdog_trigger(parent_id)
                    except Exception as e:
                        log.error("silence_notify_failed",
                                  agent_id=aid, parent_id=parent_id,
                                  error=str(e))
                else:
                    # 没有上级（CEO）— 仅记录，对齐 stall 看门狗的不可升级路径
                    log.warning("silence_unescalatable",
                                agent_id=aid, name=a["name"])

            trackers[aid] = tracker

    @staticmethod
    def _format(game_seconds: int) -> str:
        day = game_seconds // GAME_SECONDS_PER_DAY
        rem = game_seconds % GAME_SECONDS_PER_DAY
        return f"Day {day} {rem // 3600:02d}:{(rem % 3600) // 60:02d}"
