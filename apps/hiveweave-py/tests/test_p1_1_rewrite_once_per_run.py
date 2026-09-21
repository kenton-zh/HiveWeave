"""P1-1①：同一 run 内**只改写一次**（已改写即短路）。

理由：前缀缓存有效性终止于**首个被改写的 token** —— 本 run 已改写 ⇒ 缓存早已从
那个点失效，**再改一次买不回缓存**，只会把改写点继续前移。故本 run 内其余轮次
回到 append-only（0.95 硬裁与 trim 是独立安全网，不受本条影响）。

两格判据：① 已改写（`_context_rewrote=True`）⇒ **原样返回**（同一对象）；
② 未改写 ⇒ 仍会裁剪（对照组，证明门没有把功能整体关掉）。
"""

from __future__ import annotations

import pytest

from hiveweave.conversation.token_utils import estimate_tokens_for_messages
from tests.test_working_set_pressure import _Ctx, _provider, _round


def _over_pressure() -> list[dict]:
    old_body = "y" * 350_000
    return [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", old_body),
        *_round("c2", "recent-two"),
        *_round("c3", "recent-one"),
    ]


@pytest.mark.asyncio
async def test_already_rewritten_run_short_circuits():
    ctx = _Ctx()
    provider = _provider()
    messages = _over_pressure()
    _, pressure_at, _ = ctx._working_set_budgets(provider)
    assert estimate_tokens_for_messages(messages) >= pressure_at, "夹具需在压力线之上"

    ctx._context_rewrote = True
    out = await ctx._pressure_compact_if_needed(messages, provider)
    assert out is messages, "本 run 已改写后不得再次改写（会继续前移改写点）"


@pytest.mark.asyncio
async def test_fresh_run_still_compacts():
    """对照：未改写过 ⇒ 仍会裁剪（门没有把功能整体关掉）。"""
    ctx = _Ctx()
    provider = _provider()
    messages = _over_pressure()
    ctx._context_rewrote = False
    out = await ctx._pressure_compact_if_needed(messages, provider)
    assert estimate_tokens_for_messages(out) < estimate_tokens_for_messages(messages), (
        "未改写过时应正常裁剪"
    )
