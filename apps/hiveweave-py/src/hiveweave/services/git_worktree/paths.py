"""Worktree path helpers."""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

import structlog

from .constants import WORKTREE_DIR, _RELOCATION_SUFFIXES

log = structlog.get_logger(__name__)

# F13（平台修复计划 2026-08-30）：嵌套假路径清扫 ——
# ``…\worktrees\A298\.hiveweave\worktrees\A299\…`` 这类「已在 worktree
# 内却再次相对拼接 .hiveweave/worktrees/*」的路径（r4：A298→A299 双拼）。
_WORKTREE_SEG_RE = re.compile(
    re.escape(WORKTREE_DIR.replace("/", "\\/")) + r"[\\/]+", re.IGNORECASE
)


def normalize_worktree_root(path: str) -> str:
    """把已处于 worktree 内的路径归一化到**项目根**的 worktree 字典。

    输入如 ``<proj>/.hiveweave/worktrees/A298/.hiveweave/worktrees/A299``：
    取第一个 ``.hiveweave/worktrees/`` 段之前的项目根，清掉后续所有
    嵌套段，得到 ``<proj>/.hiveweave/worktrees/A299``。禁止在已位于
    worktree 内的 cwd 上再次相对拼接（r4 根因）。
    """
    if not path:
        return path
    p = str(path)
    if "." not in p.lower():  # 极快路径：无 .hiveweave 段则原样
        return p
    parts = p.replace("/", "\\").split("\\")
    # 找到第一个 `.hiveweave\\worktrees` 段：从项目根重建
    for i, part in enumerate(parts):
        if part.lower() == ".hiveweave" and i + 1 < len(parts):
            if parts[i + 1].lower() == "worktrees":
                joined = "\\".join(parts[: i + 2])
                # 取后续第一个「非 .hiveweave 段」作为 worktree 名起点
                # （A298\.hiveweave\worktrees\A299 → 保留 A299）
                tail = parts[i + 2:]
                kept = [x for x in tail if x and x.lower() != ".hiveweave"][:1]
                if kept:
                    return Path(joined) / kept[0]
                return Path(joined)
    return p


def _worktree_path(workspace_path: str, short_id: str) -> str:
    # F13：先归一化 —— 若 workspace_path 已在 worktree 内（嵌套拼接的
    # 双份 .hiveweave/worktrees），剥回项目根再拼接，杜绝 A298→A299 假路径。
    root = normalize_worktree_root(workspace_path)
    return str(Path(root) / WORKTREE_DIR / short_id)


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
