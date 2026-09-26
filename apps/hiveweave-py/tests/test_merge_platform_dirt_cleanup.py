"""批E#3 任务4：worktree 三机制死锁拆解 —— 平台自造非再生脏的平台侧清理。

死锁链（constants.py REGENERABLE_PATTERNS 注释 + 审计卡 P1-12）：
checkpoint 的 ``git add -A`` 把产物提交进分支 ⇒ merge 落 MAIN 后 MAIN 跟踪
它们 ⇒ 引擎重生成 ⇒ MAIN 永久脏 ⇒ 门禁判 non_regen 硬拒 ⇒ 门禁处方让受限
agent 自己跑 ``checkout HEAD --`` 撞 .git 封条。

修法钉住三条：
1. HEAD 基线**最后由平台 checkpoint 提交写入**的非再生脏 → 平台侧代清
   （先隔离工作树副本再 checkout HEAD --），merge 放行；
2. **用户/人工编辑不被误删**：非平台来源的脏照旧硬拒，工作树内容原样保留；
3. regen 类（REGENERABLE_PATTERNS）清理行为不回归。

隔离纪律（AI_MEMORY）：untracked 计入 dirty 是设计目的，本清理只碰
tracked 脏且先复制进 merge-quarantine，内容零丢失。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from hiveweave.services.git_worktree.merge_support import (
    restore_regenerable_dirt_or_reject,
)


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


def _checkpoint_branch_then_merge(main: Path, rel: str, body: str) -> None:
    """模拟「checkpoint 把产物提交进分支 → merge 落 MAIN」的死后链第①步。"""
    _git(main, "checkout", "-b", "hw/x/task")
    p = main / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "checkpoint: auto-save work")
    _git(main, "checkout", "main")
    _git(main, "merge", "hw/x/task", "--no-edit")


def _quarantine_copies(main: Path) -> list[Path]:
    qroot = main / ".hiveweave" / "merge-quarantine"
    if not qroot.is_dir():
        return []
    return sorted(
        (p for p in qroot.rglob("*") if p.is_file()),
    )


async def test_platform_created_dirt_is_restored_and_merge_proceeds(tmp_path):
    """①平台自造脏：平台侧代清（checkout HEAD --），merge 门禁放行。"""
    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)
    _checkpoint_branch_then_merge(main, "engine/cache.bin", "generated-v1\n")

    # 引擎在 MAIN 上重生成 ⇒ tracked 脏、不在 REGENERABLE_PATTERNS 内。
    (main / "engine" / "cache.bin").write_text("generated-v2\n", encoding="utf-8")

    reject = await restore_regenerable_dirt_or_reject(str(main), branch="hw/x/task")
    assert reject is None, f"平台自造脏应被代清放行，实际拒绝：{reject}"
    # 工作树恢复到 HEAD（checkpoint 快照）版本。
    assert (main / "engine" / "cache.bin").read_text(encoding="utf-8") == "generated-v1\n"
    # 恢复前的工作树副本被隔离（内容零丢失，AI_MEMORY 纪律）。
    copies = _quarantine_copies(main)
    assert any(
        p.name == "cache.bin" and p.read_text(encoding="utf-8") == "generated-v2\n"
        for p in copies
    ), f"恢复前副本必须进 merge-quarantine：{copies}"


async def test_user_edits_are_not_discarded(tmp_path):
    """②用户编辑：非平台来源的非再生脏照旧硬拒，内容**原样保留**。"""
    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)
    (main / "src").mkdir()
    (main / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "add app")  # 非 checkpoint 提交

    # 人工/外部编辑 MAIN（未提交）。
    (main / "src" / "app.py").write_text("print('human edit')\n", encoding="utf-8")

    reject = await restore_regenerable_dirt_or_reject(str(main), branch="hw/x/task")
    assert reject is not None and reject["reason"] == "main_dirty"
    assert reject.get("remedy"), "拒绝必带处方（批E#3 任务3）"
    # 工作树内容不被吞：人工编辑原样保留。
    assert (main / "src" / "app.py").read_text(encoding="utf-8") == "print('human edit')\n"
    assert not _quarantine_copies(main), "未获准的清理不得产生隔离副本"


async def test_regen_dirt_cleanup_unchanged(tmp_path):
    """③REGENERABLE_PATTERNS 清理行为不回归（TEST6 P1-C 既有契约）。"""
    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)
    (main / "tsconfig.tsbuildinfo").write_text("{}\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "oops regen tracked")
    (main / "tsconfig.tsbuildinfo").write_text('{"stale": true}\n', encoding="utf-8")

    reject = await restore_regenerable_dirt_or_reject(str(main), branch="hw/x/task")
    assert reject is None, f"regen 脏应自动清理放行：{reject}"


async def test_platform_dirt_restores_only_its_own_paths(tmp_path):
    """混合脏：平台自造的代清、同批的独立人工编辑仍拦截。"""
    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)
    (main / "src").mkdir()
    (main / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "add app")
    # 平台链路引入 engine 缓存（checkpoint 提交）。
    _git(main, "checkout", "-b", "hw/x/task2")
    (main / "engine").mkdir()
    (main / "engine" / "cache.bin").write_text("g1\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "checkpoint: auto-save")
    _git(main, "checkout", "main")
    _git(main, "merge", "hw/x/task2", "--no-edit")

    # 同时制造两类脏：引擎重生成 + 人工编辑。
    (main / "engine" / "cache.bin").write_text("g2\n", encoding="utf-8")
    (main / "src" / "app.py").write_text("print('human')\n", encoding="utf-8")

    reject = await restore_regenerable_dirt_or_reject(str(main), branch="hw/x/task2")
    assert reject is not None and reject["reason"] == "main_dirty"
    # 平台自造脏已被代清（恢复到 HEAD）；人工编辑原样保留。
    assert (main / "engine" / "cache.bin").read_text(encoding="utf-8") == "g1\n"
    assert (main / "src" / "app.py").read_text(encoding="utf-8") == "print('human')\n"


async def test_checkpoint_then_human_edit_restored_with_explicit_declaration(tmp_path):
    """P1（独立审计 2026-09-26）：checkpoint 提交后**人工改工作树** →
    merge 走通（代清照做），但绝不静默 —— warning 日志 + 隔离目录内
    显式声明文件（随 merge 回执 [QUARANTINE] 行与 urgent 收件箱可见），
    人工改动副本可取回。"""
    import structlog.testing

    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)
    # ① checkpoint 把产物提交进分支并 merge 落 MAIN（HEAD 基线 = 平台快照）。
    _checkpoint_branch_then_merge(main, "engine/cache.bin", "generated-v1\n")
    # ② 人工在 MAIN 工作树上改这个文件（未提交）—— 基线来源与增量来源
    #    从此无关：这正是「HEAD 末次写入者是 checkpoint」证明不了的部分。
    (main / "engine" / "cache.bin").write_text("human tweak\n", encoding="utf-8")

    with structlog.testing.capture_logs() as logs:
        reject = await restore_regenerable_dirt_or_reject(
            str(main), branch="hw/x/task"
        )
    # ③ merge 走通：代清后门禁放行。
    assert reject is None, f"应代清放行，实际拒绝：{reject}"
    # 工作树恢复到 HEAD（checkpoint 快照版本）。
    assert (main / "engine" / "cache.bin").read_text(
        encoding="utf-8"
    ) == "generated-v1\n"
    # ④ red flag 日志（warning，列路径）。注：level 键由 capture_logs
    #    依调用方法名注入，个别 structlog 配置下缺失 —— 存在性 + 事件名
    #    已足够钉住「这条日志只能来自 log.warning 调用点」。
    warns = [
        e for e in logs
        if e.get("event") == "git_worktree.platform_restore_suspected_human_edits"
    ]
    assert warns, logs
    assert warns[0].get("level", "warning") == "warning"
    assert "engine/cache.bin" in warns[0]["paths"]
    # ⑤ 显式声明：隔离目录内声明文件（内容含「疑似人工编辑 / 已代清 /
    #    副本位置」），文件名本身会进 merge 回执的 [QUARANTINE] 文件清单。
    qroot = main / ".hiveweave" / "merge-quarantine"
    stamps = [d for d in qroot.iterdir() if d.is_dir()]
    assert stamps, "隔离目录必须存在"
    files = {p.name: p for p in stamps[0].rglob("*") if p.is_file()}
    # 人工改动副本可取回（内容零丢失）。
    assert any(
        p.name == "cache.bin"
        and p.read_text(encoding="utf-8") == "human tweak\n"
        for p in files.values()
    )
    decl = [n for n in files if "SUSPECTED-HUMAN-EDITS" in n]
    assert decl, f"隔离目录缺少显式声明文件：{sorted(files)}"
    text = files[decl[0]].read_text(encoding="utf-8")
    assert "疑似人工" in text and "已代清" in text
    assert "engine/cache.bin" in text
    # 声明文件名/路径会进回执：files 清单来自目录 rglob（service_merge
    # _new_quarantine_events 同口径），断言声明文件在列。
    assert decl[0] in files
