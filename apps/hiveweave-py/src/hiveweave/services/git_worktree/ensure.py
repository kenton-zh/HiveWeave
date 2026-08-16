"""Ensure / heal writer worktrees."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from .constants import _RELOCATION_SUFFIXES
from .git_cmd import _current_branch, _git, _resolve_base_branch
from .paths import (
    _has_git,
    _is_bound_worktree_basename,
    _worktree_binding_under_project,
    _worktree_path,
)
from .service import GitWorktreeService

log = structlog.get_logger(__name__)

def agent_gets_write_worktree(agent: dict) -> bool:
    """Whether this agent owns an independent write worktree.

    契约（CEO 抽离 + 中层 builder）：executor 与 builder coordinator
    （family=coordinator，含 SOURCE_WRITE）拥有独立 worktree；
    CEO / HR 强制项目根，永不持有写 worktree。
    """
    perm = (agent.get("permission_type") or "").lower()
    if perm == "executor":
        return True
    if perm != "coordinator":
        return False
    from hiveweave.services.policy import infer_role_family

    return infer_role_family(agent) == "coordinator"


async def heal_workspace_binding_from_disk(
    org: Any,
    agent_id: str,
    project_ws: str,
    short_id: str,
    current_ws: str = "",
) -> str | None:
    """Write ``agents.workspace_path`` back when the tree still exists.

    Empty DB + live tree is a recoverable inconsistency (P1-5 used to wipe
    the binding when routing cwd to MAIN). Merge/reconcile need the path;
    wiping it is 堵截. Returns the live path, or None if nothing on disk.
    """
    sid = (short_id or "").strip()
    if not project_ws or not sid:
        return None

    def _live(path: str) -> bool:
        p = (path or "").strip()
        if not p or not _has_git(p):
            return False
        if not _is_bound_worktree_basename(Path(p).name, sid):
            return False
        return _worktree_binding_under_project(p, project_ws)

    if _live(current_ws):
        return current_ws.strip()

    canonical = _worktree_path(project_ws, sid)
    for cand in [canonical, *[canonical + s for s in _RELOCATION_SUFFIXES]]:
        if not _live(cand):
            continue
        try:
            await org.update_agent(
                agent_id,
                {"workspace_path": cand, "worktree_error": None},
            )
        except Exception:
            pass
        log.info(
            "worktree_binding_healed_from_disk",
            agent_id=agent_id,
            short_id=sid,
            path=cand,
        )
        return cand
    return None


async def ensure_executor_worktree(
    project_id: str,
    agent_id: str,
    *,
    task_name: str | None = None,
    task_id: str | None = None,
    force: bool = False,
) -> dict:
    """Ensure a writer (executor / builder coordinator) has a live worktree.

    Refuses CEO/HR — they must not own write worktrees (forced project root).
    Idempotent if a valid worktree is already bound.

    TEST6 evening P1-2 invariant: recreate only when the agent has in-flight
    write tasks (or ``force=True`` for explicit hire/dispatch override).
    Without open tasks, missing trees stay missing — close-GC / heal must
    not fight each other.

    Exception: a *broken binding* (``workspace_path`` set but the dir is
    missing or has no ``.git``) overrides idle-skip so ensure recreates
    instead of leaving a stale path. Empty path after merge GC still skips.
    A healthy ``-b/-c/-d`` relocate that has git is already returned above.

    task_name: DEPRECATED — 保留兼容旧调用方, 不再参与分支命名;
    task_id 驱动 P0 稳定命名 (hw/<sid>/t-<id8>)。

    Returns ``{success, path, short_id, branch?}`` or ``{success: False, message}``.
    """
    from hiveweave.db import meta as meta_db
    from hiveweave.services.org import OrgService
    from hiveweave.services.policy import infer_role_family

    org = OrgService()
    agent = await org.resolve_agent(agent_id)
    if not agent:
        return {"success": False, "message": f"Agent not found: {agent_id}"}

    if not agent_gets_write_worktree(agent):
        perm = (agent.get("permission_type") or "").lower()
        family = infer_role_family(agent)
        return {
            "success": False,
            "message": (
                f"Refusing worktree for {agent.get('short_id')} "
                f"(role_family={family}, permission_type={perm or 'unknown'}). "
                "Only executors and builder coordinators get write worktrees — "
                "CEO/HR work at the project root."
            ),
        }

    short_id = (agent.get("short_id") or "").strip()
    if not short_id:
        return {"success": False, "message": "Agent has no short_id"}

    ws = await meta_db.get_project_workspace(project_id)
    if not ws or not (Path(ws) / ".git").exists():
        return {"success": False, "message": "Project has no git workspace"}

    cur = (agent.get("workspace_path") or "").strip()
    if cur and Path(cur).is_dir() and (Path(cur) / ".git").exists():
        # Accept canonical short_id OR explicit -b/-c/-d relocate already
        # bound in DB. Substring matching is still banned (A003-b ≠ A003).
        # Path must also sit under THIS project's worktree root.
        dir_basename = Path(cur).name
        if (
            _is_bound_worktree_basename(dir_basename, short_id)
            and _worktree_binding_under_project(cur, ws)
        ):
            # 幂等: 透出实际检出的分支, 不按入参重算 (P0 幂等脱钩修复)
            actual = await _current_branch(cur)
            # B7: always clear stale worktree_error when tree is healthy
            # (do not gate on agent dict — DB may have error agent cache missed).
            try:
                await org.update_agent(agent_id, {"worktree_error": None})
            except Exception:
                pass
            return {
                "success": True,
                "path": cur,
                "short_id": short_id,
                "branch": actual,
                "message": (
                    "worktree already bound (relocated)"
                    if dir_basename != short_id
                    else "worktree already bound"
                ),
            }
        # Path exists but not this agent's tree — recreate under correct short_id
        log.warning(
            "worktree_path_mismatch",
            agent_id=agent_id,
            short_id=short_id,
            workspace_path=cur,
        )

    # TEST6 evening P1-2 / audit P0-1: recreate only for real write intent.
    # force=True → explicit override (rare).
    # task_id bypasses the open-tasks gate ONLY for non-VERIFY tasks
    # (dispatch/create/rework). VERIFY also has a task_id but must NOT
    # rebuild a personal write tree — that undoes close-GC and stale-tip
    # verification.
    if not force:
        bypass_open_gate = False
        if task_id:
            try:
                from hiveweave.services.task import TaskService

                trow = await TaskService().get_task(project_id, str(task_id))
                if trow and not TaskService._is_verify_task(trow):
                    bypass_open_gate = True
            except Exception as e:
                log.debug(
                    "worktree_ensure_task_lookup_failed",
                    task_id=task_id,
                    error=str(e),
                )
                bypass_open_gate = False
        if not bypass_open_gate:
            from .reconcile import _assignee_needs_write_worktree

            if not await _assignee_needs_write_worktree(ws, short_id):
                # Broken binding (DB path set, dir missing or no .git)
                # overrides idle-skip. Empty path after merge GC still skips.
                if cur and not _has_git(cur):
                    log.info(
                        "worktree_recreate_broken_binding",
                        agent_id=agent_id,
                        short_id=short_id,
                        project_id=project_id,
                        workspace_path=cur,
                        task_id=task_id,
                    )
                else:
                    log.info(
                        "worktree_recreate_skipped_no_open_tasks",
                        agent_id=agent_id,
                        short_id=short_id,
                        project_id=project_id,
                        task_id=task_id,
                    )
                    return {
                        "success": False,
                        "skipped": True,
                        "short_id": short_id,
                        "message": (
                            "worktree recreate skipped: no in-flight write tasks "
                            f"for {short_id}"
                        ),
                    }

    gwt = GitWorktreeService()
    name = task_name or agent.get("role") or "task"
    result = await gwt.create(ws, short_id, str(name), task_id=task_id)
    if not result.get("success") or not result.get("path"):
        err = result.get("message") or "worktree create failed"
        # BUG-4: race with concurrent create may report failure while the
        # tree is already healthy — re-validate before persisting error.
        # Prefer canonical, then -b/-c/-d (same suffixes create() uses).
        expected = _worktree_path(ws, short_id)
        race_candidates = [expected] + [
            expected + s for s in _RELOCATION_SUFFIXES
        ]
        for cand in race_candidates:
            if not _has_git(cand):
                continue
            try:
                await org.update_agent(
                    agent_id,
                    {"workspace_path": cand, "worktree_error": None},
                )
            except Exception:
                pass
            actual = await _current_branch(cand)
            log.info(
                "executor_worktree_healed_after_race",
                agent_id=agent_id,
                short_id=short_id,
                path=cand,
                prior_error=err,
            )
            return {
                "success": True,
                "path": cand,
                "short_id": short_id,
                "branch": actual,
                "message": "worktree healthy after create race",
            }
        try:
            await org.update_agent(agent_id, {
                "worktree_error": err,
                "workspace_path": None,  # TEST16 P0-2: clear stale path so
                # next lazy-create allocates a fresh dir instead of
                # repeatedly hitting the same locked path.
            })
        except Exception:
            pass
        return {"success": False, "message": err, "short_id": short_id}

    path = result["path"]
    try:
        await org.update_agent(
            agent_id,
            {"workspace_path": path, "worktree_error": None},
        )
    except Exception as e:
        log.warning("worktree_bind_failed", agent_id=agent_id, error=str(e))

    # P0-3: detect relocation (path basename != short_id means -b/-c/-d fallback)
    # and notify agent — silent relocation is banned.
    relocated = Path(path).name != short_id
    if relocated:
        log.warning(
            "executor_worktree_relocated",
            agent_id=agent_id,
            short_id=short_id,
            canonical=_worktree_path(ws, short_id),
            actual=path,
        )
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().send_message(
                from_agent_id="system",
                to_agent_id=agent_id,
                message=(
                    f"[WORKTREE RELOCATED] Your workspace has been moved from "
                    f"the canonical path (.hiveweave/worktrees/{short_id}) to "
                    f"{Path(path).name} due to a locked/corrupted directory. "
                    f"Your new working directory is: {path}\n"
                    f"All merge/checkpoint operations will use this path. "
                    f"If you encounter merge_precondition_no_git errors, "
                    f"report to your coordinator."
                ),
                message_type="system",
            )
        except Exception as notify_err:
            log.warning(
                "executor_worktree_relocation_notify_failed",
                agent_id=agent_id,
                error=str(notify_err),
            )

    log.info(
        "executor_worktree_ensured",
        agent_id=agent_id,
        short_id=short_id,
        path=path,
        relocated=relocated,
    )
    return {
        "success": True,
        "path": path,
        "short_id": short_id,
        "branch": result.get("branch"),
        "relocated": relocated,
    }


async def worktree_commits_behind_main(
    workspace_path: str, worktree_path: str
) -> int:
    """Count commits the worktree HEAD is behind main (best-effort, 0 on error).

    Uses ``git rev-list --count HEAD..<base>`` in the worktree directory.
    Returns 0 if the worktree is up-to-date or the check fails.
    """
    base = await _resolve_base_branch(workspace_path)
    ok, out = await _git(
        ["rev-list", "--count", f"HEAD..{base}"], worktree_path
    )
    if not ok:
        return 0
    try:
        return int(out.strip())
    except (ValueError, TypeError):
        return 0

async def heal_project_executor_worktrees(project_id: str) -> dict:
    """Ensure every active writer with in-flight tasks has a worktree.

    Prunes stale metadata, recreates missing worktrees, updates agents.workspace_path.
    CEO/HR rows are excluded — they are pinned to the project root.

    TEST6 evening P1-2: idle writers with no live tree are not recreated —
    otherwise heal undoes close-GC / merge teardown. Empty DB + existing
    tree is healed (binding write-back) without create.
    """
    from hiveweave.db import meta as meta_db
    from hiveweave.db import project as project_db
    from hiveweave.services.org import OrgService

    from .reconcile import (
        _assignee_is_verify_only,
        _assignee_needs_write_worktree,
    )

    ws = await meta_db.get_project_workspace(project_id)
    if not ws or not (Path(ws) / ".git").exists():
        return {"recovered": 0, "failed": 0, "skipped": True}

    await _git(["worktree", "prune"], ws)
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
    except project_db.ProjectDbError:
        return {"recovered": 0, "failed": 0, "skipped": True}

    cur = await conn.execute(
        "SELECT id, name, role, short_id, workspace_path, permission_type "
        "FROM agents WHERE project_id=? AND status='active' "
        "AND permission_type IN ('executor', 'coordinator')",
        [project_id],
    )
    agents = await cur.fetchall()
    await cur.close()

    # builder coordinator 纳入恢复；CEO/HR（family!=coordinator 的 coordinator
    # 行）由 agent_gets_write_worktree 过滤掉。
    agents = [a for a in agents if agent_gets_write_worktree(dict(a))]

    recovered = 0
    failed = 0
    skipped_idle = 0
    org = OrgService()
    for a in agents:
        sid = (a["short_id"] or "").strip()
        bound = (a["workspace_path"] or "").strip()
        # 疏通: DB 空但树还在 → 写回绑定，不要当成 idle 跳过
        if sid:
            live = await heal_workspace_binding_from_disk(
                org, a["id"], ws, sid, current_ws=bound,
            )
            if live and not bound:
                recovered += 1
                bound = live
        if sid and not await _assignee_needs_write_worktree(ws, sid):
            # Idle skip: empty path (merge GC cleaned) or VERIFY-only.
            # Leftover path with no .git is a broken binding — fall through
            # so ensure_executor_worktree recreates. Live trees fall through
            # too (ensure is idempotent / already-bound).
            if await _assignee_is_verify_only(ws, sid) or not bound:
                skipped_idle += 1
                log.debug(
                    "worktree_heal_skipped_no_open_tasks",
                    agent_id=a["id"],
                    short_id=sid,
                    project_id=project_id,
                )
                continue
        result = await ensure_executor_worktree(
            project_id,
            a["id"],
            task_name=a["role"] or "developer",
        )
        if result.get("skipped"):
            skipped_idle += 1
            continue
        if result.get("success"):
            if result.get("message") != "worktree already bound":
                recovered += 1
        else:
            failed += 1
    return {
        "recovered": recovered,
        "failed": failed,
        "skipped_idle": skipped_idle,
        "skipped": False,
    }
