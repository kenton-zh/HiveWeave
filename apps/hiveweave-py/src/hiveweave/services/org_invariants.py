"""Org hire/dismiss invariants — hard gates (标本兼治之本).

P0 Hard Gates extensions:
- HR cannot be a parent
- Flower name must not equal role / contain job-title literals
- At most one primary coordinator under the same parent
"""

from __future__ import annotations

import re
from typing import Any

from hiveweave.services.policy import is_test_engineer_role

# Align with coordinator IRON span prompt (5–7). Use 7 as hard max.
MAX_DIRECT_REPORTS = 7

_RESERVED_FLOWER_NAMES = frozenset({
    "归零", "知远",
})

# Names that look like job titles (forbidden as 花名)
_JOB_TITLE_LITERALS = (
    "工程师", "负责人", "测试", "架构师", "经理", "专员",
    "engineer", "manager", "lead", "architect", "director",
    "ceo", "hr", "qa",
)


def _active(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in agents if (a.get("status") or "active") == "active"]


def _is_hr_agent(agent: dict[str, Any] | None) -> bool:
    if not agent:
        return False
    role = (agent.get("role") or "").strip().lower()
    return role == "hr" or "人力资源" in (agent.get("role") or "")


def _normalize_role_key(role: str) -> str:
    return re.sub(r"\s+", "", (role or "").strip().lower())


def validate_hire(
    *,
    agents: list[dict[str, Any]],
    name: str,
    role: str,
    permission_type: str,
    parent_id: str,
    bootstrap: bool = False,
) -> str | None:
    """Return an error message if hire violates invariants, else None.

    ``bootstrap=True`` skips reserved-name / HR-parent / flower-name checks
    for CEO/HR seed creation.
    """
    name = (name or "").strip()
    role = (role or "").strip()
    perm = (permission_type or "").strip().lower()
    parent_id = (parent_id or "").strip()
    active = _active(agents)

    if not name or not role:
        return "hire_agent requires non-empty name and role"

    if not bootstrap:
        # Reserved flower names (CEO identity etc.)
        if name in _RESERVED_FLOWER_NAMES:
            return (
                f"花名 '{name}' is reserved (CEO/HR name pool). "
                "Invent a unique flower-name; do not reuse the reserved pool."
            )

        # Flower name must not equal role (case-insensitive)
        if name.lower() == role.lower():
            return (
                f"花名 '{name}' must not equal role '{role}'. "
                "Use a personal flower-name, not the job title."
            )

        # Flower name must not contain job-title literals
        name_l = name.lower()
        for lit in _JOB_TITLE_LITERALS:
            if lit.lower() in name_l or lit in name:
                return (
                    f"花名 '{name}' looks like a job title (contains '{lit}'). "
                    "Use a personal flower-name e.g. 潮汐 / 墨白."
                )

    # Unique active 花名
    for a in active:
        if (a.get("name") or "").lower() == name.lower():
            return (
                f"Active agent already named '{name}' "
                f"({a.get('short_id')}, role={a.get('role')}). "
                "Use a unique 花名, or transfer_agent / dismiss the existing "
                "person first (prefer transfer over dismiss+rehire)."
            )

    # One module one owner: unique active executor role string
    if perm == "executor":
        bare = {"前端工程师", "后端工程师", "工程师", "frontend", "backend", "developer"}
        if role in bare or role.lower() in bare:
            return (
                f"Executor role '{role}' is too generic. "
                "Use module+craft form e.g. 「签到排行榜工程师」."
            )
        for a in active:
            if (
                a.get("permission_type") == "executor"
                and (a.get("role") or "") == role
            ):
                return (
                    f"Active executor already owns role '{role}' "
                    f"({a.get('name')}/{a.get('short_id')}). "
                    "One module → one owner. transfer_agent or dismiss the "
                    "existing owner before hiring a replacement."
                )

    # Resolve parent
    parent: dict[str, Any] | None = None
    if parent_id:
        for a in agents:
            if a.get("id") == parent_id or a.get("short_id") == parent_id:
                parent = a
                break

    if parent and (parent.get("status") or "") == "archived":
        return (
            f"Parent agent is archived ({parent.get('name')}). "
            "Choose an active coordinator as parentId."
        )

    # HR never has children
    if not bootstrap and parent and _is_hr_agent(parent):
        return (
            "HR cannot have subordinates (IRON RULE). "
            "Set parentId to CEO or a domain coordinator, not HR."
        )

    # Executors must not report to CEO (hire + transfer share this rule)
    if perm == "executor" and parent:
        ceo_err = _executor_under_ceo_error(parent)
        if ceo_err:
            return ceo_err

    # At most one primary coordinator under the same parent
    # (approximate domain by parent_id; exclude CEO/HR unique roles)
    if (
        not bootstrap
        and perm == "coordinator"
        and parent_id
        and role.lower() not in ("ceo", "hr")
    ):
        role_key = _normalize_role_key(role)
        for a in active:
            if (
                a.get("permission_type") == "coordinator"
                and (a.get("parent_id") or "") == (parent.get("id") if parent else parent_id)
                and _normalize_role_key(a.get("role") or "") == role_key
            ):
                return (
                    f"Parent already has coordinator with role '{role}' "
                    f"({a.get('name')}/{a.get('short_id')}). "
                    "One primary coordinator per domain/parent. "
                    "transfer_agent or dismiss before hiring a duplicate."
                )

    # Span of control
    if parent_id:
        pid = parent.get("id") if parent else parent_id
        kids = [a for a in active if (a.get("parent_id") or "") == pid]
        if len(kids) >= MAX_DIRECT_REPORTS:
            return (
                f"Parent already has {len(kids)} active direct reports "
                f"(max {MAX_DIRECT_REPORTS}). Add a coordinator layer, or "
                "transfer/dismiss someone first."
            )

    return None


def _executor_under_ceo_error(parent: dict[str, Any] | None) -> str | None:
    """Hard rule: executors never report directly to CEO."""
    if not parent:
        return None
    prole = (parent.get("role") or "").strip().lower()
    if prole == "ceo":
        return (
            "Executors cannot report directly to CEO (span / org design). "
            "Set parentId to a coordinator (architect / tech lead / manager)."
        )
    return None


def validate_transfer(
    *,
    agents: list[dict[str, Any]],
    agent_id: str,
    new_parent_id: str | None,
) -> str | None:
    """Return error if transfer would break org invariants, else None.

    Mirrors hire gates that apply to re-parenting (executor↛CEO, no HR parent,
    span max). Does not re-check flower-name uniqueness.
    """
    active = _active(agents)
    target: dict[str, Any] | None = None
    for a in agents:
        if a.get("id") == agent_id or a.get("short_id") == agent_id:
            target = a
            break
    if not target:
        return f"Agent not found: {agent_id}"

    new_parent_id = (new_parent_id or "").strip() or None
    parent: dict[str, Any] | None = None
    if new_parent_id:
        for a in agents:
            if a.get("id") == new_parent_id or a.get("short_id") == new_parent_id:
                parent = a
                break
        if parent is None:
            return f"New parent not found: {new_parent_id}"
        if (parent.get("status") or "") == "archived":
            return (
                f"Parent agent is archived ({parent.get('name')}). "
                "Choose an active coordinator as newParentId."
            )
        if _is_hr_agent(parent):
            return (
                "HR cannot have subordinates (IRON RULE). "
                "Set newParentId to CEO or a domain coordinator, not HR."
            )

    perm = (target.get("permission_type") or "").strip().lower()
    if perm == "executor":
        ceo_err = _executor_under_ceo_error(parent)
        if ceo_err:
            return ceo_err
        # Also reject transfer to root (no parent) — that is CEO-equivalent span
        if parent is None:
            return (
                "Executors cannot be root / report to no one. "
                "Set newParentId to a coordinator (architect / tech lead / manager)."
            )

    if new_parent_id and parent:
        pid = parent.get("id") or new_parent_id
        kids = [
            a for a in active
            if (a.get("parent_id") or "") == pid and a.get("id") != agent_id
        ]
        if len(kids) >= MAX_DIRECT_REPORTS:
            return (
                f"Parent already has {len(kids)} active direct reports "
                f"(max {MAX_DIRECT_REPORTS}). Add a coordinator layer, or "
                "transfer/dismiss someone first."
            )

    return None
