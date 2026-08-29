"""P1-4 + P2 回归测试（platform-issue-report-DSH_HiveWeave 修复防线）。

覆盖：
- P1-4 迁移避让：.gitignore 自身 dirty 时 _migrate_legacy_hiveweave_ignore 跳过；
  无关 dirty（README）不推迟迁移。
- P1-4 classify_main_dirt 返回 user_suspect author 事实位。
- P2-6 git_worktree_status 无 worktree 角色回 MAIN 视角（不再裸报 No worktree）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from hiveweave.services.git_worktree import GitWorktreeService
from hiveweave.services.git_worktree.merge_support import classify_main_dirt


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return (r.stdout or "").strip()


def _legacy_repo(tmp_path: Path, name: str) -> Path:
    ws = tmp_path / name
    ws.mkdir()
    _git(ws, "init")
    _git(ws, "config", "user.email", "t@t.com")
    _git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("hi\n", encoding="utf-8")
    _git(ws, "add", "README.md")
    _git(ws, "commit", "-m", "init")
    (ws / ".gitignore").write_text(".hiveweave/\nother\n", encoding="utf-8")
    _git(ws, "add", ".gitignore")
    _git(ws, "commit", "-m", "legacy ignore")
    return ws


async def test_ignore_migration_defers_when_gitignore_dirty(tmp_path: Path):
    """P1-4：.gitignore 自身被人工 dirty → 迁移必须避让（不覆盖用户编辑）。"""
    ws = _legacy_repo(tmp_path, "defer")
    (ws / ".gitignore").write_text(".hiveweave/\nuser-edit\n", encoding="utf-8")
    svc = GitWorktreeService()
    await svc._migrate_legacy_hiveweave_ignore(str(ws))
    gi = (ws / ".gitignore").read_text(encoding="utf-8")
    assert ".hiveweave/" in gi          # 保持 legacy（未迁移）
    assert ".hiveweave/*" not in gi
    assert "user-edit" in gi            # 用户编辑未被覆盖


async def test_ignore_migration_proceeds_on_unrelated_dirty(tmp_path: Path):
    """P1-4：无关 dirty（README）不推迟迁移 —— 迁移 pathspec 只碰 .gitignore。"""
    ws = _legacy_repo(tmp_path, "unrelated")
    (ws / "README.md").write_text("hi2\n", encoding="utf-8")  # 人工改 README
    svc = GitWorktreeService()
    await svc._migrate_legacy_hiveweave_ignore(str(ws))
    gi = (ws / ".gitignore").read_text(encoding="utf-8")
    assert ".hiveweave/*" in gi         # 迁移照常执行
    assert "!.hiveweave/shared/" in gi


async def test_classify_main_dirt_exposes_user_suspect(tmp_path: Path):
    """P1-4：classify_main_dirt 返回 user_suspect（author 事实位）。"""
    ws = _legacy_repo(tmp_path, "author")
    (ws / "README.md").write_text("hi-x\n", encoding="utf-8")
    dirt = await classify_main_dirt(str(ws))
    assert "user_suspect" in dirt
    assert "README.md" in dirt["user_suspect"]
    assert set(dirt["user_suspect"]) == set(dirt["hard_blockers"])


async def test_worktree_status_no_worktree_returns_main_view(
    tmp_path: Path, monkeypatch
):
    """P2-6：无 worktree 角色查自己 → 回 MAIN 项目根状态而非 No worktree 报错。"""
    from hiveweave.tools import misc_tools
    import hiveweave.services.git_worktree as gwt_mod

    ws = _legacy_repo(tmp_path, "ceo")
    _git(ws, "branch", "-M", "main")
    (ws / "README.md").write_text("hi-ceo\n", encoding="utf-8")

    class _FakeInfoSvc:
        async def ensure_git_repo(self, ws): return {"success": True}
        async def info(self, ws, sid): return {"success": True, "status": None}

    async def _fake_wt_ctx(agent_id, ctx):
        return str(ws), "A296", None

    # 工具函数内 `from hiveweave.services.git_worktree import GitWorktreeService`
    monkeypatch.setattr(gwt_mod, "GitWorktreeService", _FakeInfoSvc)
    monkeypatch.setattr(misc_tools, "_get_worktree_context", _fake_wt_ctx)

    result = await misc_tools.git_worktree_status_tool(
        misc_tools.GitWorktreeStatusParams(), "a296", str(ws)
    )
    assert result.success is True
    out = result.output
    assert "MAIN" in out
    assert "main" in out.lower()
    assert "No worktree found" not in out