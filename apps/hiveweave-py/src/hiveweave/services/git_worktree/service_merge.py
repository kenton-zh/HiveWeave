"""GitWorktreeService merge mixin."""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import List

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
    restore_regenerable_dirt_or_reject,
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
)
from .reconcile import _assignee_has_open_tasks

log = structlog.get_logger(__name__)


class MergeMixin:
    """merge / merge_by_branch / branch resolution."""

    async def _resolve_agent_branch(self, workspace_path: str, short_id: str,
                                    task_name: str | None,
                                    task_id: str | None) -> str:
        """解析 agent 分支名 — 事实优先, 入参兜底。

        1. worktree 还在 → 实际检出分支 (legacy slug / t- 新名通吃,
           根治 task_name 重算与首次命名脱钩导致的 merge 解析问题)
        2. 有 task_id → 稳定命名 t-<id8>
        3. 只有 task_name → legacy slug 命名 (向后兼容旧调用方)
        """
        path = await self._resolve_effective_worktree_path(
            workspace_path, short_id
        )
        if _has_git(path):
            actual = await _current_branch(path)
            if actual:
                return actual
        if task_id:
            return compute_branch_name(short_id, task_id)
        return _branch_name(short_id, task_name or "task")

    @staticmethod
    async def _resolve_effective_worktree_path(
        workspace_path: str, short_id: str
    ) -> str:
        """Single source: actual worktree dir for merge/checkpoint/rollback/…

        Prefer DB ``workspace_path`` when it is a legal binding for this
        short_id (canonical **or** ``-b/-c/-d`` relocate), has ``.git``,
        and resolves under this project's ``.hiveweave/worktrees/``.
        Only then fall back to the canonical path. Preferring canonical when
        both exist re-created the TEST_YLGY A015 split: heal wrote to
        ``A015-b`` while checkpoint/merge stared at locked ``A015``.
        """
        canonical = _worktree_path(workspace_path, short_id)
        try:
            from hiveweave.services.org import OrgService

            org = OrgService()
            agents = await org.list_agents()
            for a in agents:
                if (a.get("short_id") or "") != short_id:
                    continue
                db_path = (a.get("workspace_path") or "").strip()
                if not db_path or not _has_git(db_path):
                    break
                basename = Path(db_path).name
                if not _is_bound_worktree_basename(basename, short_id):
                    break
                if not _worktree_binding_under_project(db_path, workspace_path):
                    log.warning(
                        "git_worktree.resolve_effective_path_outside_project",
                        short_id=short_id,
                        workspace=workspace_path,
                        db_path=db_path,
                    )
                    break
                if db_path != canonical:
                    log.info(
                        "git_worktree.resolve_effective_path_db",
                        short_id=short_id,
                        canonical=canonical,
                        actual=db_path,
                    )
                return db_path
        except Exception:
            pass
        return canonical

    async def _validate_merge_preconditions(
        self, workspace_path: str, short_id: str, branch: str,
        target_branch: str = "main",
    ) -> dict | None:
        """D3 precondition gate: validate worktree health before merge.

        Prevents the TEST16 D3 scenario where a husk worktree (no branch,
        no .git) gets "merged" via file copy, producing orphan commits.

        Checks 1-3 only apply when the worktree directory EXISTS on disk.
        If the directory is absent the branch may still be mergeable from
        the main repo (e.g. worktree removed but branch preserved).

        Returns None if all preconditions pass (caller should proceed),
        or a dict result to return early (error or no-op success).
        """
        # P0-1: resolve effective path (DB fallback for relocated worktrees)
        path = await self._resolve_effective_worktree_path(
            workspace_path, short_id
        )

        # Checks 1-3: only relevant when the worktree directory exists.
        # A missing directory is NOT an error — the branch can still be
        # merged from the main repo (worktree deleted, branch preserved).
        if Path(path).is_dir():
            # 1. Source directory must have .git (file or dir)
            if not _has_git(path):
                log.warning(
                    "git_worktree.merge_precondition_no_git",
                    short_id=short_id, path=path, branch=branch,
                )
                return {
                    "success": False,
                    "reason": "precondition_failed",
                    "message": (
                        f"Merge precondition failed: worktree directory for "
                        f"{short_id} has no .git (husk detected at {path}). "
                        f"This worktree is corrupted. Trigger worktree repair "
                        f"(re-create + reattach) before merging."
                    ),
                    "branch": branch,
                }

            # 2. Source directory must be a registered worktree
            ok_wl, wl_out = await _git(["worktree", "list"], workspace_path)
            if ok_wl:
                norm_path = os.path.normcase(str(Path(path)))
                registered = any(
                    os.path.normcase(line.split()[0]) == norm_path
                    for line in (wl_out or "").splitlines()
                    if line.strip()
                )
                if not registered:
                    log.warning(
                        "git_worktree.merge_precondition_not_registered",
                        short_id=short_id, path=path, branch=branch,
                    )
                    return {
                        "success": False,
                        "reason": "precondition_failed",
                        "message": (
                            f"Merge precondition failed: worktree directory "
                            f"for {short_id} ({path}) is not registered in "
                            f"'git worktree list'. The worktree metadata is "
                            f"missing. Trigger worktree repair (re-create + "
                            f"reattach) before merging."
                        ),
                        "branch": branch,
                    }

            # 3. Source must have a registered branch (not detached HEAD)
            actual_branch = await _current_branch(path)
            if not actual_branch:
                log.warning(
                    "git_worktree.merge_precondition_detached_head",
                    short_id=short_id, path=path, branch=branch,
                )
                return {
                    "success": False,
                    "reason": "precondition_failed",
                    "message": (
                        f"Merge precondition failed: worktree for {short_id} "
                        f"({path}) is in detached HEAD state (no branch "
                        f"checked out). Cannot merge without a branch. "
                        f"Trigger worktree repair (re-create + reattach) "
                        f"before merging."
                    ),
                    "branch": branch,
                }

        # 4. Source must be ahead of target — ahead == 0 is a no-op success
        ok_ahead, ahead_out = await _git(
            ["rev-list", "--count", f"{target_branch}..{branch}"],
            workspace_path,
        )
        if ok_ahead and (ahead_out or "").strip() == "0":
            log.info(
                "git_worktree.merge_precondition_noop",
                short_id=short_id, branch=branch, target=target_branch,
            )
            return {
                "success": True,
                "merged": False,
                "already_up_to_date": True,
                "message": (
                    f"No-op merge: branch {branch} has 0 commits ahead of "
                    f"{target_branch} — nothing to merge. The worktree is "
                    f"already up to date with {target_branch}."
                ),
                "branch": branch,
                "ahead": 0,
                "short_id": short_id,
            }

        return None

    async def merge(self, workspace_path: str, short_id: str,
                    task_name: str | None = None,
                    target_branch: str = "main", *,
                    task_id: str | None = None) -> dict:
        """Merge agent branch into target (git merge --no-edit), then cleanup.

        契约 09 RECONCILE: 用 --no-edit (非 ff-only), 成功后自动 remove worktree+分支.
        冲突时 abort, worktree 保留 — executor 在自己的 worktree 里合 main 解冲突.

        task_name: DEPRECATED — 不再用于命名; 分支以 worktree 实际检出
        为准 (legacy slug 分支通吃), 仅作无 worktree 时的 legacy 兜底。

        Returns ``{success, merged, hash, branch, files?}`` or
        ``{success: False, message, conflicts?, branch, files?}``.
        """
        branch = await self._resolve_agent_branch(
            workspace_path, short_id, task_name, task_id
        )

        # D3 precondition validation: reject husk/corrupted worktrees early
        precond = await self._validate_merge_preconditions(
            workspace_path, short_id, branch, target_branch
        )
        if precond is not None:
            return precond

        ok, _ = await _git(["checkout", target_branch], workspace_path)
        if not ok:
            return {"success": False,
                    "message": f"Failed to checkout {target_branch}",
                    "branch": branch}

        # Dirty main → split by regenerability (TEST6 P1-C; shared with
        # merge_by_branch via merge_support.restore_regenerable_dirt_or_reject).
        reject = await restore_regenerable_dirt_or_reject(
            workspace_path, branch=branch, short_id=short_id
        )
        if reject is not None:
            return reject

        # Capture files covered by this branch before merge mutates history
        ok_f, files_out = await _git(
            ["diff", "--name-only", f"{target_branch}...{branch}"],
            workspace_path,
        )
        branch_files = [
            f.strip() for f in (files_out or "").split("\n") if f.strip()
        ] if ok_f else []

        ok, merge_out = await _git(["merge", branch, "--no-edit"], workspace_path)
        if not ok:
            fail = await _merge_failure_result(
                workspace_path=workspace_path,
                branch=branch,
                target_branch=target_branch,
                merge_out=merge_out or "",
                branch_files=branch_files,
                short_id=short_id,
                auto_quarantine=True,
            )
            if fail is None:
                # Quarantined untracked — retry once
                ok, merge_out = await _git(
                    ["merge", branch, "--no-edit"], workspace_path
                )
                if not ok:
                    fail = await _merge_failure_result(
                        workspace_path=workspace_path,
                        branch=branch,
                        target_branch=target_branch,
                        merge_out=merge_out or "",
                        branch_files=branch_files,
                        short_id=short_id,
                        auto_quarantine=False,
                    )
                    assert fail is not None
                    return fail
            else:
                return fail

        ok, head = await _git(["rev-parse", "--short", "HEAD"], workspace_path)

        # Marker gate BEFORE cleanup — abort must keep the source branch
        # (audit T1#1: delete-then-scan left delivery only in reflog).
        marker_reject = await _reject_if_markers_landed(
            workspace_path,
            short_id=short_id,
            branch=branch,
            target_branch=target_branch,
            branch_files=branch_files,
        )
        if marker_reject is not None:
            return marker_reject

        # Auto-remove worktree + branch on success (契约 09 RECONCILE)
        # TEST3: skip delete when assignee still has other open tasks.
        cleanup_note = ""
        try:
            if await _assignee_has_open_tasks(workspace_path, short_id):
                log.info(
                    "git_worktree.merge_cleanup_skipped_open_tasks",
                    short_id=short_id,
                    branch=branch,
                )
                cleanup_note = (
                    " NOTE: worktree retained — assignee still has open tasks."
                )
            else:
                cleanup = await self.delete(workspace_path, short_id, branch=branch)
                preserved = (cleanup or {}).get("preserved_branch")
                if preserved:
                    log.warning(
                        "git_worktree.merge_cleanup_preserved_branch",
                        short_id=short_id,
                        branch=preserved.get("branch"),
                        reason=preserved.get("reason"),
                    )
                    cleanup_note = (
                        f" WARNING: worktree removed but branch "
                        f"{preserved.get('branch')} preserved "
                        f"({preserved.get('reason') or 'unmerged'}); "
                        f"reconcile will retry later."
                    )
        except Exception as cleanup_err:
            log.warning(
                "git_worktree.merge_cleanup_failed",
                short_id=short_id,
                branch=branch,
                error=str(cleanup_err),
            )
            cleanup_note = (
                f" WARNING: merge succeeded but worktree cleanup failed "
                f"({cleanup_err}); reconcile will retry later."
            )

        already = "already up to date" in (merge_out or "").lower()
        log.info("git_worktree.merge", short_id=short_id,
                 target=target_branch, hash=head if ok else "",
                 already_up_to_date=already)
        result = {
            "success": True,
            "merged": True,
            "hash": head if ok else "",
            "branch": branch,
            "files": branch_files,
            "short_id": short_id,
        }
        if already:
            result["already_up_to_date"] = True
            result["message"] = (
                f"Branch {branch} already on {target_branch} "
                f"(no new commits) — treated as merged."
            )
        if cleanup_note:
            result["cleanup_warning"] = cleanup_note.strip()
            base = result.get("message") or "Worktree merged"
            result["message"] = f"{base}{cleanup_note}"
        return result
    async def merge_by_branch(self, workspace_path: str, branch: str,
                              target_branch: str = "main") -> dict:
        """Merge a specific branch by full name (Bug G fix + Bug L enhancement).

        Enhanced merge flow:
        1. Rebase worktree branch onto latest target_branch (reduces conflicts)
        2. Attempt git merge
        3. On conflict: try semantic merge for package.json, report conflict files
        4. Post-merge verification: check key files exist
        5. Auto-remove worktree on success

        Returns ``{success, merged, hash, message?, conflicts?}`` or
        ``{success: False, message, conflicts?}``.
        """
        import json as _json
        from pathlib import Path as _Path

        # D3 precondition validation: reject husk/corrupted worktrees early
        parts = branch.split("/", 2)
        short_id = parts[1] if len(parts) >= 2 else ""
        if short_id:
            precond = await self._validate_merge_preconditions(
                workspace_path, short_id, branch, target_branch
            )
            if precond is not None:
                return precond

        # Step 0: Fetch latest target_branch
        ok, _ = await _git(["checkout", target_branch], workspace_path)
        if not ok:
            return {"success": False,
                    "message": f"Failed to checkout {target_branch}"}

        # Dirty main → split by regenerability (TEST6 P1-C; was a hard
        # reject here until the audit found merge_by_branch uncovered —
        # 4 of 5 git_worktree_merge tool paths resolve through here).
        reject = await restore_regenerable_dirt_or_reject(
            workspace_path, branch=branch, short_id=short_id
        )
        if reject is not None:
            return reject

        # Step 1: Rebase worktree branch onto target_branch to minimize conflicts
        wt_path = ""
        if short_id:
            wt_path = await self._resolve_effective_worktree_path(
                workspace_path, short_id
            )

        if wt_path and _Path(wt_path).is_dir():
            # Checkpoint worktree state before rebase
            await _git(["add", "-A"], wt_path)
            await _git(["commit", "-m", "pre-merge-checkpoint", "--allow-empty"],
                       wt_path)
            # Rebase onto target_branch
            ok_reb, reb_out = await _git(
                ["rebase", target_branch], wt_path)
            if not ok_reb:
                # Rebase conflict — abort rebase, continue with 3-way merge
                await _git(["rebase", "--abort"], wt_path)
                log.warning("git_worktree.rebase_failed",
                            branch=branch, output=reb_out[:200])

        # Capture files covered by this branch before merge
        ok_f, files_out = await _git(
            ["diff", "--name-only", f"{target_branch}...{branch}"],
            workspace_path,
        )
        branch_files = [
            f.strip() for f in (files_out or "").split("\n") if f.strip()
        ] if ok_f else []

        # Step 2: Merge with target_branch
        ok, merge_out = await _git(["merge", branch, "--no-edit"], workspace_path)

        if not ok:
            # Step 3a: Untracked on MAIN — quarantine + retry (NOT executor rework)
            untracked = parse_untracked_overwrite(merge_out or "")
            if untracked:
                await _git(["merge", "--abort"], workspace_path)
                moved = await quarantine_untracked_on_target(
                    workspace_path, untracked
                )
                if moved:
                    ok, merge_out = await _git(
                        ["merge", branch, "--no-edit"], workspace_path
                    )
                if not ok:
                    still = parse_untracked_overwrite(merge_out or "")
                    if still or not moved:
                        # Still untracked (or quarantine moved nothing) —
                        # do NOT fall through as content-conflict / fake rework.
                        await _git(["merge", "--abort"], workspace_path)
                        from hiveweave.services.worktree_review import (
                            format_untracked_on_target_message,
                        )

                        return {
                            "success": False,
                            "reason": "untracked_on_target",
                            "message": format_untracked_on_target_message(
                                branch=branch,
                                target=target_branch,
                                untracked=still or untracked,
                            ),
                            "untracked": still or untracked,
                            "conflicts": [],
                            "branch": branch,
                            "files": branch_files,
                            "short_id": short_id,
                        }
                    # Quarantine worked but retry failed for another reason
                    # (e.g. real content conflict) — fall through to 3b.

            if not ok:
                # Step 3b: Content conflict — try semantic merge for package.json
                ok_diff, diff_out = await _git(
                    ["diff", "--name-only", "--diff-filter=U"], workspace_path
                )
                conflict_files = [
                    f.strip()
                    for f in (diff_out or "").split("\n")
                    if f.strip()
                ]

                resolved = []
                pkg_path = _Path(workspace_path) / "package.json"
                if "package.json" in conflict_files and pkg_path.exists():
                    try:
                        ok_ours, ours_raw = await _git(
                            ["show", f"{target_branch}:package.json"],
                            workspace_path,
                        )
                        ok_theirs, theirs_raw = await _git(
                            ["show", f"{branch}:package.json"], workspace_path
                        )
                        if ok_ours and ok_theirs:
                            ours = _json.loads(ours_raw)
                            theirs = _json.loads(theirs_raw)
                            for dep_key in (
                                "dependencies",
                                "devDependencies",
                                "peerDependencies",
                                "scripts",
                            ):
                                if dep_key in ours or dep_key in theirs:
                                    merged_deps = ours.get(dep_key, {})
                                    merged_deps.update(theirs.get(dep_key, {}))
                                    ours[dep_key] = merged_deps
                            pkg_path.write_text(
                                _json.dumps(
                                    ours, indent=2, ensure_ascii=False
                                )
                                + "\n",
                                encoding="utf-8",
                            )
                            await _git(["add", "package.json"], workspace_path)
                            resolved.append("package.json")
                    except Exception as e:
                        log.warning(
                            "git_worktree.package_merge_failed",
                            branch=branch,
                            error=str(e),
                        )

                if resolved:
                    ok_commit, _ = await _git(
                        ["commit", "--no-edit"], workspace_path
                    )
                    if ok_commit:
                        ok = True
                        log.info(
                            "git_worktree.semantic_merge_resolved",
                            branch=branch,
                            resolved_files=resolved,
                        )

                if not ok:
                    fail2 = await _merge_failure_result(
                        workspace_path=workspace_path,
                        branch=branch,
                        target_branch=target_branch,
                        merge_out=merge_out or "",
                        branch_files=branch_files,
                        short_id=short_id,
                        auto_quarantine=False,
                    )
                    assert fail2 is not None
                    if conflict_files and fail2.get("reason") != "untracked_on_target":
                        from hiveweave.services.worktree_review import (
                            format_merge_conflict_message,
                        )

                        fail2 = {
                            **fail2,
                            "reason": "merge_conflict",
                            "conflicts": conflict_files,
                            "message": format_merge_conflict_message(
                                branch=branch,
                                target=target_branch,
                                conflicts=conflict_files,
                            ),
                        }
                    return fail2

        # Step 4: Post-merge verification
        verification_errors = []
        pkg_path = _Path(workspace_path) / "package.json"
        if pkg_path.exists():
            try:
                pkg = _json.loads(pkg_path.read_text(encoding="utf-8"))
                if not pkg.get("scripts"):
                    verification_errors.append("package.json missing scripts")
                if not pkg.get("dependencies") and not pkg.get("devDependencies"):
                    verification_errors.append("package.json missing dependencies")
            except Exception:
                verification_errors.append("package.json is invalid JSON")

        if verification_errors:
            log.warning("git_worktree.merge_verification_failed",
                        branch=branch, errors=verification_errors)
            # Don't rollback — warn but allow (agent can fix)

        ok_head, head = await _git(
            ["rev-parse", "--short", "HEAD"], workspace_path
        )

        # Marker gate BEFORE cleanup (same as merge — audit T1#1)
        marker_reject = await _reject_if_markers_landed(
            workspace_path,
            short_id=short_id or "",
            branch=branch,
            target_branch=target_branch,
            branch_files=branch_files,
        )
        if marker_reject is not None:
            return marker_reject

        # Step 5: Auto-remove worktree + branch on success
        # 显式传已合并的分支全名 — delete 走 branch -d 安全链, 必然成功
        # TEST3: retain worktree when assignee still has open tasks.
        cleanup_note = ""
        if short_id:
            try:
                if await _assignee_has_open_tasks(workspace_path, short_id):
                    log.info(
                        "git_worktree.merge_by_branch_cleanup_skipped_open_tasks",
                        short_id=short_id,
                        branch=branch,
                    )
                    cleanup_note = (
                        " NOTE: worktree retained — assignee still has open tasks."
                    )
                else:
                    cleanup = await self.delete(
                        workspace_path, short_id, branch=branch
                    )
                    preserved = (cleanup or {}).get("preserved_branch")
                    if preserved:
                        log.warning(
                            "git_worktree.merge_by_branch_cleanup_preserved",
                            short_id=short_id,
                            branch=preserved.get("branch"),
                            reason=preserved.get("reason"),
                        )
                        cleanup_note = (
                            f" WARNING: worktree removed but branch "
                            f"{preserved.get('branch')} preserved "
                            f"({preserved.get('reason') or 'unmerged'}); "
                            f"reconcile will retry later."
                        )
            except Exception as cleanup_err:
                log.warning(
                    "git_worktree.merge_by_branch_cleanup_failed",
                    short_id=short_id,
                    branch=branch,
                    error=str(cleanup_err),
                )
                cleanup_note = (
                    f" WARNING: merge succeeded but worktree cleanup failed "
                    f"({cleanup_err}); reconcile will retry later."
                )

        already = "already up to date" in (merge_out or "").lower()
        log.info("git_worktree.merge_by_branch", branch=branch,
                 target=target_branch, hash=head if ok_head else "",
                 warnings=verification_errors, already_up_to_date=already)
        result = {
            "success": True,
            "merged": True,
            "hash": head if ok_head else "",
            "branch": branch,
            "files": branch_files,
            "short_id": short_id,
        }
        if already:
            result["already_up_to_date"] = True
            result["message"] = (
                f"Branch {branch} already on {target_branch} "
                f"(no new commits) — treated as merged."
            )
        if cleanup_note:
            result["cleanup_warning"] = cleanup_note.strip()
            base = result.get("message") or "Worktree merged"
            result["message"] = f"{base}{cleanup_note}"
        if verification_errors:
            result["verification_warnings"] = verification_errors
        return result
