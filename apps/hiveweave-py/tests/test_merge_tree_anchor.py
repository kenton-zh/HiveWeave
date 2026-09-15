"""`git merge-tree` 落点的信任锚验收（#2 残余 R2 同族 —— 审计 2026-09-15 ①-1）。

**为什么单独一个文件**：`conflict_predict._merge_tree` **不走** `_git`（它要退出码，
`_git` 只返回 bool），所以要**自己**接信任锚。

本文件里的两条判据**都先跑过真机**（写测试前先把阳性对照跑通，否则断言没有意义）：

| 现场 | 裸 git（修复前形态） | 结论 |
|---|---|---|
| `<gitdir>/commondir` → 有效 evil common（内含 `merge.*.driver`） | **不执行** | ⇒ 对 merge-tree 而言 commondir 不是可用载体（与 `git config` 读取不同） |
| 指针 → 自建仓（`rm .git && git init`） | 执行该仓 config 的 driver，但**没有共享历史** ⇒ `merge-tree` 无东西可合 | ⇒ 平台侧仍必须**拒绝**（锚），否则任何依赖「读 agent 自己那棵树」的子命令都会被带跑 |
| `<gitdir>/config.worktree` 写 `merge.*.driver`（**需 `extensions.worktreeConfig=true`**） | **执行**（实测 `EXECUTED`） | ⇒ 这是 merge-tree 这条落点**当前真正的载体** = 残余 R3，见文件末 xfail |
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hiveweave.services.git_worktree.conflict_predict import predict_merge_conflicts

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


@pytest.mark.xfail(
    strict=True,
    reason="已实测残余 R3：agent 在 `<gitdir>/config.worktree` 写 "
           "`[merge \"evildrv\"] driver = <载荷>`（配仓库内 .gitattributes 的 "
           "`* merge=evildrv`）⇒ 平台的 `git merge-tree` **执行其载荷**（2026-09-15 审计"
           "实跑 EXECUTED；信任锚钉的是**路径**，不改变这个载体被读）。"
           "修法（下一批，已定位并实测）：`git config extensions.worktreeConfig false`"
           "（**repo config**）后该载体 IGNORED、载荷 BLOCKED；⚠ 命令行 "
           "`-c extensions.worktreeConfig=false` **无效**（实测仍执行）；身份改由 "
           "`-c user.name/-c user.email` 显式注入。本条转红 = 该修法落地。",
)
def test_merge_tree_driver_from_gitdir_config_worktree_is_blocked(
        repo: Path, wt: Path, tmp_path: Path) -> None:
    """R3 载体（`<gitdir>/config.worktree`）承载 merge driver ⇒ 平台不得执行。"""
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

    flag.unlink(missing_ok=True)
    asyncio.run(predict_merge_conflicts(str(wt), project_root=str(repo)))
    assert not flag.exists(), "平台 merge-tree 执行了 agent 的 merge driver"
