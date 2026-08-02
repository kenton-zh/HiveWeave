"""VERIFY nudge / merge-conflict helpers.

Split from tools/tasks/verify.py. Behavior unchanged.
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog

from hiveweave.services import task as _task_svc
from hiveweave.tools import helpers as _helpers
from hiveweave.tools.tasks.verify_spawn import (
    VERIFY_STALE_COOLDOWN_MS,
    VERIFY_STALE_MS,
    _nudge_one_verify_task,
    _spawn_post_approve_verify_task,
    _stale_verify_cooldowns,
)

log = structlog.get_logger(__name__)

def parse_short_id_from_branch(branch_name: str) -> str | None:
    b = (branch_name or "").strip()
    if b.startswith("hw/"):
        parts = b.split("/")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    if b and len(b) <= 8 and b[0].upper() == "A" and b.isalnum():
        return b.upper() if b[0].isupper() else ("A" + b[1:])
    return None


async def resolve_agent_id_by_short_id(
    project_id: str, short_id: str
) -> str | None:
    from hiveweave.services.org import OrgService

    agents = await OrgService().list_agents(project_id)
    sid = (short_id or "").strip().upper()
    for a in agents:
        if (a.get("short_id") or "").upper() == sid:
            return a.get("id")
    return None


async def rework_tasks_after_merge_conflict(
    project_id: str,
    from_agent_id: str,
    *,
    merged_short_id: str | None = None,
    merged_branch: str | None = None,
    conflicts: list[str] | None = None,
    merged_files: list[str] | None = None,
) -> int:
    """On merge conflict: rework scoped approved tasks; wake executor in worktree."""
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.worktree_review import select_tasks_for_merged_work
    from hiveweave.agents.trigger import trigger_subordinate

    ts = _task_svc.TaskService()
    short = merged_short_id or (
        parse_short_id_from_branch(merged_branch or "") if merged_branch else None
    )
    agent_id = None
    if short:
        agent_id = await resolve_agent_id_by_short_id(project_id, short)
    if not agent_id:
        return 0

    tasks = await ts.list_tasks(project_id)
    selected = select_tasks_for_merged_work(
        tasks,
        assignee_id=agent_id,
        merged_files=merged_files or conflicts,
        statuses=("approved",),
    )
    files = ", ".join((conflicts or merged_files or [])[:12]) or "(unknown)"
    feedback = (
        f"[MERGE CONFLICT] Main merge aborted. In YOUR worktree, merge or "
        f"rebase main into your branch, resolve: {files}. "
        f"Then checkpoint and re-submit. Do NOT edit main."
    )
    inbox = InboxService()
    count = 0
    for t in selected:
        tid = t.get("id")
        if not tid:
            continue
        try:
            await ts.review_task(
                project_id,
                tid,
                "rework",
                feedback,
                reason_code="merge_conflict_rework",
            )
        except Exception as e:
            log.warning("merge_conflict_rework_task_failed", task_id=tid, error=str(e))
            continue
        try:
            await inbox.send_message(
                from_agent_id=from_agent_id,
                to_agent_id=agent_id,
                message=(
                    f"[REWORK REQUESTED] [reason_code=merge_conflict_rework] "
                    f"{feedback}"
                ),
                message_type="task",
                priority="urgent",
                task_id=tid,
            )
            await trigger_subordinate(agent_id)
        except Exception as e:
            log.warning("merge_conflict_inbox_failed", error=str(e))
        count += 1
    if count:
        log.info(
            "merge_conflict_tasks_reworked",
            project_id=project_id,
            assignee_id=agent_id,
            count=count,
        )
    return count


async def _stamp_merge_fact_on_parent_tasks(
    project_id: str,
    tasks: list[dict],
    *,
    merged_by: str,
    merge_commit: str | None,
) -> None:
    """Persist merge machine facts on parent tasks after a real merge."""
    import time

    from hiveweave.services.task import _execute

    now_ms = int(time.time() * 1000)
    for t in tasks:
        tid = t.get("id")
        if not tid:
            continue
        ev = t.get("evidence") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except Exception:
                ev = {}
        if not isinstance(ev, dict):
            ev = {}
        ev = dict(ev)
        ev["merged_by"] = merged_by
        if merge_commit:
            ev["merge_commit"] = str(merge_commit)
        ev["merged_at"] = now_ms
        await _execute(
            project_id,
            "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
            [json.dumps(ev), now_ms, tid],
        )


async def nudge_verify_tasks_after_merge(
    project_id: str,
    from_agent_id: str,
    *,
    merged_short_id: str | None = None,
    merged_agent_id: str | None = None,
    merged_branch: str | None = None,
    merged_files: list[str] | None = None,
    merge_commit: str | None = None,
) -> int:
    """After successful merge: spawn VERIFY for scoped tasks, then nudge.

    VERIFY is intentionally NOT created at approve time — only here.
    Scope is the merged work (files/branch), not every approved task.
    """
    from hiveweave.services.worktree_review import select_tasks_for_merged_work

    ts = _task_svc.TaskService()
    agent_id = merged_agent_id
    short = merged_short_id or (
        parse_short_id_from_branch(merged_branch or "") if merged_branch else None
    )
    if not agent_id and short:
        agent_id = await resolve_agent_id_by_short_id(project_id, short)

    parent_ids: set[str] = set()
    if agent_id:
        try:
            all_tasks = await ts.list_tasks(project_id)
            selected = select_tasks_for_merged_work(
                all_tasks,
                assignee_id=agent_id,
                merged_files=merged_files,
                statuses=("approved", "verifying"),
            )
            parent_ids = {t["id"] for t in selected if t.get("id")}
            if selected:
                try:
                    await _stamp_merge_fact_on_parent_tasks(
                        project_id,
                        selected,
                        merged_by=from_agent_id,
                        merge_commit=merge_commit,
                    )
                except Exception as stamp_err:
                    log.warning(
                        "merge_fact_stamp_failed",
                        project_id=project_id,
                        error=str(stamp_err),
                    )
            spawned = []
            for t in selected:
                tid = t.get("id")
                try:
                    vid = await _spawn_post_approve_verify_task(
                        ts, project_id, from_agent_id, t
                    )
                    if vid:
                        spawned.append(vid)
                    elif tid:
                        # Spawn returned None without raising — still advance
                        # parent out of bare approved so it cannot stall forever.
                        try:
                            await ts.mark_verifying(project_id, tid)
                        except Exception:
                            pass
                except Exception as spawn_err:
                    log.warning(
                        "verify_spawn_one_failed",
                        parent_id=tid,
                        error=str(spawn_err),
                    )
                    if tid:
                        try:
                            await ts.mark_verifying(project_id, tid)
                        except Exception:
                            pass
            if spawned:
                log.info(
                    "verify_spawned_after_merge",
                    project_id=project_id,
                    assignee_id=agent_id,
                    count=len(spawned),
                    parent_ids=list(parent_ids),
                )
        except Exception as e:
            log.warning("verify_spawn_after_merge_failed", error=str(e))
            raise

    tasks = await ts.list_tasks(project_id)
    nudged = 0
    for t in tasks:
        # TEST19 教训: 只认系统 VERIFY: 前缀（agent 自由 tag verify 不触发）
        if not (t.get("title") or "").startswith("VERIFY:"):
            continue
        if t.get("status") not in ("created", "claimed", "running"):
            continue
        # Only VERIFY children of parents covered by this merge
        if parent_ids:
            if t.get("parent_task_id") not in parent_ids:
                continue
        elif agent_id:
            parent_id = t.get("parent_task_id")
            if parent_id:
                parent = next((p for p in tasks if p.get("id") == parent_id), None)
                if parent and parent.get("assignee_id") not in (None, agent_id):
                    continue
        if await _nudge_one_verify_task(
            project_id, from_agent_id, t, reason="merge"
        ):
            nudged += 1
    if nudged:
        log.info(
            "verify_tasks_nudged_after_merge",
            project_id=project_id,
            count=nudged,
        )
    return nudged


async def nudge_stale_verify_tasks(
    project_id: str,
    *,
    stale_ms: int = VERIFY_STALE_MS,
    now_ms: int | None = None,
) -> int:
    """Nudge VERIFY children stuck under verifying parents past stale_ms.

    Closes the gap when merge nudge never fired: parent stays verifying and
    VERIFY sits in created/claimed with nobody woken.
    """
    import time as _time

    ts = _task_svc.TaskService()
    tasks = await ts.list_tasks(project_id)
    now = now_ms if now_ms is not None else int(_time.time() * 1000)

    verifying_parents = {
        t["id"]
        for t in tasks
        if t.get("status") == "verifying" and not ts._is_verify_task(t)
    }
    if not verifying_parents:
        return 0

    nudged = 0
    for t in tasks:
        if not ts._is_verify_task(t):
            continue
        if t.get("status") not in ("created", "claimed"):
            continue
        parent_id = t.get("parent_task_id")
        if not parent_id or parent_id not in verifying_parents:
            continue
        updated = int(t.get("updated_at") or 0)
        if updated and (now - updated) < stale_ms:
            continue
        tid = t.get("id") or ""
        last = _stale_verify_cooldowns.get(tid, 0)
        if now - last < VERIFY_STALE_COOLDOWN_MS:
            continue
        if await _nudge_one_verify_task(
            project_id, "system", t, reason="stale"
        ):
            _stale_verify_cooldowns[tid] = now
            nudged += 1
            try:
                from hiveweave.services.telemetry import (
                    telemetry,
                    VERIFY_STALE_NUDGE,
                )

                telemetry.emit(
                    VERIFY_STALE_NUDGE,
                    {
                        "project_id": project_id,
                        "verify_task_id": t.get("id"),
                        "parent_task_id": parent_id,
                        "assignee_id": t.get("assignee_id"),
                    },
                )
            except Exception:
                pass

    if nudged:
        log.warning(
            "verify_stale_tasks_nudged",
            project_id=project_id,
            count=nudged,
        )
    return nudged

