"""Submit task + evidence workspace resolution."""
from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query
from .acceptance import (
    acceptance_coverage_kinds,
    format_acceptance_coverage_error,
    uncovered_acceptance_items_verified,
)
from .verify import normalize_verdict, verdict_evidence_gaps

log = structlog.get_logger(__name__)


class SubmitMixin:
    """submit_task / _resolve_evidence_workspace."""

    if TYPE_CHECKING:
        require_task_id: Any
        get_task: Any
        _persist_contract_json: Any
        _transition: Any
        _is_verify_task: Any
        emit_task_event: Any

    async def submit_task(self, project_id: str, task_id: str,
                          evidence: dict) -> None:
        """Submit a task (running → submitted). Sets evidence (JSON) + submitted_at.

        BUG-P1b: 保留既有 evidence.merged_by —— VERIFY spawn 时写入的
        合并人标记是 review_task 独立审门排除合并人的唯一依据，submit
        整体覆盖 evidence 会让该门禁失效。

        TEST11 #3: on submit, pin ``reviewer_id`` (default creator_id) so the
        designated reviewer has obligations from the submitted window onward —
        not only after they call start_review.

        Slice P0: if ``contract_json`` present, L0 machine clauses must pass
        against the assignee worktree (or project root) before transition.
        """
        task_id = await self.require_task_id(project_id, task_id)
        task = await self.get_task(project_id, task_id)

        # E1 verdict gate：终验（VERIFY / milestoneVerify）任务 evidence 必须
        # 带强制判定字段，否则硬拒提交（transition 之前拦截）。
        if task and self._is_verify_task(task):
            self._validate_verdict_evidence(evidence)
            # 任务6（门禁智能化包）/#14：VERIFY 任务带非空 acceptance_criteria 时，
            # verdict evidence 必须用 `acceptance_coverage` **逐条按 id 声明覆盖**，
            # 且声明锚在平台可核验的执行凭证上（本任务 + 正确 kind + 未过期 +
            # exit_code=0）——判据是"id 集合包含 + 凭证核验"，与措辞/语言无关；
            # 不适用的条目须先由 coordinator/CEO waive_attestation 落平台 waiver 行。
            # 缺覆盖 → 拒绝并列出缺哪几条 + 处方。清单为空的任务不受影响；
            # check_evidence_verifiable 的 VERIFY 跳过保持不动。
            _cov_kinds = await acceptance_coverage_kinds(task)
            gaps = await uncovered_acceptance_items_verified(
                project_id,
                task_id,
                task.get("acceptance_criteria"),
                evidence,
                expected_agent_id=str(task.get("assignee_id") or "") or None,
                kinds=_cov_kinds,
            )
            if gaps:
                # F1：处方必须按本任务 policy 渲染 kind（否则 agent 照抄
                # `test_run` 示例 → 撞 attestation 门）。
                raise ValueError(
                    format_acceptance_coverage_error(gaps, _cov_kinds)
                )
            # E5 断流收口纪律：降级中提交 verdict=FAIL 属「waiver 型就地
            # 收口」——必须续跑重验或升级 coordinator，不许抢在续跑前
            # 用 FAIL 提交替豁免收口（复盘终验三连打断后 waiver 收口）。
            if (
                isinstance(evidence, dict)
                and normalize_verdict(evidence.get("verdict")) == "FAIL"
                and self._is_degraded_assignee(task)
            ):
                # 45 轮 P1「拒绝无记忆」①②：machine-readable 出路标记 +
                # 同因连拒计数（45 轮降级终验 3 连拒同文案）。
                from hiveweave.services.rejection_memory import (
                    repeat_rejection_notice, rejection_count,
                )

                msg = (
                    "SUBMIT REJECTED (degraded verify): 你所在 turn 刚被断流/"
                    "打断（降级中）且正提交 FAIL 终验——禁止就地收口。可执行"
                    "两步：① 续跑完成这一轮（正常完成一轮后平台自动清除降级"
                    "标志），完成重新验证后再提交；② 或显式升级 coordinator/"
                    "CEO。 RETRY[action=resume_turn_then_resubmit|"
                    "alt=escalate_coordinator]"
                )
                # 计数**必须照旧登记**（annotate 的副作用就是计数）：把它的
                # 返回值丢掉即可 —— 提示改走独立通道，不再污染拒绝文案本身。
                _agent_for_notice = str(task.get("assignee_id") or "") or None
                _repeat_notice = repeat_rejection_notice(
                    "submit_task", msg, agent_id=_agent_for_notice
                )
                if _repeat_notice and _agent_for_notice:
                    from hiveweave.services.health_notice import (
                        KIND_REPEAT_REJECTION,
                        deliver_notice,
                    )

                    # 通道与回执物理分离：模型在自己的下一轮读到它，而
                    # 拒绝文案保持纯粹可取证（批次 4 附项三问 ②）。
                    await deliver_notice(
                        _agent_for_notice,
                        _repeat_notice,
                        kind=KIND_REPEAT_REJECTION,
                        project_id=project_id,
                        wake=False,
                    )
                # 46/11 #8 平台动作：第 2 次连拒时自动把 VERIFY 回队
                # running——QA 获得 fresh turn 重验再提交，不再卡死在
                # 「提交→拒→提交」循环（356min 项目 8 连拒烧 VERIFY 的
                # 实证解法；文案教育已证无效，改为平台代动作）。
                repeat_n = rejection_count(
                    msg, agent_id=str(task.get("assignee_id") or "") or None
                )
                if repeat_n >= 2:
                    try:
                        # submitted→running 是 _TRANSITIONS 合法迁移；
                        # assignee 保持原 QA（duty 不变），fresh turn 解除
                        # 降级后即可重验。
                        await self._transition(
                            project_id, task_id, "running",
                            actor_id="system",
                            reason_code="degraded_verify_auto_requeue",
                        )
                        log.info(
                            "degraded_verify_auto_requeued",
                            project_id=project_id,
                            task_id=task_id,
                            repeat_n=repeat_n,
                        )
                    except Exception as e:
                        log.warning(
                            "degraded_verify_requeue_failed",
                            project_id=project_id,
                            task_id=task_id,
                            error=str(e),
                        )
                raise ValueError(msg)

        # SUBMITTED MACHINE PRE-RUN (slice-driven L0)
        if task and task.get("contract_json"):
            from hiveweave.services.task_contract import (
                ensure_slice_status,
                format_prerun_failure,
                parse_contract,
                run_machine_acceptance,
            )

            contract = parse_contract(task.get("contract_json"))
            if contract:
                ws_root = await self._resolve_evidence_workspace(
                    project_id, task
                )
                prerun = run_machine_acceptance(
                    contract, workspace_root=ws_root
                )
                contract = dict(contract)
                # s3-clone_07 GAP：service_smoke 异步条款——同步文件条款通过后，
                # 启动服务跑交付级冒烟（真协议客户端）。失败 = 拒绝提交。
                # 这是 07 轮 0/22 事故的对应门：内部门禁全绿 ≠ 交付物可用。
                smoke_clause = None
                for _c in contract.get("acceptance") or []:
                    if isinstance(_c, dict) and _c.get("type") == "service_smoke":
                        smoke_clause = _c
                        break
                if smoke_clause and prerun.passed:
                    from hiveweave.services.smoke_gate import (
                        run_service_smoke_clause,
                    )

                    smoke_result, smoke_freeze = await run_service_smoke_clause(
                        smoke_clause,
                        workspace_root=str(ws_root),
                        frozen=contract.get("smoke_freeze"),
                    )
                    prerun.results.append(smoke_result)
                    prerun.passed = prerun.passed and smoke_result.passed
                    if smoke_freeze:
                        contract["smoke_freeze"] = smoke_freeze
                contract["machine_pre_run"] = {
                    **prerun.to_dict(),
                    "at_ms": int(time.time() * 1000),
                    "workspace": str(ws_root),
                }
                if not prerun.passed:
                    await self._persist_contract_json(
                        project_id, task_id, contract
                    )
                    raise ValueError(format_prerun_failure(prerun))
                contract = ensure_slice_status(contract, "submitted")
                await self._persist_contract_json(
                    project_id, task_id, contract
                )
                if isinstance(evidence, dict):
                    evidence = dict(evidence)
                    evidence["machine_pre_run"] = contract["machine_pre_run"]

        await self._transition(project_id, task_id, "submitted",
                               actor_id=(task or {}).get("assignee_id"))
        if isinstance(evidence, dict) and "merged_by" not in evidence:
            rows0 = await _query(
                project_id, "SELECT evidence FROM tasks WHERE id = ?", [task_id]
            )
            if rows0 and rows0[0]["evidence"]:
                try:
                    prev = rows0[0]["evidence"]
                    prev = json.loads(prev) if isinstance(prev, str) else dict(prev)
                except (json.JSONDecodeError, TypeError):
                    prev = {}
                if isinstance(prev, dict) and prev.get("merged_by"):
                    evidence = dict(evidence)
                    evidence["merged_by"] = prev["merged_by"]
        now_ms = int(time.time() * 1000)
        # Pin reviewer at submit: existing column wins; evidence.reviewer_id
        # only fills when column is empty (non-VERIFY). VERIFY always → creator.
        meta_rows = await _query(
            project_id,
            "SELECT assignee_id, creator_id, reviewer_id, tags, title, kind "
            "FROM tasks WHERE id = ?",
            [task_id],
        )
        agent_id = meta_rows[0]["assignee_id"] if meta_rows else None
        reviewer_id = None
        if meta_rows:
            creator_id = meta_rows[0]["creator_id"]
            existing_reviewer = meta_rows[0]["reviewer_id"]
            # ⚠ 这里的 draft 是「中间视图」：字段缺一个，下游判定就静默走错分支。
            # `kind` 必须带上 —— #11 后 `_is_verify_task` 只读 kind；漏掉它会让
            # VERIFY 的「reviewer 钉 creator」规则**永不生效**（reviewer 可自审）。
            draft = {
                "tags": meta_rows[0]["tags"],
                "title": meta_rows[0]["title"],
                "kind": meta_rows[0]["kind"],
            }
            if self._is_verify_task(draft):
                reviewer_id = creator_id
            elif existing_reviewer:
                reviewer_id = existing_reviewer
            elif isinstance(evidence, dict) and evidence.get("reviewer_id"):
                reviewer_id = str(evidence["reviewer_id"])
            else:
                reviewer_id = creator_id
        if reviewer_id:
            await _execute(
                project_id,
                "UPDATE tasks SET evidence = ?, submitted_at = ?, "
                "reviewer_id = ?, updated_at = ? WHERE id = ?",
                [json.dumps(evidence), now_ms, reviewer_id, now_ms, task_id],
            )
        else:
            await _execute(
                project_id,
                "UPDATE tasks SET evidence = ?, submitted_at = ?, updated_at = ? "
                "WHERE id = ?",
                [json.dumps(evidence), now_ms, now_ms, task_id],
            )
        await self.emit_task_event(
            project_id,
            task_id,
            "submitted",
            agent_id=agent_id,
            summary=f"[submitted] task {task_id[:8]}",
        )

        # TEST6 S11: activate/ensure review obligation on submit
        # (owner = pinned reviewer; idempotent with dispatch-time create).
        try:
            owner = reviewer_id or (meta_rows[0]["creator_id"] if meta_rows else None)
            if owner:
                from hiveweave.services.obligation import ObligationLedger

                await ObligationLedger().create(
                    project_id,
                    str(owner),
                    "review",
                    task_id=task_id,
                    context={
                        "source": "submit",
                        "assignee_id": agent_id,
                        "activated": True,
                    },
                )
        except Exception as e:
            log.warning(
                "submit_review_obligation_failed",
                task_id=task_id,
                error=str(e),
            )

    @staticmethod
    def _validate_verdict_evidence(evidence: dict) -> None:
        """E1: 终验任务 evidence 硬校验 —— verdict 强制判定字段。

        verdict ∈ {PASS, FAIL}；verdict=FAIL 时 blocking_issues 必须为非空
        list。缺失或非法 → ValueError，点名缺什么（对齐 SUBMIT PRE-RUN
        FAILED 硬拒风格）。非终验任务无需这些字段，由调用方按谓词筛选。
        判定逻辑单源在 ``verify.verdict_evidence_gaps``（工具层聚合预检
        复用同一份，文案保持一致）。
        """
        gaps = verdict_evidence_gaps(evidence)
        if gaps:
            raise ValueError(
                "SUBMIT VERDICT REJECTED (verify task): " + "；".join(gaps)
            )

    @staticmethod
    def _is_degraded_assignee(task: dict | None) -> bool:
        """E5: 提交者（assignee）是否处于断流降级标志。

        惰性 import 规避 agents→services 循环依赖；registry 读取失败按
        False 处理（fail-open，不误伤正常提交）。
        """
        if not task:
            return False
        agent_id = str(task.get("assignee_id") or "")
        if not agent_id:
            return False
        try:
            from hiveweave.agents.recovery import is_degraded

            return is_degraded(agent_id)
        except Exception:
            return False

    async def _resolve_evidence_workspace(
        self, project_id: str, task: dict
    ) -> str:
        """Prefer assignee write worktree; fall back to project root."""
        from hiveweave.db import meta as meta_db

        project_ws = await meta_db.get_project_workspace(project_id) or ""
        assignee_id = task.get("assignee_id")
        if not assignee_id:
            return project_ws
        try:
            from hiveweave.services.org import OrgService

            agent = await OrgService().get_agent(str(assignee_id))
            wt = (agent or {}).get("workspace_path") or ""
            if wt:
                from pathlib import Path

                if Path(wt).is_dir():
                    return wt
        except Exception as e:
            log.debug(
                "evidence_workspace_fallback",
                task_id=(task.get("id") or "")[:12],
                error=str(e),
            )
        return project_ws

