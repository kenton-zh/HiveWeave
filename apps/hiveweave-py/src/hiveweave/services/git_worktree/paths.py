"""Worktree path helpers."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import structlog

from .constants import WORKTREE_DIR, _RELOCATION_SUFFIXES

log = structlog.get_logger(__name__)

def _worktree_path(workspace_path: str, short_id: str) -> str:
    return str(Path(workspace_path) / WORKTREE_DIR / short_id)


def _is_bound_worktree_basename(basename: str, short_id: str) -> bool:
    """True if dirname is this agent's canonical tree or an explicit -b/-c/-d relocate.

    Exact short_id OR ``short_id + '-b'|'-c'|'-d'`` only — never substring
    matching (that revived the A003-b-masquerades-as-A003 split).
    """
    if not basename or not short_id:
        return False
    if basename == short_id:
        return True
    return basename in {f"{short_id}{s}" for s in _RELOCATION_SUFFIXES}


def _worktree_binding_under_project(db_path: str, workspace_path: str) -> bool:
    """True if ``db_path`` resolves under this project's ``.hiveweave/worktrees/``.

    Hard sandbox for prefer-DB / heal: rejects cross-project short_id collisions
    and hand-edited absolute paths that escape the project worktree root.
    """
    if not db_path or not workspace_path:
        return False
    try:
        root = (Path(workspace_path) / WORKTREE_DIR).resolve()
        resolved = Path(db_path).resolve()
        resolved.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _has_git(path: str) -> bool:
    return (Path(path) / ".git").exists()


def _force_clear_path(path: str) -> bool:
    """Remove a path so ``git worktree add`` can reuse it.

    Windows often leaves a non-git husk (e.g. only ``node_modules``) that
    ``rmtree(..., ignore_errors=True)`` cannot fully delete while files are
    locked — then ``worktree add`` fails with "already exists" and we stamp
    a false ``worktree_error``. Rename-aside lets add proceed.
    """
    p = Path(path)
    if not p.exists():
        return True
    shutil.rmtree(path, ignore_errors=True)
    if not p.exists():
        return True
    try:
        dest = p.parent / f".stale-{p.name}-{int(time.time() * 1000)}"
        p.rename(dest)
        shutil.rmtree(dest, ignore_errors=True)
        log.info(
            "git_worktree.stale_path_moved_aside",
            path=path,
            aside=str(dest),
            aside_gone=not dest.exists(),
        )
    except Exception as e:
        log.warning("git_worktree.force_clear_failed", path=path, error=str(e))
    return not Path(path).exists()
