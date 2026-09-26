"""P1-2（机制层）：压缩请求**带主前缀 + 真实 tools**。

病灶（§3 P1-2）：`_call_compactor_llm` 用 `messages=[{"role":"user","content":prompt}]`
且 `tools=None` ⇒ **第 0 个 token 就与主请求不同** ⇒ provider 前缀缓存必然 miss
（`cache_read=0`，基线 `inp=110802`）。`_build_compaction_prompt` 的输入还是
`_format_for_summary` 拍平的纯文本，进一步偏离主前缀结构。

本文件只测**机制层**（请求体是不是带着前缀与 tools）；**不测**接线（谁把前缀传进来
—— 那属 store/agent 侧，另半未做，见 worklist §3 备注）。

两格判据：① 传前缀 ⇒ 请求体 `messages` **以前缀开头**、末条是指令、`tools` 非空；
② 不传 ⇒ 请求体与旧行为**逐字一致**（7 处直调与旧回调不受影响）。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hiveweave.conversation.compaction import _call_compactor_llm
from tests.test_compaction_hardening import (  # noqa: F401
    FakeClient,
    _compactor_model,
    _ok_response,
)

PREFIX = [
    {"role": "system", "content": "identity"},
    {"role": "user", "content": "go"},
]
TOOLS = [{"type": "function", "function": {"name": "read_file"}}]


@pytest.mark.asyncio
async def test_prefix_and_tools_are_sent():
    client = FakeClient([_ok_response("summary")])
    with patch("httpx.AsyncClient", return_value=client):
        out = await _call_compactor_llm(
            _compactor_model(),
            "INSTRUCTION",
            prefix_messages=PREFIX,
            tools=TOOLS,
        )
    assert out == "summary"
    body = client.posts[0]["json"]
    assert body["messages"][: len(PREFIX)] == PREFIX, (
        "压缩请求必须以主前缀开头（否则第 0 个 token 即偏离 ⇒ 前缀缓存必 miss）"
    )
    assert body["messages"][-1] == {"role": "user", "content": "INSTRUCTION"}
    assert body.get("tools"), "tools 必须是真的（现状 tools=None 会让 envelope 缺失）"


@pytest.mark.asyncio
async def test_without_prefix_behaviour_is_unchanged():
    """对照：不传前缀/tools ⇒ 请求体与旧行为逐字一致（向后兼容）。"""
    client = FakeClient([_ok_response("summary")])
    with patch("httpx.AsyncClient", return_value=client):
        await _call_compactor_llm(_compactor_model(), "INSTRUCTION")
    body = client.posts[0]["json"]
    assert body["messages"] == [{"role": "user", "content": "INSTRUCTION"}]


# ── 批 G P1-7②：压缩请求与主链路同 session_id ⇒ 同缓存域 ────────
#
# 病灶：`build_headers()` 无参 ⇒ opencode 网关 x-opencode-session 落到
# 模块级 uuid4 兜底 ⇒ 压缩与主链路**不同会话路由** ⇒ 61 次压缩请求
# cache_read 恒 0（report P1-7）。上游机制（pi compaction.ts:649-655）：
# "Reuse caller-supplied routing when available"。


@pytest.mark.asyncio
async def test_compaction_session_matches_main_link():
    """agent_id 穿进 headers：压缩请求的会话头与主链路逐字一致。"""
    agent_id = "agent-a455"
    opencode_model = _compactor_model(
        base_url="https://opencode.ai/zen/go/v1"
    )
    client = FakeClient([_ok_response("summary")])
    with patch("httpx.AsyncClient", return_value=client):
        await _call_compactor_llm(opencode_model, "INSTRUCTION", agent_id=agent_id)
    headers = client.posts[0]["headers"]
    from hiveweave.llm.provider import (
        _OPENCODE_SESSION_HEADER,
        provider_factory,
    )

    # 与主链路同款取值（http_stream.py:272 build_headers(session_id=agent_id)）
    main_link_headers = provider_factory.create(opencode_model).build_headers(
        session_id=agent_id
    )
    assert headers[_OPENCODE_SESSION_HEADER] == agent_id
    assert headers[_OPENCODE_SESSION_HEADER] == (
        main_link_headers[_OPENCODE_SESSION_HEADER]
    ), "压缩与会话头不一致 ⇒ 不同缓存域 ⇒ 前缀缓存必 miss（P1-7②）"


@pytest.mark.asyncio
async def test_compaction_without_agent_id_still_has_stable_session():
    """无 agent_id（7 处直调路径）⇒ 仍带稳定兜底会话键（旧行为不变）。"""
    opencode_model = _compactor_model(
        base_url="https://opencode.ai/zen/go/v1"
    )
    client = FakeClient([_ok_response("summary")])
    with patch("httpx.AsyncClient", return_value=client):
        await _call_compactor_llm(opencode_model, "INSTRUCTION")
    headers = client.posts[0]["headers"]
    from hiveweave.llm.provider import (
        _OPENCODE_SESSION_HEADER,
        _opencode_fallback_session,
    )

    assert headers[_OPENCODE_SESSION_HEADER] == _opencode_fallback_session


@pytest.mark.asyncio
async def test_compaction_non_opencode_gateway_has_no_session_header():
    """非 opencode 网关（ark 等）：headers 零改动（build_headers no-op 路径）。"""
    client = FakeClient([_ok_response("summary")])
    with patch("httpx.AsyncClient", return_value=client):
        await _call_compactor_llm(
            _compactor_model(), "INSTRUCTION", agent_id="agent-a455"
        )
    assert "x-opencode-session" not in client.posts[0]["headers"]


# ── 回调适配判据（按参数名探测，不用 try/except）────────────────


def test_callback_adapter_detects_prefix_support():
    from hiveweave.conversation.compaction import _callback_accepts_prefix

    async def new_cb(prompt, *, prefix_messages=None, tools=None):
        return "S"

    async def legacy_cb(prompt):
        return "S"

    assert _callback_accepts_prefix(new_cb) is True
    assert _callback_accepts_prefix(legacy_cb) is False, (
        "旧回调被误判为支持前缀 ⇒ 带参调用会 TypeError"
    )


def test_callback_adapter_is_not_try_except_based():
    """反回归（结构判据）：探测不得靠 try/except TypeError。

    用 try 探协议会把回调**内部**真实的 TypeError 也吞掉并重试一次，掩盖故障。
    """
    import inspect as _inspect

    from hiveweave.conversation.compaction import _callback_accepts_prefix

    src = _inspect.getsource(_callback_accepts_prefix)
    assert "except TypeError" not in src
    assert "signature" in src


# ── 批 G P1-2（审计 2026-09-26）：anthropic 压缩请求命中记账 ────────
#
# raw 构造此前只透传 cache_creation 键 —— anthropic 的
# ``cache_read_input_tokens`` 没映射进 raw["cache_read"]（openai_wire_cache_read
# 的兜底键）⇒ anthropic 压缩命中被记成 cache_read=0，打在 P1-7②「压缩
# cache_read 由 0 变正」的验收口径上。


@pytest.mark.asyncio
async def test_compaction_anthropic_cache_read_is_metered():
    from unittest.mock import AsyncMock

    from hiveweave.conversation.compaction import _call_compactor_llm
    from hiveweave.services.token_meter import token_meter
    from tests.test_compaction_hardening import FakeClient, FakeResponse

    anthropic_model = _compactor_model(
        base_url="https://api.anthropic.com",
        model_id="claude-sonnet-4-5",
        provider_type="anthropic",
    )
    client = FakeClient([
        FakeResponse(200, {
            "content": [{"type": "text", "text": "summary"}],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 20,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 120,
            },
        }),
    ])
    captured: list[dict] = []

    async def _capture(**kwargs):
        captured.append(kwargs)

    with patch("httpx.AsyncClient", return_value=client), patch.object(
        token_meter, "record_compaction", side_effect=_capture
    ):
        out = await _call_compactor_llm(
            anthropic_model, "INSTRUCTION", agent_id="agent-a455"
        )
    assert out == "summary"
    assert captured, "anthropic 压缩调用必须走 record_compaction 记账"
    call = captured[0]
    assert call["cache_read_tokens"] == 800, (
        f"anthropic 命中必须映射进 cache_read_tokens，实得 "
        f"{call['cache_read_tokens']}（P1-2 病灶回归）"
    )
    assert call["cache_creation_tokens"] == 120
    assert call["input_tokens"] == 1000
