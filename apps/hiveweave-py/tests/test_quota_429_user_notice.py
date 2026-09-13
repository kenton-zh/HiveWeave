"""issue-2 §4.1 回归：429 月额度耗尽 → 无 LLM 用户通告（TEST_DSH_55）。

背景：TEST_DSH_55 中 4 名 agent 共 24 次命中 `GoUsageLimitError`（月额度，
8 天后重置），用户 4.5 小时零送达。根因不是"没有通知代码"，而是唯一的
通知试图走 `_inbox.send_message(to_agent_id="user")` 死通道（收件人不存在
→ ProjectDbError → 被 except: pass 吞掉）。

修复：429 + is_daily_quota 复用既有**无 LLM** 通道
`_notify_user_balance_exhausted`（ChatMessageService 直写 + 事件推送），
且带三处修正：
  ① 只复用通知，**不**触发 402 的 1h 全局熔断；
  ② 文案参数化（区分「8 天后重置」与「需换 key」）；
  ③ 幂等去重（停车每次必败唤醒都重进，不去重会刷最多 24 条）。
另清理 recovery.py 里 `to_agent_id="user"` 的死通道。
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.agents import recovery as recovery_mod


# ── 通告函数本体 ────────────────────────────────────────────────


def _make_agent(aid: str = "a1", pid: str = "p1"):
    from hiveweave.agents.agent import Agent

    a = Agent.__new__(Agent)
    a.id = aid
    a.project_id = pid
    return a


@pytest.fixture(autouse=True)
def _clean_notice_dedupe():
    from hiveweave.agents import agent as agent_mod

    agent_mod._sent_platform_notices.clear()
    yield
    agent_mod._sent_platform_notices.clear()


@pytest.mark.asyncio
async def test_daily_quota_notice_states_429_and_rotation_window():
    """文案必须参数化：429 月额度 ⇒ 说「N 天后重置 + 可换 key」，不得写 402 / 1 小时。"""
    agent = _make_agent()
    save = AsyncMock(return_value={})
    reset_at = time.time() + 8 * 86400
    with patch(
        "hiveweave.services.chat_message.ChatMessageService.save_message", new=save
    ), patch(
        "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
        new=AsyncMock(),
    ), patch("hiveweave.db.project.query_one", new=AsyncMock(return_value=None)):
        await agent._notify_user_balance_exhausted(
            "HTTP 429: GoUsageLimitError monthly usage limit reached. Resets in 8 days.",
            reason="daily_quota",
            reset_at_epoch=reset_at,
        )

    assert save.await_count == 1
    attrs = save.await_args.args[0]
    content = attrs["content"]
    assert "429" in content
    assert "天后" in content and "重置" in content
    assert "API key" in content, "必须给出「需换 key」这条可行动路径"
    assert "402" not in content, "429 通告绝不能写 402 —— 会给用户错误事实"
    assert "1 小时" not in content
    md = json.loads(attrs["metadata"]) if isinstance(attrs["metadata"], str) else attrs["metadata"]
    assert md["kind"] == "platform_notice"
    # 幂等键必须**量化**（审计必须修项：秒级键在 provider 只回相对 Retry-After
    # 时会随 now 漂移 ⇒ 等同无去重）。形态 = UTC 小时桶。
    assert md["dedupe_key"] == (
        "platform-notice:quota:p1:"
        + time.strftime("%Y-%m-%dT%H", time.gmtime(float(reset_at)))
    )


@pytest.mark.asyncio
async def test_402_default_wording_unchanged():
    """默认（402）调用点零改动：仍写「余额耗尽（HTTP 402）」与「约 1 小时」。"""
    agent = _make_agent()
    save = AsyncMock(return_value={})
    with patch(
        "hiveweave.services.chat_message.ChatMessageService.save_message", new=save
    ), patch(
        "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
        new=AsyncMock(),
    ), patch("hiveweave.db.project.query_one", new=AsyncMock(return_value=None)):
        await agent._notify_user_balance_exhausted("HTTP 402: Insufficient Balance")

    content = save.await_args.args[0]["content"]
    assert "402" in content
    assert "1 小时" in content
    # 402 无 dedupe_key（其"天然一次"由全局熔断保证，保持原语义）
    md = save.await_args.args[0]["metadata"]
    assert "dedupe_key" not in md


@pytest.mark.asyncio
async def test_daily_quota_notice_dedupes_same_window():
    """同一配额窗口（同 reset_at_epoch）只发一条 —— 24 次停车不得刷 24 条。"""
    agent = _make_agent()
    save = AsyncMock(return_value={})
    reset_at = time.time() + 8 * 86400
    with patch(
        "hiveweave.services.chat_message.ChatMessageService.save_message", new=save
    ), patch(
        "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
        new=AsyncMock(),
    ), patch("hiveweave.db.project.query_one", new=AsyncMock(return_value=None)):
        for _ in range(24):
            await agent._notify_user_balance_exhausted(
                "HTTP 429 GoUsageLimitError",
                reason="daily_quota",
                reset_at_epoch=reset_at,
            )
        # 不同窗口（另一个 reset 时刻）⇒ 是新通告，必须再发
        await agent._notify_user_balance_exhausted(
            "HTTP 429 GoUsageLimitError",
            reason="daily_quota",
            reset_at_epoch=reset_at + 3600,
        )

    assert save.await_count == 2, (
        f"同窗口 24 次只应发 1 条、换窗口再 1 条；实际 {save.await_count} 条"
    )


@pytest.mark.asyncio
async def test_daily_quota_notice_dedupe_survives_reset_drift():
    """审计①（必须修）：reset 时刻的**秒级漂移**不得产生新通告。

    现场成因：provider 只回相对 ``Retry-After``（或 HTTP-date）时，
    ``parse_quota_reset`` 给的是 ``now+secs`` ⇒ 同一窗口的每次挂账都给出漂移的
    reset（实测 0/3/7/12 秒）。若幂等键取秒级值，24 次停车会退化成 24 条通告，
    **等同无去重**。本用例钉住「量化后漂移被吸收」。

    base 对齐到整点后 10 分钟，确保 ±12 秒不跨小时桶（否则用例自身不稳定）。
    """
    agent = _make_agent()
    save = AsyncMock(return_value={})
    base = (int(time.time()) // 3600) * 3600 + 600 + 8 * 86400
    with patch(
        "hiveweave.services.chat_message.ChatMessageService.save_message", new=save
    ), patch(
        "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
        new=AsyncMock(),
    ), patch("hiveweave.db.project.query_one", new=AsyncMock(return_value=None)):
        for drift in (0, 3, 7, 12):
            await agent._notify_user_balance_exhausted(
                "HTTP 429 GoUsageLimitError",
                reason="daily_quota",
                reset_at_epoch=base + drift,
            )

    assert save.await_count == 1, (
        "同一窗口 reset 漂移 0/3/7/12 秒应仍只发 1 条；"
        f"实际 {save.await_count} 条（幂等键未量化？）"
    )


@pytest.mark.asyncio
async def test_daily_quota_notice_dedupes_across_agents_in_same_project():
    """幂等键按 project+窗口：同项目另一 agent 撞同一窗口也不重复发。"""
    a1 = _make_agent("a1", "p1")
    a2 = _make_agent("a2", "p1")
    save = AsyncMock(return_value={})
    reset_at = time.time() + 8 * 86400
    with patch(
        "hiveweave.services.chat_message.ChatMessageService.save_message", new=save
    ), patch(
        "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
        new=AsyncMock(),
    ), patch("hiveweave.db.project.query_one", new=AsyncMock(return_value=None)):
        await a1._notify_user_balance_exhausted(
            "429", reason="daily_quota", reset_at_epoch=reset_at
        )
        await a2._notify_user_balance_exhausted(
            "429", reason="daily_quota", reset_at_epoch=reset_at
        )
    assert save.await_count == 1


@pytest.mark.asyncio
async def test_daily_quota_notice_dedupes_via_db_when_memory_cold():
    """进程重启后内存集为空 ⇒ DB 查重兜底（同窗口不留重复）。"""
    agent = _make_agent()
    save = AsyncMock(return_value={})
    reset_at = time.time() + 8 * 86400
    # query_one 返回一行 = 该窗口通告已存在
    with patch(
        "hiveweave.services.chat_message.ChatMessageService.save_message", new=save
    ), patch(
        "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
        new=AsyncMock(),
    ), patch(
        "hiveweave.db.project.query_one", new=AsyncMock(return_value=(1,))
    ):
        await agent._notify_user_balance_exhausted(
            "429", reason="daily_quota", reset_at_epoch=reset_at
        )
    assert save.await_count == 0


# ── park_after_quota_exhausted 接线 ─────────────────────────────


class _FakeParkAgent:
    def __init__(self, aid: str = "a1", pid: str = "p1") -> None:
        self.id = aid
        self.project_id = pid
        self.disposition = None
        self.config = {"name": "归零"}
        self.pending_inbox_msg_ids = None
        self._rate_limit_streak = 0
        self._resume_cooldown_until = 0.0
        self._org = SimpleNamespace(
            get_superior=AsyncMock(return_value={"id": "boss"})
        )
        self._inbox = SimpleNamespace(send_message=AsyncMock())
        self._notify_user_balance_exhausted = AsyncMock()
        self._arm_resume_suppressed = MagicMock()
        self._arm_resume_cooldown = MagicMock()
        self._write_resume_checkpoint = AsyncMock()
        self._broadcast_agent_health = AsyncMock()


def _park_patches():
    from hiveweave.services.wait_contract import wait_contract_service

    return patch.object(
        wait_contract_service, "replace_waits", new=AsyncMock(return_value=None)
    )


@pytest.mark.asyncio
async def test_park_is_daily_triggers_user_notice():
    """429 + is_daily ⇒ 走无 LLM 通知，且**不**触发 402 全局熔断。"""
    agent = _FakeParkAgent()
    reset_at = time.time() + 8 * 86400
    with _park_patches(), patch(
        "hiveweave.agents.helpers.rate_limit.broadcast_balance_exhausted",
        new=MagicMock(),
    ) as breaker:
        await recovery_mod.park_after_quota_exhausted(
            agent,
            inbox_ids=["m1"],
            error_msg="HTTP 429 GoUsageLimitError: monthly usage limit",
            reset_at_epoch=reset_at,
            reason="daily_quota",
            is_daily=True,
        )

    assert agent._notify_user_balance_exhausted.await_count == 1
    kwargs = agent._notify_user_balance_exhausted.await_args.kwargs
    assert kwargs["reason"] == "daily_quota"
    assert kwargs["reset_at_epoch"] == reset_at
    breaker.assert_not_called()  # 8 天配额不得套 1h 全局熔断


@pytest.mark.asyncio
async def test_park_non_daily_long_retry_after_does_not_notify():
    """非 is_daily 的长 Retry-After 也走 park，但**不得**向用户发通告
    （否则会把「等几分钟」误报成「8 天后重置 / 需换 key」）。"""
    agent = _FakeParkAgent()
    with _park_patches():
        await recovery_mod.park_after_quota_exhausted(
            agent,
            inbox_ids=[],
            error_msg="HTTP 429 rate limit",
            reset_at_epoch=time.time() + 1800,
            reason="daily_quota",
            is_daily=False,
        )
    assert agent._notify_user_balance_exhausted.await_count == 0


@pytest.mark.asyncio
async def test_park_never_writes_dead_user_inbox_channel():
    """死通道清理：不得再以 to_agent_id="user" 发 inbox（收件人不存在）。"""
    agent = _FakeParkAgent()
    with _park_patches():
        await recovery_mod.park_after_quota_exhausted(
            agent,
            inbox_ids=[],
            error_msg="HTTP 429 GoUsageLimitError: monthly",
            reset_at_epoch=time.time() + 8 * 86400,
            reason="daily_quota",
            is_daily=True,
        )
    for call in agent._inbox.send_message.await_args_list:
        assert call.kwargs.get("to_agent_id") != "user", (
            "to_agent_id='user' 在 agents 表不存在 → ProjectDbError → 0 落库"
        )
    # 上级升级（可用通道）仍保留
    assert agent._inbox.send_message.await_count == 1
    assert agent._inbox.send_message.await_args.kwargs["to_agent_id"] == "boss"
