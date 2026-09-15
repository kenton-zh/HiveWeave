"""`git merge-tree` 落点的信任锚验收（#2 残余 R2 同族 —— 审计 2026-09-15 ①-1）。

**为什么单独一个文件**：`conflict_predict._merge_tree` **不走** `_git`（它要退出码，
`_git` 只返回 bool），所以要**自己**接信任锚。

本文件里的两条判据**都先跑过真机**（写测试前先把阳性对照跑通，否则断言没有意义）：

| 现场 | 裸 git（修复前形态） | 结论 |
|---|---|---|
| `<gitdir>/commondir` → 有效 evil common（内含 `merge.*.driver`） | **不执行** | ⇒ 对 merge-tree 而言 commondir 不是可用载体（与 `git config` 读取不同） |
| 指针 → 自建仓（`rm .git && git init`） | 执行该仓 config 的 driver，但**没有共享历史** ⇒ `merge-tree` 无东西可合 | ⇒ 平台侧仍必须**拒绝**（锚），否则任何依赖「读 agent 自己那棵树」的子命令都会被带跑 |
| `<gitdir>/config.worktree` 写 `merge.*.driver`（**需 `extensions.worktreeConfig=true`**） | **执行**（实测 `EXECUTED`） | ⇒ 这条落点的真实载体 = R3；**2026-09-15 已收口**：平台退休该扩展（写 repo config）后载体失效，见 `test_retire_disables_gitdir_config_carrier` |
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hiveweave.services.git_worktree.conflict_predict import predict_merge_conflicts
from hiveweave.services.git_worktree.git_identity import retire_worktree_config

pytestmark = [pytest.mark.win32]

if not sys.platform.startswith("win"):
    pytest.skip("win32 (Windows 路径语义 + git for windows)",
                allow_module_level=True)


def _raw_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


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
    # `.gitattributes` 必须在**被合并的树**里（merge 驱动按树查属性），
    # 否则 driver 永远不会被调用 —— 本文件第一版就栽在这（阳性对照跑不通）。
    (proj / ".gitattributes").write_text("* merge=evildrv\n", encoding="utf-8",
                                         newline="\n")
    (proj / "c.txt").write_text("base\n", encoding="utf-8")
    _raw_git(proj, "add", "-A")
    _raw_git(proj, "commit", "-qm", "base")
    return proj


@pytest.fixture
def wt(repo: Path) -> Path:
    wt = repo / ".hiveweave" / "worktrees" / "A001"
    _raw_git(repo, "worktree", "add", "-q", str(wt), "-b", "wt/A001")
    return wt


def _make_conflict(repo: Path, wt: Path) -> None:
    """两侧改同一行 ⇒ `merge-tree --write-tree main wt/A001` 必冲突并调用 driver。"""
    (wt / "c.txt").write_text("wt-side\n", encoding="utf-8")
    _raw_git(wt, "commit", "-qam", "wt")
    (repo / "c.txt").write_text("main-side\n", encoding="utf-8")
    _raw_git(repo, "commit", "-qam", "main")


def _driver_cmd(tmp_path: Path, flag: Path) -> str:
    p = tmp_path / "evil_driver.cmd"
    p.write_text(f"@echo off\r\necho hit > \"{flag}\"\r\n",
                 encoding="utf-8", newline="")
    return str(p).replace("\\", "/")


def test_merge_tree_refuses_on_tampered_pointer(repo: Path, wt: Path) -> None:
    """指针被换成「自建仓」⇒ `merge-tree` 必须**不跑**（锚拒绝），返回 unknown。"""
    ptr = wt / ".git"
    ptr.unlink()
    assert _raw_git(wt, "init", "-q").returncode == 0
    pred = asyncio.run(predict_merge_conflicts(str(wt),
                                               project_root=str(repo)))
    assert pred.status == "unknown", pred


def test_retire_disables_gitdir_config_carrier(repo: Path, wt: Path,
                                              tmp_path: Path) -> None:
    """R3 收口：`extensions.worktreeConfig` 退休 ⇒ `<gitdir>/config.worktree` 里的
    merge driver **不再被执行**（平台 `merge-tree` 这条落点的真实载体）。

    先做**阳性对照**：扩展开着时该现场确实会执行（否则断言无意义）。
    """
    flag = tmp_path / "flag_r3.txt"
    _raw_git(wt, "config", "extensions.worktreeConfig", "true")
    _raw_git(wt, "config", "--worktree", "merge.evildrv.driver",
             f"{_driver_cmd(tmp_path, flag)} %O %A %B")
    _make_conflict(repo, wt)

    # 阳性对照：现场是活的（裸 git 会执行）
    flag.unlink(missing_ok=True)
    _raw_git(wt, "merge-tree", "--write-tree", "main", "wt/A001")
    assert flag.exists(), (
        "阳性对照失败：R3 现场没生效（driver 未被调用）⇒ 本用例证明不了什么")

    # 平台动作：退休扩展（写 **repo config**；命令行 -c 关不掉 —— 实测）
    retired = asyncio.run(retire_worktree_config(str(repo)))
    assert retired is True
    assert _raw_git(repo, "config", "extensions.worktreeConfig").stdout.strip() \
        == "false"

    # 载体失效：平台 merge-tree 不再执行它
    flag.unlink(missing_ok=True)
    asyncio.run(predict_merge_conflicts(str(wt), project_root=str(repo)))
    assert not flag.exists(), "退休后平台 merge-tree 仍执行了 agent 的 merge driver"


def test_retire_is_idempotent_and_tolerates_non_repo(tmp_path: Path) -> None:
    """退休函数幂等；非仓库目录不抛异常（fail-quiet 契约）。"""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert asyncio.run(retire_worktree_config(str(plain))) is False
