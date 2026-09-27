"""用户发图喂 LLM 视觉 — 触发链回归测试（B8 落库修复的后续一环）。

链路：WS chat push / REST POST /api/chat（data URL 数组）
  → parse_user_images（内部格式 {media_type, data}，限幅 ≤5 张、单张 b64 ≤2.8M chars）
  → agent.chat(msg, opts={"images": ...})
  → _build_messages：本轮 user 消息带 images → provider 渲染多模态 parts
  → handle_completion：conversation 用户 turn 带 images（后续轮历史仍可见）；
    in-flight 工具截图仍不落库。

已知边界（本次不扩展）：busy insert（steer）与 inbox 排队保持纯文本；
text-only 模型由 provider 在请求构建时剥图留指引（负缓存/关键字自判定）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hiveweave.agents.agent import Agent, AgentState
from hiveweave.services.vision import (
    MAX_USER_IMAGES,
    MAX_USER_IMAGE_B64_CHARS,
    MAX_USER_IMAGES_TOTAL_B64_CHARS,
    messages_without_images,
    parse_user_images,
    strip_images_from_messages,
)
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)

PROJECT_ID = "user-images-test-project"
AGENT_ID = "user-images-exec"

USER_IMAGES = [
    {"media_type": "image/png", "data": "AAAA"},
    {"media_type": "image/jpeg", "data": "BBBB"},
]


# ── 1. data URL 解析（vision.parse_user_images）──────────────


def test_parse_user_images_valid():
    out = parse_user_images(
        [
            "data:image/png;base64,AAAA",
            "data:image/jpeg;base64,BBBB",
            "data:image/jpeg;charset=utf-8;base64,CCCC",  # 带参数头
        ]
    )
    assert out == [
        {"media_type": "image/png", "data": "AAAA"},
        {"media_type": "image/jpeg", "data": "BBBB"},
        {"media_type": "image/jpeg", "data": "CCCC"},
    ]


def test_parse_user_images_bad_entries_skipped_fail_open():
    out = parse_user_images(
        [
            "https://example.com/a.png",       # 坏前缀
            "data:image/png,AAAA",             # 非 base64（无 ;base64 标记）
            "data:text/plain;base64,QQ==",     # 非 image mime
            "data:image/png;base64,",          # 空 data
            123,                               # 非字符串
            None,
            "   ",                             # 空白
            "data:image/png;base64,GOOD",      # 合法条目夹在中间
        ]
    )
    assert out == [{"media_type": "image/png", "data": "GOOD"}]


def test_parse_user_images_caps():
    many = [
        f"data:image/png;base64,I{i}" for i in range(MAX_USER_IMAGES + 2)
    ]
    out = parse_user_images(many)
    assert len(out) == MAX_USER_IMAGES

    oversized = "data:image/png;base64," + "A" * (MAX_USER_IMAGE_B64_CHARS + 1)
    out2 = parse_user_images(["data:image/png;base64,OK", oversized])
    assert out2 == [{"media_type": "image/png", "data": "OK"}]


def test_parse_user_images_non_list_inputs():
    assert parse_user_images(None) == []
    assert parse_user_images("data:image/png;base64,AAAA") == []
    assert parse_user_images({"data": "x"}) == []
    assert parse_user_images([]) == []


# ── 2. WS idle 分支：图随 opts 穿到 agent.chat ────────────────


def _make_ws_idle_agent() -> SimpleNamespace:
    agent = SimpleNamespace(
        status=SimpleNamespace(value="idle"),
        project_id="p1",
        _on_stream_event=lambda *a, **k: None,
        chat_calls=[],
    )

    async def chat(msg: str, *args, **kwargs) -> dict:
        agent.chat_calls.append((msg, args, kwargs))
        return {"ok": True}

    agent.chat = chat
    return agent


async def _run_ws_chat_push(payload: dict, agent: SimpleNamespace):
    from hiveweave.realtime.phoenix_adapter import _handle_chat_push

    sent: list[list] = []

    class FakeChatService:
        async def save_message(self, attrs: dict) -> dict:
            return {"id": "m1"}

    class FakeManager:
        def __init__(self, a):
            self._a = a

        def get_agent(self, agent_id: str):
            return self._a

    import hiveweave.services.chat_message as chat_message_mod
    import hiveweave.agents.supervisor as supervisor_mod

    orig_chat_service = chat_message_mod.ChatMessageService
    orig_manager = supervisor_mod.agent_manager
    chat_message_mod.ChatMessageService = FakeChatService
    supervisor_mod.agent_manager = FakeManager(agent)
    try:
        async def send_fn(frame) -> bool:
            sent.append(frame)
            return True

        await _handle_chat_push("agent:agent-1", payload, send_fn)
    finally:
        chat_message_mod.ChatMessageService = orig_chat_service
        supervisor_mod.agent_manager = orig_manager
    return sent


async def test_ws_idle_images_reach_agent_chat_as_internal_format():
    agent = _make_ws_idle_agent()
    await _run_ws_chat_push(
        {
            "message": "看这张图",
            "images": ["data:image/png;base64,AAAA"],
        },
        agent,
    )
    assert len(agent.chat_calls) == 1
    msg, args, kwargs = agent.chat_calls[0]
    envelope = json.loads(msg)
    assert envelope["content"] == "看这张图"
    # 图以内部格式随 opts 传入（data URL 前缀已剥）；批 6（I15）起用户入口
    # 显式带 source:"chat"（缺省值不许是业务值——漏传必须落 unknown）。
    assert args == (
        {"source": "chat", "images": [{"media_type": "image/png", "data": "AAAA"}]},
    )
    assert kwargs == {}


async def test_ws_idle_no_images_single_arg_call_unchanged():
    """无图路径回归：批 6 起单参形态 = 显式 source 的 opts（无 images 键）。"""
    agent = _make_ws_idle_agent()
    await _run_ws_chat_push({"message": "纯文本"}, agent)
    msg, args, kwargs = agent.chat_calls[0]
    assert args == ({"source": "chat"},) and kwargs == {}


async def test_ws_idle_bad_images_trigger_no_images_call():
    """脏图载荷：解析不出任何内部格式 → 退回无图调用，不炸。"""
    agent = _make_ws_idle_agent()
    await _run_ws_chat_push(
        {"message": "x", "images": ["https://bad", "data:image/png;base64,"]},
        agent,
    )
    _msg, args, kwargs = agent.chat_calls[0]
    assert args == ({"source": "chat"},) and kwargs == {}


# ── 3. REST send_chat：图随 opts 穿到 agent.chat ──────────────


def _make_rest_agent() -> SimpleNamespace:
    agent = SimpleNamespace(
        status=SimpleNamespace(value="idle"),
        chat_calls=[],
        cancel_calls=[],
    )

    async def chat(msg: str, *args, **kwargs) -> dict:
        agent.chat_calls.append((msg, args, kwargs))
        return {"ok": True}

    async def cancel(*_a, **_k) -> None:
        agent.cancel_calls.append(True)

    agent.chat = chat
    agent.cancel = cancel
    return agent


async def _run_rest_send_chat(body: dict, agent: SimpleNamespace) -> dict:
    import hiveweave.api.chat as chat_mod
    import hiveweave.services.off_duty as off_duty_mod

    class FakeChatMsg:
        async def save_message(self, attrs: dict) -> dict:
            return {"id": "m1"}

    class FakeManager:
        def get_agent(self, _agent_id: str):
            return agent

    async def fake_ensure(agent_id: str):
        return agent, {"project_id": "p1"}

    async def fake_off_duty(_agent_id: str) -> bool:
        return False

    orig_ensure = chat_mod._ensure_agent_started
    orig_manager = chat_mod.agent_manager
    orig_chat_msg = chat_mod._chat_msg
    orig_off_duty = off_duty_mod.is_agent_off_duty
    chat_mod._ensure_agent_started = fake_ensure
    chat_mod.agent_manager = FakeManager()
    chat_mod._chat_msg = FakeChatMsg()
    off_duty_mod.is_agent_off_duty = fake_off_duty
    try:
        return await chat_mod.send_chat(
            chat_mod.ChatSendBody(**body)
        )
    finally:
        chat_mod._ensure_agent_started = orig_ensure
        chat_mod.agent_manager = orig_manager
        chat_mod._chat_msg = orig_chat_msg
        off_duty_mod.is_agent_off_duty = orig_off_duty


async def test_rest_send_chat_images_reach_agent_chat_as_internal_format():
    agent = _make_rest_agent()
    resp = await _run_rest_send_chat(
        {
            "agentId": "agent-1",
            "message": "看图",
            "images": ["data:image/png;base64,AAAA"],
        },
        agent,
    )
    assert resp.get("ok") is True
    assert len(agent.chat_calls) == 1
    msg, args, kwargs = agent.chat_calls[0]
    assert json.loads(msg)["content"] == "看图"
    # api/chat.py 经 **chat_extra 传 keyword opts；内部格式不丢不变形；
    # 批 6（I15）：REST 入口显式 source:"chat"
    assert args == ()
    assert kwargs == {
        "opts": {
            "source": "chat",
            "images": [{"media_type": "image/png", "data": "AAAA"}],
        }
    }


async def test_rest_send_chat_no_images_single_arg_call_unchanged():
    """无图路径：批 6 起单参形态 = 仅显式 source 的 opts。"""
    agent = _make_rest_agent()
    await _run_rest_send_chat({"agentId": "agent-1", "message": "纯文本"}, agent)
    _msg, args, kwargs = agent.chat_calls[0]
    assert args == () and kwargs == {"opts": {"source": "chat"}}


# ── 4. _build_messages：opts.images 上本轮 user 消息 ──────────


def _make_build_messages_agent() -> Agent:
    """test_agent_interruption_counting._make_agent 同款轻量构造。"""
    agent = Agent.__new__(Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    agent.config = {"name": "Exec", "role": "executor"}
    agent._conversation = AsyncMock()
    agent._conversation.get_compacted_prefix = lambda *_a, **_k: None
    agent._conversation.get_history = AsyncMock(return_value=[])
    agent._get_identity_prompt = lambda: "identity"
    agent._build_context_prompt = AsyncMock(return_value="")
    agent._pending_resume_hint = None
    return agent


async def test_build_messages_user_turn_carries_images(monkeypatch):
    async def _no_hint(*_a, **_k) -> str:
        return ""

    monkeypatch.setattr(
        "hiveweave.services.turn_exit.build_exit_contract_hint", _no_hint
    )
    agent = _make_build_messages_agent()
    msgs = await agent._build_messages("看图", {"images": USER_IMAGES})
    last = msgs[-1]
    assert last["role"] == "user"
    assert last["images"] == USER_IMAGES  # 内部格式原样进请求
    assert last["content"] == "看图"
    # 其余消息（system/history）不带 images
    assert all("images" not in m for m in msgs[:-1])


async def test_build_messages_no_images_exact_regression(monkeypatch):
    """无图路径逐字回归：user entry 与现状完全一致（无 images 键）。"""
    async def _no_hint(*_a, **_k) -> str:
        return ""

    monkeypatch.setattr(
        "hiveweave.services.turn_exit.build_exit_contract_hint", _no_hint
    )
    agent = _make_build_messages_agent()
    msgs = await agent._build_messages("hello", {})
    assert msgs[-1] == {"role": "user", "content": "hello"}


# ── 5. handle_completion：conversation 用户 turn 带 images ────


def _make_completion_agent() -> Agent:
    agent = Agent.__new__(Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    agent.config = {"name": "Exec", "role": "executor"}
    agent.status = AgentState.PROCESSING
    agent.empty_retry_count = 0
    agent.pending_inbox_msg_ids = None
    agent.current_job = {"started_at": 0}
    agent._cancel_reason = None
    agent._message_queue = []
    agent._streaming_msg_id = None
    agent._resume_cooldown_until = 0.0
    agent._consecutive_errors = 0
    agent._CONSECUTIVE_ERROR_MAX = 3
    agent._resume_suppressed = False
    agent._pending_resume_hint = None
    agent.disposition = "runnable"
    agent._llm_task = None
    agent._safety_timer = None
    agent._on_status_change = None
    agent._on_stream_event = None
    agent._reply_reminder_count = 0
    agent._task_reminder_count = 0
    agent._TASK_REMINDER_MAX = 2
    agent._turn_gate_count = 0
    agent._TURN_GATE_MAX = 1
    agent._slice_budget = 0
    agent._SLICE_BUDGET_MAX = 2
    agent._progress_fingerprint = None
    agent._no_progress_streak = 0
    agent._empty_done_slice_streak = 0
    agent.visibility = "foreground"
    agent._MERGE_WINDOW_MS = 300
    agent._workspace_path = None
    agent._current_run_id = None
    agent._conversation = AsyncMock()
    agent._inbox = AsyncMock()
    agent._org = AsyncMock()
    agent._chat_msg = AsyncMock()
    agent._work_log = AsyncMock()
    agent._run_ledger = AsyncMock()
    return agent


def _completion_result() -> dict:
    return {
        "status": "ok",
        "content": "done",
        "thinking": None,
        "tool_calls": [],
        "tool_turn_messages": [
            {"role": "assistant", "content": "thinking..."},
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": "screenshot",
                "images": [{"media_type": "image/png", "data": "SHOT"}],
            },
        ],
        "rounds": 1,
        "usage": None,
    }


async def test_completion_persists_user_turn_with_images():
    """触发链终点：opts.images → conversation 用户 turn（内部格式）；
    工具截图仍被剥除（in-flight only）。"""
    agent = _make_completion_agent()
    set_pending_turn_result(
        AGENT_ID, {"phase": "done_slice", "summary": "turn done"}
    )
    try:
        with patch(
            "hiveweave.services.task.TaskService.get_open_work_obligations",
            AsyncMock(return_value=[]),
        ), patch(
            "hiveweave.services.task.TaskService.list_delegated_in_flight",
            AsyncMock(return_value=[]),
        ), patch(
            "hiveweave.services.task.TaskService.clear_owner_parked_for_agent",
            AsyncMock(return_value=None),
        ), patch(
            "hiveweave.services.wait_contract.wait_contract_service.clear_waits",
            AsyncMock(return_value=None),
        ), patch.object(
            Agent, "_maybe_self_retrigger", AsyncMock()
        ):
            from hiveweave.agents.completion import handle_completion

            await handle_completion(
                agent,
                _completion_result(),
                json.dumps({"from": "用户", "content": "看图"}, ensure_ascii=False),
                {"images": USER_IMAGES},
            )
    finally:
        clear_pending_turn_result(AGENT_ID)

    agent._conversation.append_turn.assert_awaited_once()
    msgs = agent._conversation.append_turn.await_args.args[2]
    user_turn = msgs[0]
    assert user_turn["role"] == "user"
    assert user_turn["images"] == USER_IMAGES  # 内部格式，不丢不变形
    # 工具截图剥除
    assert all(
        "images" not in m for m in msgs[1:] if isinstance(m, dict)
    )


async def test_completion_no_images_turn_has_no_images_key():
    """无图路径逐字回归：用户 turn 与现状完全一致（无 images 键）。"""
    agent = _make_completion_agent()
    set_pending_turn_result(
        AGENT_ID, {"phase": "done_slice", "summary": "turn done"}
    )
    try:
        with patch(
            "hiveweave.services.task.TaskService.get_open_work_obligations",
            AsyncMock(return_value=[]),
        ), patch(
            "hiveweave.services.task.TaskService.list_delegated_in_flight",
            AsyncMock(return_value=[]),
        ), patch(
            "hiveweave.services.task.TaskService.clear_owner_parked_for_agent",
            AsyncMock(return_value=None),
        ), patch(
            "hiveweave.services.wait_contract.wait_contract_service.clear_waits",
            AsyncMock(return_value=None),
        ), patch.object(
            Agent, "_maybe_self_retrigger", AsyncMock()
        ):
            from hiveweave.agents.completion import handle_completion

            await handle_completion(
                agent, _completion_result(), "user msg", {}
            )
    finally:
        clear_pending_turn_result(AGENT_ID)

    msgs = agent._conversation.append_turn.await_args.args[2]
    assert msgs[0] == {"role": "user", "content": "user msg"}


# ── 6. 剥图语义（messages_without_images keep_user）───────────


def test_messages_without_images_default_strips_all():
    """回归：默认行为与现状一致（user turn 图也剥 — recovery 错误路径）。"""
    msgs = [
        {"role": "user", "content": "u", "images": [{"data": "A"}]},
        {"role": "tool", "tool_call_id": "t", "content": "x",
         "images": [{"data": "B"}]},
    ]
    out = messages_without_images(msgs)
    assert out == [
        {"role": "user", "content": "u"},
        {"role": "tool", "tool_call_id": "t", "content": "x"},
    ]


def test_messages_without_images_keep_user_preserves_user_turn():
    msgs = [
        {"role": "user", "content": "u", "images": USER_IMAGES},
        {"role": "tool", "tool_call_id": "t", "content": "x",
         "images": [{"media_type": "image/png", "data": "SHOT"}]},
    ]
    out = messages_without_images(msgs, keep_user=True)
    assert out[0] == {"role": "user", "content": "u", "images": USER_IMAGES}
    assert "images" not in out[1]
    # 不改输入
    assert msgs[0]["images"] == USER_IMAGES


def test_messages_without_images_no_images_is_identity():
    msgs = [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]
    assert messages_without_images(msgs) == msgs
    assert messages_without_images(msgs, keep_user=True) == msgs


# ── 7. provider：conversation 带图 user turn 的渲染与剥图 ─────


def _openai_handler():
    from hiveweave.llm.provider import OpenAIHandler

    return OpenAIHandler()


def test_provider_renders_conversation_user_images():
    """支持视觉：conversation 里的 user turn images → content array。"""
    body = _openai_handler().build_body(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "看图", "images": USER_IMAGES},
        ],
        model_id="m",
        stream=False,
        supports_images=True,
    )
    wire = body["messages"][-1]
    assert wire["role"] == "user"
    assert isinstance(wire["content"], list)
    types = [p["type"] for p in wire["content"]]
    assert types == ["text", "image_url", "image_url"]
    assert wire["content"][1]["image_url"]["url"] == "data:image/png;base64,AAAA"
    assert wire["content"][2]["image_url"]["url"] == "data:image/jpeg;base64,BBBB"
    # images 键不进请求体
    assert "images" not in wire


def test_provider_strips_conversation_user_images_for_text_only():
    """text-only：conversation user turn 的 images 被剥成文字指引。"""
    body = _openai_handler().build_body(
        messages=[
            {"role": "user", "content": "看图", "images": USER_IMAGES},
        ],
        model_id="m",
        stream=False,
        supports_images=False,
    )
    wire = body["messages"][-1]
    assert isinstance(wire["content"], str)
    assert "看图" in wire["content"]
    assert "image_url" not in json.dumps(wire)
    assert "images" not in wire


# ── 8. 审计收尾：聚合上限 / b64 字符集 / 剥图文案分流 ──────────


def test_parse_user_images_aggregate_cap():
    """P1-1：单条消息总 b64 封顶 8M chars — 6 张各 2M 只收前 4 张。"""
    big = "A" * 2_000_000
    out = parse_user_images([f"data:image/png;base64,{big}"] * 6)
    assert len(out) == 4
    assert all(len(img["data"]) == 2_000_000 for img in out)
    # 边界：4×2M = 8M 恰好等于上限 → 收满不丢
    assert sum(len(i["data"]) for i in out) == MAX_USER_IMAGES_TOTAL_B64_CHARS


def test_parse_user_images_aggregate_cap_in_order():
    """P1-1：按序丢弃，越界后不回填后续小图。"""
    big = "A" * 2_000_000
    out = parse_user_images(
        [
            f"data:image/png;base64,{big}",  # 2M ✓
            f"data:image/png;base64,{big}",  # 4M ✓
            f"data:image/png;base64,{big}",  # 6M ✓
            f"data:image/png;base64,{big}",  # 8M 恰满 ✓
            f"data:image/png;base64,{big}",  # 单张合法但聚合越界 → break
            "data:image/png;base64,SMALL",  # 按序丢弃：虽小也不回填
        ]
    )
    assert len(out) == 4
    assert all(img["data"] == big for img in out)


def test_parse_user_images_bad_charset_skipped():
    """P2-1：b64 含非法字符（@@、! 等）的条目跳过，防网关整包 400。"""
    out = parse_user_images(
        [
            "data:image/png;base64,@@@!",
            "data:image/png;base64,OK==",
            "data:image/png;base64,AB+/12",  # 合法字母表 + padding
        ]
    )
    assert out == [
        {"media_type": "image/png", "data": "OK=="},
        {"media_type": "image/png", "data": "AB+/12"},
    ]


def test_strip_images_note_routed_by_role():
    """P2-3：溢出剥图备注按来源分流 — 用户图不误导 re-screenshot。"""
    msgs = [
        {
            "role": "user",
            "content": "看图",
            "images": [{"media_type": "image/png", "data": "A"}],
        },
        {"role": "assistant", "content": "shot"},
        {
            "role": "tool",
            "tool_call_id": "t1",
            "content": "shot",
            "images": [{"media_type": "image/png", "data": "B"}],
        },
    ]
    out = strip_images_from_messages(msgs, keep_last=0)
    # user 图 → 用户图文案（无「re-screenshot」误导）
    assert "用户消息附带的图片因上下文预算未注入" in out[0]["content"]
    assert "如需像素请让用户重发" in out[0]["content"]
    assert "re-screenshot" not in out[0]["content"]
    # 工具截图 → 原文案保持
    assert "re-screenshot" in out[2]["content"]
    # 幂等：不重复标注
    out2 = strip_images_from_messages(out, keep_last=0)
    assert out2[0]["content"] == out[0]["content"]
    assert out2[2]["content"] == out[2]["content"]


def test_provider_text_only_note_routed_by_source():
    """P2-3：text-only 未注入文案分流 — user 图 vs 工具截图各用其文。"""
    body = _openai_handler().build_body(
        messages=[
            {"role": "user", "content": "看图", "images": USER_IMAGES},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "browse_main", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": "shot",
                "images": [{"media_type": "image/png", "data": "B"}],
            },
        ],
        model_id="m",
        stream=False,
        supports_images=False,
    )
    texts = [
        m["content"] for m in body["messages"] if isinstance(m["content"], str)
    ]
    # user 图 → 用户图文案
    assert any("如需像素请让用户重发" in t for t in texts)
    # 工具截图 → 原「路径仍在」文案保持
    assert any("截图文件路径仍在" in t for t in texts)
    assert not any(
        "让用户重发" in t and "截图文件路径仍在" in t for t in texts
    )


def test_provider_user_note_routed_anthropic_and_gemini():
    """P2-3：三家 provider 的 user 分支统一用用户图文案。"""
    user_msg = {"role": "user", "content": "看图", "images": USER_IMAGES}

    from hiveweave.llm.provider import AnthropicHandler, GoogleHandler

    anthropic_body = AnthropicHandler().build_body(
        messages=[user_msg], model_id="m", stream=False, supports_images=False
    )
    anthropic_text = json.dumps(anthropic_body, ensure_ascii=False)
    assert "如需像素请让用户重发" in anthropic_text
    assert "截图文件路径仍在" not in anthropic_text

    gemini_body = GoogleHandler().build_body(
        messages=[user_msg], model_id="m", stream=False, supports_images=False
    )
    gemini_text = json.dumps(gemini_body, ensure_ascii=False)
    assert "如需像素请让用户重发" in gemini_text
    assert "截图文件路径仍在" not in gemini_text
