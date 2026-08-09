"""Git status porcelain helpers."""
from __future__ import annotations

import re

from .constants import TRACKED_WS_DIRS
from .git_cmd import _git

# porcelain -z record: two status chars, one separator space, then path.
# Recognized codes are letters or blanks (e.g. " M" worktree-only modify).
# A rename emits TWO records: "R  new\0old\0" — the second is a bare path
# with NO code prefix, which must not be misread as (code, path).
_Z_RECORD_RE = re.compile(r"^(.{2}) (.+)$")


def _is_private_hiveweave_path(norm: str) -> bool:
    """True when *norm* is a .hiveweave path outside the tracked workspace dirs.

    Tracked workspace dirs (shared/reports/drafts/handoffs live on main and
    must be visible to the dirty gate; everything else under .hiveweave —
    worktrees checkouts, tool_outputs, logs, data.db — is platform-private and
    never dirty-checks.
    """
    return not _in_tracked_ws_dir(norm) and (
        norm == ".hiveweave" or norm.startswith(".hiveweave/")
    )


def _in_tracked_ws_dir(norm: str) -> bool:
    """True when *norm* is (or is inside) a tracked workspace dir."""
    n = (norm or "").replace("\\", "/")
    return any(n == d or n.startswith(d + "/") for d in TRACKED_WS_DIRS)


def _split_status_z(raw: str) -> list[tuple[str, str]]:
    """Split ``status --porcelain -z`` output into ``(XY_code, path)`` pairs.

    NUL-separated records; each record looks like ``XY path`` (the -z flag
    disables c-quoting, so non-ASCII/space paths arrive unquoted; renames
    become ``R  new\0old\0`` — two records sharing the same R code).
    """
    out: list[tuple[str, str]] = []
    for idx, rec in enumerate((raw or "").split("\x00")):
        rec = rec.rstrip("\n")
        # _git strips whole-output leading whitespace, so the FIRST record
        # may have lost its XY-code leading blank (" M x" → "M x") — restore
        # it or the record below fails the pattern and the dirty path shifts
        # by one char (losing its leading "."). The restore fires only when
        # that blank was actually eaten: a letter-X code (rename/staged, e.g.
        # "R  b.txt") never had a leading blank, and its separator is at
        # rec[2] — a stripped " M x" record has its (former) separator eaten
        # too, leaving "M x" with a real path char at rec[2] instead.
        if (
            idx == 0
            and len(rec) >= 3
            and rec[0] not in (" ", "?", "!")
            and rec[1] == " "
            and rec[2] != " "
        ):
            rec = " " + rec
        m = _Z_RECORD_RE.match(rec)
        if not m:
            continue  # rename's old-path second record (bare path, no XY)
        code = m.group(1)
        path = m.group(2)
        if path:
            out.append((code, path))
    return out


def _porcelain_tracked_dirty_paths(st_out: str) -> list[str]:
    """Tracked dirty paths outside platform-private .hiveweave (normalized).

    Expects ``git status --porcelain -z`` output (NUL-separated, unquoted
    paths). Untracked (``??``) and ignored (``!!``) entries are excluded —
    those are handled by merge-quarantine when they would be overwritten.
    """
    paths: list[str] = []
    for code, path in _split_status_z(st_out):
        if code in ("??", "!!"):
            continue  # untracked / ignored — quarantine path owns overwrite cases
        if code[:1] not in ("M", "A", "D", "R", "C", " ", "T", "U"):
            continue  # codes like "!!"/"??" already handled; be defensive
        norm = path.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        if norm.startswith("/"):
            norm = norm[1:]
        if _is_private_hiveweave_path(norm):
            continue
        if norm not in paths:
            paths.append(norm)
    return paths


def _porcelain_tracked_dirty(st_out: str) -> bool:
    """True when porcelain has *tracked* changes outside private .hiveweave.

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
    ok_st, st_out = await _git(
        ["-c", "core.quotepath=false", "status", "--porcelain", "-z"],
        workspace_path,
    )
    return bool(ok_st and _porcelain_tracked_dirty(st_out or ""))