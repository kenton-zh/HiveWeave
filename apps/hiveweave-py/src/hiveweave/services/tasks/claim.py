"""Claim / unclaim / reassign / promote helpers."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from .db import (
    _ensure_schema,
    _execute,
    _execute_tx,
    _query,
    build_task_event_insert,
    publish_task_event,
)

if TYPE_CHECKING:
    # Methods/attrs provided by sibling mixins composed into TaskService.
    # Declared here so mypy can resolve cross-mixin references.
    pass

log = structlog.get_logger(__name__)


class ClaimMixin:
    """ensure/promote claim, claim_task, reassign, unclaim."""

    if TYPE_CHECKING:
        get_task: Any
        _is_verify_task: Any
        _COLUMNS: Any
        _row: Any
        require_task_id: Any
        _transition: Any
        emit_task_event: Any

    async def ensure_assignee_claimed(
        self, project_id: str, task_id: str
    ) -> bool:
        """If task is assigned + created (non-VERIFY), promote to claimed.

        Returns True if a claim transition ran. Idempotent for already-claimed
        / VERIFY / unassigned rows.
        """
        task = await self.get_task(project_id, task_id)
        if not task:
            return False
        if task.get("status") != "created":
            return False
        if self._is_verify_task(task):
            return False
        assignee = task.get("assignee_id")
        if not assignee:
            return False
        await self.claim_task(project_id, task_id, assignee)
        return True

    async def promote_assigned_created(
        self, project_id: str, agent_id: str | None = None
    ) -> int:
        """Heal legacy rows: assignee set + status=created → claimed (non-VERIFY).

        Used so task-advance obligations see assign=claim for older data.
        """
        await _ensure_schema(project_id)
        if agent_id:
            rows = await _query(
                project_id,
                f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0 "
                "AND status = 'created' AND assignee_id = ?",
                [agent_id],
            )
        else:
            rows = await _query(
                project_id,
                f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0 "
                "AND status = 'created' AND assignee_id IS NOT NULL",
            )
        n = 0
        for r in rows:
            d = self._row(r)
            if self._is_verify_task(d):
                continue
            tid = d.get("id")
            aid = d.get("assignee_id")
            if not tid or not aid:
                continue
            try:
                await self.claim_task(project_id, tid, aid)
                n += 1
            except Exception as e:
                log.warning(
                    "promote_assigned_created_failed",
                    task_id=tid,
                    error=str(e),
                )
        return n

    async def claim_task(
        self,
        project_id: str,
        task_id: str,
        agent_id: str,
        *,
        bypass_verify_serialize: bool = False,
    ) -> None:
        """Claim a task (created → claimed). Sets assignee_id + claimed_at.

        Assign = claim: if the task is already claimed/running by this agent
        (e.g. create_task/dispatch set assignee), this is a no-op. Only
        'created' tasks transition. Wrong-assignee or illegal states raise.

        验收串行化（issue #6，终审 M1）：外部直调 claim 一个 created VERIFY
        是「第二道没有锁的门」—— QA 工具 / API / submit 自动 claim 都可能
        绕过 _nudge_one_verify_task 制造双 in-flight。故 VERIFY created 的
        claim 也走同一 per-project 锁 + in-flight 检查；被挡 raise。
        ``bypass_verify_serialize`` 仅供 _nudge_one_verify_task 持锁内调用
        （锁已持有、检查已做，直接 claim）。
        """
        task_id = await self.require_task_id(project_id, task_id)
        rows = await _query(project_id,
            "SELECT status, assignee_id, title FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        row = dict(rows[0])
        current = row["status"]
        existing_assignee = row["assignee_id"]
        if current in ("claimed", "running") and existing_assignee == agent_id:
            # Idempotent: already assigned to this agent (assign=claim path)
            return
        if current != "created":
            raise ValueError(
                f"Task {task_id[:8]} is already '{current}'. "
                f"Only 'created' tasks can be claimed. "
                f"If the task is running, continue working and submit_task when done."
            )
        is_verify = self._is_verify_task(row)
        if is_verify and not bypass_verify_serialize:
            from hiveweave.tools.tasks.verify_spawn import (
                _project_has_in_flight_verify,
                _verify_serialize_lock,
            )

            async with _verify_serialize_lock(project_id):
                if await _project_has_in_flight_verify(
                    project_id, except_id=task_id
                ):
                    raise ValueError(
                        f"Task {task_id[:8]} is a VERIFY task and another "
                        "VERIFY is in flight on the shared MAIN runtime "
                        "(verification is serialized: one at a time). This "
                        "task stays queued as 'created' — the platform will "
                        "wake you via inbox when MAIN is free. Do NOT retry "
                        "claim_task on it; commit_turn(waiting) or work on "
                        "your other tasks meanwhile."
                    )
                await self._claim_created(project_id, task_id, agent_id)
            return
        await self._claim_created(project_id, task_id, agent_id)

    async def _claim_created(
        self, project_id: str, task_id: str, agent_id: str
    ) -> None:
        """created → claimed 转移主体（claim_task 内部复用）。"""
        await self._transition(project_id, task_id, "claimed", actor_id=agent_id)
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "UPDATE tasks SET assignee_id = ?, claimed_at = ?, updated_at = ? "
            "WHERE id = ?", [agent_id, now_ms, now_ms, task_id])
        await self.emit_task_event(
            project_id,
            task_id,
            "claimed",
            agent_id=agent_id,
            summary=f"[claimed] task {task_id[:8]} by agent",
        )

    async def reassign_task(
        self,
        project_id: str,
        task_id: str,
        *,
        new_assignee_id: str,
        reassigned_by: str,
        reason: str = "",
    ) -> dict:
        """Change assignee and ensure the new owner has an actionable status.

        TEST13 P0-2: NL "forward" creates no obligation — this is the
        structured transfer. Keeps status when already claimed/running;
        promotes created → claimed; resets submitted/reviewing to claimed
        so the new assignee can re-submit.

        TEST21 M2: always write ``task.reassigned`` event (no silent drift);
        never clears ``implementer_id`` / ``implementer_worktree``.
        """
        task_id = await self.require_task_id(project_id, task_id)
        task = await self.get_task(project_id, task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        if task.get("is_archived"):
            raise ValueError(f"Task {task_id[:8]} is archived")
        status = task.get("status") or ""
        if status in ("closed", "cancelled"):
            raise ValueError(f"Cannot reassign terminal task ({status})")
        now_ms = int(time.time() * 1000)
        old = task.get("assignee_id")
        new_status = status
        if status == "created":
            if self._is_verify_task(task):
                # 验收串行化（issue #6，审计 M1）：VERIFY created 改派不能直接
                # 转 claimed —— 那会绕过 _nudge_one_verify_task 的串行锁，
                # 在已有 in-flight VERIFY 时制造双并发 E2E。只换 assignee、
                # 保持 created 排队，随后走锁内 nudge 决策（空闲即唤醒）。
                new_status = "created"
            else:
                await self._transition(project_id, task_id, "claimed",
                                       actor_id=reassigned_by)
                new_status = "claimed"
        elif status == "submitted":
            await self._transition(project_id, task_id, "running",
                                   actor_id=reassigned_by)
            new_status = "running"
        elif status == "blocked":
            await self._transition(project_id, task_id, "running",
                                   actor_id=reassigned_by)
            new_status = "running"
        elif status == "reviewing":
            # reviewing has no direct → claimed; force for reassignment
            new_status = "claimed"
        # Always audit — including running→running assignee swaps (TEST21 M2)
        payload = {
            "from_assignee": old,
            "to_assignee": new_assignee_id,
            "reason": (reason or "")[:500],
            "implementer_id": task.get("implementer_id"),
            "status": new_status,
        }
        # Outbox 纪律：改派 UPDATE 与事件 INSERT 同事务原子落库。
        # 旧实现两次 commit + 吞 INSERT 异常 → assignee 已换但
        # task.reassigned 静默缺失，§4.5 assignee 游标丢段。失败上抛。
        (ev_sql, ev_params), _ev_ts, _ev_id = build_task_event_insert(
            project_id, task_id, "task.reassigned", status, new_status,
            actor_id=reassigned_by, payload=payload, now_ms=now_ms,
        )
        stmts: list[tuple[str, list]] = []
        if status == "reviewing":
            stmts.append((
                "UPDATE tasks SET status = 'claimed', progress = 10, "
                "updated_at = ? WHERE id = ?",
                [now_ms, task_id],
            ))
        stmts.append((
            "UPDATE tasks SET assignee_id = ?, "
            "claimed_at = COALESCE(claimed_at, ?), "
            "owner_parked = 0, updated_at = ? WHERE id = ?",
            [new_assignee_id, now_ms, now_ms, task_id],
        ))
        stmts.append((ev_sql, ev_params))
        await _execute_tx(project_id, stmts)
        await publish_task_event(
            project_id, task_id, "task.reassigned", new_status, now_ms,
        )
        # VERIFY created 改派：锁内 nudge 决策是否唤醒（空闲即 claim+send）。
        # created 路径下 assignee 刚更新，重读一次拿新 assignee。best-effort。
        if status == "created" and self._is_verify_task(task):
            try:
                from hiveweave.tools.tasks.verify_spawn import (
                    _nudge_one_verify_task,
                )

                fresh = await self.get_task(project_id, task_id)
                await _nudge_one_verify_task(
                    project_id, reassigned_by, fresh, reason="merge"
                )
            except Exception as e:
                log.warning(
                    "verify_reassign_nudge_failed",
                    task_id=task_id,
                    error=str(e),
                )
        log.info(
            "task_reassigned",
            project_id=project_id,
            task_id=task_id,
            from_assignee=(old or "")[:8],
            to_assignee=new_assignee_id[:8],
            by=reassigned_by[:8],
            status=new_status,
            reason=(reason or "")[:80],
            implementer=(task.get("implementer_id") or "")[:8],
        )
        return {
            "task_id": task_id,
            "from_assignee": old,
            "to_assignee": new_assignee_id,
            "status": new_status,
            "implementer_id": task.get("implementer_id"),
        }

    async def unclaim_task(self, project_id: str, task_id: str) -> None:
        """释放认领（claimed → created），清空 assignee 供重新分配。

        误绑纠正的另一半：coordinator 把任务绑错人后，release 回 created
        再 dispatch 给正确的人（不必像过去那样新建任务、留僵尸）。
        """
        rows = await _query(
            project_id, "SELECT status FROM tasks WHERE id = ?", [task_id]
        )
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        await self._transition(project_id, task_id, "created")
        now_ms = int(time.time() * 1000)
        await _execute(
            project_id,
            "UPDATE tasks SET assignee_id = NULL, claimed_at = NULL, "
            "updated_at = ? WHERE id = ?",
            [now_ms, task_id],
        )
        log.info("task_unclaimed", project_id=project_id, task_id=task_id)

        # 验收串行化（issue #6）：VERIFY unclaim（claimed → created 清 assignee）
        # 即释放 MAIN 运行时独占，立即泵出队列中下一个 created VERIFY，
        # 不必等 game_time tick。best-effort。
        try:
            from hiveweave.tools.tasks.verify_spawn import (
                nudge_pending_verify_tasks,
            )

            await nudge_pending_verify_tasks(project_id)
        except Exception as e:
            log.warning(
                "verify_pending_pump_after_unclaim_failed",
                task_id=task_id,
                error=str(e),
            )

