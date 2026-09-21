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
