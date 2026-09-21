"""P1-6 问题 B：已 tracked 的生成物必须能自动去跟踪（ignore 对它们无效）。

验收配方（§10.2）：造 `.vite/deps/_metadata.json` **已 tracked** 的仓库 ⇒ 触发一次 ensure ⇒
`git ls-files -- <path>` stdout 为空（rc=0）；`git status --porcelain` 该路径显示为已删除。
边界：**非生成物**（源码文件、`.hiveweave/shared/` 契约）绝不能被去跟踪。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hiveweave.services.git_worktree.service_create import CreateMixin


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )


def _init_repo(path: Path) -> None:
    _git(path, "init", "-q", ".")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    # 已 tracked 的**生成物** + 两个**绝不能动**的对照项
    (tmp_path / ".vite" / "deps").mkdir(parents=True)
    (tmp_path / ".vite" / "deps" / "_metadata.json").write_text("{}", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / ".hiveweave" / "shared").mkdir(parents=True)
    (tmp_path / ".hiveweave" / "shared" / "contract.md").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    # 必须 **commit**：没有 HEAD 就谈不上「已 tracked」，git status 也不会给出
    # 规格要求的「已删除」行（未提交时去跟踪只是让该路径从暂存区消失）。
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


@pytest.mark.asyncio
async def test_tracked_generated_artifact_is_untracked(repo: Path):
    # 前置：确实被跟踪（否则测试失去意义）
    assert ".vite/deps/_metadata.json" in _git(repo, "ls-files").stdout

    await CreateMixin._untrack_generated_paths(None, str(repo))

    assert ".vite/deps/_metadata.json" not in _git(repo, "ls-files").stdout, (
        "生成物未被去跟踪"
    )
    # --cached 的语义：文件仍在磁盘上
    assert (repo / ".vite" / "deps" / "_metadata.json").exists()
    # 验收：该路径的状态码为 D（已从索引移除）。
    # ⚠ 规格写的「` D`」是**工作区删除**那一列；`git rm --cached` 会把删除**暂存**
    # ⇒ 实测为 `D `（第一列）。两列都表示「索引里没了」，故这里判**状态码**而非列位。
    porcelain = _git(repo, "status", "--porcelain").stdout
    codes = {
        ln[3:]: ln[:2].strip()
        for ln in porcelain.splitlines()
        if len(ln) > 3 and ln[3:]
    }
    assert codes.get(".vite/deps/_metadata.json") == "D", porcelain


@pytest.mark.asyncio
async def test_non_generated_paths_are_never_untracked(repo: Path):
    await CreateMixin._untrack_generated_paths(None, str(repo))
    left = _git(repo, "ls-files").stdout
    assert "src/app.py" in left, "源码文件被误去跟踪"
    assert ".hiveweave/shared/contract.md" in left, "共享契约目录被误去跟踪（绝对边界）"


@pytest.mark.asyncio
async def test_no_generated_tracked_is_a_noop(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "main.py").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    before = _git(tmp_path, "ls-files").stdout
    await CreateMixin._untrack_generated_paths(None, str(tmp_path))
    assert _git(tmp_path, "ls-files").stdout == before
