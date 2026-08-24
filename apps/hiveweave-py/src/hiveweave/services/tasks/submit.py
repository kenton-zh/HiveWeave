"""Submit task + evidence workspace resolution."""
from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query
from .verify import normalize_verdict

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
            # E5 断流收口纪律：降级中提交 verdict=FAIL 属「waiver 型就地
            # 收口」——必须续跑重验或升级 coordinator，不许抢在续跑前
            # 用 FAIL 提交替豁免收口（复盘终验三连打断后 waiver 收口）。
            if (
                isinstance(evidence, dict)
                and normalize_verdict(evidence.get("verdict")) == "FAIL"
                and self._is_degraded_assignee(task)
            ):
                raise ValueError(
                    "SUBMIT REJECTED (degraded verify): 你所在 turn 刚被断流/"
                    "打断（降级中）且正提交 FAIL 终验——禁止就地收口。可执行"
                    "两步：① 续跑完成这一轮（正常完成一轮后平台自动清除降级"
                    "标志），完成重新验证后再提交；② 或显式升级 coordinator/CEO。"
                )

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
            "SELECT assignee_id, creator_id, reviewer_id, tags, title "
            "FROM tasks WHERE id = ?",
            [task_id],
        )
        agent_id = meta_rows[0]["assignee_id"] if meta_rows else None
        reviewer_id = None
        if meta_rows:
            creator_id = meta_rows[0]["creator_id"]
            existing_reviewer = meta_rows[0]["reviewer_id"]
            draft = {
                "tags": meta_rows[0]["tags"],
                "title": meta_rows[0]["title"],
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
        """
        if not isinstance(evidence, dict):
            raise ValueError(
                "SUBMIT VERDICT REJECTED (verify task): "
                "evidence 必须是 dict 才能判定 verdict"
            )
        verdict = normalize_verdict(evidence.get("verdict"))
        if verdict is None:
            raw = evidence.get("verdict")
            missing = "verdict" if raw in (None, "") else f"verdict={raw!r}"
            raise ValueError(
                "SUBMIT VERDICT REJECTED (verify task): "
                f"evidence 缺判定字段（缺：{missing}），"
                "期望 verdict ∈ {PASS, FAIL}（大小写不敏感）"
            )
        if verdict == "FAIL":
            blocking = evidence.get("blocking_issues")
            if not isinstance(blocking, list) or not blocking:
                raise ValueError(
                    "SUBMIT VERDICT REJECTED (verify task): "
                    "verdict=FAIL 时 blocking_issues 必须为非空 list "
                    "（当前缺失或为空）"
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

