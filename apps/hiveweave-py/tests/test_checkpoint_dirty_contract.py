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
import structlog.testing

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

    async def failing_commit(args, cwd, timeout=30.0, project_root=None):
        # ⚠ 不能用 args[0] 判子命令：提交现在前置了 `-c user.name=…` 身份参数
        #   （见 git_identity：per-agent 身份改命令行注入）⇒ 判「是不是 commit」要扫全 argv
        if "commit" in args:
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

    async def failing_commit(args, cwd, timeout=30.0, project_root=None):
        # ⚠ 不能用 args[0] 判子命令：提交现在前置了 `-c user.name=…` 身份参数
        #   （见 git_identity：per-agent 身份改命令行注入）⇒ 判「是不是 commit」要扫全 argv
        if "commit" in args:
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

    async def failing_add(args, cwd, timeout=30.0, project_root=None):
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


# ── 用例 6-8：体积留痕（09-16 第 0 步 0-1）────────────────────────────
#
# 背景（实测）：`git_worktree.checkpoint` 原有的 `count` 是
# `_count_checkpoints()` = **近 7 天 checkpoint 提交数**，与变更量无关 ——
# TEST_DSH_59 的 A088 那次 `count=5` 却删掉了 12 个 tracked 文件（A087 `count=18` 删 17），
# 即**日志无法区分"正常提交"与"清空全树"**。这三条用例钉住新的体积字段。


async def test_checkpoint_reports_volume_and_alarms_on_tree_wipe(tmp_path: Path):
    """★ 事故形态：把 worktree 里的 tracked 文件全删掉后存档。

    这就是 #21 的提交形态（husk 目录 ⇒ `git add -A` 把整棵树记成删除）。
    验收（状态判据）：**删除量、树体积前后、alarm 位**都必须可读，
    且**常态日志那行**也要带上删除量（否则事后无从定位）。
    """
    main, wt = _make_worktree(tmp_path, "volume-wipe")
    (wt / "src").mkdir()
    (wt / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (wt / "src" / "b.ts").write_text("export const b = 2;\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "checkpoint: seed two files")

    tracked = [ln for ln in _git(wt, "ls-files").splitlines() if ln.strip()]
    assert len(tracked) == 3, tracked          # README.md + src/a.ts + src/b.ts
    for rel in tracked:
        (wt / rel).unlink()

    svc = _service_with_worktree(wt)
    with structlog.testing.capture_logs() as logs:
        result = await svc.checkpoint(str(main), "t20", "wipe")

    assert result["success"] is True
    assert result["staged_deleted"] == 3, result
    assert result["tree_files_before"] == 3, result
    assert result["tree_files_after"] == 0, result
    assert result["volume_alarm"] is True, result
    assert "removed 3 of 3" in (result.get("message") or "")

    # 常态日志必须带体积字段（验收：日志能读出删除量 N）
    ck = [e for e in logs if e.get("event") == "git_worktree.checkpoint"]
    assert ck, [e.get("event") for e in logs]
    assert ck[0]["staged_deleted"] == 3
    assert ck[0]["tree_files_after"] == 0
    assert ck[0]["volume_alarm"] is True
    assert "count" in ck[0]                        # 旧字段保留（不破坏既有消费者）

    # 独立可 grep 的 alarm 事件（"事后能看见"的抓手）
    alarm = [e for e in logs if e.get("event") == "git_worktree.checkpoint_volume_alarm"]
    assert len(alarm) == 1, [e.get("event") for e in logs]
    assert alarm[0]["staged_deleted"] == 3
    assert alarm[0]["tree_files_after"] == 0


async def test_checkpoint_normal_commit_has_no_deletions_and_no_alarm(
    tmp_path: Path,
):
    """反向对照：正常新增一个文件 ⇒ 删除量 0、不触发 alarm、回执无 WARNING。

    **没有这条，上面那条可能是"永远为真"的假守卫。**
    """
    main, wt = _make_worktree(tmp_path, "volume-normal")
    (wt / "src").mkdir()
    (wt / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")

    svc = _service_with_worktree(wt)
    with structlog.testing.capture_logs() as logs:
        result = await svc.checkpoint(str(main), "t21", "add one file")

    assert result["success"] is True
    assert result["staged_added"] == 1, result
    assert result["staged_deleted"] == 0, result
    assert result["tree_files_before"] == 1, result       # README.md
    assert result["tree_files_after"] == 2, result        # + src/a.ts
    assert result["volume_alarm"] is False, result
    assert "WARNING" not in (result.get("message") or "")
    assert not [
        e for e in logs
        if e.get("event") == "git_worktree.checkpoint_volume_alarm"
    ]


async def test_checkpoint_alarms_on_bulk_delete_without_emptying_tree(
    tmp_path: Path,
):
    """★ 第二条 alarm 分支：**删掉一半以上、但树没空**。

    为什么必须有这条（独立审计 P1）：alarm 有两个条件 ——
    ① 结果树为空；② 删除量 ≥ 父树 50%。**实测把 ② 整条删掉，14 条用例全绿**
    （因为其余用例只覆盖 ①），而 **② 正是真实事故 A088 的形态**：
    它删了 12 个文件、父树 17 个（树没空），只由 ② 触发。
    """
    main, wt = _make_worktree(tmp_path, "volume-bulk")
    (wt / "src").mkdir()
    for name in ("a", "b", "c", "d"):
        (wt / "src" / f"{name}.ts").write_text(f"export const {name} = 1;\n",
                                               encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "checkpoint: seed four files")

    tracked = [ln for ln in _git(wt, "ls-files").splitlines() if ln.strip()]
    assert len(tracked) == 5, tracked            # README.md + 4 个
    # 删 3 留 2 ⇒ share = 3/5 = 0.6 ≥ 0.5，而 tree_after = 2 ≠ 0（只由第二条触发）
    for rel in [t for t in tracked if not t.endswith(("README.md", "a.ts"))]:
        (wt / rel).unlink()

    svc = _service_with_worktree(wt)
    with structlog.testing.capture_logs() as logs:
        result = await svc.checkpoint(str(main), "t23", "bulk delete")

    assert result["success"] is True
    assert result["staged_deleted"] == 3, result
    assert result["tree_files_before"] == 5, result
    assert result["tree_files_after"] == 2, result      # ← 树非空
    assert result["deleted_share"] >= 0.5, result
    assert result["volume_alarm"] is True, result
    assert "removed 3 of 5" in (result.get("message") or "")
    alarm = [e for e in logs
             if e.get("event") == "git_worktree.checkpoint_volume_alarm"]
    assert len(alarm) == 1, [e.get("event") for e in logs]


async def test_all_checkpoint_paths_expose_identical_key_sets(
    tmp_path: Path, monkeypatch
):
    """**跨路径键集合一致性**：成功 / 两条 no-op / 三条失败 路径必须**同一批键**。

    为什么这么写（独立审计 P1）：原先③用**硬编码键列表**断言，等于把实现抄进测试
    —— 只改成功路径时它仍然绿（审计实测：把 commit 失败路径的字段去掉，14 条全绿）。
    这里改成**路径之间互相比对**，不再有第二份清单 ⇒ 任一路径单边增/删键都会转红。
    调用方读新字段才可能不 KeyError，而"有的返回有、有的没有"正是本仓最忌的静默失效。
    """
    # 成功路径（作为参照）
    main, wt = _make_worktree(tmp_path, "keyset-ok")
    (wt / "src").mkdir()
    (wt / "src" / "a.ts").write_text("x\n", encoding="utf-8")
    success = await _service_with_worktree(wt).checkpoint(str(main), "t30", "add")

    # no-op 路径（仓库干净）
    main2, wt2 = _make_worktree(tmp_path, "keyset-noop")
    noop = await _service_with_worktree(wt2).checkpoint(str(main2), "t31", "nothing")

    # 失败路径 A：worktree 目录不存在
    missing = await _service_with_worktree(tmp_path / "no-such-wt").checkpoint(
        str(main2), "t32", "missing"
    )

    # 失败路径 B：`git add -A` 失败
    real_git = sc_module._git

    async def failing_add(args, cwd, timeout=30.0, project_root=None):
        if args[:2] == ["add", "-A"]:
            return False, "fatal: index.lock already held (simulated)"
        return await real_git(args, cwd, timeout)

    monkeypatch.setattr(sc_module, "_git", failing_add)
    add_failed = await _service_with_worktree(wt).checkpoint(str(main), "t33", "addfail")

    # 失败路径 C：commit 失败
    # ⚠ 必须先制造一处**新变更**：上一步 `success` 已把 `wt` 提交干净，若不复用同一
    #   worktree 而不改文件，`checkpoint` 会从 "no changes to commit" 早退 ——
    #   **根本走不到 commit**，这条守卫就变成"不可达的假守卫"（实测踩过一次：
    #   去掉 commit 失败路径的体积字段，本条仍绿）。
    (wt / "src" / "b.ts").write_text("y\n", encoding="utf-8")

    async def failing_commit(args, cwd, timeout=30.0, project_root=None):
        if "commit" in args:
            return False, "error: pre-commit hook declined (simulated stderr)"
        return await real_git(args, cwd, timeout)

    monkeypatch.setattr(sc_module, "_git", failing_commit)
    commit_failed = await _service_with_worktree(wt).checkpoint(
        str(main), "t34", "commitfail"
    )
    # 钉住"这条路径真的被走到了"——否则下面的键集合比对是在比一条假路径
    assert commit_failed.get("success") is False, commit_failed
    assert "Failed to create checkpoint commit" in (
        commit_failed.get("message") or ""
    ), commit_failed

    # 参照集**从成功路径派生**（不留第二份清单 —— 否则单边加键不会被发现）。
    # ⚠ 只比"体积键"这一子集：失败路径本来就没有 `hash`/`count`（**既有设计**，
    #   它们返回 `{success: False, message}`），要求全键相同会去改既有语义。
    pre_existing = {"success", "hash", "count", "message"}
    ref_volume_keys = set(success) - pre_existing
    assert ref_volume_keys, "参照集为空 ⇒ 这条守卫什么也没守"
    assert "volume_alarm" in ref_volume_keys and "staged_deleted" in ref_volume_keys

    for label, res in (
        ("no-op", noop),
        ("worktree-missing", missing),
        ("add-failed", add_failed),
        ("commit-failed", commit_failed),
    ):
        missing_keys = ref_volume_keys - set(res)
        assert not missing_keys, (
            f"{label} 路径缺体积键 {sorted(missing_keys)} —— "
            f"调用方读它们会 KeyError"
        )
        extra_keys = set(res) - pre_existing - ref_volume_keys
        assert not extra_keys, (
            f"{label} 路径多出未在成功路径出现的键 {sorted(extra_keys)} —— "
            f"两边键清单已经不一致"
        )

    # 早退路径的体积字段必须是"零"，不能是上一棵树的值
    assert noop["staged_deleted"] == 0 and noop["volume_alarm"] is False
    assert add_failed["volume_alarm"] is False

# ── 用例：`.vite/` 依赖预构建缓存（P0-2 · TEST_DSH_65 死锁复发防线）────────
#
# 背景（2026-09-20 平台问题研究 P0-2）：`.vite/deps/_metadata.json` 与
# `.vite/deps/package.json` 被 checkpoint 的 `git add -A` 提交进分支 ⇒ MAIN
# 永久脏 ⇒ merge 门禁把它们判进 `hard_blockers` 硬拒 6 次，而门禁自己开出的
# `checkout HEAD --` 又撞 `.git` 封条 ⇒ 三机制相乘死锁、项目未交付。跨项目
# 实测 **14 个**项目的 MAIN 跟踪着同一对文件，其中 12 个尚未撞门禁（哑弹）。
#
# 阳性对照做法：注释掉 `constants.REGENERABLE_PATTERNS` 里 `.vite/` 那一条后
# 重跑本文件 —— 下面三条断言**必须转红**（硬 blockers 非空 / 边界判据反向 /
# 种子探测为空）。转不红即为假绿。

async def test_vite_tracked_dirt_is_not_a_hard_blocker(tmp_path: Path):
    """已被跟踪的 `.vite/` 缓存脏 ⇒ 不得进 hard_blockers（本项目拒合 6 次）。"""
    from hiveweave.services.git_worktree.merge_support import classify_main_dirt

    main = tmp_path / "vite-main"
    main.mkdir()
    _init_repo(main)

    vite = main / ".vite" / "deps"
    vite.mkdir(parents=True)
    (vite / "_metadata.json").write_text('{"a": 1}\n', encoding="utf-8")
    (vite / "package.json").write_text('{"b": 2}\n', encoding="utf-8")
    _git(main, "add", "-f", ".vite/deps/_metadata.json", ".vite/deps/package.json")
    _git(main, "commit", "-m", "vite cache (platform bug: git add -A)")

    # 复现「MAIN 永久脏」：缓存被 dev server 改写
    (vite / "_metadata.json").write_text('{"a": 99}\n', encoding="utf-8")
    (vite / "package.json").write_text('{"b": 99}\n', encoding="utf-8")

    verdict = await classify_main_dirt(str(main))
    assert ".vite/deps/_metadata.json" in verdict["dirty_paths"]
    assert ".vite/deps/package.json" in verdict["dirty_paths"]
    assert verdict["hard_blockers"] == [], verdict["hard_blockers"]
    assert verdict["user_suspect"] == [], verdict["user_suspect"]


def test_vite_regenerable_boundaries_do_not_overreach():
    """正样本命中、负样本不误伤（`.vitebuild/` 曾是同族误伤面）。"""
    from hiveweave.services.git_worktree.constants import is_generated_path

    # 报告里实测被提交进分支、且 14 个项目都在跟踪的那一对
    assert is_generated_path(".vite/deps/_metadata.json") is True
    # 目录级口径（与 `.godot/` 一致）：只判 `.vite/deps/` 会漏掉 temp/ 等子目录
    assert is_generated_path("web/.vite/temp/hash.js") is True
    assert is_generated_path(".vite/temp/hash.js") is True
    assert is_generated_path(".vite/deps/package.json") is True
    assert is_generated_path("mini-town/.vite/deps/_metadata.json") is True

    # 负样本：`.vitebuild/` 是另一个目录名；配置与源码更不是产物
    assert is_generated_path(".vitebuild/x.js") is False
    assert is_generated_path("vite.config.ts") is False
    assert is_generated_path("src/main.ts") is False


def test_vite_gitignore_seed_detected_from_vite_config(tmp_path: Path):
    """有 vite 标记文件才补 `.vite/` 种子；无标记不得凭空补（同 `.godot/`）。"""
    from hiveweave.services.git_worktree.constants import (
        detect_engine_gitignore_entries,
    )

    proj = tmp_path / "vite-proj"
    proj.mkdir()
    (proj / "vite.config.ts").write_text("export default {}\n", encoding="utf-8")
    assert ".vite/" in detect_engine_gitignore_entries(str(proj))

    plain = tmp_path / "plain-proj"
    plain.mkdir()
    (plain / "package.json").write_text("{}\n", encoding="utf-8")
    assert detect_engine_gitignore_entries(str(plain)) == ()

async def test_lockfile_tracked_dirt_is_not_a_hard_blocker(tmp_path: Path):
    """`GENERATED_FILES`（lockfile）脏也不得硬拒 —— 门禁此前**只读**
    ``REGENERABLE_PATTERNS``、不读 ``GENERATED_FILES``，于是 TEST_DSH_35 被
    逐字判成 `Dirty: package-lock.json` 硬拒 2 次。两处判定现已收口到
    ``constants.is_generated_path``（单一判定源），本条即该收口的守卫。

    阳性对照：把 ``is_generated_path`` 里的 ``GENERATED_FILES`` 分支去掉
    ⇒ 本条必须转红（hard_blockers 非空）。
    """
    from hiveweave.services.git_worktree.constants import is_generated_path
    from hiveweave.services.git_worktree.merge_support import classify_main_dirt

    # 先钉住判定源本身：lockfile 与 .vite/ 在**同一个函数**里被认成生成物
    assert is_generated_path("package-lock.json") is True
    assert is_generated_path(".vite/deps/_metadata.json") is True
    assert is_generated_path("src/main.ts") is False

    main = tmp_path / "lock-main"
    main.mkdir()
    _init_repo(main)
    (main / "package-lock.json").write_text('{"v": 1}\n', encoding="utf-8")
    _git(main, "add", "package-lock.json")
    _git(main, "commit", "-m", "lock")
    (main / "package-lock.json").write_text('{"v": 2}\n', encoding="utf-8")

    verdict = await classify_main_dirt(str(main))
    assert "package-lock.json" in verdict["dirty_paths"]
    assert verdict["hard_blockers"] == [], verdict["hard_blockers"]

async def test_checkpoint_never_commits_vite_cache(tmp_path: Path):
    """引入点守卫：`git add -A` 不得把 `.vite/` 提交进分支（本次事故源头）。

    阳性对照：删掉 ``REGENERABLE_PATTERNS`` 里的 `.vite/` ⇒ 本条必须转红
    （审计实测：删正则后提交里出现 `.vite/deps/_metadata.json` + `package.json`）。
    """
    main, wt = _make_worktree(tmp_path, "vite-ck")
    v = wt / ".vite" / "deps"
    v.mkdir(parents=True)
    (v / "_metadata.json").write_text('{"a": 1}\n', encoding="utf-8")
    (v / "package.json").write_text('{"b": 2}\n', encoding="utf-8")
    (wt / "src").mkdir()
    (wt / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")

    result = await _service_with_worktree(wt).checkpoint(str(main), "tv", "vite")
    assert result["success"] is True, result

    committed = _git(wt, "diff", "--name-only", "HEAD~1", "HEAD")
    assert "src/a.ts" in committed, committed   # 防空提交导致的恒真
    assert ".vite/" not in committed, committed  # 事故形态
    assert ".vite/deps/_metadata.json" in (result.get("message") or "")


async def test_merge_gate_restores_vite_dirt_instead_of_rejecting(tmp_path: Path):
    """merge 真实入口：`restore_regenerable_dirt_or_reject` 应恢复而非拒合。
    阳性对照：删掉 `.vite/` 正则 ⇒ 返回 reason='main_dirty' 拒合（事故原话）。
    """
    from hiveweave.services.git_worktree.merge_support import (
        restore_regenerable_dirt_or_reject,
    )

    main = tmp_path / "vite-restore"
    main.mkdir()
    _init_repo(main)
    v = main / ".vite" / "deps"
    v.mkdir(parents=True)
    (v / "_metadata.json").write_text('{"a": 1}\n', encoding="utf-8")
    _git(main, "add", "-f", ".vite/deps/_metadata.json")
    _git(main, "commit", "-m", "vite cache")
    (v / "_metadata.json").write_text('{"a": 99}\n', encoding="utf-8")

    verdict = await restore_regenerable_dirt_or_reject(str(main), branch="main")
    assert verdict is None, verdict
    assert _git(main, "status", "--porcelain") == ""
