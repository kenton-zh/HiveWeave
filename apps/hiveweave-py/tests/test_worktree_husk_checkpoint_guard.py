"""#21 成因链的两条行为守卫（第 1 步 1-1 / 1-2 / 1-3）。

## 事故链（TEST_DSH_58/59 实测 3 次主干清空）

1. 合并成功后平台回收 worktree，删目录被 Windows 文件锁挡住 ⇒ 留 **husk**
   （目录还在、内容已没了、`worktree` 注册也没了）；
2. `[UNCOMMITTED_WORKTREE]` 门禁照常要求 checkpoint ⇒ 平台在 husk 里跑
   `git add -A`；
3. 而信任锚 `git_anchor` **只钉了 `--git-dir`、没钉 work tree** ⇒ git 把
   **cwd 当成工作树根**（实测：在 `TEST_DSH_58/.vite` 里
   `--git-dir=<root>/.git rev-parse --show-toplevel` 输出 `.vite`，
   `ls-files -d` 报出 **10 个** = 整棵主干文件数）；
4. ⇒ 整棵树被记成「删除」并**直接提交到 `main`**（cwd=`husk`、gitdir 锚定
   到 `<main>/.git` ⇒ `commit` 更新的是 `main` 的 HEAD）。

⇒ `main^{tree}` 被清空。本文件把这条链的**两处**钉住：

- `test_platform_git_does_not_treat_cwd_as_the_work_tree` —— **1-1**：
  锚必须同时钉住工作树；任意目录都不能被当成工作树根；
- `test_checkpoint_refuses_husk_and_main_tree_unchanged` —— **1-2 / 1-3**：
  worktree 已不注册 ⇒ checkpoint 必须 **fail loud 且不提交**，
  且 `main^{tree}` 一字不变。

⚠ 两条都**先红后绿**（本文件是先写出来的阳性对照）：在 1-1/1-2 落地前，
第一条会看到"全部 tracked 被算成已删除"，第二条会看到 `success=True` +
`main^{tree}` 变了。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hiveweave.services.git_worktree import _git
from hiveweave.services.git_worktree import GitWorktreeService

_HAS_GIT = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not _HAS_GIT, reason="需要 git")

# 两条**行为**用例跑真子进程 + 平台布局路径语义，只在本机 win32 上验证过 ⇒
# 只在 win32 收集。⚠ 纯数据契约那一条**不**带这个 skip，见文件末尾
# （CI 是 ubuntu-latest，整模块 skip 会让本批的核心契约在 CI 上零覆盖 —— 审计第 6 条）。
_win_only = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="行为用例只在 win32 上验证过（路径语义 + 平台 worktree 落点）",
)


def _raw_git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return (r.stdout or "").strip()


@pytest.fixture
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """主仓 + **规范落点**的 linked worktree（`<main>/.hiveweave/worktrees/A099`）。

    落点必须规范：信任锚靠平台布局派生（`.hiveweave/worktrees/<name>`），
    非规范布局会走 `AnchorUnderivable`（不钉锚）⇒ 测不到锚的行为。
    """
    main = tmp_path / "proj"
    main.mkdir()
    _raw_git(main, "init", "-q", "-b", "main")
    _raw_git(main, "config", "user.email", "t@t.t")
    _raw_git(main, "config", "user.name", "t")
    (main / "README.md").write_text("hi\n", encoding="utf-8")
    (main / "a.txt").write_text("a\n", encoding="utf-8")
    _raw_git(main, "add", "-A")
    _raw_git(main, "commit", "-qm", "init")

    wt = main / ".hiveweave" / "worktrees" / "A099"
    wt.parent.mkdir(parents=True)
    _raw_git(main, "worktree", "add", "-q", "-b", "hw/A099/work", str(wt))
    (wt / "feature.txt").write_text("f\n", encoding="utf-8")
    _raw_git(wt, "add", "-A")
    _raw_git(wt, "commit", "-qm", "feat")
    return main, wt


def _main_tree(main: Path) -> str:
    return _raw_git(main, "rev-parse", "main^{tree}")


def _make_husk(main: Path, wt: Path) -> None:
    """把 worktree 造成「合并成功但目录被锁」之后的残留。

    三件事一起发生才算 husk（少一件就不是事故现场）：
      · 目录**还在**（rmtree 被文件锁挡住）；
      · 内容已没了；
      · `git worktree` **注册也没了**（所以 `is_dir()` 这一条判据不够）。
    """
    shutil.rmtree(main / ".git" / "worktrees" / "A099", ignore_errors=True)
    for child in sorted(wt.iterdir(), reverse=True):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    assert wt.is_dir(), "husk 目录必须还在"
    assert not (wt / ".git").exists(), "husk 的 .git 指针应已消失"


# ── 1-1：锚必须钉住工作树 ────────────────────────────────────────────


@_win_only
@pytest.mark.asyncio
async def test_platform_git_does_not_treat_cwd_as_the_work_tree(
    repo_with_worktree,
):
    """★ 1-1 验收（状态判据）：cwd 落在**非工作树**的目录里时，
    平台 git 不得把那里当成工作树根。

    这就是 #21 的第 3 环。修法不是回退锚，是**补全**：锚本就该同时钉住
    「仓库 + 公共目录 + **工作树**」。
    """
    main, _wt = repo_with_worktree
    stray = main / ".vite"
    stray.mkdir()

    ok, top = await _git(
        ["rev-parse", "--show-toplevel"], str(stray), project_root=str(main)
    )
    assert ok, top
    assert Path(top.strip()).resolve() == main.resolve(), (
        f"cwd 被当成了工作树根：--show-toplevel = {top.strip()!r}"
    )

    ok2, deleted = await _git(
        ["ls-files", "-d"], str(stray), project_root=str(main)
    )
    assert ok2, deleted
    assert deleted.strip() == "", (
        f"在非工作树目录里，全部 tracked 文件被算成已删除（这正是清空主干的第 3 环）："
        f"{deleted!r}"
    )


# ── 1-2 / 1-3：husk 上 checkpoint 必须拒绝，且主干一字不变 ────────────


@_win_only
@pytest.mark.asyncio
async def test_checkpoint_refuses_husk_and_main_tree_unchanged(
    repo_with_worktree,
):
    """★ 1-3（行为测试）：husk 上跑 checkpoint ⇒ **拒绝** 且 `main^{tree}` 不变。

    先看住"不提交"（1-2 的修法），再看住"主干没被改"（最终后果）。
    两处断言都对着状态：`main^{tree}` 是复算出来的哈希，不是文案。
    """
    main, wt = repo_with_worktree
    _make_husk(main, wt)
    tree_before = _main_tree(main)
    head_before = _raw_git(main, "rev-parse", "main")

    svc = GitWorktreeService()
    res = await svc.checkpoint(str(main), "A099", "wipe")

    assert res.get("success") is False, (
        f"husk 上 checkpoint 竟然成功了 —— 它会把整棵树记成删除并提交到 main：{res}"
    )
    assert _main_tree(main) == tree_before, "主干树被这次 checkpoint 改写了"
    assert _raw_git(main, "rev-parse", "main") == head_before, "主干 HEAD 被推进了"
    # 拒绝要给出可操作的原因（不是一句泛泛的失败）
    msg = res.get("message") or ""
    assert msg, res


@_win_only
@pytest.mark.asyncio
async def test_checkpoint_still_works_on_a_healthy_worktree(
    repo_with_worktree,
):
    """★ 反向对照：**健康** worktree 上 checkpoint 必须照常工作。

    没有这条，上面那条可以通过"checkpoint 永远拒绝"来满足 —— 那是把功能关掉，
    不是修缺陷。（本仓踩过：只为让守卫转绿而放宽断言。）
    """
    main, wt = repo_with_worktree
    tree_before = _main_tree(main)
    (wt / "more.txt").write_text("m\n", encoding="utf-8")

    svc = GitWorktreeService()
    res = await svc.checkpoint(str(main), "A099", "healthy")

    assert res.get("success") is True, res
    assert res.get("hash"), res
    # checkpoint 提交在 **worktree 的分支**上，不该动 main
    assert _main_tree(main) == tree_before, "健康 checkpoint 不该改主干树"
    assert _raw_git(wt, "log", "-1", "--format=%s").startswith("checkpoint:")


# ── 同族的第二个落点：`rollback`（审计 1-2 必修项）────────────────────


@_win_only
@pytest.mark.asyncio
async def test_rollback_refuses_on_husk_and_does_not_touch_main(
    repo_with_worktree,
):
    """★ husk 上 `rollback` 不得改写主干。

    为什么单独一条：`rollback` 先调 `checkpoint` 存档、**再 `git reset --hard`**。
    原来它丢弃 checkpoint 的回执 ⇒ 在 husk 上会继续往下走，而
    `git log --grep=checkpoint: -1`（cwd=husk、gitdir 锚定到 `<main>/.git`）
    取到的是**主干上的** checkpoint 提交 ⇒ `reset --hard` 把**主干 tip 与主干
    工作区**一起改写（审计实测：main tip 969f80ac→65384be7、工作区文件被删）。
    与 checkpoint 同形、同族，只差一行短路。

    构造：让 main 上有一个**非 tip** 的 `checkpoint:` 提交（`merge_support` 真会
    往 main 写这种提交），这样"被 reset 回去"是可观测的。
    """
    main, wt = repo_with_worktree
    _raw_git(main, "commit", "--allow-empty", "-qm", "checkpoint: on-main-probe")
    _raw_git(main, "commit", "--allow-empty", "-qm", "later")
    tip_before = _raw_git(main, "rev-parse", "main")

    _make_husk(main, wt)

    res = await GitWorktreeService().rollback(str(main), "A099")

    assert res.get("success") is False, (
        f"husk 上 rollback 竟然成功了 —— 它会把主干 reset 到某个 checkpoint：{res}"
    )
    assert _raw_git(main, "rev-parse", "main") == tip_before, (
        "主干 tip 被 rollback 改写了"
    )


# ── husk 的**其余形态**（审计 1-2 第 3 条：只造了一种 husk 不够）────────


@_win_only
@pytest.mark.asyncio
async def test_checkpoint_refuses_husk_with_intact_pointer_but_no_registration(
    repo_with_worktree,
):
    """husk 形态 D1：**注册没了，但 `.git` 指针与文件都还在**。

    与 D2（内容与指针都没了，走 `_has_git`）不同，这一档只能靠权威判据
    `git worktree list --porcelain` 认出来。审计实测过它会被拒，这里把它钉住。
    """
    main, wt = repo_with_worktree
    shutil.rmtree(main / ".git" / "worktrees" / "A099", ignore_errors=True)
    assert (wt / ".git").exists(), "本形态要求指针文件仍在"
    assert (wt / "feature.txt").exists(), "本形态要求内容仍在"
    tree_before = _main_tree(main)

    res = await GitWorktreeService().checkpoint(str(main), "A099", "no-reg")

    assert res.get("success") is False, res
    assert _main_tree(main) == tree_before


@_win_only
@pytest.mark.asyncio
async def test_checkpoint_allows_legitimately_emptied_worktree(repo_with_worktree):
    """★ **必须放行**的一档（审计 1-2 第 3 条的 D3）：在册工作树里
    agent 真的把文件都删了 ⇒ checkpoint 必须照常成功。

    没有这条，"把空树误判成 husk ⇒ 一律拒绝"这种退化能通过上面所有用例
    —— 那是把功能关掉冒充修缺陷（本仓踩过：只为让守卫转绿而放宽断言）。
    判据落在状态上：`success=True`、提交落在 **worktree 分支**、`main^{tree}` 不变。
    """
    main, wt = repo_with_worktree
    tree_before = _main_tree(main)
    for child in sorted(wt.iterdir(), reverse=True):
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)

    res = await GitWorktreeService().checkpoint(str(main), "A099", "legit-empty")

    assert res.get("success") is True, res
    assert res.get("hash"), res
    # 0-1 的体积量具应当**如实报警**（树 3→0）但**不拦门** —— 这正是它的设计口径
    assert res.get("volume_alarm") is True, res
    assert _main_tree(main) == tree_before, "合法清空不该动主干树"
    assert _raw_git(wt, "log", "-1", "--format=%s").startswith("checkpoint:")


# ── 1-1 的核心契约：**纯数据**断言（平台无关，CI 上也能跑）──────────────
#
# 为什么单独一条、且**不带 win32 skip**：上面三条行为用例只在 win32 上验证过，
# 而 CI 是 `ubuntu-latest` ⇒ 整模块 skip 会让本批最核心的契约在 CI 上**零覆盖**。
# 这条不碰子进程、不碰布局，只断言锚**声明**要钉住三样东西 ——
# 它足以在 Linux CI 上挡住"有人把 --work-tree 删回去"这类回归。
def test_anchor_pins_work_tree_alongside_gitdir_and_common_dir(tmp_path):
    """锚必须同时钉住「仓库 + 公共目录 + **工作树**」三者（#21 第 3 环）。"""
    from hiveweave.services.git_worktree.git_anchor import GitAnchor

    a = GitAnchor(
        kind="worktree",
        project_root=str(tmp_path / "proj"),
        git_dir=str(tmp_path / "proj" / ".git" / "worktrees" / "A099"),
        common_dir=str(tmp_path / "proj" / ".git"),
        work_tree=str(tmp_path / "proj" / ".hiveweave" / "worktrees" / "A099"),
    )
    joined = " ".join(a.args)
    assert f"--git-dir={a.git_dir}" in joined
    assert f"--work-tree={a.work_tree}" in joined, (
        "锚没钉工作树 ⇒ git 会把 cwd 当工作树根（#21 成因链第 3 环，实测会把主干清空）"
    )
    assert a.env["GIT_DIR"] == a.git_dir
    assert a.env["GIT_COMMON_DIR"] == a.common_dir
    assert a.env["GIT_WORK_TREE"] == a.work_tree, (
        "env 少了 GIT_WORK_TREE —— 只钉 --git-dir 的那条路已被实测证伪"
    )
