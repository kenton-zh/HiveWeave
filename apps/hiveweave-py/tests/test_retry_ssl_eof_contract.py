"""重试机制对标 DSH 的契约（2026-08-26）。

依据 TEST_DSH_29 实战数据（1736 次 LLM 请求）：
- 57 次 llm_retry / 10 次 llm_retry_exhausted，失败原因 100% 是
  "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
  （上游把连接截断）。
- 18% 的重试链打光预算 -> 原 MAX_RETRIES=2 过紧。

本测试锁定三件事：
1. 重试预算 = 5（DSH 同量级）。
2. SSL EOF 在「消息分类」与「异常分类」两条路径都判为可重试。
3. 不过度匹配：普通业务文本不得被误判可重试（否则永久错误白重试 5 次）。
"""

from __future__ import annotations

import ssl

import pytest

from hiveweave.llm.retry import (
    MAX_RETRIES,
    PermanentError,
    RetryableError,
    classify_http_error,
    matches_retryable_message,
    should_retry_exception,
)

REAL_SSL_EOF = (
    "Connection error: [SSL: UNEXPECTED_EOF_WHILE_READING] "
    "EOF occurred in violation of protocol (_ssl.c:1032)"
)


def test_retry_budget_matches_dsh():
    """预算 5 次 —— 原 2 次导致 18% 重试链失败。"""
    assert MAX_RETRIES == 5


def test_real_ssl_eof_is_retryable_by_message():
    """实战 100% 的失败原因必须走可重试路径。"""
    assert matches_retryable_message(REAL_SSL_EOF) is True
    assert isinstance(classify_http_error(None, REAL_SSL_EOF), RetryableError)


@pytest.mark.parametrize(
    "text",
    [
        "[SSL: UNEXPECTED_EOF_WHILE_READING]",
        "EOF occurred in violation of protocol",
        "SSLError: bad record mac",
        "TLSV1_ALERT_INTERNAL_ERROR",
        "handshake failure",
        "IncompleteRead(1024 bytes read)",
        "Stream ended unexpectedly",
    ],
)
def test_tls_transport_faults_are_retryable(text: str):
    """TLS/传输截断的各种表述都应可重试。"""
    assert matches_retryable_message(text) is True


def test_ssl_error_exception_is_retryable():
    """裸 ssl.SSLError（非 httpx 包装）也应重试。"""
    assert should_retry_exception(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")) is True


@pytest.mark.parametrize(
    "text",
    [
        "File not found: /tmp/eof.txt",
        "invalid api key",
        "model does not support tools",
        "user requested 4096 tokens",
        "permission denied for write_file",
    ],
)
def test_no_false_positives(text: str):
    """普通业务/配置错误不得被误判可重试 —— 否则白烧 5 次预算。"""
    assert matches_retryable_message(text) is False
    assert isinstance(classify_http_error(400, text), PermanentError)
