"""GitWorktreeService lifecycle mixin (rollback / delete / list / info)."""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any, List, TYPE_CHECKING

import structlog

from .constants import (
    CHECKPOINT_PREFIX,
    GENERATED_FILES,
    GIT_TIMEOUT,
    QUARANTINE_DIR,
    WORKTREE_DIR,
    _RELOCATION_SUFFIXES,
    _WT_LIST_RE,
    _create_locks,
    _create_locks_guard,
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


class LifecycleMixin:
    """rollback / quarantine / delete / list / info."""

    if TYPE_CHECKING:
        # Provided by the CreateMixin / MergeMixin composed into
        # GitWorktreeService. Declared here so mypy can resolve the
        # cross-mixin references.
        _resolve_effective_worktree_path: Any
        checkpoint: Any

    async def rollback(self, workspace_path: str, short_id: str,
                       commit_hash: str | None = None) -> dict:
        """Reset worktree to a previous checkpoint (or latest checkpoint).

        契约 09 安全加固: rollback 前先 checkpoint 存档当前状态 (源码未做).

        Returns ``{success, hash, message}`` or ``{success: False, message}``.
        """
        path = await self._resolve_effective_worktree_path(
            workspace_path, short_id
        )
        if not Path(path).is_dir():
            return {"success": False,
                    "message": f"Worktree for {short_id} does not exist."}

        # Safety: snapshot current state before discarding (契约 09 安全加固)
        await self.checkpoint(workspace_path, short_id, "pre-rollback-snapshot")

        target = commit_hash
        if not target:
            ok, h = await _git(
                ["log", "--format=%H", f"--grep={CHECKPOINT_PREFIX}", "-1"],
                path,
            )
            target = h if (ok and h) else None

        if not target:
            return {"success": False,
                    "message": f"No checkpoints found for {short_id}."}

        ok, _ = await _git(["reset", "--hard", target], path)
        if not ok:
            return {"success": False,
                    "message": f"Rollback failed for {short_id}"}

        ok, head = await _git(["rev-parse", "--short", "HEAD"], path)
        ok2, msg = await _git(["log", "-1", "--format=%s"], path)
        log.info("git_worktree.rollback", short_id=short_id,
                 hash=head if ok else "", target=target)
        return {"success": True,
                "hash": head if ok else "",
                "message": msg if ok2 else ""}

    # ── 5. DELETE (remove) ──────────────────────────────────

    async def quarantine_for_review(
        self, workspace_path: str, short_id: str
    ) -> dict:
        """Move a worktree aside instead of deleting it (BUG-2).

        Used when dismiss happens while submitted/reviewing tasks still need
        evidence from this tree. Git registration is pruned; the directory is
        relocated under ``.hiveweave/worktrees/_quarantine/<sid>-<ts>/``.
        Branch is preserved (not deleted).
        """
        path = await self._resolve_effective_worktree_path(
            workspace_path, short_id
        )
        fwd_path = path.replace("\\", "/")
        branch = None
        if _has_git(path):
            branch = await _current_branch(path)
        if not branch:
            branch = compute_branch_name(short_id)

        # Detach from git's worktree list without deleting the branch
        ok, _ = await _git(["worktree", "remove", fwd_path], workspace_path)
        if not ok:
            ok, _ = await _git(
                ["worktree", "remove", fwd_path, "--force"], workspace_path
            )
        if not ok and Path(path).exists():
            # Still on disk — just prune registration; we'll move the dir
            await _git(["worktree", "prune"], workspace_path)

        q_root = Path(workspace_path) / QUARANTINE_DIR
        q_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = q_root / f"{short_id}-{stamp}"
        if Path(path).exists():
            try:
                shutil.move(str(path), str(dest))
            except OSError as e:
                log.warning(
                    "git_worktree.quarantine_move_failed",
                    short_id=short_id,
                    path=path,
                    dest=str(dest),
                    error=str(e),
                )
                # Fall back to leaving the tree in place (still reviewable)
                dest = Path(path)
        else:
            dest = Path(path)

        log.info(
            "git_worktree.quarantined_for_review",
            short_id=short_id,
            branch=branch,
            quarantine=str(dest),
        )
        return {
            "success": True,
            "quarantined": True,
            "path": str(dest),
            "branch": branch,
            "short_id": short_id,
        }

    async def delete(self, workspace_path: str, short_id: str,
                     task_name: str | None = None, *,
                     task_id: str | None = None,
                     branch: str | None = None,
                     discard: bool = False) -> dict:
        """Discard agent's worktree (rejected/obsolete work) — 删除安全链 (P0).

        Orca 式生命周期语义:
        ① ``git worktree remove`` — 先不带 --force, 失败再 --force,
           仍失败 rmtree 兜底 + prune;
        ② 分支默认 ``git branch -d`` — git 自己拒删未合并分支; 拒删时
           **不强删**, preserved_branch={branch, head, reason} 透出;
        ③ 仅显式 ``discard=True`` (确认丢弃被拒工作的场景) 才 CAS 强删:
           ``update-ref -d refs/heads/<b> <expectedHead>`` — 分支在
           rev-parse 之后移动则 CAS 失败, 放弃并透出。

        task_name: DEPRECATED — 仅为旧调用方保留, 只作 legacy slug 分支
        的兜底解析, 不参与新命名。

        Always returns ``{success: True, removed: True, branch,
        preserved_branch}`` (best-effort).
        """
        path = await self._resolve_effective_worktree_path(
            workspace_path, short_id
        )
        fwd_path = path.replace("\\", "/")

        # 分支解析必须在 worktree 删除之前 — 检出分支信息随目录一起消失
        target = branch
        if not target and _has_git(path):
            target = await _current_branch(path)
        if not target and task_id:
            target = compute_branch_name(short_id, task_id)
        if not target and task_name:
            target = _branch_name(short_id, task_name)  # legacy slug 兼容
        if not target:
            target = compute_branch_name(short_id)  # 稳定 /work 名兜底

        # ⓪ P0-3: stop registered dev servers locking files in this worktree
        # (WinError 32 root cause — node holds node_modules open handles)
        try:
            from hiveweave.services.process_registry import (
                stop_processes_for_worktree,
            )

            kill_report = stop_processes_for_worktree(path)
            if kill_report.get("stopped"):
                log.info(
                    "git_worktree.delete_processes_stopped",
                    short_id=short_id,
                    stopped=kill_report["stopped"],
                )
            if kill_report.get("failed"):
                log.warning(
                    "git_worktree.delete_processes_stop_failed",
                    short_id=short_id,
                    failed=kill_report["failed"],
                )
        except Exception as proc_err:
            log.warning(
                "git_worktree.delete_process_cleanup_error",
                short_id=short_id,
                error=str(proc_err),
            )

        # ① worktree 移除链: remove → remove --force → rmtree + prune
        removed = True
        ok, _ = await _git(["worktree", "remove", fwd_path], workspace_path)
        if not ok:
            ok, _ = await _git(
                ["worktree", "remove", fwd_path, "--force"], workspace_path
            )
        if not ok:
            # Worktree may not be registered — delete directory manually
            shutil.rmtree(path, ignore_errors=True)
            await _git(["worktree", "prune"], workspace_path)
            # 2026-08-11 A023 事故：Windows 文件锁下 rmtree 静默失败 →
            # husk 永久残留。失败必须透出（removed=False），由调用方/reconcile
            # 重试，而不是假装删除成功。
            if Path(path).exists():
                log.error(
                    "git_worktree.delete_dir_remove_failed",
                    short_id=short_id,
                    path=str(path),
                    hint="Directory locked (Device busy). Reconcile will retry; "
                    "kill the holding process or move it aside if persistent.",
                )
                removed = False

        # ②/③ 分支处置 (分支不存在时 _dispose_branch 直接返回 None)
        preserved = await self._dispose_branch(workspace_path, target, discard)

        log.info("git_worktree.delete", short_id=short_id, branch=target,
                 preserved=preserved is not None, discard=discard)
        return {
            "success": True,
            "removed": removed,
            "branch": target,
            "preserved_branch": preserved,
        }

    async def _dispose_branch(self, workspace_path: str, branch: str,
                              discard: bool) -> dict | None:
        """分支处置: 默认 -d 安全删, discard=True 走 CAS 强删。

        返回 None = 分支已删除/不存在; 否则 preserved_branch dict 透出。
        """
        if not await self._branch_exists(workspace_path, branch):
            return None
        if discard:
            return await self._discard_branch(workspace_path, branch)

        ok, out = await _git(["branch", "-d", branch], workspace_path)
        if ok:
            return None
        # git 拒删 (未完全合并/被占用) — 不强删, 透出给调用方决策
        ok_h, head = await _git(["rev-parse", "--short", branch], workspace_path)
        preserved: dict = {
            "branch": branch,
            "head": head.strip() if ok_h else "",
            "reason": "unmerged",
        }
        detail = (out or "").splitlines()
        if detail:
            preserved["detail"] = detail[0]
        log.warning("git_worktree.branch_preserved", branch=branch,
                    reason=detail[0] if detail else "branch -d refused")
        return preserved

    async def _discard_branch(self, workspace_path: str,
                              branch: str) -> dict | None:
        """CAS 强删: ``update-ref -d refs/heads/<b> <expectedHead>``。

        先 rev-parse 拿 expected head; CAS 失败说明分支已移动 —
        绝不盲删, 放弃并透出。
        """
        ok_h, head = await _git(["rev-parse", branch], workspace_path)
        if not ok_h or not head.strip():
            return None  # 分支已不存在
        expected = head.strip()
        ok, out = await _git(
            ["update-ref", "-d", f"refs/heads/{branch}", expected],
            workspace_path,
        )
        if ok:
            log.info("git_worktree.branch_discarded", branch=branch,
                     head=expected[:7])
            return None
        log.warning("git_worktree.discard_cas_failed", branch=branch,
                    expected=expected[:7], error=(out or "")[:200])
        return {
            "branch": branch,
            "head": expected[:7],
            "reason": "cas_failed",
            "detail": (out.splitlines()[0] if out else "ref moved"),
        }

    async def _branch_exists(self, workspace_path: str, branch: str) -> bool:
        # --format 不带 * / + 前缀 (检出标记), 精确匹配整行即可
        ok, out = await _git(
            ["branch", "--list", branch, "--format=%(refname:short)"],
            workspace_path,
        )
        return bool(
            ok and any(ln.strip() == branch
                       for ln in out.splitlines() if ln.strip())
        )

    # ── 6. LIST ─────────────────────────────────────────────

    async def list(self, workspace_path: str) -> dict:
        """List all HiveWeave-managed worktrees.

        Returns ``{success, entries: [...]}``. Filters to only those under
        ``.hiveweave/worktrees/``.
        """
        ok, raw = await _git(["worktree", "list"], workspace_path)
        if not ok:
            return {"success": True, "entries": []}

        entries: list[dict] = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = _WT_LIST_RE.match(line)
            if not m:
                continue
            wt_path = m.group(1).strip()
            wt_fwd = wt_path.replace("\\", "/")
            if WORKTREE_DIR in wt_fwd:
                entries.append({
                    "short_id": Path(wt_path).name,
                    "path": wt_path,
                    "branch": (m.group(3) or "").strip(),
                    "head": m.group(2)[:7],
                    "active": Path(wt_path).exists(),
                })
        return {"success": True, "entries": entries}

    # ── 7. INFO (status) ────────────────────────────────────

    async def info(self, workspace_path: str, short_id: str) -> dict:
        """Detailed status of one agent's worktree.

        Returns ``{success, status: {...} | None}``.

        TEST6 audit S9: includes mechanical merge facts
        (``tip_is_ancestor_of_main``, ``commits_ahead``, ``base_branch``)
        so agents never claim "delivered to main" from memory.
        """
        path = await self._resolve_effective_worktree_path(
            workspace_path, short_id
        )
        if not Path(path).is_dir():
            return {"success": True, "status": None}

        ok, head = await _git(["rev-parse", "--short", "HEAD"], path)
        if not ok:
            return {"success": True, "status": None}

        ok2, branch = await _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
        ok3, st = await _git(["status", "--porcelain"], path)
        has_uncommitted = bool(st) if ok3 else True

        checkpoints = await self._checkpoint_list(path)

        base = await _resolve_base_branch(workspace_path)
        tip_is_ancestor: bool | None = None
        commits_ahead: int | None = None
        if base:
            ok_anc, _ = await _git(
                ["merge-base", "--is-ancestor", "HEAD", base], path
            )
            tip_is_ancestor = bool(ok_anc)
            try:
                from hiveweave.services.worktree_review import (
                    worktree_commits_ahead,
                )

                commits_ahead = await worktree_commits_ahead(
                    workspace_path, path, target_branch=base
                )
            except Exception:
                commits_ahead = None

        return {"success": True, "status": {
            "short_id": short_id,
            "branch": branch if ok2 else "",
            "active": True,
            "has_uncommitted": has_uncommitted,
            "head": head,
            "checkpoints": checkpoints,
            "base_branch": base or None,
            "tip_is_ancestor_of_main": tip_is_ancestor,
            "commits_ahead": commits_ahead,
        }}

    async def _checkpoint_list(self, path: str) -> List[dict]:
        """Get recent checkpoints (limit 20) with hash/date/message."""
        ok, raw = await _git(
            ["log", "--format=%h|%ad|%s", "--date=short",
             f"--grep={CHECKPOINT_PREFIX}", "-20"],
            path,
        )
        if not ok or not raw:
            return []

        entries: list[dict] = []
        for line in raw.split("\n"):
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            h, date, msg = parts
            # Strip the "checkpoint: " prefix from the displayed message
            if msg.startswith(f"{CHECKPOINT_PREFIX} "):
                msg = msg[len(CHECKPOINT_PREFIX) + 1:]
            entries.append({"hash": h, "date": date, "message": msg})
        return entries
