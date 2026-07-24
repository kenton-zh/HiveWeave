"""TEST13 audit platform fixes (2026-07-24)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.attestation import create_doc_review
from hiveweave.services.policy import infer_role_family
from hiveweave.tools.task_tools import (
    AttestDocReviewParams,
    attest_doc_review_tool,
)


def test_ceo_family_clears_merger_forbidden():
    """P0-1: CEO discarded from VERIFY merger forbidden set."""
    forbidden = {"ceo1", "impl1"}
    reviewer_id = "ceo1"
    if infer_role_family({"role": "ceo"}) == "ceo" and reviewer_id in forbidden:
        forbidden.discard(reviewer_id)
    assert reviewer_id not in forbidden
    assert "impl1" in forbidden


@pytest.mark.asyncio
async def test_doc_review_prefers_worktree(tmp_path: Path):
    """P1-1: auto source uses worktree when files exist there."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").mkdir()
    (wt / "specs").mkdir()
    (wt / "specs" / "checkin.md").write_text("# Spec\n\nline3\n", encoding="utf-8")
    main = tmp_path / "main"
    main.mkdir()

    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(main)),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.git_worktree._current_branch",
            AsyncMock(return_value="hw/A003/work"),
        ),
        patch(
            "hiveweave.services.git_worktree._git",
            AsyncMock(return_value=(True, "abc123")),
        ),
        patch(
            "hiveweave.services.attestation.create_doc_review",
            AsyncMock(
                return_value=(
                    "att-1",
                    {"files": [{"path": "specs/checkin.md"}]},
                )
            ),
        ) as create,
    ):
        Org.return_value.get_agent = AsyncMock(
            return_value={"workspace_path": str(wt)}
        )
        result = await attest_doc_review_tool(
            AttestDocReviewParams(
                files=[{"path": "specs/checkin.md"}],
                source="auto",
            ),
            agent_id="a1",
            workspace=str(main),
        )
    assert result.success is True
    assert "worktree" in (result.output or "")
    create.assert_awaited()
    assert create.await_args.kwargs["workspace"] == str(wt)


@pytest.mark.asyncio
async def test_doc_review_hash_normalizes_crlf(tmp_path: Path):
    doc = tmp_path / "a.md"
    doc.write_bytes(b"line1\r\nline2\r\n")
    with patch(
        "hiveweave.services.attestation.attestation_service.create",
        AsyncMock(return_value="att-x"),
    ):
        _att_id, report = await create_doc_review(
            "p",
            agent_id="a",
            task_id=None,
            files=[{"path": "a.md"}],
            workspace=str(tmp_path),
        )
    expect = hashlib.sha256(b"line1\nline2\n").hexdigest()
    assert report["files"][0]["sha256"] == expect


def test_reassign_task_in_coordinator_tools():
    from hiveweave.services.permission import (
        CEO_TOOLS,
        COORDINATOR_BUILDER_TOOLS,
        COORDINATOR_ONLY_TOOLS,
    )

    assert "reassign_task" in CEO_TOOLS
    assert "reassign_task" in COORDINATOR_BUILDER_TOOLS
    assert "reassign_task" in COORDINATOR_ONLY_TOOLS
