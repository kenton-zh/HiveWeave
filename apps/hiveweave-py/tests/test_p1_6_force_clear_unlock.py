"""P1-6 回归：worktree 清理路径（``_force_clear_path``）的锁死契约 + 去静默。

实测事故链：清 stale worktree 路径时既没先 ``unlock_git_lockdown()``（AGENTS.md
Git/ACL 锁死契约 —— 删/移 ``.git`` 附近路径前必须解锁），又用
``rmtree(ignore_errors=True)`` 把 Windows 文件锁下的半删吞成「看似成功」
⇒ gitdir 指针被删成空壳 → 沙箱校验 fail-closed → 该 agent 工具面全灭
（SandboxUnavailable 8+8 次、worktree_rebuild 9+4 次、cwd 不存在 12 次）。

本文件锁死四个单元面：
1. rmtree 前解锁被调用，且项目根从 ``<proj>/.hiveweave/worktrees/<sid>``
   正确反推；
2. 解锁失败降级继续（best-effort，不挡清理）；
3. rmtree 被锁 + rename-aside 也被锁 ⇒ 返回 **False**（上层走 repair /
   relocation 分支报错），绝不静默宣称已清；
4. rmtree 被锁但 rename-aside 成功 ⇒ 合法让位路径仍返回 True（既有语义）。
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest

from hiveweave.services.git_worktree import paths


def _make_worktree_dir(tmp_path: pathlib.Path, sid: str = "A1") -> pathlib.Path:
    proj = tmp_path / "proj"
    target = proj / ".hiveweave" / "worktrees" / sid
    target.mkdir(parents=True)
    (target / "src").mkdir()
    (target / "src" / "main.py").write_text("x = 1", encoding="utf-8")
    return target


@pytest.fixture
def unlock_calls(monkeypatch):
    """把 ``unlock_git_lockdown`` 换成记录调用参数的桩。"""
    import hiveweave.services.acl_sandbox.service as acl_service

    calls: list[str] = []
    monkeypatch.setattr(
        acl_service, "unlock_git_lockdown",
        lambda root: calls.append(str(root)) or 1,
    )
    return calls


@pytest.mark.asyncio
async def test_force_clear_unlocks_derived_project_root(tmp_path, unlock_calls):
    """删前解锁，且项目根从 .hiveweave 祖先正确反推；删净返回 True。"""
    target = _make_worktree_dir(tmp_path)

    assert paths._force_clear_path(str(target)) is True
    assert not target.exists()
    assert unlock_calls == [str(tmp_path / "proj")]

    # 路径本就不存在 ⇒ 快路径 True，且不解锁（无 I/O 侧效应）
    unlock_calls.clear()
    assert paths._force_clear_path(str(target)) is True
    assert unlock_calls == []


@pytest.mark.asyncio
async def test_force_clear_unlock_failure_degrades(tmp_path, monkeypatch):
    """解锁抛错 ⇒ 降级继续清理（best-effort），不挡、不炸。"""
    import hiveweave.services.acl_sandbox.service as acl_service

    target = _make_worktree_dir(tmp_path)

    def boom(root: str) -> int:
        raise RuntimeError("acl service unavailable")

    monkeypatch.setattr(acl_service, "unlock_git_lockdown", boom)

    assert paths._force_clear_path(str(target)) is True
    assert not target.exists()


@pytest.mark.asyncio
async def test_force_clear_returns_false_when_rename_also_locked(
    tmp_path, monkeypatch, unlock_calls
):
    """rmtree 与 rename-aside 双双被锁 ⇒ False + 目录仍在（不静默宣称已清）。

    Windows 文件锁（agent 进程还活着）删不掉是合法路径 —— 判据交上层
    （repair / relocation 分支），但绝不能返回 True。
    """

    def locked_rmtree(path, *args, **kwargs):
        raise OSError(32, "The process cannot access the file")

    def locked_rename(self, dest):
        raise OSError(32, "The process cannot access the file")

    monkeypatch.setattr(paths.shutil, "rmtree", locked_rmtree)
    monkeypatch.setattr(pathlib.Path, "rename", locked_rename)

    target = _make_worktree_dir(tmp_path, sid="A2")

    assert paths._force_clear_path(str(target)) is False
    assert target.exists()  # 未删净 —— 状态判据，不是返回值粉饰
    assert unlock_calls == [str(tmp_path / "proj")]  # 删前解锁仍被调用


@pytest.mark.asyncio
async def test_force_clear_rename_aside_fallback(tmp_path, monkeypatch, unlock_calls):
    """rmtree 被锁但 rename-aside 成功 ⇒ 合法让位路径返回 True（既有语义）。"""

    def locked_rmtree(path, *args, **kwargs):
        raise OSError(32, "The process cannot access the file")

    monkeypatch.setattr(paths.shutil, "rmtree", locked_rmtree)

    target = _make_worktree_dir(tmp_path, sid="A3")

    assert paths._force_clear_path(str(target)) is True
    assert not target.exists()
    asides = list(target.parent.glob(".stale-A3-*"))
    assert len(asides) == 1  # aside 让位成功（残留由 reconcile .stale-* 清理兜底）


@pytest.mark.asyncio
async def test_force_clear_unlock_failure_is_logged_not_silent(tmp_path, monkeypatch):
    """解锁失败必须留 warning 痕（本仓把静默失效当一等缺陷）。"""
    import hiveweave.services.acl_sandbox.service as acl_service

    target = _make_worktree_dir(tmp_path, sid="A4")

    def boom(root: str) -> int:
        raise RuntimeError("acl unavailable")

    monkeypatch.setattr(acl_service, "unlock_git_lockdown", boom)

    with patch.object(paths.log, "warning") as warn:
        assert paths._force_clear_path(str(target)) is True
    events = [c.args[0] for c in warn.call_args_list if c.args]
    assert "git_worktree.force_clear_unlock_failed" in events
