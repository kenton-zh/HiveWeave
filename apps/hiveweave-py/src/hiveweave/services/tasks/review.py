"""Review start / decide helpers."""
from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query
from .verify import VerificationCaseService, normalize_verdict  # noqa: F401

log = structlog.get_logger(__name__)


class ReworkPrescriptionAbsent(ValueError):
    """返修决定缺少可执行处方 —— **状态未变更**。

    DSH 判据（``packages/AGENTS.md:14``「**Enforce a decision in the operation
    that makes it**」）：门禁必须住在做那个决定的操作里；schema 省略、prompt
    过滤、facade、wrapper、listener 顺序**都不算 enforcement**，因为直接或
    旁路调用方都能绕开。

    这正是 fixlist #3 的病：处方门禁原本只在 tool 层的 ``decision == "rework"``
    分支上，而 ``decision='approve'`` 被证据闸强制转 rework 的路径**整条绕开
    它**（实测：rework 被拒 → 7~21 秒后改用 approve 重投 → 通过 → 被 verdict
    闸改回 rework，净状态恰是门禁要防的「无处方的返修」）。

    因此判定落在 :meth:`_force_rework` —— 两条返修路径的唯一汇聚点，且是
    状态变更**之前**。缺处方时不落 rework，由调用方补齐后重试。
    """

    def __init__(self, problem: str) -> None:
        self.problem = problem
        super().__init__(f"rework_prescription_absent:{problem}")


# 系统（证据闸）发起的返修 code —— 这类返修的处方由**平台**提供，不由
# reviewer 撰写：code = 下面的 reason_code，message = evidence.blocking_issues。
# 两者合起来就是 DSH `GoalBlockReason`（受限 code + 非空 message）的同构形态。
SYSTEM_REWORK_REASON_CODES = frozenset({
    "verdict_fail_rework",
    "integrity_check_fail",
})


class ReviewMixin:
    """start_review / review_task."""

    if TYPE_CHECKING:
        require_task_id: Any
        _transition: Any
        get_task: Any
        _persist_contract_json: Any
        _wake_dependent_tasks: Any
        _is_verify_task: Any
        _close_verify_and_parent: Any
        emit_task_event: Any
        _transition_multi: Any

    async def start_review(self, project_id: str, task_id: str,
                           reviewer_id: str | None = None) -> None:
        """Start review (submitted → reviewing). Store reviewer_id for obligations."""
        task_id = await self.require_task_id(project_id, task_id)
        await self._transition(project_id, task_id, "reviewing",
                               actor_id=reviewer_id)
        if reviewer_id:
            now_ms = int(time.time() * 1000)
            await _execute(project_id,
                "UPDATE tasks SET reviewer_id = ?, updated_at = ? WHERE id = ?",
                [reviewer_id, now_ms, task_id])

    async def review_task(self, project_id: str, task_id: str, decision: str,
                          feedback: str | None = None,
                          reviewer_id: str | None = None,
                          *,
                          reason_code: str | None = None,
                          prescription_kind: str | None = None) -> None:
        """Review a task (reviewing → approved/rework, or approved → rework).

        decision='approve': reviewing → approved.
        decision='rework':  reviewing|approved → rework → running (两步合一).
        feedback stored in evidence.review_feedback; reviewer_id stored in
        evidence.reviewed_by (merge 自有分支门 / VERIFY 独立性依赖它).

        ``prescription_kind`` 是返修处方的**结构化类别**（见
        ``worktree_review.REWORK_PRESCRIPTION_KINDS``）——给写不出文件路径的
        返修类型（补证据/改状态/引用条款）一条正规通道。

        Raises:
            ReworkPrescriptionAbsent: 最终决定为返修但拿不出可执行处方。
                两条路径共用同一判据（人工返修看 feedback/prescription_kind，
                系统返修看 evidence.blocking_issues），状态不落。
        """
        task_id = await self.require_task_id(project_id, task_id)
        await _ensure_schema(project_id)
        decision = decision.lower()
        if decision not in ("approve", "rework"):
            raise ValueError(
                f"Invalid decision: {decision} (expected 'approve' or 'rework')")

        # 取现有 evidence 以便合并 feedback（不覆盖已提交的 evidence）
        rows = await _query(project_id,
            "SELECT evidence, status FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current_status = rows[0]["status"]
        existing = rows[0]["evidence"]
        evidence: dict = {}
        if existing:
            try:
                evidence = json.loads(existing) if isinstance(existing, str) \
                    else dict(existing)
            except (json.JSONDecodeError, TypeError):
                evidence = {}
        if feedback is not None:
            evidence["review_feedback"] = feedback
        if reviewer_id:
            evidence["reviewed_by"] = reviewer_id

        now_ms = int(time.time() * 1000)
        if decision == "approve":
            if current_status != "reviewing":
                raise ValueError(
                    f"Illegal transition: {current_status} → approved"
                )
            # E2: 终验（VERIFY / milestoneVerify）任务 evidence verdict=FAIL
            # → 验收工作合格但结论不合格，强制走 rework（不复用 close）。
            # E8: merge 后整体性检查 FAIL（integrity_check=fail + blocking_issues）
            # 对非 VERIFY 实现任务同样强制 rework——合成整体的整体性违例必须返修。
            # E2 修复（2026-08-25 TEST_DSH_28 实锤）：FAIL 强制路由不再依赖
            # 任务类型/标题前缀——任何 evidence.verdict=FAIL 的提交，approve 即
            # 强制 rework（探针「非 VERIFY 标题 + verdict=FAIL 被直接 approved」）。
            # integrity_check=fail 为并列触发条件。存量数据（无 verdict 字段）
            # → 不触发，向后兼容。
            verdict = normalize_verdict(evidence.get("verdict"))
            integrity_fail = (
                evidence.get("integrity_check") == "fail"
                and isinstance(evidence.get("blocking_issues"), list)
                and bool(evidence.get("blocking_issues"))
            )
            if verdict == "FAIL" or integrity_fail:
                await self._force_rework(
                    project_id,
                    task_id,
                    evidence,
                    feedback,
                    reviewer_id,
                    # 按来源拆分 reason_code（审计 2026-08-25 m-1）：终验结论
                    # FAIL 与合成整体性 FAIL 是两条修复链，事件/通知须可区分。
                    reason_code=(
                        "verdict_fail_rework"
                        if verdict == "FAIL"
                        else "integrity_check_fail"
                    ),
                    prescription_kind=prescription_kind,
                    current_status=current_status,
                )
                return
            # reviewing → approved
            # P0-3 观测补丁：approve 事件 payload 落评审意见（rework 路径
            # 本就带 detail=feedback，这里补对称，task.approved 不再是空壳）
            await self._transition(project_id, task_id, "approved",
                                   actor_id=reviewer_id,
                                   detail=((feedback or "").strip()[:500]
                                           or None))
            await _execute(project_id,
                "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
                [json.dumps(evidence), now_ms, task_id])
            # Slice P0: mark contract verified so downstream ready gates unlock
            try:
                task_row = await self.get_task(project_id, task_id)
                from hiveweave.services.task_contract import (
                    ensure_slice_status,
                    parse_contract,
                )

                c = parse_contract((task_row or {}).get("contract_json"))
                if c:
                    c = ensure_slice_status(c, "verified")
                    await self._persist_contract_json(project_id, task_id, c)
            except Exception as e:
                log.warning(
                    "slice_mark_verified_failed",
                    task_id=task_id,
                    error=str(e),
                )
            log.info("task_reviewed", task_id=task_id, decision=decision,
                     has_feedback=feedback is not None)
            await self._wake_dependent_tasks(project_id, task_id)
            # VERIFY child: close VERIFY + close parent in one lifecycle step
            try:
                task = await self.get_task(project_id, task_id)
                if task and self._is_verify_task(task):
                    await self._close_verify_and_parent(project_id, task)
            except Exception as e:
                log.warning(
                    "verify_auto_close_failed",
                    task_id=task_id,
                    error=str(e),
                )
            await self.emit_task_event(
                project_id,
                task_id,
                "approved",
                summary=f"[approved] task {task_id[:8]}",
            )
        else:
            # rework from reviewing (normal) or approved (merge conflict)
            if current_status not in ("reviewing", "approved"):
                raise ValueError(
                    f"Illegal transition: {current_status} → rework"
                )
            await self._force_rework(
                project_id,
                task_id,
                evidence,
                feedback,
                reviewer_id,
                reason_code=reason_code or "review_rework",
                prescription_kind=prescription_kind,
                current_status=current_status,
            )

    def _rework_prescription_problem(
        self,
        reason_code: str,
        evidence: dict,
        feedback: str | None,
        prescription_kind: str | None,
    ) -> str | None:
        """DSH 式处方判定：返回 None=合格，否则返回缺失原因码。

        两条路径分流，但**同一判据、同一汇聚点**：
        - 系统返修（verdict FAIL / integrity fail）：处方 = 证据本身。
          这里的 ``verdict == "FAIL"`` 分支**并不要求** blocking_issues 非空
          （见上面的触发条件），所以「系统返修却拿不出证据条目」是一处
          真实漏点，必须显式挡住。
        - 人工返修：处方 = reviewer 声明 ``prescription_kind`` 或 feedback 里
          的可识别形式（路径 / filesChanged）。
        """
        from hiveweave.services.worktree_review import rework_prescription_problem

        if reason_code in SYSTEM_REWORK_REASON_CODES:
            bis = evidence.get("blocking_issues")
            if isinstance(bis, list) and any(str(x).strip() for x in bis):
                return None
            # 证据条目为空 → 先看调用方自己是否给出了处方（路径或 kind），
            # 与人工分支**同一**判据（审计 L2：两条分支判据不对称，会误拦
            # 「approve 被强制转 rework、feedback 里写了路径但没传 kind」）。
            if rework_prescription_problem(feedback, prescription_kind) is None:
                return None
            # 双方都拿不出 → 报系统侧这个更精确的原因（可诊断性优于笼统的
            # feedback_without_prescription：它指出问题在"证据闸没给证据"）。
            return "system_rework_without_evidence"
        return rework_prescription_problem(feedback, prescription_kind)

    async def _force_rework(
        self,
        project_id: str,
        task_id: str,
        evidence: dict,
        feedback: str | None,
        reviewer_id: str | None,
        *,
        reason_code: str,
        prescription_kind: str | None = None,
        current_status: str,
    ) -> None:
        """E2 复用收口：把任务打回 rework→running（原子两步）。

        reviewing|approved → rework → running，reason_code 可区分触发来源
        （normal ``review_rework`` / 终验 FAIL ``verdict_fail_rework``）。
        走既有 rework 收尾：invalidate waivers、VERIFY case mark_failed、
        slice mark failed，并把 blocking_issues 附进 assignee 通知。

        Raises:
            ReworkPrescriptionAbsent: 最终决定为返修却拿不出可执行处方。
                判定在**状态变更之前** —— 这是「门禁必须住在做决定的操作里」
                (DSH ``packages/AGENTS.md:14``) 的落点：tool 层的同名检查只是
                facade，``approve`` 被证据闸强制转 rework 的路径能整条绕开它。
        """
        problem = self._rework_prescription_problem(
            reason_code, evidence, feedback, prescription_kind
        )
        if problem:
            log.warning(
                "rework_rejected_prescription_absent",
                task_id=task_id,
                reason_code=reason_code,
                problem=problem,
            )
            raise ReworkPrescriptionAbsent(problem)

        now_ms = int(time.time() * 1000)
        # P0-2: invalidate unexpired waivers on rework so waived_by
        # third-party isolation does not persist across review rounds.
        # A rework starts a fresh submit/review cycle; the prior waiver
        # was tied to the now-rejected submission. Lifetime count is
        # preserved for the MAX_WAIVERS_PER_TASK cap.
        try:
            from hiveweave.services.attestation import invalidate_valid_waivers

            await invalidate_valid_waivers(project_id, task_id)
        except Exception as e:
            log.warning(
                "rework_waiver_invalidate_failed",
                task_id=task_id,
                error=str(e),
            )
        await self._transition_multi(project_id, task_id, "rework", "running",
                                     actor_id=reviewer_id or "system",
                                     reason_code=reason_code,
                                     detail=(feedback or "")[:500])
        await _execute(project_id,
            "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
            [json.dumps(evidence), now_ms, task_id])
        try:
            task_row = await self.get_task(project_id, task_id)
            if task_row and self._is_verify_task(task_row):
                await VerificationCaseService().mark_failed(
                    project_id,
                    task_id,
                    notes=str(feedback or "")[:500],
                )
        except Exception:
            pass
        try:
            task_row = await self.get_task(project_id, task_id)
            from hiveweave.services.task_contract import (
                ensure_slice_status,
                parse_contract,
            )

            c = parse_contract((task_row or {}).get("contract_json"))
            if c:
                c = ensure_slice_status(c, "failed")
                await self._persist_contract_json(project_id, task_id, c)
        except Exception as e:
            log.warning(
                "slice_mark_failed_failed",
                task_id=task_id,
                error=str(e),
            )
        log.info("task_reviewed", task_id=task_id, decision="rework",
                 has_feedback=feedback is not None,
                 from_status=current_status, reason_code=reason_code)
        rows2 = await _query(
            project_id,
            "SELECT assignee_id FROM tasks WHERE id = ?",
            [task_id],
        )
        aid = rows2[0]["assignee_id"] if rows2 else None
        summary = f"[rework] task {task_id[:8]}"
        blocking = evidence.get("blocking_issues")
        if isinstance(blocking, list) and blocking:
            summary += " | blocking_issues: " + json.dumps(
                blocking, ensure_ascii=False
            )
        await self.emit_task_event(
            project_id,
            task_id,
            "rework",
            agent_id=aid,
            summary=summary,
        )

