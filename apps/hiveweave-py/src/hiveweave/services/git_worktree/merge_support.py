"""Merge failure classification and untracked quarantine."""
from __future__ import annotations

import shutil
from pathlib import Path

import structlog

from .constants import (
    _UNTRACKED_FILE_LINE_RE,
    _UNTRACKED_OVERWRITE_RE,
    is_regenerable_path,
)
from .git_cmd import _git
from .porcelain import _porcelain_tracked_dirty_paths

log = structlog.get_logger(__name__)


async def restore_regenerable_dirt_or_reject(
    workspace_path: str, *, branch: str = "", short_id: str = ""
) -> dict | None:
    """TEST6 P1-C shared dirty-target gate for merge() and merge_by_branch().

    All-regenerable dirt (tsbuildinfo / test_output*.json left by tsc /
    vitest runs on the main checkout) is auto-cleaned and merge proceeds —
    previously this hard-reject cost a cross-agent git-stash round-trip
    (TEST6 23:21-23:23) and left residue behind. Any non-regenerable
    tracked dirt → hard reject (unchanged contract: no auto-commit spam
    on main history).

    Restore is split by presence in HEAD: paths known to HEAD are restored
    via ``checkout HEAD --``; staged-new regenerable files (an agent's
    manual ``git add`` — never committed, so HEAD has no pathspec) are
    de-staged with ``rm --cached`` instead (file stays on disk, and F1's
    info/exclude keeps it out of status afterwards). Discarded content is
    regenerable by definition, so the loss is acceptable by design.

    Returns a rejection dict the caller returns verbatim, or None when the
    target is clean / was cleaned and the merge may proceed.
    """
    ok_st, st_out = await _git(["status", "--porcelain"], workspace_path)
    dirty_paths = _porcelain_tracked_dirty_paths(st_out) if ok_st else []
    if not dirty_paths:
        return None
    non_regen = [p for p in dirty_paths if not is_regenerable_path(p)]
    if non_regen:
        return {
            "success": False,
            "reason": "main_dirty",
            "message": (
                "MAIN has uncommitted changes; clean or commit on a "
                "side branch first. Auto-save to main history is "
                f"disabled. Dirty: {', '.join(non_regen[:8])}"
            ),
            "branch": branch,
        }
    ok_lt, lt_out = await _git(
        ["ls-tree", "-r", "--name-only", "HEAD", "--"] + dirty_paths,
        workspace_path,
    )
    in_head = {ln.strip() for ln in (lt_out or "").splitlines() if ln.strip()}
    if not ok_lt:
        in_head = set()
    in_head_paths = [p for p in dirty_paths if p in in_head]
    staged_new = [p for p in dirty_paths if p not in in_head]
    if staged_new:
        ok_rm, rm_out = await _git(
            ["rm", "--cached", "--quiet", "--"] + staged_new, workspace_path
        )
        if not ok_rm:
            return {
                "success": False,
                "reason": "main_dirty",
                "message": (
                    "MAIN has regenerable-only dirt but de-staging "
                    f"staged-new files failed: {(rm_out or '')[:300]}"
                ),
                "branch": branch,
            }
    if in_head_paths:
        ok_restore, restore_out = await _git(
            ["checkout", "HEAD", "--"] + in_head_paths, workspace_path
        )
        if not ok_restore:
            return {
                "success": False,
                "reason": "main_dirty",
                "message": (
                    "MAIN has regenerable-only dirt but auto-restore "
                    f"failed: {(restore_out or '')[:300]}"
                ),
                "branch": branch,
            }
    log.info(
        "git_worktree.merge_regenerable_dirt_restored",
        short_id=short_id,
        restored=in_head_paths[:10],
        destaged=staged_new[:10],
    )
    return None


async def _abort_landed_merge(workspace_path: str) -> bool:
    """Undo a bad merge on target — reset to ORIG_HEAD or merge --abort."""
    ok_reset, _ = await _git(["reset", "--hard", "ORIG_HEAD"], workspace_path)
    if ok_reset:
        return True
    ok_abort, _ = await _git(["merge", "--abort"], workspace_path)
    return ok_abort


async def _auto_checkpoint_dirty_target(
    workspace_path: str, target_branch: str
) -> bool:
    """If target (usually main) has local changes, commit a pre-merge checkpoint.

    TEST11 evening P3-1: dirty main caused "not a content conflict" merge
    failures. Previously we only advised; now we checkpoint automatically.
    Returns True if a checkpoint commit was created.
    """
    ok_st, st_out = await _git(["status", "--porcelain"], workspace_path)
    if not ok_st or not (st_out or "").strip():
        return False
    await _git(["add", "-A"], workspace_path)
    ok_c, out_c = await _git(
        [
            "commit",
            "-m",
            f"pre-merge-checkpoint: auto-save dirty {target_branch}",
            "--allow-empty",
        ],
        workspace_path,
    )
    if ok_c:
        log.info(
            "git_worktree.pre_merge_main_checkpoint",
            target=target_branch,
            dirty_preview=(st_out or "")[:200],
        )
        return True
    log.warning(
        "git_worktree.pre_merge_checkpoint_failed",
        target=target_branch,
        output=(out_c or "")[:200],
    )
    return False


async def _current_branch(worktree_path: str) -> str | None:
    """worktree 实际检出的分支 (``git -C <path> rev-parse --abbrev-ref HEAD``)。

    幂等/解析的唯一事实来源: 路径还在, 就以检出分支为准, 不按入参
    重算 (重算名与检出分支可能脱钩)。detached HEAD 返回 None。
    """
    ok, out = await _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
    if ok and out and out.strip() != "HEAD":
        return out.strip()
    return None


def parse_untracked_overwrite(git_output: str) -> list[str]:
    """Extract paths from 'untracked working tree files would be overwritten'."""
    if not git_output or not _UNTRACKED_OVERWRITE_RE.search(git_output):
        return []
    files: list[str] = []
    for m in _UNTRACKED_FILE_LINE_RE.finditer(git_output):
        path = m.group(1).strip().replace("\\", "/")
        if path and path not in files:
            files.append(path)
    return files


async def quarantine_untracked_on_target(
    workspace_path: str, files: list[str]
) -> list[str]:
    """Move untracked files that block merge into ``.hiveweave/merge-quarantine/``.

    Returns list of successfully quarantined relative paths.
    """
    import time as _time

    root = Path(workspace_path)
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    dest_root = root / ".hiveweave" / "merge-quarantine" / stamp
    moved: list[str] = []
    for rel in files:
        src = root / rel
        if not src.exists():
            continue
        # Only quarantine untracked / not in index
        ok_ls, ls_out = await _git(["ls-files", "--", rel], workspace_path)
        if ok_ls and (ls_out or "").strip():
            continue  # tracked — leave alone
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
            moved.append(rel.replace("\\", "/"))
        except OSError as e:
            log.warning(
                "git_worktree.quarantine_failed",
                path=rel,
                error=str(e),
            )
    if moved:
        log.info(
            "git_worktree.quarantined_untracked",
            count=len(moved),
            dest=str(dest_root),
            files=moved[:12],
        )
    return moved


async def _merge_failure_result(
    *,
    workspace_path: str,
    branch: str,
    target_branch: str,
    merge_out: str,
    branch_files: list[str],
    short_id: str = "",
    auto_quarantine: bool = True,
) -> dict | None:
    """Classify merge failure. May quarantine untracked and return None to retry.

    Returns a failure dict, or ``None`` when caller should retry merge once
    after auto-quarantine.
    """
    untracked = parse_untracked_overwrite(merge_out)
    if untracked:
        # Abort any in-progress merge so main is clean
        await _git(["merge", "--abort"], workspace_path)
        if auto_quarantine:
            moved = await quarantine_untracked_on_target(
                workspace_path, untracked
            )
            if moved:
                return None  # signal retry
        from hiveweave.services.worktree_review import (
            format_untracked_on_target_message,
        )

        return {
            "success": False,
            "reason": "untracked_on_target",
            "message": format_untracked_on_target_message(
                branch=branch,
                target=target_branch,
                untracked=untracked,
            ),
            "untracked": untracked,
            "conflicts": [],
            "branch": branch,
            "files": branch_files,
            "short_id": short_id,
        }

    ok_diff, diff_out = await _git(
        ["diff", "--name-only", "--diff-filter=U"], workspace_path
    )
    conflict_files = [
        f.strip() for f in (diff_out or "").split("\n") if f.strip()
    ] if ok_diff else []
    await _git(["merge", "--abort"], workspace_path)

    from hiveweave.services.worktree_review import format_merge_conflict_message

    if conflict_files:
        return {
            "success": False,
            "reason": "merge_conflict",
            "message": format_merge_conflict_message(
                branch=branch,
                target=target_branch,
                conflicts=conflict_files,
            ),
            "conflicts": conflict_files,
            "branch": branch,
            "files": branch_files,
            "short_id": short_id,
        }

    # Not a content conflict — surface raw git output; do NOT fake conflicts
    # from branch_files (that caused "same commit" false conflict loops).
    return {
        "success": False,
        "reason": "merge_failed",
        "message": (
            f"Merge of {branch} into {target_branch} failed "
            f"(not a content conflict):\n{(merge_out or '')[:800]}\n\n"
            "Do NOT ask the executor to 'fix merge conflict in worktree' "
            "unless conflicted files are listed. Inspect main hygiene "
            "(untracked / local edits) and retry."
        ),
        "conflicts": [],
        "branch": branch,
        "files": branch_files,
        "short_id": short_id,
    }
