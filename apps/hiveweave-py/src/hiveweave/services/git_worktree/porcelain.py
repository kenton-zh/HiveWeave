"""Git status porcelain helpers."""
from __future__ import annotations

from .git_cmd import _git


def _porcelain_tracked_dirty_paths(st_out: str) -> list[str]:
    """Tracked dirty paths outside .hiveweave (normalized, forward slashes).

    Untracked (``??``) and ignored (``!!``) entries are excluded — those are
    handled by merge-quarantine when they would be overwritten.
    """
    paths: list[str] = []
    lines = [ln for ln in (st_out or "").splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        # _git strips whole-output leading whitespace, so the FIRST line may
        # have lost its leading status blank (" M x" → "M x"). Restore it —
        # XY misread is harmless here (path is stripped after XY anyway),
        # and "??"/"!!" never have a space at index 1.
        if i == 0 and len(ln) >= 2 and ln[1] == " " and ln[0] not in (" ", "?", "!"):
            ln = " " + ln
        # porcelain v1: first two chars are XY status
        code = ln[:2] if len(ln) >= 2 else ""
        if code in ("??", "!!"):
            continue  # untracked / ignored — quarantine path owns overwrite cases
        path_part = ln[3:].strip() if len(ln) > 3 else ""
        if not path_part:
            continue
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        norm = path_part.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        if norm.startswith("/"):
            norm = norm[1:]
        if norm == ".hiveweave" or norm.startswith(".hiveweave/"):
            continue
        if norm not in paths:
            paths.append(norm)
    return paths


def _porcelain_tracked_dirty(st_out: str) -> bool:
    """True when porcelain has *tracked* changes outside .hiveweave.

    Untracked (``??``) is NOT treated as main_dirty — those are handled by
    merge-quarantine when they would be overwritten. Rejecting all porcelain
    broke untracked quarantine (TEST20 N4 vs existing quarantine contract).
    """
    return bool(_porcelain_tracked_dirty_paths(st_out))


# Back-compat alias used by older call sites / tests
def _porcelain_non_hiveweave_dirty(st_out: str) -> bool:
    return _porcelain_tracked_dirty(st_out)


async def _target_worktree_is_dirty(workspace_path: str) -> bool:
    """True when target has uncommitted *tracked* changes (not mere untracked)."""
    ok_st, st_out = await _git(["status", "--porcelain"], workspace_path)
    return bool(ok_st and _porcelain_tracked_dirty(st_out or ""))
