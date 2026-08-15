"""Off-turn job registry — spawn_subagent / bash(background=true) wait/wake.

Long coding work returns immediately with waiting_on kind=external.
Completion delivers a platform-prefix inbox message, clears the matching
wait, and wakes the agent. Do not nest this work inside streamer HARD 570
/ SAFETY 600.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import structlog

try:
    from hiveweave.services.wake_policy import OFFTURN_COMPLETION_MESSAGE_TYPE
except ImportError:
    OFFTURN_COMPLETION_MESSAGE_TYPE = "offturn_completion"

log = structlog.get_logger(__name__)

_INBOX_CHARS = 4000
_REAP_JOIN_S = 2.0
_REAP_DEAD_S = 15.0
_STOP_REASON = "stopped (project off-duty, dismiss, or shutdown)"

PREFIX_SUB_DONE = "[SUBAGENT DONE]"
PREFIX_SUB_FAILED = "[SUBAGENT FAILED]"
PREFIX_BASH_DONE = "[BASH DONE]"
PREFIX_BASH_FAILED = "[BASH FAILED]"

_COMPLETION_PREFIXES = (
    PREFIX_SUB_DONE,
    PREFIX_SUB_FAILED,
    PREFIX_BASH_DONE,
    PREFIX_BASH_FAILED,
)

_DONE = {"subagent": PREFIX_SUB_DONE, "bash": PREFIX_BASH_DONE}
_FAILED = {"subagent": PREFIX_SUB_FAILED, "bash": PREFIX_BASH_FAILED}

WorkFn = Callable[[], Awaitable[tuple[bool, str]]]
_T = TypeVar("_T")


async def await_even_if_cancelled(aw: Awaitable[_T]) -> _T:
    """Wait until *aw* finishes even if this task is cancelled.

    ``asyncio.shield`` alone does not delay cancellation: ``Task.cancel()``
    cancels the shield waiter Future, so the job is marked done while
    deliver still runs. Reap then pops the registry / deletes the
    worktree under a live inbox write, and a later CancelledError
    handler can send FAILED after DONE.

    On cancel we ``uncancel()`` just long enough to join the inner
    task, then restore cancellation so the job still surfaces
    CancelledError to join/reap.
    """
    task = aw if isinstance(aw, asyncio.Task) else asyncio.create_task(aw)
    current = asyncio.current_task()
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if current is not None:
            while current.cancelling():
                current.uncancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning("shielded_join_failed", exc_info=True)
        if current is not None:
            current.cancel()
        raise


@dataclass
class OffturnJob:
    job_id: str
    kind: str
    agent_id: str
    project_id: str
    worktree: str
    task: asyncio.Task
    wake_on_complete: bool = True
    task_id: str | None = None


_JOBS: dict[str, OffturnJob] = {}


def is_offturn_completion_text(text: str | None) -> bool:
    """Platform protocol prefixes for native off-turn jobs (not free-text)."""
    t = (text or "").lstrip()
    return any(t.startswith(p) for p in _COMPLETION_PREFIXES)


_NO_REAP_CANCEL_REASONS = frozenset({"busy_reset", "reset_processing"})


def cancel_should_reap_offturn(reason: str | None) -> bool:
    """User Stop / off_duty / stop_agent reap; 409 busy-reset does not."""
    return (reason or "cancelled").strip() not in _NO_REAP_CANCEL_REASONS


def is_live_job(ref: str, *, agent_id: str | None = None) -> bool:
    """True when *ref* is an in-flight off-turn job id.

    When *agent_id* is set, the job must belong to that agent — another
    assignee must not park ``ASSIGNEE_MUST_SUBMIT`` on a foreign job.
    """
    job = _JOBS.get((ref or "").strip())
    if job is None or job.task.done():
        return False
    if agent_id and job.agent_id != agent_id:
        return False
    return True


def has_live_jobs_for_agent(agent_id: str, *, exclude: str | None = None) -> bool:
    """True when this agent still has an in-flight off-turn job.

    Used so a sibling ``[SUBAGENT|BASH DONE]`` does not wipe the other
    job's wait contract (``wait_satisfied`` used to clear every wait).
    """
    aid = (agent_id or "").strip()
    if not aid:
        return False
    skip = (exclude or "").strip()
    for job in list(_JOBS.values()):
        if job.agent_id != aid:
            continue
        if skip and job.job_id == skip:
            continue
        if not job.task.done():
            return True
    return False


def job_bound_task_id(ref: str, *, agent_id: str | None = None) -> str | None:
    """Bound ledger task_id of a live job, else None.
    Respect agent_id owner check like is_live_job."""
    if not is_live_job(ref, agent_id=agent_id):
        return None
    job = _JOBS.get((ref or "").strip())
    bound = (job.task_id or "").strip() if job is not None else ""
    return bound or None


def agent_has_live_job_for_task(agent_id: str, task_id: str) -> bool:
    """True when this agent has an in-flight job with task_id == task_id.
    Empty task_id never matches (unbound jobs do not cover any submit gate)."""
    aid = (agent_id or "").strip()
    tid = (task_id or "").strip()
    if not aid or not tid:
        return False
    for job in _jobs_for_agent(aid):
        if job.task.done():
            continue
        if (job.task_id or "").strip() == tid:
            return True
    return False


def live_job_ids_for_agent(agent_id: str) -> list[str]:
    """Job ids of in-flight jobs owned by this agent (not done)."""
    aid = (agent_id or "").strip()
    if not aid:
        return []
    return [j.job_id for j in _jobs_for_agent(aid) if not j.task.done()]


def build_waiting_on(
    job_id: str, task_id: str | None = None, *, agent_id: str | None = None
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = [{"kind": "external", "ref": job_id}]
    seen: set[tuple[str, str]] = {("external", job_id)}
    if task_id and str(task_id).strip():
        tid = str(task_id).strip()
        items.append({"kind": "task", "ref": tid})
        seen.add(("task", tid))
    if agent_id:
        for job in _jobs_for_agent(agent_id):
            if job.job_id == job_id or job.task.done():
                continue
            key = ("external", job.job_id)
            if key not in seen:
                items.append({"kind": "external", "ref": job.job_id})
                seen.add(key)
            bound = (job.task_id or "").strip()
            tkey = ("task", bound)
            if bound and tkey not in seen:
                items.append({"kind": "task", "ref": bound})
                seen.add(tkey)
    return items


def next_action_waiting(waiting_on: list[dict[str, str]]) -> str:
    return (
        f"NEXT ACTION: commit_turn(phase=waiting, waiting_on={waiting_on}). "
        "Do not poll."
    )


async def resolve_assignee_task_id(
    project_id: str,
    agent_id: str,
    explicit: str | None = None,
) -> str | None:
    """Unique claimed/running/rework assignee task, or an explicit id."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    if not project_id:
        return None
    try:
        from hiveweave.services.task import TaskService

        mine = await TaskService().list_tasks(project_id, assignee_id=agent_id)
    except Exception:
        return None
    active = [
        t
        for t in (mine or [])
        if t.get("status") in ("running", "claimed", "rework")
    ]
    if len(active) == 1:
        tid = active[0].get("id")
        return str(tid) if tid else None
    return None


def start_offturn_job(
    *,
    kind: str,
    agent_id: str,
    project_id: str,
    work: WorkFn,
    worktree: str = "",
    wake_on_complete: bool = True,
    task_id: str | None = None,
) -> str:
    """Register *work* as a background task. Returns the job id immediately.

    *work* must return ``(ok, payload)``. The wrapper adds the protocol
    prefix, caps the inbox body, and always delivers — including on
    CancelledError (FAILED + stop reason).
    """
    if kind not in _DONE:
        raise ValueError(f"unknown offturn kind: {kind!r}")
    prefix = "bg-sub" if kind == "subagent" else "bg-bash"
    job_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
    # Register before the coroutine runs so is_live_job cannot miss a
    # fast-finishing task. The Event keeps work from starting until then.
    ready = asyncio.Event()

    async def _run() -> None:
        await ready.wait()
        try:
            try:
                ok, payload = await work()
            except asyncio.CancelledError:
                log.info(
                    "offturn_job_cancelled",
                    agent_id=agent_id,
                    job_id=job_id,
                    kind=kind,
                )
                body = f"{_FAILED[kind]} job={job_id}\n{_STOP_REASON}"
                wake = _job_wake_on_complete(job_id)
                try:
                    await await_even_if_cancelled(
                        deliver(agent_id, job_id, body, ok=False, wake=wake)
                    )
                except Exception as exc:
                    log.warning(
                        "offturn_job_cancel_deliver_failed",
                        agent_id=agent_id,
                        job_id=job_id,
                        error=str(exc),
                    )
                raise
            except Exception as exc:
                log.warning(
                    "offturn_job_failed",
                    agent_id=agent_id,
                    job_id=job_id,
                    kind=kind,
                    error=str(exc),
                )
                body = (
                    f"{_FAILED[kind]} job={job_id}\n"
                    f"{type(exc).__name__}: {_redact(str(exc))}"
                )
                wake = _job_wake_on_complete(job_id)
                try:
                    await await_even_if_cancelled(
                        deliver(agent_id, job_id, body, ok=False, wake=wake)
                    )
                except Exception as deliver_exc:
                    log.warning(
                        "offturn_job_fail_deliver_failed",
                        agent_id=agent_id,
                        job_id=job_id,
                        error=str(deliver_exc),
                    )
                return
            prefix_s = _DONE[kind] if ok else _FAILED[kind]
            body = _format_body(prefix_s, job_id, payload, worktree, agent_id, kind)
            wake = _job_wake_on_complete(job_id)
            try:
                await await_even_if_cancelled(
                    deliver(agent_id, job_id, body, ok=ok, wake=wake)
                )
            except Exception as deliver_exc:
                log.warning(
                    "offturn_job_ok_deliver_failed",
                    agent_id=agent_id,
                    job_id=job_id,
                    error=str(deliver_exc),
                )
        finally:
            _JOBS.pop(job_id, None)

    task = asyncio.create_task(_run(), name=job_id)
    _JOBS[job_id] = OffturnJob(
        job_id=job_id,
        kind=kind,
        agent_id=agent_id,
        project_id=project_id or "",
        worktree=worktree or "",
        task=task,
        wake_on_complete=wake_on_complete,
        task_id=(str(task_id).strip() or None) if task_id else None,
    )
    ready.set()
    return job_id


def _job_wake_on_complete(job_id: str) -> bool:
    """Read wake flag without unregistering — keep is_live_job true during deliver.

    Popping before inbox/clear_waits let ``clear_expired`` fire
    ``[WAIT_TIMEOUT]`` on the still-NULL live wait.
    """
    job = _JOBS.get(job_id)
    if job is None:
        return True
    return bool(job.wake_on_complete)


_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|authorization|bearer)\s*[=:]\s*(?:bearer\s+)?)\S+"
)
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")


def _redact(text: str) -> str:
    t = _SECRET_RE.sub(r"\1***", text or "")
    return _SK_RE.sub("sk-***", t)


def _format_body(
    prefix: str,
    job_id: str,
    payload: str,
    worktree: str,
    agent_id: str,
    kind: str,
) -> str:
    text = _redact((payload or "").strip() or "(no output)")
    spilled = _spill_large(text, agent_id, kind, worktree)
    return f"{prefix} job={job_id}\n{spilled}"


def _spill_large(text: str, agent_id: str, kind: str, worktree: str) -> str:
    """Cap inbox body; persist overflow so truncation is not the only copy."""
    if len(text) <= _INBOX_CHARS:
        return text
    path = ""
    try:
        from hiveweave.tools.executor import ToolExecutor

        path = ToolExecutor._save_tool_output_file(
            text, agent_id, f"offturn_{kind}", worktree or ""
        )
    except Exception as exc:
        log.debug("offturn_spill_failed", error=str(exc))
    head = text[:_INBOX_CHARS]
    if path:
        return (
            f"{head}\n…(truncated)\n"
            f"(full output saved: {path})"
        )
    return (
        f"{head}\n…(truncated)\n"
        "(body truncated in inbox; re-run a narrower command for the rest)"
    )


async def deliver(
    agent_id: str,
    job_id: str,
    message: str,
    *,
    ok: bool,
    wake: bool = True,
) -> None:
    await notify_completion(
        agent_id, message, wake=wake, clear_ref=job_id, ok=ok
    )


async def notify_completion(
    agent_id: str,
    message: str,
    *,
    wake: bool = True,
    clear_ref: str | None = None,
    ok: bool = True,
) -> None:
    """Land inbox + clear matching wait + one wake path (busy enqueue XOR trigger).

    Busy agents get ``wake=False`` on the inbox row so the watcher does not
    fire a second turn; ``enqueue_wake`` carries ``inbox_msg_ids``.
    Completions always clear the matching *ref* only — last-job ``*`` used
    to wipe sibling ``kind=agent`` waits.
    """
    sent: dict = {}
    busy = False
    try:
        from hiveweave.agents.supervisor import agent_manager
        from hiveweave.agents.types import AgentState

        agent = agent_manager.get_agent(agent_id)
        busy = agent is not None and agent.status == AgentState.PROCESSING
    except Exception:
        agent = None
        busy = False

    inbox_wake = bool(wake) and not busy
    try:
        from hiveweave.services.inbox import InboxService

        sent = await InboxService().send_message(
            from_agent_id="system",
            to_agent_id=agent_id,
            message=message,
            message_type=OFFTURN_COMPLETION_MESSAGE_TYPE,
            wake=inbox_wake,
            trusted_platform=True,
        ) or {}
    except Exception as exc:
        log.warning("offturn_inbox_failed", agent_id=agent_id, error=str(exc))

    if clear_ref:
        await _clear_job_wait(agent_id, clear_ref)
    if not wake:
        return

    try:
        from hiveweave.agents.trigger import trigger_subordinate
        from hiveweave.agents.types import AgentState

        still_busy = (
            agent is not None and agent.status == AgentState.PROCESSING
        )
        msg_id = str(sent.get("id") or "").strip()
        latch = {
            "trigger": True,
            "source": "wait_satisfied",
            "clear_waits": False,
            "from_agent_id": "system",
            "message_type": OFFTURN_COMPLETION_MESSAGE_TYPE,
        }
        if msg_id:
            latch["inbox_msg_ids"] = [msg_id]
        if still_busy and agent is not None:
            await agent.enqueue_wake(message, latch)
            return
        if not inbox_wake and agent is not None:
            await agent.chat(message, latch)
            return
        await trigger_subordinate(agent_id)
    except Exception as exc:
        log.warning(
            "offturn_wake_failed",
            agent_id=agent_id,
            error=str(exc),
            ok=ok,
        )


async def _clear_job_wait(agent_id: str, job_id: str) -> None:
    project_id = await _project_id_for(agent_id)
    if not project_id:
        return
    try:
        from hiveweave.services.wait_contract import wait_contract_service

        await wait_contract_service.clear_waits_matching_ref(
            project_id, agent_id, job_id
        )
    except Exception as exc:
        log.debug("offturn_clear_job_wait_failed", error=str(exc))


async def _clear_agent_waits(agent_id: str) -> None:
    project_id = await _project_id_for(agent_id)
    if not project_id:
        return
    try:
        from hiveweave.services.wait_contract import wait_contract_service

        await wait_contract_service.clear_waits(project_id, agent_id)
    except Exception as exc:
        log.debug("offturn_clear_waits_failed", error=str(exc))


async def _project_id_for(agent_id: str) -> str | None:
    try:
        from hiveweave.agents.supervisor import agent_manager
        from hiveweave.db import meta as meta_db

        agent = agent_manager.get_agent(agent_id)
        project_id = getattr(agent, "project_id", None) if agent is not None else None
        if not project_id:
            project_id = await meta_db.get_agent_project_id(agent_id)
        return str(project_id) if project_id else None
    except Exception:
        return None


def _jobs_for_agent(agent_id: str) -> list[OffturnJob]:
    return [j for j in list(_JOBS.values()) if j.agent_id == agent_id]


def _jobs_for_worktree(worktree: str) -> list[OffturnJob]:
    try:
        key = str(Path(worktree).resolve())
    except OSError:
        key = worktree
    found: list[OffturnJob] = []
    for job in list(_JOBS.values()):
        ws = job.worktree or ""
        if not ws:
            continue
        try:
            jk = str(Path(ws).resolve())
        except OSError:
            jk = ws
        if jk == key or ws == worktree:
            found.append(job)
    return found


async def _join_or_cancel(job: OffturnJob) -> None:
    """Let the job deliver FAILED on close/cancel; CancelledError still delivers."""
    task = job.task
    if task.done():
        return
    _done, pending = await asyncio.wait({task}, timeout=_REAP_JOIN_S)
    if not pending:
        return
    if not task.done():
        task.cancel()
    await asyncio.wait({task}, timeout=_REAP_JOIN_S)
    if not task.done():
        await asyncio.wait({task}, timeout=_REAP_DEAD_S)


async def kill_offturn_job(
    job_id: str, *, agent_id: str | None = None
) -> dict:
    """Agent-facing job_kill: cancel one live job; FAILED still delivers."""
    jid = (job_id or "").strip()
    job = _JOBS.get(jid)
    if job is None:
        return {"ok": False, "error": f"unknown job {jid}"}
    if agent_id and job.agent_id != agent_id:
        return {"ok": False, "error": "job belongs to another agent"}
    if job.task.done():
        return {"ok": True, "already_done": True, "job_id": jid}
    job.task.cancel()
    await asyncio.wait({job.task}, timeout=_REAP_JOIN_S)
    log.info("offturn_job_killed", job_id=jid, kind=job.kind)
    return {"ok": True, "job_id": jid, "kind": job.kind}


async def reap_offturn_for_agent(agent_id: str) -> int:
    """Off-duty / dismiss / user Stop: clear waits, then join+cancel jobs."""
    jobs = _jobs_for_agent(agent_id)
    for job in jobs:
        job.wake_on_complete = False
    await _clear_agent_waits(agent_id)
    for job in jobs:
        try:
            await _join_or_cancel(job)
        except Exception as exc:
            log.warning(
                "offturn_reap_agent_job_failed",
                agent_id=agent_id,
                job_id=job.job_id,
                error=str(exc),
            )
    return len(jobs)


async def reap_offturn_for_task(agent_id: str, task_id: str) -> int:
    """cancel_task: reap jobs bound to this ledger task only."""
    tid = (task_id or "").strip()
    if not tid:
        return 0
    jobs = [
        j
        for j in _jobs_for_agent(agent_id)
        if (j.task_id or "").strip() == tid
    ]
    for job in jobs:
        job.wake_on_complete = False
    n = 0
    for job in jobs:
        try:
            await _clear_job_wait(agent_id, job.job_id)
            await _join_or_cancel(job)
            n += 1
        except Exception as exc:
            log.warning(
                "offturn_reap_task_job_failed",
                agent_id=agent_id,
                task_id=tid,
                job_id=job.job_id,
                error=str(exc),
            )
    return n


async def reap_offturn_for_worktree(
    worktree: str, agent_id: str | None = None
) -> int:
    """Worktree teardown: join then cancel jobs bound to this tree."""
    jobs = _jobs_for_worktree(worktree)
    if agent_id:
        extra = [
            j for j in _jobs_for_agent(agent_id) if j.job_id not in {x.job_id for x in jobs}
        ]
        jobs = jobs + extra
    for job in jobs:
        job.wake_on_complete = False
    if agent_id:
        await _clear_agent_waits(agent_id)
    else:
        for job in jobs:
            if job.agent_id:
                await _clear_job_wait(job.agent_id, job.job_id)
    n = 0
    for job in jobs:
        try:
            await _join_or_cancel(job)
            n += 1
        except Exception as exc:
            log.warning(
                "offturn_reap_tree_job_failed",
                worktree=worktree,
                job_id=job.job_id,
                error=str(exc),
            )
    return n


async def reap_offturn_for_project(project_id: str) -> int:
    """Off-duty stop: cancel in-flight native bg jobs for this project."""
    from hiveweave.services.org import OrgService

    seen: set[str] = set()
    n = 0
    try:
        agents = await OrgService().list_agents(project_id)
    except Exception as exc:
        log.warning(
            "offturn_reap_list_agents_failed",
            project_id=project_id,
            error=str(exc),
        )
        agents = []
    for agent in agents or []:
        aid = str(agent.get("id") or "").strip()
        if not aid or aid in seen:
            continue
        seen.add(aid)
        try:
            n += await reap_offturn_for_agent(aid)
        except Exception as exc:
            log.warning(
                "offturn_reap_agent_failed",
                project_id=project_id,
                agent_id=aid,
                error=str(exc),
            )
    leftover = [
        j
        for j in list(_JOBS.values())
        if j.project_id == project_id and j.agent_id not in seen
    ]
    for job in leftover:
        job.wake_on_complete = False
    for job in leftover:
        try:
            await _clear_agent_waits(job.agent_id)
            await _join_or_cancel(job)
            n += 1
        except Exception as exc:
            log.warning(
                "offturn_reap_leftover_failed",
                project_id=project_id,
                job_id=job.job_id,
                error=str(exc),
            )
    return n


async def reap_all_offturn_jobs() -> int:
    """Process shutdown: cancel every native bg job without waking agents."""
    jobs = list(_JOBS.values())
    for job in jobs:
        job.wake_on_complete = False
    n = 0
    for job in jobs:
        try:
            await _clear_agent_waits(job.agent_id)
            await _join_or_cancel(job)
            n += 1
        except Exception as exc:
            log.warning(
                "offturn_reap_all_failed",
                job_id=job.job_id,
                error=str(exc),
            )
    return n


async def reset_offturn_for_tests() -> None:
    """Cancel leftover jobs so tests don't leak across cases."""
    for job in list(_JOBS.values()):
        job.wake_on_complete = False
    tasks = [j.task for j in list(_JOBS.values()) if not j.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(set(tasks), timeout=_REAP_JOIN_S)
    _JOBS.clear()
