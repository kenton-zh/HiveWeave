"""Per-agent git 身份回归：每个 agent 在 git 内有自己的名字（**新机制**）。

用户需求（2026-08-29）：git log 可区分哪个 agent 干的活。

⚠ **机制在 2026-09-15 换过**（fixqueue #2 残余 R3）：原实现靠
`git config extensions.worktreeConfig true` + `git config --worktree user.name …`，
代价是 git 从此会读两个 **agent 可写面内**的 config 文件
（`<proj>/.git/config.worktree`、`<gitdir>/config.worktree`），而 `filter.<n>.clean` /
`merge.<n>.driver` 是**动态键名**（`GIT_CONFIG_*` 静态清单覆盖不到）
⇒ 实测平台进程会执行 agent 写的驱动。**现机制**：repo 级扩展一律置 `false`
（自愈存量），per-agent 身份改在**提交命令行**注入 `-c user.name/-c user.email`。

⇒ 本文件的判据因此全部落在**结果态**（commit author / 扩展标志），不再断言
「worktree-local config 里有名字」——那是旧机制的实现细节。
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


async def _create(git_repo: Path, short_id: str, name: str | None):
    with _fake_agent(name):
        gwt = GitWorktreeService()
        res = await gwt.create(str(git_repo), short_id, task_id="12345678-abcd")
    assert res["success"] is True, res
    return gwt, Path(res["path"])


# ── 1. 载体退休（R3 的核心断言） ────────────────────────────────
async def test_worktree_config_extension_is_disabled(git_repo: Path):
    """create 后 repo 级 `extensions.worktreeConfig` 必须是 false —— 载体失效的前提。"""
    await _create(git_repo, "A004", "晨露")
    got = _git(git_repo, "config", "extensions.worktreeConfig")
    assert got == "false", (
        f"扩展仍开着（{got!r}）⇒ git 会读 agent 可写的 config.worktree")


async def test_create_disables_previously_enabled_extension(git_repo: Path):
    """存量项目（扩展已被旧版本打开）⇒ create 必须把它关掉（自愈）。"""
    _git(git_repo, "config", "extensions.worktreeConfig", "true")
    await _create(git_repo, "A006", "石浪")
    assert _git(git_repo, "config", "extensions.worktreeConfig") == "false"


async def test_agent_written_worktree_config_is_ignored(git_repo: Path):
    """**判活**：agent 往 `<gitdir>/config.worktree` 写的身份不再被 git 读到。"""
    _, wt = await _create(git_repo, "A007", "折纸")
    gitdir = git_repo / ".git" / "worktrees" / "A007"
    (gitdir / "config.worktree").write_text(
        "[user]\n\tname = EVIL\n", encoding="utf-8")
    assert _git(wt, "config", "user.name") != "EVIL", (
        "扩展关掉后 worktree config 仍被读到 ⇒ 载体没退休")


# ── 2. 可见性不变：commit author 仍是花名 ──────────────────────
async def test_checkpoint_commit_authored_by_agent(git_repo: Path):
    # ⚠ 身份在 **checkpoint 时**解析（不再由 create 写进 worktree config）
    #   ⇒ patch 必须覆盖到 checkpoint，否则解析到的是真实 DB（= 兜底身份）
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
        res = await gwt.create(str(git_repo), "A002", task_id="12345678-abcd")
        assert res["success"] is True, res
        wt = Path(res["path"])
        (wt / "w.txt").write_text("w\n", encoding="utf-8")
        ck = await gwt.checkpoint(str(git_repo), "A002", "存档")
    assert ck["success"] is True, ck
    author = _git(git_repo, "-C", str(wt), "log", "-1", "--format=%an|%ae")
    assert author == "HiveWeave Agent A002|A002@agents.hiveweave.local"


async def test_identity_written_when_resolver_raises(git_repo: Path):
    """resolve_agent 抛异常 ⇒ fail-quiet，checkpoint 仍成功且作者可区分。"""
    with patch(
        "hiveweave.services.org.OrgService.resolve_agent",
        new=AsyncMock(side_effect=RuntimeError("meta db gone")),
    ):
        gwt = GitWorktreeService()
        res = await gwt.create(str(git_repo), "A005", task_id="33333333-abcd")
        assert res["success"] is True, res
        wt = Path(res["path"])
        (wt / "w.txt").write_text("w\n", encoding="utf-8")
        ck = await gwt.checkpoint(str(git_repo), "A005", "存档")
    assert ck["success"] is True, ck
    author = _git(git_repo, "-C", str(wt), "log", "-1", "--format=%an")
    assert author == "HiveWeave Agent A005"


async def test_repo_identity_is_not_overwritten(git_repo: Path):
    """仓库已有 user.name ⇒ 不得覆盖（只兜底缺失）。"""
    await _create(git_repo, "A008", "折纸")
    assert _git(git_repo, "config", "user.name") == "HiveWeave Test"


async def test_works_without_repo_level_identity(git_repo: Path):
    """仓库**本级**没有 user.name（收养仓库）⇒ 提交仍须成功。

    ⚠ 这里**不**断言「一定补写了 repo 级身份」：兜底只在 `git config user.name`
    **完全解析不到**时才写，而开发机通常有全局身份（实测本机全局是 `99744`）⇒
    断言 repo 级一定被写会假红。真正的不变量是**提交成功且作者可区分**。
    """
    _git(git_repo, "config", "--unset", "user.name")
    _git(git_repo, "config", "--unset", "user.email")
    gwt, wt = await _create(git_repo, "A009", "折纸")
    (wt / "w.txt").write_text("w\n", encoding="utf-8")
    ck = await gwt.checkpoint(str(git_repo), "A009", "存档")
    assert ck["success"] is True, ck
    author = _git(git_repo, "-C", str(wt), "log", "-1", "--format=%an|%ae")
    assert author == "HiveWeave Agent A009|A009@agents.hiveweave.local", author
    # agent 自己在 worktree 里 commit 也不能因缺身份而硬失败
    (wt / "own.txt").write_text("own\n", encoding="utf-8")
    _git(wt, "add", "own.txt")
    _git(wt, "commit", "-m", "self")
    assert _git(wt, "log", "-1", "--format=%s") == "self"


async def test_agent_own_commit_in_worktree_still_works(git_repo: Path):
    """扩展关掉后，agent 自己在 worktree 里 `git commit` 仍须成功（用兜底身份）。"""
    _, wt = await _create(git_repo, "A010", "折纸")
    (wt / "own.txt").write_text("own\n", encoding="utf-8")
    _git(wt, "add", "own.txt")
    _git(wt, "commit", "-m", "agent self commit")
    assert _git(wt, "log", "-1", "--format=%s") == "agent self commit"
