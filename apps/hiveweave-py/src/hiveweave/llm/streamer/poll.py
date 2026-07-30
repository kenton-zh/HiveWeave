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
    """
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.task import TaskService

        project_id = await meta_db.get_agent_project_id(agent_id)
        if not project_id:
            return ""
        obligations = await TaskService().get_actionable_obligations(
            project_id, agent_id
        )
        if not obligations:
            return "\nCurrent obligations: none — safe to commit_turn(waiting)."
        lines = ["\nCurrent obligations (act directly, do NOT re-poll):"]
        for ob in obligations[:8]:
            tid = str(ob.get("id") or "")[:8]
            title = (ob.get("title") or "")[:40].replace("\n", " ")
            status = ob.get("status") or "?"
            role = ob.get("role_hint") or "?"
            lines.append(f"  - [{role}/{status}] taskId={tid} {title}")
        if len(obligations) > 8:
            lines.append(f"  ... and {len(obligations) - 8} more")
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

