"""Worktree-aware review/merge helpers — avoid main-vs-worktree dual reality.

P0 contract (human-aligned):
- Review against assignee worktree, not main-only view.
- Merge conflicts: abort on main, executor rebases/merges main *in their worktree*.
- VERIFY is post-merge and scoped to the merged work, not every approved task.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.org import OrgService

log = structlog.get_logger(__name__)


async def project_main_workspace(project_id: str) -> str | None:
    return await meta_db.get_project_workspace(project_id)


async def agent_worktree_path(agent_id: str) -> str | None:
    """Return agents.workspace_path if it looks like a live worktree dir."""
    row = await meta_db.get_agent_by_id(agent_id)
    if not row:
        try:
            a = await OrgService().resolve_agent(agent_id)
        except Exception:
            a = None
        row = a
    if not row:
        return None
    ws = (row.get("workspace_path") or "").strip()
    if not ws:
        return None
    p = Path(ws)
    if not p.is_dir():
        return None
    # Prefer a real checkout (worktree or repo)
    if (p / ".git").exists() or any(p.iterdir()):
        return str(p)
    return None


def _rel_paths(files: list[Any]) -> list[str]:
    out: list[str] = []
    for f in files or []:
        s = str(f or "").replace("\\", "/").lstrip("./")
        if s:
            out.append(s)
    return out


def _norm_set(files: list[Any] | None) -> set[str]:
    return set(_rel_paths(list(files or [])))


def compare_worktree_to_main(
    *,
    main_ws: str,
    worktree_ws: str,
    files_changed: list[Any] | None,
) -> tuple[str | None, dict[str, Any]]:
    """Return (deny_reason, meta) for review against assignee worktree.

    Deny when:
    - files_changed is empty (no proof of what to review)
    - any claimed file is missing in worktree
    - any claimed file is identical to main
    """
    meta: dict[str, Any] = {
        "mainWorkspace": main_ws,
        "worktreeWorkspace": worktree_ws,
        "checkedFiles": [],
        "divergedFiles": [],
        "identicalToMain": [],
        "missingInWorktree": [],
    }
    rels = _rel_paths(list(files_changed or []))
    if not rels:
        return (
            "Approve blocked: evidence.files_changed is empty. "
            "List the paths you reviewed in the assignee worktree "
            "(not main). Without that list there is no worktree proof.",
            meta,
        )

    main_root = Path(main_ws)
    wt_root = Path(worktree_ws)
    for rel in rels[:40]:
        meta["checkedFiles"].append(rel)
        wt_f = wt_root / rel
        main_f = main_root / rel
        if not wt_f.is_file():
            meta["missingInWorktree"].append(rel)
            continue
        if not main_f.is_file():
            meta["divergedFiles"].append(rel)
            continue
        try:
            if wt_f.read_bytes() == main_f.read_bytes():
                meta["identicalToMain"].append(rel)
            else:
                meta["divergedFiles"].append(rel)
        except OSError:
            meta["missingInWorktree"].append(rel)

    if meta["missingInWorktree"]:
        return (
            "Approve blocked: claimed files_changed missing in assignee "
            f"worktree ({worktree_ws}). Review that worktree, not main. "
            f"Missing: {meta['missingInWorktree'][:8]}",
            meta,
        )
    if meta["identicalToMain"]:
        return (
            "Approve blocked: some claimed files_changed are identical to MAIN "
            f"in assignee worktree ({worktree_ws}). "
            f"Identical: {meta['identicalToMain'][:8]}. "
            "Only approve real worktree diffs.",
            meta,
        )
    if not meta["divergedFiles"]:
        return (
            "Approve blocked: no diverged files vs MAIN in assignee worktree.",
            meta,
        )
    return None, meta


async def review_worktree_gate(
    project_id: str,
    task: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Hard gate for approve: evidence must exist in assignee worktree."""
    assignee = task.get("assignee_id")
    main_ws = await project_main_workspace(project_id)
    if not main_ws:
        return None, {}
    if not assignee:
        return None, {"mainWorkspace": main_ws}

    wt = await agent_worktree_path(str(assignee))
    meta: dict[str, Any] = {
        "mainWorkspace": main_ws,
        "worktreeWorkspace": wt,
        "assigneeId": assignee,
    }
    if not wt:
        return (
            "Approve blocked: assignee has no worktree path. "
            "Executor worktrees are created on hire/dispatch — re-dispatch "
            "or wait for worktree heal, then review that tree (not main).",
            meta,
        )

    files = evidence.get("files_changed") or evidence.get("filesChanged") or []
    deny, cmp_meta = compare_worktree_to_main(
        main_ws=main_ws, worktree_ws=wt, files_changed=files
    )
    meta.update(cmp_meta)
    return deny, meta


# Human-aligned conflict ownership (NOT coordinator edit_file on aborted main)
MERGE_CONFLICT_HINT = (
    "[MERGE CONFLICT — EXECUTOR FIXES IN WORKTREE] "
    "Main merge was aborted (no conflict markers left on main). "
    "Coordinator: review_task(decision='rework') on the related task with the "
    "conflict file list — do NOT ask the executor to 'fix merge on main', "
    "and do NOT use bash/git CLI merge yourself. "
    "Executor: in YOUR worktree, merge or rebase main into your branch, "
    "resolve conflicts there, checkpoint, re-submit. "
    "Coordinator then retries git_worktree_merge. "
    "VERIFY is created only after a successful merge."
)

# Back-compat alias for older imports / messages
COORDINATOR_MERGE_OWNERSHIP = MERGE_CONFLICT_HINT


def format_merge_conflict_message(
    *,
    branch: str,
    target: str,
    conflicts: list[str] | None,
) -> str:
    files = ", ".join((conflicts or [])[:12]) or "(unknown)"
    return (
        f"Merge conflict for {branch} into {target}. "
        f"Conflicted files: {files}.\n\n{MERGE_CONFLICT_HINT}"
    )


def select_tasks_for_merged_work(
    tasks: list[dict[str, Any]],
    *,
    assignee_id: str,
    merged_files: list[str] | None = None,
    statuses: tuple[str, ...] = ("approved", "verifying"),
) -> list[dict[str, Any]]:
    """Pick parent tasks that this merge actually covers.

    - One matching approved/verifying task → that one
    - Several → intersect evidence.files_changed with merged_files
    - Still ambiguous → most recently updated only (never all)
    """
    from hiveweave.services.task import TaskService

    ts = TaskService()
    candidates: list[dict[str, Any]] = []
    for t in tasks:
        if ts._is_verify_task(t):
            continue
        if t.get("assignee_id") != assignee_id:
            continue
        if t.get("status") not in statuses:
            continue
        candidates.append(t)

    if not candidates:
        return []
    if len(candidates) == 1:
        return candidates

    merged = _norm_set(merged_files)
    if merged:
        matched: list[dict[str, Any]] = []
        for t in candidates:
            evidence = t.get("evidence") or {}
            if isinstance(evidence, str):
                import json

                try:
                    evidence = json.loads(evidence)
                except Exception:
                    evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            claimed = _norm_set(
                evidence.get("files_changed") or evidence.get("filesChanged")
            )
            if claimed and claimed & merged:
                matched.append(t)
        if matched:
            return matched

    # Ambiguous multi-task: only the newest — avoid VERIFY for unmerged siblings
    candidates.sort(key=lambda x: int(x.get("updated_at") or 0), reverse=True)
    return candidates[:1]
