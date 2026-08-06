"""Actionable obligation queries."""
from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query

log = structlog.get_logger(__name__)


class ObligationsMixin:
    """get_actionable_obligations."""

    if TYPE_CHECKING:
        promote_assigned_created: Any
        _COLUMNS: Any
        _row: Any
        _is_verify_task: Any

    async def get_actionable_obligations(
        self, project_id: str, agent_id: str
    ) -> list[dict]:
        """Tasks this agent must act on now (open-task reminder / stall helpers).

        - As assignee: claimed | running | rework | verifying (VERIFY assignee)
          Assign = claim: assigned non-VERIFY tasks are promoted from created
          before this query. VERIFY stays created until merge/stale nudge.
        - As reviewer: submitted | reviewing (TEST11 #3 — obligation from submit)
        - As creator: submitted | reviewing | approved
          When reviewer_id is set and ≠ creator, review obligation sits on the
          reviewer only (creator keeps approved → merge).
          approved (non-VERIFY) = must git_worktree_merge (CREATOR_MUST_MERGE).
          VERIFY children never stay as creator merge obligations.
        Excludes blocked / closed / archived.
        Each dict includes role_hint: 'assignee' | 'reviewer' | 'creator'.
        """
        await _ensure_schema(project_id)
        # Heal legacy assign-without-claim rows so obligations stay consistent
        try:
            await self.promote_assigned_created(project_id, agent_id)
        except Exception as e:
            log.warning(
                "promote_assigned_created_on_obligations_failed",
                agent_id=agent_id,
                error=str(e),
            )
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0 AND ("
            "  (assignee_id = ? AND status IN "
            "   ('claimed','running','rework','verifying'))"
            "  OR (reviewer_id = ? AND status IN ('submitted','reviewing'))"
            "  OR (creator_id = ? AND status IN "
            "   ('submitted','reviewing','approved'))"
            ") ORDER BY updated_at DESC",
            [agent_id, agent_id, agent_id],
        )
        out: list[dict] = []
        for r in rows:
            d = self._row(r)
            status = d.get("status")
            if d.get("assignee_id") == agent_id and status in (
                "claimed", "running", "rework", "verifying",
            ):
                # verifying on non-VERIFY assignee is not actionable for them
                if status == "verifying" and not self._is_verify_task(d):
                    continue
                d["role_hint"] = "assignee"
            elif d.get("reviewer_id") == agent_id and status in (
                "submitted", "reviewing",
            ):
                # reviewer obligation from submit onward (TEST11 #3)
                d["role_hint"] = "reviewer"
            else:
                # Creator merge obligation: skip VERIFY (closed on approve)
                if status == "approved" and self._is_verify_task(d):
                    continue
                # Designated reviewer ≠ creator owns the review window
                if status in ("submitted", "reviewing"):
                    rid = d.get("reviewer_id")
                    if rid and rid != agent_id:
                        continue
                d["role_hint"] = "creator"
            out.append(d)
        return out

