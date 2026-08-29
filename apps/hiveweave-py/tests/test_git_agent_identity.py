"""Per-agent git 身份回归：每个 agent 在 git 内有自己的名字。

覆盖（用户需求 2026-08-29：git log 可区分哪个 agent 干的活）：
1. create 后 worktree-local config 生效（user.name=花名, user.email=合成）。
2. checkpoint commit 的 author = agent 身份（main fast-forward 后 git log
   直接显示 agent 花名）。
3. resolve_agent 查不到花名 → fallback "HiveWeave Agent <short_id>"。
4. 幂等复用（二次 create 同 short_id）→ 身份仍在。
5. extensions.worktreeConfig 开启（repo 级，幂等）。

身份 helper 是 fail-quiet 的：OrgService 解析失败不影响 worktree 创建。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.git_worktree import GitWorktreeService


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return r.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@hiveweave.local")
    _git(repo, "config", "user.name", "HiveWeave Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _fake_agent(name: str | None):
    """patch OrgService.resolve_agent → {"name": name} 或 None。"""
    async def _resolve(short_id):
        return {"name": name, "id": "uuid-1"} if name else None

    return patch(
        "hiveweave.services.org.OrgService.resolve_agent",
        new=AsyncMock(side_effect=_resolve),
    )


async def test_create_applies_agent_identity(git_repo: Path):
    with _fake_agent("石浪"):
        gwt = GitWorktreeService()
        res = await gwt.create(str(git_repo), "A001", task_id="12345678-abcd")
    assert res["success"] is True, res
    wt = Path(res["path"])

    # worktree-local config 生效（repo 级 config 仍是测试默认身份）
    assert _git(wt, "config", "user.name") == "石浪"
    assert _git(wt, "config", "user.email") == "A001@agents.hiveweave.local"
    # 主 repo 未被污染（--worktree 只写该 worktree 的 config.worktree）
    assert _git(git_repo, "config", "user.name") == "HiveWeave Test"


async def test_checkpoint_commit_authored_by_agent(git_repo: Path):
    with _fake_agent("石浪"):
        gwt = GitWorktreeService()
        res = await gwt.create(str(git_repo), "A001", task_id="12345678-abcd")
        assert res["success"] is True, res

        wt = Path(res["path"])
        (wt / "work.txt").write_text("agent work\n", encoding="utf-8")
        ck = await gwt.checkpoint(str(git_repo), "A001", "存档")
    assert ck["success"] is True, ck

    author = _git(git_repo, "-C", str(wt), "log", "-1", "--format=%an|%ae")
    assert author == "石浪|A001@agents.hiveweave.local"


async def test_fallback_identity_when_agent_unresolvable(git_repo: Path):
    with _fake_agent(None):
        gwt = GitWorktreeService()
        res = await gwt.create(str(git_repo), "A002", task_id="87654321-abcd")
    assert res["success"] is True, res
    wt = Path(res["path"])
    assert _git(wt, "config", "user.name") == "HiveWeave Agent A002"
    assert _git(wt, "config", "user.email") == "A002@agents.hiveweave.local"


async def test_identity_survives_idempotent_recreate(git_repo: Path):
    with _fake_agent("折纸"):
        gwt = GitWorktreeService()
        first = await gwt.create(str(git_repo), "A003", task_id="11111111-abcd")
        assert first["success"] is True
        wt = Path(first["path"])
        # 清掉首次写入的身份 —— 强制二次 create 必须重写（防残留假绿）
        _git(wt, "config", "--worktree", "--unset", "user.name")
        _git(wt, "config", "--worktree", "--unset", "user.email")
        second = await gwt.create(str(git_repo), "A003", task_id="11111111-abcd")
    assert second["success"] is True
    assert _git(wt, "config", "user.name") == "折纸"


async def test_identity_written_when_resolver_raises(git_repo: Path):
    """resolve_agent 抛异常（meta DB 瞬断等）→ fail-quiet 且 fallback 身份仍写入。"""
    with patch(
        "hiveweave.services.org.OrgService.resolve_agent",
        new=AsyncMock(side_effect=RuntimeError("meta db gone")),
    ):
        gwt = GitWorktreeService()
        res = await gwt.create(str(git_repo), "A005", task_id="33333333-abcd")
    assert res["success"] is True, res
    wt = Path(res["path"])
    assert _git(wt, "config", "user.name") == "HiveWeave Agent A005"
    assert _git(wt, "config", "user.email") == "A005@agents.hiveweave.local"


async def test_worktree_config_extension_enabled(git_repo: Path):
    with _fake_agent("晨露"):
        gwt = GitWorktreeService()
        res = await gwt.create(str(git_repo), "A004", task_id="22222222-abcd")
    assert res["success"] is True
    assert _git(git_repo, "config", "extensions.worktreeConfig") == "true"
