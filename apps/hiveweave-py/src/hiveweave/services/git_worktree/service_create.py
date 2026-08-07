"""GitWorktreeService create / checkpoint mixin."""
from __future__ import annotations

import asyncio
import os
import shutil
import threading
from pathlib import Path
from typing import Any, List, TYPE_CHECKING

import structlog

from .constants import (
    CHECKPOINT_PREFIX,
    GENERATED_FILES,
    GIT_TIMEOUT,
    GITIGNORE_GENERATED_ENTRIES,
    QUARANTINE_DIR,
    SHARED_DIR,
    WORKTREE_DIR,
    _RELOCATION_SUFFIXES,
    _WT_LIST_RE,
    _create_locks,
    _create_locks_guard,
    is_regenerable_path,
)
from .conflict_markers import _reject_if_markers_landed, scan_conflict_markers
from .git_cmd import _current_branch, _git, _resolve_base_branch
from .merge_support import (
    _auto_checkpoint_dirty_target,
    _merge_failure_result,
    parse_untracked_overwrite,
    quarantine_untracked_on_target,
)
from .naming import _branch_name, _slugify, compute_branch_name
from .paths import (
    _force_clear_path,
    _has_git,
    _is_bound_worktree_basename,
    _worktree_binding_under_project,
    _worktree_path,
)
from .porcelain import (
    _porcelain_non_hiveweave_dirty,
    _porcelain_tracked_dirty,
    _target_worktree_is_dirty,
)

log = structlog.get_logger(__name__)
from .reconcile import _log_worktree_rebuild_event

# Issue #3: serializes shared-dir sync per workspace. The sync runs in a
# thread executor (to_thread) — threading.Lock is the right primitive — and
# protects against a torn read when the root .hiveweave/shared is being
# written concurrently while we copy it into a worktree.
_shared_sync_locks: dict[str, threading.Lock] = {}
_shared_sync_locks_guard = threading.Lock()


def _workspace_sync_lock(workspace_path: str) -> threading.Lock:
    """Return the per-workspace sync lock, creating it under a guard.

    The guard makes the get-or-create atomic so two threads racing on the
    *first* sync of a workspace cannot each build a separate lock (which would
    bypass the serialization the lock exists to provide).
    """
    lock_key = str(Path(workspace_path).resolve())
    with _shared_sync_locks_guard:
        lock = _shared_sync_locks.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _shared_sync_locks[lock_key] = lock
        return lock


def _copy_if_newer(src: str, dst: str) -> bool:
    """Copy *src* to *dst* only when *dst* is missing or stale.

    ``shutil.copy2`` preserves mtime, so a dst whose mtime is at-or-newer than
    src is either an unchanged prior copy or a worktree-local edit — never
    silently clobber it (Issue #3 review: top-level files were unconditionally
    overwritten, wiping agent edits). Root-updated contracts (src newer) still
    propagate. A same-second update is detected via size when mtimes tie.
    Returns True when a copy was performed, False when skipped.
    """
    s = Path(src)
    d = Path(dst)
    if d.exists():
        try:
            sm = s.stat()
            dm = d.stat()
            if sm.st_mtime < dm.st_mtime:
                return False
            if sm.st_mtime == dm.st_mtime and sm.st_size == dm.st_size:
                return False
        except OSError:
            return False
    shutil.copy2(s, d)
    return True


def _copy_tree_skip_symlinks(src: Path, dst: Path) -> int:
    """Merge *src* into *dst*, skipping symlinks at every level.

    ``shutil.copytree`` cannot skip symlinks nested inside a directory (with
    ``symlinks=False`` it dereferences them via the copy function; with
    ``symlinks=True`` it recreates them). We walk manually so a symlink never
    has its target content pulled into a worktree — consistent with the
    top-level symlink handling. Returns the number of directories copied.
    """
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in src.iterdir():
        s = src / item.name
        if s.is_symlink():
            continue  # never dereference, at any depth
        d = dst / item.name
        if s.is_dir():
            copied += _copy_tree_skip_symlinks(s, d)
        elif s.is_file():
            _copy_if_newer(str(s), str(d))
    return copied + 1


class CreateMixin:
    """ensure_git_repo / create / checkpoint."""

    if TYPE_CHECKING:
        # Provided by the MergeMixin composed into GitWorktreeService.
        # Declared here so mypy can resolve the cross-mixin reference.
        _resolve_effective_worktree_path: Any

    async def ensure_git_repo(self, workspace_path: str) -> dict:
        """Ensure workspace is a git repo. Auto-init + master→main if needed.

        初始化时自动 commit 现有项目文件到 main 分支，这样 worktree
        创建时能继承完整代码。.gitignore 排除 node_modules/.hiveweave 等。

        Returns ``{success, initialized}`` or ``{success: False, message}``.
        """
        if _has_git(workspace_path):
            # Existing repo — still patch .gitignore idempotently (TEST6 P1-A).
            await self._ensure_gitignore_entries(workspace_path)
            return {"success": True, "initialized": False}

        ok, _ = await _git(["--version"], workspace_path)
        if not ok:
            return {"success": False, "message": "Git is not installed or not on PATH."}

        ok, _ = await _git(["init"], workspace_path)
        if not ok:
            return {"success": False, "message": "Failed to initialize git repository."}

        # Rename master → main (ignore failure — may already be main/trunk)
        await _git(["branch", "-m", "master", "main"], workspace_path)

        # Ensure git identity (needed for commits)
        await _git(["config", "user.email", "hiveweave@agent.local"], workspace_path)
        await _git(["config", "user.name", "HiveWeave Agent"], workspace_path)

        # 创建 .gitignore — 排除不应进入 worktree 的文件
        # (node_modules 每个 worktree 独立安装; .hiveweave 是系统目录;
        #  *.db 是数据库; dist/build 是构建产物; .env 是密钥)
        gitignore_path = Path(workspace_path) / ".gitignore"
        if not gitignore_path.exists():
            gitignore_content = """\
# HiveWeave 系统目录 (worktree 不继承)
.hiveweave/

# 依赖 (每个 worktree 独立安装)
node_modules/
.venv/
venv/

# 数据库
*.db
*.db-shm
*.db-wal

# 构建产物
dist/
build/
.next/
.nuxt/
.turbo/

# 密钥
.env
.env.*
!.env.example

# 缓存
__pycache__/
*.pyc
.cache/
coverage/

# 生成物 (tsc/vite/vitest 可再生输出 — TEST6 P1)
*.tsbuildinfo
test_output*.json
test-results/
playwright-report/

# IDE
.idea/
.vscode/
"""
            gitignore_path.write_text(gitignore_content, encoding="utf-8")
        else:
            await self._ensure_gitignore_entries(workspace_path)

        # P1-1: .gitattributes — lockfile union merge strategy.
        # package-lock.json conflicts are 100% predictable (every executor
        # runs npm install); union + post-merge regenerate eliminates rework.
        gitattributes_path = Path(workspace_path) / ".gitattributes"
        if not gitattributes_path.exists():
            gitattributes_content = """\
# HiveWeave P1-1: generated files use union merge (no content conflicts).
# Post-merge regeneration (npm install / pnpm install) fixes semantics.
package-lock.json merge=union
pnpm-lock.yaml merge=union
yarn.lock merge=union
"""
            gitattributes_path.write_text(gitattributes_content, encoding="utf-8")

        # 把现有项目文件 commit 到 main 分支
        await _git(["add", "-A"], workspace_path)
        ok, out = await _git(
            ["commit", "-m", "initial: project files imported by HiveWeave"],
            workspace_path,
        )
        if not ok:
            # 没有文件可 commit (空目录) — 用空提交兜底
            ok, _ = await _git(
                ["commit", "--allow-empty", "-m", "root: initialized by HiveWeave"],
                workspace_path,
            )
            if not ok:
                return {"success": False, "message": "Failed to create initial commit."}

        log.info("git_worktree.init_repo", workspace=workspace_path)
        return {"success": True, "initialized": True}

    async def _ensure_gitignore_entries(self, workspace_path: str) -> None:
        """TEST6 P1-A: add GITIGNORE_GENERATED_ENTRIES to the repo-local
        exclude file (``.git/info/exclude``), idempotently.

        Deliberately NOT the tracked ``.gitignore``: patching a tracked file
        would itself dirty main and trip the merge dirty gate. info/exclude
        is untracked, shared by all worktrees, and has identical semantics.
        """
        exclude = Path(workspace_path) / ".git" / "info" / "exclude"
        if not exclude.parent.exists():
            return
        try:
            existing = ""
            if exclude.exists():
                existing = exclude.read_text(
                    encoding="utf-8", errors="replace"
                )
            existing_lines = {
                ln.strip() for ln in existing.splitlines() if ln.strip()
            }
            missing = [
                e for e in GITIGNORE_GENERATED_ENTRIES
                if e not in existing_lines
            ]
            if not missing:
                return
            with open(exclude, "a", encoding="utf-8") as f:
                f.write(
                    "\n# HiveWeave generated artifacts (auto-appended)\n"
                    + "\n".join(missing)
                    + "\n"
                )
            log.info(
                "git_worktree.exclude_patched",
                workspace=workspace_path,
                added=missing,
            )
        except OSError as e:
            log.warning(
                "git_worktree.exclude_patch_failed",
                workspace=workspace_path,
                error=str(e),
            )

    def get_worktree_path(self, workspace_path: str, short_id: str) -> str | None:
        """Get the worktree path for an agent, or None if not found."""
        path = _worktree_path(workspace_path, short_id)
        return path if _has_git(path) else None

    def _sync_shared_contracts(self, workspace_path: str, worktree_path: str) -> None:
        """Issue #3: copy the project's ``.hiveweave/shared/`` into a worktree.

        ``.hiveweave/`` is gitignored, so a freshly created worktree has no
        shared contracts (``taskflow-contract.md`` etc.). Cross-end agents
        (frontend aligning to the backend API) previously had to either read
        the project root via an absolute path or peek into another owner's
        worktree. Mirroring the shared dir into each worktree makes contracts
        locally readable by every agent. Idempotent; missing source → no-op.

        Merge semantics: directories are merged by a manual walk that never
        ``rmtree``s a worktree-local shared subdir, so a repeated create cannot
        wipe files the agent added locally. Files (top-level and inside dirs)
        are copied incrementally via ``_copy_if_newer``: an unchanged dst is
        skipped, and a dst newer than src (a worktree-local edit) is preserved
        rather than clobbered. Symlinks are skipped at every level (we never
        dereference an external target into a worktree). A per-workspace lock
        serializes concurrent syncs to avoid torn reads.
        """
        if not worktree_path:
            return
        src = Path(workspace_path) / SHARED_DIR
        dst = Path(worktree_path) / SHARED_DIR
        if not src.exists():
            return
        with _workspace_sync_lock(workspace_path):
            dst.mkdir(parents=True, exist_ok=True)
            copied = 0
            skipped = 0
            for item in src.iterdir():
                s = src / item.name
                if s.is_symlink():
                    continue  # don't dereference external targets into worktrees
                d = dst / item.name
                if s.is_dir():
                    copied += _copy_tree_skip_symlinks(s, d)
                elif s.is_file():
                    if _copy_if_newer(s, d):
                        copied += 1
                    else:
                        skipped += 1
                else:
                    skipped += 1
            log.info(
                "git_worktree.shared_synced",
                worktree=worktree_path,
                copied=copied,
                skipped=skipped,
            )

    # ── 1. CREATE ────────────────────────────────────────────

    async def create(self, workspace_path: str, short_id: str,
                     task_name: str | None = None,
                     base_branch: str = "main", *,
                     task_id: str | None = None) -> dict:
        """Allocate an isolated worktree + branch for a subordinate agent.

        task_name: DEPRECATED — 保留兼容旧调用方, 不再参与分支命名
        (P0 命名稳定化, 见 compute_branch_name)。

        Returns ``{success, path, branch}`` or ``{success: False, message}``.
        """
        lock_key = f"{Path(workspace_path).resolve()}::{short_id}"
        async with _create_locks_guard:
            lock = _create_locks.get(lock_key)
            if lock is None:
                lock = asyncio.Lock()
                _create_locks[lock_key] = lock
        async with lock:
            result = await self._create_unlocked(
                workspace_path, short_id, task_name, base_branch, task_id=task_id
            )
        # Issue #3: contract sharing — .hiveweave/ is gitignored so shared
        # contract files never reach a fresh worktree, leaving cross-end agents
        # (e.g. frontend needing the backend API) able to read the contract only
        # by peeking into the project root. Sync the project's .hiveweave/shared/
        # into the worktree so contracts are locally visible to every agent.
        # Deliberately OUTSIDE the create lock and run in a thread executor: the
        # copy is blocking file I/O that must not hold the lock or stall the event
        # loop. Best-effort; never fails the create.
        if result.get("success"):
            wt_path = result.get("path") or ""
            try:
                await asyncio.to_thread(
                    self._sync_shared_contracts, workspace_path, wt_path
                )
            except Exception as e:
                log.warning(
                    "git_worktree.shared_sync_failed",
                    short_id=short_id,
                    error=str(e),
                )
        return result

    async def _create_unlocked(
        self,
        workspace_path: str,
        short_id: str,
        task_name: str | None = None,
        base_branch: str = "main",
        *,
        task_id: str | None = None,
    ) -> dict:
        repo = await self.ensure_git_repo(workspace_path)
        if not repo["success"]:
            return repo

        wt_root = Path(workspace_path) / WORKTREE_DIR
        wt_root.mkdir(parents=True, exist_ok=True)

        path = _worktree_path(workspace_path, short_id)
        branch = compute_branch_name(short_id, task_id)

        # Already exists and valid — idempotent.
        # P0 幂等脱钩修复: 返回 worktree 实际检出的分支, 不按当前入参
        # 新算 (新算名与检出分支可能不同: task_id 变化 / legacy slug 分支)。
        if _has_git(path):
            actual = await _current_branch(path)
            return {
                "success": True,
                "path": path,
                "branch": actual or branch,
                "message": (
                    "worktree already exists. "
                    "Evidence files: prefix with short_id "
                    f"(e.g. {short_id}-verify.txt), never bare shared names."
                ),
                "cleared_error": True,
            }

        # Stale cleanup. Two failure modes we must handle before add:
        # 1) Path exists but is not a valid worktree (partial delete) →
        #    `worktree add` fails with "'<path>' already exists".
        # 2) Path is gone but git still has a registered worktree entry →
        #    add fails with "is a missing but registered worktree" until prune.
        # Always prune when the target is not a valid worktree.
        relocated = False  # P0-3: flag for caller to notify agent
        if Path(path).exists():
            # P0-3: stop dev servers that may lock files (WinError 32 root cause)
            try:
                from hiveweave.services.process_registry import (
                    stop_processes_for_worktree,
                )

                stop_processes_for_worktree(path)
            except Exception:
                pass

            if not _force_clear_path(path):
                # P0-3: try git worktree repair + second clear attempt
                # (correct action for stale metadata, per postmortem §P0-3)
                await _git(["worktree", "repair", path], workspace_path)
                if not _force_clear_path(path):
                    # Last resort: alternate directory name (disk truly corrupted).
                    # P0-3: this is NOT silent — relocated flag notifies agent.
                    original_path = path
                    for suffix in _RELOCATION_SUFFIXES:
                        alt = path + suffix
                        # L5: if alt already exists as a valid worktree, reuse it
                        # (idempotent — prevents -c/-d proliferation when -b is healthy)
                        if _has_git(alt):
                            log.info(
                                "git_worktree.stale_path_reuse_existing",
                                original=path,
                                reuse=alt,
                            )
                            path = alt
                            relocated = True
                            await _log_worktree_rebuild_event(
                                workspace_path,
                                short_id,
                                reason="stale_path_reuse_existing",
                                original=original_path,
                                path=alt,
                            )
                            break
                        if not Path(alt).exists():
                            log.warning(
                                "git_worktree.stale_path_fallback",
                                original=path,
                                fallback=alt,
                            )
                            path = alt
                            relocated = True
                            await _log_worktree_rebuild_event(
                                workspace_path,
                                short_id,
                                reason="stale_path_fallback",
                                original=original_path,
                                path=alt,
                            )
                            break
                    else:
                        return {
                            "success": False,
                            "message": (
                                f"Failed to create worktree: stale path locked "
                                f"and could not be cleared: {path}"
                            ),
                        }
        await _git(["worktree", "prune"], workspace_path)

        fwd_path = path.replace("\\", "/")

        # If the agent branch already exists (worktree dir deleted but branch
        # kept), attach to it — do NOT -B reset, or we wipe executor commits.
        ok_list, branch_list = await _git(
            ["branch", "--list", branch], workspace_path
        )
        branch_exists = bool(
            ok_list and any(
                ln.strip().lstrip("* ").strip() == branch
                for ln in branch_list.splitlines()
                if ln.strip()
            )
        )
        if branch_exists:
            ok, out = await _git(
                ["worktree", "add", fwd_path, branch], workspace_path
            )
            if ok:
                log.info("git_worktree.create", short_id=short_id,
                         branch=branch, base="existing-branch")
                return {
                    "success": True,
                    "path": path,
                    "branch": branch,
                    "message": (
                        f"Worktree ready. Name evidence files with {short_id}- "
                        f"prefix to avoid merge collisions."
                    ),
                }
            last_error = out
            # Path-exists race: clear husk and retry attach once
            err_l = (out or "").lower()
            if "already exists" in err_l:
                _force_clear_path(path)
                await _git(["worktree", "prune"], workspace_path)
                ok2, out2 = await _git(
                    ["worktree", "add", fwd_path, branch], workspace_path
                )
                if ok2:
                    log.info(
                        "git_worktree.create_retry_after_path_exists",
                        short_id=short_id,
                        branch=branch,
                    )
                    return {
                        "success": True,
                        "path": path,
                        "branch": branch,
                        "message": (
                            f"Worktree ready. Name evidence files with "
                            f"{short_id}- prefix to avoid merge collisions."
                        ),
                    }
                last_error = out2 or out
            # Fall through: branch may be checked out elsewhere; try -B paths
        else:
            last_error = ""

        # 3-level fallback: origin/<base> → <base> → HEAD
        # HEAD 作为最终兜底（当前分支），避免在只有 main 的仓库上尝试不存在的 master
        # Use -b (create) when branch was absent; -B only as last resort after
        # attach failed (e.g. branch locked by another worktree).
        flag = "-B" if branch_exists else "-b"
        attempts = [
            ["worktree", "add", fwd_path, flag, branch, f"origin/{base_branch}"],
            ["worktree", "add", fwd_path, flag, branch, base_branch],
            ["worktree", "add", fwd_path, flag, branch, "HEAD"],
        ]
        for args in attempts:
            ok, out = await _git(args, workspace_path)
            if ok:
                log.info("git_worktree.create", short_id=short_id,
                         branch=branch, base=base_branch)
                return {
                    "success": True,
                    "path": path,
                    "branch": branch,
                    "message": (
                        f"Worktree ready. Name evidence files with {short_id}- "
                        f"prefix to avoid merge collisions."
                    ),
                }
            last_error = out
            # branch_exists detection can miss (format/race); -b then fails with
            # "a branch named X already exists" — or path husk left → clear + attach.
            err_l = (out or "").lower()
            if "already exists" in err_l:
                _force_clear_path(path)
                await _git(["worktree", "prune"], workspace_path)
                ok_att, out_att = await _git(
                    ["worktree", "add", fwd_path, branch], workspace_path
                )
                if ok_att:
                    log.info(
                        "git_worktree.create_attached_after_exists",
                        short_id=short_id,
                        branch=branch,
                    )
                    return {
                        "success": True,
                        "path": path,
                        "branch": branch,
                        "message": (
                            f"Worktree ready (attached existing branch). "
                            f"Name evidence files with {short_id}- prefix."
                        ),
                    }
                last_error = out_att or out

        # Final heal: another path may have created a valid tree during races
        if _has_git(path):
            actual = await _current_branch(path)
            log.info(
                "git_worktree.create_healed_existing",
                short_id=short_id,
                branch=actual or branch,
                prior_error=last_error,
            )
            return {
                "success": True,
                "path": path,
                "branch": actual or branch,
                "message": "worktree healthy after create race",
                "cleared_error": True,
            }

        log.error("git_worktree.create_failed", short_id=short_id,
                  path=path, branch=branch, error=last_error)
        return {"success": False, "message": f"Failed to create worktree: {last_error}"}
    # ── 2. CHECKPOINT ────────────────────────────────────────

    async def checkpoint(self, workspace_path: str, short_id: str,
                         message: str) -> dict:
        """Snapshot current state (git add -A + commit). No empty commits.

        Returns ``{success, hash, count}`` or ``{success: False, message}``.
        """
        path = await self._resolve_effective_worktree_path(
            workspace_path, short_id
        )
        if not Path(path).is_dir():
            return {"success": False,
                    "message": f"Worktree for {short_id} does not exist."}

        ok, _ = await _git(["add", "-A"], path)
        if not ok:
            return {"success": False, "message": "Failed to stage files"}

        # P1-1: strip GENERATED_FILES from staging (lockfiles cause predictable
        # merge conflicts; they should be regenerated post-merge, not committed).
        # TEST6 P1-B: regenerable artifacts (tsbuildinfo / test_output*.json)
        # are stripped too — and de-tracked when already tracked, so they stop
        # dirtying every future checkout (merge-blocking main dirt).
        regen_stripped: list[str] = []
        try:
            ok_st, staged_out = await _git(
                ["diff", "--cached", "--name-only"], path
            )
            if ok_st and staged_out:
                gen_stripped: list[str] = []
                for ln in staged_out.splitlines():
                    fname = ln.strip()
                    if not fname:
                        continue
                    # Match by basename (lockfile at any depth)
                    basename = fname.rsplit("/", 1)[-1] if "/" in fname else fname
                    if basename in GENERATED_FILES:
                        gen_stripped.append(fname)
                    elif is_regenerable_path(fname):
                        regen_stripped.append(fname)
                if gen_stripped:
                    await _git(
                        ["reset", "HEAD", "--"] + gen_stripped, path
                    )
                    log.info(
                        "checkpoint_generated_files_stripped",
                        short_id=short_id,
                        files=gen_stripped[:10],
                    )
                if regen_stripped:
                    # De-track: stage the removal so the merge lands
                    # "untracked + gitignored" on main, not just unstaged.
                    await _git(
                        ["rm", "--cached", "--quiet", "--"] + regen_stripped,
                        path,
                    )
                    log.info(
                        "checkpoint_regenerable_detracked",
                        short_id=short_id,
                        files=regen_stripped[:10],
                    )
        except Exception:
            pass  # best-effort: don't fail checkpoint on strip

        # Regenerable files get their own note — the generic ignored-files
        # warning below advises `git add -f`, which for this class just gets
        # stripped again next checkpoint (audit: dead-loop advice).
        regen_note = ""
        if regen_stripped:
            regen_note = (
                f" NOTE: {len(regen_stripped)} regenerable artifact(s) "
                f"de-tracked by design (never committed): "
                f"{', '.join(regen_stripped[:5])}"
                f"{'...' if len(regen_stripped) > 5 else ''}. To keep one "
                f"as evidence, rename it with your short_id prefix (e.g. "
                f"{short_id}-evidence.txt) and checkpoint again."
            )

        # P1 fix(TEST10): 检测被 .gitignore 屏蔽的产物文件
        # 如果 worktree 中有文件被 ignore，checkpoint 不会包含它们，
        # merge 后产物会静默丢失。主动警告 agent。
        ignored_warning = ""
        try:
            # 列出所有未跟踪+被忽略的文件
            ok_ign, ign_out = await _git(
                ["status", "--porcelain", "--ignored", "-u"], path
            )
            if ok_ign and ign_out:
                ignored_files = [
                    ln[3:].strip() for ln in ign_out.split("\n")
                    if ln.startswith("!!")
                ]
                # 只关注可能是产物的文件（排除 .pyc/__pycache__/.hiveweave 等）
                # 再生文件走上面的 regen_note（`git add -f` 建议对它们是死循环）
                _NOISE = (".pyc", "__pycache__", ".hiveweave/", "node_modules/",
                          ".venv/", ".git/")
                product_ignored = [
                    f for f in ignored_files
                    if not any(n in f for n in _NOISE)
                    and not is_regenerable_path(f)
                ]
                if product_ignored:
                    ignored_warning = (
                        f" WARNING: {len(product_ignored)} file(s) are "
                        f".gitignore'd and will NOT be committed: "
                        f"{', '.join(product_ignored[:5])}"
                        f"{'...' if len(product_ignored) > 5 else ''}. "
                        f"Fix .gitignore or use `git add -f` to force-include."
                    )
                    log.warning(
                        "checkpoint_ignored_files",
                        short_id=short_id,
                        files=product_ignored[:10],
                    )
        except Exception:
            pass  # best-effort: don't fail checkpoint on ignore check

        # No changes → return current HEAD, count=0
        ok, status = await _git(["status", "--porcelain"], path)
        if ok and status == "":
            ok2, head = await _git(["rev-parse", "--short", "HEAD"], path)
            return {"success": True, "hash": head if ok2 else "",
                    "count": 0,
                    "message": "no changes to commit" + ignored_warning + regen_note}

        commit_msg = f"{CHECKPOINT_PREFIX} {message}"
        ok, _ = await _git(["commit", "-m", commit_msg], path)
        if not ok:
            return {"success": False, "message": "Failed to create checkpoint commit"}

        ok, head = await _git(["rev-parse", "--short", "HEAD"], path)
        count = await self._count_checkpoints(path)
        log.info("git_worktree.checkpoint", short_id=short_id,
                 hash=head if ok else "", count=count)
        return {"success": True, "hash": head if ok else "", "count": count,
                "message": (ignored_warning + regen_note) or None}

    async def _count_checkpoints(self, path: str) -> int:
        """Count checkpoint commits in the last 7 days."""
        ok, log_out = await _git(
            ["log", "--oneline", f"--grep={CHECKPOINT_PREFIX}",
             "--since=7 days ago"],
            path,
        )
        if ok and log_out:
            return len([ln for ln in log_out.split("\n") if ln.strip()])
        return 1

    # ── 3. MERGE ─────────────────────────────────────────────
