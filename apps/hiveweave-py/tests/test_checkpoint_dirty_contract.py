"""checkpoint 与 dirty 门禁一致性契约测试（T1.3 / P0-1 复发防线）。

两个口径必须被这份测试钉在一起：
- ``worktree_dirty_counts``（T1.1：生成物 untracked 不计 dirty，源码 untracked 必须计）
- ``GitWorktreeService.checkpoint``（T1.2：剥离有说明、无 committable 变更
  是成功 no-op、真失败带 git 输出）

任一侧单独再改，这五条用例至少爆一条 —— 不会再漂。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hiveweave.services.git_worktree import GitWorktreeService
from hiveweave.services.git_worktree import service_create as sc_module
from hiveweave.services.worktree_review import worktree_dirty_counts


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (r.stdout or "").strip()


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "t@t.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "init")
    _git(root, "branch", "-M", "main")


def _make_worktree(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """main 仓库 + 真实 linked worktree（共享历史，commits_ahead 可判）。"""
    main = tmp_path / name
    main.mkdir()
    _init_repo(main)
    wt = tmp_path / f"{name}-wt"
    _git(main, "worktree", "add", "-b", f"hw/x/{name}", str(wt))
    return main, wt


def _service_with_worktree(wt: Path) -> GitWorktreeService:
    svc = GitWorktreeService()

    async def _fake_resolve(workspace_path: str, short_id: str) -> str:
        return str(wt)

    svc._resolve_effective_worktree_path = _fake_resolve  # type: ignore[method-assign]
    return svc


# ── 用例 1：仅 untracked lockfile → checkpoint 成功且不报脏 ──────────────


async def test_only_untracked_lockfile_is_clean_and_checkpoint_succeeds(
    tmp_path: Path,
):
    main, wt = _make_worktree(tmp_path, "lock-only")
    (wt / "package-lock.json").write_text('{"lock": 1}\n', encoding="utf-8")

    dirty = await worktree_dirty_counts(str(wt))
    assert dirty["dirty_count"] == 0
    assert dirty["generated_untracked"] == 1
    assert dirty["generated_paths"] == ["package-lock.json"]

    svc = _service_with_worktree(wt)
    result = await svc.checkpoint(str(main), "t1", "save")
    assert result["success"] is True
    assert result["count"] == 0
    msg = result.get("message") or ""
    assert "package-lock.json" in msg
    assert "no committable changes" in msg
    assert "Failed" not in msg


# ── 用例 2：剥离清单进返回值（含 REGENERABLE_PATTERNS 项） ───────────────


async def test_stripped_list_in_return_message_including_regenerable(
    tmp_path: Path,
):
    main, wt = _make_worktree(tmp_path, "strip-note")
    (wt / "pnpm-lock.yaml").write_text("lock\n", encoding="utf-8")
    (wt / "tsconfig.tsbuildinfo").write_text("{}\n", encoding="utf-8")
    (wt / "test_output_run1.json").write_text("{}\n", encoding="utf-8")

    dirty = await worktree_dirty_counts(str(wt))
    assert dirty["dirty_count"] == 0
    assert dirty["generated_untracked"] == 3

    svc = _service_with_worktree(wt)
    result = await svc.checkpoint(str(main), "t2", "save")
    assert result["success"] is True
    msg = result.get("message") or ""
    # lockfile 走 generated_note（剥离）
    assert "pnpm-lock.yaml" in msg
    assert "stripped by policy" in msg
    # REGENERABLE_PATTERNS 两项走 regen_note（de-tracked）
    assert "tsconfig.tsbuildinfo" in msg
    assert "test_output_run1.json" in msg
    assert "de-tracked by design" in msg


# ── 用例 3：commit 真失败时带 git 输出 ───────────────────────────────────


async def test_real_commit_failure_carries_git_output(tmp_path: Path, monkeypatch):
    main, wt = _make_worktree(tmp_path, "commit-fail")
    (wt / "src").mkdir()
    (wt / "src" / "a.ts").write_text("export {};\n", encoding="utf-8")

    real_git = sc_module._git

    async def failing_commit(args, cwd, timeout=30.0):
        if args[:1] == ["commit"]:
            return False, "error: pre-commit hook declined (simulated stderr)"
        return await real_git(args, cwd, timeout)

    monkeypatch.setattr(sc_module, "_git", failing_commit)

    svc = _service_with_worktree(wt)
    result = await svc.checkpoint(str(main), "t3", "save")
    assert result["success"] is False
    msg = result.get("message") or ""
    assert "Failed to create checkpoint commit" in msg
    # T1.2 核心断言：git commit 的输出不再被吞
    assert "pre-commit hook declined" in msg


async def test_commit_failure_appends_stripped_list(tmp_path: Path, monkeypatch):
    main, wt = _make_worktree(tmp_path, "commit-fail-strip")
    (wt / "src").mkdir()
    (wt / "src" / "b.ts").write_text("x\n", encoding="utf-8")
    (wt / "package-lock.json").write_text("{}\n", encoding="utf-8")

    real_git = sc_module._git

    async def failing_commit(args, cwd, timeout=30.0):
        if args[:1] == ["commit"]:
            return False, "error: cannot commit"
        return await real_git(args, cwd, timeout)

    monkeypatch.setattr(sc_module, "_git", failing_commit)

    svc = _service_with_worktree(wt)
    result = await svc.checkpoint(str(main), "t3b", "save")
    assert result["success"] is False
    msg = result.get("message") or ""
    assert "stripped-by-policy: package-lock.json" in msg


# ── 用例 4（回归防线）：untracked 非生成物源码 → dirty_count=1 ───────────


async def test_untracked_source_file_still_counts_dirty(tmp_path: Path):
    """第一版 T1.1 口径（排除全部 ??）会在这里失败 —— 防住 worktree 被删。"""
    main, wt = _make_worktree(tmp_path, "source-dirty")
    (wt / "src").mkdir()
    (wt / "src" / "new.ts").write_text("export 1;\n", encoding="utf-8")

    dirty = await worktree_dirty_counts(str(wt))
    assert dirty["dirty_count"] == 1
    assert dirty["untracked_count"] == 1
    assert dirty["generated_untracked"] == 0


async def test_mixed_source_and_lockfile_counts_only_source(tmp_path: Path):
    main, wt = _make_worktree(tmp_path, "mixed")
    (wt / "src").mkdir()
    (wt / "src" / "app.ts").write_text("x\n", encoding="utf-8")
    (wt / "package-lock.json").write_text("{}\n", encoding="utf-8")

    dirty = await worktree_dirty_counts(str(wt))
    assert dirty["dirty_count"] == 1  # 只有源码
    assert dirty["generated_untracked"] == 1


# ── 审计 P0-1 回归防线：tracked 生成物修改不再死锁 ────────────────────────


async def test_tracked_lockfile_modification_is_clean_and_noop(tmp_path: Path):
    """tracked package-lock.json 被修改（npm install 标准场景）：
    dirty=0（checkpoint 必剥离、不可提交，计数纯属噪音）+ checkpoint
    成功 no-op —— 不再出现「checkpoint 说剥离、dirty 门禁又计数」的
    无限循环（TEST_DSH_35 实测 11.4 min 死锁的 tracked 变体）。"""
    main, wt = _make_worktree(tmp_path, "tracked-lock")
    (wt / "package-lock.json").write_text('{"base": 1}\n', encoding="utf-8")
    _git(wt, "add", "package-lock.json")
    _git(wt, "commit", "-m", "add lockfile")

    # npm install 改写 tracked lockfile
    (wt / "package-lock.json").write_text('{"base": 2, "x": 1}\n', encoding="utf-8")

    dirty = await worktree_dirty_counts(str(wt))
    assert dirty["dirty_count"] == 0
    assert dirty["generated_untracked"] == 1

    svc = _service_with_worktree(wt)
    result = await svc.checkpoint(str(main), "t6", "save")
    assert result["success"] is True
    assert result["count"] == 0
    msg = result.get("message") or ""
    assert "package-lock.json" in msg
    assert "no committable changes" in msg
    assert "Failed" not in msg


async def test_tracked_lockfile_plus_source_counts_only_source(tmp_path: Path):
    """tracked lockfile 修改 + 源码修改 → 只有源码计 dirty，且 checkpoint
    只提交源码（lockfile 被剥离）。"""
    main, wt = _make_worktree(tmp_path, "tracked-mixed")
    (wt / "package-lock.json").write_text('{"base": 1}\n', encoding="utf-8")
    (wt / "src").mkdir()
    (wt / "src" / "a.ts").write_text("export 0;\n", encoding="utf-8")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "init slice")

    (wt / "package-lock.json").write_text('{"base": 2}\n', encoding="utf-8")
    (wt / "src" / "a.ts").write_text("export 1;\n", encoding="utf-8")

    dirty = await worktree_dirty_counts(str(wt))
    assert dirty["dirty_count"] == 1
    assert dirty["generated_untracked"] == 1

    svc = _service_with_worktree(wt)
    result = await svc.checkpoint(str(main), "t7", "save")
    assert result["success"] is True
    committed = _git(wt, "diff", "--name-only", "HEAD~1", "HEAD")
    assert "src/a.ts" in committed
    assert "package-lock.json" not in committed


# ── 用例 5：dirty 与 checkpoint 对同一状态判定一致 ────────────────────────


async def test_dirty_and_checkpoint_agree_across_states(tmp_path: Path):
    main, wt = _make_worktree(tmp_path, "agree")

    # 状态 A：只有 untracked lockfile → 不脏 + checkpoint 成功 no-op
    (wt / "package-lock.json").write_text("{}\n", encoding="utf-8")
    dirty_a = await worktree_dirty_counts(str(wt))
    svc = _service_with_worktree(wt)
    res_a = await svc.checkpoint(str(main), "t5", "a")
    assert dirty_a["dirty_count"] == 0
    assert res_a["success"] is True

    # 状态 B：源码 + lockfile → 脏 1 + checkpoint 真提交（lockfile 被剥离）
    (wt / "src").mkdir()
    (wt / "src" / "app.ts").write_text("export 1;\n", encoding="utf-8")
    dirty_b = await worktree_dirty_counts(str(wt))
    res_b = await svc.checkpoint(str(main), "t5", "b")
    assert dirty_b["dirty_count"] == 1
    assert res_b["success"] is True
    assert res_b["count"] == 1
    committed = _git(
        wt, "diff", "--name-only", "HEAD~1", "HEAD"
    ) if True else ""
    assert "src/app.ts" in committed
    assert "package-lock.json" not in committed

    # 状态 C：提交后干净 → 不脏 + checkpoint 成功 no-op
    # （lockfile 从未提交、仍以 untracked 留在 worktree → 走剥离说明路径）
    dirty_c = await worktree_dirty_counts(str(wt))
    res_c = await svc.checkpoint(str(main), "t5", "c")
    assert dirty_c["dirty_count"] == 0
    assert res_c["success"] is True
    c_msg = res_c.get("message") or ""
    assert (
        "no changes to commit" in c_msg or "no committable changes" in c_msg
    )


# ── P1-2 回归防线：平台运行时目录剥离 + add 失败透传 ────────────────────


async def test_platform_runtime_dirs_stripped_from_checkpoint(tmp_path: Path):
    """`.hiveweave/` 下非共享目录（npm-cache/sandbox-temp 等）不进提交；
    共享四目录保留；回执 message 带剥离说明（platform-issue-report P1-2：
    untracked 平台目录曾让 git add 失败 → 19 分钟自救马拉松）。"""
    main, wt = _make_worktree(tmp_path, "runtime-strip")
    (wt / ".hiveweave").mkdir()
    (wt / ".hiveweave" / "npm-cache").mkdir(parents=True)
    (wt / ".hiveweave" / "npm-cache" / "dep.bin").write_bytes(b"x" * 64)
    (wt / ".hiveweave" / "shared").mkdir(parents=True)
    (wt / ".hiveweave" / "shared" / "contract.md").write_text(
        "# shared\n", encoding="utf-8"
    )
    (wt / "src").mkdir()
    (wt / "src" / "app.ts").write_text("export 1;\n", encoding="utf-8")

    svc = _service_with_worktree(wt)
    result = await svc.checkpoint(str(main), "t8", "runtime strip")
    assert result["success"] is True
    assert result["count"] == 1
    msg = result.get("message") or ""
    assert "platform runtime path(s) under .hiveweave/" in msg
    assert "npm-cache" in msg
    committed = _git(wt, "diff", "--name-only", "HEAD~1", "HEAD")
    assert "src/app.ts" in committed
    assert ".hiveweave/shared/contract.md" in committed
    assert "npm-cache" not in committed


async def test_checkpoint_add_failure_carries_git_output(tmp_path: Path, monkeypatch):
    """`git add -A` 失败必须透传 git 原始输出，而不是只回八字盲盒
    （此前 stderr 被 `_` 丢弃 —— platform-issue-report P1-2 根因）。"""
    main, wt = _make_worktree(tmp_path, "add-fail")
    (wt / "src").mkdir()
    (wt / "src" / "a.ts").write_text("x\n", encoding="utf-8")

    real_git = sc_module._git

    async def failing_add(args, cwd, timeout=30.0):
        if args[:2] == ["add", "-A"]:
            return False, "fatal: index.lock already held (simulated)"
        return await real_git(args, cwd, timeout)

    monkeypatch.setattr(sc_module, "_git", failing_add)

    svc = _service_with_worktree(wt)
    result = await svc.checkpoint(str(main), "t9", "fail")
    assert result["success"] is False
    msg = result.get("message") or ""
    assert "Failed to stage files" in msg
    assert "simulated" in msg
