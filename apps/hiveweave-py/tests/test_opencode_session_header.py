"""opencode Go 网关 x-opencode-session 会话头（2026-09-07 MissingSessionID 400 热修）。

opencode Go 网关（https://opencode.ai/zen/go/v1，muse-spark / open_DS_V 的
当前上游）强制校验 x-opencode-session：缺失即 400 MissingSessionID。
官方契约（https://opencode.ai/docs/go）：每会话带稳定 id 供路由/提示缓存
优化，并建议自定义 User-Agent（python-httpx 通用 UA 是其 problem client
判定特征）。本文件锁定：

1. opencode host 的请求（两种协议格式）必带会话头 + 自定义 UA；
2. 显式 session_id 原样透传且稳定（agent 主对话传 agent_id）；
3. 无 session_id 的平台一次性调用（压缩/审计/视觉/探针）落到进程级
   稳定兜底键——不按请求换（换太勤=网关路由/缓存亲和全废）；
4. 非 opencode 网关（ark/bigmodel 等）请求零改动。
"""

from __future__ import annotations

from hiveweave.llm.provider import (
    ApiFormat,
    ProviderConfig,
    _is_opencode_gateway,
    _OPENCODE_SESSION_HEADER,
)


def _cfg(base_url: str, fmt: ApiFormat = ApiFormat.OPENAI_RESPONSES) -> ProviderConfig:
    return ProviderConfig(
        api_format=fmt,
        base_url=base_url,
        api_key="test-key",
        model_name="muse-spark-1.3-contributor",
        context_window=1_000_000,
        max_output_tokens=128_000,
    )


def test_opencode_host_detection():
    assert _is_opencode_gateway("https://opencode.ai/zen/go/v1")
    assert _is_opencode_gateway("https://sub.opencode.ai/zen/v1")
    assert _is_opencode_gateway("https://opencode.ai")  # 裸域也算
    assert not _is_opencode_gateway("https://opencode.ai.evil.example/v1")  # 后缀伪装
    assert not _is_opencode_gateway("https://ark.cn-beijing.volces.com/api/plan/v3")
    assert not _is_opencode_gateway(None)
    assert not _is_opencode_gateway("")


def test_opencode_responses_format_gets_session_and_ua():
    """muse-spark 走 openai-responses：必带会话头 + hiveweave UA。"""
    headers = _cfg("https://opencode.ai/zen/go/v1").build_headers(
        session_id="A455"
    )
    assert headers[_OPENCODE_SESSION_HEADER] == "A455"
    assert headers["User-Agent"].startswith("hiveweave/")


def test_opencode_compatible_format_also_covered():
    """open_DS_V 走 openai-compatible 同host：同样注入（协议无关，按 host）。"""
    headers = _cfg(
        "https://opencode.ai/zen/go/v1", ApiFormat.OPENAI_COMPATIBLE
    ).build_headers(session_id="agent-uuid")
    assert headers[_OPENCODE_SESSION_HEADER] == "agent-uuid"


def test_explicit_session_id_stable_across_calls():
    cfg = _cfg("https://opencode.ai/zen/go/v1")
    for _ in range(3):
        assert cfg.build_headers(session_id="A450")[
            _OPENCODE_SESSION_HEADER
        ] == "A450"


def test_blank_session_id_falls_back_to_stable_process_key():
    """空/空白 session_id → 进程级兜底键；进程内稳定（不逐请求换）。"""
    cfg = _cfg("https://opencode.ai/zen/go/v1")
    blank = cfg.build_headers(session_id="   ")[_OPENCODE_SESSION_HEADER]
    first = cfg.build_headers()[_OPENCODE_SESSION_HEADER]
    second = cfg.build_headers()[_OPENCODE_SESSION_HEADER]
    assert blank == first == second
    assert len(first) >= 32  # uuid4 形态


def test_non_opencode_gateway_untouched():
    """ark / bigmodel 等其他网关：不注入会话头，不覆写 UA。"""
    for url in (
        "https://ark.cn-beijing.volces.com/api/plan/v3",
        "https://open.bigmodel.cn/api/paas/v4",
    ):
        headers = _cfg(url, ApiFormat.OPENAI_COMPATIBLE).build_headers(
            session_id="A455"
        )
        assert _OPENCODE_SESSION_HEADER not in headers
        assert "User-Agent" not in headers
