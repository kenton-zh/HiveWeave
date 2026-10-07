"""Task attestation policy resolution."""
from __future__ import annotations

from typing import Any


def resolve_task_policy(
    title: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
) -> str:
    """Infer attestation policy_id from task metadata.

    Returns: ``ui_browser_e2e`` | ``docs_only`` | ``generic_tests``.
    """
    from hiveweave.services.attestation import resolve_task_policy as _resolve

    return _resolve(title, tags, description)


# ── P1-7 (TEST_DSH_33): 收口期望前置下发 ────────────────────
# submit_task 13 次调用失败 6 次，其中 5 次是「撞了才知道规则」：
# deliveryContract 缺 summary/test（3 次）、attestation kind 不匹配
# （交 test_run 但门要 browse_e2e，2 次）。期望在 submit 侧本来就算得出来，
# 缺的只是**提前**告诉 assignee。本模块把同一份推导（ledger_policy_id →
# required_attestation_kinds、parse_delivery_contract → REQUIRED_REPLY_FIELDS）
# 复用成一段人话，dispatch/claim 时下发；不新造判定，避免两处口径漂移。

# kind → 产出该凭证的工具（纯展示；kind 集合仍由 POLICY_REQUIRED_KINDS 唯一裁定）
_KIND_SOURCE_TOOL = {
    "test_run": "bash(..., taskId=本任务) 跑测试",
    "browse_e2e": "browse(..., taskId=本任务) 真跑一遍 UI",
    "visual_check": "assert_visual(..., taskId=本任务)",
    "doc_review": "attest_doc_review(taskId=本任务, files=[…])",
    "code_audit": "request_code_audit(taskId=本任务)",
}

# deliveryContract 字段 → 下发时展示的填写形态（对齐 submit 侧拒绝文案）
# 注意：没有"写 N/A 文本就放行"的形态 —— 不适用走 evidence_kind 状态 + 平台 waiver，
# 形态见 format_submit_expectations 里 dc_fields 分支的说明。
_DC_FIELD_SHAPE = {
    "summary": "<实现摘要：实际改了什么、与预期的偏差>",
    "test": "test_run:<attestationId>（平台机器验证，须绑定本任务）",
}

# 批26 修复1（2026-10-07）：可照抄回执模板 —— submit 首过率前置化。
# 第五轮 submit 首过失败主拒因 = 「Delivery contract 回执未填齐」74+64 次；
# 拒收回执里的 COPY-PASTE 处方实测有效（重交 22s 过），但**首次通过**才是
# 摩擦。模板值（照抄槽位）**不得落在占位前缀上**（is_placeholder_text 同源
# 自查，delivery_contract._PLACEHOLDER_LEADS）—— 旧 _DC_FIELD_SHAPE 的
# `<…>` 形态本身就是占位前缀，照抄必被占位门二次拒收。
# ⚠ 槽位记号用全角〈〉而非 ASCII <：占位门只认**开头**的 ASCII "<"，
#   全角记号在句中不会被误判，模板值本身可安全通过占位自查。
_DC_TEMPLATE_SUMMARY = "把〈改动对象〉改成〈效果〉，与预期的偏差〈说明〉"
_DC_TEMPLATE_TEST = (
    "test_run:〈用 bash(taskId=本任务) 跑完测试后，从 bash 回执整条复制的凭证 id〉"
)

# required_attestation_kinds 对未知 policy 的 fail-close 哨兵：不是真 kind，
# 不下发（下发会教 agent 去找一个不存在的凭证）。
_UNKNOWN_POLICY_SENTINEL = "_unknown_policy"


def submit_expectations(task: dict[str, Any] | None) -> dict[str, Any]:
    """本任务 submit 时的收口期望（机器可读）。

    ``attestation_kinds``：submit/approve 硬门要求的凭证 kind 列表；
    ``None`` = 软策略（无强制 kind）。``delivery_contract_fields``：
    ``evidence.delivery_contract`` 的必填字段（无 dc 契约则空）。
    """
    from hiveweave.services.attestation import (
        ledger_policy_id,
        required_attestation_kinds,
    )
    from hiveweave.services.code_audit import strip_code_audit_kind_for_gate
    from hiveweave.services.delivery_contract import (
        REQUIRED_REPLY_FIELDS,
        parse_delivery_contract,
    )

    if not task:
        return {
            "policy_id": None,
            "attestation_kinds": None,
            "delivery_contract_fields": [],
            "policy_unknown": False,
        }
    policy_id = ledger_policy_id(task)
    # T42：与门禁同源 —— flag off 时 code_audit kind 不是可达契约，
    # [SUBMIT CONTRACT] 不得教 assignee 去 request_code_audit。
    needed = strip_code_audit_kind_for_gate(
        required_attestation_kinds(policy_id)
    )
    kinds = (
        sorted(k for k in needed if k != _UNKNOWN_POLICY_SENTINEL)
        if needed
        else []
    )
    dc_fields = (
        list(REQUIRED_REPLY_FIELDS) if parse_delivery_contract(task) else []
    )
    return {
        "policy_id": policy_id,
        "attestation_kinds": kinds or None,
        "delivery_contract_fields": dc_fields,
        # 未知 policy 会 fail-close 成不可能满足的哨兵 kind —— 不是软策略，
        # 也不该把哨兵当凭证下发（会让 agent 去找不存在的东西）。
        "policy_unknown": bool(needed) and not kinds,
    }


def format_submit_expectations(task: dict[str, Any] | None) -> str:
    """把 :func:`submit_expectations` 渲染成下发给 assignee 的提示块。

    无任何硬性期望时返回空串（调用方据此决定是否注入）。
    """
    exp = submit_expectations(task)
    kinds = exp["attestation_kinds"]
    dc_fields = exp["delivery_contract_fields"]
    if not kinds and not dc_fields and not exp["policy_unknown"]:
        return ""

    lines = [f"[SUBMIT CONTRACT] submitGate policy={exp['policy_id']}"]
    if kinds:
        how = "；".join(
            _KIND_SOURCE_TOOL.get(k, f"产出 {k} 凭证") for k in kinds
        )
        lines.append(
            f"- 必需 attestation kind：{kinds}（缺一即拒）。取证方式：{how}。"
            f"submit_task(attestationIds=[…]) 必须**包含**这些 kind 且绑定本任务"
            f" —— 判据是集合**包含**：少任何一个都会被拒；多带的 kind 无害"
            f"（被忽略，可留可去，不要为了「凑对」而撤回已挂的凭证）。"
        )
    elif exp["policy_unknown"]:
        lines.append(
            f"- policy_id={exp['policy_id']!r} 平台不认识，attestation 门会"
            f"fail-close 无法通过。提交前请让协调者用 dispatch_task(taskId, "
            f"submitGate=…) 重设合法 gate。"
        )
    else:
        lines.append(
            "- 无强制 attestation kind（软策略），但仍应附上真实跑过的凭证。"
        )
    if dc_fields:
        shapes = ", ".join(
            f"'{f}': '{_DC_FIELD_SHAPE.get(f, '<必填>')}'" for f in dc_fields
        )
        lines.append(
            f"- 必填 deliveryContract={{{shapes}}}（空白/占位符视为未填）。"
            f"`test` 必须引用**本任务**跑出来的 test_run 凭证 id，平台机器验证；"
            f"不存在或未绑定本任务同样被拒。"
            f"确无法跑测试：不要写 N/A 文本，改为提交 "
            f"deliveryContract={{'evidence_kind': 'not_applicable', "
            f"'not_applicable_reason': '<为什么跑不了>'}}，并请协调者 "
            f"waive_attestation 正式豁免 —— 未豁免的 not_applicable 不放行。"
            f"非代码交付显式 contractWaived=true，不要静默省略。"
        )
        # 批26 修复1：可照抄模板与上面的契约段合并在**同一个** [SUBMIT
        # CONTRACT] 块内（不另起段落 ⇒ dispatch/claim 的整块去重仍然有效，
        # 不会出现两段契约区）。
        lines.append(
            "- 可照抄回执模板（把〈槽位〉换成本任务真实值，其余原样提交——"
            f'提交界面的模板照此填）：deliveryContract={{"summary": '
            f'"{_DC_TEMPLATE_SUMMARY}", "test": "{_DC_TEMPLATE_TEST}"}}。'
            "摘要必须写真实改动，凭证 id 以 bash 回执为准；只抄结构不抄槽位。"
        )
    lines.append(
        "- 无法满足时不要硬试：让协调者 waive_attestation(taskId, "
        "evidenceAttestationId, reason) 正式豁免。"
    )
    return "\n".join(lines)
