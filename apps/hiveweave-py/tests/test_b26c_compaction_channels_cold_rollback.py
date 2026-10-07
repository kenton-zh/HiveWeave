"""批26 修复4：compaction 两通道暖前缀接线**回退**（批25 修复3 实测证伪）。

第五轮 d89 诊断结论（详见 ``llm/streamer/context.py::
warm_main_prefix_gate`` docstring「批26 结论」）：

1. conversation 通道：completion.py 的 P1-2 接线恒显式传
   ``prefix_messages=[identity system]`` ⇒ 批25 的「仅当 prefix_messages
   is None 才判暖」分支结构性死代码，主路径上暖门从未执行；
2. memory 通道：暖门 attach 全量快照前缀的唯一干净样本 provider 端
   cache_read=143 / inp=491861 —— 前缀逐字节同源也命中不了（opencode zen
   免费档对非主链路请求形态的前缀缓存不生效）；
3. 换域判冷（压缩模型≠主模型）不是主因：30/30 压缩行与主链路模型一致。

⇒ 请求侧救不了 ⇒ 拆除 ``warm_compaction_prefix`` 及 ``_call_compactor_llm``
的接线，compaction_conversation / compaction_memory 恢复冷形态；
**working_set 通道不受影响**（同一暖门 + 快照机制原样保留）。

契约（可机检）：
① 两通道 prefix_messages=None ⇒ 请求恒冷 `[{user, prompt}]`（即使有暖快照）；
② 显式传入的 prefix_messages / tools 永不覆盖（P1-2 既有契约不动）；
③ ``warm_compaction_prefix`` 已不存在（防复活）；
④ 暖门本身健在且 working_set 消费方委托不变。
"""

from __future__ import annotations

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


# ── ① 两通道恢复冷形态（即使有暖快照也不带前缀）──────────────────────


@pytest.mark.asyncio
async def test_conversation_channel_is_cold_even_with_warm_snapshot():
    """回退核心断言：暖快照在 ⇒ 请求仍与旧行为逐字节一致（接线已拆）。"""
    _arm_snapshot("a1", MESSAGES)
    prompt = "Create a concise anchored summary ... ## Conversation to summarize:\n..."
    text, bodies = await _call(prompt, agent_id="a1", kind="conversation")
    assert text == "summary text"
    assert len(bodies) == 1
    body = bodies[0]
    assert body["messages"] == [{"role": "user", "content": prompt}], (
        "compaction_conversation 必须恢复冷形态（不带主前缀白烧 input）"
    )
    assert not body.get("tools")


@pytest.mark.asyncio
async def test_memory_channel_is_cold_even_with_warm_snapshot():
    _arm_snapshot("a2", MESSAGES)
    prompt = "Condense the following memories ...\n- fact one"
    text, bodies = await _call(prompt, agent_id="a2", kind="memory")
    assert text == "summary text"
    assert bodies[0]["messages"] == [{"role": "user", "content": prompt}]


@pytest.mark.asyncio
async def test_cold_without_snapshot_keeps_old_shape():
    text, bodies = await _call("p", agent_id="a3", kind="memory")
    assert text == "summary text"
    assert bodies[0]["messages"] == [{"role": "user", "content": "p"}]
    assert not bodies[0].get("tools")


# ── ② 显式 prefix 永不覆盖（P1-2 契约不动）─────────────────────────


@pytest.mark.asyncio
async def test_explicit_prefix_messages_never_overridden():
    _arm_snapshot("a4", MESSAGES)
    explicit = [{"role": "user", "content": "explicit prefix"}]
    _, bodies = await _call(
        "p", agent_id="a4", kind="conversation", prefix_messages=explicit
    )
    body = bodies[0]
    assert body["messages"][: len(explicit)] == explicit
    assert len(body["messages"]) == len(explicit) + 1


# ── ③ warm_compaction_prefix 已拆除（防复活）────────────────────────


def test_warm_compaction_prefix_is_gone():
    """批25 修复3 的接线函数不得复活（要接先推翻批26 诊断结论）。"""
    import hiveweave.conversation.compaction as compaction_mod

    assert not hasattr(compaction_mod, "warm_compaction_prefix"), (
        "warm_compaction_prefix 复活 ⇒ 必须先推翻批26 修复4 的诊断结论"
        "（见 warm_main_prefix_gate docstring「批26 结论」）"
    )


def test_no_attach_wiring_in_throat():
    """咽喉函数内不得再引用暖门 / attach 日志（AST 级防回归）。"""
    import ast
    import inspect

    from hiveweave.conversation import compaction as compaction_mod

    src = inspect.getsource(compaction_mod)
    tree = ast.parse(src)
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert "warm_compaction_prefix" not in names
    assert "warm_main_prefix_gate" not in names
    assert "compaction_warm_prefix_attached" not in src


# ── ④ working_set 通道不受影响（暖门 + 委托健在）────────────────────


def test_warm_gate_still_attaches_for_working_set():
    """同一份快照 + 同域 provider ⇒ 暖门照常返回前缀（working_set 语义）。"""
    from hiveweave.llm.streamer.context import warm_main_prefix_gate

    _arm_snapshot("a5", MESSAGES)
    provider = _provider("m")
    # working_set 生产形态：messages 是当前主链路消息（快照是其前缀）
    current = MESSAGES + [{"role": "user", "content": "next round"}]
    assert warm_main_prefix_gate("a5", provider, current) is not None


def test_warm_gate_still_cold_on_domain_mismatch():
    from hiveweave.llm.streamer.context import warm_main_prefix_gate

    _arm_snapshot("a6", MESSAGES)
    provider = _provider("other-model")
    current = MESSAGES + [{"role": "user", "content": "next round"}]
    assert warm_main_prefix_gate("a6", provider, current) is None


def test_warm_gate_is_single_source():
    """批23 抽出的模块级暖门与 ContextMixin 方法同源（方法只委托）。"""
    import inspect

    from hiveweave.llm.streamer.context import (
        ContextMixin,
        warm_main_prefix_gate,
    )

    src = inspect.getsource(
        ContextMixin._warm_main_prefix_for_working_set
    )
    assert "warm_main_prefix_gate" in src
    assert callable(warm_main_prefix_gate)
