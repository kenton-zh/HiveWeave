"""Git worktree constants (contract 09)."""
from __future__ import annotations

import asyncio
import re

WORKTREE_DIR = ".hiveweave/worktrees"
QUARANTINE_DIR = ".hiveweave/worktrees/_quarantine"
HIVEWEAVE_DIR = ".hiveweave"
SHARED_DIR = ".hiveweave/shared"

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
    ".gstack/",
)

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
