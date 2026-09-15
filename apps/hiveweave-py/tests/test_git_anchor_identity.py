"""gitdir **身份指纹**验收（#2 残余 R4：**同路径替换**）。

信任锚（`git_anchor`）钉的是**路径**；R4 是「把那个路径上的 gitdir 删掉、再在同路径造
一个自己的仓」——审计 2026-09-15 实测：项目根边界形态 `rmdir /s /q <proj>\\.git` 后
`git init`，平台 `_git` 就读到了 agent 的 config。

本文件验的是第二层：`(st_dev, st_ino)` 身份（NTFS 上 = 卷序列号 + 文件索引）记在**平台
数据根**（`get_data_root()/git-anchor`，在项目 ACL 授权面之外 ⇒ agent 写不到），每次跑 git
前比对；不符 ⇒ 拒绝执行。

判据形态：**每条先做阳性对照**（裸 git = 无本层时确实会中招），再用平台路径断言拒绝。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hiveweave.services.git_worktree import git_anchor as ga
from hiveweave.services.git_worktree.git_cmd import _git

pytestmark = [pytest.mark.win32]

if not sys.platform.startswith("win"):
    pytest.skip("win32 (Windows 路径语义 + git for windows)",
                allow_module_level=True)

KEY = "probe.r4"


def _raw_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """裸 git（**无本层**的形态）：让 git 自己找 gitdir —— 阳性对照用。"""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _platform_git(cwd: Path, *args: str,
                  project_root: Path | None = None) -> tuple[bool, str]:
    return asyncio.run(_git(list(args), str(cwd),
                            project_root=str(project_root) if project_root
                            else None))


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    """进程级缓存会跨用例残留（同 realpath 才有影响）—— 每例清一次，判据才干净。"""
    ga._IDENTITY_CACHE.clear()
    yield
    ga._IDENTITY_CACHE.clear()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if not shutil.which("git"):
        pytest.skip("git not on PATH")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".hiveweave" / "worktrees").mkdir(parents=True)
    _raw_git(proj, "init", "-q", "-b", "main")
    _raw_git(proj, "config", "user.email", "t@t.t")
    _raw_git(proj, "config", "user.name", "T")
    (proj / "f.txt").write_text("a\n", encoding="utf-8")
    _raw_git(proj, "add", "f.txt")
    _raw_git(proj, "commit", "-qm", "init")
    return proj


@pytest.fixture
def wt(repo: Path) -> Path:
    wt = repo / ".hiveweave" / "worktrees" / "A001"
    _raw_git(repo, "worktree", "add", "-q", str(wt), "-b", "wt/A001")
    return wt


def _force_rmtree(path: Path) -> None:
    """删目录（含只读对象文件）。

    ⚠ 不用 `shutil.rmtree`：git 的 objects 里有**只读**文件 ⇒ Windows 会 WinError 5。
    这里走 `rmdir /s /q`（= 审计 2026-09-15 实测 agent 那侧用的同一条命令）。
    """
    subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(path)],
                   capture_output=True, text=True)
    assert not path.exists(), f"删除失败：{path}"


def _agent_replaces_project_gitdir(repo: Path) -> None:
    """模拟 agent：删掉整个 `.git`，在**同路径**造自己的仓并写入标记键。"""
    _force_rmtree(repo / ".git")
    _raw_git(repo, "init", "-q", "-b", "main")
    _raw_git(repo, "config", KEY, "FROM_AGENT_REPO")


# ── 1. 项目 `.git` 的同路径替换 ──────────────────────────────────
def test_same_path_replacement_is_refused(repo: Path) -> None:
    # 首次平台调用 = 记录身份（TOFU；生产里创建点就会记）
    ok, _ = _platform_git(repo, "config", "--get", "core.repositoryformatversion")
    assert ok, "首次调用应当正常（并记录身份）"
    assert ga.read_gitdir_identity(str(repo / ".git")) is not None

    _agent_replaces_project_gitdir(repo)
    # 阳性对照：裸 git（无本层）确实读到 agent 的 config
    raw = _raw_git(repo, "config", "--get", KEY)
    assert raw.stdout.strip() == "FROM_AGENT_REPO", (
        f"阳性对照失败：裸 git 没读到 agent 的 config（out={raw.stdout!r} "
        f"err={raw.stderr!r}）⇒ 本用例证明不了什么")

    ok, out = _platform_git(repo, "config", "--get", KEY)
    assert not ok, f"平台仍跑了 git：{out!r}"
    assert "FROM_AGENT_REPO" not in out
    assert "身份" in out or "identity" in out.lower(), out


def test_creation_point_record_is_authoritative(repo: Path) -> None:
    """创建点记录（`record_gitdir_identity`）⇒ 即使第一次平台调用发生在篡改**之后**也拒。"""
    ga.record_gitdir_identity(str(repo / ".git"))
    _agent_replaces_project_gitdir(repo)
    ok, out = _platform_git(repo, "config", "--get", KEY)
    assert not ok and "FROM_AGENT_REPO" not in out, out


def test_config_rewrite_does_not_trip_identity(repo: Path) -> None:
    """平台频繁重写 `.git/config`（lock+rename，换的是**文件** inode）⇒ 不得误拒。"""
    assert _platform_git(repo, "config", "user.email", "p@t.t")[0]
    assert _platform_git(repo, "config", "user.name", "P")[0]
    assert _platform_git(repo, "status", "--porcelain")[0]
    assert _raw_git(repo, "config", "--get", "user.email").stdout.strip() \
        == "p@t.t"


def test_tofu_adopts_and_writes_record(repo: Path) -> None:
    """无记录 ⇒ TOFU：采用当下身份、落盘记录、放行（覆盖机制上线前的存量项目）。"""
    assert ga.read_gitdir_identity(str(repo / ".git")) is None
    ok, _ = _platform_git(repo, "status", "--porcelain")
    assert ok
    assert ga.read_gitdir_identity(str(repo / ".git")) is not None


# ── 2. worktree gitdir 的同路径替换（同一 helper，纵深防御） ──────
def test_worktree_gitdir_replacement_is_refused(repo: Path, wt: Path) -> None:
    ok, _ = _platform_git(wt, "status", "--porcelain")
    assert ok
    gitdir = repo / ".git" / "worktrees" / "A001"
    assert ga.read_gitdir_identity(str(gitdir)) is not None

    ident_before = os.stat(str(gitdir)).st_ino
    _force_rmtree(gitdir)
    gitdir.mkdir()
    (gitdir / "config").write_text(f'[probe]\n\tr4 = FROM_AGENT_GITDIR\n',
                                   encoding="utf-8")
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert os.stat(str(gitdir)).st_ino != ident_before, "同路径重建未换身份？"
    ga._IDENTITY_CACHE.clear()

    ok, out = _platform_git(wt, "config", "--get", KEY)
    assert not ok and "FROM_AGENT_GITDIR" not in out, out
