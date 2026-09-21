"""Task status transition helpers."""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from .db import (
    _conn,
    _ensure_schema,
    _execute,
    _execute_tx,
    _query,
    build_task_event_insert,
    build_task_wait_clear_statement,
    publish_task_event,
)
from .constants import _TRANSITIONS

log = structlog.get_logger(__name__)


class TransitionsMixin:
    """_transition / _transition_multi."""

    if TYPE_CHECKING:
        _clear_task_wait_contracts: Any
        _pending_task_waiters: Any
        _wake_task_waiters: Any

    async def _transition(self, project_id: str, task_id: str, target: str,
                          *, actor_id: str | None = None,
                          reason_code: str | None = None,
                          detail: str | None = None) -> None:
        """Validate and execute a status transition.

        Raises ValueError if the task is not found or the transition is illegal.
        Writes a task_events row in the same transaction (Transactional Outbox).

        TEST21 M9: system migrations pass ``reason_code`` + ``detail`` into
        task_events.payload.

        Leaving ``blocked`` clears wait metadata (blocked_reason / wait_kind /
        wake_at) in the same transaction — state-machine invariant: not blocked
        means not waiting. Fixes residual wait_kind on running tasks when
        ``start_task`` is used instead of ``unblock_task`` (TEST11 #5-L1).
        """
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT status FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current = rows[0]["status"]
        if target not in _TRANSITIONS.get(current, set()):
            raise ValueError(f"Illegal transition: {current} → {target}")
        now_ms = int(time.time() * 1000)
        payload_obj: dict = {}
        if reason_code:
            payload_obj["reason_code"] = str(reason_code)[:80]
        if detail:
            payload_obj["detail"] = str(detail)[:500]
        payload = json.dumps(payload_obj) if payload_obj else "{}"
        (event_sql, event_params), event_ts, _event_id = build_task_event_insert(
            project_id, task_id, f"task.{target}", current, target,
            actor_id=actor_id, payload=payload, now_ms=now_ms,
        )
        # P2-5: 清等待必须与 task 状态写 + outbox 事件**同一次提交**——
        # 否则崩溃窗口可只落一边（等待已清、唤醒事件没落 ⇒ 唤醒永久丢失）。
        # 先读 waiter 行（供提交后唤醒用），再把它作为第三条语句追加进
        # 下面既有的事务；唤醒在 `_execute_tx` 返回之后。
        # ⚠ 取舍（有意）：语句并进事务 ⇒ 它失败则整批回滚，**状态转换会失败**
        # （旧版清等待在事务外、失败被吞）。这里不为此加二次降级：同一批里
        # `UPDATE tasks` 先执行，能读写 `agent_waits` 的连接/锁问题会先在那里
        # 暴露；而 blocked-exit 支保有的降级重试只服务「保住状态转换」这个
        # 既有目标（见下）。
        waiters = await self._pending_task_waiters(project_id, task_id)
        wait_clear = build_task_wait_clear_statement(
            now_ms, [r["id"] for r in waiters]
        )
        if current == "blocked":
            # Defensive: any exit from blocked clears wait metadata
            try:
                stmts: list[tuple[str, list]] = [
                    ("UPDATE tasks SET status = ?, blocked_reason = NULL, "
                     "wait_kind = NULL, wake_at = NULL, updated_at = ? WHERE id = ?",
                     [target, now_ms, task_id]),
                    (event_sql, event_params),
                ]
                if wait_clear is not None:
                    stmts.append(wait_clear)
                await _execute_tx(project_id, stmts)
            except Exception as e:
                # Prefer status transition over abort; then best-effort clear
                log.warning(
                    "blocked_exit_clear_metadata_failed",
                    task_id=task_id,
                    error=str(e),
                )
                # 降级路径**故意**不带清等待语句：本支存在的理由就是
                # 「宁可牺牲附带清理也要保住状态转换」。等待清理由提交后
                # 的 `_clear_task_wait_contracts` 兜底（退化为 P2-5 之前的
                # 语义）。
                # ⚠ `waiters` 必须同时清空：不清空会出现**双重唤醒**
                # （下面 `_wake_task_waiters` 叫醒一次 + 事务外兜底再叫一次，
                # 因为等待此时确实还没清）—— 阳性对照实测 2 次调用。
                waiters = []
                await _execute_tx(project_id, [
                    ("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                     [target, now_ms, task_id]),
                    (event_sql, event_params),
                ])
                try:
                    await _execute(
                        project_id,
                        "UPDATE tasks SET blocked_reason = NULL, wait_kind = NULL, "
                        "wake_at = NULL, updated_at = ? WHERE id = ?",
                        [now_ms, task_id],
                    )
                except Exception as e2:
                    log.warning(
                        "blocked_exit_metadata_retry_failed",
                        task_id=task_id,
                        error=str(e2),
                    )
        else:
            stmts = [
                ("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                 [target, now_ms, task_id]),
                (event_sql, event_params),
            ]
            if wait_clear is not None:
                stmts.append(wait_clear)
            await _execute_tx(project_id, stmts)
        await publish_task_event(
            project_id, task_id, f"task.{target}", target, event_ts
        )
        log.info("task_transition", task_id=task_id,
                 from_status=current, to_status=target,
                 reason_code=reason_code)

        # TEST17 fix: clear agent_waits referencing this task and wake waiters.
        # wake_on=["task_transition"] was dead code — no production caller ever
        # matched it. Wire it up here: any transition clears matching waits.
        # P2-5: 主路径已在上面的事务里清完，这里先唤醒那批（提交后），
        # 再由 `_clear_task_wait_contracts` 兜底提交窗口内新建的等待行。
        await self._wake_task_waiters(project_id, task_id, waiters, in_tx=True)
        await self._clear_task_wait_contracts(project_id, task_id)

    async def _transition_multi(self, project_id: str, task_id: str,
                               *targets: str,
                               actor_id: str | None = None,
                               reason_code: str | None = None,
                               detail: str | None = None) -> None:
        """Validate and execute a multi-step transition atomically.

        Validates each step against _TRANSITIONS, then performs a single
        UPDATE to the final state — no intermediate state is ever visible
        to concurrent readers. Writes a task_events row in the same tx.

        Example: _transition_multi(pid, tid, "rework", "running")
        validates reviewing → rework → running, then UPDATEs directly
        to "running" in one statement.
        """
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT status FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current = rows[0]["status"]
        # Validate each step
        state = current
        for target in targets:
            if target not in _TRANSITIONS.get(state, set()):
                raise ValueError(f"Illegal transition: {state} → {target}")
            state = target
        # Single UPDATE to final state — atomic, no intermediate visible
        now_ms = int(time.time() * 1000)
        final = targets[-1]
        payload_obj: dict = {}
        if reason_code:
            payload_obj["reason_code"] = str(reason_code)[:80]
        if detail:
            payload_obj["detail"] = str(detail)[:500]
        payload = json.dumps(payload_obj) if payload_obj else "{}"
        (event_sql, event_params), event_ts, _event_id = build_task_event_insert(
            project_id, task_id, f"task.{final}", current, final,
            actor_id=actor_id, payload=payload, now_ms=now_ms,
        )
        # P2-5: 同 `_transition` —— 清等待并入本次提交，提交后才唤醒。
        waiters = await self._pending_task_waiters(project_id, task_id)
        wait_clear = build_task_wait_clear_statement(
            now_ms, [r["id"] for r in waiters]
        )
        multi_stmts: list[tuple[str, list]] = [
            ("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
             [final, now_ms, task_id]),
            (event_sql, event_params),
        ]
        if wait_clear is not None:
            multi_stmts.append(wait_clear)
        await _execute_tx(project_id, multi_stmts)
        await publish_task_event(
            project_id, task_id, f"task.{final}", final, event_ts
        )
        log.info("task_transition_multi", task_id=task_id,
                 from_status=current, through=list(targets[:-1]),
                 to_status=final, reason_code=reason_code)

        # L2 fix: clear wait contracts on multi-step transitions too
        # (rework path uses _transition_multi, waiters need to be woken)
        await self._wake_task_waiters(project_id, task_id, waiters, in_tx=True)
        await self._clear_task_wait_contracts(project_id, task_id)

