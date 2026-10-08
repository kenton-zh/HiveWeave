"""submit_task tool

Split from tools/task_tools.py (AI-friendly package layout). Behavior unchanged.
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveweave.config import submit_audit_gate_enabled
from hiveweave.services import task as _task_svc
from hiveweave.services.param_shapes import coerce_list_param
from hiveweave.services.tasks.submit import (
    evidence_fingerprint_changed,
    parse_stored_evidence,
)
from hiveweave.services.tasks.verify import is_verify_task
from hiveweave.tools.base import tool
from hiveweave.tools import helpers as _helpers

_coerce_to_list = _helpers.coerce_to_list
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

# ── 批E#3 任务3：拒绝必带处方（结构化 remedy）────────────────────
# 门禁 issue 的**约定键**：``remedy`` = 一句**可执行**的下一步（模型语言）。
# 测试守卫（test_rejection_remedy_guard）AST 扫描本文件全部 ``issues.append``
# 字典：带 ``code`` 键的必须带 ``remedy`` —— 新增无处方拒绝分支时转红。
# 渲染经 :func:`_fmt_issue_line` 进 dry-run 与聚合回执（结构化键 + 文案双面）。


def _fmt_issue_line(i: dict) -> str:
    """聚合回执里的单条门禁行：``[code] message`` + 处方行（有则附）。"""
    line = f"- [{i.get('code', '?')}] {i.get('message', '')}"
    remedy = i.get("remedy")
    if remedy:
        line += f"\n  处方: {remedy}"
    return line

# ── T37 契约墙：dc 拒收回执附「按本任务实例化」的可抄模板 ───────────
# S19 屿洲实测：delivery contract 回执未填齐导致 44 次连续提交失败（43 分钟）
# —— 旧回执只给字段名 + 格式示意，模型反复填错（evidence.delivery_contract
# .summary 字样被当成参数键名）。修法：拒绝分支实例化一份完整可抄的
# deliveryContract JSON，test 字段用本任务已 attest 的 test_run 凭证 id
# 实时填充（照抄即过同一张门），并附一行抄写指令。凭证查询与 auto-attach
# 同源（find_recent_for_agent：task 匹配优先 + exit=0 + stdout_hash + 未过期），
# 保证填进去的 id 能过 verify_ids(expected_kinds=["test_run"], task_id=…)。

# P0-4：summary 示例值必须是**可照抄**的真实示例——旧值 "<实现摘要：…>" 以
# "<" 开头，正好落在 delivery_contract._PLACEHOLDER_LEADS 占位前缀上，照抄
# 必被同一张门二次拒收（处方自相矛盾）。新值不落在任何占位前缀上，原样照
# 抄即过 dc 门；summary 的合法性判定与门禁同源（delivery_contract_missing）。
_DC_SUMMARY_EXAMPLE = "实现摘要：本次实际改动内容与预期的偏差"
_DC_TEST_PLACEHOLDER = "test_run:<本任务的attestationId>"


def _dc_copyable_json(evidence: dict[str, Any], test_value: str) -> str:
    """实例化可抄 deliveryContract JSON（纯函数）。

    summary 尽量复用本次提交里**已合法**的摘要（只缺/错 test 的场景照抄
    整份 JSON 即过）；缺失/占位时换可照抄示例（:data:`_DC_SUMMARY_EXAMPLE`，
    不落在 _PLACEHOLDER_LEADS 占位前缀上）——合法与否的判定与门禁同源
    （delivery_contract_missing），不在此处另立判据。
    """
    from hiveweave.services.delivery_contract import delivery_contract_missing

    rc = evidence.get("delivery_contract")
    summary = ""
    if isinstance(rc, dict):
        s = rc.get("summary")
        if isinstance(s, str):
            summary = s.strip()
    if not summary or delivery_contract_missing(
        {"delivery_contract": {"summary": summary, "test": "test_run:x"}}
    ):
        summary = _DC_SUMMARY_EXAMPLE
    return json.dumps(
        {"summary": summary, "test": test_value}, ensure_ascii=False
    )


async def _dc_receipt_template_block(
    project_id: str, agent_id: str, task_id: str, evidence: dict[str, Any]
) -> str:
    """T37 拒收回执的可抄模板块：一行抄写指令 + 实例化 JSON。

    test 字段优先填本任务真实的 test_run 凭证 id；查不到退占位符并附跑测
    指引（fail-open：查询失败不改变拒绝本身）。
    """
    test_value = _DC_TEST_PLACEHOLDER
    try:
        from hiveweave.services.attestation import attestation_service

        ids = await attestation_service.find_recent_for_agent(
            project_id, agent_id=agent_id, task_id=task_id, kinds=["test_run"]
        )
        if ids:
            test_value = f"test_run:{ids[0]}"
    except Exception as e:  # noqa: BLE001 — 模板尽力填充，失败退占位符
        log.debug("submit_dc_template_test_run_lookup_failed", error=str(e))
    template = _dc_copyable_json(evidence, test_value)
    if test_value == _DC_TEST_PLACEHOLDER:
        test_note = (
            "（当前查不到本任务已 attest 的 test_run 凭证——先 bash 真跑"
            "一次测试拿到凭证 id，再替换 test 字段）"
        )
    else:
        test_note = "（test 已填本任务真实凭证 id，照抄即可）"
    return (
        "\nCOPY-PASTE: 把下面的 JSON 原样作为 submit_task 的 "
        f"deliveryContract 参数重交{test_note}：\n{template}"
    )


# ── submit_task ─────────────────────────────────────────


# B6 契约单源化（缺口 19）：工具最终描述的唯一 owner 是本模块 —— @tool
# 注册处与 executor.py 的 TOOL_PARAM_SCHEMAS 手写条目都引用
# ``SUBMIT_TASK_TOOL_DESCRIPTION``。
# 批3 灰度：审计门条款按 ``SUBMIT_AUDIT_GATE`` flag 装配（导入期定稿，
# 与全部 HIVEWEAVE_* 设置同口径：改 env 重启生效）。off（默认）时描述
# 不得出现审计门契约 —— 模型可见面必须与门禁行为同 commit 一致（prompt
# authoring skill §4：事实单源，运行时工具数组即能力）。
_SUBMIT_DESCRIPTION_HEAD = (
    "Submit for review (running → submitted). testsPassed must be true "
    "after real tests. Pass attestationIds from bash/browse. dryRun=true "
    "lists missing items without submitting. VERIFY tasks MUST pass "
    "verdict=PASS|FAIL (blockingIssues when FAIL, E1 hard gate); "
    "delivery-contract tasks MUST pass deliveryContract={summary, test}. "
)

_SUBMIT_DESCRIPTION_TAIL = (
    "Branch conflicting with main is rejected (merge_conflict_with_main) "
    "— call git_worktree_sync in your worktree (mode=materialize_conflict "
    "to get the conflict left there for you to resolve by hand), then "
    "resubmit. Only the assignee can submit."
)

# T16 残尾尾巴：gate=on 条款的挂起处方按模式分流（auto ⇒ wait_on，
# legacy ⇒ commit_turn(waiting_on …) 旧形态）。函数形态：使用时求值，
# 不在 import 钉死模式。
def _audit_gate_park_rx() -> str:
    from hiveweave.config import turn_exit_mode

    if turn_exit_mode() == "auto":
        return (
            'wait_on(refs=[{"kind": "task", "ref": "<audit task id>"}]) '
            "to park"
        )
    return "commit_turn(waiting_on kind=task, ref=<audit task id>) to park"


def _submit_audit_gate_on_clause() -> str:
    return (
        "CODE AUDIT GATE: a cumulative code diff over 20 lines requires an "
        "already-PASSING code_audit attestation to submit — you do NOT run the "
        "audit synchronously in your own turn. When the gate triggers, the "
        "platform auto-dispatches a SYSTEM AUDIT task to the reviewer pool and "
        "the submit is rejected with the dispatch receipt; follow that receipt: "
        + _audit_gate_park_rx()
        + ", and "
        "re-submit after the attestation lands (the platform wakes you). If the "
        "receipt says no reviewer is available, have your coordinator/superior "
        "fix the reviewer pool or run waive_attestation — do not waive on your "
        "own, and do not blindly re-submit. "
        "If a required credential is STRUCTURALLY impossible to issue "
        "(e.g. code_audit when your branch has no diff vs MAIN — task "
        "already merged), the gate records attestation_impossible"
        "(reason=tool_limited) and consumes it itself: no waiver needed. "
    )

_SUBMIT_AUDIT_GATE_OFF_CLAUSE = (
    "Per-commit code audit is retired (SUBMIT_AUDIT_GATE=off): submit "
    "requires no code_audit attestation and never dispatches audit tasks; "
    "audit duty belongs to milestone QA acceptance. "
)


def build_submit_task_description(*, audit_gate: bool) -> str:
    """submit_task 描述单源组装（B6）：审计门条款按灰度 flag 拼装（纯函数）。

    ``audit_gate=True`` 渲染旧门契约（CODE AUDIT GATE 全文）；
    ``False``（默认 off）渲染拆门契约，不提审计要求。
    """
    return _SUBMIT_DESCRIPTION_HEAD + (
        _submit_audit_gate_on_clause() if audit_gate
        else _SUBMIT_AUDIT_GATE_OFF_CLAUSE
    ) + _SUBMIT_DESCRIPTION_TAIL


SUBMIT_TASK_TOOL_DESCRIPTION = build_submit_task_description(
    audit_gate=submit_audit_gate_enabled()
)


class SubmitTaskParams(BaseModel):
    """Parameters for submit_task tool."""
    model_config = ConfigDict(populate_by_name=True)

    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description="ID of the task to submit. If omitted, auto-detects your current running task.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    summary: str | None = Field(
        default=None,
        description=(
            "Summary of work done. OPTIONAL (P0-4 single authoritative "
            "standard): when omitted, the platform backfills it from "
            "deliveryContract.summary — the one required reply path. "
            "Rejected only when BOTH are absent."
        ),
        # 别名 = 手写层（TOOL_PARAM_SCHEMAS）曾接受名字的并集——registered
        # 管线的别名解析只认 pydantic 这张表，回填才不丢既有可达面（B6 红线）。
        json_schema_extra={"aliases": ["summary", "report", "description"]},
    )
    commit: str | None = Field(
        default=None,
        description="Git commit hash (optional).",
        # llm_name 钉住 LLM 可见 property 正名为 commit（别名 commitSha 含
        # 大写会被 base._llm_prop_name 选成键名）。别名不收 "commitHash"
        # —— 那是 commit_hash 字段（VERIFY 证据）的正名，放这里会在别名
        # 解析时把 VERIFY 证据错路由到 commit。
        json_schema_extra={
            "aliases": ["commit", "commitSha", "commit_sha"],
            "llm_name": "commit",
        },
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
        # TEST_DSH_70 P1-6：平台级入参归一 —— {"item": […]} 群体单键形态
        # 解包（实测 submit_task 35 条）；多键 dict 禁展平（fail-closed）。
        return coerce_list_param(v)

    @field_validator("attestation_ids", mode="before")
    @classmethod
    def _coerce_attestation_ids(cls, v: Any) -> Any:
        return coerce_list_param(v)

    @field_validator("failures_acknowledged", mode="before")
    @classmethod
    def _coerce_failures_acknowledged(cls, v: Any) -> Any:
        return coerce_list_param(v)

    @field_validator("blocking_issues", mode="before")
    @classmethod
    def _coerce_blocking_issues(cls, v: Any) -> Any:
        return coerce_list_param(v)


def _build_evidence(
    params: SubmitTaskParams,
    policy_id: str,
    attest_ids: list[str],
) -> dict[str, Any]:
    """submit 参数 → verdict evidence 骨架（**纯函数**，无 IO / 无门禁判定）。

    从 _submit_preflight 原地抽出（TEST_DSH_63 接线修复）：只负责把参数
    逐键透传进 evidence；audit_soft 落章等需要任务上下文的步骤仍留在
    调用方。除新增 acceptance_coverage 透传外，行为与抽取前一致。

    P0-4 单一口径回填：顶层 ``summary`` 缺失/空白时从
    ``deliveryContract.summary`` 回执取（回执 summary 是存侧权威路径）。
    占位/空白值不回填——dc 门会以同一判据（``_PLACEHOLDER_LEADS``）拒绝，
    入口不把垃圾搬进 ``evidence.summary``；两者皆缺 ⇒ evidence.summary
    为 None，由 _submit_preflight 的 summary_missing 检查统一拒（唯一口径）。
    """
    # ⚠ `getattr` 容读：与下方 acceptance_coverage 同款纪律 —— 进程内其它
    # 调用点 / 测试里的 SimpleNamespace 假参数可能没有 summary 属性。
    _summary = getattr(params, "summary", None)
    if not (isinstance(_summary, str) and _summary.strip()):
        from hiveweave.services.delivery_contract import is_placeholder_text

        _rc = getattr(params, "delivery_contract", None)
        _rs = _rc.get("summary") if isinstance(_rc, dict) else None
        if (
            isinstance(_rs, str)
            and _rs.strip()
            and not is_placeholder_text(_rs)
        ):
            _summary = _rs
    evidence: dict[str, Any] = {
        "summary": _summary,
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
    if getattr(params, "acceptance_coverage", None):
        # ⚠ 必须 `getattr` 容读：与上面 `delivery_contract` / `contract_waived` 同款。
        # 直读 `params.acceptance_coverage` 时，任何**未带该字段**的 params（进程内
        # 其它调用点、测试里的 SimpleNamespace 假参数）都会 `AttributeError`
        # ⇒ 实测全量 17 条红（`test_submit_audit_reminder` 9 / `test_submit_conflict_gate` 5 /
        # `test_submit_auto_no_code_change` 3）全部落在这一行。
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
    from hiveweave.services.code_audit import (
        code_audit_soft_fail_pending,
        strip_code_audit_kind_for_gate,
    )

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
    # ── 批3 灰度：SUBMIT_AUDIT_GATE=off（默认）⇒ 拆除逐提交审计门 ──────
    # T42：剥离判据抽为单一事实源 ``strip_code_audit_kind_for_gate``（本路径
    # 与 review/api approve·HTTP 门共用）——覆盖 >20 行默认门 + 任务级
    # submitGate 政策（code_audit_unit 等）。单点过滤 = 下游全自然收敛：
    # verify_ids 不再要求 code_audit 凭证、audit_gate_pending 恒 False（不铸
    # SYSTEM AUDIT）、impossible/soft 探测短路、回执处方不再提
    # request_code_audit。test_run / browse_e2e 等任务级 kind 原样保留
    # （unit/visual 门不动）；审计责任归 QA 里程碑终验，QA 自愿审计仍走
    # request_code_audit（工具本体不动）。
    _stripped = strip_code_audit_kind_for_gate(needed)
    if _stripped != needed:
        needed = _stripped
        log.debug(
            "submit_audit_gate_off_code_audit_dropped",
            agent_id=agent_id,
            task_id=str(task_id)[:8],
            policy_id=policy_id,
        )
    # P0-3 fail-loud (TEST_DSH_38): an llm_failed attempt no longer drops the
    # kind at submit. The kind stays required; a passing attestation satisfies
    # verify_ids, otherwise the submit is rejected with an explicit-waive hint.
    audit_soft = code_audit_soft_fail_pending(needed, agent_id, task_id)
    # T1：审计门触发事实位（含义与判定见下方 verify_ids 之后），默认 False。
    audit_gate_pending = False
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
                "remedy": (
                    "按上面 Do 步骤用 browse 真实执行核心交互（taskId 记得带上），"
                    "然后原样重新 submit_task —— 平台自动附上凭证。"
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
                    "remedy": (
                        f"重新 submit_task 并带 failuresAcknowledged=[{{\"test\": "
                        f"\"<失败用例>\", \"reason\": \"<为何非阻塞>\"}} …] "
                        f"（至少 {required} 条，字段非空）；或先修到全绿再交。"
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
                    "remedy": (
                        '重新 submit_task，每条 entry 一律 '
                        '{"test": "<用例名>", "reason": "<原因>"}，两字段非空。'
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
            # ── T1 审查/审计派单化（单轨）：审计门触发事实位 ─────────────
            # policy 要求 code_audit 凭证且**未满足**（verify 整体失败 且
            # submitter 名下没有可自动附上的有效 code_audit 凭证——后者排除
            # 「门因其它 kind 失败 / 凭证在手只是没传 id」的误铸）。真实
            # submit 撞门时，submit_task_tool 据此铸造内部审计任务派审查池
            # （submitter 不再同步跑审计）；dry-run 只回执不铸造。
            audit_gate_pending = False
            if not ok and CODE_AUDIT_KIND in gate_needed:
                try:
                    _audit_cands = await attestation_service.find_recent_for_agent(
                        project_id,
                        agent_id=agent_id,
                        task_id=task_id,
                        kinds=[CODE_AUDIT_KIND],
                    )
                except Exception as _ac_e:  # noqa: BLE001 — 查不动按无可附
                    log.debug(
                        "submit_audit_gate_pending_probe_failed", error=str(_ac_e)
                    )
                    _audit_cands = []
                audit_gate_pending = not _audit_cands
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
                        "remedy": (
                            "按 umbrella-gate 提示把缺失 kind 的凭证补齐后重交；"
                            "确属本任务无法签发时，请协调者/CEO 对**这一条**任务 "
                            "waive_attestation。"
                        ),
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
                    # 批3 灰度：处方菜单按门内实际 kind 渲染 —— code_audit
                    # 不在必需清单（off 拆门后恒如此；或本就非审计 policy）
                    # 时不提 request_code_audit，回执只给剩余可达通道。
                    _remedy_menu = (
                        "request_code_audit / browse / attest_doc_review"
                        if (needed and CODE_AUDIT_KIND in needed)
                        else "browse / attest_doc_review / bash 跑测试(test_run)"
                    )
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
                        "remedy": (
                            "按 Options 选一条执行（本任务 policy 对应 "
                            + _remedy_menu
                            + "），凭证到手后原样重交；确属平台侧无法签发时才走 "
                            "waive_attestation。"
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
                            "llm_failed (platform-recorded) — silent skip is "
                            "not allowed (P0-3 fail-loud). This is NOT "
                            "missing evidence on your side. Under the "
                            "dispatched-audit flow you do NOT retry the audit "
                            "yourself: the platform dispatches a SYSTEM AUDIT "
                            "task with this receipt (see the AUDIT TASK "
                            "DISPATCHED block) — wait on it. Your own failed "
                            "attempt still gets background auto-retries; "
                            "whichever lands first opens the gate."
                        ),
                        "remedy": (
                            "按回执 AUDIT TASK DISPATCHED 区块收尾："
                            f"{_audit_gate_park_rx()}；凭证落地唤醒你后重交。"
                            "不要循环 request_code_audit；多次自动重试仍失败"
                            "且无可用审查者时，才请协调者 waive_attestation"
                            f"(taskId=\"{task_id}\", reasonKind=\"tool_failure\")。"
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
            "remedy": "重新 submit_task 并带 testsPassed=true（summary 里注明 N/A）。",
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
            assess_acceptance_coverage_verified,
            format_acceptance_coverage_verdict,
        )
        from hiveweave.services.tasks.verify import verdict_evidence_gaps

        for _gap in verdict_evidence_gaps(evidence):
            issues.append({
                "code": "verdict_gate",
                "message": (
                    "E1 verdict gate: " + _gap + "。补齐后重新 submit_task"
                    "（verdict ∈ {PASS, FAIL}；FAIL 还需非空 blockingIssues）。"
                ),
                "remedy": (
                    "重新 submit_task：verdict 必填 PASS|FAIL；FAIL 时另带非空 "
                    "blockingIssues=[...]。"
                ),
            })
        _cov_kinds = await acceptance_coverage_kinds(task)
        # TEST_DSH_70 P0-1a：三态评估（未声明/形状错/未核验），回执按态分解
        # + 逐条凭证绑定诊断；P1-2：waiver 在门内对齐主门消费（形状错除外）。
        _coverage = await assess_acceptance_coverage_verified(
            project_id,
            task_id,
            task.get("acceptance_criteria"),
            evidence,
            expected_agent_id=agent_id,
            kinds=_cov_kinds,
        )
        if _coverage.has_gaps:
            # 批21 修复1（T37 范式）：查本任务已 attest 的合格凭证 id 实时
            # 填进可照抄模板（照抄即过同一张门）；查不到 fail-open 退示例值。
            _cov_ids: list[str] = []
            try:
                _cov_ids = await attestation_service.find_recent_for_agent(
                    project_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    kinds=list(_cov_kinds),
                )
            except Exception as _cov_e:  # noqa: BLE001 — 模板尽力填充
                log.debug(
                    "submit_coverage_template_lookup_failed", error=str(_cov_e)
                )
            issues.append({
                "code": "acceptance_coverage",
                # F1：处方按本任务 policy 渲染 kind（防照抄 test_run 示例）。
                "message": format_acceptance_coverage_verdict(
                    _coverage, _cov_kinds, credential_ids=_cov_ids
                ),
                "remedy": (
                    "按回执逐条补齐缺失 kind 的执行凭证（以本任务 policy 渲染"
                    "的 kind 为准）；已填真实凭证 id 的 COPY-PASTE 模板可原样"
                    "照抄重交。"
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
                    "remedy": "先 git_worktree_checkpoint 提交工作树，再重新 submit_task。",
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
                    "remedy": (
                        "先 git_worktree_checkpoint —— 平台会从 worktree diff "
                        "自动回填 filesChanged 清单，然后重新 submit_task。"
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
                "remedy": (
                    "首选 1)：git_worktree_checkpoint 后重交（平台自动回填清单）；"
                    "或显式 submit_task(..., filesChanged=[磁盘上真实存在的路径])。"
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
                    # 批E#3 任务3（审计 named rejection）：门禁方向对（磁盘
                    # 存在性），但旧回执不说「以什么为准」——agent 凭记忆列
                    # 路径（TEST_DSH_74 逐字自述：rowband_probe.py 是记错的
                    # 名字）。处方指向**磁盘真名单**，不靠回忆。
                    "remedy": (
                        "以磁盘为准，不要凭记忆列路径：在提交前用 "
                        "`git diff --name-status main...HEAD` 取真实变更名单，"
                        "逐字抄进 filesChanged 再重交；未提交的先 "
                        "git_worktree_checkpoint。"
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
                    "remedy": (
                        "在你的 worktree 里 git_worktree_sync 同步 main → "
                        "解冲突 → commit → git_worktree_checkpoint → 重新 "
                        "submit_task。"
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
    # ── P0-4 单一权威口径：实现摘要必填性（两套互斥必填收敛为一条）──────
    # 修前 pydantic 顶层 summary 必填 与 dc 门 REQUIRED_REPLY_FIELDS 并存，
    # 同一 agent 78s 内拿到两种互斥处方（实测 116/272=42.6% 拒绝）。现口径：
    # 回执 summary（deliveryContract.summary）是唯一权威路径，顶层 summary
    # 是兼容入口（缺失时 _build_evidence 自动回填）；两者皆缺才拒。
    # 带 dc 契约且未豁免的任务由 dc 门统一开火（其回执自带可照抄模板，
    # 是唯一处方——此处不得重复报因，否则又是两种口径）。
    if not str(evidence.get("summary") or "").strip() and not (
        _dc_contract and not _dc_waived
    ):
        issues.append({
            "code": "summary_missing",
            "message": (
                "submit_task rejected: missing implementation summary "
                "（顶层 summary 与 deliveryContract.summary 均未提供——"
                "唯一权威口径见处方）。"
            ),
            "remedy": (
                "重新 submit_task 并带 summary=\"<真实实现摘要>\""
                "（唯一必填路径）。"
            ),
        })
    if _dc_contract and not _dc_waived:
        missing = delivery_contract_missing(evidence)
        if missing:
            # T37：可抄模板按本任务实例化（test 优先填真实凭证 id）。
            _dc_block = await _dc_receipt_template_block(
                project_id, agent_id, task_id, evidence
            )
            issues.append({
                "code": "delivery_contract_incomplete",
                "message": (
                    "Delivery contract 回执未填齐：缺 "
                    + ", ".join(missing)
                    + "。回执（deliveryContract.summary/test）是唯一权威"
                    "必填口径——顶层 summary 缺失时平台自动从回执回填"
                    "（P0-4）。"
                    + _dc_block
                    + " test 必须引用**本任务**的 test_run 凭证 id"
                    "（平台机器验证）。确无法跑测试：改用 "
                    "deliveryContract={'evidence_kind': 'not_applicable', "
                    "'not_applicable_reason': '<为什么跑不了>'}，并由协调者 "
                    "waive_attestation 正式豁免——未豁免的 not_applicable "
                    "不放行。非代码交付可显式 contractWaived=true 跳过"
                    "（不静默缺失）。"
                ),
                "remedy": (
                    "照抄上面 COPY-PASTE 的 JSON 作为 deliveryContract 参数"
                    "重交（示例值可原样照抄，不落占位前缀；建议把 summary "
                    "换成你的真实实现摘要）；跑不了测试走 not_applicable + "
                    "waive_attestation；非代码交付用 contractWaived=true。"
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
                        "remedy": (
                            "重交 deliveryContract={'evidence_kind': "
                            "'not_applicable', 'not_applicable_reason': '<原因>'} "
                            "并请协调者 waive_attestation；能跑就引用 test_run:<id>。"
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
                        "remedy": (
                            "用库里那张成功凭证的 id 重交：deliveryContract="
                            "{'summary': …, 'test': 'test_run:<id>'}。"
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
                        "remedy": (
                            "test 字段一律写成 'test_run:<本任务的 "
                            "attestationId>'（bash 跑测试自动落库的那张凭证）"
                            "后重交。"
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
                        # T37：id 在但过不了门（过期/绑错任务/编造）——同样附
                        # 可抄模板，库里有合格凭证时照抄真 id 即过。
                        _dc_block = await _dc_receipt_template_block(
                            project_id, agent_id, task_id, evidence
                        )
                        issues.append({
                            "code": "delivery_contract_incomplete",
                            "message": (
                                "Delivery contract 测试凭证不可验证："
                                f"test_run:{aid} —— {_terr}。"
                                "请用真实 test_run 凭证 id（bash 跑测试自动落库）。"
                                + _dc_block
                            ),
                            "remedy": (
                                "照抄上面 COPY-PASTE 的 JSON 重交（test 已填"
                                "本任务合格凭证 id 时直接用；否则先 "
                                "bash(..., taskId=本任务) 真跑一次测试拿到"
                                "新凭证）。"
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
        # T1：审计门触发（code_audit 必需且未满足）——submit_task_tool 据此
        # 铸造内部审计任务派审查池（dry-run 不铸）。
        "audit_gate_pending": audit_gate_pending,
    }


@tool(
    "submit_task",
    # B6 单源：唯一 owner 是上方 SUBMIT_TASK_TOOL_DESCRIPTION（executor
    # 手写条目 import 同一常量）——此处不再维护第二份文本。
    SUBMIT_TASK_TOOL_DESCRIPTION,
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
            + "\n".join(_fmt_issue_line(i) for i in preflight["issues"]),
            dry_run=True,
            missing=preflight["issues"],
        )

    # ── 聚合：一次列出全部缺失前置条件（不再第一个失败就停）──
    if preflight["issues"]:
        first = preflight["issues"][0]
        msg = str(first["message"])
        if first.get("remedy"):
            msg += f"\n处方: {first['remedy']}"
        if len(preflight["issues"]) > 1:
            msg += "\n\n[additional blockers]\n" + "\n".join(
                _fmt_issue_line(i) for i in preflight["issues"][1:]
            )
        # ── T1 审查/审计派单化（单轨）：审计门触发 ⇒ 平台自动铸造内部
        # 「代码审计任务」派给审查池（qa_lead → parent coordinator 回退链）。
        # submit 仍被既有门拒绝（门逻辑零改动）；拒绝回执自带 waiting_on
        # 处方——submitter 不再同步跑审计 LLM 调用。幂等由铸造侧保证
        # （同 (submitter, 源任务) 进行中的审计任务复用不重铸）。
        audit_mint: dict[str, Any] = {}
        # 批3 灰度：off 时不铸造（audit_gate_pending 在 preflight 已恒
        # False，此 flag 守卫是第二道防御；铸造入口自身还有第三道短路）。
        if preflight.get("audit_gate_pending") and submit_audit_gate_enabled():
            from hiveweave.services.tasks.audit_dispatch import (
                format_dispatch_receipt_block,
                mint_audit_task,
            )

            audit_mint = await mint_audit_task(project_id, task, agent_id)
            _mint_block = format_dispatch_receipt_block(
                audit_mint, str(task.get("id") or task_id)
            )
            if _mint_block:
                msg += f"\n\n{_mint_block}"
        # TEST_DSH_70 P1-2 活性出口：同一任务连续被门拒 N 次 ⇒ 自动
        # blocked(wait_kind=external) 升级，不再无限拉锯（37 连拒 110′ 实证）。
        try:
            from hiveweave.services.tasks.gate_reject_guard import (
                escalate_if_gate_reject_loop,
            )

            msg += await escalate_if_gate_reject_loop(
                project_id, task, error_text=msg
            )
        except Exception as _esc_e:  # noqa: BLE001 — 出口护栏不改变拒绝本身
            log.debug("gate_reject_escalation_failed", error=str(_esc_e))
        if audit_mint.get("dispatched"):
            return ToolResult.err(
                msg,
                audit_task_id=audit_mint.get("task_id"),
                audit_dispatched=True,
                audit_reviewer_id=audit_mint.get("reviewer_id"),
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

    # ── 批27-1 N1：submitted 态证据感知重提（A+B）────────────────────────
    # 早判必须放在 preflight 完成之后（此时 files_changed 已可能由
    # ``git diff main...HEAD`` 自动回填，指纹才算得全），透传与服务层的
    # 幂等/证据更新判定共用同一把指纹（evidence_fingerprint_changed）。
    # 三分流：
    #   同指纹 + 入参显式带证据字段 ⇒ 无效重提，err + 处方（B）——防抖
    #     语义不再掩盖「你没有任何新东西可交」；
    #   同指纹 + 裸重提（双发/手滑）⇒ 幂等 ok 早退（A-3，防抖不动）；
    #   指纹不同 ⇒ 不在此早退，透传 ts.submit_task 走服务层「证据更新」
    #     路径（状态保持 submitted + reviewer 收 RESUBMITTED 通知）。
    if ((task.get("status") or "").lower() == "submitted"):
        # 状态可能在本轮 preflight 期间被 reviewer 改动（rework ⇒ running），
        # 重读一次再判；非 submitted 态交回正常转移路径处理。
        task_now = await ts.get_task(project_id, task_id) or task
        if ((task_now.get("status") or "").lower() == "submitted"):
            old_ev = parse_stored_evidence(task_now.get("evidence"))
            if not evidence_fingerprint_changed(old_ev, evidence):
                _explicit_evidence = bool(
                    getattr(params, "commit", None)
                    or getattr(params, "commit_hash", None)
                    or getattr(params, "files_changed", None)
                    or getattr(params, "attestation_ids", None)
                )
                if _explicit_evidence:
                    _old_c = str((old_ev or {}).get("commit") or "")
                    _new_c = str(evidence.get("commit") or "")
                    return ToolResult.err(
                        f"Task {task_id} 已处于 submitted，且本次提交无新证据"
                        f"（账本 commit={_old_c or '∅'} = 你的 "
                        f"commit={_new_c or '∅'}，files_changed / "
                        "attestationIds 亦未变）——重复提交不会更新评审证据，"
                        "也不会再次通知 reviewer。\n"
                        "处方: QA 反馈已给出 ⇒ 等 reviewer "
                        "review_task(rework)，或自行 "
                        "update_task_status(running) 后修码重提；无反馈 ⇒ "
                        "无需动作，等待评审即可。"
                    )
                log.info(
                    "submit_task_tool_idempotent",
                    project_id=project_id,
                    task_id=task_id,
                )
                return ToolResult.ok(
                    f"Task {task_id} already submitted (idempotent). "
                    f"status={task_now.get('status')} "
                    f"submitted_at={task_now.get('submitted_at')} — "
                    "no new reviewer notification sent.",
                    idempotent=True,
                    task_id=task_id,
                    status=task_now.get("status"),
                    submitted_at=task_now.get("submitted_at"),
                )
            log.info(
                "submit_task_tool_evidence_resubmit",
                project_id=project_id,
                task_id=task_id,
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
        _submit_result = await ts.submit_task(project_id, task_id, evidence)
        # 批27-1 N1：服务层证据更新重提（submitted 态 + 指纹不同）——状态
        # 保持 submitted，reviewer 已由服务层收到 [TASK RESUBMITTED]；此处
        # 只渲染如实回执，不再走首提的 [TASK SUBMITTED] creator 唤醒（否则
        # 同一次重提通知两次）。isinstance 守卫兼容测试里 submit_task 被
        # mock 成非 dict 返回的调用方。
        _resubmitted = (
            isinstance(_submit_result, dict)
            and bool(_submit_result.get("resubmitted"))
        )
        # A4 让渡项（批 2）：消费服务层幂等出口 —— submitted 状态的重提在
        # 此早退，跳过 preflight 之后的 audit 提醒 / handoff 汇报 / reviewer
        # 唤醒（inbox wake + trigger_coordinator）。状态与证据均未变动，
        # reviewer 不该多收一次 [TASK SUBMITTED]。
        if (
            isinstance(_submit_result, dict)
            and _submit_result.get("idempotent")
            and not _resubmitted
        ):
            return ToolResult.ok(
                f"Task {task_id} already submitted (idempotent). "
                f"status={_submit_result.get('status')} "
                f"submitted_at={_submit_result.get('submitted_at')} — "
                "no new reviewer notification sent.",
                idempotent=True,
                task_id=_submit_result.get("task_id") or task_id,
                status=_submit_result.get("status"),
                submitted_at=_submit_result.get("submitted_at"),
            )

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

            # 批3 灰度：off 时不再提审计要求（软提醒一并静默）；账本重置
            # 保留（提交成功即清账，与门禁去留无关）。
            if (
                submit_audit_gate_enabled()
                and get_unaudited_lines(agent_id) > CODE_AUDIT_LINE_THRESHOLD
            ):
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
        # 证据更新重提（N1）不进此块：reviewer 已由服务层收 RESUBMITTED。
        task_after = await ts.get_task(project_id, task_id)
        if (
            not _resubmitted
            and task_after
            and task_after.get("creator_id")
        ):
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

        if _resubmitted:
            receipt = (
                f"Task {task_id} resubmitted: evidence updated on the "
                "already-submitted task (status stays submitted) — reviewer "
                "notified with [TASK RESUBMITTED]."
            )
        else:
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

