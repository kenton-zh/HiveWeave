"""#22（2026-09-16）：LLM 错误必须能回答「是谁的错 / 哪个模型」。

## 现场（58/59 两个项目的停摆最后一条消息）

```
event_type = "llm_error.ValueError"
payload    = {"error": "HTTP 403: {\"type\":\"error\",\"error\":{\"type\":\"RegionError\",
              \"message\":\"This model is not available in your country.\"}}",
              "error_type": "ValueError"}
```

三处缺陷，本文件逐条钉住：
① **status 丢了**：明确的终态错误（HTTP 403 + `RegionError`）被压成
   `llm_error.ValueError` —— 而 `ValueError` 本身就是**谎**（它不是参数错）；
② **没有 provider/model** ⇒ 事后无法定位"是哪个模型被 region 拦"
   （对比 `llm_unknown_error_sample` 是带 provider/model 的）；
③ 处置无从下手：region-blocked = 该模型**永久**不可用。

⚠ 关于 ③ 的口径（不要误读成"没做"）：`agent.py` 有一段**刻意的设计决定** ——
「同 tier 自动切换备用模型已移除（对标 DSH，2026-08-26）：换 model = 换缓存域，
整条前缀缓存作废；且静默改变模型身份会掩盖真实故障」。
⇒ 本条**不**重新引入自动切备用（那是撤销一个已论证的决定）。
本条做的是把 ③ 变成**可判定的**：错误带上 status + 模型身份，
于是「该模型永久不可用」与「本次请求偶发失败」在数据上可区分，
下一步（标记模型不可用 / 由人或上层决策切换）才有据可依。
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.llm.retry import PermanentError, RetryableError, classify_http_error

# 58/59 现场的**原文**（逐字取自 agent_events.payload.error）
_REGION_BODY = (
    '{"type":"error","error":{"type":"RegionError",'
    '"message":"This model is not available in your country."}}'
)


# ── ① 分类层：403 必须给出「永久 + 带 status」，且不能是 ValueError ──


def test_region_403_is_permanent_with_status_and_not_valueerror():
    """★ 状态判据：403 地域不可用 ⇒ `PermanentError` 且 `status == 403`。"""
    err = classify_http_error(403, _REGION_BODY)
    assert isinstance(err, PermanentError), type(err)
    assert not isinstance(err, RetryableError)
    assert err.status == 403, err.status
    assert not isinstance(err, ValueError), (
        "ValueError 是谎 —— 它不是参数错，而是一个明确的终态 HTTP 错误"
    )
    assert "HTTP 403" in str(err)


def test_429_still_retryable_unaffected_by_the_identity_work():
    """反向对照：可重试类不受影响（本条只动"身份/归因"，不动重试语义）。"""
    err = classify_http_error(429, "slow down")
    assert isinstance(err, RetryableError)
    assert err.status == 429


# ── ② 事件层：payload 必须带 status / provider / model ──────────────


def _fake_agent(config: dict | None = None) -> AsyncMock:
    """够用的假 agent：`AsyncMock` 让**所有**方法调用可 await（`handle_error`
    尾部还会走 inbox/resume/升级等一串分支），只需把**要用作数值/None 的属性**
    显式定死 —— 否则 `MagicMock > int` 会 TypeError。
    """
    a = AsyncMock()
    a.id = "a-1"
    a.project_id = "p-1"
    a.config = config or {}
    a._current_run_id = None
    a._consecutive_errors = 0
    a._CONSECUTIVE_ERROR_MAX = 5
    a.pending_inbox_msg_ids = None
    a._stream_timeout_streak = 0
    a._streaming_msg_id = None
    a.current_job = None
    # 同步方法要显式退回 MagicMock：`AsyncMock` 会把子属性也变成 AsyncMock，
    # 同步调用就会造出一个**从不被 await 的协程**（刷 RuntimeWarning，
    # 也可能掩盖真实问题）。
    for _sync in ("_broadcast_stream_event", "_broadcast_agent_health",
                  "_cancel_safety_timer", "_reset_to_idle",
                  "_arm_resume_cooldown", "_arm_resume_suppressed"):
        setattr(a, _sync, MagicMock())
    return a


@pytest.mark.asyncio
async def test_handle_error_records_status_provider_and_model():
    """★ #22 验收（重放同一 403）：事件里能读出 `status=403` 与模型 id。"""
    from hiveweave.agents import recovery

    agent = _fake_agent()
    err = PermanentError(f"HTTP 403: {_REGION_BODY}", status=403)
    # 流层盖的"实际使用"的 provider/model（`core.py::_stamp_error_identity`）
    partial = {
        "status": "error",
        "error": str(err),
        "error_status": 403,
        "provider": "开源网关",
        "model": "muse-spark-1.3-contributor",
    }
    with patch(
        "hiveweave.services.event_audit.event_audit.log", new=AsyncMock()
    ) as ev:
        await recovery.handle_error(agent, err, partial_result=partial)

    assert ev.await_count == 1, ev.await_args_list
    kwargs = ev.await_args.kwargs
    payload = kwargs["payload"]
    assert kwargs["event_type"] == "llm_error.PermanentError", kwargs["event_type"]
    assert payload["error_status"] == 403, payload
    assert payload["provider"] == "开源网关", payload
    assert payload["model"] == "muse-spark-1.3-contributor", payload
    assert "RegionError" in payload["error"], payload


@pytest.mark.asyncio
async def test_handle_error_falls_back_to_agent_config():
    """没有 partial_result 时退到 `agent.config`（兜底，不静默丢字段）。"""
    from hiveweave.agents import recovery

    agent = _fake_agent({"model_id": "step-3.7-flash", "provider_type": "openai"})
    err = PermanentError("HTTP 403: nope", status=403)
    with patch(
        "hiveweave.services.event_audit.event_audit.log", new=AsyncMock()
    ) as ev:
        await recovery.handle_error(agent, err)

    payload = ev.await_args.kwargs["payload"]
    assert payload["error_status"] == 403, payload
    assert payload["model"] == "step-3.7-flash", payload
    assert payload["provider"] == "openai", payload


def test_llm_identity_fields_does_not_fill_defaults():
    """缺值**不补**默认：`status=None` 与 `status=0` 语义完全不同。

    补默认会把"没这条信息"说成"确认值"（本仓在事实位上的既有教训）。
    """
    from hiveweave.agents.recovery import _llm_identity_fields

    assert _llm_identity_fields(None, "", "") == {}
    assert _llm_identity_fields(0, "", "") == {"error_status": 0}
    assert _llm_identity_fields(403, "p", "m") == {
        "error_status": 403, "provider": "p", "model": "m",
    }


# ── 根因守卫：不许再把 LLM 错误压成裸 ValueError ────────────────────


def test_llm_error_path_does_not_wrap_message_in_bare_valueerror():
    """★ AST 守卫（不是文案匹配）：`agents/agent.py` 里不得再出现
    `ValueError(error_msg)`。

    这就是 #22 的根因形态：把流层已经结构化的错误（带 status）压成一个
    **裸 ValueError**，`error_type` 于是变成 `ValueError`、status 丢失。
    ⚠ 只禁"用 LLM 错误消息构造 ValueError"这一形态 —— 文件里其它
    `ValueError(...)`（如"没有配置模型"）是**真的**参数/配置错，不该一起禁。
    """
    src = Path(__file__).resolve().parents[1] / "src" / "hiveweave" / "agents" / "agent.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "ValueError"):
            continue
        if any(
            isinstance(a, ast.Name) and a.id == "error_msg" for a in node.args
        ):
            offenders.append(node.lineno)
    assert not offenders, (
        f"agent.py 又把 LLM 错误消息包成裸 ValueError（行 {offenders}）—— "
        "status 会丢、error_type 会说谎，改用 PermanentError(msg, status=…)"
    )


def test_streamer_stamps_provider_and_model_on_error_results():
    """流层**单点**盖章：错误 result 必须带 provider/model（7 个出口一次覆盖）。"""
    import asyncio

    from hiveweave.llm.streamer.core import Streamer

    st = Streamer(max_tool_rounds=1)
    provider = MagicMock()
    provider.model_name = "muse-spark-1.3-contributor"
    model_config = {"name": "开源网关", "model_id": "fallback-id"}

    async def _boom(**_kw):
        raise RuntimeError("boom")

    with (
        patch.object(st._provider_factory, "create", return_value=provider),
        patch.object(st, "_run_tool_loop", new=_boom),
    ):
        res = asyncio.run(
            st.stream(agent_id="a-1", messages=[], model_config=model_config)
        )

    assert res["status"] == "error", res
    assert res["provider"] == "开源网关", res
    assert res["model"] == "muse-spark-1.3-contributor", res
