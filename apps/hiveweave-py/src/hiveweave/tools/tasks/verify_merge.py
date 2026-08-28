"""VERIFY nudge / merge-conflict helpers.

Split from tools/tasks/verify.py. Behavior unchanged.
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog

from hiveweave.services import task as _task_svc
from hiveweave.services.tasks.verify import is_verify_title
from hiveweave.tools import helpers as _helpers
from hiveweave.tools.tasks.verify_spawn import (
    VERIFY_STALE_COOLDOWN_MS,
    VERIFY_STALE_MS,
    _nudge_one_verify_task,
    _stale_verify_cooldowns,
    nudge_pending_verify_tasks,
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
    merged_files: list[str] | None = None,
    target_branch: str | None = None,
) -> None:
    """Persist merge machine facts on parent tasks after a real merge.

    T4.4: 每个 parent task 同步落一条 ``task.merged`` 事件（此前只有裸
    ``UPDATE tasks SET evidence``，零事件零通知 —— 下游只能靠反复跑
    「核验主分支落地状态」的 run 确认，等待—超时—重建循环的根源之一）。
    回执内容：commit hash + 涉及文件 + 目标分支。
    """
    import time

    from hiveweave.services.task import _execute
    from hiveweave.services.tasks.db import insert_task_event

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
        # Issue #5 / s3-clone_01: scope-aware VERIFY baseline reads
        # parent evidence.files_changed. If submit left it empty, stamp
        # this merge's paths so an unrelated later tip does not force a
        # full re-E2E.
        if merged_files:
            existing = ev.get("files_changed") or ev.get("filesChanged")
            if not (isinstance(existing, list) and existing):
                from hiveweave.services.worktree_review import (
                    normalize_files_changed,
                )

                ev["files_changed"] = normalize_files_changed(list(merged_files))
        t["evidence"] = ev
        await _execute(
            project_id,
            "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
            [json.dumps(ev), now_ms, tid],
        )
        # T4.4: 事件 + lobby publish（relay 依 event_type 转 inbox）。
        try:
            await insert_task_event(
                project_id,
                str(tid),
                "task.merged",
                from_status=None,
                to_status=None,
                actor_id=str(merged_by) if merged_by else None,
                payload={
                    "merge_commit": str(merge_commit or ""),
                    "files": list(merged_files or [])[:20],
                    "files_total": len(merged_files or []),
                    "target_branch": str(target_branch or "main"),
                },
                now_ms=now_ms,
            )
        except Exception as e:
            log.warning(
                "task_merged_event_failed",
                project_id=project_id,
                task_id=str(tid)[:12],
                error=str(e),
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
    target_branch: str | None = None,
) -> int:
    """After successful merge: stamp merge facts, then nudge existing VERIFY.

    Leaf merges do **not** auto-spawn VERIFY. Coordinators dispatch one MAIN
    milestone QA task (``milestoneVerify=true``). Existing VERIFY rows that
    belong to this merge scope may still be nudged (serial lock).
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
    selected: list = []  # agent_id 缺失（dismissed/legacy 分支）时保持空，避免 UnboundLocalError
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
                        merged_files=merged_files,
                        target_branch=target_branch,
                    )
                except Exception as stamp_err:
                    log.warning(
                        "merge_fact_stamp_failed",
                        project_id=project_id,
                        error=str(stamp_err),
                    )
        except Exception as e:
            log.warning("verify_nudge_after_merge_failed", error=str(e))
            raise

    tasks = await ts.list_tasks(project_id)
    nudged = 0
    # 公平性（审计可选）：验收串行化下至多唤醒一个，按 created_at 最老优先。
    for t in sorted(
        tasks,
        key=lambda x: (x.get("created_at") or 0, x.get("id") or ""),
    ):
        # TEST19 教训: 只认系统 VERIFY: 前缀（agent 自由 tag verify 不触发）；
        # H1 收口: 判定统一走 is_verify_title（覆盖 【】/[]/全角冒号形态）。
        if not is_verify_title(t.get("title")):
            continue
        # 审计 O2：只剩 created/claimed —— running 的 VERIFY 已在跑，merge
        # nudge 不得再骚扰（它会被 except_id 自豁免而重复 send+trigger）。
        if t.get("status") not in ("created", "claimed"):
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
    # 合并义务收口：approved 父任务若已有 VERIFY 子任务（预置 VERIFY —— 中层在
    # 合 MAIN 前就派了里程碑 QA），merge 落地后必须把父任务 approved → verifying。
    # 否则父任务停在 approved，creator 的 CREATOR_MUST_MERGE 义务永不消除（分支
    # 已拆、无法再 merge），creator 只能被迫 waive_merge。mark_verifying 同时清掉
    # 该任务的 [MERGE PENDING] 收件箱（_clear_merge_pending_inbox）。
    for t in selected:
        tid = t.get("id")
        if not tid or t.get("status") != "approved":
            continue
        has_verify_child = any(
            (c.get("parent_task_id") == tid)
            and is_verify_title(c.get("title"))
            # 在途 VERIFY 才算（对齐 verify_spawn._spawn_post_approve_verify_task）：
            # 已 closed/approved 的子任务不再阻止父任务收口。
            and c.get("status") not in ("closed", "approved")
            for c in tasks
        )
        if not has_verify_child:
            continue
        try:
            await ts.mark_verifying(project_id, tid, reason_code="merged")
        except Exception as e:
            log.warning(
                "merge_parent_mark_verifying_failed",
                task_id=tid,
                error=str(e),
            )
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

