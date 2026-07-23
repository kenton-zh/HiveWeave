"""Standalone helper functions for tool implementation.

Extracted from ToolExecutor methods so that @tool-registered functions
can use them without needing ``self``.
"""

from __future__ import annotations

import json
from typing import Any

from hiveweave.db import meta as meta_db


def coerce_to_list(v: Any) -> Any:
    """Coerce a value to list if it's a JSON string representing a list.

    LLMs sometimes pass list fields as JSON strings (e.g. '["a","b"]')
    instead of actual arrays. This validator parses them.
    """
    if v is None:
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return [v]
    return v


async def get_project_id(agent_id: str) -> str | None:
    """Resolve agent_id -> project_id via Meta DB."""
    return await meta_db.get_agent_project_id(agent_id)


async def resolve_agent_id(
    project_id: str,
    name_or_id: str,
    org_service: Any = None,
) -> str | None:
    """Resolve agent name/short_id/UUID to a real agent_id within a project.

    Priority: UUID exact -> short_id -> UUID prefix -> 花名 -> role.
    Returns the agent_id (UUID) or None if not found.
    """
    if not name_or_id:
        return None

    if org_service is None:
        from hiveweave.services.org import OrgService
        org_service = OrgService()

    agent = await org_service.resolve_agent_ref(project_id, name_or_id)
    if agent:
        return agent["id"]
    return None
