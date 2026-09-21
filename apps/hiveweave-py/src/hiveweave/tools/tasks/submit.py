"""submit_task tool

Split from tools/task_tools.py (AI-friendly package layout). Behavior unchanged.
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveweave.services import task as _task_svc
from hiveweave.services.tasks.verify import is_verify_task
from hiveweave.tools.base import tool
from hiveweave.tools import helpers as _helpers

_coerce_to_list = _helpers.coerce_to_list
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

# ── submit_task ─────────────────────────────────────────


class SubmitTaskParams(BaseModel):
    """Parameters for submit_task tool."""
    model_config = ConfigDict(populate_by_name=True)

    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description="ID of the task to submit. If omitted, auto-detects your current running task.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    summary: str = Field(
        description="Summary of work done.",
        json_schema_extra={"aliases": ["summary", "report"]},
    )
    commit: str | None = Field(
        default=None,
        description="Git commit hash (optional).",
        json_schema_extra={"aliases": ["commit", "commitHash"]},
    )
    files_changed: list[str] | None = Field(
        default=None,
        alias="filesChanged",
        description="List of changed files (optional).",
        json_schema_extra={"aliases": ["filesChanged", "files_changed", "files"]},
    )
    tests_passed: bool | None = Field(
        default=None,
        alias="testsPassed",
        description=(
            "MANDATORY for code tasks: true only after you actually ran tests "
            "(npm test / pytest / etc.) and they passed. "
            "Documentation/explore-only tasks may set true with summary noting N/A."
        ),
        json_schema_extra={"aliases": ["testsPassed", "tests_passed"]},
    )
    test_output: str | None = Field(
        default=None,
        alias="testOutput",
        description="Brief test command output / proof (recommended).",
        json_schema_extra={"aliases": ["testOutput", "test_output", "testLog"]},
    )
    attestation_ids: list[str] | None = Field(
        default=None,
        alias="attestationIds",
        description=(
            "Server-issued attestation ids from browse/bash test runs. "
            "Required for UI/code tasks (bare testsPassed is rejected)."
        ),
        json_schema_extra={"aliases": ["attestationIds", "attestation_ids"]},
    )
    core_interaction_executed: bool | None = Field(
        default=None,
        alias="coreInteractionExecuted",
        description=(
            "UI VERIFY: true when core canvas/DOM interaction was exercised "
            "(browse js/eval or manual attestation). Prefer leaving unset — "
            "platform auto-accepts when a core_interaction browse_e2e "
            "attestation exists for this task."
        ),
        json_schema_extra={
            "aliases": ["coreInteractionExecuted", "core_interaction_executed"]
        },
    )
    failures_acknowledged: list[dict[str, Any]] | None = Field(
        default=None,
        alias="failuresAcknowledged",
        description=(
            "VERIFY only: when testOutput reports N>0 failures, provide a "
            "structured list of {test, reason} entries (one per failing case "
            "or group). Free-text excuses are rejected."
        ),
        json_schema_extra={
            "aliases": [
                "failuresAcknowledged",
                "failures_acknowledged",
                "acknowledgedFailures",
            ]
        },
    )
    verdict: str | None = Field(
        default=None,
        description=(
            "VERIFY tasks ONLY (title starts with 'VERIFY:'): structured "
            "conclusion — exactly 'PASS' or 'FAIL' (case-insensitive). "
            "Missing verdict on a VERIFY task is hard-rejected by E1. "
            "verdict=FAIL additionally requires blockingIssues."
        ),
        json_schema_extra={"aliases": ["verdict", "conclusion"]},
    )
    blocking_issues: list[str] | None = Field(
        default=None,
        alias="blockingIssues",
        description=(
            "VERIFY tasks ONLY: when verdict=FAIL, the blocking defect list "
            "that must be fixed (non-empty required — E1 hard gate). "
            "Routes the task to rework on approve (E2)."
        ),
        json_schema_extra={
            "aliases": ["blockingIssues", "blocking_issues", "blocking"]
        },
    )
    commit_hash: str | None = Field(
        default=None,
        alias="commitHash",
        description="Git commit hash on MAIN for VERIFY evidence (optional).",
        json_schema_extra={"aliases": ["commitHash", "commit_hash"]},
    )
    env_snapshot: str | None = Field(
        default=None,
        alias="envSnapshot",
        description="Optional environment snapshot for VERIFY evidence.",
        json_schema_extra={"aliases": ["envSnapshot", "env_snapshot"]},
    )
    delivery_contract: dict[str, Any] | None = Field(
        default=None,
        alias="deliveryContract",
        description=(
            "Delivery contract 回执 {summary, test}——仅当任务带 delivery "
            "contract（slice_type=delivery_contract）时必填。test 须引用"
            "**本任务**的 test_run:<id>（平台机器验证，不绑定本任务同样被拒）；"
            "确无法跑测试改用 evidence_kind=not_applicable + "
            "not_applicable_reason，并经 waive_attestation 豁免——N/A 文本不放行。"
        ),
        json_schema_extra={
            "aliases": ["deliveryContract", "delivery_contract", "contract"]
        },
    )
    contract_waived: bool = Field(
        default=False,
        alias="contractWaived",
        description=(
            "Delivery contract 显式跳过：当任务确实无交付回执可填（非代码/"
            "紧急 hotfix 等）且需通过 submit 时置 true。拒绝沉默缺失——绕过"
            "必须显式、留痕进 evidence.contract_waived=true。"
        ),
        json_schema_extra={"aliases": ["contractWaived", "contract_waived"]},
    )
    acceptance_coverage: dict[str, Any] | None = Field(
        default=None,
        alias="acceptanceCoverage",
        description=(
            "VERIFY 验收清单覆盖声明（仅 VERIFY 任务需要）。格式："
            '{"<条目id>": {"attestation_ids": ["<凭证id>"]}}；确不适的条目 '
            '{"<条目id>": {"not_applicable_reason": "<理由>"}}（还须平台 '
            "waiver）。凭证须属本任务要求的类型、未过期且成功（平台机器"
            "核验）。条目 id 以平台拒绝回执清单为准。"
        ),
        json_schema_extra={
            "aliases": ["acceptanceCoverage", "acceptance_coverage"]
        },
    )
    dry_run: bool = Field(
        default=False,
        alias="dryRun",
        description=(
            "Preflight (dry-run): when true, run ONLY the precondition checks "
            "(attestation ids / core interaction / failures acknowledged / "
            "delivery gate / files_changed existence) and return the complete "
            "missing-items list. NO mutations: no submit, no claim/start, "
            "no notifications. Default false = real submit."
        ),
        json_schema_extra={"aliases": ["dryRun", "dry_run", "preflight", "check"]},
    )

    @field_validator("files_changed", mode="before")
    @classmethod
    def _coerce_files_changed(cls, v: Any) -> Any:
        return _coerce_to_list(v)

    @field_validator("attestation_ids", mode="before")
    @classmethod
    def _coerce_attestation_ids(cls, v: Any) -> Any:
        return _coerce_to_list(v)


def _build_evidence(
    params: SubmitTaskParams,
    policy_id: str,
    attest_ids: list[str],
) -> dict[str, Any]:
    """submit 参数 → verdict evidence 骨架（**纯函数**，无 IO / 无门禁判定）。

    从 _submit_preflight 原地抽出（TEST_DSH_63 接线修复）：只负责把参数
    逐键透传进 evidence；audit_soft 落章等需要任务上下文的步骤仍留在
    调用方。除新增 acceptance_coverage 透传外，行为与抽取前一致。
    """
    evidence: dict[str, Any] = {
        "summary": params.summary,
        "tests_passed": True,
        "policy_id": policy_id,
        "attestation_ids": attest_ids,
    }
    if getattr(params, "commit", None) or getattr(params, "commit_hash", None):
        evidence["commit"] = (
            getattr(params, "commit", None) or getattr(params, "commit_hash", None)
        )
    if getattr(params, "core_interaction_executed", None):
        evidence["core_interaction_executed"] = True
    if getattr(params, "failures_acknowledged", None):
        evidence["failures_acknowledged"] = params.failures_acknowledged
    # E1 通道：VERIFY 任务的 verdict / blockingIssues 透传进 evidence
    # （service 层硬校验依赖这两个字段；缺失时由 E1 硬拒并给出明确文案）。
    from hiveweave.services.tasks.verify import normalize_verdict

    if getattr(params, "verdict", None):
        nv = normalize_verdict(params.verdict)
        evidence["verdict"] = nv if nv else str(params.verdict).strip()
    if getattr(params, "blocking_issues", None):
        evidence["blocking_issues"] = list(params.blocking_issues)
    if getattr(params, "env_snapshot", None):
        evidence["env_snapshot"] = str(params.env_snapshot)[:4000]
    _delivery_contract = getattr(params, "delivery_contract", None)
    if _delivery_contract:
        evidence["delivery_contract"] = _delivery_contract
    if getattr(params, "contract_waived", False):
        evidence["contract_waived"] = True
    # 验收清单覆盖声明 → evidence（acceptance.py 覆盖门的结构化判据）。
    # 此前该门经 submit_task 结构上不可满足：参数无入口，模型硬传也被
    # pydantic extra=ignore 静默吞掉（TEST_DSH_63：82 次调用 0 次带参）。
    if params.acceptance_coverage:
        evidence["acceptance_coverage"] = params.acceptance_coverage
    if params.files_changed:
        from hiveweave.services.worktree_review import normalize_files_changed

        evidence["files_changed"] = normalize_files_changed(params.files_changed)
    if params.test_output:
        evidence["test_output"] = params.test_output[:4000]
    return evidence


async def _submit_preflight(
    project_id: str,
    agent_id: str,
    task_id: str,
    task: dict,
    params: SubmitTaskParams,
) -> dict:
    """只读 submit 预检 — 收集全部前置缺失项，不写库/不发通知。

    镜像 submit_task_tool 的检查链（同序同文案）：UI core interaction /
    failures acknowledged / attestation 门 / docs tests_passed / 交付门
    （worktree 脏）/ files_changed 存在性。真实路径复用返回的中间量
    （attest_ids/policy_id/skip_delivery_gate/evidence），保证一次检查、
    行为一致。
    """
    from hiveweave.services.attestation import (
        CODE_AUDIT_KIND,
        attestation_service,
        ledger_policy_id,
        required_attestation_kinds,
        resolve_task_policy,
    )
    from hiveweave.services.code_audit import code_audit_soft_fail_pending

    ts = _task_svc.TaskService()
    issues: list[dict] = []

    tags = task.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    policy_id = ledger_policy_id(task)
    needed = required_attestation_kinds(policy_id)
    # P0-3 fail-loud (TEST_DSH_38): an llm_failed attempt no longer drops the
    # kind at submit. The kind stays required; a passing attestation satisfies
    # verify_ids, otherwise the submit is rejected with an explicit-waive hint.
    audit_soft = code_audit_soft_fail_pending(needed, agent_id, task_id)
    if audit_soft:
        log.info(
            "submit_code_audit_soft_fail_gate",
            agent_id=agent_id,
            task_id=str(task_id)[:8],
            policy_id=policy_id,
        )
    # ── 报告 Layer 6 第 4 行（歪招典藏）：把「凭证物理上无法签发」做成结构化
    # 事实、由门禁自己消化。典型：任务已 merge ⇒ 分支相对 MAIN 已无 diff ⇒
    # request_code_audit 结构上发不出 PASS。这不是「质量不够所以豁免」，
    # 因此不该教人去 waive_attestation（豁免入口保留，默认路径不再需要）。
    # 检测 fail-closed：不可证（无 worktree / git 不可判 / 有 ISSUES 前科）
    # 时返回 None，原门禁 + 人工豁免路径逐字不变。
    impossible: dict | None = None
    if needed and CODE_AUDIT_KIND in needed:
        from hiveweave.services.attestation import detect_attestation_impossible

        try:
            impossible = await detect_attestation_impossible(
                project_id, agent_id, task_id, needed
            )
        except Exception as _imp_e:  # noqa: BLE001 — 检测失败保持原门禁
            log.debug(
                "submit_attestation_impossible_probe_failed", error=str(_imp_e)
            )
            impossible = None
        if impossible:
            log.info(
                "submit_attestation_impossible",
                agent_id=agent_id,
                task_id=str(task_id)[:8],
                reason=impossible.get("reason"),
                detail=impossible.get("detail"),
            )
    attest_ids = list(params.attestation_ids or [])

    # Drop code_audit ids only when the policy does not require that kind
    # (audit is not a substitute for tests/browse). When code_audit* gates
    # require it, keep the ids so AND verify_ids can pass.
    if attest_ids and not (needed and CODE_AUDIT_KIND in needed):
        try:
            kept = []
            for _aid in attest_ids:
                _row = await attestation_service.get(project_id, str(_aid))
                if _row and str(_row.get("kind") or "") == CODE_AUDIT_KIND:
                    continue
                kept.append(_aid)
            if len(kept) != len(attest_ids):
                log.info(
                    "submit_filtered_code_audit_attestations",
                    agent_id=agent_id,
                    task_id=task_id,
                    removed=len(attest_ids) - len(kept),
                )
            attest_ids = kept
        except Exception as _fe:  # noqa: BLE001
            log.debug("submit_code_audit_filter_failed", error=str(_fe))

    is_verify = is_verify_task(task)
    parent_policy = policy_id
    parent_tags: list = []
    if is_verify and task.get("parent_task_id"):
        parent = await ts.get_task(project_id, task["parent_task_id"])
        if parent:
            parent_tags = parent.get("tags") or []
            if isinstance(parent_tags, str):
                try:
                    parent_tags = json.loads(parent_tags)
                except Exception:
                    parent_tags = []
            parent_policy = (
                parent.get("policy_id")
                or resolve_task_policy(
                    title=parent.get("title") or "",
                    tags=parent_tags if isinstance(parent_tags, list) else [],
                    description=parent.get("description") or "",
                )
            )
    ui_verify = is_verify and (
        "ui" in {str(t).lower() for t in (tags if isinstance(tags, list) else [])}
        or parent_policy == "ui_browser_e2e"
        or "ui" in {str(t).lower() for t in (parent_tags if isinstance(parent_tags, list) else [])}
    )
    if ui_verify:
        from hiveweave.services.attestation import find_core_interaction_attestation

        core_att = await find_core_interaction_attestation(
            project_id, task_id, agent_id
        )
        # Also accept any-agent attestation on this task (delegated VERIFY)
        if not core_att:
            core_att = await find_core_interaction_attestation(
                project_id, task_id, None
            )
        has_flag = bool(getattr(params, "core_interaction_executed", None))
        if not core_att:
            issues.append({
                "code": "ui_core_interaction",
                "message": (
                    "UI VERIFY submit rejected: need a browse_e2e attestation "
                    "with [core_interaction=1] (boolean flags alone do not unlock).\n"
                    "Do:\n"
                    f"1) browse(args=[\"js\",\"() => document.querySelector("
                    f"'canvas')?.dispatchEvent(new MouseEvent('click'))\"], "
                    f"taskId=\"{task_id}\")\n"
                    f"2) Then submit_task(..., taskId=\"{task_id}\") — platform "
                    f"auto-attaches the core_interaction attestation.\n"
                    + (
                        "Note: coreInteractionExecuted=true was set but no matching "
                        "attestation exists — inventing the flag is rejected."
                        if has_flag
                        else ""
                    )
                ),
            })
        elif core_att not in attest_ids:
            attest_ids.append(core_att)

    # TEST6 P0-3: VERIFY with reported failures must acknowledge structurally
    if is_verify:
        from hiveweave.services.attestation import count_reported_test_failures

        fail_n = count_reported_test_failures(params.test_output)
        if fail_n is not None and fail_n > 0:
            from hiveweave.services.attestation import required_failure_acks

            required = required_failure_acks(fail_n)
            acks = getattr(params, "failures_acknowledged", None) or []
            if not isinstance(acks, list) or len(acks) < required:
                issues.append({
                    "code": "failures_acknowledged",
                    "message": (
                        f"VERIFY submit rejected: testOutput reports {fail_n} "
                        f"failed test(s); need failuresAcknowledged with at least "
                        f"{required} {{test, reason}} entries "
                        f"(got {len(acks) if isinstance(acks, list) else 0}). "
                        f"Either rework until green, or acknowledge structurally "
                        f"(free-text excuses alone are not accepted)."
                    ),
                })
            else:
                bad = [
                    a for a in acks
                    if not isinstance(a, dict)
                    or not str(a.get("test") or a.get("name") or "").strip()
                    or not str(a.get("reason") or a.get("why") or "").strip()
                ]
                if bad:
                    issues.append({
                        "code": "failures_acknowledged_invalid",
                        "message": (
                            "VERIFY submit rejected: each failuresAcknowledged "
                            "entry must be {test, reason} with non-empty fields."
                        ),
                    })

    if needed:
        # Waiver 短路：coordinator 已显式豁免（CLI/脚本类任务正式出口）
        from hiveweave.services.attestation import has_valid_waiver

        # 门禁自行消化：物理无法签发的 kind 从本轮必需清单里剔除（边界见
        # detect_attestation_impossible docstring —— 只有可证「无 diff 可审」
        # 才命中；不可证/有 ISSUES 前科时 impossible=None，本清单不变）。
        gate_needed = needed
        if impossible and CODE_AUDIT_KIND in needed:
            gate_needed = frozenset(
                k for k in needed if k != CODE_AUDIT_KIND
            )
            log.info(
                "submit_attestation_impossible_gate_self_consumed",
                agent_id=agent_id,
                task_id=str(task_id)[:8],
                dropped=CODE_AUDIT_KIND,
            )
        if not await has_valid_waiver(project_id, task_id) and gate_needed:
            # TEST4: auto-attach recent matching attestations if LLM omitted ids
            if not attest_ids:
                attest_ids = await attestation_service.find_recent_for_agent(
                    project_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    kinds=gate_needed,
                )
                if attest_ids:
                    log.info(
                        "submit_task_auto_attached_attestations",
                        agent_id=agent_id,
                        task_id=task_id,
                        count=len(attest_ids),
                    )
            # P2-4：门禁回执要带结构化事实（expected/given/ignored kinds），
            # 免得调用方去解析 error 文案。
            _gate_report: dict = {}
            ok, err = await attestation_service.verify_ids(
                project_id,
                attest_ids,
                expected_agent_id=agent_id,
                expected_kinds=gate_needed,
                task_id=task_id,
                report=_gate_report,
            )
            if not ok:
                from hiveweave.services.attestation import (
                    format_umbrella_gate_hint,
                    should_hint_umbrella_gate,
                    task_is_umbrella,
                )

                if should_hint_umbrella_gate(
                    policy_id, needed, err
                ) and await task_is_umbrella(project_id, task):
                    issues.append({
                        "code": "attestation",
                        "message": format_umbrella_gate_hint(policy_id, err),
                        "gate": "submit_attestation",
                        "expectedKinds": _gate_report.get("expectedKinds", []),
                        "givenKinds": _gate_report.get("givenKinds", []),
                        "ignoredKinds": _gate_report.get("ignoredKinds", []),
                    })
                else:
                    if policy_id == "docs_only":
                        opt1 = (
                            f"1) attest_doc_review(taskId=\"{task_id}\", "
                            f"files=[{{path: \"specs/...\"}}]) then "
                            f"submit_task(..., attestationIds=[...]).\n"
                        )
                    elif policy_id in ("ui_browser_e2e", "code_audit_visual"):
                        audit_bit = (
                            "request_code_audit(...) then "
                            if (
                                policy_id == "code_audit_visual"
                                and CODE_AUDIT_KIND in needed
                            )
                            else ""
                        )
                        opt1 = (
                            f"1) {audit_bit}browse(...) until you have a "
                            f"browse_e2e row for this task, then "
                            f"submit_task(taskId=\"{task_id}\", "
                            f"attestationIds=[...]). "
                            f"Need kinds {sorted(needed)}.\n"
                        )
                    elif needed and CODE_AUDIT_KIND in needed:
                        extra = (
                            " and bash(..., taskId=this task) for test_run"
                            if "test_run" in needed
                            else ""
                        )
                        opt1 = (
                            f"1) request_code_audit(taskId=\"{task_id}\"){extra} "
                            f"then submit_task(..., attestationIds=[code_audit id, ...]).\n"
                        )
                    else:
                        opt1 = (
                            f"1) Run bash/tests as the assignee, then "
                            f"submit_task(taskId=\"{task_id}\", "
                            f"attestationIds=[...]).\n"
                        )
                    full_tid = task.get("id") or task_id
                    issues.append({
                        "code": "attestation",
                        "message": (
                            f"submit_task attestation gate failed ({policy_id}): {err}. "
                            f"taskId={full_tid} (use this full id).\n"
                            f"Options:\n"
                            + opt1
                            + (
                                f"2) Coordinator: "
                                f"waive_attestation(taskId=\"{task_id}\", "
                                f"evidenceAttestationId=\"<test_run|browse_e2e id>\", "
                                f"reason=\"<why THIS task>\").\n"
                                f"CEO may omit evidenceAttestationId after looking "
                                f"at this one task (cannot waive all tasks).\n"
                                f"Bare testsPassed is rejected."
                            )
                        ),
                        "gate": "submit_attestation",
                        "expectedKinds": _gate_report.get("expectedKinds", []),
                        "givenKinds": _gate_report.get("givenKinds", []),
                        "ignoredKinds": _gate_report.get("ignoredKinds", []),
                    })
                if audit_soft and CODE_AUDIT_KIND in gate_needed:
                    issues.append({
                        "code": "audit_soft_fail_gate",
                        "message": (
                            "A previous request_code_audit attempt ended "
                            "llm_failed — silent skip is no longer allowed "
                            "(P0-3 fail-loud). This is NOT missing evidence "
                            "on your side: the platform auto-enqueued a "
                            "background retry and will notify you in your "
                            "inbox when it succeeds — wait for that notice. "
                            "Only if repeated auto-retries (5) still fail, "
                            "have a coordinator run waive_attestation(taskId=\""
                            f"{task_id}\", reason=\"audit unavailable\", "
                            "reasonKind=\"tool_failure\") — a real human "
                            "decision. (A structurally IMPOSSIBLE audit — "
                            "e.g. the branch has no diff left because it is "
                            "already merged — is NOT this case: the gate "
                            "records attestation_impossible(reason="
                            "tool_limited) itself and needs no waiver.)"
                        ),
                    })
    elif params.tests_passed is not True:
        # docs_only still asks for explicit ack
        issues.append({
            "code": "docs_tests_passed",
            "message": (
                "docs_only submit still requires testsPassed=true "
                "(note N/A in summary)."
            ),
        })

    evidence = _build_evidence(params, policy_id, attest_ids)
    # ── F1：软失败「平台判定」点 ────────────────────────────────────────────
    # 触发条件是 ``audit_soft`` = ``code_audit_soft_fail_pending(needed, agent_id,
    # task_id)``，即 **平台**根据 policy 必需清单 + 进程内审计尝试记录
    # (``get_last_audit_attempt``，由 run_code_audit 的 record_audit_attempt
    # 写入) 判定，reason 亦取自该平台记录 —— **不是客户端输入**。
    # 这里除写 evidence 盖章（UI/兼容用，approve 侧已不再信它）之外，还必须在
    # 真实提交时补落一条**持久事实行**（见 submit_task_tool 的
    # record_code_audit_soft_fail 调用）——那才是 approve/HTTP 门禁的服务端复核
    # 判据。
    if audit_soft:
        from hiveweave.services.code_audit import (
            CODE_AUDIT_SOFT_FAIL_EVIDENCE_KEY,
            get_last_audit_attempt,
        )

        rec = get_last_audit_attempt(agent_id) or {}
        evidence[CODE_AUDIT_SOFT_FAIL_EVIDENCE_KEY] = {
            "reason": rec.get("reason") or "llm_failed",
            "task_id": task_id,
        }

    # ── 门禁智能化包任务1/6 + #14：E1 verdict 门 + 验收清单覆盖 → 并入聚合预检 ──
    # 服务层硬门保持不变（API 直提仍会被 services.tasks.submit 拒绝）；此处
    # 用同一套判定函数提前算进「一次报全」回执，免去 verdict 问题逐轮撞门。
    # #14：覆盖判据 = `acceptance_coverage` 的 id 声明 + 平台执行凭证核验
    # （kinds 按任务 policy 取；soft/未知回落全部执行类 kind），文本/措辞无关。
    if is_verify:
        from hiveweave.services.tasks.acceptance import (
            acceptance_coverage_kinds,
            format_acceptance_coverage_error,
            uncovered_acceptance_items_verified,
        )
        from hiveweave.services.tasks.verify import verdict_evidence_gaps

        for _gap in verdict_evidence_gaps(evidence):
            issues.append({
                "code": "verdict_gate",
                "message": (
                    "E1 verdict gate: " + _gap + "。补齐后重新 submit_task"
                    "（verdict ∈ {PASS, FAIL}；FAIL 还需非空 blockingIssues）。"
                ),
            })
        _cov_kinds = await acceptance_coverage_kinds(task)
        _uncovered = await uncovered_acceptance_items_verified(
            project_id,
            task_id,
            task.get("acceptance_criteria"),
            evidence,
            expected_agent_id=agent_id,
            kinds=_cov_kinds,
        )
        if _uncovered:
            issues.append({
                "code": "acceptance_coverage",
                # F1：处方按本任务 policy 渲染 kind（防照抄 test_run 示例）。
                "message": format_acceptance_coverage_error(
                    _uncovered, _cov_kinds
                ),
            })

    # P1-C/N5: code tasks require clean worktree + files_changed proof.
    tag_l = {
        str(t).strip().lower()
        for t in (tags if isinstance(tags, list) else [])
        if t
    }
    skip_delivery_gate = policy_id in ("docs_only", "explore") or bool(
        tag_l & {"docs_only", "docs", "explore", "no-code", "no_code"}
    )
    if not skip_delivery_gate:
        from hiveweave.services.worktree_review import (
            agent_worktree_path,
            effective_delivery,
            normalize_files_changed,
            project_main_workspace,
        )

        main_ws = await project_main_workspace(project_id)
        wt = await agent_worktree_path(agent_id)
        dirty_count = 0
        if wt and main_ws:
            delivery = await effective_delivery(main_ws, wt)
            dirty_count = int(delivery.get("dirty_count") or 0)
            if dirty_count > 0:
                issues.append({
                    "code": "worktree_dirty",
                    "message": (
                        "submit_task rejected: worktree has uncommitted changes. "
                        "Call git_worktree_checkpoint first, then submit_task."
                    ),
                })
            if not evidence.get("files_changed"):
                from hiveweave.services.git_worktree import _git

                ok_diff, diff_out = await _git(
                    ["diff", "--name-only", "main...HEAD"], wt
                )
                if ok_diff and (diff_out or "").strip():
                    evidence["files_changed"] = normalize_files_changed(
                        [
                            ln.strip()
                            for ln in diff_out.splitlines()
                            if ln.strip()
                        ]
                    )
            if not evidence.get("files_changed") and dirty_count > 0:
                issues.append({
                    "code": "worktree_dirty_no_files",
                    "message": (
                        "submit_task rejected: no files_changed and worktree is dirty. "
                        "Call git_worktree_checkpoint first."
                    ),
                })

        # BUG-ORGWT 疏通（2026-08-05 feature-test 死锁）：attestation 背书但
        # 无任何代码变更的提交 = verification-only 交付（平台功能测试/纯验证类
        # 任务，交付物是 .hiveweave/reports/ 下的报告 + 真实 test_run 凭证，
        # 按规则 .hiveweave/ 文件对 files_changed 不可见）。此前这类提交在
        # submit 被放行，到 approve 却被 review_worktree_gate 以
        # "no worktree path / no diverged files" 硬拒，agent 侧无解法
        # （不知道要显式打 no_code_change 旗标）。submit 是最后能一致化
        # evidence 的位置——此处自动补旗标，与上方自动回填 files_changed
        # 对称。安全边界：仅当 attestations 经平台验真（存在、未过期、
        # 归属本 agent/本任务、stdout_hash 齐备）且自动 diff 确实挖不到
        # 文件时触发；approve 侧审查方 fresh test_run 硬闸（P0-2）不受
        # 影响，仍独立生效。
        # 审计 P1（2026-08-05）：软策略（generic_tests/coordinator_review）
        # 下 attestation_ids 是 agent 自述、上方 strict 门不校验——必须先
        # verify_ids 再信，否则伪造 ID + 空交付即可借自动旗标绕过
        # review/close 双侧 merge gate（TEST20 N1 "Rita escape" 复活）。
        if not evidence.get("files_changed"):
            _aids = [str(x) for x in (evidence.get("attestation_ids") or []) if x]
            if _aids and evidence.get("no_code_change") is not True:
                try:
                    _aok, _aerr = await attestation_service.verify_ids(
                        project_id,
                        _aids,
                        expected_agent_id=agent_id,
                        task_id=task_id,
                    )
                except Exception as _ve:
                    _aok, _aerr = False, f"verify_error: {_ve}"
                if _aok:
                    evidence["no_code_change"] = True
                    evidence["_auto_no_code_change"] = "attestation_only_delivery"
                    log.info(
                        "submit_auto_no_code_change",
                        task_id=task_id,
                        agent_id=agent_id,
                        attestations=len(_aids),
                    )
                else:
                    log.warning(
                        "submit_auto_no_code_change_rejected_unverified",
                        task_id=task_id,
                        agent_id=agent_id,
                        attestations=len(_aids),
                        error=_aerr,
                    )

        # ── 门禁智能化包任务1①：files_changed 空（非 doc 类任务）─────────
        # 此前空清单会一路走到 review 才被 compare_worktree_to_main 拒绝
        # （42 轮实证 files_changed 空 3+3 次全在对面侧才爆）。这里只把缺口
        # 提前并进聚合回执 + 给处方；有 verified attestation 时上方已自动盖
        # no_code_change 旗标（verification-only 交付），不会进此分支。
        # dirty>0 且无清单的场景已由上方 worktree_dirty_no_files 覆盖，不重复报。
        # VERIFY 任务排除：其交付物是凭证/verdict 而非 diff（review 侧
        # review_worktree_gate 对 VERIFY 本就整体跳过），不在此门拦截。
        # worktree/MAIN 无法定位时不判（不可测量即不拦截，沿用旧契约）。
        if (
            not is_verify
            and wt
            and main_ws
            and dirty_count == 0
            and not evidence.get("files_changed")
            and not evidence.get("no_code_change")
        ):
            issues.append({
                "code": "files_changed_empty",
                "message": (
                    "submit_task rejected: files_changed 为空（非 "
                    "docs/explore 类任务）。处方（三选一）："
                    "1) 确有代码变更 → git_worktree_checkpoint 后重交"
                    "（平台会从 worktree diff 自动回填清单）；"
                    "2) 清单漏填 → submit_task(..., filesChanged=[...]) "
                    "显式列出 worktree 中真实存在的文件；"
                    "3) 本任务确无代码变更 → 带本任务的 verified "
                    "attestationIds 重交（平台自动盖 no_code_change "
                    "旗标），或打 docs/explore 标签。"
                ),
            })

    # P1-2: submit-time symmetric existence gate (mirrors approve-time
    # missing_claimed check). Catches "submit with no actual deliverable"
    # and ".hiveweave/ invisible files" at submit rather than at approve.
    fc_list = evidence.get("files_changed") or []
    if fc_list and not skip_delivery_gate:
        from pathlib import Path as _PSub

        from hiveweave.services.worktree_review import (
            agent_worktree_path as _awt,
            normalize_evidence_path,
            project_main_workspace as _pmw,
        )

        _sub_ws = await _pmw(project_id)
        _sub_wt = await _awt(agent_id)
        _roots = [r for r in (_sub_wt, _sub_ws) if r]
        if _roots:
            missing_at_submit: list[str] = []
            invisible_at_submit: list[str] = []
            for fc in fc_list[:30]:  # cap to avoid perf issues
                # Do NOT use str.lstrip("./") — strips every leading '.' and
                # turns ".hiveweave/…" into "hiveweave/…" (TEST11/TEST19).
                fc_clean = normalize_evidence_path(fc)
                if not fc_clean:
                    continue
                if ".hiveweave/" in fc_clean:
                    invisible_at_submit.append(fc_clean)
                    continue
                if not any((_PSub(r) / fc_clean).exists() for r in _roots):
                    missing_at_submit.append(fc_clean)
            if missing_at_submit:
                from hiveweave.services.worktree_review import (
                    hint_missing_file_locations as _hint,
                )

                hints = _hint(_roots, missing_at_submit)
                root_note = " | ".join(f"root: {r}" for r in _roots)
                issues.append({
                    "code": "files_changed_missing",
                    "message": (
                        "submit_task rejected: files_changed references paths "
                        "that do not exist on disk: "
                        + ", ".join(missing_at_submit[:8])
                        + ("…" if len(missing_at_submit) > 8 else "")
                        + f". Checked: {root_note}. "
                        + (" ".join(hints) + " " if hints else "")
                        + "Ensure all deliverables are committed in your "
                        "worktree before submitting."
                    ),
                })
            if invisible_at_submit:
                log.warning(
                    "submit_files_under_hiveweave",
                    task_id=task_id,
                    agent_id=agent_id,
                    paths=invisible_at_submit[:5],
                )
                # Warning only — don't block, but inform the agent
                evidence["_hiveweave_invisible_warning"] = invisible_at_submit[:5]

    # ── 冲突左移门(2026-08-26): 分支与 main 冲突时拒绝提交 ──
    # executor 在自己上下文还热着时解决冲突, 而不是 merge 瞬间撞墙打回
    # 重审。放在脏工作区门之后(此时工作区已 commit, 预演的是已提交状态)。
    # fail-open: 无 worktree / git 过旧 / 预演失败一律放行, 唯一阻塞条件
    # = merge-tree 明确报冲突。
    try:
        from hiveweave.services.git_worktree.conflict_predict import (
            predict_merge_conflicts as _predict,
        )
        from hiveweave.services.worktree_review import (
            agent_worktree_path as _cwt,
            project_main_workspace as _pmw2,
        )

        _wt = await _cwt(agent_id)
        if _wt:
            # 冲突预演要信任锚的项目根（本块作用域里 _sub_ws 不存在 —— 那是上面
            # 另一个 try 块里的局部名；加关键字参数务必连调用链一起 grep）
            _pred = await _predict(
                _wt, project_root=await _pmw2(project_id)
            )
            if _pred.status == "conflict":
                issues.append({
                    "code": "merge_conflict_with_main",
                    "message": (
                        "submit_task rejected: 你的分支与 main 存在合并冲突"
                        f"(main 领先 {_pred.behind} 个提交, 冲突文件: "
                        + (", ".join(_pred.conflicts[:8])
                           if _pred.conflicts else "(清单解析失败, 以 rebase 实际输出为准)")
                        + ("…" if len(_pred.conflicts) > 8 else "")
                        + ")。请在你的 worktree 内调用 `git_worktree_sync`"
                          "（默认先拒绝可预判冲突、不留半成品；要把冲突就地"
                          "手工解就用 mode=materialize_conflict，解完 "
                          "`git add` + commit，或 mode=abort 退回）"
                          "，然后重新 submit_task。"
                          "不要让 coordinator 在合并时替你解冲突。"
                    ),
                })
    except Exception as _ce:  # noqa: BLE001
        log.debug("submit_conflict_gate_failed", task_id=task_id,
                  error=str(_ce))

    # ── DELIVERY CONTRACT 预检（普通代码任务回执完整性 + 测试凭证机器验证）──
    # 只检查带 delivery_contract 类型契约的任务；非 dc 契约（协调者自建 slice）
    # 与 verify 类（走 E1）天然不受影响。contractWaived 显式跳过留痕。
    from hiveweave.services.delivery_contract import (
        delivery_contract_missing,
        has_successful_test_run,
        parse_delivery_contract,
        parse_test_evidence_attestation_id,
        test_evidence_is_na,
        test_evidence_reason,
    )

    # 豁免出口与主 attestation 门一致:协调者显式 waiver 后,交付契约回执
    # 不再拦截(豁免 = "凭证缺失/结构完整度暂时让位",语义对齐既有 waiver)。
    from hiveweave.services.attestation import has_valid_waiver

    _dc_contract = parse_delivery_contract(task)
    _dc_waived = bool(evidence.get("contract_waived")) or await has_valid_waiver(
        project_id, task_id
    )
    if _dc_contract and not _dc_waived:
        missing = delivery_contract_missing(evidence)
        if missing:
            issues.append({
                "code": "delivery_contract_incomplete",
                "message": (
                    "Delivery contract 回执未填齐：缺 "
                    + ", ".join(missing)
                    + "。请补入 submit_task(..., deliveryContract={"
                    "'summary': '<实现摘要>', 'test': 'test_run:<本任务凭证id>'})。"
                    "test 必须引用**本任务**的 test_run 凭证 id（平台机器验证，"
                    "不绑定本任务同样被拒）。确无法跑测试：改用 "
                    "deliveryContract={'evidence_kind': 'not_applicable', "
                    "'not_applicable_reason': '<为什么跑不了>'}，并由协调者 "
                    "waive_attestation 正式豁免——未豁免的 not_applicable 不放行。"
                    "非代码交付可显式 contractWaived=true 跳过（不静默缺失）。"
                ),
            })
        else:
            test_v = str(
                (evidence.get("delivery_contract") or {}).get("test") or ""
            )
            if test_evidence_is_na(test_v):
                if len(test_evidence_reason(test_v).strip()) < 2:
                    issues.append({
                        "code": "delivery_contract_incomplete",
                        "message": (
                            "Delivery contract 测试证据 N/A 文本不再被接受："
                            "确无法跑测试请改交 evidence_kind='not_applicable' + "
                            "not_applicable_reason，并经 waive_attestation 豁免。"
                        ),
                    })
                elif await has_successful_test_run(
                    project_id, task_id, task=task
                ):
                    # R1 回执一致性：声明 N/A"跑不了测试"，但库里有该任务的
                    # 成功 test_run 凭证——声明与机器事实矛盾，应引用凭证。
                    issues.append({
                        "code": "delivery_contract_inconsistent",
                        "message": (
                            "Delivery contract 测试证据写 N/A，但本任务已存在"
                            "成功（exit_code=0）的 test_run 凭证。应引用该凭证 "
                            "test_run:<id>；N/A 文本已不再放行。"
                        ),
                    })
            else:
                aid = parse_test_evidence_attestation_id(test_v)
                if not aid:
                    issues.append({
                        "code": "delivery_contract_incomplete",
                        "message": (
                            "Delivery contract 测试证据格式无法识别：期望 "
                            "'test_run:<本任务 attestationId>'，"
                            f"实际：{test_v[:80]!r}。"
                        ),
                    })
                else:
                    _dc_report: dict = {}
                    _tok, _terr = await attestation_service.verify_ids(
                        project_id,
                        [aid],
                        expected_kinds=["test_run"],
                        task_id=task_id,
                        report=_dc_report,
                    )
                    if not _tok:
                        issues.append({
                            "code": "delivery_contract_incomplete",
                            "message": (
                                "Delivery contract 测试凭证不可验证："
                                f"test_run:{aid} —— {_terr}。"
                                "请用真实 test_run 凭证 id（bash 跑测试自动落库）。"
                            ),
                            "gate": "delivery_contract_test",
                            "expectedKinds": _dc_report.get("expectedKinds", []),
                            "givenKinds": _dc_report.get("givenKinds", []),
                            "ignoredKinds": _dc_report.get("ignoredKinds", []),
                        })

    return {
        "ok": not issues,
        "issues": issues,
        "attest_ids": attest_ids,
        "policy_id": policy_id,
        "ui_verify": ui_verify,
        "skip_delivery_gate": skip_delivery_gate,
        "evidence": evidence,
        "impossible": impossible,
        # F1：平台判定的软失败（供提交时补落持久事实行；dry-run 不写库）。
        "audit_soft": audit_soft,
    }


@tool(
    "submit_task",
    "Submit a task for review (running -> submitted). Requires server "
    "attestationIds from browse (UI) or bash test runs (code). "
    "VERIFY task: MUST pass verdict=PASS|FAIL (+blockingIssues when FAIL). "
    "Tasks with a delivery contract (写树代码任务): MUST pass "
    "deliveryContract={summary, test:'test_run:<id>' bound to THIS task}. "
    "GATE CONTRACT (code_audit_unit policy): if your code edits exceed 20 "
    "lines, a PASSING code_audit attestation (exit_code=0) is REQUIRED — "
    "submit without one is rejected (dry-run lists the missing kinds). If "
    "request_code_audit soft-fails (llm_failed/no_model/no_callback), the "
    "gate stays CLOSED: retry once; if it keeps failing, ask your "
    "coordinator/superior to run waive_attestation(taskId, reason) — a "
    "silent re-submit just gets rejected again. If instead the audit is "
    "STRUCTURALLY IMPOSSIBLE (your branch has no diff vs MAIN — e.g. the "
    "task is already merged), the gate records a structured fact "
    "attestation_impossible(reason=tool_limited) and consumes it itself: "
    "no waiver needed. "
    "docs/explore tasks may use tags docs/explore. "
    "If taskId omitted, auto-detects your current running task. "
    "See PLATFORM MECHANISMS (system prompt) for gate semantics.",
    requires_workspace=False,
    security_level="standard",
)
async def submit_task_tool(
    params: SubmitTaskParams, agent_id: str, workspace: str
) -> ToolResult:
    """Submit a task for review."""
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    task_id = params.task_id
    ts = _task_svc.TaskService()
    if not task_id:
        tasks = await ts.list_tasks(project_id, assignee_id=agent_id)
        active = [t for t in tasks if t.get("status") in ("running", "claimed")]
        if not active:
            return ToolResult.err(
                "submit_task requires 'taskId'. No active task found for your agent. "
                "Call get_tasks to find your tasks, then pass taskId."
            )
        if len(active) > 1:
            task_list = ", ".join(
                f"{t['id']} ({t.get('title', '?')})" for t in active
            )
            return ToolResult.err(
                f"Multiple active tasks found: {task_list}. "
                "Please specify which taskId to submit "
                "(full 36-char ids above — copy directly)."
            )
        task_id = active[0]["id"]

    task = await ts.get_task(project_id, task_id)
    if not task:
        return ToolResult.err(f"Task not found: {task_id}")

    # B3: 归档任务写保护 —— 已归档任务不可提交
    if task.get("is_archived"):
        return ToolResult.err(
            f"Task {task.get('id') or task_id} is archived and cannot be "
            "submitted (full id above — copy directly). "
            f"Use create_task or dispatch_task for new work."
        )

    # TEST_DSH_32 P5：approved 后重交（Illegal transition approved→submitted）
    # 此前会裸抛状态机异常。提前给明确指引：等 merge；确有新工作要走
    # rework 循环或新任务。
    _st = (task.get("status") or "").lower()
    if _st == "approved":
        return ToolResult.err(
            f"Task {task.get('id') or task_id} is already APPROVED — submit "
            "is closed for this round. The reviewer/creator will "
            "git_worktree_merge it. Do NOT resubmit. If you have NEW changes "
            "that must be reviewed, ask the reviewer to review_task(rework) "
            "first, or create a new task referencing this one."
        )
    if _st in ("closed", "cancelled"):
        return ToolResult.err(
            f"Task {task.get('id') or task_id} is {_st} (terminal) — it "
            "cannot be submitted. Create a new task for further work."
        )

    # B4: 只有 assignee 可以提交任务。creator==assignee 的自交任务
    # 在 task_assignee == agent_id 时已经通过，不需要 creator 例外。
    # creator 例外会让 CEO 代 assignee 提交（代交+自审一条龙），
    # 绕过 ASSIGNEE_MUST_SUBMIT 义务账本。
    task_assignee = task.get("assignee_id")
    if task_assignee and str(task_assignee) != str(agent_id):
        return ToolResult.err(
            f"Only the assignee can submit this task. "
            f"You are not the assignee (assignee={task_assignee}). "
            f"If you are the creator, use review_task or dispatch_task instead."
        )

    # TEST21 M2: backfill implementer lock for pre-M2 running tasks
    if not getattr(params, "dry_run", False):
        try:
            await ts.lock_implementer_if_needed(project_id, task_id, agent_id)
        except Exception as e:
            log.debug("submit_lock_implementer_failed", error=str(e))

    preflight = await _submit_preflight(
        project_id, agent_id, task_id, task, params
    )

    # ── dry-run：只读预检，列出全部缺失项，零写操作 ──
    if getattr(params, "dry_run", False):
        if preflight["ok"]:
            _dr_note = ""
            if preflight.get("impossible"):
                _dr_note = (
                    "\n[attestation_impossible] 本任务的 code_audit 凭证物理"
                    "无法签发（reason=tool_limited，worktree 相对基准无 diff）"
                    "——门禁已自行消化该 kind，无需人工豁免；提交时会落一条"
                    "结构化事实位供审计。"
                )
            return ToolResult.ok(
                "submit_task dry-run: 所有前置条件已满足，可以提交。" + _dr_note,
                dry_run=True,
                missing=[],
                impossible=preflight.get("impossible") or None,
            )
        return ToolResult.ok(
            "submit_task dry-run: 以下前置条件未满足，提交将被拒绝：\n"
            + "\n".join(
                f"- [{i['code']}] {i['message']}" for i in preflight["issues"]
            ),
            dry_run=True,
            missing=preflight["issues"],
        )

    # ── 聚合：一次列出全部缺失前置条件（不再第一个失败就停）──
    if preflight["issues"]:
        first = preflight["issues"][0]
        msg = str(first["message"])
        if len(preflight["issues"]) > 1:
            msg += "\n\n[additional blockers]\n" + "\n".join(
                f"- [{i['code']}] {i['message']}"
                for i in preflight["issues"][1:]
            )
        return ToolResult.err(msg)

    evidence: dict[str, Any] = preflight["evidence"]

    # ── 门禁智能化包任务4：verdict_claim_check（FAIL 主张机械复核）──────
    # 只增事实位：结果附进 verdict 证据（evidence.claim_check）与提交回执，
    # 供 reviewer 参考；绝不据此拒绝或翻转 verdict（红线）。
    claim_check_lines: list[str] = []
    if (
        str(evidence.get("verdict") or "").upper() == "FAIL"
        and evidence.get("blocking_issues")
    ):
        try:
            from hiveweave.services.tasks.verdict_claim_check import (
                format_claim_check_lines,
                run_verdict_claim_check,
            )

            _checks = await run_verdict_claim_check(project_id, task, evidence)
            if _checks:
                evidence["claim_check"] = _checks
                claim_check_lines = format_claim_check_lines(_checks)
        except Exception as e:  # noqa: BLE001
            log.debug("verdict_claim_check_failed", task_id=task_id, error=str(e))

    # ── 报告 Layer 6 第 4 行：门禁自行消化的「凭证物理无法签发」事实位 ──
    # 结构化事实落成 tool_attestations 行（kind=attestation_impossible，平台
    # 签发），并同步进 evidence —— 事后可审计「谁在何时因何结构原因免检」，
    # 而不是像豁免那样只留下「CEO 关了闸」。落库失败不回滚提交（事实行是
    # 观测面，不是门禁判据）。
    impossible_info = preflight.get("impossible")
    if impossible_info:
        from hiveweave.services.attestation import (
            ATTESTATION_IMPOSSIBLE_EVIDENCE_KEY,
            record_attestation_impossible,
        )

        _imp_stamp: dict[str, Any] = {
            "reason": impossible_info.get("reason"),
            "need": impossible_info.get("need"),
            "detail": impossible_info.get("detail"),
            "detected_at": impossible_info.get("detected_at"),
            "consumed_by": "submit_gate",
        }
        try:
            _imp_id = await record_attestation_impossible(
                project_id,
                agent_id=agent_id,
                task_id=task_id,
                info=impossible_info,
            )
            _imp_stamp["attestation_id"] = _imp_id
        except Exception as _rec_e:  # noqa: BLE001 — 观测面失败不阻断提交
            log.warning(
                "submit_record_attestation_impossible_failed", error=str(_rec_e)
            )
        evidence[ATTESTATION_IMPOSSIBLE_EVIDENCE_KEY] = _imp_stamp

    # ── F1：软失败也走持久化平台事实（与 attestation_impossible 同构）──────
    # 提交通过（非 dry-run）时，为平台判定的软失败补落一条
    # tool_attestations 事实行（kind=code_audit_soft_fail，平台签发）。approve/
    # HTTP 门禁只认这条落库行（``resolve_soft_fail_kind``），**不再信 evidence**
    # ——否则任何调用方自带 ``{"code_audit_soft_fail": {"reason": "llm_failed"}}``
    # 即可零 attestation 过门。落库失败不回滚提交（与 impossible 事实同语义：
    # 观测面失败不阻断提交；缺失的后果只是 approve 侧更严——保持原门禁）。
    if preflight.get("audit_soft"):
        from hiveweave.services.attestation import record_code_audit_soft_fail
        from hiveweave.services.code_audit import CODE_AUDIT_SOFT_FAIL_EVIDENCE_KEY

        _soft_stamp = evidence.get(CODE_AUDIT_SOFT_FAIL_EVIDENCE_KEY) or {}
        try:
            await record_code_audit_soft_fail(
                project_id,
                agent_id=agent_id,
                task_id=task_id,
                reason=str(_soft_stamp.get("reason") or "llm_failed"),
            )
        except Exception as _soft_e:  # noqa: BLE001 — 观测面失败不阻断提交
            log.warning(
                "submit_record_code_audit_soft_fail_failed", error=str(_soft_e)
            )

    try:
        # Auto-transition: if task is in 'created' or 'claimed' status,
        # automatically claim/start it before submitting.
        if task:
            status = task.get("status", "")
            if status == "created":
                await ts.claim_task(project_id, task_id, agent_id)
                await ts.start_task(project_id, task_id)
            elif status == "claimed":
                await ts.start_task(project_id, task_id)
        await ts.submit_task(project_id, task_id, evidence)

        # ── code_audit：账本超阈且近期无审计凭证 → 软提醒（不阻断、不改
        #    状态流）；无论是否提醒，真实提交成功即重置账本 ──
        audit_reminder = ""
        try:
            from hiveweave.services.attestation import find_latest_attestation_by_kind
            from hiveweave.services.code_audit import (
                CODE_AUDIT_KIND,
                CODE_AUDIT_LINE_THRESHOLD,
                code_audit_submit_reminder,
                get_last_change_ts,
                get_unaudited_lines,
                reset_ledger,
            )

            if get_unaudited_lines(agent_id) > CODE_AUDIT_LINE_THRESHOLD:
                latest = await find_latest_attestation_by_kind(
                    project_id, agent_id=agent_id, kind=CODE_AUDIT_KIND
                )
                audited_after_changes = (
                    latest is not None
                    and latest.get("created_at", 0) >= get_last_change_ts(agent_id) * 1000
                )
                if not audited_after_changes:
                    audit_reminder = f"\n{code_audit_submit_reminder(agent_id)}"
            reset_ledger(agent_id)
        except Exception as e:  # noqa: BLE001
            log.debug("submit_code_audit_reminder_failed", error=str(e))

        # ── 标记 handoff 为已汇报 ──
        # submit_task 即"向上汇报"，清除 expect_report 义务
        try:
            from hiveweave.services.handoff import HandoffService
            hs = HandoffService()
            cnt = await hs.mark_reported(project_id, agent_id, task_id)
            if cnt:
                log.info("handoff_marked_reported", agent_id=agent_id, task_id=task_id, count=cnt)
        except Exception as e:
            log.warning("handoff_mark_reported_failed", error=str(e))

        # ── 通知 reviewer 有 task 待审 ──
        # 正常路径：wake creator。自交（creator==assignee，如中层自建骨架任务）
        # 时改 wake org parent（中层→CEO），避免「通知自己 + 禁自审」死锁。
        task_after = await ts.get_task(project_id, task_id)
        if task_after and task_after.get("creator_id"):
            creator_id = task_after["creator_id"]
            from hiveweave.services.inbox import InboxService
            inbox = InboxService()
            self_submit = creator_id == agent_id
            notify_id = creator_id
            if self_submit:
                try:
                    from hiveweave.services.org import OrgService

                    me = await OrgService().resolve_agent(agent_id)
                    parent_id = (me or {}).get("parent_id")
                    if parent_id:
                        notify_id = parent_id
                except Exception as e:
                    log.warning("submit_parent_lookup_failed", error=str(e))
            await inbox.send_message(
                from_agent_id=agent_id if not self_submit else "system",
                to_agent_id=notify_id,
                message=(
                    f"[TASK SUBMITTED] Task '{task_after.get('title', '')[:60]}' "
                    f"has been submitted for your review. "
                    f"Use review_task(taskId='{task_id}', decision='approve'/'rework') "
                    f"to review."
                ),
                message_type="task",
                priority="normal",
                task_id=task_id,
                wake=True,
            )
            from hiveweave.agents.trigger import trigger_coordinator
            await trigger_coordinator(notify_id)

        receipt = f"Task {task_id} submitted for review.{audit_reminder}"
        if claim_check_lines:
            receipt += (
                "\n[claim_check] blockingIssues 文件级主张机械复核"
                "（事实位，不改变 verdict，仅供 reviewer 参考）：\n"
                + "\n".join(claim_check_lines)
            )
        if impossible_info:
            receipt += (
                "\n[attestation_impossible] code_audit 凭证物理无法签发"
                f"（reason={impossible_info.get('reason')}, "
                f"detail={impossible_info.get('detail')}）：门禁已自行消化，"
                "未走人工豁免；结构化事实位已落库（tool_attestations + "
                "evidence）。如该事实判定有误，仍可用 waive_attestation 人工"
                "覆盖或让上游重新指派任务。"
            )
        return ToolResult.ok(receipt)
    except Exception as e:
        return ToolResult.err(f"Failed to submit task: {e}")

