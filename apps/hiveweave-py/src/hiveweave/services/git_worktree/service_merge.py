"""GitWorktreeService merge mixin."""
from __future__ import annotations

import asyncio
import os
import re
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
    classify_main_dirt,
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
from .reconcile import _assignee_has_open_tasks, _try_reattach_worktree

log = structlog.get_logger(__name__)


class MergeMixin:
    """merge / merge_by_branch / branch resolution."""

    if TYPE_CHECKING:
        # Provided by the LifecycleMixin composed into GitWorktreeService.
        # Declared here so mypy can resolve the cross-mixin reference.
        delete: Any

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

    async def _auto_repair_husk(
        self,
        workspace_path: str,
        short_id: str,
        path: str,
        branch: str,
    ) -> str | None:
        """Husk（目录无 .git）自动修复：停进程 → 删空壳 → 重新注册 worktree。

        2026-08-11 A023 事故：D3 预条件检测到 husk 只报 "Trigger worktree
        repair"，但 agent 侧无 repair 工具，团队只能手动 ``git worktree
        add`` 绕圈（CEO 反复撞 husk 40 分钟）。分支存在时这里直接重建到
        规范路径 —— 与磐石手动做的事等价，但平台自动完成。

        Returns None on success, or an error message on failure.
        """
        # 文件系统审计 F2：repair 会 rmtree —— path 必须是本项目
        # .hiveweave/worktrees/ 下的绑定目录（DB workspace_path 损坏时
        # _resolve_effective_worktree_path 可能返回任意路径；husk 无 .git
        # 绕过了 DB 分支的 binding 校验，这里必须兜底）。
        if not _worktree_binding_under_project(path, workspace_path):
            return (
                f"auto-repair refused: {path} is not a binding under this "
                f"project's .hiveweave/worktrees/ — refusing to delete it. "
                f"Fix the agent's workspace_path first."
            )
        try:
            from hiveweave.services.process_registry import (
                stop_processes_for_worktree,
            )

            stop_processes_for_worktree(path)
        except Exception:
            pass
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
        # 并发窗口二次 stat：stop 进程期间可能已有并发 add 复活该目录
        if Path(path).exists() and _has_git(path):
            return (
                f"auto-repair skipped: {path} regained .git during repair "
                f"(concurrent worktree add) — it is a live tree now, "
                f"retry the merge."
            )
        if Path(path).exists():
            return (
                f"auto-repair failed: could not remove husk directory {path} "
                f"(locked by a process — Device busy). Move it aside or kill "
                f"the holding process, then retry."
            )
        ok, out = await _git(
            ["worktree", "add", path.replace("\\", "/"), branch],
            workspace_path,
        )
        if not ok:
            return (
                f"auto-repair failed: git worktree add {path} {branch} "
                f"failed: {out.strip()[:200]}"
            )
        log.info(
            "git_worktree.merge_husk_auto_repaired",
            short_id=short_id,
            path=path,
            branch=branch,
        )
        return None

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
                # 2026-08-11 A023 事故：husk 自动修复（分支存在时重建到规范
                # 路径），修复成功重跑本函数继续校验；失败返回可操作错误。
                repair_err = await self._auto_repair_husk(
                    workspace_path, short_id, path, branch
                )
                if repair_err is None:
                    log.info(
                        "git_worktree.merge_precondition_husk_repaired",
                        short_id=short_id, path=path, branch=branch,
                    )
                    return await self._validate_merge_preconditions(
                        workspace_path, short_id, branch, target_branch
                    )
                return {
                    "success": False,
                    "reason": "precondition_failed",
                    "message": (
                        f"Merge precondition failed: worktree directory for "
                        f"{short_id} has no .git (husk detected at {path}). "
                        f"Auto-repair attempted but {repair_err} The worktree "
                        f"is corrupted; reconcile will also retry husk "
                        f"cleanup. Move the directory aside or kill the "
                        f"holding process, then retry."
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
                    reattached = await _try_reattach_worktree(
                        workspace_path, short_id, path
                    )
                    if reattached:
                        log.info(
                            "git_worktree.merge_precondition_reattached",
                            short_id=short_id, path=path, branch=branch,
                        )
                    else:
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
                                f"'git worktree list' and auto-reattach failed. "
                                f"The directory exists; retry git_worktree_merge "
                                f"after `git worktree repair` from the project root."
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

    async def preflight_merge(
        self,
        workspace_path: str,
        short_id: str,
        branch: str,
        target_branch: str = "main",
    ) -> dict:
        """只读 merge 预检（dry-run）— 列出全部缺失前置条件，零改动。

        与 ``_validate_merge_preconditions`` 的区别：不自动修复 husk、
        不清理 regenerable 脏文件、不提交 —— 纯检查。merge() 失败时
        ``_aggregate_merge_blockers`` 复用同一份检查聚合补报。

        Returns ``{"success": bool, "dry_run": True, "missing": [{code,
        message}], "already_up_to_date": bool, "branch": str}``。
        """
        missing: list[dict] = []
        path = await self._resolve_effective_worktree_path(
            workspace_path, short_id
        )
        if Path(path).is_dir():
            if not _has_git(path):
                missing.append({
                    "code": "worktree_husk",
                    "message": (
                        f"worktree 目录 {path} 无 .git（husk）— 需触发 "
                        f"worktree repair 后再 merge"
                    ),
                })
            else:
                ok_wl, wl_out = await _git(["worktree", "list"], workspace_path)
                if ok_wl:
                    norm_path = os.path.normcase(str(Path(path)))
                    registered = any(
                        os.path.normcase(line.split()[0]) == norm_path
                        for line in (wl_out or "").splitlines()
                        if line.strip()
                    )
                    if not registered:
                        missing.append({
                            "code": "worktree_unregistered",
                            "message": (
                                f"worktree 目录 {path} 未在 'git worktree "
                                f"list' 注册 — 需 repair（重建 + 重新挂接）"
                            ),
                        })
                    else:
                        actual = await _current_branch(path)
                        if not actual:
                            missing.append({
                                "code": "worktree_detached",
                                "message": (
                                    f"worktree {path} 处于 detached HEAD "
                                    f"— 无分支可合并，需 repair 后重试"
                                ),
                            })
        dirt = await classify_main_dirt(workspace_path)
        if dirt["hard_blockers"]:
            missing.append({
                "code": "main_dirty",
                "message": (
                    "MAIN 有未提交变更（merge 会硬拒）："
                    + ", ".join(dirt["hard_blockers"][:8])
                ),
            })
        ok_b, b_out = await _git(["branch", "--list", branch], workspace_path)
        branch_exists = bool(ok_b) and any(
            (ln.strip()[2:].strip() if ln.strip().startswith(("* ", "+ "))
             else ln.strip()) == branch
            for ln in (b_out or "").splitlines()
            if ln.strip()
        )
        if not branch_exists:
            missing.append({
                "code": "branch_missing",
                "message": f"分支 {branch} 不存在（可能已合并拆除）",
            })
        ahead = 0
        ok_ahead, ahead_out = await _git(
            ["rev-list", "--count", f"{target_branch}..{branch}"],
            workspace_path,
        )
        if ok_ahead:
            try:
                ahead = int((ahead_out or "0").strip() or "0")
            except ValueError:
                ahead = 0
        return {
            "success": not missing,
            "dry_run": True,
            "missing": missing,
            "already_up_to_date": ahead == 0 and not missing,
            "branch": branch,
        }

    async def _aggregate_merge_blockers(
        self,
        workspace_path: str,
        short_id: str,
        branch: str,
        target_branch: str,
        base: dict,
        *,
        skip_codes: set[str],
    ) -> dict:
        """merge 失败时聚合补报其余缺失前置条件（撞门前一次性可见）。

        ``base`` 是已命中的失败 dict（首个错误文案原样保留）；缺失项里
        与 base 同源（已被首错覆盖）的 code 经 ``skip_codes`` 过滤。
        """
        try:
            report = await self.preflight_merge(
                workspace_path, short_id, branch, target_branch
            )
            extras = [
                i for i in report.get("missing", []) if i["code"] not in skip_codes
            ]
            if extras:
                base = dict(base)
                msg = str(base.get("message") or "")
                base["message"] = (
                    msg
                    + "\n\n[additional blockers]\n"
                    + "\n".join(
                        f"- [{i['code']}] {i['message']}" for i in extras
                    )
                )
        except Exception as e:
            log.debug("merge_aggregate_blockers_failed", error=str(e))
        return base

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
            return await self._aggregate_merge_blockers(
                workspace_path, short_id, branch, target_branch, precond,
                skip_codes={
                    "worktree_husk", "worktree_unregistered", "worktree_detached",
                },
            )

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
            return await self._aggregate_merge_blockers(
                workspace_path, short_id, branch, target_branch, reject,
                skip_codes={"main_dirty"},
            )

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
                return await self._aggregate_merge_blockers(
                    workspace_path, short_id, branch, target_branch, precond,
                    skip_codes={
                        "worktree_husk", "worktree_unregistered",
                        "worktree_detached",
                    },
                )

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
            if short_id:
                return await self._aggregate_merge_blockers(
                    workspace_path, short_id, branch, target_branch, reject,
                    skip_codes={"main_dirty"},
                )
            return reject

        # Step 1: Rebase worktree branch onto target_branch to minimize conflicts
        wt_path = ""
        if short_id:
            wt_path = await self._resolve_effective_worktree_path(
                workspace_path, short_id
            )

        if wt_path and _Path(wt_path).is_dir():
            # Checkpoint worktree state before rebase — skip vacuum empty commits
            # (TEST6 evening P3-7: empty pre-merge-checkpoint polluted main tip).
            await _git(["add", "-A"], wt_path)
            ok_diff, diff_out = await _git(
                ["diff", "--cached", "--quiet"], wt_path
            )
            # diff --quiet: exit 0 = no staged changes; exit 1 = dirty
            has_staged = not ok_diff
            if has_staged:
                await _git(
                    ["commit", "-m", "pre-merge-checkpoint"],
                    wt_path,
                )
            else:
                log.info(
                    "git_worktree.pre_merge_checkpoint_skipped_empty",
                    branch=branch,
                )
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


# ── merge → 任务结转：同分支 running 任务自动置 submitted ─────
# 里程碑主任务死锁（owner 自营任务 merge 完成但永不 submit）根因：
# merge 成功路径只处理 approved/verifying（VERIFY spawn），running 任务
# 无结转钩子。分支↔任务可精确反查（稳定命名 hw/<sid>/t-<taskid8>），
# 且 running → submitted 是唯一合法转换（tasks/constants.py），天然保守。

_TASK_BRANCH_RE = re.compile(r"^hw/([^/]+)/t-([0-9a-fA-F]{8})$")


async def auto_submit_running_task_after_merge(
    project_id: str,
    workspace_path: str,
    *,
    branch: str,
    short_id: str | None = None,
    merged_by: str,
    merge_commit: str | None = None,
    already_on_main: bool = False,
) -> tuple[int, list[str]]:
    """merge 成功（或分支已合入 main）后，同分支的 running 任务自动 submit。

    - 仅处理稳定命名分支 ``hw/<sid>/t-<taskid8>``（legacy slug 分支无法
      可靠反查任务，跳过）。
    - 候选任务：id 前 8 位 == 分支前缀 + status == "running" + assignee ==
      分支 owner。非 running 不动（created/claimed 无合法 → submitted 转换）。
    - ``already_on_main=False`` 时先跑 ``git branch --merged`` 确认分支已
      合入 main，避免未合入误提交。
    - submit 走 TaskService.submit_task（自带 reviewer pinning + review
      obligation 激活），任何失败只记日志，绝不回滚/中断 merge。

    Returns ``(submitted 数, 任务标题列表)``。
    """
    m = _TASK_BRANCH_RE.match(branch or "")
    if not m:
        return 0, []
    branch_sid = m.group(1)
    prefix8 = m.group(2).lower()
    if short_id and short_id.upper() != branch_sid.upper():
        log.info(
            "git_worktree.auto_submit_branch_sid_wins",
            branch=branch, short_id=short_id, branch_sid=branch_sid,
        )

    if not already_on_main:
        base = await _resolve_base_branch(workspace_path) or "main"
        ok, out = await _git(
            ["branch", "--merged", base, "--list", branch], workspace_path
        )
        merged = ok and any(
            line.strip() == branch
            for line in (out or "").splitlines()
            if line.strip()
        )
        if not merged:
            return 0, []

    try:
        from hiveweave.services.org import OrgService

        agents = await OrgService().list_agents(project_id)
        owner_id = next(
            (
                a.get("id")
                for a in agents
                if (a.get("short_id") or "").upper() == branch_sid.upper()
            ),
            None,
        )
    except Exception as e:
        log.warning("git_worktree.auto_submit_owner_resolve_failed",
                    branch=branch, error=str(e))
        return 0, []
    if not owner_id:
        log.info("git_worktree.auto_submit_owner_missing",
                 branch=branch, short_id=branch_sid)
        return 0, []

    try:
        from hiveweave.services.task import TaskService

        ts = TaskService()
        tasks = await ts.list_tasks(project_id)
    except Exception as e:
        log.warning("git_worktree.auto_submit_task_query_failed",
                    branch=branch, error=str(e))
        return 0, []

    candidates = [
        t
        for t in tasks
        if str(t.get("id") or "").lower().startswith(prefix8)
        and t.get("status") == "running"
        and str(t.get("assignee_id") or "") == str(owner_id)
    ]
    if not candidates:
        return 0, []

    # E8: merge 后整体性检查槽——软件实例静态扫描，merge 成功路径统一入口
    # （semantic：合成的整体不允许从未被整体性地看过一眼就放行）。FAIL 时
    # auto-submit evidence 前置 verdict=FAIL + blocking_issues，由 E2 强制
    # 路由转到 rework；扫描本身只进回执，绝不中断/回滚 merge。
    integrity = None
    try:
        from hiveweave.services.git_worktree.integrity import (
            run_integrity_checks,
        )

        integrity = await run_integrity_checks(workspace_path, branch)
    except Exception as e:
        log.debug(
            "git_worktree.integrity_scan_failed", branch=branch, error=str(e)
        )

    now_ms = int(time.time() * 1000)
    submitted: list[str] = []
    submitted_ids: list[str] = []
    for t in candidates:
        tid = t.get("id")
        if not tid:
            continue
        evidence = {
            "merged_by": merged_by,
            "merge_commit": merge_commit,
            "merged_at": now_ms,
            "auto_submitted_by_merge": True,
        }
        if integrity is not None and not integrity.passed:
            evidence["verdict"] = "FAIL"
            evidence["blocking_issues"] = list(integrity.issues)
            evidence["integrity_check"] = "fail"
        else:
            # E1 schema：VERIFY 类候选也必须带 verdict；整体性检查通过 → PASS。
            evidence["verdict"] = "PASS"
            evidence["integrity_check"] = "pass"
        try:
            await ts.submit_task(project_id, tid, evidence)
        except Exception as e:
            log.warning(
                "git_worktree.auto_submit_task_failed",
                task_id=tid, branch=branch, error=str(e),
            )
            continue
        submitted.append(str(t.get("title") or tid))
        submitted_ids.append(str(tid)[:8])
    if submitted:
        log.info(
            "git_worktree.auto_submitted_running_tasks",
            project_id=project_id, branch=branch, count=len(submitted),
            task_ids=submitted_ids,
        )
    return len(submitted), submitted
