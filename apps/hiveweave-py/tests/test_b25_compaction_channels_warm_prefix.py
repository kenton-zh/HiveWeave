"""批25 修复3（compaction 洼地 · 暖前缀扩通道）。

批23 已给 working_set 通道接暖前缀（``warm_main_prefix_gate`` 五重暖门
+ ``cache_warmer.latest_snapshot``）；compaction_conversation（实测命中
11.9-13.3%）与 compaction_memory（2.9-10.6%）此前恒 ``prefix_messages=
None`` ⇒ `[{user, prompt}]` 第 0 个 token 即偏离 ⇒ 全价。

修法：``_call_compactor_llm``（两通道唯一咽喉）在未显式传前缀时按
**同一个**五重暖门判定，携带逐字节一致的主前缀 + 同源 tools（不换缓存
域）。开关复用批23 的 ``HIVEWEAVE_COMPACTION_WORKING_SET_WARM_PREFIX``。

契约（镜像 test_p1_9_working_set_warm_prefix 四约束）：
① 有暖快照 ⇒ 两通道请求带逐字节一致前缀（+同源 tools）；
② 冷 run（无快照/换模型域）⇒ 请求与旧行为逐字节一致；
③ 显式传入的 prefix_messages / tools 永不覆盖（P1-2 既有契约不动）；
④ 开关关闭 ⇒ 恒冷回旧行为。

不测真实 provider 缓存行为（线上观测面），只测请求侧成因已消除。
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from hiveweave.llm.provider import ApiFormat, ProviderConfig, provider_factory
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

_MODEL_DICT = {
    "model_id": "m",
    "base_url": "https://gw.example/v1",
    "api_key": "k",
    "provider_type": "openai-compatible",
}

_SNAPSHOT_MC = {
    "model_id": "m",
    "base_url": "https://gw.example/v1",
    "provider_type": "openai-compatible",
}

# usage 缺席 ⇒ 跳过 token metering（零 DB 接触）
_SUMMARY_PAYLOAD = {
    "choices": [{"message": {"content": "summary text"}}],
}


@pytest.fixture(autouse=True)
def _clean_warm_state():
    """cache_warmer 快照是进程级单例 —— 用例间清场。"""
    cache_warmer._latest.clear()
    yield
    cache_warmer._latest.clear()


class _Resp:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _CapturingClient:
    """截获压缩请求的 wire body（httpx.AsyncClient 类替身，bodies 类级共享）。"""

    bodies: list[dict] = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url=None, json=None, headers=None, **kw):
        type(self).bodies.append(json)
        return _Resp(_SUMMARY_PAYLOAD)


def _provider(model_id: str = "m") -> ProviderConfig:
    return ProviderConfig(
        api_format=ApiFormat.OPENAI_COMPATIBLE,
        base_url="https://gw.example/v1",
        api_key="k",
        model_name=model_id,
        context_window=128_000,
        max_output_tokens=8_192,
    )


def _arm_snapshot(agent_id: str, messages: list[dict], **mc_overrides):
    """arming：伪造「上次真实主请求」快照（observe_request 生产形态）。"""
    cache_warmer.observe_request(
        agent_id=agent_id,
        project_id=None,
        model_config={**_SNAPSHOT_MC, **mc_overrides},
        messages=messages,
        tools=TOOLS,
        prompt_tokens=1_000,
    )


async def _call(prompt: str, *, agent_id=None, kind="conversation",
                prefix_messages=None, tools=None,
                model_dict: dict | None = None) -> tuple[str | None, list[dict]]:
    """跑 _call_compactor_llm，返回 (summary, wire bodies)。"""
    from hiveweave.conversation.compaction import _call_compactor_llm

    _CapturingClient.bodies = []
    with (
        patch.object(
            provider_factory,
            "create",
            return_value=_provider(model_id=(model_dict or {}).get("model_id", "m")),
        ),
        patch("httpx.AsyncClient", _CapturingClient),
    ):
        text = await _call_compactor_llm(
            dict(model_dict or _MODEL_DICT),
            prompt,
            agent_id=agent_id,
            kind=kind,
            prefix_messages=prefix_messages,
            tools=tools,
        )
    return text, _CapturingClient.bodies


# ── ① 有暖快照：两通道请求带逐字节一致前缀 ─────────────────────────


@pytest.mark.asyncio
async def test_conversation_channel_warm_prefix_byte_identical():
    _arm_snapshot("a1", MESSAGES)
    prompt = "Create a concise anchored summary ... ## Conversation to summarize:\n..."
    text, bodies = await _call(prompt, agent_id="a1", kind="conversation")
    assert text == "summary text"
    assert len(bodies) == 1
    body = bodies[0]
    assert body["messages"][: len(MESSAGES)] == MESSAGES, (
        "compaction_conversation 前缀必须与主链路请求逐字节一致"
    )
    assert len(body["messages"]) == len(MESSAGES) + 1
    tail = body["messages"][-1]
    assert tail["role"] == "user"
    assert tail["content"] == prompt  # 摘要指令本身不变
    # tools 同源回传（不换缓存域；与主链路 build_body 产物一致）
    ref = _provider().build_body(
        messages=MESSAGES, stream=False, tools=TOOLS
    )
    assert body.get("tools") == ref.get("tools")


@pytest.mark.asyncio
async def test_memory_channel_warm_prefix_byte_identical():
    """compaction_memory 通道（memory.py / task_memory.py 的 kind=memory
    路径同走 _call_compactor_llm 咽喉）同样带逐字节一致前缀。"""
    _arm_snapshot("a2", MESSAGES)
    prompt = "Condense the following memories ...\n- fact one"
    text, bodies = await _call(prompt, agent_id="a2", kind="memory")
    assert text == "summary text"
    body = bodies[0]
    assert body["messages"][: len(MESSAGES)] == MESSAGES
    assert body["messages"][-1]["content"] == prompt


# ── ② 冷 run：请求与旧行为逐字节一致 ───────────────────────────────


@pytest.mark.asyncio
async def test_cold_run_without_snapshot_keeps_old_shape():
    prompt = "Condense memories"
    text, bodies = await _call(prompt, agent_id="a3", kind="memory")
    assert text == "summary text"
    body = bodies[0]
    assert body["messages"] == [{"role": "user", "content": prompt}]
    assert not body.get("tools")


@pytest.mark.asyncio
async def test_model_mismatch_is_cold():
    """专用压缩模型 ≠ 主链路模型 ⇒ 换缓存域 ⇒ 冷 run（不带前缀白烧）。
    快照（主链路 "m"）与 compactor 实际模型（"other"）不同域。"""
    _arm_snapshot("a4", MESSAGES)
    _, bodies = await _call(
        "p",
        agent_id="a4",
        kind="memory",
        model_dict={**_MODEL_DICT, "model_id": "other"},
    )
    assert bodies[0]["messages"] == [{"role": "user", "content": "p"}]


@pytest.mark.asyncio
async def test_snapshot_model_mismatch_is_cold():
    _arm_snapshot("a5", MESSAGES, model_id="other-model")
    _, bodies = await _call("p", agent_id="a5", kind="conversation")
    assert bodies[0]["messages"] == [{"role": "user", "content": "p"}]


@pytest.mark.asyncio
async def test_gateway_mismatch_is_cold():
    _arm_snapshot("a6", MESSAGES, base_url="https://other.example/v1")
    _, bodies = await _call("p", agent_id="a6", kind="conversation")
    assert bodies[0]["messages"] == [{"role": "user", "content": "p"}]


# ── ③ 显式 prefix 永不覆盖（P1-2 契约不动）─────────────────────────


@pytest.mark.asyncio
async def test_explicit_prefix_messages_never_overridden():
    _arm_snapshot("a7", MESSAGES)
    explicit = [{"role": "user", "content": "explicit prefix"}]
    _, bodies = await _call(
        "p", agent_id="a7", kind="conversation", prefix_messages=explicit
    )
    body = bodies[0]
    assert body["messages"][: len(explicit)] == explicit
    assert len(body["messages"]) == len(explicit) + 1


# ── ④ 开关关闭 ⇒ 恒冷（复用批23 总开关）────────────────────────────


@pytest.mark.asyncio
async def test_env_switch_off_is_cold(monkeypatch):
    from hiveweave.config import settings

    _arm_snapshot("a8", MESSAGES)
    monkeypatch.setattr(
        settings, "compaction_working_set_warm_prefix", False
    )
    _, bodies = await _call("p", agent_id="a8", kind="conversation")
    assert bodies[0]["messages"] == [{"role": "user", "content": "p"}]


# ── 暖门单源：两处消费走同一函数 ────────────────────────────────────


def test_warm_gate_is_single_source():
    """批25 抽出的模块级暖门与 ContextMixin 方法同源（方法只委托）。"""
    import inspect

    from hiveweave.llm.streamer.context import (
        ContextMixin,
        warm_main_prefix_gate,
    )

    src = inspect.getsource(
        ContextMixin._warm_main_prefix_for_working_set
    )
    assert "warm_main_prefix_gate" in src
    # 模块级函数可直接消费（compaction 通道入口）
    assert callable(warm_main_prefix_gate)
