"""Soft unblock: non-specific reminders + hard forbids (no next-actor routing).

Platform may state *facts* and *refuse* unlawful shortcuts. It must NOT
emit structured next_action / candidate lists / auto-dispatch.

TEST6 audit (2026-07-30): hard gates must be deadlock-free in combination.
When no lawful approver exists, cancel is an audited escape hatch.
"""

from __future__ import annotations

from typing import Any

# Non-specific reminder — AI chooses the lawful advance path.
REVIEW_PATH_BLOCKED_REMINDER = (
    "审批路径受阻且证据仍在；勿以 cancel 清场；请自行选择合法推进方式。"
)

# Statuses where work is waiting on review — cancel is the wrong escape hatch
# when machine evidence (waiver or attestation) already exists.
_REVIEW_PIPE_STATUSES = frozenset({"submitted", "reviewing"})

# Minimum reason length when cancel is allowed via deadlock exemption.
DEADLOCK_CANCEL_REASON_MIN = 20

# Sentinel: org roster unreadable — cancel escape must fail-closed.
_ORG_LOOKUP_FAILED = "__org_lookup_failed__"


async def list_review_capable_agent_ids(
    project_id: str,
    *,
    exclude_ids: set[str] | None = None,
) -> list[str] | None:
    """Active agents with REVIEW capability, optionally excluding ids.

    Returns ``None`` when the org roster could not be read (caller must
    fail-closed for cancel escape — TEST6 audit fix).
    Returns ``[]`` when the roster was read and no eligible holders exist.
    """
    from hiveweave.services.org import OrgService
    from hiveweave.services.policy import Capability, has_capability

    excl = {str(x) for x in (exclude_ids or set()) if x}
    out: list[str] = []
    try:
        agents = await OrgService().list_agents(project_id)
    except Exception:
        return None
    for a in agents or []:
        if (a.get("status") or "").lower() == "archived":
            continue
        aid = str(a.get("id") or "")
        if not aid or aid in excl:
            continue
        if has_capability(a, Capability.REVIEW):
            out.append(aid)
    return out


async def is_small_team_sole_reviewer(
    project_id: str,
    *,
    assignee_id: str | None,
    reviewer_id: str,
) -> bool:
    """True when reviewer is the only REVIEW holder besides the assignee.

    TEST6 audit S2: small-team waive→self-approve exemption (audit-stamped).
    Org lookup failure → False (no exemption without a reliable roster).
    """
    excl: set[str] = set()
    if assignee_id:
        excl.add(str(assignee_id))
    holders = await list_review_capable_agent_ids(project_id, exclude_ids=excl)
    if holders is None:
        return False
    return len(holders) == 1 and holders[0] == str(reviewer_id)


async def no_lawful_approver(
    project_id: str,
    task: dict[str, Any],
    *,
    waiver_row: dict[str, Any] | None = None,
) -> str | None:
    """Return a deadlock diagnosis if no agent can lawfully approve, else None.

    Machine-checkable (TEST6 audit S3/S7):
    - Exclude assignee (self-review forever forbidden).
    - Eligible = active REVIEW holders minus assignee.
    - If a waiver exists: waived_by is excluded unless small-team sole reviewer.
    - Empty eligible set → deadlock.

    Returns the sentinel ``_ORG_LOOKUP_FAILED`` message when the org roster
    is unreadable — callers of cancel escape must treat this as NOT an
    escape (fail-closed).
    """
    assignee_id = str(task.get("assignee_id") or "") or None
    excl: set[str] = set()
    if assignee_id:
        excl.add(assignee_id)

    holders = await list_review_capable_agent_ids(project_id, exclude_ids=excl)
    if holders is None:
        return _ORG_LOOKUP_FAILED
    if not holders:
        return (
            "No lawful approver: no active agent with REVIEW capability "
            "besides the assignee (self-review forbidden)."
        )

    waived_by = ""
    if waiver_row:
        waived_by = str(waiver_row.get("agent_id") or "")
    elif task.get("id"):
        try:
            from hiveweave.services.attestation import get_valid_waiver

            wr = await get_valid_waiver(project_id, str(task["id"]))
            if wr:
                waived_by = str(wr.get("agent_id") or "")
                waiver_row = wr
        except Exception:
            pass

    if waived_by:
        # Small-team sole reviewer may self-approve their own waiver (S2).
        sole_ok = False
        if waived_by in holders:
            sole_ok = await is_small_team_sole_reviewer(
                project_id,
                assignee_id=assignee_id,
                reviewer_id=waived_by,
            )
        eligible = [
            h for h in holders
            if h != waived_by or sole_ok
        ]
        if not eligible:
            return (
                "No lawful approver: waiver issuer cannot approve "
                f"(waived_by={waived_by[:8]}…) and no other REVIEW holder "
                "exists beside the assignee. Deadlock: waive→approve needs "
                "a third party, or small-team sole-reviewer exemption."
            )
    return None


def is_org_lookup_failed(deadlock: str | None) -> bool:
    """True when no_lawful_approver could not read the org roster."""
    return deadlock == _ORG_LOOKUP_FAILED


async def review_deadlock_blocks_cancel(
    project_id: str,
    task: dict[str, Any],
    *,
    cancel_reason: str | None = None,
) -> str | None:
    """Return forbid reason if cancel would only clear a review deadlock.

    Machine-checkable (no NL intent scan):
    - task is in submitted/reviewing (review pipe)
    - AND a valid waiver exists OR evidence carries attestation_ids / tests_passed

    TEST6 audit S7: when no lawful approver exists, do NOT block — caller
    must pass a reason ≥ DEADLOCK_CANCEL_REASON_MIN chars; stamp
    ``cancelled_in_deadlock`` on the cancel path.

    Org roster unreadable → fail-closed (keep cancel blocked).
    """
    status = (task.get("status") or "").strip()
    if status not in _REVIEW_PIPE_STATUSES:
        return None

    tid = task.get("id")
    if not tid:
        return None

    from hiveweave.services.attestation import get_valid_waiver

    waiver_row = None
    try:
        waiver_row = await get_valid_waiver(project_id, str(tid))
    except Exception:
        pass

    has_evidence_block = False
    if waiver_row:
        has_evidence_block = True
    else:
        evidence = task.get("evidence") or {}
        if isinstance(evidence, str):
            import json

            try:
                evidence = json.loads(evidence)
            except Exception:
                evidence = {}
        if isinstance(evidence, dict):
            aids = evidence.get("attestation_ids") or []
            has_aids = isinstance(aids, list) and any(str(x).strip() for x in aids)
            tests_ok = evidence.get("tests_passed") is True
            if has_aids or tests_ok:
                has_evidence_block = True

    if not has_evidence_block:
        return None

    # S7: escape hatch when approve path is empty — but NOT when we cannot
    # determine the approver set (org lookup failed).
    deadlock = await no_lawful_approver(
        project_id, task, waiver_row=waiver_row
    )
    if is_org_lookup_failed(deadlock):
        return (
            f"cancel_task refused for task {str(tid)[:8]}: "
            f"cannot determine lawful approvers (org roster unreadable). "
            f"Retry after org is available; do not cancel to clear review. "
            f"{REVIEW_PATH_BLOCKED_REMINDER}"
        )
    if deadlock:
        reason = (cancel_reason or "").strip()
        if len(reason) < DEADLOCK_CANCEL_REASON_MIN:
            return (
                f"cancel_task refused for task {str(tid)[:8]}: "
                f"approve path is deadlocked ({deadlock}) but escape-hatch "
                f"cancel requires reason ≥{DEADLOCK_CANCEL_REASON_MIN} chars "
                f"explaining the deadlock (will stamp cancelled_in_deadlock)."
            )
        # Allow cancel — signal via sentinel return None; caller stamps audit.
        return None

    if waiver_row:
        return (
            f"cancel_task refused for task {str(tid)[:8]}: "
            f"review pipe ({status}) with an active waiver. "
            f"{REVIEW_PATH_BLOCKED_REMINDER}"
        )
    return (
        f"cancel_task refused for task {str(tid)[:8]}: "
        f"review pipe ({status}) still has execution evidence. "
        f"{REVIEW_PATH_BLOCKED_REMINDER}"
    )


async def cancel_allowed_due_to_approve_deadlock(
    project_id: str,
    task: dict[str, Any],
) -> bool:
    """True when review-pipe+evidence would block cancel, but no lawful approver.

    Org lookup failure → False (fail-closed; do not stamp cancelled_in_deadlock).
    """
    status = (task.get("status") or "").strip()
    if status not in _REVIEW_PIPE_STATUSES:
        return False

    from hiveweave.services.attestation import get_valid_waiver

    has_block = False
    try:
        if await get_valid_waiver(project_id, str(task.get("id") or "")):
            has_block = True
    except Exception:
        pass
    if not has_block:
        evidence = task.get("evidence") or {}
        if isinstance(evidence, str):
            import json

            try:
                evidence = json.loads(evidence)
            except Exception:
                evidence = {}
        if isinstance(evidence, dict):
            aids = evidence.get("attestation_ids") or []
            has_aids = isinstance(aids, list) and any(str(x).strip() for x in aids)
            if has_aids or evidence.get("tests_passed") is True:
                has_block = True
    if not has_block:
        return False
    deadlock = await no_lawful_approver(project_id, task)
    if is_org_lookup_failed(deadlock):
        return False
    return deadlock is not None


def soft_reminder_after_self_review_deny(*, has_waiver: bool) -> str:
    """Append soft reminder when self-review is denied (esp. after waiver)."""
    if not has_waiver:
        return ""
    return f"\n{REVIEW_PATH_BLOCKED_REMINDER}"
