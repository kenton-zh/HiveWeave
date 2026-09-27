"""批 10 / I5：旁路请求（turn_summary / compaction_working_set）不再丢工具表。

病灶（fixplan §四 I5(P0-3) / PLATFORM-ISSUES §十五 I5）：
``llm/streamer/context.py`` 两条旁路 ``client.post``（收尾总结
``_make_max_rounds_summary``、工作集头摘要 ``_summarize_working_set_head``）
的 ``build_body`` 把 ``tools`` 设成 ``None`` ⇒ 请求级工具表与主链路不同
⇒ 换缓存域。实测 ``request_type="turn_summary"`` 13 条命中率仅 7.4%，
其中多条与上一请求只隔 10–31 秒（远在缓存窗口内 ⇒ 不是过期，是换域）。

修法照抄 ``conversation/compaction.py`` P1-2 的同构实现（真实 tools 进
``build_body``，不另起一套）。「禁用工具」语义走**对话内撤工具**形态：
上游 pi ``packages/ai/src/api/anthropic-messages.ts:1126-1127``「tools stay
declared and are withdrawn by tool_removal — the request-level list
therefore only grows」在本仓 OpenAI 兼容 wire 上的适配 = 请求级 tools 保真
+ summary prompt 在对话内撤（"Respond with text ONLY. Do NOT attempt any
tool calls."）；旁路响应只读 ``message.content``，模型真回 tool_call 时
落既有 fallback，无新增风险。

判据（可机检）：wire body 里 ``tools`` **非空且与主链路同源** —— 同一
``ProviderConfig.build_body`` 在主链路形态（``stream=True``）与旁路形态
（``stream=False``）下序列化出的 ``tools`` **逐字一致**（openai-compatible
直传 + anthropic 含 cache_control 断点标记的完整序列化各验一份）。

不测真实 provider 缓存行为（那是线上观测面：llm_usage
``cache_read/(input+cache_read) ≥ 0.80``），只测请求侧成因已消除。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.llm.provider import ApiFormat, ProviderConfig
from hiveweave.llm.streamer.context import ContextMixin

TOOLS = [
    {"type": "function", "function": {"name": "read_file"}},
    {"type": "function", "function": {"name": "write_file"}},
]

MESSAGES = [
    {"role": "system", "content": "identity"},
    {"role": "user", "content": "go"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "file body"},
]

_SUMMARY_PAYLOAD = {
    "choices": [{"message": {"content": "summary text"}}],
    "usage": {"prompt_tokens": 500, "completion_tokens": 40},
}


class _Resp:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _CapturingClient:
    """截获旁路 client.post 的 wire body（context.py 用 content= 传 JSON）。

    agent_id 传 None ⇒ ``_record_bypass_usage`` 第一行即返回 ⇒ 零 DB 接触
    （测试不写任何库）。
    """

    def __init__(self, payload: dict | None = None):
        self._payload = payload if payload is not None else _SUMMARY_PAYLOAD
        self.bodies: list[dict] = []
        self.headers: list[dict] = []

    async def post(self, url=None, headers=None, content=None, **kw):
        self.headers.append(headers or {})
        self.bodies.append(json.loads(content.decode("utf-8")))
        return _Resp(self._payload)

    async def aclose(self):
        pass


def _provider(
    api_format: ApiFormat = ApiFormat.OPENAI_COMPATIBLE,
    **kw,
) -> ProviderConfig:
    return ProviderConfig(
        api_format=api_format,
        base_url="https://gw.example/v1",
        api_key="k",
        model_name="m",
        context_window=128_000,
        max_output_tokens=8_192,
        **kw,
    )


def _install_client(provider: ProviderConfig, client: _CapturingClient) -> None:
    provider.build_client = lambda: client  # type: ignore[method-assign]


def _main_chain_tools(provider: ProviderConfig) -> list:
    """主链路形态（stream=True + 同一 tools 列表）的 wire tools 序列化。"""
    return provider.build_body(messages=MESSAGES, stream=True, tools=TOOLS)[
        "tools"
    ]


# ── turn_summary（收尾总结，原 :831-836）──────────────────────────


@pytest.mark.asyncio
async def test_turn_summary_body_keeps_main_chain_tools():
    """旁路收尾总结的 wire body：tools 非空且与主链路逐字同源。"""
    mixin = ContextMixin()

    async def _fire(on_delta, event):
        pass

    mixin._fire_delta = _fire  # 生产由 Streamer 混入
    provider = _provider()
    client = _CapturingClient()
    _install_client(provider, client)

    text = await mixin._make_max_rounds_summary(
        None, provider, MESSAGES, None,
        reason="max_rounds", tools=TOOLS,
    )
    assert text == "summary text"
    body = client.bodies[0]
    assert body.get("tools"), (
        "turn_summary 请求 tools 为空 ⇒ 与主链路不同源 ⇒ 换缓存域（I5 病灶回归）"
    )
    assert body["tools"] == _main_chain_tools(provider), (
        "旁路 tools 必须与主链路序列化逐字一致（同源，非重建）"
    )
    # messages 前缀 = 主链路 messages 整段 + 收尾指令 user turn（对话内撤
    # 工具的载体）：前缀 token 与主请求完全一致才有缓存可谈。
    assert body["messages"][: len(MESSAGES)] == MESSAGES
    assert body["messages"][-1]["role"] == "user"
    assert "Do NOT attempt any tool calls" in body["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_turn_summary_without_tools_kwarg_keeps_old_shape():
    """对照：不传 tools（旧调用方）⇒ wire body 无 tools 键（行为不变）。"""
    mixin = ContextMixin()

    async def _fire(on_delta, event):
        pass

    mixin._fire_delta = _fire
    provider = _provider()
    client = _CapturingClient()
    _install_client(provider, client)

    await mixin._make_max_rounds_summary(
        None, provider, MESSAGES, None, reason="max_rounds",
    )
    assert "tools" not in client.bodies[0]


@pytest.mark.asyncio
async def test_turn_summary_anthropic_tools_match_main_chain():
    """anthropic 形态：含 cache_control 断点标记的 tools 序列化也逐字同源。

    旁路走同一个 ProviderConfig ⇒ supports_prompt_cache 的末工具标记位置
    与主链路一致 ⇒ Anthropic 前缀缓存同样不断。
    """
    mixin = ContextMixin()

    async def _fire(on_delta, event):
        pass

    mixin._fire_delta = _fire
    provider = _provider(ApiFormat.ANTHROPIC, supports_prompt_cache=True)
    client = _CapturingClient()
    _install_client(provider, client)

    await mixin._make_max_rounds_summary(
        None, provider, MESSAGES, None,
        reason="stall_break", stall_reason="tool_failed", tools=TOOLS,
    )
    body = client.bodies[0]
    assert body.get("tools") == _main_chain_tools(provider)
    assert body["tools"][-1].get("cache_control"), (
        "主链路形态的末工具 cache_control 断点必须在旁路 tools 上同样存在"
    )


# ── compaction_working_set（工作集头摘要，原 :486）─────────────────


@pytest.mark.asyncio
async def test_working_set_head_body_keeps_main_chain_tools():
    """工作集头摘要的 wire body：tools 非空且与主链路逐字同源。"""
    provider = _provider()
    client = _CapturingClient()
    _install_client(provider, client)

    text = await ContextMixin._summarize_working_set_head(
        None, provider, "transcript", session_id=None, agent_id=None,
        tools=TOOLS,
    )
    assert text == "summary text"
    body = client.bodies[0]
    assert body.get("tools") == _main_chain_tools(provider), (
        "compaction_working_set 请求 tools 为空 ⇒ 换缓存域（I5 另一处病灶回归）"
    )
    # 消息体仍是独立 transcript 形态（有意不带主前缀，压力路径不重发全量）。
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert "transcript" in body["messages"][0]["content"]


@pytest.mark.asyncio
async def test_working_set_head_without_tools_kwarg_unchanged():
    """对照：不传 tools（旧调用方/summarize 回调路径）⇒ 行为不变。"""
    provider = _provider()
    client = _CapturingClient()
    _install_client(provider, client)

    await ContextMixin._summarize_working_set_head(
        None, provider, "transcript", session_id=None, agent_id=None,
    )
    assert "tools" not in client.bodies[0]


@pytest.mark.asyncio
async def test_pressure_compact_forwards_tools_to_head_summary():
    """_pressure_compact_if_needed 把 tools 透传给工作集头摘要。"""
    from tests.test_prefix_cache_append import _round

    ctx = ContextMixin()
    provider = _provider()
    _, pressure_at, _ = ctx._working_set_budgets(provider)
    body = "z" * 12_000
    messages: list[dict] = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
    ]
    for i in range(30):
        messages.extend(_round(f"c{i}", body))
    assert len(messages) and pressure_at > 0
    # 压力线之上（复用 test_working_set_pressure 的量级构造）。
    from hiveweave.conversation.token_utils import estimate_tokens_for_messages

    assert estimate_tokens_for_messages(messages) >= pressure_at

    spy = AsyncMock(return_value="Goal: continue. Next: finish.")
    with patch.object(ctx, "_summarize_working_set_head", spy):
        await ctx._pressure_compact_if_needed(
            messages, provider, session_id="a1", agent_id="a1", tools=TOOLS,
        )
    assert spy.await_count == 1
    kw = spy.await_args.kwargs
    assert kw.get("tools") == TOOLS, (
        "tool_loop 侧的主链路 tools 必须原样透传到旁路摘要请求（I5 接线）"
    )


# ── 结构守卫：context.py 里 build_body 调用不得再传 tools=None ──────


def test_no_build_body_tools_none_in_context_module():
    """反回归守卫（AST 精确判据，注释/文档字样不误伤）：

    ``llm/streamer/context.py`` 全文件任何 ``build_body(...)`` 调用的
    ``tools`` 关键字不得传 ``None`` 字面量 —— 两条旁路（收尾总结 / 工作集
    头摘要）曾以此丢工具表换缓存域（I5）。打红方式：把任一处改回
    ``tools=None`` ⇒ 本测试必红。
    """
    import ast
    import inspect
    import pathlib

    path = inspect.getfile(ContextMixin)
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_build_body = (
            isinstance(func, ast.Attribute) and func.attr == "build_body"
        ) or (isinstance(func, ast.Name) and func.id == "build_body")
        if not is_build_body:
            continue
        for kw in node.keywords:
            if (
                kw.arg == "tools"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is None
            ):
                offenders.append(f"line {node.lineno}")
    assert not offenders, (
        f"context.py 的 build_body 调用仍在传 tools=None（I5 回归）：{offenders}"
    )


def test_tool_loop_bypass_calls_pass_tools_kwarg():
    """批 10 审计 P2-1：tool_loop 四处旁路调用必须带 ``tools=tools`` 接线。

    ``_make_max_rounds_summary`` / ``_pressure_compact_if_needed`` 的
    ``tools`` 参数是缺省 None —— tool_loop 侧若哪个调用点丢了
    ``tools=tools``，该旁路就静默退回换缓存域（I5 复发），而 context.py
    的守卫（上一条）看不见 tool_loop。AST 断言：tool_loop.py 全文件对这两个
    函数的每次调用都必须出现 ``tools`` 关键字。打红方式：删掉任一处的
    ``tools=tools`` ⇒ 本测试必红。
    """
    import ast
    import inspect
    import pathlib

    from hiveweave.llm.streamer import tool_loop as tool_loop_mod

    path = inspect.getfile(tool_loop_mod)
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    targets = {"_make_max_rounds_summary", "_pressure_compact_if_needed"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name) else None
        )
        if name not in targets:
            continue
        if not any(kw.arg == "tools" for kw in node.keywords):
            offenders.append(f"{name} @ line {node.lineno}")
    assert not offenders, (
        f"tool_loop.py 的旁路调用缺 tools 接线（I5 回归）：{offenders}"
    )
