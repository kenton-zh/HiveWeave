"""Conflict-marker scan after merge."""
from __future__ import annotations

import os
from pathlib import Path

import structlog

from .constants import (
    HIVEWEAVE_DIR,
    PRIVATE_WS_DIRS,
    TRACKED_WS_DIRS,
    _CONFLICT_MARKER_RE,
    _MARKER_SCAN_MAX_BYTES,
    _MARKER_SCAN_MAX_HITS,
    _MARKER_SCAN_SKIP_DIRS,
)
from .git_cmd import _git
from .merge_support import _abort_landed_merge

log = structlog.get_logger(__name__)

def scan_conflict_markers(
    root: str, paths: list[str] | None = None
) -> list[str]:
    """Scan for unresolved git conflict markers (merge 后残留检测).

    行首锚定 ``<<<<<<<`` / ``>>>>>>>``。只扫文本文件 — 跳过
    .git/.hiveweave 私有子目录(worktrees/tool_outputs/db, but NOT the
    tracked workspace dirs)/node_modules/dist/build 等目录、含 NUL 字节的
    二进制文件、以及 >1MB 的大文件。返回 POSIX 风格相对路径的排序列表。

    If *paths* is provided, only those relative paths are checked (the files
    touched by this merge). Full-tree walks mis-fire on docs/fixtures that
    contain marker samples and are unrelated to the landed merge.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return []

    # Tracked workspace dirs (shared/reports/drafts/handoffs) are scannable;
    # private dirs (worktrees/tool_outputs/logs/db) keep being skipped even
    # though they live under .hiveweave.
    def _is_private_dir(dirname: str, parent_rel: Path) -> bool:
        if dirname in _MARKER_SCAN_SKIP_DIRS and dirname != HIVEWEAVE_DIR:
            return True
        if parent_rel.as_posix() == HIVEWEAVE_DIR:
            tracked_basenames = {d.rsplit("/", 1)[-1] for d in TRACKED_WS_DIRS}
            return dirname not in tracked_basenames
        return False

    def _check_file(fpath: Path) -> bool:
        try:
            if fpath.stat().st_size > _MARKER_SCAN_MAX_BYTES:
                return False
            raw = fpath.read_bytes()
        except OSError:
            return False
        if b"\x00" in raw[:8192]:
            return False
        return bool(
            _CONFLICT_MARKER_RE.search(raw.decode("utf-8", errors="replace"))
        )

    hits: list[str] = []
    if paths is not None:
        for rel in paths:
            if len(hits) >= _MARKER_SCAN_MAX_HITS:
                break
            norm = (rel or "").strip().replace("\\", "/")
            if not norm:
                continue
            fpath = root_path / norm
            if not fpath.is_file():
                continue
            if _check_file(fpath):
                hits.append(Path(norm).as_posix())
        return sorted(hits)

    for dirpath, dirnames, filenames in os.walk(root_path):
        parent_rel = (Path(dirpath).relative_to(root_path)
                      if dirpath != str(root_path) else Path("."))
        dirnames[:] = [
            d for d in dirnames if not _is_private_dir(d, parent_rel)
        ]
        for name in filenames:
            if len(hits) >= _MARKER_SCAN_MAX_HITS:
                return sorted(hits)
            fpath = Path(dirpath) / name
            if _check_file(fpath):
                hits.append(fpath.relative_to(root_path).as_posix())
    return sorted(hits)


async def _reject_if_markers_landed(
    workspace_path: str,
    *,
    short_id: str,
    branch: str,
    target_branch: str,
    branch_files: list[str],
) -> dict | None:
    """After a successful git merge, abort if conflict markers landed.

    Must run BEFORE worktree/branch cleanup — otherwise abort leaves the
    delivery branch deleted (audit T1#1). Scopes scan to *branch_files*
    when non-empty so pre-existing marker samples elsewhere on main do not
    false-trigger.
    """
    scan_paths: list[str] | None = list(branch_files) if branch_files else None
    if not scan_paths:
        # Prefer recovering merge-touched files over full-tree walk (fixtures
        # with marker samples false-abort). Fail closed if still empty.
        ok_dt, out_dt = await _git(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "HEAD"],
            workspace_path,
        )
        if ok_dt and (out_dt or "").strip():
            scan_paths = [
                ln.strip().replace("\\", "/")
                for ln in out_dt.splitlines()
                if ln.strip()
            ]
        else:
            # 39 轮审计 P0-2：零差异 merge（branch 已合入 main，branch_files 与
            # diff-tree -m 双空）此前被 fail-closed 报「Merge aborted」——但 merge
            # commit 已落地，出现「重试→No-op→再重试」×3（178K tok）。修复：报失败
            # 前先查 is-ancestor，已合入 → 返回 no-op 成功（merged_already 事实位），
            # 调用方走 already_up_to_date 既有路径（义务结算/自动 submit 已覆盖）。
            # 扫描跳过是安全的：diff-tree 全空说明没有 merge 引入的文件可扫。
            ok_anc, _ = await _git(
                ["merge-base", "--is-ancestor", branch, target_branch],
                workspace_path,
            )
            if ok_anc:
                log.info(
                    "git_worktree.merge_zero_diff_noop",
                    short_id=short_id,
                    target=target_branch,
                    branch=branch,
                )
                return {
                    "success": True,
                    "already_up_to_date": True,
                    "merged_already": True,
                    "reason": "no_op_zero_diff",
                    "conflicts": [],
                    "message": (
                        f"No-op merge: branch already merged into {target_branch} "
                        "(zero diff after merge) — conflict scan not needed."
                    ),
                    "branch": branch,
                    "files": branch_files,
                    "short_id": short_id,
                }
            return {
                "success": False,
                "reason": "conflict_scan_unscoped",
                "conflicts": [],
                "message": (
                    f"Merge aborted: cannot scope conflict-marker scan "
                    f"(empty file list for {branch} → {target_branch}). "
                    "Retry merge after ensuring git diff-tree works."
                ),
                "branch": branch,
                "files": branch_files,
                "short_id": short_id,
            }
    marker_files = scan_conflict_markers(workspace_path, paths=scan_paths)
    if not marker_files:
        return None
    log.warning(
        "git_worktree.merge_markers_found",
        short_id=short_id,
        target=target_branch,
        files=marker_files[:10],
    )
    aborted = await _abort_landed_merge(workspace_path)
    if not aborted:
        log.error(
            "git_worktree.merge_marker_abort_failed",
            short_id=short_id,
            target=target_branch,
        )
    return {
        "success": False,
        "reason": "conflict_markers_landed",
        "conflicts": marker_files,
        "message": (
            f"Merge aborted: conflict markers landed on {target_branch}: "
            f"{', '.join(marker_files[:8])}. "
            f"Remove markers in the worktree branch and retry merge."
        ),
        "branch": branch,
        "files": branch_files,
        "short_id": short_id,
    }
