"""2026-08-11 slack-clone_01 ROUND3 三平台 bug 修复回归.

- Bug 1: husk 死区 —— reconcile 曾跳过 active agent 的 husk（无 .git 非活树）
  + rmtree 失败静默；现统一 _rmtree_husk（protected husk 也清 + 失败计数重试告警）
- Bug 2: merge husk 自动修复 —— D3 预条件检测到 husk 自动重建规范 worktree
- Bug 3: approve evidence gate 失败自动 rework —— assignee 可立即重交
  （不再死等 reviewer 手动 rework）
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing

from hiveweave.services.git_worktree import GitWorktreeService


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return (r.stdout or "").strip()

from hiveweave.services.git_worktree.reconcile import (
    _HUSK_RETRY_MAX,
    _husk_remove_failures,
    _rmtree_husk,
)
from hiveweave.services.task import TaskService
from hiveweave.tools.result import ToolResult
from hiveweave.tools.tasks.lifecycle import UpdateTaskStatusParams  # noqa: F401
from hiveweave.tools.tasks.review import (
    ReviewTaskParams,
    _auto_rework_on_evidence_gate,
    review_task_tool,
)

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


@pytest.fixture(autouse=True)
def clean_husk_retry_state():
    """模块级 _husk_remove_failures 跨测试泄漏（审计发现：失败测试先跑会让
    成功测试断言全空失败）——每个测试前后清空。"""
    _husk_remove_failures.clear()
    yield
    _husk_remove_failures.clear()


# ── Bug 1: husk 删除 + 失败重试 ─────────────────────────────


def test_rmtree_husk_success_clears_retry_count(tmp_path):
    husk = tmp_path / "A023"
    husk.mkdir()
    report = {}
    _husk_remove_failures[str(husk).lower()] = 2  # 之前失败过
    with patch(
        "hiveweave.services.git_worktree.reconcile.shutil.rmtree",
        side_effect=lambda *a, **k: husk.rmdir(),  # 模拟成功删除
    ):
        _rmtree_husk(husk, report, str(tmp_path), source="husk")
    assert report["removed_dirs"] == 1
    assert not _husk_remove_failures  # 重试计数清除


def test_rmtree_husk_failure_counts_and_escalates(tmp_path, monkeypatch):
    husk = tmp_path / "A023"
    husk.mkdir()
    report = {}
    _husk_remove_failures.clear()
    # 模拟 rmtree 失败（Windows Device busy —— 目录仍在）
    def fake_rmtree(*a, **k):
        pass

    with patch(
        "hiveweave.services.git_worktree.reconcile.shutil.rmtree",
        side_effect=fake_rmtree,
    ):
        for i in range(1, _HUSK_RETRY_MAX + 1):
            _rmtree_husk(husk, report, str(tmp_path), source="husk")
    assert len(report["errors"]) == _HUSK_RETRY_MAX
    assert f"attempt {_HUSK_RETRY_MAX}" in report["errors"][-1]
    assert _husk_remove_failures[str(husk).lower()] == _HUSK_RETRY_MAX


# ── Bug 2: merge husk 自动修复 ──────────────────────────────


def _make_gwt():
    gwt = GitWorktreeService()
    return gwt


@pytest.mark.asyncio
async def test_auto_repair_husk_success(tmp_path, monkeypatch):
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)
    branch = "hw/A023/work"

    with (
        patch(
            "hiveweave.services.git_worktree.service_merge.shutil.rmtree",
            side_effect=lambda *a, **k: husk.rmdir(),
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._git",
            new=AsyncMock(return_value=(True, "added")),
        ) as git,
    ):
        err = await gwt._auto_repair_husk(
            str(tmp_path), "A023", str(husk), branch
        )
    assert err is None
    assert not husk.exists()
    git.assert_awaited_once()
    cmd = git.await_args.args[0]  # _git(args, cwd)
    assert cmd[:2] == ["worktree", "add"]


@pytest.mark.asyncio
async def test_auto_repair_husk_locked_dir_returns_error(tmp_path):
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)

    with (
        patch(
            "hiveweave.services.git_worktree.service_merge.shutil.rmtree",
            side_effect=lambda *a, **k: None,  # 删不掉（锁住）
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._git",
            new=AsyncMock(return_value=(True, "")),
        ),
    ):
        err = await gwt._auto_repair_husk(
            str(tmp_path), "A023", str(husk), "hw/A023/work"
        )
    assert err is not None
    assert "locked" in err or "remove husk" in err


@pytest.mark.asyncio
async def test_auto_repair_husk_git_add_failure_returns_error(tmp_path):
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)

    with (
        patch(
            "hiveweave.services.git_worktree.service_merge.shutil.rmtree",
            side_effect=lambda *a, **k: husk.rmdir(),
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._git",
            new=AsyncMock(return_value=(False, "fatal: branch in use")),
        ),
    ):
        err = await gwt._auto_repair_husk(
            str(tmp_path), "A023", str(husk), "hw/A023/work"
        )
    assert err is not None
    assert "worktree add" in err


@pytest.mark.asyncio
async def test_merge_preconditions_husk_auto_repairs(tmp_path, monkeypatch):
    """D3 预条件：husk 分支自动修复后继续校验（不再死报错误）。"""
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)
    branch = "hw/A023/work"

    calls = {"n": 0}

    async def fake_git(cmd, cwd):
        head = cmd[0] if cmd else ""
        if head == "worktree" and len(cmd) >= 2 and cmd[1] == "add":
            # 模拟 add 成功：重建目录 + 出现 .git 文件
            husk.mkdir(parents=True, exist_ok=True)
            (husk / ".git").write_text("gitdir: x")
            calls["n"] += 1
            return True, ""
        if head == "worktree" and len(cmd) >= 2 and cmd[1] == "list":
            return True, f"{husk}  {branch}"
        if head == "rev-parse":  # current_branch
            return True, branch
        if head == "rev-list":  # ahead 检查：1 个提交领先（非 no-op）
            return True, "1"
        return True, ""

    with (
        patch(
            "hiveweave.services.git_worktree.service_merge.shutil.rmtree",
            side_effect=lambda *a, **k: husk.rmdir() if husk.exists() else None,
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._git",
            new=AsyncMock(side_effect=fake_git),
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._has_git",
            side_effect=lambda p: (Path(p) / ".git").exists(),
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._current_branch",
            new=AsyncMock(return_value=branch),
        ),
    ):
        result = await gwt._validate_merge_preconditions(
            str(tmp_path), "A023", branch
        )
    assert result is None  # 修复后预条件全过 → 继续 merge
    assert calls["n"] == 1


# ── Bug 1b: delete() removed 契约诚实化 ─────────────────────


@pytest.mark.asyncio
async def test_delete_reports_removed_false_when_dir_survives(tmp_path):
    """delete() rmtree 失败（目录残留）→ removed=False 透出（不再假装成功）。"""
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)

    with (
        patch(
            "hiveweave.services.git_worktree.service_lifecycle._git",
            new=AsyncMock(return_value=(False, "")),  # remove 链全失败
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle.shutil.rmtree",
            side_effect=lambda *a, **k: None,  # 删不掉（Device busy）
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle._has_git",
            return_value=False,
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle._current_branch",
            new=AsyncMock(return_value="hw/A023/work"),
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle.LifecycleMixin._dispose_branch",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await gwt.delete(str(tmp_path), "A023")
    assert result["removed"] is False
    assert husk.exists()
    assert result["path"] == str(husk), result


@pytest.mark.asyncio
async def test_delete_measures_removed_unconditionally(tmp_path):
    """★ 0-2 后置条件（独立审计 Q7③）：`git worktree remove` **报成功**（rc=0）
    却把目录留下时，`removed` 必须仍是 False。

    为什么需要这条：原实现只在「两级 remove 都失败」的分支里量一次 ⇒
    「rc=0 的假成功」形态**对所有消费者隐形**（包括 0-2 新加的 husk 事件）。
    本仓已实证该形态真实存在（git 的删除失败只在 stderr，`_git` 只看
    returncode；`git worktree prune` 堵死那次就是这个形态）。
    判据 = **目录在不在**（状态），不是"走了哪条分支"。
    """
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)

    with (
        patch(
            "hiveweave.services.git_worktree.service_lifecycle._git",
            new=AsyncMock(return_value=(True, "")),  # remove 报成功
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle.LifecycleMixin._dispose_branch",
            new=AsyncMock(return_value=None),
        ),
        structlog.testing.capture_logs() as logs,
    ):
        result = await gwt.delete(str(tmp_path), "A023")

    assert result["removed"] is False, result          # ← 后置条件生效
    assert husk.exists()
    assert [
        e for e in logs
        if e.get("event") == "git_worktree.delete_dir_remove_failed"
    ]


# ── 0-2（09-16）：husk 必须透出成「独立可 grep 的事件」 ──────────────
#
# 背景（#21 成因链，TEST_DSH_58/59 实证 3 次主干清空）：
# 合并成功后回收 worktree 被 Windows 文件锁挡住 ⇒ 留 husk 目录；
# 此时 `[UNCOMMITTED_WORKTREE]` 门禁照常要求 checkpoint ⇒ 在 husk 里
# `git add -A` 把整棵树记成删除并提交进 main。
#
# 当时唯一的线索是 `delete()` 深处那条 `delete_dir_remove_failed`，
# 与**触发它的那次操作无从关联**（3 处 husk 全是事后靠体积留痕反推出来的）。
#
# 独立审计（09-16）推翻了首版两处设计，本块按审计后的形态写：
# ① husk 的来源有 **5 处**，首版只覆盖了 2 处（漏了 `close.py` 的 gc 补删
#    路径与 `org.py` 的 dismiss 路径 —— 而 gc 那条正是 #21 常态链的收尾）；
# ② 保守方向要选在「可疑」侧（只有 `removed is True` 才静默）—— 误报是一行
#    日志，漏报是下一次主干被清空，代价不对称。


def _husk_payload(path: Path, *, removed=False) -> dict:
    return {
        "success": True,
        "removed": removed,
        "path": str(path),
        "branch": "hw/A023/work",
        "preserved_branch": None,
    }


def _husk_events(logs):
    return [e for e in logs if "husk_left" in str(e.get("event") or "")]


def test_surface_husk_left_emits_dedicated_event(tmp_path):
    """removed=False ⇒ 落独立事件（带短号 + 路径 + 原始 removed 值）。"""
    from hiveweave.services.git_worktree.service_lifecycle import _surface_husk_left

    husk = tmp_path / "A023"
    with structlog.testing.capture_logs() as logs:
        _surface_husk_left(
            _husk_payload(husk),
            short_id="A023",
            branch="hw/A023/work",
            event="git_worktree.merge_cleanup_husk_left",
        )

    hits = [e for e in logs if e.get("event") == "git_worktree.merge_cleanup_husk_left"]
    assert len(hits) == 1, [e.get("event") for e in logs]
    assert hits[0]["short_id"] == "A023"
    assert hits[0]["path"] == str(husk)
    assert hits[0]["branch"] == "hw/A023/work"
    assert hits[0]["removed"] is False
    # 事件名必须与操作层的泛化失败事件**不同**（否则「可 grep 的独立事件」不成立）
    assert not [e for e in logs if e.get("event") == "git_worktree.merge_cleanup_failed"]


def test_surface_husk_left_silent_only_when_removed_true(tmp_path):
    """反向对照：**只有** `removed is True` 才静默。

    **没有这条，上面那条可能是"永远为真"的假守卫** ——
    把 `raw_removed is True` 放宽成 `not raw_removed` 两者都绿。
    """
    from hiveweave.services.git_worktree.service_lifecycle import _surface_husk_left

    with structlog.testing.capture_logs() as logs:
        _surface_husk_left(
            _husk_payload(tmp_path / "A023", removed=True),
            short_id="A023",
            branch="hw/A023/work",
            event="git_worktree.merge_cleanup_husk_left",
        )

    assert _husk_events(logs) == []


def test_surface_husk_left_treats_missing_removed_key_as_suspicious(tmp_path):
    """`removed` 键缺失 ⇒ 仍落事件，payload 里 `removed is None`。

    ★ 方向是「可疑」而不是「已删」（独立审计 Q5）：本事件的代价极不对称 ——
    误报 = 一行日志，漏报 = 下一次主干被清空（已实测 3 次）。生产侧唯一的
    `delete()` 生产者**必然**写这个键 ⇒ 缺键只可能是新调用点/桩/重构漏配，
    正该被看见。payload 带原始值，让「确认残留」与「判据缺失」grep 时仍可区分。
    """
    from hiveweave.services.git_worktree.service_lifecycle import _surface_husk_left

    with structlog.testing.capture_logs() as logs:
        _surface_husk_left(
            {"success": True, "branch": "hw/A023/work"},
            short_id="A023",
            branch="hw/A023/work",
            event="git_worktree.merge_cleanup_husk_left",
        )

    hits = _husk_events(logs)
    assert len(hits) == 1, [e.get("event") for e in logs]
    assert hits[0]["removed"] is None, hits[0]
    assert hits[0]["path"] == "<unknown>", hits[0]


def test_surface_husk_left_cannot_break_caller(tmp_path):
    """纯观测件**绝不许**把异常穿出去（审计 Q1）。

    调用点包在 `try:` 里：本函数一抛错，外层 `except` 就会把回执改写成
    `cleanup_failed`（丢掉 `preserved_branch` 警告）⇒ 违反「只透出、不改判定」。
    """
    from hiveweave.services.git_worktree.service_lifecycle import _surface_husk_left

    class _Boom(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("boom")

    with structlog.testing.capture_logs() as logs:
        _surface_husk_left(
            _Boom(),
            short_id="A023",
            branch="b",
            event="git_worktree.merge_cleanup_husk_left",
        )

    assert [e for e in logs if e.get("event") == "git_worktree.husk_surface_failed"]


def _init_repo_with_worktree(tmp_path, short_id: str = "A023"):
    """真实仓 + 真实 linked worktree（与平台规范落点一致）+ 一条待合并提交。"""
    main = tmp_path / "proj"
    main.mkdir()
    _git(main, "init")
    _git(main, "config", "user.email", "t@t.com")
    _git(main, "config", "user.name", "t")
    (main / "README.md").write_text("hi\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "init")
    _git(main, "branch", "-M", "main")

    wt = main / ".hiveweave" / "worktrees" / short_id
    wt.parent.mkdir(parents=True)
    _git(main, "worktree", "add", "-b", f"hw/{short_id}/work", str(wt))
    (wt / "feature.txt").write_text("f\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "feat")
    return main, wt


@pytest.mark.asyncio
async def test_merge_cleanup_surfaces_husk_when_delete_leaves_dir(tmp_path):
    """★ 穿透真实 `merge()`：只把 `delete` 换成「返回 removed=False」。

    验收（状态判据）：merge 仍 success（第 0 步**只透出、不改判定**），
    且日志里出现 `git_worktree.merge_cleanup_husk_left`。

    ⚠ **不覆盖**：`delete()` ↔ helper 的接缝（payload 是手搓的）与
    `merge_by_branch`（下一条用例专测）。
    """
    main, wt = _init_repo_with_worktree(tmp_path)
    gwt = GitWorktreeService()

    with (
        patch(
            "hiveweave.services.git_worktree.service_merge."
            "_assignee_has_open_tasks",
            new=AsyncMock(return_value=False),
        ),
        patch.object(
            gwt, "delete", new=AsyncMock(return_value=_husk_payload(wt))
        ),
        structlog.testing.capture_logs() as logs,
    ):
        result = await gwt.merge(str(main), "A023")

    assert result["success"] is True, result
    hits = [
        e for e in logs
        if e.get("event") == "git_worktree.merge_cleanup_husk_left"
    ]
    assert len(hits) == 1, [e.get("event") for e in logs]
    assert hits[0]["short_id"] == "A023"
    assert hits[0]["path"] == str(wt)
    assert hits[0]["removed"] is False


@pytest.mark.asyncio
async def test_merge_by_branch_cleanup_surfaces_husk(tmp_path):
    """★ `merge_by_branch` 才是**生产主路径**（`git_worktree_merge` 工具在
    `misc_tools.py` 的 4 个分支里全走它，只有 1 个分支走 `merge()`）。

    独立审计实测：首版只覆盖 `merge()` ⇒ 主路径的事件**改名/传错参/被删掉
    都不会红**。这条补上。
    """
    main, wt = _init_repo_with_worktree(tmp_path)
    gwt = GitWorktreeService()

    with (
        patch(
            "hiveweave.services.git_worktree.service_merge."
            "_assignee_has_open_tasks",
            new=AsyncMock(return_value=False),
        ),
        patch.object(
            gwt, "delete", new=AsyncMock(return_value=_husk_payload(wt))
        ),
        structlog.testing.capture_logs() as logs,
    ):
        result = await gwt.merge_by_branch(str(main), "hw/A023/work", "main")

    assert result["success"] is True, result
    hits = [
        e for e in logs
        if e.get("event") == "git_worktree.merge_by_branch_cleanup_husk_left"
    ]
    assert len(hits) == 1, [e.get("event") for e in logs]
    assert hits[0]["short_id"] == "A023"
    assert hits[0]["path"] == str(wt)


@pytest.mark.asyncio
async def test_worktree_remove_tool_reports_husk_without_failing(tmp_path):
    """★ `git_worktree_remove` 不许「假装删除成功」，但**也不许升成硬失败**。

    口径（审计 Q3）：第 0 步是"先能看见"；「removed=False 该不该变成失败」
    由 1-4 单独立项按全项目频次×后果评估 —— 提前 err 会把 24 次"本来没事"
    的删除失败变成停摆，并污染 1-4 的数据口径。
    """
    import hiveweave.tools.misc_tools as mt
    from hiveweave.tools.misc_tools import (
        GitWorktreeRemoveParams,
        git_worktree_remove_tool,
    )

    husk = str(tmp_path / ".hiveweave" / "worktrees" / "A023")

    async def _fake_delete(self, workspace_path, short_id, branch=None, **kw):
        return _husk_payload(Path(husk))

    with (
        patch.object(
            mt,
            "_get_worktree_context",
            new=AsyncMock(return_value=(str(tmp_path), "A023", "p1")),
        ),
        patch.object(
            GitWorktreeService, "ensure_git_repo", new=AsyncMock(return_value=None)
        ),
        patch.object(GitWorktreeService, "delete", new=_fake_delete),
        structlog.testing.capture_logs() as logs,
    ):
        res = await git_worktree_remove_tool(
            GitWorktreeRemoveParams(branchName="hw/A023/work"),
            "A023-agent",
            str(tmp_path),
        )

    assert res.success is True, res
    assert "still on disk" in (res.output or ""), res.output
    assert husk in (res.output or ""), res.output
    assert [e for e in logs if e.get("event") == "git_worktree_remove_husk_left"]


@pytest.mark.asyncio
async def test_worktree_remove_tool_ok_when_really_removed(tmp_path):
    """反向对照：removed=True ⇒ 仍回干净的 ok("Worktree removed")，且不落 husk 事件。"""
    import hiveweave.tools.misc_tools as mt
    from hiveweave.tools.misc_tools import (
        GitWorktreeRemoveParams,
        git_worktree_remove_tool,
    )

    async def _fake_delete(self, workspace_path, short_id, branch=None, **kw):
        return _husk_payload(tmp_path / "gone", removed=True)

    with (
        patch.object(
            mt,
            "_get_worktree_context",
            new=AsyncMock(return_value=(str(tmp_path), "A023", "p1")),
        ),
        patch.object(
            GitWorktreeService, "ensure_git_repo", new=AsyncMock(return_value=None)
        ),
        patch.object(GitWorktreeService, "delete", new=_fake_delete),
        structlog.testing.capture_logs() as logs,
    ):
        res = await git_worktree_remove_tool(
            GitWorktreeRemoveParams(branchName="hw/A023/work"),
            "A023-agent",
            str(tmp_path),
        )

    assert res.success is True, res
    assert (res.output or "") == "Worktree removed", res.output
    assert not [e for e in logs if e.get("event") == "git_worktree_remove_husk_left"]


# ── 结构性网：`delete()` 的**每一个**调用点都必须在同一函数内透出 husk ──
#
# 独立审计实测：`delete()` 的生产调用点有 **5 处**，不是首版以为的 3 处 ——
# `close.py` 的 gc 补删路径（#21 常态链的收尾）与 `org.py` 的 dismiss 路径
# （reconcile.py 注释点名的 "dismiss/reset residue"）此前都会静默丢 `removed`。
# 人肉 grep 会再漏 ⇒ 用 AST 把「新调用点必须接上」钉死。
#
# 判据是**数据流**（哪些函数调了 `delete` 却没有透出），不是文案匹配。

_DELETE_RECEIVERS = {"self", "gwt"}


def _iter_worktree_delete_sites(tree):
    """枚举「worktree 服务 delete()」的调用点 → (enclosing_fn, node)。

    只认接收者是 `self` / `gwt` / `GitWorktreeService()` 的 `.delete(...)`
    —— 排除 `SettingsService().delete()`、`_model.delete()` 这类同名调用，
    也天然排除 `@router.delete(...)`（装饰器不在函数体里）。
    """
    import ast

    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            if not isinstance(fn, ast.Attribute) or fn.attr != "delete":
                continue
            recv = fn.value
            ok = (
                (isinstance(recv, ast.Name) and recv.id in _DELETE_RECEIVERS)
                or (
                    isinstance(recv, ast.Call)
                    and isinstance(recv.func, ast.Name)
                    and recv.func.id == "GitWorktreeService"
                )
            )
            if ok:
                sites.append((node, sub))
    return sites


def test_every_worktree_delete_callsite_surfaces_husk():
    """★ 新调用点漏接 `_surface_husk_left` ⇒ 本用例转红。

    顺带钉住「单实现」：所有调用点用的都是**同一个** `_surface_husk_left`
    （它住在 `service_lifecycle`，即 `removed` 事实的产地）。
    """
    import ast
    from pathlib import Path as _P

    src_root = _P(__file__).resolve().parents[1] / "src" / "hiveweave"
    seen: list[str] = []
    missing: list[str] = []

    for py in src_root.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for fn, call in _iter_worktree_delete_sites(tree):
            rel = f"{py.relative_to(src_root)}:{call.lineno}"
            seen.append(rel)
            names = {
                n.func.id
                for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if "_surface_husk_left" not in names:
                missing.append(f"{rel} (in {fn.name})")

    # 5 处已知调用点，全部要被枚举到（**下界**也是判据：漏扫比漏接更隐蔽）
    assert len(seen) >= 5, seen
    assert not missing, missing


# ── Bug 3: approve evidence gate 自动 rework ────────────────


async def _submit_approved_flow(ts, pid, tid):
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid, tid, evidence={
            "tests_passed": True,
            "test_output": "ok",
            "files_changed": ["bad/path.py"],
        }
    )


@pytest.mark.asyncio
async def test_evidence_gate_deny_auto_rework(task_env):
    """approve 时 files_changed 校验失败 → 自动 rework，任务转回 running，
    assignee 收到通知 —— 不再死等 reviewer 手动 rework。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    await _submit_approved_flow(ts, pid, tid)
    assert (await ts.get_task(pid, tid))["status"] == "submitted"

    deny_msg = "files_changed contains bad/path.py not in worktree"
    with (
        patch(
            "hiveweave.services.worktree_review.review_worktree_gate",
            new=AsyncMock(return_value=(None, {})),
        ),
        patch(
            "hiveweave.services.worktree_review.check_evidence_verifiable",
            new=AsyncMock(return_value=deny_msg),
        ),
        # 绕过前序 attestation/reviewer-execution gate（本测试只关注
        # evidence gate 的自动 rework）
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            return_value=[],
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.reviewer_required_kinds",
            return_value=[],
        ),
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=pid),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            new=AsyncMock(),
        ) as trigger,
    ):
        params = ReviewTaskParams(task_id=tid, decision="approve")
        result = await review_task_tool(params, COORD, "/tmp/ws")

    assert result.success is False
    assert "auto-rework" in (result.error or "")
    assert (await ts.get_task(pid, tid))["status"] == "running"
    # assignee 收到 rework 通知
    assert send.await_count >= 1
    assert "REWORK REQUESTED" in send.await_args.kwargs["message"]


@pytest.mark.asyncio
async def test_auto_rework_ignores_verify_and_plain_gate_deny(task_env):
    """非 evidence-gate 拒绝（review_worktree_gate）不触发自动 rework
    （环境类问题不自动循环）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    await _submit_approved_flow(ts, pid, tid)

    with (
        patch(
            "hiveweave.services.worktree_review.review_worktree_gate",
            new=AsyncMock(return_value=("no worktree path", {})),
        ),
        patch(
            "hiveweave.services.worktree_review.check_evidence_verifiable",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=pid),
        ),
    ):
        params = ReviewTaskParams(task_id=tid, decision="approve")
        result = await review_task_tool(params, COORD, "/tmp/ws")

    assert result.success is False
    assert "auto-rework" not in (result.error or "")
    assert (await ts.get_task(pid, tid))["status"] == "submitted"


@pytest.mark.asyncio
async def test_auto_rework_on_evidence_gate_direct(task_env):
    """_auto_rework_on_evidence_gate 直接调用：submitted → reviewing → running，
    review obligation fulfill。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    await _submit_approved_flow(ts, pid, tid)

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            new=AsyncMock(),
        ),
    ):
        await _auto_rework_on_evidence_gate(
            pid, MagicMock(task_id=tid, feedback=None), COORD, None,
            "bad files_changed",
        )
    assert (await ts.get_task(pid, tid))["status"] == "running"


# ── E2 工具层配套（2026-08-25 TEST_DSH_28 实锤）───────────────
# approve 被 service 强制 rework（verdict=FAIL）后，工具层不得按 approve
# 收尾：不发 [TASK APPROVED]、不注入 merge/close；按 rework 通知+短路。


@pytest.mark.asyncio
async def test_tool_approve_forced_rework_no_approved_notice(task_env):
    """非 VERIFY 任务 + verdict=FAIL → review_task_tool(approve) 后任务
    running、assignee 收到 REWORK REQUESTED（非 TASK APPROVED）、无
    merge/close 注入、返回文案点名 verdict gate。

    变异: 删掉工具层 forced_rework 短路 → 通知变 [TASK APPROVED] →
    本测试失败（误发「已批准」+ 对运行中任务注入 merge pending 复现）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "[probe] 非 VERIFY 的 FAIL 探针", "d",
        creator_id=COORD, assignee_id=EXEC,
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid,
        tid,
        evidence={
            "verdict": "FAIL",
            "blocking_issues": ["/_admin 404"],
            "tests_passed": True,  # 满足 approve 前 attestation 软校验
        },
    )
    await ts.start_review(pid, tid)

    with (
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            return_value=[],
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.reviewer_required_kinds",
            return_value=[],
        ),
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=pid),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.worktree_commits_ahead",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            new=AsyncMock(return_value=None),
        ),
        patch(
            # 加固（审计 m4）：forced_rework 时 merge pending 不得注入
            "hiveweave.tools.tasks.review._inject_merge_pending_wake",
            new=AsyncMock(),
        ) as merge_wake,
    ):
        params = ReviewTaskParams(task_id=tid, decision="approve")
        result = await review_task_tool(params, COORD, "/tmp/ws")

    assert result.success is True
    assert "forced back to rework" in (result.output or "")
    assert (await ts.get_task(pid, tid))["status"] == "running"
    msgs = [c.kwargs.get("message") or "" for c in send.await_args_list]
    assert any("REWORK REQUESTED" in m for m in msgs), msgs
    assert not any("TASK APPROVED" in m for m in msgs), msgs
    assert not any("MERGE PENDING" in m for m in msgs), msgs
    merge_wake.assert_not_awaited()
