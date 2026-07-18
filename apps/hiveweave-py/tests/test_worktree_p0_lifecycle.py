"""P0 worktree 生命周期修复 — 命名稳定化 / 删除安全链 / 孤儿回收对账。

借鉴 Orca 生命周期语义:
- compute_branch_name: task_id 派生稳定名, 与任务描述文本无关 (治分支增生)
- delete: git branch -d 拒删未合并分支则保留透出; 仅 discard=True CAS 强删
- reconcile_worktrees: 注册表 / 磁盘 / 任务表三方核对, 绝不强删未合并分支
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hiveweave.services.git_worktree import (
    GitWorktreeService,
    WORKTREE_DIR,
    compute_branch_name,
    reconcile_worktrees,
)


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        # git 输出是 UTF-8; 中文 Windows 默认 GBK 会 UnicodeDecodeError
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


def _branches(repo: Path) -> list[str]:
    out = _git(repo, "branch", "--list", "hw/*/*")
    # 检出标记: 当前 worktree 用 *, 其他 worktree 检出用 +
    return [ln.strip().lstrip("*+ ").strip()
            for ln in out.splitlines() if ln.strip()]


async def _create_with_commit(repo: Path, short_id: str,
                              task_id: str | None,
                              filename: str = "work.txt") -> dict:
    """创建 executor worktree 并在其分支上提交一个文件 (未合并状态)。"""
    gwt = GitWorktreeService()
    res = await gwt.create(str(repo), short_id, task_id=task_id)
    assert res["success"] is True, res
    wt = Path(res["path"])
    (wt / filename).write_text(f"from {short_id}\n", encoding="utf-8")
    _git(wt, "add", filename)
    _git(wt, "commit", "-m", f"work on {filename}")
    return res


# ── 任务一: 分支命名稳定化 ──────────────────────────────────


class TestStableNaming:
    def test_task_id_derived_name(self) -> None:
        assert (compute_branch_name("A004", "12345678-ABCD-efgh")
                == "hw/A004/t-12345678")

    def test_same_task_id_same_name(self) -> None:
        a = compute_branch_name("A004", "deadbeef-0000-aaaa")
        b = compute_branch_name("A004", "deadbeef-0000-aaaa")
        assert a == b == "hw/A004/t-deadbeef"

    def test_no_task_id_falls_back_to_work(self) -> None:
        assert compute_branch_name("A004") == "hw/A004/work"
        assert compute_branch_name("A004", None) == "hw/A004/work"
        assert compute_branch_name("A004", "  ") == "hw/A004/work"

    def test_short_task_id(self) -> None:
        assert compute_branch_name("A004", "abc") == "hw/A004/t-abc"

    async def test_create_name_independent_of_task_name(
        self, git_repo: Path
    ) -> None:
        """task_name 保留兼容但不再参与命名 — 描述文本改动不再增生分支。"""
        gwt = GitWorktreeService()
        tid = "cafef00d-1234-5678"
        r1 = await gwt.create(str(git_repo), "A004", "卡槽系统工程师",
                              task_id=tid)
        assert r1["branch"] == "hw/A004/t-cafef00d"

        r2 = await gwt.create(str(git_repo), "A004", "完全无关的另一段描述",
                              task_id=tid)
        assert r2["branch"] == "hw/A004/t-cafef00d"
        assert r1["path"] == r2["path"]
        assert _branches(git_repo) == ["hw/A004/t-cafef00d"]

    async def test_create_without_task_id_uses_work_branch(
        self, git_repo: Path
    ) -> None:
        gwt = GitWorktreeService()
        r = await gwt.create(str(git_repo), "A007", "随便什么角色名")
        assert r["branch"] == "hw/A007/work"


class TestIdempotentReturnsActualBranch:
    async def test_second_create_returns_checked_out_branch(
        self, git_repo: Path
    ) -> None:
        """幂等脱钩: 路径已存在时返回实际检出分支, 不按新入参重算。"""
        gwt = GitWorktreeService()
        r1 = await gwt.create(str(git_repo), "A004", task_id="11111111-aaaa")
        assert r1["branch"] == "hw/A004/t-11111111"

        # 第二次传了不同的 task_id — 必须返回现有 worktree 的真实分支
        r2 = await gwt.create(str(git_repo), "A004", task_id="22222222-bbbb")
        assert r2["success"] is True
        assert r2["branch"] == "hw/A004/t-11111111"
        assert "already exists" in r2["message"]
        assert _branches(git_repo) == ["hw/A004/t-11111111"]  # 没有增生

    async def test_legacy_slug_branch_returned_as_is(
        self, git_repo: Path
    ) -> None:
        """存量 legacy slug 分支的 worktree 幂等返回其真实分支名。"""
        gwt = GitWorktreeService()
        r1 = await gwt.create(str(git_repo), "A005")
        wt = Path(r1["path"])
        _git(wt, "branch", "-m", "hw/A005/work", "hw/A005/老slug分支")

        r2 = await gwt.create(str(git_repo), "A005")
        assert r2["branch"] == "hw/A005/老slug分支"


# ── 任务二: 删除安全链 ──────────────────────────────────────


class TestDeleteSafetyChain:
    async def test_unmerged_branch_preserved(self, git_repo: Path) -> None:
        """默认删除: git branch -d 拒删未合并分支 → 保留并透出。"""
        res = await _create_with_commit(git_repo, "A004", "deadbeef-0001")
        gwt = GitWorktreeService()

        out = await gwt.delete(str(git_repo), "A004")

        assert out["success"] is True and out["removed"] is True
        assert not Path(res["path"]).exists()  # worktree 已删
        assert out["branch"] == "hw/A004/t-deadbeef"
        pb = out["preserved_branch"]
        assert pb is not None
        assert pb["branch"] == "hw/A004/t-deadbeef"
        assert pb["reason"] == "unmerged"
        assert len(pb["head"]) == 7
        assert "hw/A004/t-deadbeef" in _branches(git_repo)  # 分支仍在

    async def test_merged_branch_deleted_via_merge_chain(
        self, git_repo: Path
    ) -> None:
        """merge 成功后连带删分支走 -d — 已合并必然成功。"""
        await _create_with_commit(git_repo, "A004", "deadbeef-0002")
        gwt = GitWorktreeService()

        res = await gwt.merge(str(git_repo), "A004", task_id="deadbeef-0002")

        assert res["success"] is True, res
        assert res["branch"] == "hw/A004/t-deadbeef"
        assert _branches(git_repo) == []
        assert not (git_repo / WORKTREE_DIR / "A004").exists()

    async def test_merge_resolves_legacy_slug_branch(
        self, git_repo: Path
    ) -> None:
        """legacy 兼容: 老 slug 分支能 merge (按实际检出分支解析)。"""
        gwt = GitWorktreeService()
        res = await gwt.create(str(git_repo), "A004")
        wt = Path(res["path"])
        _git(wt, "branch", "-m", "hw/A004/work", "hw/A004/老任务slug")
        (wt / "legacy.py").write_text("print('old')\n", encoding="utf-8")
        _git(wt, "add", "legacy.py")
        _git(wt, "commit", "-m", "legacy work")

        # 调用方传了一个对不上的 task_name — 旧代码会算错分支名
        out = await gwt.merge(str(git_repo), "A004", "完全不同的任务名")

        assert out["success"] is True, out
        assert out["branch"] == "hw/A004/老任务slug"
        assert (git_repo / "legacy.py").exists()
        assert _branches(git_repo) == []

    async def test_discard_cas_deletes_unmerged(self, git_repo: Path) -> None:
        """discard=True: CAS 强删未合并分支 (确认丢弃被拒工作)。"""
        await _create_with_commit(git_repo, "A004", "deadbeef-0003")
        gwt = GitWorktreeService()

        out = await gwt.delete(str(git_repo), "A004", discard=True)

        assert out["preserved_branch"] is None
        assert _branches(git_repo) == []

    async def test_discard_cas_failure_preserves(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CAS 失败 (分支已移动) → 放弃强删并透出, 绝不盲删。"""
        await _create_with_commit(git_repo, "A004", "deadbeef-0004")
        gwt = GitWorktreeService()

        from hiveweave.services import git_worktree as gw_mod

        real_git = gw_mod._git

        async def flaky_git(args, cwd, timeout=gw_mod.GIT_TIMEOUT):
            if args[:2] == ["update-ref", "-d"]:
                return False, "cannot lock ref: is at <new> but expected <old>"
            return await real_git(args, cwd, timeout)

        monkeypatch.setattr(gw_mod, "_git", flaky_git)

        out = await gwt.delete(str(git_repo), "A004", discard=True)

        pb = out["preserved_branch"]
        assert pb is not None and pb["reason"] == "cas_failed"
        assert "hw/A004/t-deadbeef" in _branches(git_repo)  # 分支未被盲删

    async def test_legacy_task_name_fallback_after_dir_gone(
        self, git_repo: Path
    ) -> None:
        """旧调用方 (只传 task_name): 目录已消失时按 legacy slug 兜底删分支。"""
        gwt = GitWorktreeService()
        res = await gwt.create(str(git_repo), "A004")
        wt = Path(res["path"])
        _git(wt, "branch", "-m", "hw/A004/work", "hw/A004/旧功能")
        shutil.rmtree(wt)  # worktree 目录消失 (注册残留)

        out = await gwt.delete(str(git_repo), "A004", "旧功能")

        assert out["success"] is True
        assert out["preserved_branch"] is None  # 无提交 → 已合并 → -d 成功
        assert _branches(git_repo) == []

    async def test_delete_nonexistent_is_noop(self, git_repo: Path) -> None:
        gwt = GitWorktreeService()
        out = await gwt.delete(str(git_repo), "A404")
        assert out["success"] is True and out["removed"] is True
        assert out["preserved_branch"] is None


# ── 任务三: 孤儿回收对账 ────────────────────────────────────


class TestReconcile:
    async def test_prune_missing_registered_and_preserve_unmerged(
        self, git_repo: Path
    ) -> None:
        """目录消失但注册残留 → prune; 未合并孤儿分支绝不强删。"""
        res = await _create_with_commit(git_repo, "A004", "deadbeef-0010")
        shutil.rmtree(res["path"])  # 目录消失, 注册残留

        report = await reconcile_worktrees(str(git_repo))

        assert report["pruned"] == 1
        assert report["removed_dirs"] == 0
        assert report["deleted_branches"] == []
        preserved = report["preserved_branches"]
        assert [p["branch"] for p in preserved] == ["hw/A004/t-deadbeef"]
        assert preserved[0]["reason"] == "task_missing_unmerged"
        assert "hw/A004/t-deadbeef" in _branches(git_repo)

        # 注册表已清理 — 再次对账 pruned=0
        again = await reconcile_worktrees(str(git_repo))
        assert again["pruned"] == 0

    async def test_reconcile_three_way(self, git_repo: Path) -> None:
        """孤儿目录 + 孤儿分支 + 任务表三方核对。"""
        # 项目 DB + 任务行 (沿用 per-project DB 访问模式)
        from hiveweave.db.project import ensure_project_db

        conn = await ensure_project_db(str(git_repo))
        assert conn is not None
        now = 1700000000000
        await conn.execute(
            "INSERT INTO tasks (id, project_id, title, creator_id, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ["c10sed00-aaaa-bbbb", "p1", "done task", "ceo", "closed",
             now, now],
        )
        await conn.execute(
            "INSERT INTO tasks (id, project_id, title, creator_id, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ["0pen0000-aaaa-bbbb", "p1", "open task", "ceo", "running",
             now, now],
        )
        await conn.commit()

        # 1) 已合并 + 任务 closed 的 t- 分支 → 删除
        _git(git_repo, "branch", "hw/A004/t-c10sed00", "main")
        # 2) 未合并 + 任务 closed 的 t- 分支 → preserved (绝不强删)
        res5 = await _create_with_commit(git_repo, "A005", "c10sed00-work",
                                         "x.txt")
        _git(git_repo, "worktree", "remove", "--force", res5["path"])
        # 3) 已合并 + 任务 running 的 t- 分支 → 不动 (任务仍活跃)
        _git(git_repo, "branch", "hw/A006/t-0pen0000", "main")
        # 4) legacy slug 已合并 → 删除
        _git(git_repo, "branch", "hw/A007/旧slug", "main")
        # 5) legacy slug 未合并 → preserved
        res8 = await _create_with_commit(git_repo, "A008", None, "y.txt")
        _git(Path(res8["path"]), "branch", "-m", "hw/A008/work",
             "hw/A008/未完成legacy")
        _git(git_repo, "worktree", "remove", "--force", res8["path"])
        # 6) 孤儿目录 (不在注册表) → rmtree
        orphan = git_repo / WORKTREE_DIR / "A099"
        orphan.mkdir(parents=True)
        (orphan / "junk.txt").write_text("x", encoding="utf-8")

        report = await reconcile_worktrees(str(git_repo))

        assert report["removed_dirs"] == 1
        assert not orphan.exists()
        assert set(report["deleted_branches"]) == {
            "hw/A004/t-c10sed00", "hw/A007/旧slug",
        }
        preserved = {p["branch"]: p for p in report["preserved_branches"]}
        assert preserved["hw/A005/t-c10sed00"]["reason"] == (
            "task_closed_unmerged")
        assert preserved["hw/A008/未完成legacy"]["reason"] == "legacy_unmerged"
        assert all(len(p["head"]) == 7 for p in preserved.values())
        # 任务仍活跃的分支既没删也没进 preserved
        assert "hw/A006/t-0pen0000" in _branches(git_repo)
        assert "hw/A006/t-0pen0000" not in preserved
        assert report["errors"] == []
