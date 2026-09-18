"""MeetingTurnRunner — 过程遗忘的根（docs/spec/team-meeting.md）。

仿 ``tools/subagent.py``：自建 Streamer、占 Agent 排他槽（PROCESSING +
心跳），**不**走 ``agent.chat()`` / ``handle_completion``。

写侧隔离（必须全部成立，code-review 逐条核对）：
- 不 ``append_turn``、不建主聊 streaming 行、不自动 work_log；
- 不 ACK inbox、不 turn-exit、不 task-advance、不 drain、不 ``clear_waits``；
- 流式只发 ``meeting_updated``（走 bus.publish，**禁** publish_stream_event
  —— text_delta 不得漏进 agent 主聊或 lobby 活动流）。

工具执行期白名单（替换不是并集）：
- 参会者：``speak_in_meeting`` + 只读集；
- 主持：另加 ``continue_meeting_round`` / ``conclude_topic``（第 3 轮去掉
  continue）；
- ``write_memory`` / ``send_message`` / ``write_file`` 等即使模型硬调也 deny。

空发言 / 不调 speak / doom / 超时（180s）→ abstain，不把 assistant 散文当
发言。``MEETING_MAX_TOOL_ROUNDS = 8``。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

import structlog

from hiveweave.llm.streamer import Streamer
from hiveweave.services.meetings.service import (
    ABSTAIN_BUDGET_EXHAUSTED,
    ABSTAIN_ERROR,
    ABSTAIN_NO_SPEECH,
    ABSTAIN_TIMEOUT,
    MAX_ROUNDS,
)

log = structlog.get_logger(__name__)

MEETING_SPEECH_TIMEOUT_S = 180
MEETING_MAX_TOOL_ROUNDS = 8

# abstain 原因枚举的**唯一定义处**在 services/meetings/service.py（prompts 也要
# 引用它，放这里会造 prompts→runner 的新依赖边）。此处只是转出，勿另立一份。
# 参见 service.py 的同名常量块：那里解释了为什么必须是**结构化列**而不是文案。

# 只读集（参会者 + 主持共用）——白名单是唯一权威：未列入不出现。
_MEETING_READONLY_TOOLS = frozenset({
    "read_file", "list_files", "grep", "search_files",
    "read_memory", "read_work_logs", "get_tasks", "calculate", "websearch",
})

MEETING_PARTICIPANT_TOOLS = frozenset({"speak_in_meeting"}) | _MEETING_READONLY_TOOLS
MEETING_CHAIR_TOOLS = (
    MEETING_PARTICIPANT_TOOLS | {"continue_meeting_round", "conclude_topic"}
)

#: 统一的 runner_fn 注入签名（编排器唯一 LLM 入口；测试注入 fake）。
#: 返回 dict: {action: speak|abstain|continue|conclude|none,
#:            content, direction, result}
RunnerFn = Callable[..., Awaitable[dict[str, Any]]]


def tools_for_profile(profile: str, *, allow_continue: bool = True) -> frozenset[str]:
    """执行期白名单（替换不是并集）。第 3 轮去掉 continue。"""
    if profile == "chair":
        tools = set(MEETING_CHAIR_TOOLS)
        if not allow_continue:
            tools.discard("continue_meeting_round")
        return frozenset(tools)
    return frozenset(MEETING_PARTICIPANT_TOOLS)


def _tool_defs(whitelist: frozenset[str]) -> list[dict[str, Any]]:
    """从注册表 + TOOL_PARAM_SCHEMAS 构建白名单工具的 LLM defs。"""
    from hiveweave.tools.executor import (
        get_tool_description,
        get_tool_schema_for_llm,
    )

    defs: list[dict[str, Any]] = []
    for name in sorted(whitelist):
        defs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": get_tool_description(name),
                "parameters": get_tool_schema_for_llm(name),
            },
        })
    return defs


def _identity(agent: Any, tool_profile: str) -> str:
    cfg = getattr(agent, "config", None) or {}
    name = cfg.get("name") or getattr(agent, "id", "?")
    role = cfg.get("role") or "agent"
    from hiveweave.services.meetings.prompts import runner_overlay

    return f"You are {name} ({role}).\n\n{runner_overlay(tool_profile)}"


async def _load_context_messages(agent: Any) -> list[dict[str, Any]]:
    """读侧：compacted_prefix + 既有 conversation_turns（只读，不回写）。"""
    messages: list[dict[str, Any]] = []
    try:
        from hiveweave.conversation.store import conversation_store

        prefix = conversation_store.get_compacted_prefix(
            getattr(agent, "project_id", ""), getattr(agent, "id", "")
        )
        if prefix:
            messages.append({"role": "system", "content": prefix})
        history = await conversation_store.get_history(
            getattr(agent, "id", ""), getattr(agent, "project_id", "")
        )
        messages.extend(history or [])
    except Exception as e:
        log.debug("meeting_runner_context_load_failed", error=str(e))
    return messages


def make_on_tool_call(
    *,
    agent: Any,
    executor: Any | None,
    workspace: str,
    project_root: str | None,
    whitelist: frozenset[str],
    holder: dict[str, Any],
    on_speech: Callable[[str], Awaitable[None]] | None = None,
) -> Any:
    """构建会务回合工具回调（拦截会务工具 + 深度防御白名单）。

    - ``speak_in_meeting`` / ``continue_meeting_round`` / ``conclude_topic``
      本地拦截（不落 executor —— 普通权限路径对它们是硬拒）；
    - 白名单外（write_memory / send_message / write_file / …）一律 deny；
    - 只读工具转发 ToolExecutor（权限按 agent 身份评估）。
    """

    async def callback(tool_name: str, arguments: str, tool_call_id: str) -> dict:
        def _reply(text: str, *, end_turn: bool = False) -> dict:
            reply: dict[str, Any] = {
                "role": "tool",
                "content": text,
                "tool_call_id": tool_call_id,
            }
            if end_turn:
                reply["end_turn"] = True
            return reply

        args: dict[str, Any] = {}
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            args = {}

        if tool_name == "speak_in_meeting":
            content = str(args.get("content") or "").strip()
            if not content:
                return _reply(
                    "[Tool Error] speak_in_meeting requires non-empty content."
                )
            holder["speech"] = content
            if on_speech is not None:
                try:
                    await on_speech(content)
                except Exception as e:
                    return _reply(f"[Tool Error] speech rejected: {e}")
            return _reply(
                "发言已登记。不要再调用工具，直接结束本回合。",
                end_turn=True,
            )

        if tool_name == "continue_meeting_round":
            direction = str(args.get("direction") or "").strip()
            if not direction:
                return _reply(
                    "[Tool Error] continue_meeting_round requires a direction."
                )
            if "continue_meeting_round" not in whitelist:
                return _reply(
                    "[Tool Error] continue is unavailable in the final round; "
                    "you must conclude_topic."
                )
            holder["decision"] = {"action": "continue", "direction": direction}
            return _reply("方向已登记，会议继续。", end_turn=True)

        if tool_name == "conclude_topic":
            result = str(args.get("result") or "").strip()
            if not result:
                return _reply(
                    "[Tool Error] conclude_topic requires a non-empty result."
                )
            if "conclude_topic" not in whitelist:
                return _reply("[Tool Error] conclude_topic not available.")
            holder["decision"] = {"action": "conclude", "result": result}
            return _reply("议题已收口。", end_turn=True)

        # 深度防御：白名单外直接拒绝（不落 executor）
        if tool_name not in whitelist:
            return _reply(
                f"[Tool Error] {tool_name} is not available in a meeting turn "
                "(write tools, messaging and memory are hard-denied)."
            )

        if executor is None:
            return _reply(f"[Tool Error] {tool_name}: no executor available")
        try:
            result = await executor.execute(
                getattr(agent, "id", ""),
                tool_name,
                args,
                workspace,
                project_root,
            )
        except Exception as e:
            return _reply(
                f"[Tool Error] {tool_name}: {type(e).__name__}: {e}"
            )
        content = result.get("output") or result.get("error") or "(empty)"
        return _reply(str(content))

    return callback


async def run_meeting_turn(
    agent: Any,
    *,
    tool_profile: str,
    briefing: str,
    timeout_s: float = MEETING_SPEECH_TIMEOUT_S,
    allow_continue: bool = True,
    on_speech: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """跑一个会务 LLM 回合。返回统一形状 dict：

    - 参会者：``{"action": "speak", "content": …}`` 或
      ``{"action": "abstain", "content": <reason>}``
    - 主持：``{"action": "continue", "direction": …}`` /
      ``{"action": "conclude", "result": …}`` /
      ``{"action": "none", "content": <reason>}``
    """
    agent_id = getattr(agent, "id", "?")
    # 执行期白名单（替换不是并集）：第 3 轮主持去掉 continue。
    whitelist = tools_for_profile(
        tool_profile,
        allow_continue=allow_continue if tool_profile == "chair" else True,
    )
    holder: dict[str, Any] = {}
    workspace = ""
    project_root = None
    try:
        got = await agent._get_workspace_path()
        if got:
            workspace = str(got)
    except Exception:
        pass
    try:
        from hiveweave.db import meta as meta_db

        project_root = await meta_db.get_project_workspace(
            getattr(agent, "project_id", "")
        )
    except Exception:
        pass

    executor = getattr(agent, "_tool_executor", None)
    on_tool_call = make_on_tool_call(
        agent=agent,
        executor=executor,
        workspace=workspace,
        project_root=project_root,
        whitelist=whitelist,
        holder=holder,
        on_speech=on_speech,
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _identity(agent, tool_profile)},
    ]
    messages.extend(await _load_context_messages(agent))
    messages.append({"role": "user", "content": briefing})

    model_config = None
    try:
        model_config = await agent._get_model_config()
    except Exception:
        model_config = None
    if not model_config:
        return {
            "action": "abstain" if tool_profile != "chair" else "none",
            "content": "no model config available",
        }

    tools = _tool_defs(whitelist)
    streamer = Streamer(max_tool_rounds=MEETING_MAX_TOOL_ROUNDS)

    # 排他槽：PROCESSING + 心跳（避免僵尸误杀），结束必须释放。
    prev_status = getattr(agent, "status", None)
    try:
        from hiveweave.agents.types import AgentState

        agent.status = AgentState.PROCESSING
    except Exception:
        agent.status = "processing"
    try:
        agent._broadcast_status("processing", {"meeting_turn": True})
    except Exception:
        pass
    agent._start_heartbeat()
    started_ms = int(time.time() * 1000)
    try:
        result = await asyncio.wait_for(
            streamer.stream(
                agent_id=f"meeting-{agent_id}",
                messages=messages,
                model_config=model_config,
                tools=tools,
                on_tool_call=on_tool_call,
                max_tool_rounds=MEETING_MAX_TOOL_ROUNDS,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        log.warning("meeting_turn_timeout", agent_id=agent_id,
                    profile=tool_profile, timeout_s=timeout_s)
        return _finish(agent, prev_status, _timeout_outcome(tool_profile))
    except asyncio.CancelledError:
        # 泵重启/停机取消：先释放排他槽状态再上抛——否则 agent 行状态滞留
        # PROCESSING 只能等泵自愈（遗留 P3：runner CancelledError 滞留）。
        # 心跳由 finally 统一停，这里只还状态。
        _finish(agent, prev_status, {"action": "none", "content": "cancelled"})
        raise
    except Exception as e:
        log.warning("meeting_turn_error", agent_id=agent_id,
                    profile=tool_profile, error=str(e))
        return _finish(agent, prev_status, _fail_outcome(tool_profile, str(e)))
    finally:
        try:
            agent._stop_heartbeat()
        except Exception:
            pass
    log.info(
        "meeting_turn_done",
        agent_id=agent_id,
        profile=tool_profile,
        status=result.get("status"),
        elapsed_ms=int(time.time() * 1000) - started_ms,
    )
    return _finish(agent, prev_status, _outcome_from(tool_profile, holder, result))


def _timeout_outcome(tool_profile: str) -> dict[str, Any]:
    if tool_profile == "chair":
        return {"action": "none", "content": "chair turn timed out"}
    return {
        "action": "abstain",
        "content": "speech timed out (180s)",
        "abstain_reason": ABSTAIN_TIMEOUT,
    }


def _fail_outcome(tool_profile: str, error: str) -> dict[str, Any]:
    if tool_profile == "chair":
        return {"action": "none", "content": f"chair turn failed: {error}"}
    return {
        "action": "abstain",
        "content": f"turn failed: {error}",
        "abstain_reason": ABSTAIN_ERROR,
    }


def _outcome_from(
    tool_profile: str, holder: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """从 holder / streamer 结果合成终局。

    ⚠ ``budget_exhausted`` 必须在这里被判（与 ``tools/subagent.py`` 的 P0-1
    同源）：8 轮被掐断时若混进普通 ``abstain``，主持人会把「**没来得及说话**」
    读成「**没有意见**」，据此推进决策 —— 这是「用正常状态掩盖异常状态」的
    又一实例（同族见 PLATFORM-ISSUES §8.5）。
    """
    decision = holder.get("decision")
    if decision:
        return decision
    # 被轮次预算切断 ⇒ 独立的 abstain 原因。为什么排在 speech **之前**：
    # 预算闸口都在「开新一轮之前」触发，且成功调工具的一轮必带
    # ``end_turn``（见 ``tool_loop.py`` 的 ``if end_turn:`` 早返回，
    # 那条路径**不带** ``budget_exhausted``）⇒ 二者互斥。若真出现共存，
    # 说明轮次被用完而非发言成功，按「未完成」记更保守（宁可让主席知道
    # 有人没跑完，也不要把可疑内容当正式表态）。
    if result.get("budget_exhausted"):
        return {
            "action": "abstain",
            "content": "budget exhausted before the turn finished",
            "abstain_reason": ABSTAIN_BUDGET_EXHAUSTED,
        }
    speech = holder.get("speech")
    if speech:
        return {"action": "speak", "content": speech}
    # 空发言 / 不调工具 → abstain / none（不把 assistant 散文当发言）
    if tool_profile == "chair":
        return {"action": "none", "content": "no continue/conclude decision"}
    return {
        "action": "abstain",
        "content": "no speak_in_meeting call",
        "abstain_reason": ABSTAIN_NO_SPEECH,
    }


def _finish(
    agent: Any, prev_status: Any, outcome: dict[str, Any]
) -> dict[str, Any]:
    """释放排他槽回 IDLE 并广播。"""
    try:
        from hiveweave.agents.types import AgentState

        agent.status = AgentState.IDLE
    except Exception:
        agent.status = "idle"
    try:
        agent._broadcast_status("idle", {"meeting_turn": True})
    except Exception:
        pass
    return outcome
