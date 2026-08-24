"""E7 复盘验收：配额风暴组织级降速（容量错误不进重试 + 项目级暂停）。

复盘致命链二：129 条运行错误全部来自网关层；13-15 时配额风暴使全组织
停摆约 2 小时。daily_quota / GoUsageLimitError 是容量耗尽（窗口级重置），
秒级退避重试只会白撞（75 个 error runs）。

修复锁定：
- is_capacity_error：容量错误 vs 瞬时限流分类。
- RetryHandler：容量错误不进逐次重试（立即上抛给 agent 层）。
- 项目级容量暂停：一个 agent 撞墙 → 同项目 peer 冷却到配额窗口重置，
  恢复后由既有 watcher/cooldown 批量唤醒。
"""

from __future__ import annotations

import importlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.agents.helpers import rate_limit as rl
from hiveweave.llm.retry import (
    RetryHandler,
    RetryableError,
    is_capacity_error,
)


def _run_sync(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro)


# ── is_capacity_error 分类 ────────────────────────────────────


@pytest.mark.parametrize(
    "msg",
    [
        "GoUsageLimitError: quota for 5h rolling window exceeded",
        "goUsageLimitError daily quota exhausted",
        "HTTP 429: daily_quota exceeded — resets at 2026-08-24 20:00",
        "quota_exhausted: no tokens left for this window",
        "insufficient quota to complete the request",
    ],
)
def test_is_capacity_error_true(msg):
    assert is_capacity_error(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "HTTP 429: rate limit exceeded, retry in 30s",
        "Too many requests, slow down",
        "requests per minute limit reached, back off",
        "resource exhausted: quota for current provider",  # 瞬态 429，非窗口级
        "",
        None,
    ],
)
def test_is_capacity_error_false(msg):
    assert is_capacity_error(msg) is False


# ── RetryHandler：容量错误不进逐次重试 ─────────────────────────


def test_capacity_error_not_retried():
    """容量错误 → fn 只调一次，立即上抛（不进退避重试）。"""
    calls = []

    async def fn():
        calls.append(1)
        raise RetryableError(
            "GoUsageLimitError: 5h rolling quota exceeded", status=429
        )

    handler = RetryHandler(max_retries=2)
    with pytest.raises(RetryableError):
        _run_sync(handler.with_retry(fn))
    assert len(calls) == 1


def test_transient_429_still_retried():
    """瞬时限流（非容量）维持既有逐次重试语义（fn 调用 > 1）。"""
    calls = []

    async def fn():
        calls.append(1)
        raise RetryableError(
            "HTTP 429: Too Many Requests, retry after 1s", status=429
        )

    handler = RetryHandler(max_retries=2)
    # 2 次退避会 sleep（base ~1s + jitter）——重试路径本身已有测试覆盖；
    # 这里只需证明它不是容量直抛路径（fn 至少进入重试调度）。
    with patch.object(handler, "_fire_retry", new=AsyncMock()), patch(
        "hiveweave.llm.retry.asyncio.sleep", new=AsyncMock()
    ):
        with pytest.raises(RetryableError):
            _run_sync(handler.with_retry(fn))
    assert len(calls) == 3  # 首次 + 2 次重试


# ── 项目级容量暂停（组织级降速）───────────────────────────────


def test_project_capacity_pause_until_reset():
    """arm 到 reset_at → remaining > 0；到期后自动恢复（remaining=0）。"""
    rl._project_capacity_until.pop("p-e7-1", None)
    future = time.time() + 120
    try:
        remaining = rl.arm_project_capacity_pause("p-e7-1", future)
        assert remaining > 0
        assert rl.project_capacity_remaining("p-e7-1") > 0
        # 手动把暂停置为过期 → 恢复（remaining=0 且清账）
        rl._project_capacity_until["p-e7-1"] = time.time() - 1
        assert rl.project_capacity_remaining("p-e7-1") == 0.0
        assert "p-e7-1" not in rl._project_capacity_until
    finally:
        rl._project_capacity_until.pop("p-e7-1", None)


def test_project_capacity_pause_ignores_past_reset():
    """reset_at 已过 / 缺失 → 不设立暂停（0）。"""
    assert rl.arm_project_capacity_pause("p-e7-2", time.time() - 10) == 0.0
    assert rl.arm_project_capacity_pause("p-e7-2", None) == 0.0
    rl._project_capacity_until.pop("p-e7-2", None)


def test_broadcast_capacity_pause_cools_peers():
    """同项目 peer 冷却到重置（≥60s），异项目与 source 不误伤。"""

    class _Peer:
        def __init__(self, pid: str, aid: str):
            self.project_id = pid
            self.id = aid
            self.cooldowns: list[float] = []

        def _arm_resume_cooldown(self, secs: float) -> None:
            self.cooldowns.append(secs)

    source = _Peer("p-e7-3", "src-1")
    peer_a = _Peer("p-e7-3", "a-1")
    peer_b = _Peer("other-proj", "b-1")

    fake_mgr = SimpleNamespace(list_all=lambda: [source, peer_a, peer_b])
    rl._project_capacity_until.pop("p-e7-3", None)
    supervisor_mod = importlib.import_module("hiveweave.agents.supervisor")
    try:
        with patch.object(supervisor_mod, "agent_manager", fake_mgr):
            cooled = rl.broadcast_project_capacity_pause(
                "p-e7-3",
                time.time() + 300,
                source_agent_id="src-1",
            )
    finally:
        rl._project_capacity_until.pop("p-e7-3", None)

    assert cooled == 1
    assert bool(peer_a.cooldowns) and peer_a.cooldowns[0] >= 60.0
    assert source.cooldowns == []  # source 已有 per-agent park，不重复冷却
    assert peer_b.cooldowns == []  # 异项目不受影响