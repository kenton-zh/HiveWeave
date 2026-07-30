"""Task attestation policy resolution."""
from __future__ import annotations

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

