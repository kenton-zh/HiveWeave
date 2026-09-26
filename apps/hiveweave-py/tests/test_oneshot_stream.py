"""批 B·第 1 步①：oneshot 审计/评审链路流式化回归测试。

背景（审计 P0·L6）：request_code_audit 旧走非流式单发 HTTP，httpx read
等效「总时长墙」——慢 = 死，69-86% 调用整齐卡死在 111-112s。批 B 把
oneshot 链路迁到 llm/streamer/oneshot（SSE 流式 + idle 看门狗 + 独立总
时长帽 + 重试只对连接失败/立即拒绝）+ llm_usage 记账。

快测缩放：全部 mock 传输（无真实网络），事件间隔用 ``time.sleep(0.01)``
级、idle/总帽用参数或 env 调小（0.2~0.3s 级）——「总时长超过旧 90s read
帽」的形状按比例缩到亚秒级复现。

上游裁决：pi / DSH / opencode 均无 request_code_audit 同类工具
（HiveWeave 特有）；流式机制复用本仓主链路机件。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import hiveweave.agents.agent as agent_mod
import hiveweave.llm.streamer.oneshot as oneshot_mod
from hiveweave.db import project as project_db
from hiveweave.llm.provider import provider_factory
from hiveweave.llm.retry import PermanentError, RetryableError
from hiveweave.llm.streamer.oneshot import (
    OneshotIdleTimeout,
    OneshotTotalTimeout,
    record_oneshot_usage,
    stream_oneshot,
    stream_oneshot_with_retry,
)
from hiveweave.services.code_audit import _invoke_audit_llm

PROJECT_ID = "oneshot-proj"
AGENT_ID = "agent-oneshot"
MODEL_CFG = {"base_url": "https://gw.fake/v1", "api_key": "sk-test", "model_id": "gpt-test"}


# ── SSE / 传输替身 ──────────────────────────────────────────


def _sse_text(content: str) -> bytes:
    return (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": content}}]})
        + "\n\n"
    ).encode()


_SSE_DONE = b"data: [DONE]\n\n"


class _FakeSseResponse:
    """流式响应替身：按事件列表滴事件，可模拟慢流/中途停滞/永久滴流。"""

    status_code = 200
    headers: dict = {}

    def __init__(
        self,
        events: list[bytes] | None = None,
        *,
        interval_s: float = 0.0,
        stall_after: int | None = None,
        die_after: BaseException | None = None,
        repeat_last: bool = False,
    ):
        self._events = events or [_sse_text("x")]
        self._interval = interval_s
        self._stall_after = stall_after
        self._die_after = die_after
        self._repeat_last = repeat_last
        self._close_evt = threading.Event()

    def close(self) -> None:
        self._close_evt.set()

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> "_FakeSseResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def iter_bytes(self):  # type: ignore[no-untyped-def]
        n = len(self._events)
        for i in range(n):
            if self._close_evt.is_set():
                return
            if self._interval:
                time.sleep(self._interval)
            if self._stall_after is not None and i >= self._stall_after:
                # 停滞：等看门狗关流（close 置位）后退出
                self._close_evt.wait()
                return
            if self._die_after is not None and i == n - 1:
                raise self._die_after
            yield self._events[i]
        if self._stall_after is not None and self._stall_after >= n:
            # 语义「吐完 stall_after 个事件后停滞」：stall_after ≥ 事件数时
            # 在 yield 完全部事件后停滞。
            self._close_evt.wait()
        if self._repeat_last:
            last = self._events[-1]
            while not self._close_evt.is_set():
                time.sleep(self._interval or 0.01)
                yield last


class _FakeClient:
    """httpx.Client 替身：按 behaviors 顺序应答；close 联动当前响应。"""

    def __init__(self, timeout: object = None) -> None:
        self.timeout = timeout

    def stream(self, method: str, url: str, headers: object = None,
               content: object = None):  # noqa: ARG002 — 签名对齐
        state = _STATE["holder"]
        state["attempts"] += 1
        behavior = state["behaviors"].pop(0)
        state["current"] = behavior if isinstance(behavior, _FakeSseResponse) else None
        return behavior

    def close(self) -> None:
        current = _STATE["holder"].get("current")
        if current is not None:
            current.close()


_STATE: dict = {}


def _install_transport(behaviors: list):  # type: ignore[type-arg]
    """把 httpx.Client 换成按 behaviors 应答的替身（monkeypatch 自动还原）。"""
    state: dict = {"behaviors": behaviors, "attempts": 0, "current": None}
    _STATE["holder"] = state
    return patch("httpx.Client", lambda timeout=None: _FakeClient(timeout)), state


def _ok_response(text: str = "audit ok") -> _FakeSseResponse:
    return _FakeSseResponse([_sse_text(text), _SSE_DONE])


@pytest.fixture(autouse=True)
def _isolate_state():
    yield
    _STATE.pop("holder", None)


@pytest.fixture(autouse=True)
def _fast_sleep():
    """重试退避不真睡（0.5-1s → 0），与旧 retry 测试同一手法。"""
    with patch.object(asyncio, "sleep", new=AsyncMock()):
        yield


# ── 1. 慢而活着：事件间隔恒小于 idle 帽 ⇒ 成功（旧 read 总时长墙必杀）──


@pytest.mark.asyncio
async def test_slow_but_alive_stream_succeeds():
    """120 事件 × 0.01s ≈ 1.2s 总时长（> 缩放后的旧 read 帽 0.5s），
    但事件间隔 0.01s << idle 帽 0.5s ⇒ 流式判活，完整内容返回。"""
    n = 120
    events = [_sse_text("a") for _ in range(n)] + [_SSE_DONE]
    behaviors = [_FakeSseResponse(events, interval_s=0.01)]
    p, state = _install_transport(behaviors)
    provider = provider_factory.create(MODEL_CFG)
    with p:
        result = await stream_oneshot(
            provider,
            [{"role": "user", "content": "audit me"}],
            agent_id=AGENT_ID,
            first_chunk_timeout_s=0.5,
            idle_timeout_s=0.5,
        )
    assert result["text"] == "a" * n
    assert result["duration_ms"] >= 1000
    assert state["attempts"] == 1


@pytest.mark.asyncio
async def test_slow_but_alive_default_idle_cap_uses_main_link_semantics():
    """idle 帽缺省走 oneshot_idle_timeout_s()（对齐主链路 150s 语义）；
    env 收紧后同样生效（审计场景独立配置口）。"""
    events = [_sse_text("b"), _sse_text("b"), _SSE_DONE]
    behaviors = [_FakeSseResponse(events, interval_s=0.02)]
    p, _state = _install_transport(behaviors)
    provider = provider_factory.create(MODEL_CFG)
    import os

    old = os.environ.get("HIVEWEAVE_ONESHOT_IDLE_TIMEOUT_S")
    os.environ["HIVEWEAVE_ONESHOT_IDLE_TIMEOUT_S"] = "1"
    try:
        with p:
            result = await stream_oneshot(
                provider, [{"role": "user", "content": "x"}], agent_id=AGENT_ID,
            )
    finally:
        if old is None:
            os.environ.pop("HIVEWEAVE_ONESHOT_IDLE_TIMEOUT_S", None)
        else:
            os.environ["HIVEWEAVE_ONESHOT_IDLE_TIMEOUT_S"] = old
    assert result["text"] == "bb"


# ── 2. idle 判死：流中途停发超过 idle 帽 ⇒ 判死 + idle 归因 + 不重试 ──


@pytest.mark.asyncio
async def test_idle_stall_is_attributed_and_not_retried():
    """2 个事件后停滞 > idle 帽 ⇒ OneshotIdleTimeout（layer=idle），
    且**不重试**（慢而沉默不再触发整段重发）。"""
    behaviors = [
        _FakeSseResponse([_sse_text("x"), _sse_text("y")], stall_after=2),
        _ok_response("never reached"),
    ]
    p, state = _install_transport(behaviors)
    provider = provider_factory.create(MODEL_CFG)
    with p, pytest.raises(OneshotIdleTimeout) as exc_info:
        await stream_oneshot_with_retry(
            provider,
            [{"role": "user", "content": "x"}],
            agent_id=AGENT_ID,
            total_timeout_s=30,
            first_chunk_timeout_s=0.2,
            idle_timeout_s=0.2,
        )
    assert exc_info.value.timeout_layer == "idle"
    assert "idle" in str(exc_info.value)
    assert state["attempts"] == 1


@pytest.mark.asyncio
async def test_near_budget_silence_attributed_idle_not_total():
    """P2-2（独立审计）：剩余预算 < idle 帽时真沉默归因 **idle** 而非 total。

    total 只留给「事件仍在流动时预算耗尽」——沉默 = 慢而死了，归 total
    会误导诊断（以为多给预算就能完成）。场景：2 个事件耗 ~0.03s 后流
    停滞，剩余 ≈0.22s < idle 帽 0.5s ⇒ 等待被预算钳到 0.22s 内到点，
    归因必须是 idle（旧实现此处归 total，即本回归钉住的失真）。"""
    behaviors = [
        _FakeSseResponse(
            [_sse_text("x"), _sse_text("y")], interval_s=0.01, stall_after=2
        ),
        _ok_response("never reached"),
    ]
    p, state = _install_transport(behaviors)
    provider = provider_factory.create(MODEL_CFG)
    with p, pytest.raises(OneshotIdleTimeout) as exc_info:
        await stream_oneshot_with_retry(
            provider,
            [{"role": "user", "content": "x"}],
            agent_id=AGENT_ID,
            total_timeout_s=0.25,   # 事件后剩余 < idle 帽 0.5s ⇒ 等待被钳
            idle_timeout_s=0.5,
            first_chunk_timeout_s=0.5,
        )
    assert exc_info.value.timeout_layer == "idle"
    assert "idle" in str(exc_info.value)
    assert "silent" in str(exc_info.value)
    assert state["attempts"] == 1


@pytest.mark.asyncio
async def test_first_chunk_stall_attributed_first_chunk():
    """首事件前停滞 ⇒ first_chunk 归因（批 D timeout_layer 事实位取值）。"""
    behaviors = [_FakeSseResponse([_sse_text("x")], stall_after=0)]
    p, _state = _install_transport(behaviors)
    provider = provider_factory.create(MODEL_CFG)
    with p, pytest.raises(OneshotIdleTimeout) as exc_info:
        await stream_oneshot(
            provider,
            [{"role": "user", "content": "x"}],
            agent_id=AGENT_ID,
            total_timeout_s=30,
            first_chunk_timeout_s=0.2,
        )
    assert exc_info.value.timeout_layer == "first_chunk"
    assert "first chunk" in str(exc_info.value)


# ── 3. 总时长闸：流持续滴事件但总墙钟超帽 ⇒ 判死 + total 归因 + env 真闸 ──


@pytest.mark.asyncio
async def test_total_timeout_while_dripping_attributed_total():
    """滴流不停（间隔 0.01s << idle 帽）但总墙钟 0.3s 到点 ⇒
    OneshotTotalTimeout（layer=total），不重试。"""
    behaviors = [_FakeSseResponse(interval_s=0.01, repeat_last=True)]
    p, state = _install_transport(behaviors)
    provider = provider_factory.create(MODEL_CFG)
    with p, pytest.raises(OneshotTotalTimeout) as exc_info:
        await stream_oneshot_with_retry(
            provider,
            [{"role": "user", "content": "x"}],
            agent_id=AGENT_ID,
            total_timeout_s=0.3,
            idle_timeout_s=5,
            first_chunk_timeout_s=5,
        )
    assert exc_info.value.timeout_layer == "total"
    assert "total timeout" in str(exc_info.value)
    assert state["attempts"] == 1


@pytest.mark.asyncio
async def test_code_audit_timeout_env_is_a_real_gate(monkeypatch):
    """env HIVEWEAVE_CODE_AUDIT_TIMEOUT_S 变真闸：调小后 wrapper 实时
    提前判死（旧实现内层 110s 恒 < 外层 120s ⇒ env 永不生效）。"""
    monkeypatch.setenv("HIVEWEAVE_CODE_AUDIT_TIMEOUT_S", "0.3")
    behaviors = [_FakeSseResponse(interval_s=0.01, repeat_last=True)]
    p, _state = _install_transport(behaviors)
    provider = provider_factory.create(MODEL_CFG)
    with p, pytest.raises(OneshotTotalTimeout):
        await stream_oneshot_with_retry(
            provider, [{"role": "user", "content": "x"}], agent_id=AGENT_ID,
        )


# ── 4. 重试语义：只对连接失败 / 立即拒绝 ────────────────────────


@pytest.mark.asyncio
async def test_connect_error_retried_once_then_success():
    """连接失败（流未开口）→ 退避后重试一次成功。"""
    behaviors = [httpx.ConnectError("connection refused"), _ok_response("recovered")]
    p, state = _install_transport(behaviors)
    provider = provider_factory.create(MODEL_CFG)
    with p:
        result = await stream_oneshot_with_retry(
            provider, [{"role": "user", "content": "x"}], agent_id=AGENT_ID,
        )
    assert result["text"] == "recovered"
    assert state["attempts"] == 2


@pytest.mark.asyncio
async def test_429_immediate_rejection_retries_with_retry_after():
    """429 立即拒绝 → 尊重 Retry-After 退避后重试成功。"""
    # 非 200 分支替身（带头），走 stream_oneshot 的 classify 分支
    class _ErrResponse:
        def __init__(self) -> None:
            self.status_code = 429
            self.headers = {"retry-after": "1"}

        def read(self) -> bytes:
            return b'{"error":{"message":"rate limited"}}'

        def __enter__(self) -> "_ErrResponse":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def iter_bytes(self):  # type: ignore[no-untyped-def]
            return iter(())

    behaviors: list = [_ErrResponse(), _ok_response("after 429")]
    p, state = _install_transport(behaviors)
    provider = provider_factory.create(MODEL_CFG)
    with p:
        result = await stream_oneshot_with_retry(
            provider, [{"role": "user", "content": "x"}], agent_id=AGENT_ID,
        )
    assert result["text"] == "after 429"
    assert state["attempts"] == 2
    # Retry-After: 1s 被尊重（退避 mock 上断言）
    import hiveweave.llm.streamer.oneshot as om

    sleeps = [
        c.args[0] for c in om.asyncio.sleep.await_args_list if c.args
    ] if om.asyncio.sleep.await_count else []
    assert 1.0 in sleeps, sleeps


@pytest.mark.asyncio
async def test_midstream_death_not_retried():
    """流已开口后传输死亡 → 不重试（重发 = 整段生成重来）。"""
    behaviors = [
        _FakeSseResponse([_sse_text("part"), _SSE_DONE], die_after=httpx.ReadError("dropped")),
        _ok_response("never reached"),
    ]
    p, state = _install_transport(behaviors)
    provider = provider_factory.create(MODEL_CFG)
    with p, pytest.raises(RetryableError):
        await stream_oneshot_with_retry(
            provider, [{"role": "user", "content": "x"}], agent_id=AGENT_ID,
        )
    assert state["attempts"] == 1


def test_retry_allowed_pure_semantics():
    """_retry_allowed 判定表：立即拒绝/连接失败可，判死/开口后不可。"""
    assert oneshot_mod._retry_allowed(RetryableError("HTTP 429", status=429)) is True
    assert oneshot_mod._retry_allowed(RetryableError("HTTP 503", status=503)) is True
    assert oneshot_mod._retry_allowed(RetryableError("conn")) is True
    assert oneshot_mod._retry_allowed(httpx.ConnectError("x")) is True
    # 流已开口 → 一律不可
    got = RetryableError("HTTP 503", status=503)
    got.oneshot_got_event = True  # type: ignore[attr-defined]
    assert oneshot_mod._retry_allowed(got) is False
    # 判死/永久 → 不可
    assert oneshot_mod._retry_allowed(OneshotIdleTimeout("idle")) is False
    assert oneshot_mod._retry_allowed(OneshotTotalTimeout("total")) is False
    assert oneshot_mod._retry_allowed(PermanentError("401", status=401)) is False


# ── 5. 记账：成功调用进 llm_usage（request_type=oneshot）────────


@pytest.fixture
async def usage_env():
    """临时 workspace + Meta 路由 patch（同 test_llm_usage_creation_flag）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = str(Path(tmpdir).resolve())

        async def fake_pid(agent_id: str):
            return PROJECT_ID if agent_id == AGENT_ID else None

        async def fake_ws(pid: str):
            return ws if pid == PROJECT_ID else None

        with patch(
            "hiveweave.db.meta.get_agent_project_id", side_effect=fake_pid
        ), patch(
            "hiveweave.db.meta.get_project_workspace", side_effect=fake_ws
        ):
            yield {"ws": ws}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass
        project_db._agent_cache.pop(AGENT_ID, None)
        project_db._write_locks.pop(AGENT_ID, None)


@pytest.mark.asyncio
async def test_oneshot_llm_records_llm_usage_row(usage_env):
    """Agent._oneshot_llm 全漏斗：mock 流式层返回 usage → llm_usage 落行，
    request_type='oneshot'、duration>0、cache 字段经 normalize_usage。"""
    await project_db.ensure_project_db(usage_env["ws"])

    async def fake_pid(agent_id: str) -> str | None:
        return PROJECT_ID if agent_id == AGENT_ID else None

    async def fake_ws(pid: str) -> str | None:
        return usage_env["ws"] if pid == PROJECT_ID else None

    stream_result = {
        "text": "VERDICT: PASS",
        "thinking": "",
        "finish_reason": "stop",
        "usage": {"input": 12, "output": 7, "total": 19},
        "duration_ms": 1234,
    }
    agent = object.__new__(agent_mod.Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    with (
        patch("hiveweave.db.meta.get_agent_project_id", side_effect=fake_pid),
        patch("hiveweave.db.meta.get_project_workspace", side_effect=fake_ws),
        patch(
            "hiveweave.llm.streamer.oneshot.stream_oneshot_with_retry",
            new=AsyncMock(return_value=stream_result),
        ),
    ):
        text = await agent._oneshot_llm(
            dict(MODEL_CFG), "system", "user prompt",
        )
    assert text == "VERDICT: PASS"
    conn = await project_db.ensure_project_db(usage_env["ws"])
    cur = await conn.execute(
        "SELECT agent_id, project_id, request_type, provider, model_id, "
        "input_tokens, output_tokens, total_tokens, duration_ms "
        "FROM llm_usage WHERE request_type = 'oneshot'"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    (agent_id, project_id, rtype, provider, model_id, tin, tout, total, dur) = rows[0]
    assert agent_id == AGENT_ID
    assert project_id == PROJECT_ID
    assert rtype == "oneshot"
    assert provider == "openai"
    assert model_id == "gpt-test"
    assert (tin, tout, total) == (12, 7, 19)
    assert dur == 1234


@pytest.mark.asyncio
async def test_record_oneshot_usage_normalizes_cache_fields():
    """cache_creation 透传键经 normalize_usage（openai 形状 → cache_read）。"""
    calls: list = []

    class _Recorder:
        async def record_rounds(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))

    provider = provider_factory.create(MODEL_CFG)
    result = {
        "text": "ok",
        "usage": {
            "input": 100,
            "output": 5,
            "total": 105,
            "cache_read": 40,
        },
        "duration_ms": 88,
    }
    with patch(
        "hiveweave.services.token_meter.token_meter", _Recorder()
    ):
        await record_oneshot_usage(
            agent_id=AGENT_ID,
            project_id=PROJECT_ID,
            provider=provider,
            model_config=dict(MODEL_CFG),
            result=result,
        )
    assert len(calls) == 1
    args, kwargs = calls[0]
    rounds = kwargs.get("rounds") or args[2]
    (round_row,) = rounds
    assert round_row["cache_read"] == 40
    assert round_row["duration_ms"] == 88
    assert kwargs["request_type"] == "oneshot"


# ── 6. 失败归因落位：code_audit meta 带 timeout_layer ───────────


@pytest.mark.asyncio
async def test_code_audit_meta_carries_timeout_layer_total():
    """总时长判死异常 → meta 带 timeout_layer='total' + capped_at_s。"""
    from hiveweave.services.code_audit import effective_audit_timeout_s

    text, meta = await _invoke_audit_llm(
        PROJECT_ID,
        AGENT_ID,
        "sys",
        "user",
        call_llm=AsyncMock(side_effect=OneshotTotalTimeout("Oneshot total timeout (540s wall clock)")),
        oneshot_llm=None,
    )
    assert text is None
    assert meta["reason"] == "llm_failed"
    assert meta["timeout_layer"] == "total"
    assert meta["capped_at_s"] == effective_audit_timeout_s()


@pytest.mark.asyncio
async def test_code_audit_meta_carries_timeout_layer_idle():
    """idle 判死异常 → meta 带 timeout_layer='idle'。"""
    text, meta = await _invoke_audit_llm(
        PROJECT_ID,
        AGENT_ID,
        "sys",
        "user",
        call_llm=AsyncMock(
            side_effect=OneshotIdleTimeout("Oneshot stream idle timeout (150s between events)")
        ),
        oneshot_llm=None,
    )
    assert text is None
    assert meta["timeout_layer"] == "idle"


@pytest.mark.asyncio
async def test_code_audit_outer_waitfor_is_a_real_gate(monkeypatch):
    """外层 wait_for 共享同一 env：env 调小后挂在慢回调上真提前杀。"""
    from hiveweave.services.code_audit import effective_audit_timeout_s

    async def hang_forever(system: str, user: str) -> str:
        # 不用 asyncio.sleep（本文件 autouse mock 了它）：Event 永不置位
        await asyncio.Event().wait()
        return "never"  # pragma: no cover

    async def fake_ws(pid: str) -> None:
        return None

    monkeypatch.setenv("HIVEWEAVE_CODE_AUDIT_TIMEOUT_S", "0.2")
    with patch("hiveweave.db.meta.get_project_workspace", side_effect=fake_ws):
        text, meta = await _invoke_audit_llm(
            PROJECT_ID, AGENT_ID, "sys", "user",
            call_llm=hang_forever, oneshot_llm=None,
        )
    assert text is None
    assert meta["reason"] == "llm_failed"
    assert meta["timeout_layer"] == "total"
    assert meta["capped_at_s"] == effective_audit_timeout_s() == 0.2


# ── 7. reasoning 回退：text 空 + thinking 有内容 → 返回 thinking ──


@pytest.mark.asyncio
async def test_oneshot_llm_reasoning_content_fallback():
    """thinking 模型把结论写进 reasoning 通道 → _oneshot_llm 回退取 thinking
    （旧 reasoning_content 回退的流式等价物，契约不回退）。"""
    stream_result = {
        "text": "",
        "thinking": "VERDICT: PASS\n",
        "finish_reason": "stop",
        "usage": None,
        "duration_ms": 5,
    }
    agent = object.__new__(agent_mod.Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    with patch(
        "hiveweave.llm.streamer.oneshot.stream_oneshot_with_retry",
        new=AsyncMock(return_value=stream_result),
    ):
        text = await agent._oneshot_llm(dict(MODEL_CFG), "sys", "user")
    assert text == "VERDICT: PASS\n"
