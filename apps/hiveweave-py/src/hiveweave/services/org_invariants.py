"""Org hire/dismiss invariants — hard gates (标本兼治之本).

Soft prompts alone allowed flat CEO spans, duplicate 花名/roles, and
dismiss→rehire churn. These checks reject illegal org states at the tool
boundary.
"""

from __future__ import annotations

from typing import Any

# Align with coordinator IRON span prompt (5–7). Use 7 as hard max.
MAX_DIRECT_REPORTS = 7

# Flower names reserved for CEO/HR bootstrapping (Name Pool in coordinator.py).
# Matching is exact on the 花名 string; role "ceo"/"hr" agents also protected.
_RESERVED_FLOWER_NAMES = frozenset({
    # Style samples from prompt — block reuse while those agents are active
    "归零", "知远",
})


def _active(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in agents if (a.get("status") or "active") == "active"]


def validate_hire(
    *,
    agents: list[dict[str, Any]],
    name: str,
    role: str,
    permission_type: str,
    parent_id: str,
) -> str | None:
    """Return an error message if hire violates invariants, else None."""
    name = (name or "").strip()
    role = (role or "").strip()
    perm = (permission_type or "").strip().lower()
    parent_id = (parent_id or "").strip()
    active = _active(agents)

    if not name or not role:
        return "hire_agent requires non-empty name and role"

    # Reserved flower names (CEO identity etc.)
    if name in _RESERVED_FLOWER_NAMES:
        holders = [
            a for a in active
            if (a.get("name") or "") == name
            or (a.get("role") or "").lower() in ("ceo", "hr")
        ]
        if holders or name in _RESERVED_FLOWER_NAMES:
            # Always block reserved pool names for new hires
            return (
                f"花名 '{name}' is reserved (CEO/HR name pool). "
                "Invent a unique flower-name; do not reuse the reserved pool."
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
            if a.get("id") == parent_id:
                parent = a
                break

    if parent and (parent.get("status") or "") == "archived":
        return (
            f"Parent agent is archived ({parent.get('name')}). "
            "Choose an active coordinator as parentId."
        )

    # Executors must not report to CEO
    if perm == "executor" and parent:
        prole = (parent.get("role") or "").lower()
        if prole == "ceo":
            return (
                "Executors cannot report directly to CEO (span / org design). "
                "Set parentId to a coordinator (architect / tech lead / manager)."
            )

    # Span of control
    if parent_id:
        kids = [a for a in active if (a.get("parent_id") or "") == parent_id]
        if len(kids) >= MAX_DIRECT_REPORTS:
            return (
                f"Parent already has {len(kids)} active direct reports "
                f"(max {MAX_DIRECT_REPORTS}). Add a coordinator layer, or "
                "transfer/dismiss someone first."
            )

    return None
