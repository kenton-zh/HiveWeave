"""平台侧 git 信任锚（#2 残余 R2）—— 单测 + 攻击链验收。

对应实现：`services/git_worktree/git_anchor.py` + `git_cmd.py::_git`。

判据形态（本仓纪律）：
- **状态判据**：断言「平台的 git 是否读到了 agent 写的 config」= 看它**读出的值**，
  不看退出码、不看文案；
- **阳性对照**：每条攻击都要先证明「不钉锚时它**确实成立**」（否则断言无意义）；
- **fail-closed**：锚点派生不出来的可疑形态必须**拒绝执行**。

不需要 ACL 沙箱（攻击的「写」这一侧由测试直接做文件操作，与审计 A1 的做法一致）。
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

PAYLOAD_KEY = "probe.anchor"


def _raw_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """不带锚的裸 git（= 修复前的平台行为），用来做**阳性对照**。"""
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
    (proj / "f.txt").write_text("a\n", encoding="utf-8")
    _raw_git(proj, "add", "f.txt")
    _raw_git(proj, "commit", "-qm", "init")
    return proj


@pytest.fixture
def wt(repo: Path) -> Path:
    wt = repo / ".hiveweave" / "worktrees" / "A001"
    _raw_git(repo, "worktree", "add", "-q", str(wt), "-b", "wt/A001")
    return wt


def _platform_git(cwd: Path, *args: str,
                  project_root: Path | None = None) -> tuple[bool, str]:
    """平台路径（走 `_git`：带信任锚 + 漏斗加固）。"""
    return asyncio.run(_git(list(args), str(cwd),
                            project_root=str(project_root) if project_root
                            else None))


# ══════════════════════════════════════════════════════════════════
# 1. 派生正确性
# ══════════════════════════════════════════════════════════════════
def test_anchor_main_tree(repo: Path) -> None:
    a = ga.resolve_anchor(str(repo))
    assert a is not None and a.kind == "main"
    assert Path(a.git_dir) == repo / ".git"
    assert Path(a.common_dir) == repo / ".git"


def test_anchor_from_main_subdir(repo: Path) -> None:
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    a = ga.resolve_anchor(str(sub))
    assert a is not None and a.kind == "main"
    assert Path(a.git_dir) == repo / ".git", "子目录必须上溯到主树"


def test_anchor_worktree(repo: Path, wt: Path) -> None:
    a = ga.resolve_anchor(str(wt))
    assert a is not None and a.kind == "worktree"
    assert Path(a.git_dir) == repo / ".git" / "worktrees" / "A001"
    assert Path(a.common_dir) == repo / ".git"


def test_anchor_none_before_init(tmp_path: Path) -> None:
    """`git init` 之前不是仓 ⇒ None，且**不得**拒绝（否则建项目流程直接死）。"""
    d = tmp_path / "fresh"
    d.mkdir()
    anchor, refusal = ga.anchor_for_git(str(d))
    assert anchor is None and refusal is None


# ══════════════════════════════════════════════════════════════════
# 2. 攻击链验收（每条先做阳性对照）
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("with_project_root", [False, True],
                         ids=["structural", "explicit-root"])
def test_pointer_redirect_is_neutralised(repo: Path, wt: Path,
                                         with_project_root: bool) -> None:
    """R2-①：删指针 → 在 worktree 里自建仓（agent 的真实手法：`rm .git; git init`）。

    阳性对照用**裸 git**（修复前形态）：经指针直接读到 agent 的 config。
    平台路径必须**拒绝执行**（平台 worktree 的 `.git` 只能是指针文件）。
    """
    ptr = wt / ".git"
    ptr.unlink()
    r = _raw_git(wt, "init", "-q")
    assert r.returncode == 0, r.stderr
    _raw_git(wt, "config", PAYLOAD_KEY, "FROM_EVIL_POINTER")
    assert (wt / ".git").is_dir()
    # 阳性对照：裸 git（修复前形态）确实读到 agent 的 config
    raw = _raw_git(wt, "config", "--get", PAYLOAD_KEY)
    assert raw.stdout.strip() == "FROM_EVIL_POINTER", (
        f"阳性对照失败：裸 git 没读到 agent 的 config ⇒ 本用例证明不了什么 "
        f"(out={raw.stdout.strip()!r} err={raw.stderr.strip()!r})")
    # 平台路径：必须拒绝，且绝不读到 agent 的值
    ok, out = _platform_git(wt, "config", "--get", PAYLOAD_KEY,
                            project_root=repo if with_project_root else None)
    assert not ok, f"平台仍跑了 git：{out!r}"
    assert "FROM_EVIL_POINTER" not in out, out


@pytest.mark.parametrize("with_project_root", [False, True],
                         ids=["structural", "explicit-root"])
def test_commondir_redirect_is_neutralised(repo: Path, wt: Path,
                                          with_project_root: bool) -> None:
    """R2-②：把 `commondir` 改写指向一个**有效的 evil common dir**。

    ⚠ 这条正是「封条也挡不住」的那一族：gitdir 目录对 agent 可写（index/index.lock
    需要），所以 `commondir` 删了还能重建。修复手段是平台**同时钉住 common dir**
    （`GIT_COMMON_DIR`）—— 只钉 `--git-dir` 不够（实测）。
    """
    gitdir = repo / ".git" / "worktrees" / "A001"
    evil = repo.parent / "evilcommon"
    (evil / "objects").mkdir(parents=True, exist_ok=True)
    (evil / "refs").mkdir(exist_ok=True)
    (evil / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n"
        f"[probe]\n\tanchor = FROM_EVIL_COMMON\n", encoding="utf-8")
    rel = os.path.relpath(evil, gitdir).replace("\\", "/")
    (gitdir / "commondir").write_text(rel, encoding="utf-8")
    # 阳性对照：裸 git（修复前形态）经 commondir 读到 evil 的 common config
    raw = _raw_git(wt, "config", "--get", PAYLOAD_KEY)
    assert raw.stdout.strip() == "FROM_EVIL_COMMON", (
        f"阳性对照失败：裸 git 没经 commondir 读到 evil common "
        f"(out={raw.stdout.strip()!r} err={raw.stderr.strip()!r})")
    # 平台路径：common dir 由平台钉住 ⇒ 读不到
    ok, out = _platform_git(wt, "config", "--get", PAYLOAD_KEY,
                            project_root=repo if with_project_root else None)
    assert "FROM_EVIL_COMMON" not in out, (
        f"平台读到了 evil common config：ok={ok} out={out!r}")
    # 且平台的正常读取仍在（钉住不等于读不到自己的 config）
    ok, out = _platform_git(wt, "config", "--get", "core.repositoryformatversion")
    assert ok and out.strip() == "0", (ok, out)


def test_anchor_refuses_when_expected_gitdir_missing(repo: Path, wt: Path) -> None:
    """期望 gitdir 被删掉 ⇒ **可证篡改** ⇒ 拒绝跑 git（loud），不回落「让 git 自己找」。"""
    shutil.rmtree(repo / ".git" / "worktrees" / "A001")
    ok, out = _platform_git(wt, "status", "--porcelain",
                            project_root=str(repo))
    assert not ok and ("信任锚" in out or "gitdir" in out), out


def test_underivable_layout_is_not_refused(repo: Path, tmp_path: Path) -> None:
    """**派生不出**（worktree 与主仓是兄弟目录）⇒ 不拒绝，退回不钉锚 + loud 记账。

    这条是「不要把两档混成一档」的守卫：实测 `test_checkpoint_dirty_contract`
    的夹具就是这种合法布局，fail-closed 会把它们一起打死。
    """
    sibling = tmp_path / "sibling-wt"
    r = _raw_git(repo, "worktree", "add", "-q", str(sibling), "-b", "wt/sib")
    assert r.returncode == 0, r.stderr
    anchor, refusal = ga.anchor_for_git(str(sibling))
    assert anchor is None and refusal is None, (anchor, refusal)
    # 且 git 照常能跑（不钉锚 = 修复前行为）
    ok, out = _platform_git(sibling, "status", "--porcelain")
    assert ok, out


# ══════════════════════════════════════════════════════════════════
# 3. 功能面：钉住之后平台的 git 用法照常
# ══════════════════════════════════════════════════════════════════
def test_platform_git_flows_still_work(repo: Path, wt: Path) -> None:
    assert _platform_git(repo, "config", "user.email", "p@t.t")[0]
    assert _platform_git(repo, "config", "--get",
                         "user.email")[1].strip() == "p@t.t"
    assert _platform_git(wt, "status", "--porcelain")[0]
    (wt / "w.txt").write_text("w\n", encoding="utf-8")
    assert _platform_git(wt, "add", "-A")[0]
    ok, out = _platform_git(wt, "commit", "-qm", "wt commit")
    assert ok, out
    assert _platform_git(wt, "rev-parse", "--abbrev-ref", "HEAD")[1].strip() \
        == "wt/A001"
    assert _platform_git(repo, "worktree", "list", "--porcelain")[0]
    assert _platform_git(repo, "merge", "--ff-only", "-q", "wt/A001")[0]
    (repo / "m.txt").write_text("m\n", encoding="utf-8")
    assert _platform_git(repo, "add", "-A")[0]
    assert _platform_git(repo, "commit", "-qm", "main commit")[0]
    assert _platform_git(repo, "log", "--oneline", "-1")[0]
    # 新 worktree 也用锚（创建后再跑一条）
    wt2 = repo / ".hiveweave" / "worktrees" / "B002"
    assert _platform_git(repo, "worktree", "add", "-q", str(wt2),
                         "-b", "wt/B002")[0]
    assert _platform_git(wt2, "status", "--porcelain")[0]
