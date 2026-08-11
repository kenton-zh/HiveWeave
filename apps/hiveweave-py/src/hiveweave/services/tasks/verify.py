"""VERIFY lifecycle + VerificationCaseService."""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query

log = structlog.get_logger(__name__)

# API 人类创建任务的 creator 哨兵（api/tasks.py）—— 不是 agent，发给它
# inbox/义务无意义，必须 fallback 到真实 agent。
_HUMAN_CREATOR_SENTINELS = frozenset({"user", "用户", "human"})

# 2026-08-11 B4 事故：approved→merge 是正常窗口（VERIFY 只在 merge 后
# spawn），migrate_orphan_approved 在此窗口内不得判定孤儿。
ORPHAN_APPROVED_GRACE_MS = 10 * 60 * 1000


def resolve_merge_owner(task: dict, fallback: str | None) -> str | None:
    """Merge 职责方：任务 creator（排除 API 人类哨兵）→ fallback（同排除）。

    2026-08-11 slack-clone_01 复盘：merge 义务/提醒/清理三处接收者曾分别
    写死（reviewer/creator/creator），代审场景互相打错。统一入口：wake
    接收者、obligation owner、merge 后清理目标都用本函数取同一人。
    fallback 同样排除哨兵（API 任务 submit 时 reviewer 默认 = creator，
    可能也是 "user"）—— 全哨兵链返回 None，调用方兜底到真实 agent。
    """
    creator = task.get("creator_id")
    if creator and creator not in _HUMAN_CREATOR_SENTINELS:
        return creator
    if fallback and fallback not in _HUMAN_CREATOR_SENTINELS:
        return fallback
    return None


class VerifyMixin:
    """mark_verifying / close verify parent / migrate orphan approved."""

    if TYPE_CHECKING:
        _transition: Any
        emit_task_event: Any
        close_task: Any
        get_task: Any
        list_tasks: Any
        archive_task: Any
        _COLUMNS: Any
        _row: Any

    async def mark_verifying(
        self,
        project_id: str,
        task_id: str,
        *,
        reason_code: str | None = None,
    ) -> None:
        """Parent task enters verifying after VERIFY child is spawned."""
        rows = await _query(
            project_id,
            "SELECT status, creator_id, reviewer_id FROM tasks WHERE id = ?",
            [task_id],
        )
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current = rows[0]["status"]
        owner = resolve_merge_owner(
            {"creator_id": rows[0]["creator_id"]}, rows[0]["reviewer_id"]
        )
        if current == "verifying":
            await self._clear_merge_pending_inbox(task_id, owner)
            return
        if current == "approved":
            await self._transition(
                project_id,
                task_id,
                "verifying",
                reason_code=reason_code,
            )
            await self.emit_task_event(
                project_id,
                task_id,
                "verifying",
                summary=f"[verifying] task {task_id[:8]}",
            )
            await self._clear_merge_pending_inbox(task_id, owner)
            return
        if current == "closed":
            await self._clear_merge_pending_inbox(task_id, owner)
            return
        raise ValueError(f"Cannot mark verifying from status={current}")

    async def _clear_merge_pending_inbox(
        self, task_id: str, owner_id: str | None
    ) -> None:
        """Mark stale [MERGE PENDING] for this task as read (merge already done)."""
        if not owner_id or not task_id:
            return
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().supersede_watchdog_messages(
                owner_id,
                prefixes=["[MERGE PENDING]", "[MERGE PROXY]"],
                contains=task_id[:8],
            )
        except Exception as e:
            log.warning(
                "clear_merge_pending_failed",
                task_id=task_id,
                owner_id=owner_id,
                error=str(e),
            )

    @staticmethod
    def _is_verify_task(task: dict) -> bool:
        """True only for system VERIFY tasks (title ``VERIFY:`` prefix).

        TEST19 教训: agent 自由 tag ``verify`` 不再参与判定 —— 它曾被
        磐石用来标记普通模块验证任务, 使 14+ 处 VERIFY 特殊逻辑（隔离门/
        强制 main/sibling 清扫/claim 行为等）误伤普通实施任务。系统
        spawn（verify_spawn.py）创建的 VERIFY 任务标题始终带 ``VERIFY:``
        前缀, 收敛为单通道不丢失系统任务。
        """
        title = task.get("title") or ""
        return isinstance(title, str) and title.startswith("VERIFY:")

    @staticmethod
    def _verify_title_key(title: str | None) -> str:
        """归一化 VERIFY 标题用于判重：去前缀/括号块/空白。

        'VERIFY: 项9 演练（归零 CEO 配合）' 与
        'VERIFY: 项9 演练（重建）' 归一到 '项9 演练' —— 同目标。
        """
        t = (title or "").strip()
        if t.startswith("VERIFY:"):
            t = t[len("VERIFY:"):].strip()
        # 去括号块（中文/英文）
        t = re.sub(r"[（(][^（）()]*[）)]", "", t)
        return re.sub(r"\s+", " ", t).strip()

    async def _close_verify_and_parent(
        self, project_id: str, verify_task: dict
    ) -> None:
        """Close VERIFY child and its parent (approved|verifying → closed).

        Also archives/closes sibling open VERIFY tasks for the same parent
        (system + manual duplicates left behind after one VERIFY succeeds).
        """
        verify_id = verify_task.get("id")
        if not verify_id:
            return
        # Close VERIFY itself (approved → closed)
        try:
            await self.close_task(project_id, verify_id)
        except Exception as e:
            log.warning(
                "verify_child_close_failed",
                task_id=verify_id,
                error=str(e),
            )
            return

        # Mark verification case as passed (carry review feedback + merge hash)
        try:
            vcs = VerificationCaseService()
            notes = ""
            merge_hash = None
            ev = verify_task.get("evidence") or {}
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except Exception:
                    ev = {}
            if isinstance(ev, dict):
                notes = str(
                    ev.get("review_feedback") or ev.get("summary") or ""
                )
                merge_hash = (
                    ev.get("merge_commit")
                    or ev.get("merge_commit_hash")
                    or ev.get("commit")
                )
            # Prefer parent evidence merge hash if child lacks it
            parent_id = verify_task.get("parent_task_id")
            if not merge_hash and parent_id:
                parent = await self.get_task(project_id, parent_id)
                pev = (parent or {}).get("evidence") or {}
                if isinstance(pev, str):
                    try:
                        pev = json.loads(pev)
                    except Exception:
                        pev = {}
                if isinstance(pev, dict):
                    merge_hash = (
                        pev.get("merge_commit")
                        or pev.get("merge_commit_hash")
                        or pev.get("commit")
                    )
            await vcs.mark_passed(
                project_id,
                verify_id,
                notes=notes,
                merge_commit_hash=str(merge_hash) if merge_hash else None,
            )
            # Mirror case onto VERIFY evidence so get_tasks / CEO reports see it
            try:
                case = await vcs.get_case_for_task(project_id, verify_id)
                if case:
                    ev2 = dict(ev) if isinstance(ev, dict) else {}
                    ev2["verification_case"] = {
                        "id": (case.get("id") or "")[:8],
                        "status": case.get("status"),
                        "merge_commit_hash": case.get("merge_commit_hash"),
                        "review_notes": (case.get("review_notes") or "")[:300],
                        "qa_agent_id": case.get("qa_agent_id"),
                    }
                    await _execute(
                        project_id,
                        "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
                        [json.dumps(ev2), int(time.time() * 1000), verify_id],
                    )
            except Exception as e:
                log.warning(
                    "verification_case_mirror_failed",
                    verify_id=verify_id,
                    error=str(e),
                )
        except Exception:
            pass  # best-effort

        parent_id = verify_task.get("parent_task_id")
        if parent_id:
            await self._close_sibling_verify_tasks(
                project_id, parent_id, except_id=verify_id
            )
        # 验收串行化（issue #6）：前置 VERIFY 收口即释放 MAIN 运行时独占，唤醒
        # 队列中下一个 created VERIFY。best-effort；game_time tick 兜底。
        try:
            from hiveweave.tools.tasks.verify_spawn import (
                nudge_pending_verify_tasks,
            )

            pumped = await nudge_pending_verify_tasks(project_id)
            if pumped:
                log.info(
                    "verify_pending_pumped_after_close",
                    verify_id=verify_id,
                    pumped=pumped,
                )
        except Exception as e:
            log.warning(
                "verify_pending_pump_failed",
                verify_id=verify_id,
                error=str(e),
            )

        if not parent_id:
            # Infer: title "VERIFY: <parent title>" + same assignee
            return
        parent = await self.get_task(project_id, parent_id)
        if not parent:
            return
        status = parent.get("status")
        if status in ("approved", "verifying"):
            try:
                await self.close_task(project_id, parent_id)
                log.info(
                    "verify_parent_closed",
                    verify_id=verify_id,
                    parent_id=parent_id,
                )
            except Exception as e:
                # 2026-08-11 意见核实：此前双重吞没（此处 + 上游 review.py
                # try/except），父任务可能永挂 approved/verifying 且无人知晓
                # （migrate 只扫 approved，verifying 无自动重试）。至少：
                # 事件落账 + 通知父任务 creator（能解卡的人）。
                log.warning(
                    "verify_parent_close_failed",
                    verify_id=verify_id,
                    parent_id=parent_id,
                    error=str(e),
                )
                try:
                    from hiveweave.services.tasks.db import insert_task_event

                    await insert_task_event(
                        project_id,
                        parent_id,
                        "verify_parent_close_failed",
                        "verifying",
                        "verifying",
                        actor_id="system",
                        payload={
                            "verify_id": str(verify_id)[:8],
                            "error": str(e)[:200],
                        },
                    )
                except Exception:
                    pass
                try:
                    from hiveweave.agents.trigger import trigger_coordinator
                    from hiveweave.services.inbox import InboxService

                    creator = parent.get("creator_id")
                    if creator:
                        await InboxService().send_message(
                            from_agent_id="system",
                            to_agent_id=creator,
                            message=(
                                f"[VERIFY PARENT CLOSE FAILED] Task "
                                f"'{str(parent_id)[:8]}' VERIFY passed but "
                                f"close blocked: {str(e)[:200]} — merge the "
                                f"branch or waive_merge, then resolve."
                            ),
                            message_type="task",
                            priority="urgent",
                            task_id=parent_id,
                            wake=True,
                        )
                        await trigger_coordinator(creator)
                except Exception:
                    pass

    async def _close_sibling_verify_tasks(
        self,
        project_id: str,
        parent_id: str,
        *,
        except_id: str | None = None,
    ) -> int:
        """Close/archive duplicate VERIFY children of the same parent.

        只清理「真重复」：同为 *系统 spawn* 的 VERIFY: 任务（title 前缀
        VERIFY:，tags 含 verify 的普通验证实施任务 **不是**）且标题归一化后
        与本次收口的 VERIFY 相同。执行中/已提交/审查中（running/submitted/
        reviewing/verifying/approved/blocked）一律跳过——TEST19 教训：
        归零 approve 模块A 时把正在跑模块D 的普通验证任务（tags=verify）
        当重复清扫，导致验证工作被系统重建 2 轮。
        """
        await _ensure_schema(project_id)
        except_task = (
            await self.get_task(project_id, except_id) if except_id else None
        )
        except_title = (except_task or {}).get("title") or ""
        # 只有「系统 spawn 的 VERIFY: 任务」收口才触发清扫——普通
        # tags=verify 实施任务 approve 不派生清扫权（TEST19 教训）
        if not (isinstance(except_title, str) and except_title.startswith("VERIFY:")):
            return 0
        except_key = self._verify_title_key(except_title)
        tasks = await self.list_tasks(project_id)
        closed = 0
        for t in tasks:
            tid = t.get("id")
            if not tid or tid == except_id:
                continue
            if t.get("parent_task_id") != parent_id:
                continue
            title = t.get("title") or ""
            # 只认系统 VERIFY: 前缀（tags 含 verify 的普通任务不是重复）
            if not (isinstance(title, str) and title.startswith("VERIFY:")):
                continue
            if self._verify_title_key(title) != except_key:
                log.info(
                    "verify_sibling_skipped_different_target",
                    task_id=tid,
                    parent_id=parent_id,
                    title=title[:60],
                )
                continue
            st = t.get("status")
            if st == "closed":
                continue
            if st in ("running", "submitted", "reviewing", "verifying",
                      "approved", "blocked", "rework"):
                # 执行中/审查中的重复 VERIFY 不自动杀——留给协调者裁决
                log.warning(
                    "verify_sibling_skipped_in_flight",
                    task_id=tid,
                    parent_id=parent_id,
                    status=st,
                    title=title[:60],
                )
                continue
            try:
                # 仅剩 created/claimed 等未开始状态：确认重复后归档
                await self.archive_task(
                    project_id,
                    tid,
                    archived_by="system",
                    reason=(
                        f"duplicate VERIFY closed; sibling {except_id[:8]} "  # type: ignore[index]
                        "succeeded, inactive duplicate cleaned up"
                    ),
                    reason_code="duplicate_cleanup",
                )
                closed += 1
            except Exception as e:
                log.warning(
                    "verify_sibling_cleanup_failed",
                    task_id=tid,
                    parent_id=parent_id,
                    error=str(e),
                )
        if closed:
            log.info(
                "verify_siblings_cleaned",
                parent_id=parent_id,
                closed=closed,
            )
        return closed

    async def migrate_orphan_approved(self, project_id: str) -> dict:
        """One-shot: approved with open VERIFY → verifying; else → closed.

        2026-08-11 slack-clone_01 B4 事故：approved 无 VERIFY 子是
        approve→merge 的**正常窗口**（VERIFY 只在 merge 后 spawn，
        verify_spawn docstring），不是孤儿态。migrate 曾无门槛地在
        merge/hire/启动热路径上把刚 approved 的任务直接 close
        （skip_merge_gate=True，不检查分支）→ B4 被 husk 阻塞 merge 时
        被静默关闭，代码后续合入 main 但独立验收永久缺失。门槛：
        - 宽限期 ``ORPHAN_APPROVED_GRACE_MS`` 内不判定（merger 响应时间）；
        - 有 pending merge obligation 不 close（merge 流程进行中）。
        """
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE is_archived = 0 AND status = 'approved'",
        )
        to_verifying = 0
        to_closed = 0
        for r in rows:
            task = self._row(r)
            if self._is_verify_task(task):
                # Orphan approved VERIFY → close (and parent if any)
                await self._close_verify_and_parent(project_id, task)
                to_closed += 1
                continue
            tid = task["id"]
            updated = int(task.get("updated_at") or task.get("created_at") or 0)
            if now_ms - updated < ORPHAN_APPROVED_GRACE_MS:
                # approve→merge 正常窗口内：不判定孤儿（B4 事故修复）
                continue
            try:
                pending = await _query(
                    project_id,
                    "SELECT count(*) AS c FROM obligations "
                    "WHERE task_id = ? AND obligation_type = 'merge' "
                    "AND status = 'pending'",
                    [tid],
                )
                if pending and int(pending[0]["c"] or 0) > 0:
                    # merge 流程进行中（obligation 未 fulfill）——不 close
                    continue
            except Exception:
                pass  # 表缺失等：不阻塞判定（保守继续）
            children = await _query(
                project_id,
                f"SELECT {self._COLUMNS} FROM tasks "
                "WHERE parent_task_id = ? AND is_archived = 0",
                [tid],
            )
            has_open_verify = False
            for ch in children:
                child = self._row(ch)
                if self._is_verify_task(child) and child.get("status") not in (
                    "closed",
                ):
                    has_open_verify = True
                    break
            if has_open_verify:
                await self.mark_verifying(
                    project_id,
                    tid,
                    reason_code="orphan_approved_migrate",
                )
                to_verifying += 1
            else:
                # Ledger hygiene — no VERIFY child means orphan approved;
                # skip merge gate (no worktree/merge fact expected).
                await self.close_task(
                    project_id,
                    tid,
                    skip_merge_gate=True,
                    reason_code="orphan_approved_migrate",
                )
                to_closed += 1
        return {"verifying": to_verifying, "closed": to_closed}


class VerificationCaseService:
    """Single authoritative entity for the VERIFY lifecycle.

    Links original_task → verify_task → merger → QA reviewer.
    Status: pending → in_review → passed | failed | waived
    """

    async def create_case(
        self,
        project_id: str,
        original_task_id: str,
        verify_task_id: str | None = None,
        merger_agent_id: str | None = None,
    ) -> str | None:
        """Create a verification case when VERIFY is spawned."""
        case_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "INSERT INTO verification_cases "
                "(id, project_id, original_task_id, verify_task_id, "
                "merger_agent_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                [case_id, project_id, original_task_id, verify_task_id,
                 merger_agent_id, now_ms, now_ms],
            )
            log.info("verification_case_created", case_id=case_id,
                     original_task=original_task_id[:8],
                     verify_task=(verify_task_id or "")[:8])
            return case_id
        except Exception as e:
            log.warning("verification_case_create_failed", error=str(e))
            return None

    async def update_verify_task(
        self, project_id: str, original_task_id: str,
        verify_task_id: str,
    ) -> None:
        """Link the verify task to an existing case."""
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "UPDATE verification_cases SET verify_task_id = ?, "
                "status = 'in_review', updated_at = ? "
                "WHERE original_task_id = ? AND status = 'pending'",
                [verify_task_id, now_ms, original_task_id],
            )
        except Exception as e:
            log.warning("verification_case_update_verify_failed", error=str(e))

    async def set_reviewer(
        self, project_id: str, verify_task_id: str,
        qa_agent_id: str,
    ) -> None:
        """Set the QA reviewer for a verification case."""
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "UPDATE verification_cases SET qa_agent_id = ?, "
                "status = 'in_review', updated_at = ? "
                "WHERE verify_task_id = ?",
                [qa_agent_id, now_ms, verify_task_id],
            )
        except Exception as e:
            log.warning("verification_case_set_reviewer_failed", error=str(e))

    async def mark_passed(
        self, project_id: str, verify_task_id: str,
        notes: str = "",
        merge_commit_hash: str | None = None,
    ) -> None:
        """Mark verification as passed (VERIFY approved)."""
        now_ms = int(time.time() * 1000)
        try:
            if merge_commit_hash:
                await _execute(project_id,
                    "UPDATE verification_cases SET status = 'passed', "
                    "review_notes = ?, merge_commit_hash = ?, "
                    "closed_at = ?, updated_at = ? "
                    "WHERE verify_task_id = ?",
                    [notes[:500], merge_commit_hash[:64], now_ms, now_ms,
                     verify_task_id],
                )
            else:
                await _execute(project_id,
                    "UPDATE verification_cases SET status = 'passed', "
                    "review_notes = ?, closed_at = ?, updated_at = ? "
                    "WHERE verify_task_id = ?",
                    [notes[:500], now_ms, now_ms, verify_task_id],
                )
        except Exception as e:
            log.warning("verification_case_mark_passed_failed", error=str(e))

    async def set_merge_commit(
        self, project_id: str, original_task_id: str,
        merge_commit_hash: str,
    ) -> None:
        """Persist merge commit on the case when parent is merged."""
        if not merge_commit_hash:
            return
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "UPDATE verification_cases SET merge_commit_hash = ?, "
                "updated_at = ? "
                "WHERE original_task_id = ? AND "
                "(merge_commit_hash IS NULL OR merge_commit_hash = '')",
                [merge_commit_hash[:64], now_ms, original_task_id],
            )
        except Exception as e:
            log.warning("verification_case_set_merge_failed", error=str(e))

    async def mark_failed(
        self, project_id: str, verify_task_id: str,
        notes: str = "",
    ) -> None:
        """Mark verification as failed (VERIFY rejected/rework)."""
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "UPDATE verification_cases SET status = 'failed', "
                "review_notes = ?, updated_at = ? "
                "WHERE verify_task_id = ?",
                [notes[:500], now_ms, verify_task_id],
            )
        except Exception as e:
            log.warning("verification_case_mark_failed_failed", error=str(e))

    async def mark_cancelled(
        self, project_id: str, verify_task_id: str,
        reason: str = "",
    ) -> None:
        """Close verification case when VERIFY task is cancelled/archived."""
        now_ms = int(time.time() * 1000)
        note = (reason or "VERIFY task cancelled")[:500]
        try:
            await _execute(project_id,
                "UPDATE verification_cases SET status = 'cancelled', "
                "review_notes = ?, closed_at = ?, updated_at = ? "
                "WHERE verify_task_id = ? AND status NOT IN "
                "('passed', 'cancelled', 'waived')",
                [note, now_ms, now_ms, verify_task_id],
            )
        except Exception as e:
            log.warning("verification_case_mark_cancelled_failed", error=str(e))

    async def reconcile_orphans(self, project_id: str) -> int:
        """Close cases whose verify_task is terminal but case still open.

        Returns number of rows fixed.
        """
        try:
            rows = await _query(
                project_id,
                "SELECT vc.id, vc.verify_task_id, t.status AS tstatus, "
                "t.is_archived "
                "FROM verification_cases vc "
                "LEFT JOIN tasks t ON t.id = vc.verify_task_id "
                "WHERE vc.project_id = ? AND vc.status IN "
                "('pending', 'in_review')",
                [project_id],
            )
        except Exception:
            return 0
        fixed = 0
        now_ms = int(time.time() * 1000)
        for r in rows:
            vid = r.get("verify_task_id")
            tstatus = r.get("tstatus")
            archived = r.get("is_archived")
            if not vid:
                continue
            if archived or tstatus in ("cancelled", "closed"):
                try:
                    await _execute(
                        project_id,
                        "UPDATE verification_cases SET status = 'cancelled', "
                        "review_notes = COALESCE(NULLIF(review_notes,''), "
                        "'reconcile: verify task terminal'), "
                        "closed_at = ?, updated_at = ? WHERE id = ?",
                        [now_ms, now_ms, r["id"]],
                    )
                    fixed += 1
                except Exception:
                    pass
        if fixed:
            log.info(
                "verification_cases_reconciled",
                project_id=project_id,
                fixed=fixed,
            )
        return fixed

    async def mark_waived(
        self, project_id: str, original_task_id: str,
        reason: str = "",
        *,
        verify_task_id: str | None = None,
    ) -> None:
        """Mark verification as waived (attestation waiver).

        Matches by original_task_id and optionally verify_task_id so a waive
        on the VERIFY child still stamps the case.
        """
        now_ms = int(time.time() * 1000)
        note = (reason or "")[:500]
        if note and not note.lower().startswith("waived:"):
            note = f"WAIVED: {note}"
        try:
            # Prefer verify_task_id match when provided
            if verify_task_id:
                await _execute(project_id,
                    "UPDATE verification_cases SET status = 'waived', "
                    "review_notes = ?, closed_at = ?, updated_at = ? "
                    "WHERE verify_task_id = ? AND status NOT IN ('passed')",
                    [note, now_ms, now_ms, verify_task_id],
                )
            await _execute(project_id,
                "UPDATE verification_cases SET status = 'waived', "
                "review_notes = ?, closed_at = ?, updated_at = ? "
                "WHERE original_task_id = ? AND status NOT IN ('passed', 'waived')",
                [note, now_ms, now_ms, original_task_id],
            )
        except Exception as e:
            log.warning("verification_case_mark_waived_failed", error=str(e))

    async def ensure_case(
        self,
        project_id: str,
        *,
        original_task_id: str,
        verify_task_id: str | None = None,
        merger_agent_id: str | None = None,
    ) -> str | None:
        """Return existing case id or create one (idempotent for waive/approve)."""
        existing = await self.get_case_for_task(
            project_id, verify_task_id or original_task_id
        )
        if existing:
            return existing.get("id")
        return await self.create_case(
            project_id,
            original_task_id=original_task_id,
            verify_task_id=verify_task_id,
            merger_agent_id=merger_agent_id,
        )

    async def get_case_for_task(
        self, project_id: str, task_id: str
    ) -> dict | None:
        """Get verification case by original or verify task ID."""
        try:
            rows = await _query(project_id,
                "SELECT * FROM verification_cases "
                "WHERE original_task_id = ? OR verify_task_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                [task_id, task_id],
            )
            return dict(rows[0]) if rows else None
        except Exception:
            return None

    async def list_cases_for_project(
        self, project_id: str, limit: int = 30
    ) -> list[dict]:
        """Recent verification cases for platform_state / get_tasks."""
        try:
            rows = await _query(
                project_id,
                "SELECT * FROM verification_cases "
                "WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                [project_id, limit],
            )
            return [dict(r) for r in rows]
        except Exception:
            return []
