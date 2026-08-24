"""waive_attestation / waive_merge

Split from tools/task_tools.py (AI-friendly package layout). Behavior unchanged.
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveweave.services import task as _task_svc
from hiveweave.tools.base import tool
from hiveweave.tools import helpers as _helpers

_coerce_to_list = _helpers.coerce_to_list
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

# Structured bulk tokens — not free-text intent scanning.
_BULK_TASK_TOKENS = frozenset({
    "*", "all", "ALL", "every", "EVERY", "全部", "所有",
})


def deny_if_not_single_task_id(raw: str | None) -> str | None:
    """Belt: one concrete task per waive. No project-wide gate shutdown."""
    s = str(raw or "").strip()
    if not s:
        return (
            "waive requires exactly one taskId. "
            "Cannot close gates for every task at once."
        )
    if s in _BULK_TASK_TOKENS:
        return (
            "Cannot waive all tasks at once. Pass one concrete taskId "
            "(UUID or 8-char prefix) after looking at that task."
        )
    if s.startswith("[") or any(ch in s for ch in ",;\n|"):
        return (
            "waive takes exactly one taskId per call. "
            "Cannot close gates for multiple tasks together — "
            "waive each task separately."
        )
    parts = s.split()
    if len(parts) >= 2:
        return (
            "waive takes exactly one taskId per call. "
            "Cannot close gates for multiple tasks together."
        )
    return None

# ── waive_attestation ────────────────────────────────────────


class WaiveAttestationParams(BaseModel):
    """Parameters for waive_attestation tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task whose attestation gate should be waived.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    reason: str = Field(
        description="Why this task is exempt (e.g. 'CLI 任务无 UI 可 browse，"
        "以 bash 验证日志替代'). Required for auditability.",
    )
    evidence_attestation_id: str = Field(
        default="",
        alias="evidenceAttestationId",
        description=(
            "Coordinator: REQUIRED id of test_run / browse_e2e / visual_check / "
            "doc_review bound to this task. CEO: optional — looking at this "
            "task then waive_attestation(taskId) is enough; still one task "
            "per call. Pure read_file is not accepted for coordinators."
        ),
        json_schema_extra={
            "aliases": [
                "evidenceAttestationId",
                "evidence_attestation_id",
                "attestationId",
                "attestation_id",
            ]
        },
    )


async def _agent_has_open_verify(project_id: str, agent_id: str) -> bool:
    """名下有未闭环 VERIFY 验收义务（E5 收口纪律判定用）。"""
    try:
        ts = _task_svc.TaskService()
        open_tasks = await ts.get_open_work_obligations(project_id, agent_id)
        if not open_tasks:
            return False
        return any(ts._is_verify_task(t) for t in open_tasks)
    except Exception as e:
        log.warning("open_verify_scan_failed", agent_id=agent_id, error=str(e))
        return False


@tool(
    "waive_attestation",
    "Waive the attestation gate for ONE task (coordinator/CEO). "
    "Always pass a single taskId — never all tasks. "
    "CEO may omit evidenceAttestationId after looking at that task "
    "(browse then waive this id; that is how 'no QA hire' is recorded). "
    "Coordinators must cite evidenceAttestationId "
    "(test_run/browse_e2e/visual_check/doc_review). "
    "Max 2 waivers per task. The waiving agent CANNOT later approve "
    "the same task (third-party isolation) unless small-team sole reviewer. "
    "VERIFY waive is CEO-only. Prefer attest_doc_review for document "
    "VERIFY unless CEO waives that one task.",
    requires_workspace=False,
    security_level="standard",
)
async def waive_attestation_tool(
    params: WaiveAttestationParams, agent_id: str, workspace: str
) -> ToolResult:
    """Coordinator 显式豁免任务的 attestation 门禁（可审计的正式通道）。

    替代过去的 charter 口头豁免（工具层不读 charter，口头豁免无效）。
    """
    from hiveweave.services.attestation import (
        AmbiguousAttestationId,
        BROWSE_E2E_KIND,
        MAX_WAIVERS_PER_TASK,
        VISUAL_CHECK_KIND,
        WAIVER_EVIDENCE_KINDS,
        attestation_service,
        count_waivers,
        create_waiver,
        has_valid_waiver,
    )
    from hiveweave.services.org import OrgService
    from hiveweave.services.policy import infer_role_family

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    bulk = deny_if_not_single_task_id(params.task_id)
    if bulk:
        return ToolResult.err(bulk)

    reason = (params.reason or "").strip()
    if not reason:
        return ToolResult.err(
            "waive_attestation requires a non-empty 'reason' (auditability)."
        )
    if len(reason) < 20:
        return ToolResult.err(
            "waive_attestation reason too short (min 20 chars). "
            "State what was checked and why this one task is exempt."
        )

    try:
        agent = await OrgService().get_agent(agent_id)
    except Exception:
        agent = None
    family = infer_role_family(agent or {})
    ceo_override = family == "ceo"

    evidence_id = (params.evidence_attestation_id or "").strip()
    if not evidence_id and not ceo_override:
        return ToolResult.err(
            "waive_attestation requires evidenceAttestationId — cite a "
            "test_run / browse_e2e / visual_check / doc_review attestation. "
            "Only CEO may omit evidence after looking at this one task."
        )

    ts = _task_svc.TaskService()
    try:
        task = await ts.get_task(project_id, params.task_id)
    except Exception as e:
        return ToolResult.err(f"Failed to load task: {e}")
    if not task:
        return ToolResult.err(f"Task not found: {params.task_id}")

    # E3 waiver 治理（复盘致命链一）：verdict=FAIL 的结论不可豁免。
    # waiver 的豁免边界是「凭证缺失」，永不覆盖「结论不合格」——FAIL 只能
    # 走 rework 修复翻转 verdict。get_task 已把 evidence 反序列化为 dict，
    # normalize_verdict 统一大小写（存量小写形态同样拦截）。
    from hiveweave.services.tasks.verify import normalize_verdict

    ev = task.get("evidence") or {}
    if isinstance(ev, dict) and normalize_verdict(ev.get("verdict")) == "FAIL":
        return ToolResult.err(
            "waive_attestation rejected: task evidence verdict=FAIL——"
            "结论不合格不可豁免（waiver 只豁免凭证缺失，请走 rework 返修）"
        )

    # E5 断流收口纪律（复盘致命链二）：turn 刚被打断（降级中）且名下有
    # 未闭环 VERIFY → 拒绝就地 waiver 收口——强制续跑重验或显式升级
    # coordinator，不许带着未闭环的验收义务走豁免捷径。
    from hiveweave.agents.recovery import is_degraded

    if is_degraded(agent_id) and await _agent_has_open_verify(
        project_id, agent_id
    ):
        return ToolResult.err(
            "waive_attestation rejected: 你所在 turn 刚被断流/打断（降级中）"
            "且名下仍有未闭环 VERIFY 验收义务——禁止就地 waiver 收口。"
            "可执行两步：① 续跑完成这一轮（正常完成一轮后平台自动清除降级"
            "标志），完成验收后再提交；② 或显式升级 coordinator/CEO 处理。"
        )

    # Lifetime cap — escape hatch must stay narrower than the front door
    prior = await count_waivers(project_id, params.task_id)
    if prior >= MAX_WAIVERS_PER_TASK:
        return ToolResult.err(
            f"waive_attestation rejected: task {params.task_id} already has "
            f"{prior} waiver(s) (max {MAX_WAIVERS_PER_TASK}). "
            "Obtain real attestation evidence instead."
        )
    if await has_valid_waiver(project_id, params.task_id):
        waived_by = agent_id
        try:
            from hiveweave.services.attestation import get_valid_waiver

            wr = await get_valid_waiver(project_id, params.task_id)
            if wr and wr.get("agent_id"):
                waived_by = str(wr["agent_id"])
        except Exception:
            pass
        tip = await _format_post_waive_approve_tip(
            project_id,
            waived_by=waived_by,
            assignee_id=str(task.get("assignee_id") or "") or None,
            caller_id=agent_id,
        )
        return ToolResult.err(
            f"waive_attestation rejected: task {params.task_id} already has "
            "an unexpired waiver. Wait for expiry or approve via a different "
            "agent (waived_by cannot approve)."
            + tip
        )

    # Evidence attestation must be a real execution kind (not another waiver).
    # CEO may omit evidence after looking at this one task.
    if evidence_id:
        try:
            await attestation_service.ensure_schema(project_id)
            ev = await attestation_service.get(project_id, evidence_id)
        except AmbiguousAttestationId as e:
            return ToolResult.err(str(e))
        except Exception as e:
            return ToolResult.err(f"Failed to load evidence attestation: {e}")
        if not ev:
            return ToolResult.err(
                f"evidenceAttestationId not found: {evidence_id}"
            )
        ev_kind = (ev.get("kind") or "").strip()
        if ev_kind not in WAIVER_EVIDENCE_KINDS:
            return ToolResult.err(
                f"evidenceAttestationId kind '{ev_kind}' is not execution evidence. "
                f"Allowed: {sorted(WAIVER_EVIDENCE_KINDS)}."
            )
        ev_task = ev.get("task_id")
        # Same binding matrix as verify_ids (not "must equal waived task"):
        # same-agent different-task OK if commit rule passes when hash present;
        # different-agent same-task OK; different-agent different-task reject.
        # Do not require evidence.agent_id == waiving agent.
        if not ev_task:
            return ToolResult.err(
                f"evidenceAttestationId must be bound to a task "
                f"(attestation task_id={ev_task!r}, waive for {params.task_id}). "
                "Null evidence cannot unlock an arbitrary task."
            )
        from hiveweave.services.attestation import check_attestation_reuse_binding

        bind_ok, bind_err = await check_attestation_reuse_binding(
            project_id,
            ev,
            expected_task_id=params.task_id,
            expected_agent_id=agent_id,
        )
        if not bind_ok:
            return ToolResult.err(bind_err)
        ev_exit = ev.get("exit_code")
        if (
            ev_exit is not None
            and int(ev_exit) != 0
            and ev_kind in ("test_run", VISUAL_CHECK_KIND, BROWSE_E2E_KIND)
        ):
            return ToolResult.err(
                f"evidenceAttestationId {evidence_id} is a failed {ev_kind} "
                f"(exit_code={ev_exit}); cannot unlock waiver."
            )

    tags = task.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    from hiveweave.services.attestation import resolve_task_policy

    policy_id = task.get("policy_id") or resolve_task_policy(
        title=task.get("title") or "",
        tags=tags if isinstance(tags, list) else [],
        description=task.get("description") or "",
    )
    if (
        not ceo_override
        and (
            policy_id == "docs_only"
            or (isinstance(tags, list) and "docs_only" in tags)
        )
    ):
        return ToolResult.err(
            f"Cannot waive docs_only task {params.task_id}: "
            "use attest_doc_review(taskId, files=[{{path}}]) then "
            "submit/approve with attestationIds. Only CEO may waive "
            "a document task (one taskId at a time)."
        )

    is_verify = ts._is_verify_task(task)
    if is_verify and not ceo_override:
        return ToolResult.err(
            f"VERIFY task {params.task_id}: only CEO may waive_attestation "
            "(identity / attestation last resort). Coordinators must require "
            "test_run / browse_e2e attestationIds, or escalate to CEO with "
            "an auditable reason."
        )

    stored_reason = (
        f"[evidence={evidence_id}] {reason}"
        if evidence_id
        else f"[ceo_look] {reason}"
    )
    try:
        waiver_id = await create_waiver(
            project_id,
            task_id=params.task_id,
            waived_by=agent_id,
            reason=stored_reason,
        )
    except Exception as e:
        return ToolResult.err(f"Failed to create waiver: {e}")

    # Best-effort: mark / ensure verification case when this is a VERIFY task
    try:
        if is_verify:
            from hiveweave.services.task import VerificationCaseService

            parent_id = task.get("parent_task_id") or params.task_id
            vcs = VerificationCaseService()
            await vcs.ensure_case(
                project_id,
                original_task_id=parent_id,
                verify_task_id=params.task_id,
            )
            await vcs.mark_waived(
                project_id,
                parent_id,
                reason=reason,
                verify_task_id=params.task_id,
            )
    except Exception:
        pass

    # 通知 assignee 现在可以无 attestationIds 提交（task 通道，wake=1）
    assignee = task.get("assignee_id")
    if assignee and assignee != agent_id:
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().send_message(
                from_agent_id=agent_id,
                to_agent_id=assignee,
                message=(
                    f"[TASK] Attestation gate waived for task "
                    f"'{(task.get('title') or '')[:60]}' ({params.task_id}). "
                    f"Reason: {reason[:200]}. You may now submit_task without "
                    f"attestationIds (bash 验证日志已在 summary 中说明)。"
                ),
                message_type="task",
                priority="normal",
                task_id=params.task_id,
            )
        except Exception as e:
            log.warning("waiver_notify_failed", error=str(e))

    log.info(
        "attestation_waived",
        project_id=project_id,
        task_id=params.task_id,
        waived_by=agent_id,
        reason=reason[:120],
        is_verify=is_verify,
    )

    # TEST18 NEW-9: do not make the AI guess who can approve — list them.
    next_tip = await _format_post_waive_approve_tip(
        project_id,
        waived_by=agent_id,
        assignee_id=str(assignee) if assignee else None,
    )

    return ToolResult.ok(
        f"Attestation waived for task {params.task_id} "
        f"(waiver {waiver_id[:8]}, expires in 24h).\n"
        f"Stored reason (quote this in reports): {reason}\n"
        f"Assignee may now submit_task without attestationIds."
        + (
            " CEO look-waiver: this task only — other tasks keep their gates."
            if ceo_override and not evidence_id
            else ""
        )
        + (
            " VERIFY waive is CEO-only and leaves an auditable verification_case."
            if is_verify
            else ""
        )
        + next_tip
    )


async def _format_post_waive_approve_tip(
    project_id: str,
    *,
    waived_by: str,
    assignee_id: str | None,
    caller_id: str | None = None,
) -> str:
    """Concrete NEXT ACTION after waive — name people, do not say '找人'.

    BUG-WAIVE-TIP（2026-08-05 DevBlog 死锁根因）：幂等拒绝分支里，调用者
    本人常在合法 approver 名单内（典型：waiver 已由 CEO 签发，协调者误判
    「我也得签一次 waiver / 我得自己跑出 test_run」而重复调用本工具）。
    旧提示一律让他「去 ask 名单里的人」——当名单里的人就是他自己时，
    等于让他 ask 自己，死锁于此。现在 caller 在名单内时直接告知：
    你就是合法第三方 approver，立即 review_task(approve)，无需新 waiver、
    无需自跑测试（waived 路径跳过审查方 attestation 校验）。
    """
    from hiveweave.services.org import OrgService
    from hiveweave.services.unblock_soft import (
        is_small_team_sole_reviewer,
        list_review_capable_agent_ids,
    )

    # Small-team sole REVIEW holder may self-approve their own waiver.
    try:
        sole = await is_small_team_sole_reviewer(
            project_id,
            assignee_id=assignee_id,
            reviewer_id=waived_by,
        )
    except Exception:
        sole = False
    if sole:
        return (
            "\n\nNEXT ACTION: You are the sole REVIEW holder besides the "
            "assignee — you MAY review_task(approve) yourself "
            "(small-team exemption; will stamp waive_self_approve_small_team). "
            "Do NOT invent a fake rework to escape."
        )

    excl: set[str] = {str(waived_by)}
    if assignee_id:
        excl.add(str(assignee_id))
    holders = await list_review_capable_agent_ids(
        project_id, exclude_ids=excl
    )
    if holders is None:
        return (
            "\n\nNEXT ACTION: You CANNOT approve this task yourself "
            "(waived_by third-party rule). Org roster unreadable right now — "
            "retry, then ask another REVIEW holder to approve. "
            "Do NOT invent a fake rework."
        )
    if not holders:
        return (
            "\n\nNEXT ACTION: You CANNOT approve this task yourself "
            "(waived_by third-party rule), and no other active REVIEW holder "
            "exists beside the assignee. Approve path is deadlocked — escalate "
            "to CEO or cancel_task with an audited deadlock reason "
            "(≥20 chars). Do NOT invent a fake rework."
        )

    # BUG-WAIVE-TIP: caller 本人在合法 approver 名单里 → 别再让他「去找人」，
    # 直接告诉他现在就能 approve（幂等拒绝分支的典型死锁破解）。
    if caller_id and str(caller_id) in {str(h) for h in holders}:
        return (
            "\n\nNEXT ACTION: A valid waiver already exists and YOU are a "
            "lawful third-party approver (you are neither waived_by nor the "
            "assignee). Do NOT request another waiver and do NOT re-run "
            "tests — the waived path skips reviewer-attestation checks. "
            "Approve NOW via review_task(decision=\"approve\", feedback=...), "
            "then git_worktree_merge the assignee's branch."
        )

    org = OrgService()
    lines: list[str] = [
        "\n\nNEXT ACTION: You CANNOT approve this task yourself "
        "(waived_by third-party rule). Ask ONE of these REVIEW holders "
        "to review_task(approve) — do NOT invent a fake rework:",
    ]
    for hid in holders[:8]:
        row = None
        try:
            row = await org.get_agent(hid)
        except Exception:
            row = None
        name = (row or {}).get("name") or hid[:8]
        short = (row or {}).get("short_id") or "?"
        role = ((row or {}).get("role") or "")[:40]
        lines.append(
            f"  - {name} ({short}) id={hid}"
            + (f" role={role}" if role else "")
        )
    lines.append(
        "Use ask_agent / send_message to one of the above with the taskId."
    )
    return "\n".join(lines)


# ── waive_merge ───────────────────────────────────────────────


class WaiveMergeParams(BaseModel):
    """Parameters for waive_merge tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the approved task whose merge-before-close gate "
        "should be waived.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    reason: str = Field(
        description=(
            "Auditable reason merge is not required (min 20 chars). "
            "E.g. docs-only delivery already on main via prior commit abc123."
        ),
    )


@tool(
    "waive_merge",
    "Last-resort waiver of the merge-before-close hard gate "
    "(coordinator/CEO). Prefer git_worktree_merge. After waiving, "
    "close_task may proceed; evidence records merge_waived for audit.",
    requires_workspace=False,
    security_level="standard",
)
async def waive_merge_tool(
    params: WaiveMergeParams, agent_id: str, workspace: str
) -> ToolResult:
    """Explicit merge exemption — the only escape from close hard gate."""
    import time as _time

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    bulk = deny_if_not_single_task_id(params.task_id)
    if bulk:
        return ToolResult.err(bulk)

    reason = (params.reason or "").strip()
    if not reason:
        return ToolResult.err(
            "waive_merge requires a non-empty 'reason' (auditability)."
        )
    if len(reason) < 20:
        return ToolResult.err(
            "waive_merge reason too short (min 20 chars). "
            "State what was checked and why merge is not required."
        )

    ts = _task_svc.TaskService()
    try:
        task = await ts.get_task(project_id, params.task_id)
    except Exception as e:
        return ToolResult.err(f"Failed to load task: {e}")
    if not task:
        return ToolResult.err(f"Task not found: {params.task_id}")

    if ts._is_verify_task(task):
        return ToolResult.err(
            f"VERIFY task {params.task_id}: waive_merge does not apply "
            "(VERIFY has no merge step). Use waive_attestation if needed."
        )

    ev = task.get("evidence") or {}
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = {}
    if not isinstance(ev, dict):
        ev = {}
    ev["merge_waived"] = True
    ev["merge_waive_reason"] = reason[:500]
    ev["merge_waived_by"] = agent_id
    ev["merge_waived_at"] = int(_time.time() * 1000)

    try:
        from hiveweave.services import task as task_module

        await task_module._execute(
            project_id,
            "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
            # task["id"] is the resolved stored id (get_task resolves short
            # prefixes); params.task_id may be an 8-char prefix that would
            # silently match 0 rows here.
            [json.dumps(ev), int(_time.time() * 1000), str(task["id"])],
        )
    except Exception as e:
        return ToolResult.err(f"Failed to stamp merge waiver: {e}")

    # Fulfill pending merge obligation so exit gates clear
    try:
        from hiveweave.services.obligation import ObligationLedger

        await ObligationLedger().fulfill(project_id, params.task_id, "merge")
    except Exception as e:
        log.warning("waive_merge_fulfill_failed", error=str(e))

    log.info(
        "merge_waived",
        project_id=project_id,
        task_id=params.task_id,
        waived_by=agent_id,
        reason=reason[:120],
    )

    # BUG-MERGE-WAIVE-CLOSE（2026-08-05 feature-test 停滞根因）：旧回执让
    # agent 去「close_task / approve auto-close」——但 agent 工具表里没有
    # close_task，approve auto-close 也只发生在 approve 当下。若 waive_merge
    # 发生在 approve 之后（典型顺序：approve 时 worktree 仍有 ahead → 未
    # auto-close → CEO 随后 waive merge），任务永远停在 approved 95%，
    # 无任何在册义务触发后续动作。waive_merge 是最后一个能一致化账本的
    # 位置：任务已 approved 时直接代闭环（close_task 内部 merge gate 会因
    # merge_waived 放行；VERIFY spawn 本来就不属于 waive 语义）。
    auto_closed = False
    close_err: str | None = None
    try:
        cur = await ts.get_task(project_id, params.task_id)
        if cur and cur.get("status") == "approved":
            await ts.close_task(
                project_id, params.task_id, reason_code="merge_waived"
            )
            auto_closed = True
    except Exception as e:
        close_err = str(e)
        log.warning(
            "waive_merge_auto_close_failed",
            task_id=params.task_id,
            error=close_err,
        )

    # 2026-08-11 slack-clone_01 复盘：waive 后残留的 [MERGE PENDING] 消息
    # 无人清理（merge 路径靠 verify._clear_merge_pending_inbox，waive 不走
    # 那条路）——审批人/creator inbox 永远挂着一条"已读未完成"的 merge
    # 指令。auto-close 成功后按 verify.py 同款清理 creator 的残留。
    if auto_closed:
        try:
            from hiveweave.services.inbox import InboxService
            from hiveweave.services.tasks.verify import resolve_merge_owner

            # 清理目标与 _inject_merge_pending_wake 的接收者一致（共享
            # resolve_merge_owner）：creator → reviewer → 执行 waive 的 agent。
            await InboxService().supersede_watchdog_messages(
                resolve_merge_owner(
                    task, task.get("reviewer_id") or agent_id
                )
                or agent_id,
                prefixes=["[MERGE PENDING]", "[MERGE PROXY]"],
                contains=str(task["id"])[:8],
            )
        except Exception as e:
            log.warning(
                "waive_merge_clear_pending_failed",
                task_id=task.get("id"),
                error=str(e),
            )

    msg = (
        f"Merge waived for task {params.task_id}. "
        f"Stored reason: {reason}\n"
    )
    if auto_closed:
        msg += (
            "Task was approved — auto-closed now (ledger complete). "
            "No further action needed."
        )
    elif close_err:
        msg += (
            f"Auto-close failed ({close_err}); task remains approved. "
            "Escalate to operator if it stays stuck."
        )
    else:
        msg += (
            "Task is not yet approved — after approval the ledger can "
            "close without git_worktree_merge."
        )
    return ToolResult.ok(msg)

