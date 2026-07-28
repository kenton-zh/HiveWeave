"""Worktree relocate (-b) binding + effective-path resolution (TEST_YLGY audit)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.git_worktree import (
    GitWorktreeService,
    WORKTREE_DIR,
    _is_bound_worktree_basename,
    _worktree_binding_under_project,
    _worktree_path,
    ensure_executor_worktree,
)


def test_bound_basename_accepts_canonical_and_relocate_suffixes() -> None:
    assert _is_bound_worktree_basename("A015", "A015")
    assert _is_bound_worktree_basename("A015-b", "A015")
    assert _is_bound_worktree_basename("A015-c", "A015")
    assert _is_bound_worktree_basename("A015-d", "A015")


def test_bound_basename_rejects_substring_and_foreign() -> None:
    # Old split bug: A003-b must not masquerade as A003 via substring
    assert not _is_bound_worktree_basename("A015x", "A015")
    assert not _is_bound_worktree_basename("XA015", "A015")
    assert not _is_bound_worktree_basename("A015-bb", "A015")
    assert not _is_bound_worktree_basename("A014-b", "A015")
    assert not _is_bound_worktree_basename("", "A015")
    assert not _is_bound_worktree_basename("A015", "")


def test_binding_under_project_accepts_worktree_child(tmp_path: Path) -> None:
    ws = tmp_path / "repo"
    relocated = ws / WORKTREE_DIR / "A015-b"
    relocated.mkdir(parents=True)
    assert _worktree_binding_under_project(str(relocated), str(ws))


def test_binding_under_project_rejects_foreign_workspace(tmp_path: Path) -> None:
    ws_a = tmp_path / "projA"
    ws_b = tmp_path / "projB"
    foreign = ws_b / WORKTREE_DIR / "A015-b"
    foreign.mkdir(parents=True)
    assert not _worktree_binding_under_project(str(foreign), str(ws_a))


@pytest.mark.asyncio
async def test_resolve_effective_prefers_db_relocated_over_canonical_with_git(
    tmp_path: Path,
) -> None:
    """Both canonical and -b have .git — DB binding to -b must win (A015 split)."""
    ws = tmp_path / "repo"
    wt_root = ws / ".hiveweave" / "worktrees"
    canonical = wt_root / "A015"
    relocated = wt_root / "A015-b"
    for p in (canonical, relocated):
        p.mkdir(parents=True)
        (p / ".git").write_text("gitdir: dummy\n", encoding="utf-8")

    agent = {
        "short_id": "A015",
        "workspace_path": str(relocated),
    }

    with patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[agent]),
    ):
        path = await GitWorktreeService._resolve_effective_worktree_path(
            str(ws), "A015"
        )
    assert Path(path) == relocated


@pytest.mark.asyncio
async def test_resolve_effective_falls_back_to_canonical_when_no_db(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "repo"
    canonical = _worktree_path(str(ws), "A014")
    Path(canonical).mkdir(parents=True)
    (Path(canonical) / ".git").write_text("gitdir: dummy\n", encoding="utf-8")

    with patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[]),
    ):
        path = await GitWorktreeService._resolve_effective_worktree_path(
            str(ws), "A014"
        )
    assert path == canonical


@pytest.mark.asyncio
async def test_resolve_effective_ignores_illegal_db_basename(
    tmp_path: Path,
) -> None:
    """Foreign basename in DB must not hijack resolution."""
    ws = tmp_path / "repo"
    canonical = Path(_worktree_path(str(ws), "A015"))
    foreign = ws / ".hiveweave" / "worktrees" / "A015x"
    for p in (canonical, foreign):
        p.mkdir(parents=True)
        (p / ".git").write_text("gitdir: dummy\n", encoding="utf-8")

    agent = {
        "short_id": "A015",
        "workspace_path": str(foreign),
    }
    with patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[agent]),
    ):
        path = await GitWorktreeService._resolve_effective_worktree_path(
            str(ws), "A015"
        )
    assert Path(path) == canonical


@pytest.mark.asyncio
async def test_resolve_effective_rejects_db_path_outside_project(
    tmp_path: Path,
) -> None:
    """Cross-project / escaped absolute path must fall back to canonical."""
    ws = tmp_path / "projA"
    other = tmp_path / "projB"
    canonical = Path(_worktree_path(str(ws), "A015"))
    escaped = other / WORKTREE_DIR / "A015-b"
    for p in (canonical, escaped):
        p.mkdir(parents=True)
        (p / ".git").write_text("gitdir: dummy\n", encoding="utf-8")

    agent = {
        "short_id": "A015",
        "workspace_path": str(escaped),
    }
    with patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[agent]),
    ):
        path = await GitWorktreeService._resolve_effective_worktree_path(
            str(ws), "A015"
        )
    assert Path(path) == canonical


@pytest.mark.asyncio
async def test_ensure_accepts_healthy_relocated_binding_without_create(
    tmp_path: Path,
) -> None:
    """DB bound to A015-b + healthy git → already bound; create must not run."""
    ws = tmp_path / "repo"
    (ws / ".git").mkdir(parents=True)
    relocated = ws / WORKTREE_DIR / "A015-b"
    relocated.mkdir(parents=True)
    (relocated / ".git").write_text("gitdir: dummy\n", encoding="utf-8")
    # Locked/stale canonical still present — must NOT trigger rebuild
    canonical = ws / WORKTREE_DIR / "A015"
    canonical.mkdir(parents=True)
    (canonical / ".git").write_text("gitdir: dummy\n", encoding="utf-8")

    agent = {
        "id": "agent-015",
        "short_id": "A015",
        "permission_type": "executor",
        "role": "H5 QA",
        "workspace_path": str(relocated),
        "worktree_error": "stale",
    }
    create_mock = AsyncMock(
        side_effect=AssertionError("create must not run for healthy -b bind")
    )
    org = MagicMock()
    org.resolve_agent = AsyncMock(return_value=agent)
    org.update_agent = AsyncMock()

    with (
        patch("hiveweave.services.org.OrgService", return_value=org),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            new=AsyncMock(return_value=str(ws)),
        ),
        patch(
            "hiveweave.services.git_worktree._current_branch",
            new=AsyncMock(return_value="hw/A015/work"),
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService.create",
            create_mock,
        ),
    ):
        result = await ensure_executor_worktree("proj-1", "agent-015")

    assert result["success"] is True
    assert result["path"] == str(relocated)
    assert "relocated" in (result.get("message") or "")
    create_mock.assert_not_called()
    org.update_agent.assert_awaited()
    # worktree_error cleared
    cleared = org.update_agent.await_args.args[1]
    assert cleared.get("worktree_error") is None


@pytest.mark.asyncio
async def test_ensure_relocation_notify_uses_inbox_send_message_kwargs(
    tmp_path: Path,
) -> None:
    """Relocate notify must use from_agent_id/to_agent_id/message — no project_id."""
    ws = tmp_path / "repo"
    (ws / ".git").mkdir(parents=True)
    relocated = str(ws / WORKTREE_DIR / "A015-b")

    agent = {
        "id": "agent-015",
        "short_id": "A015",
        "permission_type": "executor",
        "role": "H5 QA",
        "workspace_path": None,
    }
    org = MagicMock()
    org.resolve_agent = AsyncMock(return_value=agent)
    org.update_agent = AsyncMock()

    send = AsyncMock(return_value={"should_wake": False})
    inbox_inst = MagicMock()
    inbox_inst.send_message = send

    with (
        patch("hiveweave.services.org.OrgService", return_value=org),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            new=AsyncMock(return_value=str(ws)),
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService.create",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "path": relocated,
                    "branch": "hw/A015/work",
                }
            ),
        ),
        patch(
            "hiveweave.services.inbox.InboxService",
            return_value=inbox_inst,
        ),
    ):
        result = await ensure_executor_worktree("proj-1", "agent-015")

    assert result["success"] is True
    assert result.get("relocated") is True or Path(result["path"]).name == "A015-b"
    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert "project_id" not in kwargs
    assert "sender_id" not in kwargs
    assert "recipient_id" not in kwargs
    assert "content" not in kwargs
    assert kwargs["from_agent_id"] == "system"
    assert kwargs["to_agent_id"] == "agent-015"
    assert "[WORKTREE RELOCATED]" in kwargs["message"]
    assert "A015-b" in kwargs["message"]


@pytest.mark.asyncio
async def test_checkpoint_uses_effective_relocated_path(tmp_path: Path) -> None:
    """checkpoint must operate on DB -b binding, not locked canonical."""
    ws = tmp_path / "repo"
    canonical = Path(_worktree_path(str(ws), "A015"))
    relocated = ws / WORKTREE_DIR / "A015-b"
    for p in (canonical, relocated):
        p.mkdir(parents=True)
        (p / ".git").write_text("gitdir: dummy\n", encoding="utf-8")

    agent = {"short_id": "A015", "workspace_path": str(relocated)}
    git_calls: list[tuple[list[str], str]] = []

    async def fake_git(args: list[str], cwd: str):
        git_calls.append((list(args), cwd))
        # Empty porcelain → "no changes" early return still proves cwd
        if args[:1] == ["status"]:
            return True, ""
        if args[:2] == ["rev-parse", "--short"]:
            return True, "abc1234"
        if args[0] == "add":
            return True, ""
        if args[:2] == ["diff", "--cached"]:
            return True, ""
        return True, ""

    with (
        patch(
            "hiveweave.services.org.OrgService.list_agents",
            new=AsyncMock(return_value=[agent]),
        ),
        patch("hiveweave.services.git_worktree._git", side_effect=fake_git),
    ):
        result = await GitWorktreeService().checkpoint(
            str(ws), "A015", "probe"
        )

    assert result["success"] is True, result
    assert git_calls, "expected git ops"
    assert all(cwd == str(relocated) for _, cwd in git_calls)


@pytest.mark.asyncio
async def test_vision_analyze_image_forces_supports_images() -> None:
    """vision 槽位不得被模型行 supports_images=0 剥图。"""
    from hiveweave.services import vision as vision_mod

    captured: dict = {}

    class FakeProvider:
        def build_body(self, **kwargs):
            return {"ok": True}

        def build_headers(self):
            return {}

        def build_url(self):
            return "https://example.test/v1/chat"

    class FakeFactory:
        def create(self, cfg):
            captured["cfg"] = dict(cfg)
            return FakeProvider()

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "saw a canvas"}}],
            }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **k):
            return FakeResp()

    with (
        patch("hiveweave.llm.provider.provider_factory", FakeFactory()),
        patch("httpx.AsyncClient", FakeClient),
    ):
        text = await vision_mod.analyze_image(
            image={"media_type": "image/png", "data": "AAAA"},
            prompt="describe",
            model_config={
                "provider": "openai",
                "model_id": "text-only",
                "supports_images": False,
            },
        )
    assert "canvas" in text
    assert captured["cfg"]["supports_images"] is True
