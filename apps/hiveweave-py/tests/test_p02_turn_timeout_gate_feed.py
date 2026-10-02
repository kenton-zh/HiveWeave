"""P0-2：整轮兜底总超时归因为上游级失败 —— 喂熔断器 / T34 连败 / T33 骑乘账。

病灶（qoder 双项目报告，已回源坐实）：``core.stream`` 的外层
``asyncio.wait_for`` 到点 ⇒ ``except TimeoutError`` 分支此前只返回裸
error result —— 下游 ``with_retry``（T33 波次骑乘 / T34 failover）看不见
异常，因此不进重试、不上报熔断器、不触发 failover。实测 13,125 次调用
仅 5 次 failover。

修复语义（三处闸门同缝；防重复计数边界：**轮级**失败仍在
``_run_round_chain`` 上报（C10），整轮兜底超时只在此补一次独立失败）：
- 总超时 ⇒ 熔断器 ``report_failure``（error_code=turn_total_timeout）；
- 总超时 ⇒ T34 连败计数 +1（达阈值 ⇒ ``maybe_switch`` 换模型接管本 run）；
- 总超时 ⇒ T33 骑乘账落 ``llm_retry_exhausted`` 事件（failover_streak 随行）；
- 正常收口 / 用户停止（CancelledError）零误报。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.llm.streamer.constants import TURN_TOTAL_TIMEOUT_ERROR_CODE
from hiveweave.llm.streamer.core import Streamer


# ── 工具（对齐 test_t33_t34_wave_retry_failover.py 风格）─────


def _model(
    uid: str,
    *,
    name: str = "",
    model_id: str = "m1",
    base_url: str = "https://gw.test/v1",
    dialect: str = "openai-compatible",
    is_active: bool = True,
) -> dict:
    return {
        "id": uid,
        "name": name or f"name-{uid}",
        "model_id": model_id,
        "base_url": base_url,
        "api_key": f"key-{uid}",
        "provider_type": dialect,
        "is_active": is_active,
    }


def _mk_pool(*models: dict) -> AsyncMock:
    return AsyncMock(return_value=list(models))


def _inbox_cls(sink: list):
    """InboxService 替身类工厂（patch 替换的是类本身，实例化后记录投递）。"""

    class _Svc:
        async def send_message(self, **kw):
            sink.append(kw)
            return {}

    return _Svc


class _FakeProvider:
    """ProviderConfig 替身（stream() 入口只消费 model_name/provider_type）。"""

    def __init__(self, model_name: str = "fake"):
        self.model_name = model_name
        self.provider_type = "openai-compatible"


def _mk_streamer() -> tuple[Streamer, MagicMock]:
    """stream() 单测公共底座：AsyncMock 熔断器 + 放行 check + fake 工厂。"""
    breaker = MagicMock(
        register=AsyncMock(),
        check=AsyncMock(return_value=SimpleNamespace(allowed=True)),
        cooldown_left_s=MagicMock(return_value=0),
        report_failure=AsyncMock(),
        report_success=AsyncMock(),
        open_for=AsyncMock(),
    )

    class _FakeFactory:
        def create(self, cfg: dict) -> _FakeProvider:
            return _FakeProvider(model_name=str(cfg.get("model_id") or ""))

    streamer = Streamer(
        provider_factory_inst=_FakeFactory(),
        circuit_breaker_inst=breaker,
        retry_handler=MagicMock(),
    )
    # 换模型 cold-start 广播会碰 ChatMessageService —— 单测一律旁路。
    streamer._broadcast_model_cold_start = AsyncMock()  # type: ignore[method-assign]
    return streamer, breaker


async def _stream_with_tool_loop(streamer: Streamer, tool_loop, model_config: dict) -> dict:
    streamer._run_tool_loop = tool_loop  # type: ignore[method-assign]
    return await streamer.stream(
        agent_id="a1",
        messages=[{"role": "user", "content": "hi"}],
        model_config=model_config,
        tools=[],
        on_delta=None,
    )


async def _hang_past_wall(*_a, **_k):
    """外层 wait_for 兜底到点的形态：整轮悬挂被强杀 ⇒ TimeoutError。"""
    raise TimeoutError


_OK_RESULT = {
    "status": "ok",
    "content": "done",
    "thinking": "",
    "tool_calls": [],
    "tool_turn_messages": [],
    "rounds": 1,
    "usage": None,
}


# ── ① 总超时 ⇒ 熔断器收到失败上报 ───────────────────────────


@pytest.mark.asyncio
async def test_turn_total_timeout_reports_breaker_failure():
    """总超时必须按上游级失败上报熔断器（带稳定错误码），不再裸返回。

    result 形状保留（recovery.py 依赖）：error 文案 / timeout_kind='turn' /
    timeout_ms 原样；error_code 为新增机检通道。
    """
    streamer, breaker = _mk_streamer()
    result = await _stream_with_tool_loop(
        streamer, _hang_past_wall, {"name": "primary", "model_id": "m"}
    )

    assert result["status"] == "error"
    assert result["error"] == "请求总超时"
    assert result["timeout_kind"] == "turn"  # F7 语义不动
    assert result["timeout_ms"] and result["timeout_ms"] > 0
    assert result["error_code"] == TURN_TOTAL_TIMEOUT_ERROR_CODE
    breaker.report_failure.assert_awaited_once_with(
        "primary", error_code=TURN_TOTAL_TIMEOUT_ERROR_CODE
    )
    breaker.report_success.assert_not_awaited()


# ── ② 总超时 ⇒ 骑乘计数递增（达阈值 ⇒ failover 触发）────────


@pytest.mark.asyncio
async def test_turn_total_timeout_feeds_streak_and_triggers_failover():
    """同一 run 内 3 次总超时 = 3 次上游级失败：streak 1→2→3，第 3 次达
    阈值 ⇒ maybe_switch 换备用模型接管本 run（notice + 连败归零）；
    骑乘账逐次落 llm_retry_exhausted 事件且 failover_streak 随行。"""
    primary = _model("uuid-A", model_id="longcat")
    backup = _model("uuid-B", model_id="ark")
    notices: list[dict] = []
    audited: list[dict] = []

    class _FakeAudit:
        async def log(self, **kw):
            audited.append(kw)

    streamer, breaker = _mk_streamer()
    streamer._failover_state().bind_primary(primary)

    with (
        patch(
            "hiveweave.services.model.ModelService.list_active_full",
            _mk_pool(primary, backup),
        ),
        patch("hiveweave.services.inbox.InboxService", _inbox_cls(notices)),
        patch("hiveweave.services.event_audit.event_audit", _FakeAudit()),
    ):
        for i in range(1, 4):
            result = await _stream_with_tool_loop(streamer, _hang_past_wall, primary)
            assert result["status"] == "error"
            fo = streamer._failover_state()
            if i < 3:
                assert fo.streak == i, f"第 {i} 次总超时后连败计数须为 {i}"
                assert fo.active_config is None  # 未达阈值不换模型
            else:
                assert fo.active_config is not None
                assert fo.active_config["id"] == "uuid-B"  # 备用接管本 run
                assert fo.streak == 0  # 新模型新梯

    # 骑乘账：每次总超时一条 exhausted 事件，账面 streak 含本次失败
    # （落账先于 maybe_switch 的归零 —— 账面不得被洗掉）。
    timeout_events = [
        e
        for e in audited
        if e["event_type"] == "llm_retry_exhausted"
        and "总超时" in str(e["payload"].get("error", ""))
    ]
    assert [e["payload"]["failover_streak"] for e in timeout_events] == [1, 2, 3]
    assert all(e["agent_id"] == "a1" for e in timeout_events)
    # 熔断器每次总超时都收到失败上报
    assert breaker.report_failure.await_count == 3
    # 诚实原则：切换发生 ⇒ platform_notice 到 agent
    assert len(notices) == 1 and "已切换" in notices[0]["message"]


# ── ③ 正常收口 / 用户停止不误报 ─────────────────────────────


@pytest.mark.asyncio
async def test_normal_completion_does_not_feed_gates():
    """正常收口（tool loop 返回结果）零误报：不上报熔断器、不动连败计数。"""
    streamer, breaker = _mk_streamer()

    async def _ok(*_a, **_k):
        return dict(_OK_RESULT)

    result = await _stream_with_tool_loop(
        streamer, _ok, {"name": "primary", "model_id": "m"}
    )
    assert result["status"] == "ok"
    breaker.report_failure.assert_not_awaited()
    assert streamer._failover_state().streak == 0


@pytest.mark.asyncio
async def test_user_cancel_propagates_without_gate_feed():
    """用户主动停止（CancelledError）不被误判为总超时：异常原样传播，
    三处闸门一律不喂（``except TimeoutError`` 天然不接 BaseException 族）。"""
    streamer, breaker = _mk_streamer()

    async def _cancelled(*_a, **_k):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _stream_with_tool_loop(
            streamer, _cancelled, {"name": "primary", "model_id": "m"}
        )
    breaker.report_failure.assert_not_awaited()
    assert streamer._failover_state().streak == 0
