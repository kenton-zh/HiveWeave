"""BUG-ORGWT 回归：agent_worktree_path 孤儿 worktree 读路径挂回。

2026-08-05 feature-test 项目审批死锁根因：HR 把 qa-family 角色
（org测试工程师）误配 permission_type=coordinator —— hire 期建过
worktree，但 agent_gets_write_worktree 不认（coordinator 须
family==coordinator），heal/reconcile 跳过 → DB workspace_path 为 NULL，
树在磁盘 + git 在册却成孤儿。review_worktree_gate 以
"implementer/assignee has no worktree path" 硬拒 approve，agent 侧无解法。

修复：DB 路径为空/失效时，回退规范位置
`<main_ws>/.hiveweave/worktrees/<short_id>`（须为目录且含 .git）。
只读审查定位，不授权写路径。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.services import worktree_review as wr


def _mk_tree(root: Path, short_id: str) -> Path:
    wt = root / ".hiveweave" / "worktrees" / short_id
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /fake", encoding="utf-8")
    return wt


@pytest.fixture
def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir).resolve()
        yield {"workspace": ws}


def _patch_row(row: dict, ws: Path):
    async def fake_get_agent(agent_id: str):
        return row

    async def fake_main_ws(project_id: str):
        return str(ws)

    return (
        patch.object(wr.meta_db, "get_agent_by_id", fake_get_agent),
        patch.object(wr, "project_main_workspace", fake_main_ws),
    )


@pytest.mark.asyncio
async def test_orphan_worktree_rebound_from_canonical_path(env):
    """DB 空 + 磁盘/git 在册 → 挂回规范路径。"""
    ws = env["workspace"]
    canon = _mk_tree(ws, "A021")
    row = {
        "id": "agent-1",
        "short_id": "A021",
        "project_id": "p1",
        "workspace_path": None,
    }
    p1, p2 = _patch_row(row, ws)
    with p1, p2:
        got = await wr.agent_worktree_path("agent-1")
    assert got == str(canon)


@pytest.mark.asyncio
async def test_stale_db_path_falls_back_to_canonical(env):
    """DB 路径失效（目录已删）+ 规范位置存在 → 回退规范位置。"""
    ws = env["workspace"]
    canon = _mk_tree(ws, "A019")
    row = {
        "id": "agent-2",
        "short_id": "A019",
        "project_id": "p1",
        "workspace_path": str(ws / "gone" / "A019"),
    }
    p1, p2 = _patch_row(row, ws)
    with p1, p2:
        got = await wr.agent_worktree_path("agent-2")
    assert got == str(canon)


@pytest.mark.asyncio
async def test_no_disk_tree_returns_none(env):
    """DB 空 + 磁盘无树 → None（门禁行为不变）。"""
    ws = env["workspace"]
    row = {
        "id": "agent-3",
        "short_id": "A099",
        "project_id": "p1",
        "workspace_path": None,
    }
    p1, p2 = _patch_row(row, ws)
    with p1, p2:
        got = await wr.agent_worktree_path("agent-3")
    assert got is None


@pytest.mark.asyncio
async def test_dir_without_git_not_rebound(env):
    """规范位置是目录但无 .git（node_modules 空壳）→ 不挂回。"""
    ws = env["workspace"]
    wt = ws / ".hiveweave" / "worktrees" / "A030"
    wt.mkdir(parents=True)  # no .git
    row = {
        "id": "agent-4",
        "short_id": "A030",
        "project_id": "p1",
        "workspace_path": None,
    }
    p1, p2 = _patch_row(row, ws)
    with p1, p2:
        got = await wr.agent_worktree_path("agent-4")
    assert got is None


@pytest.mark.asyncio
async def test_valid_db_path_still_preferred(env):
    """DB 路径有效 → 直接用 DB 路径（现有行为不变）。"""
    ws = env["workspace"]
    bound = ws / "elsewhere" / "A018"
    bound.mkdir(parents=True)
    (bound / ".git").write_text("gitdir: /fake", encoding="utf-8")
    row = {
        "id": "agent-5",
        "short_id": "A018",
        "project_id": "p1",
        "workspace_path": str(bound),
    }
    p1, p2 = _patch_row(row, ws)
    with p1, p2:
        got = await wr.agent_worktree_path("agent-5")
    assert got == str(bound)
