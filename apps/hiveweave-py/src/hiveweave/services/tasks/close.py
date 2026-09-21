"""Close, merge gate, archive, wait-contract clear."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from .db import (
    _ensure_schema,
    _execute,
    _execute_tx,
    _query,
    build_task_event_insert,
    WAKE_CATEGORY_TASK_WAIT_CLEARED,
    build_inbox_wake_insert,
    build_task_wait_clear_statement,
    publish_task_event,
)
from .errors import MergeRequiredError
from .verify import VerificationCaseService  # noqa: F401

log = structlog.get_logger(__name__)

# 持有 fire-and-forget 记忆任务引用，防止 GC/loop 关闭时静默取消
# （P2-1：async 生命周期卫生，对齐孤儿 streaming 纪律）。
_task_memory_pending: set["asyncio.Task"] = set()


class CloseMixin:
    """close / merge gate / archive / umbrella / wait contracts."""

    if TYPE_CHECKING:
        require_task_id: Any
        get_task: Any
        _is_verify_task: Any
        _transition: Any
        emit_task_event: Any
        _wake_dependent_tasks: Any
        list_tasks: Any

    async def close_task(
        self,
        project_id: str,
        task_id: str,
        *,
        skip_merge_gate: bool = False,
        reason_code: str | None = None,
    ) -> None:
        """Close a task (approved|verifying → closed). Sets closed_at.

        Non-VERIFY code tasks must pass the merge/delivery hard gate
        (``_enforce_merge_on_close``). Detection alone used to stamp and
        still close — that fail-open path is gone (TEST20 P0-A / N1).

        ``skip_merge_gate`` is for ledger hygiene migrations only
        (e.g. ``migrate_orphan_approved``).
        """
        task_id = await self.require_task_id(project_id, task_id)

        task = await self.get_task(project_id, task_id)
        if task and not self._is_verify_task(task) and not skip_merge_gate:
            await self._enforce_merge_on_close(project_id, task)

        await self._transition(
            project_id, task_id, "closed", reason_code=reason_code
        )
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "UPDATE tasks SET closed_at = ?, updated_at = ? WHERE id = ?",
            [now_ms, now_ms, task_id])
        await self.emit_task_event(
            project_id,
            task_id,
            "closed",
            summary=f"[closed] task {task_id[:8]}",
        )
        # TEST_DSH_32 P1（closed 事件作废）：终态任务名下残留的
        # unread wake=1 通知降级为背景——义务已消失，不得继续唤醒。
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().demote_wake_for_task(
                project_id, task_id, reason="task_closed"
            )
        except Exception as e:
            log.debug(
                "close_demote_inbox_wake_failed",
                task_id=task_id,
                error=str(e),
            )
        await self._wake_dependent_tasks(project_id, task_id)
        try:
            await self._maybe_close_umbrella_parent(project_id, task_id)
        except Exception as e:
            log.warning(
                "umbrella_parent_close_failed",
                task_id=task_id,
                error=str(e),
            )
        try:
            await self._gc_assignee_worktree_if_idle(project_id, task)
        except Exception as e:
            log.warning(
                "worktree_gc_on_close_failed",
                task_id=task_id,
                error=str(e),
            )
        # TEST6 evening P1-1: closed ⇒ no pending obligations (fail-open)
        try:
            from hiveweave.services.obligation import ObligationLedger

            await ObligationLedger().reconcile_closed_task(project_id, task_id)
        except Exception as e:
            log.warning(
                "obligation.reconcile_closed_failed",
                task_id=task_id,
                error=str(e),
            )
        # 审计 #9: 任务终态后回收悬空 staffing demand（如 VERIFY 停摆看门狗
        # 建的 open 需求）；已 hire 兑现的 fulfilled 记录不受影响。
        try:
            from hiveweave.services.staffing import staffing_demand_service

            await staffing_demand_service.cancel_open_demands_for_task(
                project_id, task_id, reason="task_closed"
            )
        except Exception as e:
            log.warning(
                "staffing_demand_cancel_on_close_failed",
                task_id=task_id,
                error=str(e),
            )

        try:
            from .ship_nudge import maybe_nudge_ceo_ship_ready

            await maybe_nudge_ceo_ship_ready(project_id, task)
        except Exception as e:
            log.warning(
                "ship_nudge_failed",
                task_id=task_id,
                error=str(e),
            )

        # 任务完成记忆：异步 best-effort 为 assignee LLM 总结并写一条记忆。
        # 绝不影响 close 主流程与账本；失败只记日志（内含幂等与降级）。
        try:
            from hiveweave.services.task_memory import (
                maybe_write_task_completion_memory,
            )

            task_handle = asyncio.create_task(
                maybe_write_task_completion_memory(project_id, task_id)
            )
            _task_memory_pending.add(task_handle)
            task_handle.add_done_callback(_task_memory_pending.discard)
        except Exception as e:
            log.warning(
                "task_memory_schedule_failed",
                task_id=task_id,
                error=str(e),
            )

    async def _gc_assignee_worktree_if_idle(
        self, project_id: str, task: dict | None
    ) -> None:
        """TEST6 P2: after close, reclaim the assignee's write worktree when
        they have no in-flight tasks left.

        Merge-time cleanup skips teardown while the assignee has open tasks
        — but nothing re-triggered it afterwards, so TEST6 ended with 5
        worktrees + branches still checked out after 8/8 tasks closed.
        Safety is inherited from ``delete()``: the branch is disposed with
        ``git branch -d`` and unmerged branches are preserved + reported
        (never force-deleted), so evidence-only branches survive.
        """
        if not task:
            return
        assignee_id = task.get("assignee_id")
        if not assignee_id:
            return
        from hiveweave.db import meta as meta_db

        ws = await meta_db.get_project_workspace(project_id)
        if not ws:
            return
        rows = await _query(
            project_id,
            "SELECT id, short_id, status, role, permission_type "
            "FROM agents WHERE id = ?",
            [str(assignee_id)],
        )
        if not rows:
            return
        agent = dict(rows[0])
        if (agent.get("status") or "").lower() != "active":
            return  # dismiss path already owns teardown
        from hiveweave.services.git_worktree.ensure import (
            agent_gets_write_worktree,
        )

        if not agent_gets_write_worktree(agent):
            return
        short_id = (agent.get("short_id") or "").strip()
        if not short_id:
            return
        from hiveweave.services.git_worktree.reconcile import (
            _assignee_has_open_tasks,
        )

        if await _assignee_has_open_tasks(ws, short_id):
            return
        from hiveweave.services.git_worktree import GitWorktreeService

        res = await GitWorktreeService().delete(ws, short_id)
        log.info(
            "worktree_gc_on_close",
            task_id=task.get("id"),
            short_id=short_id,
            branch=res.get("branch"),
            preserved_branch=res.get("preserved_branch"),
            removed=res.get("removed"),
        )
        # 0-2：这条是 merge 侧「assignee 还有 open 任务 ⇒ 跳过清理」之后**唯一
        # 的补删路径** ⇒ #21 的现场形态（合并成功 + worktree 回收 + husk 残留）
        # 完全可以由它产生，而此前它与 merge 侧一样只读 preserved_branch。
        from hiveweave.services.git_worktree.service_lifecycle import (
            _surface_husk_left,
        )

        _surface_husk_left(
            res,
            short_id=short_id,
            branch=str(res.get("branch") or ""),
            event="worktree_gc_on_close_husk_left",
        )

    def _task_skips_merge_gate(self, task: dict) -> bool:
        """docs/explore / explicit no-code / already verifying after merge."""
        status = (task.get("status") or "").lower()
        # verifying ⇒ merge already landed and VERIFY was spawned
        if status == "verifying":
            return True
        tags = task.get("tags") or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []
        tag_l = {str(t).lower() for t in tags} if isinstance(tags, list) else set()
        if tag_l & {"docs_only", "docs", "explore", "no-code", "no_code"}:
            return True
        policy = (task.get("policy_id") or "").lower()
        if policy in ("docs_only", "explore"):
            return True
        ev = task.get("evidence") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except Exception:
                ev = {}
        if isinstance(ev, dict):
            from hiveweave.services.worktree_review import (
                evidence_merge_waived,
            )

            if evidence_merge_waived(ev):
                return True
            # 2026-08-11 意见核实：merge_fact 不再在此跳过 —— 有 merge fact
            # 仍须验 tip 是否真在 main（_enforce_merge_on_close 顶部校验，
            # 防「merge 后又新增 commit」静默通过）。跳过仅限无 merge 需求
            # 的场景（verifying/docs/no_code/waived）。
            for key in (
                "no_code_change",
                "noCodeChange",
                "verification_only",
                "verificationOnly",
            ):
                if ev.get(key) is True:
                    return True
        return False

    async def _enforce_merge_on_close(
        self, project_id: str, task: dict
    ) -> None:
        """Hard gate: refuse close when worktree still has effective output.

        On block: restore approved(95), rebuild MERGE obligation, wake
        merge_proxy. Explicit ``waive_merge`` / merge facts / verifying
        status / docs-only skip the gate.
        """
        from hiveweave.services.worktree_review import (
            agent_worktree_path,
            effective_delivery,
            evidence_has_merge_fact,
            evidence_merge_waived,
            project_main_workspace,
        )

        if self._task_skips_merge_gate(task):
            return

        ev = task.get("evidence") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except Exception:
                ev = {}
        if not isinstance(ev, dict):
            ev = {}

        if evidence_merge_waived(ev):
            return

        if evidence_has_merge_fact(ev):
            # P0-1 is-ancestor companion: merge fact exists, but verify the
            # branch tip is actually in main. Catches "new commits added after
            # merge" (like 5510049 stranded on hw/A003/work post-close).
            # 2026-08-11 意见核实：此分支此前是死代码 —— _task_skips_merge_gate
            # 在 evidence_has_merge_fact 时已提前 return True，永远到不了这里。
            # 已把 merge_fact 从 _task_skips_merge_gate 移除，本校验恢复生效。
            assignee = task.get("assignee_id")
            tid = str(task.get("id") or "")
            main_ws = await project_main_workspace(project_id)
            if main_ws and assignee:
                try:
                    from hiveweave.services.git_worktree import (
                        GitWorktreeService,
                        _git,
                        _has_git,
                        _resolve_base_branch,
                        _worktree_path,
                    )
                    from hiveweave.services.org import OrgService

                    org = OrgService()
                    agent_rec = await org.resolve_agent(str(assignee))
                    sid = (agent_rec or {}).get("short_id", "")
                    if sid:
                        # Resolve effective path (P0-1 single source)
                        eff_path = await GitWorktreeService._resolve_effective_worktree_path(
                            main_ws, sid
                        )
                        branch = None
                        if _has_git(eff_path):
                            from hiveweave.services.git_worktree import (
                                _current_branch,
                            )
                            branch = await _current_branch(eff_path)
                        if not branch:
                            branch = f"hw/{sid}/work"
                        # Resolve base branch (W2: don't hardcode "main")
                        base_br = await _resolve_base_branch(main_ws) or "main"
                        # Check if branch still exists and is NOT ancestor of base
                        ok_exists, _ = await _git(
                            ["rev-parse", "--verify", branch], main_ws
                        )
                        if ok_exists:
                            ok_anc, _ = await _git(
                                ["merge-base", "--is-ancestor", branch, base_br],
                                main_ws,
                            )
                            if not ok_anc:
                                # Branch has commits not in main — block close
                                log.warning(
                                    "task.close_blocked_tip_not_ancestor",
                                    task_id=tid,
                                    branch=branch,
                                    assignee=assignee,
                                )
                                await self._rollback_close_to_approved(
                                    project_id,
                                    task,
                                    reason="branch_tip_not_in_main",
                                    commits_ahead=None,
                                    dirty_count=0,
                                )
                                raise MergeRequiredError(
                                    f"Cannot close task {tid[:8]}: branch "
                                    f"{branch} has commits not in main "
                                    f"(merge-base --is-ancestor failed). "
                                    f"Merge the branch first.",
                                    reason="branch_tip_not_in_main",
                                    task_id=tid,
                                )
                except MergeRequiredError:
                    raise
                except Exception as anc_err:
                    log.warning(
                        "task.is_ancestor_check_failed",
                        task_id=str(task.get("id") or ""),
                        error=str(anc_err),
                    )
            # P0-1 variant safety net (audit 2026-07-28): merge landed in git
            # but the merge obligation was not cleared (merge via merge_proxy /
            # service-direct / fulfill failed silently). The merge tool's
            # fulfill (misc_tools.py) is the primary path; this is the backstop
            # so a closed task can never leave a pending merge obligation that
            # keeps escalating (CEO had to cancel_task to clear 81b43baa).
            if tid:
                try:
                    from hiveweave.services.obligation import ObligationLedger

                    fulfilled = await ObligationLedger().fulfill(
                        project_id, tid, "merge"
                    )
                    if fulfilled:
                        log.info(
                            "task.close_merge_obligation_cleared",
                            task_id=tid,
                            count=fulfilled,
                            source="close_safety_net",
                        )
                except Exception as ob_err:
                    log.warning(
                        "task.close_merge_obligation_clear_failed",
                        task_id=tid,
                        error=str(ob_err),
                    )
            return

        assignee = task.get("assignee_id")
        tid = str(task.get("id") or "")
        main_ws = await project_main_workspace(project_id)
        wt = await agent_worktree_path(str(assignee)) if assignee else None

        # Worktree already cleaned after a real merge but evidence lacked
        # merge stamp — allow only when status was verifying (handled above)
        # or a verification case records the merge hash.
        if not wt or not main_ws:
            has_case_merge = False
            try:
                rows = await _query(
                    project_id,
                    "SELECT merge_commit_hash, status FROM verification_cases "
                    "WHERE original_task_id = ? ORDER BY created_at DESC LIMIT 1",
                    [tid],
                )
                if rows:
                    case = rows[0]
                    if case.get("merge_commit_hash") or case.get("status") in (
                        "passed",
                        "in_review",
                        "pending",
                    ):
                        # Case exists ⇒ merge path already ran (VERIFY spawned)
                        has_case_merge = True
            except Exception:
                has_case_merge = False
            if has_case_merge:
                return
            # No worktree + no merge fact while still approved = suspicious.
            if (task.get("status") or "").lower() == "approved":
                await self._rollback_close_to_approved(
                    project_id,
                    task,
                    reason="no_worktree_no_merge_fact",
                    commits_ahead=None,
                    dirty_count=0,
                )
                raise MergeRequiredError(
                    f"Cannot close task {tid[:8]}: assignee worktree gone and "
                    f"no merge fact on evidence. Merge first "
                    f"(git_worktree_merge) or waive_merge with audit reason.",
                    reason="no_worktree_no_merge_fact",
                    task_id=tid,
                )
            return

        delivery = await effective_delivery(main_ws, wt)
        ahead = delivery.get("commits_ahead")
        dirty = int(delivery.get("dirty_count") or 0)
        has_output = bool(delivery.get("has_effective_output"))

        if has_output:
            reason = (
                "unmerged_commits"
                if (ahead is not None and int(ahead) > 0)
                else "uncommitted_dirty"
            )
            log.warning(
                "task.close_blocked_unmerged",
                task_id=tid,
                assignee_id=assignee,
                commits_ahead=ahead,
                dirty_count=dirty,
                reason=reason,
            )
            await self._rollback_close_to_approved(
                project_id,
                task,
                reason=reason,
                commits_ahead=ahead if isinstance(ahead, int) else None,
                dirty_count=dirty,
            )
            raise MergeRequiredError(
                f"Cannot close task {tid[:8]}: worktree still has delivery "
                f"(commits_ahead={ahead}, dirty={dirty}). "
                f"git_worktree_checkpoint if dirty, then git_worktree_merge, "
                f"or waive_merge(reason=…) as last resort.",
                reason=reason,
                task_id=tid,
                commits_ahead=ahead if isinstance(ahead, int) else None,
                dirty_count=dirty,
            )

        # Clean + 0 ahead + no merge fact = zero delivery (Rita escape)
        log.warning(
            "task.close_blocked_no_delivery",
            task_id=tid,
            assignee_id=assignee,
        )
        await self._rollback_close_to_approved(
            project_id,
            task,
            reason="no_delivery",
            commits_ahead=0,
            dirty_count=0,
        )
        raise MergeRequiredError(
            f"Cannot close task {tid[:8]}: no effective delivery "
            f"(0 commits ahead, clean worktree, no merge fact). "
            f"Implement + checkpoint + merge, mark no_code_change in "
            f"evidence, or waive_merge with audit reason.",
            reason="no_delivery",
            task_id=tid,
            commits_ahead=0,
            dirty_count=0,
        )

    async def _rollback_close_to_approved(
        self,
        project_id: str,
        task: dict,
        *,
        reason: str,
        commits_ahead: int | None,
        dirty_count: int,
    ) -> None:
        """Stamp evidence + ensure approved status + rebuild MERGE obligation."""
        tid = str(task.get("id") or "")
        if not tid:
            return
        ev = task.get("evidence") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except Exception:
                ev = {}
        if not isinstance(ev, dict):
            ev = {}
        ev["close_blocked"] = True
        ev["close_blocked_reason"] = reason
        if commits_ahead is not None:
            ev["unmerged_commits_ahead"] = commits_ahead
            if commits_ahead > 0:
                ev["closed_with_unmerged_branch"] = True  # legacy stamp name
        if dirty_count:
            ev["uncommitted_dirty_count"] = dirty_count
        now_ms = int(time.time() * 1000)
        status = (task.get("status") or "").lower()
        # Keep / restore approved so CREATOR_MUST_MERGE stays actionable
        if status != "approved":
            try:
                await _execute(
                    project_id,
                    "UPDATE tasks SET status = 'approved', progress = MAX(progress, 95), "
                    "evidence = ?, updated_at = ? WHERE id = ?",
                    [json.dumps(ev), now_ms, tid],
                )
            except Exception:
                await _execute(
                    project_id,
                    "UPDATE tasks SET status = 'approved', progress = 95, "
                    "evidence = ?, updated_at = ? WHERE id = ?",
                    [json.dumps(ev), now_ms, tid],
                )
        else:
            await _execute(
                project_id,
                "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
                [json.dumps(ev), now_ms, tid],
            )

        # Rebuild merge obligation + proxy wake
        try:
            from hiveweave.services.obligation import ObligationLedger

            creator = task.get("creator_id") or task.get("reviewer_id")
            if creator:
                await ObligationLedger().create(
                    project_id,
                    str(creator),
                    "merge",
                    task_id=tid,
                    context={"reason": reason, "source": "close_blocked"},
                )
        except Exception as e:
            log.warning(
                "close_blocked_merge_obligation_failed",
                task_id=tid,
                error=str(e),
            )
        try:
            from hiveweave.services.merge_proxy import escalate_merge_proxy

            await escalate_merge_proxy(
                project_id, {**task, "id": tid, "status": "approved"},
                reason=f"close_blocked:{reason}",
            )
        except Exception as e:
            log.warning(
                "close_blocked_merge_proxy_failed",
                task_id=tid,
                error=str(e),
            )

    # Back-compat alias (tests / callers may still import the old name)
    async def _stamp_merge_status_on_close(
        self, project_id: str, task: dict
    ) -> None:
        await self._enforce_merge_on_close(project_id, task)

    # ── 清等待三件套（P2-5：并入触发方那次提交）──────────────
    #
    # 病：清等待（`UPDATE agent_waits SET cleared_at`）与触发侧的
    # 「task 状态写 + task_events outbox」分属两次提交 ⇒ 崩溃窗口可
    # **只落一边**（等待已清但唤醒事件没落 / 事件落了等待没清）。
    #
    # 落点纪律（**不要一刀切**）：全仓清等待写入点共 **11 处**
    # （`grep -rn "UPDATE agent_waits SET cleared_at" src/`）——10 处在
    # `services/wait_contract.py`，语义各异（等待方自清 / 被唤醒方 admit /
    # TTL / 破环 / 事实发布）；**只有 `_clear_task_wait_contracts` 这一处属
    # task 转换触发侧且原本在事务外**。只动这一处，其余 10 处保持原语义 ——
    # 一刀切会把语义不同的点同质化，正是本仓「同一语义两表」病灶的**反向形态**。
    #
    # 形态：进 tx 前读 waiter 行 → 把批量 UPDATE **追加进触发侧既有
    # `_execute_tx` 的 statements** → 提交后 `trigger_subordinate`。
    # 唤醒**必须**在提交之后：提交前唤醒一旦遇回滚，就造出「agent 被叫醒、
    # 等待却还在」的幻影唤醒。
    #
    # ⚠ **本改动的边界（别当成「触发侧全集已覆盖」）**：接的是 4 条已把
    # 「tasks UPDATE + task_events outbox」放进同一次 `_execute_tx` 的路径
    # （`transitions.py` 单步 else 支 / blocked 主支 / 多步支 + `archive_task`）。
    # 已知同形未接线点（独立议题，勿在本批顺手改）：
    # `services/org.py` dismiss_agent 的无父任务批量归档（同 tx 写
    # `status='cancelled'` + `task.archived`，不清等待）、
    # `tools/tasks/verify_spawn.py` 的 rehang（status 与事件分两次提交）。

    async def _pending_task_waiters(
        self, project_id: str, task_id: str
    ) -> list[dict]:
        """Un-cleared ``kind='task'`` wait rows (``id``/``agent_id``).

        ⚠ **ref 必须双形态匹配**：`agent_waits.ref` 存的是 commit_turn 的
        原始写法，历史上既有完整 task_id 也有 8 位短号（2026-09-21 取证：
        TEST_DSH_5x/6x + s3-clone 十个项目库里 `kind='task'` 的 2135 行 ref
        长度分布 = {36: 1918, 8: 214, 12: 7, 20: 1, 19: 1}，其中 214 条 8 位
        ref **全部**能对上某个真实 `tasks.id` 前缀）。裸 UUID 匹配是假阴性
        —— 与 09-01 已记录的「按 ref 查询必须带身份变体集合」同源。
        （残留：`wait_contract._short_circuit_satisfied_task_waits` 仍只按
        完整 id 匹配，属既有不一致面，另议。）

        Read failure ⇒ ``[]``（fail-open）：读不到只是退回「事务外清等待」
        的旧语义，绝不能让一次读失败阻断 task 状态转换本身。
        """
        try:
            rows = await _query(
                project_id,
                "SELECT id, agent_id FROM agent_waits "
                "WHERE kind = 'task' AND ref IN (?, ?) AND cleared_at IS NULL",
                [task_id, task_id[:8]],
            )
            return [dict(r) for r in rows]
        except Exception as e:  # noqa: BLE001 — 读失败不阻塞转换
            log.warning(
                "task_waiters_read_failed", task_id=task_id, error=str(e)
            )
            return []

    async def _wake_task_waiters(
        self,
        project_id: str,
        task_id: str,
        waiters: list[dict],
        *,
        in_tx: bool,
    ) -> None:
        """提交后唤醒被清等待的 agent，并发 `task_wait_contracts_cleared`。

        ``in_tx=True`` = 清等待与 task 状态写/outbox 同一次提交（P2-5 主路径）；
        ``in_tx=False`` = 事务外兜底路径（``_clear_task_wait_contracts``）——
        字段只用于取证区分，两条路径的语义同一。

        唤醒按 **agent 去重**：同一 agent 可能对同一 task 留有多条等待行
        （`_pending_task_waiters` 是批量形态），不去重会把它连叫多次。
        """
        rows = list(waiters or [])
        if not rows:
            return
        log.info(
            "task_wait_contracts_cleared",
            project_id=project_id,
            task_id=task_id,
            cleared=len(rows),
            cleared_in_tx=in_tx,
            agents=[str(r.get("agent_id") or "") for r in rows],
        )
        woken: set[str] = set()
        for row in rows:
            agent_id = str(row.get("agent_id") or "")
            if not agent_id or agent_id in woken:
                continue
            woken.add(agent_id)
            try:
                from hiveweave.agents.trigger import trigger_subordinate

                await trigger_subordinate(agent_id)
            except Exception as e:
                log.warning(
                    "task_wait_wake_trigger_failed",
                    agent_id=agent_id,
                    task_id=task_id,
                    error=str(e),
                )

    async def _durable_wake_stmts(
        self, task_id: str, waiters: list[dict], now_ms: int
    ) -> list[tuple[str, list]]:
        """把「durable 唤醒行」构造成可并入**触发侧事务**的语句列表（新-①）。

        单一实现：4 个投放点（`archive_task` / `_transition` / `_transition_multi` /
        `_clear_task_wait_contracts` 兜底）都调用它 —— 各写一遍必然漂移。
        按 **agent 去重**（同一 agent 可能对同一 task 留多条等待行，不去重会连叫多次）。
        """
        stmts: list[tuple[str, list]] = []
        for agent_id in sorted(
            {str(r.get("agent_id") or "") for r in waiters if r.get("agent_id")}
        ):
            stmt, _ts, _iid = build_inbox_wake_insert(
                agent_id,
                f"[等待解除] 任务 {task_id} 已进入终态或有新进展，你的等待已解除 —— "
                "不必再等，请转向其它任务。",
                task_id=task_id,
                now_ms=now_ms,
            )
            stmts.append(stmt)
        return stmts

    async def _clear_task_wait_contracts(
        self, project_id: str, task_id: str
    ) -> None:
        """TEST17 fix: clear agent_waits referencing a task on any transition.

        wake_on=["task_transition"] on agent_waits was dead code — no
        production code ever matched it. This method wires it up: when a
        task transitions, any agent waiting on that task (kind='task',
        ref=task_id) gets their wait cleared and is triggered to resume.

        Does NOT touch the trigger.py task_event filter (TEST3 busy-wait
        guard) — this is a targeted wake for explicit waiters only.

        P2-5 后本方法的**主路径已移入触发方事务**（见上方「清等待三件套」）
        ——转换点会在 `_execute_tx` 里先清一遍，方法在多数情况下读到 0 行。
        保留它作为**残余兜底**：覆盖「进 tx 前的读」与「提交」之间新建的
        等待行（窗口极小但非零），语义与旧版逐字一致。
        """
        try:
            waiters = await self._pending_task_waiters(project_id, task_id)
            if not waiters:
                return
            stmt = build_task_wait_clear_statement(
                int(time.time() * 1000), [r["id"] for r in waiters]
            )
            if stmt is None:
                return
            # 新-①：唤醒行与清等待**同一事务**（旧形态：清完再提交后才 trigger，
            # 崩在中间就只剩「等待已清、唤醒永不发生」）。
            await _execute_tx(
                project_id,
                [stmt, *await self._durable_wake_stmts(
                    task_id, waiters, int(time.time() * 1000)
                )],
            )
            await self._wake_task_waiters(
                project_id, task_id, waiters, in_tx=False
            )
        except Exception as e:
            log.warning(
                "clear_task_wait_contracts_failed",
                project_id=project_id,
                task_id=task_id,
                error=str(e),
            )

    async def _maybe_close_umbrella_parent(
        self, project_id: str, closed_child_id: str
    ) -> None:
        """Archive/close non-VERIFY parent when all sibling children are done.

        Dogfood: Phase 3 BUILD umbrella hung ~80min after children closed.
        VERIFY parents are handled by ``_close_verify_and_parent`` — skip them.
        """
        child = await self.get_task(project_id, closed_child_id)
        if not child:
            return
        if self._is_verify_task(child):
            return
        parent_id = child.get("parent_task_id")
        if not parent_id:
            return
        parent = await self.get_task(project_id, parent_id)
        if not parent or parent.get("is_archived"):
            return
        if self._is_verify_task(parent):
            return
        pst = parent.get("status")
        if pst in ("closed",):
            return
        tasks = await self.list_tasks(project_id)
        siblings = [
            t for t in tasks
            if t.get("parent_task_id") == parent_id
            and not self._is_verify_task(t)
            and not t.get("is_archived")
        ]
        if not siblings:
            return
        if not all(t.get("status") == "closed" for t in siblings):
            return
        # Do not close umbrella while a VERIFY child of the same parent is open
        open_verify = [
            t for t in tasks
            if t.get("parent_task_id") == parent_id
            and self._is_verify_task(t)
            and not t.get("is_archived")
            and t.get("status") not in ("closed",)
        ]
        if open_verify:
            return
        # Children done — archive running umbrella or close if already approved
        if pst in ("approved", "verifying"):
            await self.close_task(project_id, parent_id)
            log.info(
                "umbrella_parent_closed",
                parent_id=parent_id,
                via_child=closed_child_id[:8],
            )
        elif pst in ("running", "claimed", "submitted", "reviewing", "created"):
            await self.archive_task(
                project_id,
                parent_id,
                archived_by="system",
                reason="all child tasks closed — umbrella auto-archived",
                reason_code="umbrella_closed",
            )
            log.info(
                "umbrella_parent_archived",
                parent_id=parent_id,
                via_child=closed_child_id[:8],
            )

    async def archive_task(
        self,
        project_id: str,
        task_id: str,
        *,
        archived_by: str,
        reason: str,
        reason_code: str = "agent_cancel",
    ) -> str:
        """废弃任务（任意非 closed 状态 → archived）。coordinator 纠错通道。

        背景（井字棋实测 #5）：误绑的 task 卡在 claimed，状态机无出口
        （claimed 只能 →running/created），没有废弃路径 → 僵尸任务永远挂着，
        还会一直占据 assignee 的 obligations 导致 exit-gate 误判。

        archive 不走 _TRANSITIONS（它是生命周期外的纠偏操作，不是状态机的一环），
        但必须留审计痕迹：archived_by / archived_reason / archived_at。
        所有查询（list/obligations/stall）已过滤 is_archived=0，立即生效。

        Returns: 任务废弃前的状态。
        """
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("archive_task requires a non-empty reason (audit)")
        task_id = await self.require_task_id(project_id, task_id)
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            "SELECT status, is_archived FROM tasks WHERE id = ?", [task_id],
        )
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current, is_arch = rows[0]["status"], rows[0]["is_archived"]
        if is_arch:
            raise ValueError(f"Task {task_id[:8]} is already archived")
        if current == "closed":
            raise ValueError(
                f"Task {task_id[:8]} is already closed; archiving is a no-op. "
                "Closed tasks are the terminal success state."
            )
        now_ms = int(time.time() * 1000)
        code = (reason_code or "agent_cancel").strip() or "agent_cancel"
        arch_payload = json.dumps({
            "archived_by": archived_by,
            "reason": reason[:500],
            "reason_code": code,
            "detail": reason[:500],
        })
        (event_sql, event_params), event_ts, event_id = build_task_event_insert(
            project_id, task_id, "task.archived", current, "cancelled",
            actor_id=archived_by, payload=arch_payload, now_ms=now_ms,
        )
        # P2-5: 读 waiter 行，清等待并入下方同一次提交（提交后才唤醒）。
        waiters = await self._pending_task_waiters(project_id, task_id)
        wait_clear = build_task_wait_clear_statement(
            now_ms, [r["id"] for r in waiters]
        )
        archive_stmts: list[tuple[str, list]] = [
            # 根因修复：归档时同步置终态 status='cancelled'，避免
            # archived=1 但 status 停留在 verifying/submitted 等非终态
            # 导致数据矛盾（直接查 DB / task_events 审计 / 外部脚本困惑）
            # P2-3: reset progress to 0 — cancelled tasks must not retain
            # stale progress (e.g. 90 from submitted state).
            ("UPDATE tasks SET is_archived = 1, status = 'cancelled', "
            "progress = 0, "
            "archived_by = ?, archived_reason = ?, archived_at = ?, "
            "wake_at = NULL, updated_at = ? WHERE id = ?",
            [archived_by, reason[:500], now_ms, now_ms, task_id]),
            (event_sql, event_params),
        ]
        if wait_clear is not None:
            archive_stmts.append(wait_clear)
        # ⭐ 新-①（2026-09-21）：**唤醒也落成一行**，与状态写/事件/清等待同一次提交。
        # 旧形态只在提交后调 `trigger_subordinate()`（进程内调用）⇒ 崩在 COMMIT 与
        # 它之间就只剩「等待已清、唤醒永不发生」。⚠ 该行的 `wake_category` 必须与
        # `inbox.demote_wake_for_task` 的降级条件互斥，否则刚落就会被降成 wake=0
        #（2026-09-21 实测踩过：durable 唤醒形同虚设）。
        archive_stmts.extend(
            await self._durable_wake_stmts(task_id, waiters, now_ms)
        )
        await _execute_tx(project_id, archive_stmts)
        await publish_task_event(
            project_id, task_id, "task.archived", "cancelled", event_ts
        )
        # TEST_DSH_32 P1（closed 事件作废）：归档同理——残留的
        # [REWORK REQUESTED]/[TASK APPROVED] 等未读唤醒降级为背景
        # （「其实不用补交」却反复被叫醒的空转根因之一）。
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().demote_wake_for_task(
                project_id, task_id, reason="task_archived"
            )
        except Exception as e:
            log.debug(
                "archive_demote_inbox_wake_failed",
                task_id=task_id,
                error=str(e),
            )

        # 40 轮待办 #3（qwen 补充，reports 工件生命周期）：取消/归档任务时，
        # 在 MAIN 的 .hiveweave/reports/<task_id>/ 落一个 CANCELLED 标记——
        # 读取通道（40 轮 P0-1 已放开）的读者能看到生命周期状态，不会把
        # 取消单的预研/取证当仍然有效的验收依据。best-effort，绝不阻塞。
        try:
            from hiveweave.db import meta as _meta_db

            _ws = await _meta_db.get_project_workspace(project_id)
            if _ws:
                _rdir = Path(_ws) / ".hiveweave" / "reports" / task_id
                if _rdir.is_dir():
                    _marker = _rdir / "CANCELLED.md"
                    if not _marker.exists():
                        _marker.write_text(
                            f"# 任务已取消/归档\n\n"
                            f"- task_id: {task_id}\n"
                            f"- cancelled_at: {now_ms}\n"
                            f"- reason_code: {code}\n"
                            f"- reason: {reason[:300]}\n\n"
                            f"本目录下的取证/预研材料属已取消任务，"
                            f"不作为有效验收依据。\n",
                            encoding="utf-8",
                        )
                        log.info(
                            "reports_lifecycle_marker_written",
                            task_id=task_id[:12],
                        )
        except Exception as e:  # noqa: BLE001 — 标记失败不阻塞归档
            log.debug("reports_lifecycle_marker_failed", error=str(e))
        log.info(
            "task_archived",
            project_id=project_id,
            task_id=task_id,
            from_status=current,
            archived_by=archived_by,
            reason=reason[:120],
            reason_code=code,
        )

        # B2: VERIFY 归档时级联父任务 —— 如果归档的是 VERIFY 子任务，
        # 其父任务可能卡在 verifying 状态无法前进。回退到 approved，
        # 让 CEO/coordinator 可以重新走 merge+VERIFY 流程或直接 close。
        # （archive_task 在 current=="closed" 时已 raise，此处 current 必非 closed）
        archived_task = await self.get_task(project_id, task_id)
        if archived_task and self._is_verify_task(archived_task):
            # TEST13 P1-3: cascade close verification_case
            try:
                await VerificationCaseService().mark_cancelled(
                    project_id,
                    task_id,
                    reason=f"VERIFY archived: {reason[:200]}",
                )
            except Exception as e:
                log.warning(
                    "verify_case_cancel_on_archive_failed",
                    task_id=task_id,
                    error=str(e),
                )
            parent_id = archived_task.get("parent_task_id")
            if parent_id:
                parent_rows = await _query(
                    project_id,
                    "SELECT status FROM tasks WHERE id = ?",
                    [parent_id],
                )
                if parent_rows and parent_rows[0]["status"] == "verifying":
                    try:
                        await self._transition(project_id, parent_id, "approved")
                        log.info(
                            "verify_archived_parent_reverted",
                            project_id=project_id,
                            verify_task_id=task_id,
                            parent_task_id=parent_id,
                            from_status="verifying",
                            to_status="approved",
                        )
                    except Exception as e:
                        log.warning(
                            "verify_archived_parent_revert_failed",
                            parent_task_id=parent_id,
                            error=str(e),
                        )

        # L3: wake waiters already cleared inside the transaction above,
        # then run the outside-tx residual net (waiters created mid-window).
        await self._wake_task_waiters(project_id, task_id, waiters, in_tx=True)
        await self._clear_task_wait_contracts(project_id, task_id)

        # L1: detect reverse dependents — tasks whose depends_on contains
        # this task will have a dangling reference (cancelled ∉ completed).
        # Log warning + cancel obligations so downstream doesn't silently hang.
        try:
            dependents = await _query(
                project_id,
                "SELECT id, title, status FROM tasks "
                "WHERE is_archived = 0 AND status NOT IN ('closed', 'cancelled') "
                "AND depends_on IS NOT NULL AND depends_on != '[]'",
            )
            dangling = []
            for dep in dependents:
                deps_raw = dep.get("depends_on") or "[]"
                try:
                    deps_list = json.loads(deps_raw) if isinstance(deps_raw, str) else deps_raw
                except (json.JSONDecodeError, TypeError):
                    deps_list = []
                if task_id in (deps_list or []):
                    dangling.append(dep)
            if dangling:
                log.warning(
                    "task_archived_dangling_dependents",
                    project_id=project_id,
                    task_id=task_id,
                    dependents=[d["id"] for d in dangling],
                )
        except Exception as e:
            log.warning("archive_reverse_dep_check_failed", error=str(e))

        # Cancel pending obligations for this task
        try:
            from hiveweave.services.obligation import ObligationLedger

            await ObligationLedger().cancel_for_task(project_id, task_id)
        except Exception:
            pass

        # 审计 #9: 归档同理——回收任务名下悬空的 open staffing demand。
        try:
            from hiveweave.services.staffing import staffing_demand_service

            await staffing_demand_service.cancel_open_demands_for_task(
                project_id, task_id, reason=f"task_archived: {code}"
            )
        except Exception as e:
            log.warning(
                "staffing_demand_cancel_on_archive_failed",
                task_id=task_id,
                error=str(e),
            )

        # TEST19 ③: 归档后立即向 assignee + creator 推送恢复指引。
        # task_event_relay 对 task.archived 跳过（避免 tick 抢跑占
        # idempotency key，把详指引挤掉）。key 仍用 task_event:… 以便
        # 旧 relay 路径若曾投递过仍幂等。
        try:
            await self._notify_archived_with_guidance(
                project_id,
                task_id,
                event_id=event_id,
                archived_by=archived_by,
                reason=reason,
                reason_code=code,
            )
        except Exception as e:
            log.warning(
                "archive_notify_guidance_failed",
                task_id=task_id[:12],
                error=str(e),
            )

        # 验收串行化（issue #6）：VERIFY 归档（含 coordinator `cancel` 路径）
        # 即释放 MAIN 运行时独占，立即泵出队列中下一个 created VERIFY，
        # 不必等 game_time tick。仅 VERIFY 归档才有释放语义（审计 O1）。best-effort。
        if archived_task and self._is_verify_task(archived_task):
            try:
                from hiveweave.tools.tasks.verify_spawn import (
                    nudge_pending_verify_tasks,
                )

                pumped = await nudge_pending_verify_tasks(project_id)
                if pumped:
                    log.info(
                        "verify_pending_pumped_after_archive",
                        verify_task_id=task_id,
                        pumped=pumped,
                    )
            except Exception as e:
                log.warning(
                    "verify_pending_pump_after_archive_failed",
                    verify_task_id=task_id,
                    error=str(e),
                )

        return current

    async def _notify_archived_with_guidance(
        self,
        project_id: str,
        task_id: str,
        *,
        event_id: str,
        archived_by: str,
        reason: str,
        reason_code: str,
    ) -> None:
        """TEST19 ③: push recovery guidance to assignee + creator on archive.

        The task is already archived when this runs — inbox force-wake does
        not apply to archived tasks, so recipients see it next time they
        wake naturally (FYI semantics, same as the relay).
        """
        rows = await _query(
            project_id,
            "SELECT assignee_id, creator_id, title FROM tasks WHERE id = ?",
            [task_id],
        )
        if not rows:
            return
        row = rows[0]
        assignee = row["assignee_id"] if "assignee_id" in row.keys() else None
        creator = row["creator_id"] if "creator_id" in row.keys() else None
        title = (row["title"] or "")[:80] if "title" in row.keys() else ""
        short_id = task_id[:8]

        recipients = []
        for r in (assignee, creator):
            if r and r != archived_by and r not in recipients:
                recipients.append(r)
        if not recipients:
            return

        reason_short = (reason or "")[:200] or "no reason given"
        guidance_by_code = {
            "duplicate_cleanup": (
                "此任务与已成功的验证任务重复，被系统自动清扫归档。"
                "无需恢复；如仍有未覆盖的验证面，请重新创建任务并说明差异。"
            ),
            "umbrella_closed": (
                "所有子任务已完成，汇总（umbrella）任务自动归档收口。"
                "无需恢复；如需新的汇总层，请另行创建任务。"
            ),
            "agent_cancel": (
                "任务被取消归档。若工作仍需继续：用 create_task 重新创建，"
                "并在描述中注明原任务 shortId 与归档原因，以便 context 延续。"
            ),
        }
        guidance = guidance_by_code.get(
            reason_code,
            "若工作仍需继续：用 create_task 重新创建，并在描述中注明原任务 "
            f"shortId（{short_id}）与归档原因。",
        )
        message = (
            f"[TASK ARCHIVED] {title} ({short_id}) was archived by "
            f"{archived_by}. Reason: {reason_short}。"
            f" 恢复指引：任务已废弃（不会进入义务/看门狗/统计）。{guidance}"
        )

        from hiveweave.services.inbox import InboxService

        inbox = InboxService()
        for recipient_id in recipients:
            try:
                await inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=recipient_id,
                    message=message,
                    message_type="task_event",
                    priority="normal",
                    task_id=task_id,
                    idempotency_key=f"task_event:{event_id}:{recipient_id}",
                    wake=False,
                )
            except Exception as e:
                log.debug(
                    "archive_guidance_send_skipped",
                    recipient=recipient_id[:12],
                    error=str(e),
                )

