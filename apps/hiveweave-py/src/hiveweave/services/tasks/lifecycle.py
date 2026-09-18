"""Start / block / unblock / reconcile / park helpers."""
from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query
from .verify import VerifyMixin

log = structlog.get_logger(__name__)


SELF_DEPENDENCY_BLOCK_ERROR = (
    "A task cannot depend on itself — dependsOn / dependsOnTaskIds may "
    "only be other task ids (self-dependency never unblocks). Waiting on "
    "a person is commit_turn(waiting_on=[{kind:agent, ref:...}]); keep "
    "the task running."
)


def _same_task_id(left: str, right: str) -> bool:
    """True if two task ids name the same row (dash/case insensitive)."""
    a = (left or "").replace("-", "").strip().casefold()
    b = (right or "").replace("-", "").strip().casefold()
    return bool(a) and a == b


def blocked_task_has_wake_path(task: dict, now_ms: int | None = None) -> bool:
    """A blocked task has a live auto-unblock path iff its wait metadata says so.

    2026-08-11 slack-clone_01 死锁复盘：手工 block（wait_kind 非 timer 且
    depends_on 为空）没有任何自动解封路径，reconcile 永远不会碰它 —— 这类
    parked 任务若被当成「占用 MAIN 运行时」会让整个 VERIFY 队列永久冻结。
    只认结构化字段（HARD RULE：禁止用文案猜意图）：
    - ``depends_on`` 非空 → reconcile 在全部依赖 approved/closed 后自动解封；
    - ``wait_kind == "timer"`` 且 ``wake_at`` 非空 → reconcile 到期自动解封。
      **不判过期**：game_time 同一 STALL_CHECK 内泵（nudge_pending_verify）
      先于 reconcile 运行，若把已过期 timer 判为 parked，泵会放行第二个
      VERIFY，reconcile 紧接着把第一个解封成 running → 双 VERIFY 上 MAIN。
    """
    deps = task.get("depends_on") or []
    if isinstance(deps, str):
        try:
            deps = json.loads(deps) if deps else []
        except (json.JSONDecodeError, TypeError):
            deps = []
    if isinstance(deps, list) and deps:
        return True
    kind = (task.get("wait_kind") or "").lower()
    wake_at = task.get("wake_at")
    if kind == "timer" and wake_at is not None:
        return True
    return False


#: 「等一个 agent 做裁决」的等待类 blocked 的升级判据宽限（PLATFORM-ISSUES §11.6）。
#:
#: 现场取证（2026-09-17，TEST_DSH_61 `309e2489`）：该任务 `wait_kind='timer'`、
#: `wake_at` 已过期 5.4h，**却仍是 blocked** —— 即「声明了出口但出口没生效」。
#: 成因是工具层当时不给「等人裁决」出口（见 `tools/tasks/lifecycle.py` 的
#: `waitKind` 校验），agent 只能把裁决等待**伪装成 timer**。
#:
#: ⇒ 本判据因此认两种形态（**只认结构化字段，禁止文案**）：
#:   ① **无出口**：deps 空 且 非 timer —— reconcile 永远不会碰它；
#:   ② **出口失效**：`wait_kind='timer'` 且 `wake_at` 已过期超过宽限 ——
#:      本该被 reconcile 解封却没解封（现场正是此形态）。
#:
#: ⚠ 宽限必须 > 0：`reconcile_blocked_tasks` 每 120s 跑一次，刚过期的 timer
#: 尚未被扫到属正常；判「出口失效」要留出至少一个 reconcile 周期。
ARBITRATION_GRACE_MS = 30 * 60 * 1000  # 30 minutes

#: 等一个 agent 的 wait_kind 值（无机器可判的自动解封路径，须由升级兜底）。
_ARBITRATION_WAIT_KINDS = frozenset({"user", "external"})


def blocked_task_needs_arbitration_escalation(
    task: dict, now_ms: int | None = None
) -> bool:
    """「等一个 agent 做裁决」且**已陈旧**的 blocked —— 需要升级兜底。

    2026-09-17 现场取证（PLATFORM-ISSUES §11.6）：`reconcile_blocked_tasks` 只
    覆盖两类出口（deps 满足 / timer 到期）。**第三类成因「等人裁决」没有出口**：
    `[BLOCKED STALE]` / `[BLOCKED ESCALATION]` inbox 已在 `game_time.py` 明写
    禁用，且 `obligations` 表里没有对应义务 ⇒ 没有任何机制把它推回裁决者面前。

    判据（**纯状态判据**，只读 DB 行 / 结构化字段）：

        status == 'blocked' 且 is_archived == 0
        且 claimed_at 非空            （曾被告认领 —— 排除"出生即 blocked"的依赖任务）
        且 assignee_id 非空           （有人担责）
        且 creator_id 非空            （有升级对象）
        且 deps 为空                  （有依赖出口 ⇒ reconcile 在管，不升级）
        且 [ 等人(②) 或 出口失效(③) 或 无出口(④) ]
        且 陈旧 (now - updated_at) > ARBITRATION_GRACE_MS

    ⚠ **不判** `blocked_reason` 文案（本仓 HARD RULE：禁止用文案猜意图）。
    ⚠ **不判** `wait_kind` 的字符串前缀 —— 只认**集合成员资格**。
    """
    if (task.get("status") or "") != "blocked":
        return False
    if task.get("is_archived"):
        return False
    if task.get("claimed_at") is None:
        return False
    if not task.get("assignee_id") or not task.get("creator_id"):
        return False

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    updated_at = task.get("updated_at") or 0
    if now - int(updated_at) <= ARBITRATION_GRACE_MS:
        return False  # 刚 block 的，留给 reconcile / 正常流程

    kind = (task.get("wait_kind") or "").lower()
    wake_at = task.get("wake_at")

    # ① 有依赖出口 ⇒ 不升级（审计 P4）：deps 非空时 reconcile 在管，
    # 「出口未到」≠「出口失效」—— 不该被升级噪音打扰。同时防
    # `waitKind='user'` + deps 并存时被误判（工具层现已拒绝该组合，
    # 此处仍自防御 DB 直写 / 历史行）。
    deps = task.get("depends_on") or []
    if isinstance(deps, str):
        try:
            deps = json.loads(deps) if deps else []
        except (json.JSONDecodeError, TypeError):
            deps = []
    if isinstance(deps, list) and deps:
        return False

    if kind in _ARBITRATION_WAIT_KINDS:
        return True  # ② 显式声明「等人」—— 无自动解封路径

    # ③ 出口失效：声明了 timer 出口，却已过期超过宽限仍 blocked
    if kind == "timer" and wake_at is not None:
        try:
            if int(wake_at) + ARBITRATION_GRACE_MS < now:
                return True
        except (TypeError, ValueError):
            return False

    # ④ 无出口：deps 空且非 timer（`blocked_task_has_wake_path` 为假）
    return not blocked_task_has_wake_path(task, now)



def _deps_merged_head_note(merge_commit: str | None) -> str:
    """新 HEAD 合流复核提示（duty 增强第二部分）。

    merge_commit 非空 → 「依赖已合流进 MAIN（HEAD <sha8>），请基于新 HEAD
    复核/重验后继续」；空 → 空串（调用方回落现状文案，回归不破坏）。
    """
    if not merge_commit:
        return ""
    return (
        f"依赖已合流进 MAIN（HEAD {str(merge_commit)[:8]}），"
        "请基于新 HEAD 复核/重验后继续。"
    )


def _dependency_met_message(
    completed_task_id: str, title: str, merge_commit: str | None
) -> str:
    """[DEPENDENCY MET] assignee 文案：带 merge HEAD → 合流复核语义。

    merge_commit 为 None 时保持 41 轮前的现状文案（review/close 调用点
    零改动路径的回归保障）。
    """
    base = (
        f"[DEPENDENCY MET] Blocker {completed_task_id[:8]}… is done. "
        f"Your blocked task '{title}' is unblocked (running). "
    )
    return base + (
        _deps_merged_head_note(merge_commit) or "Continue work or submit_task."
    )


async def _notify_creator_deps_merged(
    project_id: str, task: dict, merge_commit: str | None
) -> None:
    """Creator FYI（wake=False）：被解封任务的创建者知会，非职责信号。

    解封 = 依赖已合流自动继续，创建者无需行动（与 task.blocked relay 的
    wake=True 职责信号相对）。幂等：显式 idempotency_key 含 commit 前缀，
    同一 commit 解封只提醒一次（inbox 显式键「只通知一次」契约，不被
    read-rearm 击穿）。**不带 task_id**：inbox TEST13 P2-2 会把绑定 open
    任务的 wake=False 消息强制改成 wake=True，破坏 FYI 语义。creator 与
    assignee 同人时跳过（assignee 已收 [DEPENDENCY MET]）。
    """
    creator = str(task.get("creator_id") or "")
    assignee = str(task.get("assignee_id") or "")
    tid = str(task.get("id") or "")
    if not creator or not tid or creator == assignee:
        return
    try:
        from hiveweave.services.inbox import InboxService

        title = (task.get("title") or "")[:80]
        if merge_commit:
            fact = "依赖已全部合流"
            head = f"（HEAD {str(merge_commit)[:8]}）"
        else:
            # review/close 路径 deps 只是 approved/closed，未必合流进
            # MAIN —— 措辞不冒认「合流」（审计 P2-C）
            fact = "依赖已完成"
            head = ""
        await InboxService().send_message(
            "system",
            creator,
            (
                f"[DEPS MERGED FYI] 你创建的任务 '{title}' 的{fact}"
                f"{head}并自动解封——无需你行动。"
            ),
            message_type="system",
            priority="normal",
            wake=False,
            idempotency_key=(
                f"deps-merged-fyi:{tid}:{str(merge_commit or '')[:12]}"
            ),
        )
    except Exception as e:
        log.warning(
            "creator_deps_merged_fyi_failed",
            task_id=tid[:12],
            error=str(e),
        )


async def _recent_merged_commit_for_deps(
    project_id: str, deps: list
) -> str | None:
    """reconcile 兜底路径取 HEAD：deps 的 ``task.merged`` 事件（新→旧）。

    task.merged 由 merge 后钩子（_stamp_merge_fact_on_parent_tasks）落在
    被合流任务上，payload.merge_commit 即 merge() 返回的 MAIN HEAD。
    **不能只看最新一条**（审计 P1-B）：payload 写入是 ``str(merge_commit
    or "")``，最新事件 commit 可为空串——按 created_at 倒序取**第一条
    非空** commit（较旧事件带真 commit 也要用）。fail-open：查不到 /
    解析失败 → None（文案保持现状）。
    """
    ids = [str(d) for d in deps if d]
    if not ids:
        return None
    placeholders = ",".join("?" * len(ids))
    try:
        rows = await _query(
            project_id,
            f"SELECT payload FROM task_events "
            f"WHERE event_type = 'task.merged' "
            f"AND task_id IN ({placeholders}) "
            f"ORDER BY created_at DESC LIMIT 50",
            ids,
        )
    except Exception as e:
        log.debug("recent_merged_commit_lookup_failed", error=str(e))
        return None
    for r in rows:
        try:
            raw = r["payload"]
            payload = (
                json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            )
        except (json.JSONDecodeError, TypeError):
            continue
        commit = str((payload or {}).get("merge_commit") or "").strip()
        if commit:
            return commit
    return None


class LifecycleMixin:
    """start/block/unblock/reconcile + implementer lock / park."""

    if TYPE_CHECKING:
        require_task_id: Any
        emit_task_event: Any
        get_task: Any
        find_task_by_slice_id: Any
        unmet_depends_on: Any
        _depends_on_list: Any
        _transition: Any
        _persist_contract_json: Any
        _is_verify_task: Any
        _COLUMNS: Any
        _row: Any

    async def start_task(self, project_id: str, task_id: str) -> None:
        """Start a task (claimed → running).

        If the task is currently ``blocked``, delegates to ``unblock_task`` so
        wait metadata is cleared (TEST11 #5-L1). Callers that used to rely on
        ``blocked → running`` being a legal ``_transition`` must not skip that.

        Slice P0: if ``contract_json`` present, enforce ready gate (upstream
        verified) before transitioning; then set slice_status=in_progress.
        """
        task_id = await self.require_task_id(project_id, task_id)
        rows = await _query(
            project_id, "SELECT status, assignee_id FROM tasks WHERE id = ?",
            [task_id],
        )
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        if rows[0]["status"] == "blocked":
            # blocked must go through unblock_task to clear wait metadata
            await self.unblock_task(project_id, task_id)
            agent_id = rows[0]["assignee_id"]
            await self.emit_task_event(
                project_id,
                task_id,
                "running",
                agent_id=agent_id,
                summary=f"[running] task {task_id[:8]} unblocked via start_task",
            )
            return

        # READY GATE (slice-driven)
        task = await self.get_task(project_id, task_id)
        if task and not self._is_verify_task(task):
            unmet = await self.unmet_depends_on(
                project_id, self._depends_on_list(task.get("depends_on"))
            )
            if unmet:
                raise ValueError(
                    "Cannot start while depends_on are unmet: "
                    + ", ".join(u[:8] for u in unmet[:5])
                    + ". Wait for blockers to be approved/closed."
                )
        if task and task.get("contract_json"):
            from hiveweave.services.task_contract import (
                check_ready_gate,
                ensure_slice_status,
                parse_contract,
            )

            async def _lookup_tid(tid: str):
                return await self.get_task(project_id, tid)

            async def _lookup_sid(sid: str):
                return await self.find_task_by_slice_id(project_id, sid)

            ready_err = await check_ready_gate(
                project_id,
                task,
                lookup_by_slice_id=_lookup_sid,
                lookup_by_task_id=_lookup_tid,
            )
            if ready_err:
                raise ValueError(ready_err)
            contract = parse_contract(task.get("contract_json"))
            if contract:
                contract = ensure_slice_status(contract, "ready")
                # Will flip to in_progress after transition succeeds

        await self._transition(project_id, task_id, "running",
                               actor_id=rows[0]["assignee_id"])
        agent_id = rows[0]["assignee_id"]

        if task and task.get("contract_json"):
            from hiveweave.services.task_contract import (
                ensure_slice_status,
                parse_contract,
            )

            contract = parse_contract(task.get("contract_json"))
            if contract:
                contract = ensure_slice_status(contract, "in_progress")
                await self._persist_contract_json(project_id, task_id, contract)

        # TEST21 M2: lock implementer on first transition to running
        if agent_id:
            await self.lock_implementer_if_needed(
                project_id, task_id, str(agent_id)
            )

        await self.emit_task_event(
            project_id,
            task_id,
            "running",
            agent_id=agent_id,
            summary=f"[running] task {task_id[:8]} started",
        )

    async def block_task(
        self,
        project_id: str,
        task_id: str,
        reason: str,
        *,
        depends_on_task_id: str | None = None,
        depends_on_task_ids: list[str] | None = None,
        wait_kind: str | None = None,
        wake_at: int | None = None,
    ) -> None:
        """Block a task (running → blocked). Sets blocked_reason + wait metadata.

        Auto-unblock paths are structured only:
        - ``depends_on_task_ids``: blocker task ids merged into ``depends_on``
          (``reconcile_blocked_tasks`` unblocks when all are approved/closed);
        - ``wait_kind="timer"`` + ``wake_at`` (epoch ms): deadline for
          ``reconcile_blocked_tasks``.
        ``wait_kind`` is explicit; inferring it from an English prefix in
        ``reason`` is legacy-only and violates the HARD RULE (禁止用文案猜
        意图) — new callers must pass it explicitly. A block with no deps and
        no timer has no auto-unblock path and parks the task forever; callers
        that need that (QA dead zone) must use the dedicated system paths.

        ⚠ **2026-09-17 例外（PLATFORM-ISSUES §11.6）**：``wait_kind`` 为
        ``"user"`` / ``"external"`` 时**无自动解封路径是有意的** —— 它表达
        「等一个 agent 做裁决 / 等外部世界」，由平台的**升级兜底**接手
        （``audit_missing_arbitration_obligations`` → ``scan_overdue`` →
        org parent）。这类 blocked **不需要** deps/timer，也不再是「永久 parked」。
        ``depends_on`` that includes this task's own id is rejected before
        the transition (self-dep never unblocks).
        """
        task_id = await self.require_task_id(project_id, task_id)
        dep_ids: list[str] = []
        for d in (depends_on_task_ids or []):
            dep_ids.append(await self.require_task_id(project_id, d))
        if depends_on_task_id:
            dep_ids.append(await self.require_task_id(project_id, depends_on_task_id))
        if any(_same_task_id(d, task_id) for d in dep_ids):
            raise ValueError(SELF_DEPENDENCY_BLOCK_ERROR)
        await self._transition(project_id, task_id, "blocked")
        now_ms = int(time.time() * 1000)
        reason = (reason or "Blocked by agent").strip()
        if not wait_kind:
            wait_kind = self._infer_wait_kind(reason)  # legacy callers only
        if not wait_kind and dep_ids:
            wait_kind = "dependency"  # structured: deps present → dependency
        if not wait_kind and wake_at is not None:
            wait_kind = "timer"  # structured: deadline present → timer
            # 否则 wake_at 存了但 wait_kind 非 timer → 被当 parked（并发审计 F3）
        try:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = ?, wait_kind = ?, "
                "wake_at = CASE WHEN ? IS NOT NULL THEN ? "
                "WHEN ? = 'timer' THEN wake_at ELSE NULL END, "
                "updated_at = ? WHERE id = ?",
                [reason, wait_kind, wake_at, wake_at, wait_kind, now_ms, task_id],
            )
        except Exception:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = ?, updated_at = ? WHERE id = ?",
                [reason, now_ms, task_id],
            )
        # Structured dependency refs → merge into depends_on (auto-wake path)
        if dep_ids:
            try:
                rows = await _query(
                    project_id,
                    "SELECT depends_on FROM tasks WHERE id = ?",
                    [task_id],
                )
                deps: list = []
                if rows and rows[0]["depends_on"]:
                    raw = rows[0]["depends_on"]
                    try:
                        deps = json.loads(raw) if isinstance(raw, str) else list(raw)
                    except (json.JSONDecodeError, TypeError):
                        deps = []
                if not isinstance(deps, list):
                    deps = []
                added = False
                for d in dep_ids:
                    if d not in deps:
                        deps.append(d)
                        added = True
                if added:
                    await _execute(
                        project_id,
                        "UPDATE tasks SET depends_on = ?, updated_at = ? WHERE id = ?",
                        [json.dumps(deps), now_ms, task_id],
                    )
            except Exception as e:
                log.warning(
                    "block_task_depends_on_merge_failed",
                    task_id=task_id,
                    error=str(e),
                )

    async def update_blocked_metadata(
        self,
        project_id: str,
        task_id: str,
        *,
        reason: str | None = None,
        depends_on_task_ids: list[str] | None = None,
        wait_kind: str | None = None,
        wake_at: int | None = None,
    ) -> None:
        """Refresh a blocked task's wait metadata in place — no transition.

        TEST_DSH_62 P5/L8（2026-09-18）：现场 blocked→blocked 的
        update_task_status 实为元数据刷新需求（args 带了新 blockedReason +
        dependsOnTaskIds），状态机无自环、走转移必然 Illegal transition。
        工具层在发转移请求前读当前态，同态 blocked 且带元数据时改走本
        入口：只更新任务字段，**不写状态转移事件**。窄函数：仅 blocked
        态可调（其余状态 ValueError），且只动显式传入的字段——未传的
        字段保持原值，不照搬 block_task 的默认值覆盖语义。
        """
        task_id = await self.require_task_id(project_id, task_id)
        row = await self.get_task(project_id, task_id)
        if not row:
            raise ValueError(f"Task not found: {task_id}")
        if row.get("status") != "blocked":
            raise ValueError(
                f"update_blocked_metadata only applies to blocked tasks — "
                f"task {task_id[:8]} is '{row.get('status')}'. Use "
                f"block_task for the initial transition."
            )
        dep_ids: list[str] = []
        for d in (depends_on_task_ids or []):
            dep_ids.append(await self.require_task_id(project_id, d))
        if any(_same_task_id(d, task_id) for d in dep_ids):
            raise ValueError(SELF_DEPENDENCY_BLOCK_ERROR)
        now_ms = int(time.time() * 1000)
        # wait 三件套只有显式传入任一项才触碰（纯 reason 刷新不得把
        # wait_kind 意外改写成推断默认值）；kind 推断与 block_task 同源：
        # deps → dependency，wake_at → timer。
        touch_wait = (
            wait_kind is not None or wake_at is not None or bool(dep_ids)
        )
        if touch_wait:
            eff_kind = wait_kind or ("dependency" if dep_ids else "timer")
            await _execute(
                project_id,
                "UPDATE tasks SET "
                "blocked_reason = COALESCE(?, blocked_reason), "
                "wait_kind = ?, "
                "wake_at = CASE WHEN ? IS NOT NULL THEN ? "
                "WHEN ? = 'timer' THEN wake_at ELSE NULL END, "
                "updated_at = ? WHERE id = ?",
                [reason, eff_kind, wake_at, wake_at, eff_kind, now_ms, task_id],
            )
        elif reason is not None:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = ?, updated_at = ? "
                "WHERE id = ?",
                [reason, now_ms, task_id],
            )
        # Structured dependency refs → merge into depends_on (same as
        # block_task: additive only, never strips existing deps)
        if dep_ids:
            try:
                rows = await _query(
                    project_id,
                    "SELECT depends_on FROM tasks WHERE id = ?",
                    [task_id],
                )
                deps: list = []
                if rows and rows[0]["depends_on"]:
                    raw = rows[0]["depends_on"]
                    try:
                        deps = json.loads(raw) if isinstance(raw, str) else list(raw)
                    except (json.JSONDecodeError, TypeError):
                        deps = []
                if not isinstance(deps, list):
                    deps = []
                added = False
                for d in dep_ids:
                    if d not in deps:
                        deps.append(d)
                        added = True
                if added:
                    await _execute(
                        project_id,
                        "UPDATE tasks SET depends_on = ?, updated_at = ? "
                        "WHERE id = ?",
                        [json.dumps(deps), now_ms, task_id],
                    )
            except Exception as e:
                log.warning(
                    "update_blocked_metadata_depends_on_merge_failed",
                    task_id=task_id,
                    error=str(e),
                )

    async def unblock_task(self, project_id: str, task_id: str) -> None:
        """Unblock a task (blocked → running). Clears blocked_reason.

        验收串行化（2026-08-11 并发审计 F1）：手动解封一个 VERIFY 任务必须
        走串行化门 —— parked VERIFY 不占锁，若另一个 VERIFY 正在 MAIN 上跑，
        直接解封会制造双 VERIFY 并发（issue #6）。自动路径（reconcile /
        _wake_dependent_tasks）只触达 has-wake 任务（它们自身占锁），
        except_id 排除自身后门禁必然放行，不受影响。
        """
        task_id = await self.require_task_id(project_id, task_id)
        row = await self.get_task(project_id, task_id)
        if row and not VerifyMixin._is_verify_task(row):
            unmet = await self.unmet_depends_on(
                project_id, self._depends_on_list(row.get("depends_on"))
            )
            if unmet:
                raise ValueError(
                    "Cannot unblock while depends_on are unmet: "
                    + ", ".join(u[:8] for u in unmet[:5])
                    + ". Wait for blockers to be approved/closed "
                    "(reconcile will wake you)."
                )
        if row and VerifyMixin._is_verify_task(row):
            from hiveweave.tools.tasks.verify_spawn import (
                _in_flight_verify_task,
                _verify_serialize_lock,
            )

            async with _verify_serialize_lock(project_id):
                blocker = await _in_flight_verify_task(
                    project_id, except_id=task_id
                )
                if blocker:
                    raise ValueError(
                        f"Task {task_id[:8]} is a VERIFY task and another "
                        f"VERIFY ({str(blocker.get('id'))[:8]}, "
                        f"{blocker.get('status')}) is in flight on the shared "
                        f"MAIN runtime (verification is serialized: one at a "
                        f"time). Unblocking now would run two VERIFYs "
                        f"concurrently. Wait for the in-flight VERIFY to "
                        f"close, or if the blocker is parked (no "
                        f"auto-unblock path), give it dependsOnTaskIds / "
                        f"wakeAt first."
                    )
        await self._transition(project_id, task_id, "running")
        now_ms = int(time.time() * 1000)
        try:
            # 账本一致性（2026-08-19 DSH_11 复盘）：auto_block_deps 创建即
            # blocked 的任务从未 claim —— 解封直落 running 会留下
            # progress=0 / claimed_at=NULL 的 running 任务。补 running 地板
            # （MAX 不降）+ 回填 claimed_at（assign=claim 语义）。
            await _execute(
                project_id,
                "UPDATE tasks SET progress = MAX(progress, 20), "
                "claimed_at = COALESCE(claimed_at, ?), "
                "blocked_reason = NULL, wait_kind = NULL, "
                "wake_at = NULL, updated_at = ? WHERE id = ?",
                [now_ms, now_ms, task_id],
            )
        except Exception:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = NULL, updated_at = ? WHERE id = ?",
                [now_ms, task_id],
            )
        # 2026-09-17（PLATFORM-ISSUES §11.6，审计 P1）：离开 blocked ⇒ 结清
        # arbitration 义务。否则陈旧 pending 行（deadline 已过、escalation
        # count 续用）会让任务**下一次** block 无宽限立即升级。fail-open
        # （与 close.py 同模式）：结清失败不得阻断解封。
        # 覆盖两条路径：手动 unblock + reconcile_blocked_tasks（同走本入口）。
        try:
            from hiveweave.services.obligation import ObligationLedger

            await ObligationLedger().settle_arbitration_on_unblock(
                project_id, task_id
            )
        except Exception as e:
            log.warning(
                "obligation.arbitration_settle_failed",
                task_id=task_id,
                error=str(e),
            )

    @staticmethod
    def _infer_wait_kind(reason: str) -> str | None:
        r = (reason or "").strip().lower()
        for kind in ("dependency", "timer", "user", "external"):
            if r.startswith(f"{kind}:"):
                return kind
        return None

    async def _wake_dependent_tasks(
        self,
        project_id: str,
        completed_task_id: str,
        merge_commit: str | None = None,
    ) -> None:
        """Unblock + notify assignees whose depends_on are all approved/closed.

        merge_commit（duty 增强第二部分）：merge 义务 fulfill 路径传入本次
        merge 的 MAIN HEAD —— [DEPENDENCY MET] 文案升级为「基于新 HEAD 复核/
        重验」；为 None（review.py / close.py 默认调用）文案保持现状。
        """
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE status = 'blocked' AND is_archived = 0",
            [],
        )
        if not rows:
            return

        completed = set()
        done_rows = await _query(
            project_id,
            "SELECT id FROM tasks WHERE status IN ('approved','closed') "
            "AND is_archived = 0",
            [],
        )
        completed = {r["id"] for r in done_rows}
        completed.add(completed_task_id)

        for row in rows:
            task = self._row(row)
            tid = task["id"]
            deps = task.get("depends_on") or []
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except (json.JSONDecodeError, TypeError):
                    deps = []
            if not isinstance(deps, list):
                deps = []

            reason = (task.get("blocked_reason") or "").strip()
            reason_l = reason.lower()
            mentions = completed_task_id in reason or completed_task_id[:8] in reason

            # Auto-unblock only with structured evidence (task id in depends_on
            # or dependency: reason mentioning the completed task id).
            # Agent-name-only weak match removed (TEST11 audit H3) — CEO/HR
            # and zero-assignment agents were false-positive "all done".
            if completed_task_id not in deps and not (
                reason_l.startswith("dependency:") and mentions
            ):
                continue

            # All explicit depends_on must be done (if any)
            if deps and not all(d in completed for d in deps):
                continue

            assignee = task.get("assignee_id")
            try:
                await self.unblock_task(project_id, tid)
            except Exception as e:
                log.warning(
                    "dependent_unblock_failed",
                    task_id=tid,
                    completed=completed_task_id,
                    error=str(e),
                )
                continue

            log.info(
                "dependent_task_unblocked",
                task_id=tid,
                completed=completed_task_id,
                assignee=assignee,
            )
            if not assignee:
                await _notify_creator_deps_merged(
                    project_id, task, merge_commit
                )
                continue
            try:
                from hiveweave.services.inbox import InboxService
                from hiveweave.agents.trigger import trigger_subordinate

                title = (task.get("title") or "")[:80]
                await InboxService().send_message(
                    "system",
                    assignee,
                    _dependency_met_message(
                        completed_task_id, title, merge_commit
                    ),
                    message_type="system",
                    priority="urgent",
                    task_id=tid,
                    # 显式同键（审计 D）：与 obligation 实现共用，文案漂移
                    # 不再靠内容哈希巧合防双发
                    idempotency_key=(
                        f"dep-met:{tid}:{completed_task_id}:"
                        f"{str(merge_commit or '')[:12]}"
                    ),
                )
                await trigger_subordinate(assignee)
            except Exception as e:
                log.warning(
                    "dependent_wake_failed",
                    task_id=tid,
                    error=str(e),
                )
            # duty 增强第二部分：creator FYI（wake=False，同 commit 幂等）
            await _notify_creator_deps_merged(project_id, task, merge_commit)

    async def reconcile_blocked_tasks(self, project_id: str) -> int:
        """Sweep blocked tasks: met deps / expired timers → unblock (TEST11 #8).

        Returns number of tasks unblocked. Idempotent.
        """
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE status = 'blocked' AND is_archived = 0",
            [],
        )
        if not rows:
            return 0

        done_rows = await _query(
            project_id,
            "SELECT id FROM tasks WHERE status IN ('approved','closed') "
            "AND is_archived = 0",
            [],
        )
        completed = {r["id"] for r in done_rows}
        woken = 0
        for row in rows:
            task = self._row(row)
            tid = task["id"]
            wait_kind = (task.get("wait_kind") or "").lower()
            wake_at = task.get("wake_at")
            deps = task.get("depends_on") or []
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except (json.JSONDecodeError, TypeError):
                    deps = []
            if not isinstance(deps, list):
                deps = []

            should_wake = False
            reason_tag = ""
            deps_met = bool(deps) and all(d in completed for d in deps)
            if wait_kind == "timer" and wake_at is not None:
                try:
                    if int(wake_at) <= now_ms:
                        should_wake = True
                        reason_tag = "timer_expired"
                except (TypeError, ValueError):
                    pass
            if deps_met:
                should_wake = True
                reason_tag = reason_tag or "depends_on_met"

            if not should_wake:
                continue
            # duty 增强第二部分：deps 满足驱动的解封，尝试从 deps 最近的
            # task.merged 事件取 MAIN HEAD（取不到 → 文案保持现状）。
            merge_commit = (
                await _recent_merged_commit_for_deps(project_id, deps)
                if deps_met
                else None
            )
            assignee = task.get("assignee_id")
            # 审计 O3：无 assignee 的 VERIFY 是 QA 死区（等待 hire），即使
            # wait 已满足也不能 unblock —— 顶成 running 却没有 QA 真正执行，
            # 反被 _project_has_in_flight 视为占用 MAIN 运行时，拖死整个队列。
            if not assignee and VerifyMixin._is_verify_task(task):
                continue
            try:
                await self.unblock_task(project_id, tid)
                woken += 1
            except Exception as e:
                log.warning(
                    "reconcile_blocked_unblock_failed",
                    task_id=tid,
                    error=str(e),
                )
                continue
            log.info(
                "reconcile_blocked_unblocked",
                task_id=tid,
                reason=reason_tag,
                assignee=assignee,
            )
            if not assignee:
                if deps_met:
                    await _notify_creator_deps_merged(
                        project_id, task, merge_commit
                    )
                continue
            try:
                from hiveweave.services.inbox import InboxService
                from hiveweave.agents.trigger import trigger_subordinate

                title = (task.get("title") or "")[:80]
                if merge_commit:
                    body = (
                        f"[BLOCKED RECONCILED] Task '{title}' ({tid[:8]}) "
                        "unblocked. " + _deps_merged_head_note(merge_commit)
                    )
                else:
                    body = (
                        f"[BLOCKED RECONCILED] Task '{title}' ({tid[:8]}) "
                        f"unblocked ({reason_tag}). Continue or submit_task."
                    )
                await InboxService().send_message(
                    "system",
                    assignee,
                    body,
                    message_type="system",
                    priority="urgent",
                    task_id=tid,
                )
                await trigger_subordinate(assignee)
            except Exception as e:
                log.warning(
                    "reconcile_blocked_notify_failed",
                    task_id=tid,
                    error=str(e),
                )
            # duty 增强第二部分：creator FYI（wake=False，同 commit 幂等）
            if deps_met:
                await _notify_creator_deps_merged(
                    project_id, task, merge_commit
                )
        return woken

    async def lock_implementer_if_needed(
        self,
        project_id: str,
        task_id: str,
        agent_id: str,
    ) -> None:
        """Pin implementer_id + worktree on first running (TEST21 M2).

        Reassign must not rewrite these — review evidence follows the
        implementer worktree, not the current assignee.
        """
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            "SELECT implementer_id FROM tasks WHERE id = ?",
            [task_id],
        )
        if not rows:
            return
        if rows[0]["implementer_id"]:
            return
        wt: str | None = None
        try:
            from hiveweave.services.worktree_review import agent_worktree_path

            wt = await agent_worktree_path(str(agent_id))
        except Exception as e:
            log.debug(
                "lock_implementer_worktree_lookup_failed",
                task_id=task_id,
                error=str(e),
            )
        now_ms = int(time.time() * 1000)
        await _execute(
            project_id,
            "UPDATE tasks SET implementer_id = ?, implementer_worktree = ?, "
            "updated_at = ? WHERE id = ? AND "
            "(implementer_id IS NULL OR implementer_id = '')",
            [agent_id, wt, now_ms, task_id],
        )
        log.info(
            "implementer_locked",
            task_id=task_id[:12],
            implementer_id=agent_id[:8],
            worktree=(wt or "")[-40:],
        )

    async def set_owner_parked(
        self,
        project_id: str,
        task_ids: list[str],
        *,
        parked: bool,
    ) -> None:
        """Mark/clear owner_parked on tasks (TEST21 M5 stall mute)."""
        if not task_ids:
            return
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        flag = 1 if parked else 0
        for tid in task_ids:
            try:
                await _execute(
                    project_id,
                    "UPDATE tasks SET owner_parked = ?, updated_at = ? "
                    "WHERE id = ?",
                    [flag, now_ms, tid],
                )
            except Exception as e:
                log.warning(
                    "set_owner_parked_failed",
                    task_id=tid[:12],
                    error=str(e),
                )

    async def clear_owner_parked_for_agent(
        self, project_id: str, agent_id: str
    ) -> None:
        """Clear owner_parked on recovery (agent completed a turn)."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        try:
            await _execute(
                project_id,
                "UPDATE tasks SET owner_parked = 0, updated_at = ? "
                "WHERE assignee_id = ? AND owner_parked = 1 AND is_archived = 0",
                [now_ms, agent_id],
            )
        except Exception as e:
            log.warning(
                "clear_owner_parked_failed",
                agent_id=agent_id[:8],
                error=str(e),
            )

    async def set_wake_at(
        self, project_id: str, task_id: str, wake_at_ms: int | None
    ) -> None:
        """Set or clear wake_at (real-time ms) for timer waits."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        await _execute(
            project_id,
            "UPDATE tasks SET wake_at = ?, updated_at = ? WHERE id = ?",
            [wake_at_ms, now_ms, task_id],
        )

