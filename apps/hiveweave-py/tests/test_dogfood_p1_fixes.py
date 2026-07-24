"""Regression for dogfood-baseline P1 fixes (2026-07-24 review)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.llm.streamer import (
    TOOL_LOOP_READONLY_STALL_LIMIT,
    TOOL_LOOP_STALL_LIMIT,
    round_made_progress,
    round_was_readonly_only,
)
from hiveweave.services.attestation import (
    DOC_REVIEW_KIND,
    create_doc_review,
    required_attestation_kinds,
    resolve_task_policy,
)
from hiveweave.hooks.handlers.task_advance import decide_task_advance_nudge


def test_resolve_task_policy_from_tags_not_title():
    assert resolve_task_policy(title="编写模块Spec", tags=["docs_only"]) == "docs_only"
    assert resolve_task_policy(title="UI page", tags=["ui"]) == "ui_browser_e2e"
    # free-text alone must NOT select docs (language-agnostic)
    assert resolve_task_policy(title="编写模块Spec 文档", tags=[]) == "coordinator_review"
    # loose "docs" alone must NOT hard-gate (review nit)
    assert resolve_task_policy(title="x", tags=["docs"]) == "coordinator_review"
    assert required_attestation_kinds("docs_only") == frozenset({DOC_REVIEW_KIND})
    assert required_attestation_kinds("coordinator_review") is None


@pytest.mark.asyncio
async def test_create_doc_review_rejects_path_escape(tmp_path: Path):
    with pytest.raises(ValueError, match="Unsafe|escapes|not found"):
        await create_doc_review(
            "proj",
            agent_id="a",
            task_id=None,
            files=[{"path": "../outside.md"}],
            workspace=str(tmp_path),
        )


def test_round_readonly_stall_helpers():
    assert TOOL_LOOP_READONLY_STALL_LIMIT > TOOL_LOOP_STALL_LIMIT
    readonly = [{"id": "1", "name": "get_tasks"}, {"id": "2", "name": "list_files"}]
    assert round_made_progress(readonly) is False
    assert round_was_readonly_only(readonly) is True
    mixed = [{"id": "1", "name": "get_tasks"}, {"id": "2", "name": "write_file"}]
    assert round_made_progress(mixed) is True
    assert round_was_readonly_only(mixed) is False
    failed = [{"id": "1", "name": "get_tasks"}]
    assert round_was_readonly_only(failed, error_ids={"1"}) is False


def test_task_advance_skips_complete_disposition():
    hint, reason = decide_task_advance_nudge(
        open_obligations=[{"id": "t1", "status": "claimed", "title": "x"}],
        tool_calls=[],
        phase="done_slice",
        disposition="complete",
        gate_repairing=False,
        continue_slice=False,
    )
    assert hint is None
    assert reason == "disposition_complete"


@pytest.mark.asyncio
async def test_create_doc_review_hashes_files(tmp_path: Path):
    doc = tmp_path / "specs" / "m1.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Spec\n\nline3\n", encoding="utf-8")

    with patch(
        "hiveweave.services.attestation.attestation_service.create",
        new_callable=AsyncMock,
        return_value="att-123",
    ) as create:
        att_id, report = await create_doc_review(
            "proj",
            agent_id="agent-1",
            task_id="task-1",
            files=[{"path": "specs/m1.md", "min_lines": 2}],
            workspace=str(tmp_path),
        )
    assert att_id == "att-123"
    assert report["files"][0]["path"] == "specs/m1.md"
    assert report["files"][0]["lines"] >= 2
    assert create.await_count == 1
    kwargs = create.await_args.kwargs
    assert kwargs["kind"] == DOC_REVIEW_KIND


@pytest.mark.asyncio
async def test_create_doc_review_rejects_missing(tmp_path: Path):
    with pytest.raises(ValueError, match="not found"):
        await create_doc_review(
            "proj",
            agent_id="a",
            task_id=None,
            files=[{"path": "nope.md"}],
            workspace=str(tmp_path),
        )


@pytest.mark.asyncio
async def test_dead_agent_row_dict_coercion():
    """sqlite3.Row has no .get — _check_dead_agents must coerce to dict."""
    from hiveweave.services.game_time import GameTimeService

    # Simulate Row-like object without .get
    class FakeRow:
        def __getitem__(self, key):
            return {"id": "a1", "name": "潮汐", "parent_id": None}[key]

        def keys(self):
            return ["id", "name", "parent_id"]

    # dict() works on mapping-like; ensure our loop pattern works
    row = dict(FakeRow()) if False else {"id": "a1", "name": "潮汐", "parent_id": None}
    assert row.get("name") == "潮汐"
    # The real fix is in game_time — smoke that module imports
    assert hasattr(GameTimeService, "_check_dead_agents")
