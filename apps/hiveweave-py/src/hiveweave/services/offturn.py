"""Off-turn job registry — spawn_subagent / bash(background=true) wait/wake.

Long coding work returns immediately with waiting_on kind=external.
Completion delivers a platform-prefix inbox message, clears the matching
wait, and wakes the agent. Do not nest this work inside streamer HARD 570
/ SAFETY 600.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

import structlog

from hiveweave.util.redact import redact_secrets

try:
    from hiveweave.services.wake_policy import OFFTURN_COMPLETION_MESSAGE_TYPE
except ImportError:
    OFFTURN_COMPLETION_MESSAGE_TYPE = "offturn_completion"

log = structlog.get_logger(__name__)

_INBOX_CHARS = 4000
_REAP_JOIN_S = 2.0
_REAP_DEAD_S = 15.0
_STOP_REASON = "stopped (project off-duty, dismiss, or shutdown)"


# ─────────────────────── 终态：结构化状态 → 协议前缀 ───────────────────────
#
# 一条协议前缀串承载**两个**事实：哪个 phase（``[SUBAGENT …]`` / ``[BASH …]``）
# 与哪个终态（``DONE`` / ``DONE_TRUNCATED`` / ``FAILED``）。把**已渲染**的前缀
# 反解析回终态 = 文本判据 —— 禁用（用户 2026-09-14 钦定；且任何这种解析器都能
# 靠「换一种写法」绕过）。
# ⇒ 机制是**单向**的：`OFFTURN_STATE`（结构化）→ `.prefix`（渲染）。每个成员
# 以它渲染出的字符串命名，框架只用这一个方向，**绝不**从渲染结果反推终态。
#
# ⚠ 曾经试过两条错路，都别回去：
# ① 「payload 里带某个前缀」——还是读自由文本；
# ② 「按 `id(payload)` 登记标记」——`id()` 是**裸地址**：CPython 同 size-class
#    字符串在对象回收后立刻复用地址，一个**正常完成**的产出可能继承某个已死
#    作业的 id 而被误报 TRUNCATED（已实测复现）。终态是 `work()` 的**返回值**，
#    本来就该显式返回，不必也不准绕到对象身份上去猜。


class OFFTURN_STATE(StrEnum):
    """离轮作业终态（结构化，与渲染后的前缀字符串**一一对应**）。

    三值不是「OK / FAIL / 未知」而是「干完了 / 干过但没收尾 / 炸了」：
    第二值是本仓新增的第三态（P0-1），对应子代理 `budget_exhausted`
    ——产出**可能没落盘**，父代理必须验货而不是当完成。
    """

    SUBAGENT_DONE = "SUBAGENT_DONE"
    SUBAGENT_DONE_TRUNCATED = "SUBAGENT_DONE_TRUNCATED"
    SUBAGENT_FAILED = "SUBAGENT_FAILED"
    BASH_DONE = "BASH_DONE"
    BASH_FAILED = "BASH_FAILED"

    @property
    def prefix(self) -> str:
        """渲染成协议前缀串（**唯一**方向：状态 → 文本）。"""
        return _PREFIX_BY_STATE[self]


_PREFIX_BY_STATE: dict[OFFTURN_STATE, str] = {
    OFFTURN_STATE.SUBAGENT_DONE: "[SUBAGENT DONE]",
    # P0-1（TEST_DSH_60）：第三终态——「干过但没收尾（可能没落盘）」。
    # 真实终态本来是三值，过去被压成 DONE / FAILED 二值 ⇒ 7 条回执里 3 条
    # DONE 其实带着 `Hard turn budget exhausted`，父以为完成、不再验货。
    OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED: "[SUBAGENT DONE_TRUNCATED]",
    OFFTURN_STATE.SUBAGENT_FAILED: "[SUBAGENT FAILED]",
    OFFTURN_STATE.BASH_DONE: "[BASH DONE]",
    OFFTURN_STATE.BASH_FAILED: "[BASH FAILED]",
}
PREFIX_SUB_DONE = OFFTURN_STATE.SUBAGENT_DONE.prefix
PREFIX_SUB_FAILED = OFFTURN_STATE.SUBAGENT_FAILED.prefix
PREFIX_SUB_TRUNCATED = OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED.prefix
PREFIX_BASH_DONE = OFFTURN_STATE.BASH_DONE.prefix
PREFIX_BASH_FAILED = OFFTURN_STATE.BASH_FAILED.prefix

_COMPLETION_PREFIXES = tuple(s.prefix for s in OFFTURN_STATE)
"""回执协议全集（含第三值 TRUNCATED）——`is_offturn_completion_text` 用。
⚠ 不要和 `inbox` 的两张前缀表混淆：那两张管「give-up ACK / park 是否吞掉」，
**不含任何 DONE 类**（语义见 inbox.py 注释），不同源，**不要合并**。"""

#: 哪些终态属于哪个 kind（bash 无 TRUNCATED，见模块 docstring）。
_STATES_BY_KIND: dict[str, dict[str, OFFTURN_STATE]] = {
    "subagent": {
        "ok": OFFTURN_STATE.SUBAGENT_DONE,
        "truncated": OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED,
        "failed": OFFTURN_STATE.SUBAGENT_FAILED,
    },
    "bash": {
        "ok": OFFTURN_STATE.BASH_DONE,
        "failed": OFFTURN_STATE.BASH_FAILED,
    },
}

#: 可被 `work()` 用 `(False, payload, state)` 报出的非 ok 终态。
#: `DONE_TRUNCATED` 是**完成语义的弱化**，不是失败：它必须与 `DONE` 同族
#: （进 `_COMPLETION_PREFIXES` ⇒ 父的 kind=agent wait 照常满足；进
#: `inbox.ACK_SPARE_PREFIXES` ⇒ 不被 give-up ACK 吞掉），否则父会留在 wait
#: 上等一个永不再来的唤醒（静默停泊），或回执被静默吞掉。
_TRUNCATED_STATES = frozenset(
    s for s in OFFTURN_STATE if s is OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED
)
# ⚠ 为什么 TRUNCATED 进 `_COMPLETION_PREFIXES`（=> 被认成回执、满足父的
# kind=agent wait）却**不进** inbox 的 PARK_EXEMPT / ACK_SPARE：
# 那两张表**不含任何 DONE 类前缀**（语义见 inbox.py 注释），TRUNCATED 与
# DONE 同族；单独把它塞进去会制造「DONE 被人为 park 就永久丢」的新不一致。
# ⚠ 前缀是**现象**不是原因：为什么被切断写在正文（tool_loop 的收口说明）。

#: 离轮作业的 work 契约：``(ok, payload[, state])``。
#:
#: - ``ok=True``  → 该 kind 的 DONE。
#: - ``ok=False`` → 该 kind 的 FAILED（父应重派）。
#: - 第三位可选：显式声明非 ok 的**具体**终态，用于 `DONE_TRUNCATED`
#:   （完成语义的弱化，不是失败——见 `_TRUNCATED_STATES`）。
#:   终态是 `work()` 的返回值，**必须显式返回**，不许靠 payload 文本或对象
#:   身份反推（理由见上文三条）。
WorkFn = Callable[..., Awaitable[tuple]]


def _terminal_state(kind: str, ok: bool, state: OFFTURN_STATE | None) -> OFFTURN_STATE:
    """(kind, ok, 显式 state) → 终态。非法组合直接抛，绝不静默兜底。"""
    states = _STATES_BY_KIND[kind]
    if state is None:
        # 未显式声明：按 ok 走二值默认（旧调用方无需改动）
        return states["ok"] if ok else states["failed"]
    if state not in states.values():
        raise ValueError(
            f"terminal state {state} is not valid for offturn kind {kind!r} "
            f"(allowed: {sorted(s.value for s in states.values())})"
        )
    # 一致性：显式声明不得与 ok 矛盾
    is_failed = state is states["failed"]
    if ok == is_failed:
        raise ValueError(
            f"offturn kind {kind!r}: ok={ok} contradicts state={state.value}"
        )
    return state
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

    The wrapper renders the protocol prefix from the terminal state, caps the
    inbox body, and always delivers — including on CancelledError (FAILED +
    stop reason).  (P0-1)

    ``work()`` 返回 ``(ok, payload)`` 或 ``(ok, payload, OFFTURN_STATE)``：
    ``ok=True`` → DONE；``ok=False`` → FAILED；第三位显式声明第三终态
    （目前只有 ``SUBAGENT_DONE_TRUNCATED`` = 「干过但没收尾，产出可能没落
    盘」）。非法/矛盾组合抛 ``ValueError``，不静默兜底。
    """
    if kind not in _STATES_BY_KIND:
        raise ValueError(f"unknown offturn kind: {kind!r}")
    prefix = "bg-sub" if kind == "subagent" else "bg-bash"
    job_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
    # Register before the coroutine runs so is_live_job cannot miss a
    # fast-finishing task. The Event keeps work from starting until then.
    ready = asyncio.Event()

    async def _run() -> None:
        _failed_p = _STATES_BY_KIND[kind]["failed"].prefix
        await ready.wait()
        try:
            try:
                returned = await work()
            except asyncio.CancelledError:
                log.info(
                    "offturn_job_cancelled",
                    agent_id=agent_id,
                    job_id=job_id,
                    kind=kind,
                )
                body = f"{_failed_p} job={job_id}\n{_STOP_REASON}"
                wake = _job_wake_on_complete(job_id)
                try:
                    await await_even_if_cancelled(
                        deliver(
                            agent_id, job_id, body, ok=False, wake=wake, kind=kind
                        )
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
                    f"{_failed_p} job={job_id}\n"
                    f"{type(exc).__name__}: {_redact(str(exc))}"
                )
                wake = _job_wake_on_complete(job_id)
                try:
                    await await_even_if_cancelled(
                        deliver(
                            agent_id, job_id, body, ok=False, wake=wake, kind=kind
                        )
                    )
                except Exception as deliver_exc:
                    log.warning(
                        "offturn_job_fail_deliver_failed",
                        agent_id=agent_id,
                        job_id=job_id,
                        error=str(deliver_exc),
                    )
                return
            # P0-1：终态**由 work() 显式返回**，不读正文、不猜对象身份。
            ok = bool(returned[0])
            payload = str(returned[1])
            declared = returned[2] if len(returned) > 2 else None
            state = _terminal_state(kind, ok, declared)
            if state in _TRUNCATED_STATES:
                log.info(
                    "offturn_job_truncated",
                    agent_id=agent_id,
                    job_id=job_id,
                    kind=kind,
                )
            body = _format_body(
                state.prefix, job_id, payload, worktree, agent_id, kind
            )
            wake = _job_wake_on_complete(job_id)
            try:
                await await_even_if_cancelled(
                    deliver(
                        agent_id,
                        job_id,
                        body,
                        # P0-1：TRUNCATED 与 DONE 同属「干过活了」⇒ ok=True，
                        # 父的 kind=agent wait 照常满足。若给 False，父会留在
                        # wait 上等一个永不再来的唤醒（静默停泊）——「被预算
                        # 切断」是完成语义的弱化，不是失败。
                        ok=ok,
                        wake=wake,
                        kind=kind,
                    )
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


_redact = redact_secrets
"""保留旧名（调用点很多）。实现已收口到 ``util/redact.py`` —— 单一脱敏面。"""


def _format_body(
    prefix: str,
    job_id: str,
    payload: str,
    worktree: str,
    agent_id: str,
    kind: str,
) -> str:
    text = _redact((payload or "").strip() or "(no output)")
    fits = len(text) <= _INBOX_CHARS
    spilled = _spill_large(text, agent_id, kind, worktree)
    # TEST_DSH_64 #9（2026-09-19）DONE_TRUNCATED 三事实位 · 信封侧：
    # 正文未被收件箱截断时，显式声明「以上正文即子代理完整输出」—— 父不必
    # 猜 [TRUNCATED] 横幅之下还有没有隐藏下文（旧回执缺这一位，父无从区分
    # 「正文就是全部」与「正文只是头部」）。判据是本函数自己渲染的前缀常量
    # （状态→文本单向，不解析自由文本）；溢出分支的事实位由 _spill_large 附
    # （spill 路径 + 验货指路）。
    if fits and prefix == OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED.prefix:
        spilled = (
            f"{spilled}\n"
            "(以上正文即该子代理的完整输出，未经收件箱截断；仍需验货 —— "
            "git_worktree_status + read_file 确认实际落盘。)"
        )
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
    # TEST_DSH_64 #9：溢出分支同口径补验货指路（子代理用 git_worktree_status
    # + read_file；bash 产出无 worktree 语义，指路 read_file 即可）。
    verify_hint = (
        "git_worktree_status + read_file 验货"
        if kind == "subagent"
        else "read_file 验货"
    )
    head = text[:_INBOX_CHARS]
    if path:
        return (
            f"{head}\n…(truncated)\n"
            f"(full output saved: {path}; 可用 {verify_hint})"
        )
    return (
        f"{head}\n…(truncated)\n"
        "(body truncated in inbox; re-run a narrower command for the rest; "
        f"可用 {verify_hint})"
    )


async def deliver(
    agent_id: str,
    job_id: str,
    message: str,
    *,
    ok: bool,
    wake: bool = True,
    kind: str | None = None,
) -> None:
    await notify_completion(
        agent_id, message, wake=wake, clear_ref=job_id, ok=ok, kind=kind
    )


async def _record_offturn_failure_signature(
    agent_id: str, message: str, kind: str, clear_ref: str | None
) -> None:
    """Off-turn job 失败也进项目共享签名池（审计 #4：F10 hook 只包同步工具）。

    best-effort：签名写入失败绝不影响投递。project_id 优先取注册表里的
    OffturnJob.project_id（deliver 时 job 尚未 pop），兜底按 agent 反查。
    """
    try:
        from hiveweave.services.failure_signature import record_failure_signature

        project_id = ""
        job = _JOBS.get((clear_ref or "").strip())
        if job is not None:
            project_id = job.project_id or ""
        if not project_id:
            project_id = (await _project_id_for(agent_id)) or ""
        lines = (message or "").split("\n", 1)
        error_payload = lines[1].strip() if len(lines) > 1 and lines[1].strip() else (message or "").strip()
        await record_failure_signature(
            project_id=project_id or None,
            agent_id=agent_id,
            tool_name="spawn_subagent" if kind == "subagent" else f"offturn:{kind}",
            error=error_payload or None,
            attribution="off-turn job failure",
        )
    except Exception as exc:
        log.warning(
            "offturn_failure_signature_failed",
            agent_id=agent_id,
            kind=kind,
            error=str(exc),
        )


async def notify_completion(
    agent_id: str,
    message: str,
    *,
    wake: bool = True,
    clear_ref: str | None = None,
    ok: bool = True,
    kind: str | None = None,
) -> None:
    """Land inbox + clear matching wait + one wake path (busy enqueue XOR trigger).

    Busy agents get ``wake=False`` on the inbox row so the watcher does not
    fire a second turn; ``enqueue_wake`` carries ``inbox_msg_ids``.
    Completions always clear the matching *ref* only — last-job ``*`` used
    to wipe sibling ``kind=agent`` waits.
    """
    sent: dict = {}
    busy = False
    if not ok and kind:
        await _record_offturn_failure_signature(agent_id, message, kind, clear_ref)
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
