"""Poll-tool cache, waiting gate, and obligations snapshot."""
from __future__ import annotations

import time

# Status-poll cache / waiting gate (TEST3 — stop check_agent_status storms)
# get_tasks is intentionally NOT gated while waiting — resume turns need it.
# Per-turn hard reject (TEST4): same get_tasks fingerprint ≥3 → force waiting.
_POLL_CACHE_TOOLS = frozenset({"check_agent_status", "get_tasks"})
_WAITING_GATE_TOOLS = frozenset({"check_agent_status"})
_POLL_HARD_REJECT_TOOLS = frozenset({"get_tasks"})
_POLL_HARD_REJECT_LIMIT = 3
_POLL_CACHE_TTL_S = 30.0
_poll_result_cache: dict[tuple[str, str, str], tuple[float, str]] = {}


def _poll_cache_get(agent_id: str, tool_name: str, arguments: str) -> str | None:
    if tool_name not in _POLL_CACHE_TOOLS:
        return None
    key = (agent_id, tool_name, arguments or "")
    entry = _poll_result_cache.get(key)
    if not entry:
        return None
    expires, content = entry
    if time.monotonic() > expires:
        _poll_result_cache.pop(key, None)
        return None
    return f"[cached {tool_name} ≤{_POLL_CACHE_TTL_S:.0f}s] {content}"


def _poll_cache_put(
    agent_id: str, tool_name: str, arguments: str, content: str
) -> None:
    if tool_name not in _POLL_CACHE_TOOLS:
        return
    key = (agent_id, tool_name, arguments or "")
    _poll_result_cache[key] = (time.monotonic() + _POLL_CACHE_TTL_S, content)


async def _build_obligations_snapshot(agent_id: str) -> str:
    """TEST10: poll hard-reject 时附带当前待办快照（与 exit-gate 同源）。

    让 agent 被禁止继续轮询时仍拿到可行动信息（任务 id / 状态 / 角色），
    直接对任务操作，而不是盲目重试 get_tasks。best-effort：失败返回空串。

    ⚠ F6 本体（2026-09-18）两段式：**白名单查询先行**（它带
    ``promote_assigned_created`` 自愈写副作用，不能因短路漏跑），再取
    **闭式** ``get_open_work_obligations``（与完成闸同源）判「有没有活」。
    **空结果不再输出 "safe to commit_turn(waiting)"**——那是许可语义，
    白名单排 blocked ⇒ blocked-only 的 agent 曾被谎告可收尾
    （PLATFORM-ISSUES §1.3）；「能否收尾」归 ``TaskService.can_idle``，
    poll 只报账本。闭式有活而白名单空（blocked 等）⇒ 明说「还有活、
    但现在不可行动」。
    """
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.task import TaskService

        project_id = await meta_db.get_agent_project_id(agent_id)
        if not project_id:
            return ""
        svc = TaskService()
        obligations = await svc.get_actionable_obligations(
            project_id, agent_id
        )
        open_work = await svc.get_open_work_obligations(project_id, agent_id)
        if not open_work:
            return "\nCurrent obligations: none."
        # 分支条件用「闭式 − 可行动」的 id 集差表达（不用白名单的**空**做
        # 门槛 —— 那正是 scripts/verify_commit_license.py 禁的判据形态；
        # 本分支输出的是反许可提示，按 id 集差写才与真实条件同构）。
        actionable_ids = {str(o.get("id") or "") for o in obligations}
        non_actionable = [
            o
            for o in open_work
            if str(o.get("id") or "") not in actionable_ids
        ]
        if not actionable_ids and non_actionable:
            return (
                f"\nYou still hold open work ({len(non_actionable)} item(s), "
                "e.g. blocked / awaiting arbitration) that is NOT currently "
                "actionable. Do NOT claim done or 'no obligations'; "
                "commit_turn(phase='blocked', waiting_on=[…]) to park with "
                "an explicit reason, or escalate to the creator."
            )
        lines = ["\nCurrent obligations (act directly, do NOT re-poll):"]
        for ob in obligations[:8]:
            # #9（2026-09-11）：此前这里印的是 `taskId=<前 8 位>` —— **把一个
            # 被截断的 id 标成了权威字段名 `taskId=`**。模型照抄去 claim/submit
            # 会失败（解析器只认完整 id 或唯一前缀），然后这次失败被归因成
            # "模型幻觉 id"。**平台展示既要给可回填的值，又不能谎报它的身份。**
            #
            # 现在：给**完整 id**（`taskId=` 名副其实），另用 `shortId=` 显式
            # 标注缩写身份 —— 名字不同，模型就不会把缩写当权威值用。
            _tid_full = str(ob.get("id") or "")
            title = (ob.get("title") or "")[:40].replace("\n", " ")
            status = ob.get("status") or "?"
            role = ob.get("role_hint") or "?"
            lines.append(
                f"  - [{role}/{status}] taskId={_tid_full} "
                f"(shortId={_tid_full[:8]}) {title}"
            )
        if len(obligations) > 8:
            lines.append(f"  ... and {len(obligations) - 8} more")
        if non_actionable:
            # F6 审计 LOW-3：混合态下 blocked 义务也要显形（不构成许可输出）
            lines.append(
                f"  ... plus {len(non_actionable)} open item(s) NOT currently "
                "actionable (e.g. blocked) — do not claim 'no obligations'"
            )
        return "\n".join(lines)
    except Exception:
        return ""


async def _poll_waiting_gate_block_async(
    agent_id: str, tool_name: str
) -> str | None:
    """Block repeated check_agent_status while wait contract is active.

    Does not block get_tasks — woken agents must be able to locate work.
    """
    if tool_name not in _WAITING_GATE_TOOLS:
        return None
    try:
        from hiveweave.agents.supervisor import agent_manager
        from hiveweave.services.wait_contract import wait_contract_service

        agent = agent_manager.get_agent(agent_id)
        if agent is None:
            return None
        disp = (getattr(agent, "disposition", None) or "")
        if not disp.startswith("waiting"):
            return None
        project_id = getattr(agent, "project_id", None)
        if not project_id:
            return None
        waits = await wait_contract_service.list_active(project_id, agent_id)
        if not waits:
            return None
        refs = ", ".join(
            f"{w.get('kind')}:{w.get('ref')}" for w in waits[:4]
        )
        return (
            f"[wait contract active] disposition={disp}; waits=[{refs}]. "
            f"Do NOT poll {tool_name} again — call commit_turn(phase='waiting') "
            "if needed and wait for event wake (ask_reply / task_transition / timeout)."
        )
    except Exception:
        return None

