"""Worktree-aware review/merge helpers — avoid main-vs-worktree dual reality.

P0 contract (human-aligned):
- Review against assignee worktree, not main-only view.
- Merge conflicts: abort on main, executor rebases/merges main *in their worktree*.
- VERIFY is post-merge and scoped to the merged work, not every approved task.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.org import OrgService

log = structlog.get_logger(__name__)

# Strip accidental worktree prefixes from evidence.files_changed (TEST4).
# Matches: .hiveweave/worktrees/<sid>/, hiveweave/worktrees/<sid>/,
# and absolute paths containing those segments.
_WORKTREE_PREFIX_RE = re.compile(
    r"(?:^|/)\.?hiveweave/worktrees/[^/]+/",
    re.IGNORECASE,
)
_BARE_WORKTREE_PREFIX_RE = re.compile(
    r"(?:^|/)worktrees/[A-Za-z0-9_-]+/",
)


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


def normalize_evidence_path(path: str | Any) -> str:
    """Normalize a claimed file path to repo-relative (main) form.

    Executors often pass worktree-relative or absolute worktree paths
    (e.g. ``.hiveweave/worktrees/A004/module_a.py``). Approve compares
    against the worktree checkout using repo-relative names.
    """
    s = str(path or "").replace("\\", "/").strip()
    if not s:
        return ""
    # Strip only "./" / leading "/", NOT every "." — lstrip("./") wrongly
    # turns ".editorconfig" into "editorconfig" (TEST5 approve miss).
    while s.startswith("./"):
        s = s[2:]
    s = s.lstrip("/")
    m = _WORKTREE_PREFIX_RE.search(s)
    if m:
        s = s[m.end() :]
    else:
        m2 = _BARE_WORKTREE_PREFIX_RE.search(s)
        if m2:
            s = s[m2.end() :]
    return s.lstrip("/")


def normalize_files_changed(files: list[Any] | None) -> list[str]:
    """Normalize + dedupe files_changed while preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for f in files or []:
        s = normalize_evidence_path(f)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _rel_paths(files: list[Any]) -> list[str]:
    return normalize_files_changed(list(files or []))


def _norm_set(files: list[Any] | None) -> set[str]:
    return set(_rel_paths(list(files or [])))


async def worktree_commits_ahead(
    main_ws: str, worktree_ws: str, *, target_branch: str = "main"
) -> int | None:
    """How many commits worktree HEAD is ahead of target branch tip.

    ``0`` → already on main (pure verification / already merged).
    ``None`` → could not determine (git error).
    """
    try:
        from hiveweave.services.git_worktree import _git

        ok_m, main_tip = await _git(
            ["rev-parse", target_branch], main_ws
        )
        if not ok_m or not (main_tip or "").strip():
            return None
        ok_w, wt_tip = await _git(["rev-parse", "HEAD"], worktree_ws)
        if not ok_w or not (wt_tip or "").strip():
            return None
        if main_tip.strip() == wt_tip.strip():
            return 0
        ok_c, count_out = await _git(
            ["rev-list", "--count", f"{main_tip.strip()}..HEAD"],
            worktree_ws,
        )
        if not ok_c:
            return None
        return int((count_out or "0").strip() or "0")
    except Exception as e:
        log.warning("worktree_commits_ahead_failed", error=str(e))
        return None


def compare_worktree_to_main(
    *,
    main_ws: str,
    worktree_ws: str,
    files_changed: list[Any] | None,
    allow_empty_files: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    """Return (deny_reason, meta) for review against assignee worktree.

    Deny when:
    - files_changed is empty AND allow_empty_files is False
      (no proof of what to review for a code-changing task)
    - any claimed file is missing in worktree
    - any claimed file is identical to main (when a list was provided)
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
        if allow_empty_files:
            meta["skipped"] = "empty_files_changed_allowed"
            return None, meta
        return (
            "Approve blocked: evidence.files_changed is empty. "
            "List the paths you reviewed in the assignee worktree "
            "(not main). Without that list there is no worktree proof. "
            "Pure verification / no-code tasks: submit with attestation "
            "and empty files_changed only when the worktree has 0 commits "
            "ahead of main (or tag the task VERIFY).",
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


def _is_no_code_evidence(evidence: dict[str, Any]) -> bool:
    """Explicit no-code / verification-only delivery flags on evidence."""
    for key in (
        "no_code_change",
        "noCodeChange",
        "verification_only",
        "verificationOnly",
    ):
        if evidence.get(key) is True:
            return True
    return False


async def review_worktree_gate(
    project_id: str,
    task: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Hard gate for approve: code tasks need worktree proof; verify/no-diff do not.

    Pure verification (VERIFY: title/tag, or 0 commits ahead of main with empty
    files_changed) must be approvable — otherwise CEO can only cancel.
    """
    from hiveweave.services.task import TaskService

    assignee = task.get("assignee_id")
    main_ws = await project_main_workspace(project_id)
    if not main_ws:
        return None, {}
    if not assignee:
        return None, {"mainWorkspace": main_ws}

    meta: dict[str, Any] = {
        "mainWorkspace": main_ws,
        "assigneeId": assignee,
    }

    # VERIFY child / tagged verify: delivery is attestation/script, not a diff
    if TaskService._is_verify_task(task):
        meta["skipped"] = "verify_task"
        return None, meta

    if _is_no_code_evidence(evidence):
        meta["skipped"] = "no_code_change_flag"
        return None, meta

    wt = await agent_worktree_path(str(assignee))
    meta["worktreeWorkspace"] = wt
    if not wt:
        return (
            "Approve blocked: assignee has no worktree path. "
            "Executor worktrees are created on hire/dispatch — re-dispatch "
            "or wait for worktree heal, then review that tree (not main).",
            meta,
        )

    files = evidence.get("files_changed") or evidence.get("filesChanged") or []
    ahead = await worktree_commits_ahead(main_ws, wt)
    meta["commitsAhead"] = ahead

    # Empty files_changed + worktree already on main → verification-only OK
    allow_empty = False
    if not _rel_paths(list(files or [])):
        if ahead == 0:
            allow_empty = True
            meta["skipped"] = "zero_commits_ahead"
        elif ahead is None:
            # Can't measure — still allow empty when attestation-backed
            # caller already passed attestation gate before this.
            aids = evidence.get("attestation_ids") or evidence.get(
                "attestationIds"
            )
            if isinstance(aids, list) and aids:
                allow_empty = True
                meta["skipped"] = "attestation_only_unknown_ahead"

    deny, cmp_meta = compare_worktree_to_main(
        main_ws=main_ws,
        worktree_ws=wt,
        files_changed=files,
        allow_empty_files=allow_empty,
    )
    meta.update(cmp_meta)
    # Prefer the more specific skip reason over compare's generic flag
    if allow_empty and ahead == 0:
        meta["skipped"] = "zero_commits_ahead"
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

# Untracked on MAIN is a coordinator/main hygiene issue — NOT executor rework
UNTRACKED_ON_TARGET_HINT = (
    "[UNTRACKED ON MAIN — NOT A MERGE CONFLICT] "
    "Main has untracked files that would be overwritten by the merge. "
    "This is NOT an executor worktree problem — do NOT rework the assignee "
    "and do NOT ask them to 'fix it in the worktree'. "
    "Coordinator: quarantine/remove those untracked files on MAIN "
    "(or let git_worktree_merge auto-quarantine), then retry merge. "
    "If branch tip already equals main tip, merge is a no-op after cleanup."
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


def format_untracked_on_target_message(
    *,
    branch: str,
    target: str,
    untracked: list[str] | None,
) -> str:
    files = ", ".join((untracked or [])[:12]) or "(unknown)"
    return (
        f"Merge blocked for {branch} into {target}: untracked files on "
        f"{target} would be overwritten: {files}.\n\n{UNTRACKED_ON_TARGET_HINT}"
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
    - Still ambiguous (same assignee, no file overlap) → **all** of them
      (same worktree merge covers that assignee's approved work; do not
      silently drop siblings with ``[:1]``)
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

    # Same assignee — this worktree merge covers all their approved tasks
    candidates.sort(key=lambda x: int(x.get("updated_at") or 0), reverse=True)
    return candidates
