"""Git worktree constants (contract 09)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

WORKTREE_DIR = ".hiveweave/worktrees"
QUARANTINE_DIR = ".hiveweave/worktrees/_quarantine"
HIVEWEAVE_DIR = ".hiveweave"
SHARED_DIR = ".hiveweave/shared"

# Agent-visible workspace dirs tracked in git (跨 worktree 可见的共享区).
# These are NOT gitignored: contracts/reports/drafts/handoffs must survive
# checkpoints and merges. Everything else under .hiveweave stays private.
TRACKED_WS_DIRS: tuple[str, ...] = (
    ".hiveweave/shared",
    ".hiveweave/reports",
    ".hiveweave/drafts",
    ".hiveweave/handoffs",
)

# .hiveweave dirs that remain platform-private (never tracked, never merged):
# worktree checkouts, tool outputs, logs. Porcelain dirty-scan and
# conflict-marker scans must keep ignoring these even though *shared* is now
# tracked — otherwise merge-gate and marker checks start failing on
# .hiveweave/worktrees/... (TEST? P-R audit).
PRIVATE_WS_DIRS: tuple[str, ...] = (
    ".hiveweave/worktrees",
    ".hiveweave/tool_outputs",
    ".hiveweave/logs",
    ".hiveweave/db",
)

# P1-1: Generated files that cause predictable merge conflicts.
# Checkpoint strips these from commits; merge auto-regenerates after landing.
# "生成物不随提交走" — platform-level enforcement, not prompt-level advice.
GENERATED_FILES: frozenset[str] = frozenset({
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Pipfile.lock",
    "poetry.lock",
    "composer.lock",
    "Gemfile.lock",
})

# TEST6 P1: regenerable artifacts produced by tsc/vite/vitest runs.
# These must never be checkpointed, never block merge as "dirty main", and
# are de-tracked on sight. Dirty main full of these cost a cross-agent
# git-stash round-trip (TEST6 23:21-23:23) and left residue behind.
REGENERABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|/)[^/]*\.tsbuildinfo$"),
    re.compile(r"(?:^|/)test_output[^/]*\.json$"),
    # 引擎自生成缓存（fixlist #7）：这不是团队写的东西 —— 报成「main 脏」会
    # 让门禁建议 commit，而 commit 那一路会把引擎缓存**正式入库**、反向制造
    # 下一个坑（TEST_DSH_51 实测：`.godot/global_script_class_cache.cfg` 被拒
    # 2 次；同引擎同版本同一天的 50 因叶子补了 .gitignore 而零复发）。
    # 注意与 `ENGINE_GITIGNORE_SEEDS` 互补：种子防新增入库，本模式治**已有**脏。
    re.compile(r"(?:^|/)\.godot/(?:.*)$"),
)


def is_regenerable_path(path: str) -> bool:
    """True when *path* is a regenerable build/test artifact."""
    norm = (path or "").replace("\\", "/")
    return any(rx.search(norm) for rx in REGENERABLE_PATTERNS)


# .gitignore entries the platform guarantees for every project repo.
# ensure_git_repo appends the missing ones idempotently (existing repos
# created before this list grew still get patched).
GITIGNORE_GENERATED_ENTRIES: tuple[str, ...] = (
    "*.tsbuildinfo",
    "test_output*.json",
    "test-results/",
    "playwright-report/",
    ".agent-browser/",
    # ACL 沙箱项目级共享缓存（spec §8）：checkpoint 是无条件 git add -A，
    # 缓存必须排除在提交外。放 .hiveweave-cache/ 而非 .hiveweave/ 内侧，
    # 防 _HIVEWEAVE_FILE_OPS 前缀误伤 + 不受 §4.9 PROTECTED 裁剪影响。
    ".hiveweave-cache/",
    # B-2 平台审计产物：首轮审计遗留 .audit/ untracked 噪音；git ignore 后平台
    # Remove-Item -Recurse 清理流程不再碰它，避免撞 120s 权限超时。
    ".audit/",
    # P0（2026-09-05，s3c09 git×ACL 交集）：.hiveweave 私有区（sandbox-temp/
    # data.db/tool_outputs/logs 等）不再新增跟踪 —— 本清单走 .git/info/exclude
    # （优先级低于 tracked .gitignore，老仓库模板反选不受扰；已跟踪文件不受
    # ignore 影响，只拦「新 add」）。反选四共享目录必须跟在 .hiveweave/* 之后
    # （同文件内 gitignore 后行覆盖前行），否则新写入的共享产物会被误忽略。
    ".hiveweave/*",
    "!.hiveweave/shared/",
    "!.hiveweave/reports/",
    "!.hiveweave/drafts/",
    "!.hiveweave/handoffs/",
)

# ── 引擎自生成产物种子（fixlist #7）────────────────────────────
# 会自生成缓存的引擎类项目，其缓存必须尽早被排除，否则 MAIN 常脏 → merge 被
# 拒；而 merge 门禁处方「clean 或 commit」的 commit 那一路会把引擎缓存**正式
# 入库**，反向制造下一个坑。
#
# 实测依据（TEST_DSH_50/51，同引擎同版本同一天）：51 被拒 2 次
# （`.godot/global_script_class_cache.cfg`，`.gitignore` 自建项目起未改、
# `.godot/**` 已被 9399bb3 入库）；50 因叶子补了 `.gitignore` 而**零复发**。
# 定性纪律：这是**模板缺失**，不是「团队不小心」。
#
# 探测口径 = 目录里出现引擎标记文件，所以在每次幂等补条时重新探测即可 ——
# agent 写出 `project.godot` 之后的第一次 ensure 就会补上，不依赖"建项目那一刻"。
ENGINE_GITIGNORE_SEEDS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    # (标记文件, 额外排除条目)
    (
        ("project.godot",),
        # 只排缓存目录。`*.import` / `*.uid` **必须保持版本化** —— 把它们忽略
        # 掉会让资源引用断链（50 号在 5054b7d 已踩过这一层）。
        (".godot/",),
    ),
)


def detect_engine_gitignore_entries(workspace_path: str) -> tuple[str, ...]:
    """探测 workspace 里出现的引擎标记文件，返回应额外排除的条目（去重保序）。"""
    root = Path(workspace_path)
    found: list[str] = []
    for markers, entries in ENGINE_GITIGNORE_SEEDS:
        try:
            if any((root / marker).exists() for marker in markers):
                found.extend(entries)
        except OSError:
            # 目录不可读时当作未探测到：种子是**加分项**，不该让 ensure 失败
            continue
    return tuple(dict.fromkeys(found))


# BUG-4: serialize create per (workspace, short_id) so hire + lazy-ensure
# cannot race and leave a false worktree_error while the tree is healthy.
_create_locks: dict[str, asyncio.Lock] = {}
_create_locks_guard = asyncio.Lock()
CHECKPOINT_PREFIX = "checkpoint:"
GIT_TIMEOUT = 30.0
SLUG_MAX_LEN = 40

# slugify regexes (契约 09: 保留 CJK \u4e00-\u9fff)
_SLUG_SPACE = re.compile(r"[\s/\\]+")
_SLUG_INVALID = re.compile(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+")
_SLUG_TRIM = re.compile(r"^-+|-+$")

# Match: "<path>  <hash> [<branch>]" from `git worktree list`
_WT_LIST_RE = re.compile(r"^(.+?)\s+([a-f0-9]+)\s*(?:\[(.+?)\])?$")

# git merge/checkout: untracked files would be overwritten
_UNTRACKED_OVERWRITE_RE = re.compile(
    r"(?:untracked working tree files would be overwritten|"
    r"The following untracked working tree files would be overwritten)"
    r"[\s\S]*?(?:Please move or remove them|Aborting)",
    re.IGNORECASE,
)
_UNTRACKED_FILE_LINE_RE = re.compile(r"^\t(.+)$", re.MULTILINE)

# create() last-resort suffixes when canonical path is locked (WinError 32).
_RELOCATION_SUFFIXES = ("-b", "-c", "-d")

# ── 冲突标记扫描 (merge 成功后 main 树残留检测) ─────────────
# 行首锚定 <<<<<<< / >>>>>>> (标准 git conflict marker, 7 字符)。
# 故意不含 ^={7} — 一行等号同时是 setext 标题下划线, 误报率高。
_CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}|>{7})", re.MULTILINE)

# 扫描时跳过的目录: 系统目录 / 依赖 / 构建产物 (口径与 ensure_git_repo
# 生成的 .gitignore 一致, 另含 worktree 宿主目录 .hiveweave)
_MARKER_SCAN_SKIP_DIRS = frozenset({
    ".git", ".hiveweave", "node_modules", "dist", "build",
    ".next", ".nuxt", ".turbo", ".venv", "venv", "__pycache__",
    ".cache", "coverage", ".idea", ".vscode",
})

_MARKER_SCAN_MAX_BYTES = 1_000_000  # 大文件跳过 (大概率是产物/压缩包)
_MARKER_SCAN_MAX_HITS = 50          # 报告上限, 防止异常输出刷屏

# 新稳定命名 hw/<sid>/t-<taskid8> 的解析正则; 非 t- 后缀即 legacy slug 分支
_TASK_BRANCH_RE = re.compile(r"^hw/[^/]+/t-(.{8})$")

# Protect reconcile from deleting dirs still needed for in-flight / pending-merge work
_PROTECT_TASK_STATUSES = frozenset({
    "created", "claimed", "running", "blocked", "submitted",
    "reviewing", "rework", "verifying", "approved",
})
# After a successful merge, approved work is done — retain worktree only if
# assignee still has truly in-flight tasks (not the just-merged approved ones).
_IN_FLIGHT_AFTER_MERGE_STATUSES = frozenset({
    "created", "claimed", "running", "blocked", "submitted",
    "reviewing", "rework", "verifying",
})
