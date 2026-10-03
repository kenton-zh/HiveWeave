"""批23 P1-9：working_set 压缩请求携带已暖主前缀（缓存命中率拖累项修复）。

病灶（qoder L7 节 + R3）：``compaction_working_set`` 命中仅 33-43%、
``drift_zero_hit=10`` —— 工作集头摘要请求刻意不带主前缀（批10/I5 只对齐
了 tools 半），每次全价重写长 transcript。

修法（qoder 处方，镜像 ``conversation/compaction.py`` P1-2 的
``prefix_messages`` 同构先例）：直连路径按暖门判定携带**逐字节一致**的
主前缀（命中缓存读 1/10 价 + 保持前缀热）。四约束：

① 主前缀字节与主链路逐字节一致（不引入新改写点 —— messages 原样透传）；
② 冷 run 保持现状（无快照 / 超缓存窗口 / 换缓存域 / 前缀被改写 ⇒ None
   ⇒ 请求与旧行为逐字节一致）；
③ summary 输出语义零变化（摘要指令 + transcript 不变）；
④ env 开关默认开（HIVEWEAVE_COMPACTION_WORKING_SET_WARM_PREFIX）。

不测真实 provider 缓存行为（线上观测面），只测请求侧成因已消除。
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.llm.provider import ApiFormat, ProviderConfig
from hiveweave.llm.streamer.context import ContextMixin
from hiveweave.services.cache_warmer import cache_warmer

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

_MODEL_CONFIG = {
    "model_id": "m",
    "base_url": "https://gw.example/v1",
    "provider_type": "openai-compatible",
}


@pytest.fixture(autouse=True)
def _clean_warm_state():
    """cache_warmer 快照是进程级单例 —— 用例间清场。"""
    cache_warmer._latest.clear()
    yield
    cache_warmer._latest.clear()


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
    """截获旁路 client.post 的 wire body。agent_id=None ⇒ 零 DB 接触。"""

    def __init__(self, payload: dict | None = None):
        self._payload = payload if payload is not None else _SUMMARY_PAYLOAD
        self.bodies: list[dict] = []

    async def post(self, url=None, headers=None, content=None, **kw):
        self.bodies.append(json.loads(content.decode("utf-8")))
        return _Resp(self._payload)

    async def aclose(self):
        pass


def _provider(
    api_format: ApiFormat = ApiFormat.OPENAI_COMPATIBLE,
    model_name: str = "m",
    base_url: str = "https://gw.example/v1",
    **kw,
) -> ProviderConfig:
    return ProviderConfig(
        api_format=api_format,
        base_url=base_url,
        api_key="k",
        model_name=model_name,
        context_window=128_000,
        max_output_tokens=8_192,
        **kw,
    )


def _arm_snapshot(agent_id: str, messages: list[dict], **mc_overrides):
    """arming：伪造「上次真实请求」快照（observe_request 的生产调用形态）。"""
    mc = {**_MODEL_CONFIG, **mc_overrides}
    cache_warmer.observe_request(
        agent_id=agent_id,
        project_id=None,
        model_config=mc,
        messages=messages,
        tools=TOOLS,
        prompt_tokens=1_000,
    )


# ── 约束①：暖前缀逐字节一致 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_warm_prefix_byte_identical_in_wire_body():
    """有暖前缀：wire body 的 messages = 主前缀逐字节 + 摘要 user turn。"""
    provider = _provider()
    client = _CapturingClient()
    provider.build_client = lambda: client  # type: ignore[method-assign]
    prefix = list(MESSAGES)

    text = await ContextMixin._summarize_working_set_head(
        None, provider, "transcript body", session_id="a1", agent_id=None,
        tools=TOOLS, prefix_messages=prefix,
    )
    assert text == "summary text"
    body = client.bodies[0]
    assert body["messages"][: len(prefix)] == prefix, (
        "主前缀必须与主链路请求逐字节一致（P1-9 约束①，不引入新改写点）"
    )
    assert len(body["messages"]) == len(prefix) + 1
    tail = body["messages"][-1]
    assert tail["role"] == "user"
    # 约束③：摘要指令与 transcript 不变。
    assert "transcript body" in tail["content"]
    assert "Summarize the following" in tail["content"]
    # tools 同源纪律不回退（I5 批次契约）。
    assert body.get("tools") == provider.build_body(
        messages=MESSAGES, stream=True, tools=TOOLS
    )["tools"]


@pytest.mark.asyncio
async def test_pressure_compact_forwards_warm_prefix():
    """暖 run：_pressure_compact_if_needed 把当前 messages 原样作前缀。"""
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

    _arm_snapshot("a1", messages[:5])  # 上次请求 = 当前 history 的前缀

    spy = AsyncMock(return_value="Goal: continue. Next: finish.")
    with patch.object(ctx, "_summarize_working_set_head", spy):
        await ctx._pressure_compact_if_needed(
            messages, provider, session_id="a1", agent_id="a1", tools=TOOLS,
        )
    assert spy.await_count == 1
    kwargs = spy.await_args.kwargs
    assert kwargs.get("prefix_messages") is messages, (
        "暖 run 的工作集摘要请求必须携带当前主链路 messages（原样、同源）"
    )


# ── 约束②：冷 run 保持现状 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cold_run_without_snapshot_keeps_old_shape():
    """无快照（冷 run）：请求与旧行为逐字节一致（transcript 单消息）。"""
    provider = _provider()
    client = _CapturingClient()
    provider.build_client = lambda: client  # type: ignore[method-assign]

    text = await ContextMixin._summarize_working_set_head(
        None, provider, "transcript body", session_id=None, agent_id=None,
        tools=TOOLS, prefix_messages=None,
    )
    assert text == "summary text"
    body = client.bodies[0]
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert "transcript body" in body["messages"][0]["content"]


@pytest.mark.asyncio
async def test_pressure_compact_cold_run_passes_none_prefix():
    """冷 run：_pressure_compact_if_needed 传 prefix_messages=None。"""
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
    from hiveweave.conversation.token_utils import estimate_tokens_for_messages

    assert estimate_tokens_for_messages(messages) >= pressure_at
    # 故意不 arm 快照 ⇒ 冷 run。

    spy = AsyncMock(return_value="ok summary")
    with patch.object(ctx, "_summarize_working_set_head", spy):
        await ctx._pressure_compact_if_needed(
            messages, provider, session_id="a1", agent_id="a1", tools=TOOLS,
        )
    assert spy.await_args.kwargs.get("prefix_messages") is None


# ── 暖门判定单元（_warm_main_prefix_for_working_set）───────────────


def test_gate_happy_path_returns_messages():
    ctx = ContextMixin()
    provider = _provider()
    _arm_snapshot("a1", MESSAGES[:2])
    assert ctx._warm_main_prefix_for_working_set(
        "a1", provider, list(MESSAGES)
    ) is not None


def test_gate_stale_snapshot_is_cold():
    """超协议缓存窗口（openai 系 5min）⇒ 冷 run。"""
    ctx = ContextMixin()
    provider = _provider()
    _arm_snapshot("a1", MESSAGES)
    cache_warmer._latest["a1"]["ts"] = time.monotonic() - 400.0
    assert ctx._warm_main_prefix_for_working_set(
        "a1", provider, list(MESSAGES)
    ) is None


def test_gate_model_mismatch_is_cold():
    """failover 换模型 ⇒ 换缓存域 ⇒ 冷 run。"""
    ctx = ContextMixin()
    provider = _provider()
    _arm_snapshot("a1", MESSAGES, model_id="other-model")
    assert ctx._warm_main_prefix_for_working_set(
        "a1", provider, list(MESSAGES)
    ) is None


def test_gateway_mismatch_is_cold():
    """跨网关（base_url 变）⇒ 换缓存域 ⇒ 冷 run。"""
    ctx = ContextMixin()
    provider = _provider()
    _arm_snapshot("a1", MESSAGES, base_url="https://other.example/v1")
    assert ctx._warm_main_prefix_for_working_set(
        "a1", provider, list(MESSAGES)
    ) is None


def test_gate_protocol_without_ttl_evidence_is_cold():
    """无 TTL 证据的协议（google）不判暖 —— 宁可冷跑不盲发。"""
    ctx = ContextMixin()
    provider = _provider(api_format=ApiFormat.GOOGLE)
    _arm_snapshot("a1", MESSAGES)
    assert ctx._warm_main_prefix_for_working_set(
        "a1", provider, list(MESSAGES)
    ) is None


def test_gate_rewritten_prefix_is_cold():
    """前缀被改写（快照不再是当前 messages 的前缀）⇒ 不带。"""
    ctx = ContextMixin()
    provider = _provider()
    rewritten = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "REWRIDDEN history"},
    ]
    _arm_snapshot("a1", rewritten)
    assert ctx._warm_main_prefix_for_working_set(
        "a1", provider, list(MESSAGES)
    ) is None


def test_gate_env_switch_off_is_cold(monkeypatch):
    """约束④：开关关闭 ⇒ 恒冷（回切旧行为）。"""
    from hiveweave.config import settings

    ctx = ContextMixin()
    provider = _provider()
    _arm_snapshot("a1", MESSAGES)
    monkeypatch.setattr(
        settings, "compaction_working_set_warm_prefix", False
    )
    assert ctx._warm_main_prefix_for_working_set(
        "a1", provider, list(MESSAGES)
    ) is None
