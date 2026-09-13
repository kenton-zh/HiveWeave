"""件5（42 轮报告 P2-8）+ B 方案（2026-09-11）：timer wait 的目标时刻语义。

旧行为：commit_turn(waiting_on=[{kind:timer, ref:<目标时刻>}]) 的
expires_at 一律 = created + 15min，agent 设 4h 后目标会被虚假唤醒两次。
新语义（wait_contract.replace_waits）：
  A. 目标 ≤ created+TTL → expires_at = 目标时刻（按目标排队）；
  B. 目标 > created+TTL → expires_at 封顶 TTL，note 打 ttl_cap 标记；
  C. 解析不了目标（quota_reset / alarm-<uuid> 等平台内部 timer）→ **首票**走
     基础 TTL，此后按退避阶梯放大，并标 `wakeup_reason=ttl_expire`（P0-B
     2026-09-13；原先一律退回基础 TTL ⇒ 阶梯不可达，实测连醒 19 次）。
game_time 唤醒文案区分 wakeup_reason=target_reached | ttl_cap。

B 方案（2026-09-11，雾屿 a728cba5 空转实测）：封顶额度按**真超时轮次**
指数退避（默认 ×1/×4/×24/×96 = 15min/1h/6h/24h，末档饱和），消除
「7 天目标 ≈ 672 次零产出空转」。两条硬性质：
  - 只数真超时（cleared_at >= expires_at）；被 replace_waits 提前清的
    行不算，故频繁重挂不虚增档位。
  - 退避永不越过目标：目标落入退避窗即转 target_reached —— 只会少醒，
    不会漏醒。
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

import hiveweave.services.inbox as inbox_mod
import hiveweave.services.wait_contract as wait_mod
from hiveweave.config import settings
from hiveweave.services.game_time import GameTimeService
from hiveweave.services.wait_contract import (
    WAIT_TIMER_BACKOFF_MAX_MS,
    WaitContractService,
    _conn as _wait_conn,
    _timer_rounds_key,
    default_ttl_ms,
    parse_timer_target_ms,
    timer_backoff_ttl_ms,
    wait_target_iso,
    wait_wakeup_reason,
)
from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401

TTL_MS = default_ttl_ms("timer")  # 15 * 60 * 1000


@pytest.fixture(autouse=True)
def _clear_migrated():
    wait_mod._migrated.clear()
    inbox_mod._migrated.clear()
    yield
    wait_mod._migrated.clear()
    inbox_mod._migrated.clear()


@pytest.fixture(autouse=True)
def _cleanup_game_time_state(task_env):
    yield
    import hiveweave.services.game_time as game_time_mod

    game_time_mod._states.pop(task_env["project_id"], None)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── 解析器单测 ────────────────────────────────────────────


def test_parse_timer_target_ms_formats():
    now = _now_ms()
    # epoch 秒自动升毫秒
    assert parse_timer_target_ms(1770000000) == 1770000000000
    # ISO-8601 Z
    assert parse_timer_target_ms("2030-01-01T15:00:00Z") is not None
    # 纯时刻 HH:MMZ（下一个未来发生点）
    t = parse_timer_target_ms("23:59Z")
    assert t is not None and t > now
    # 平台内部 ref 解析不了 → None
    assert parse_timer_target_ms("quota_reset") is None
    assert parse_timer_target_ms("alarm-1") is None
    assert parse_timer_target_ms(None, "") is None


# ── replace_waits 排队语义 ────────────────────────────────


async def test_timer_target_within_ttl_queues_at_target(task_env):
    """A：目标 ≤ TTL → expires_at = 目标时刻（不是 +900s），打 target_reached。"""
    pid = task_env["project_id"]
    wc = WaitContractService()
    target = _now_ms() + 5 * 60 * 1000  # 5min < 15min TTL
    created = await wc.replace_waits(
        pid, EXEC, [{"kind": "timer", "ref": f"{target}"}], phase="waiting"
    )
    assert len(created) == 1
    w = created[0]
    assert w["expiresAt"] == target  # 按目标排队，而非 created+900s
    assert wait_wakeup_reason(w) == "target_reached"


async def test_timer_target_beyond_ttl_caps_with_ttl_cap(task_env):
    """B：目标 > TTL → expires_at 封顶 TTL，note 打 ttl_cap 标记。"""
    pid = task_env["project_id"]
    wc = WaitContractService()
    before = _now_ms()
    target = before + 4 * 60 * 60 * 1000  # 4h 后 ≫ 15min TTL
    created = await wc.replace_waits(
        pid,
        EXEC,
        [{"kind": "timer", "ref": "2030-01-01T15:00:00Z"}],
        phase="waiting",
    )
    assert len(created) == 1
    w = created[0]
    assert w["expiresAt"] is not None
    # 封顶在 created+TTL 附近（远早于 2030 目标）
    assert before + TTL_MS - 2000 <= w["expiresAt"] <= before + TTL_MS + 5000
    assert wait_wakeup_reason(w) == "ttl_cap"
    assert wait_target_iso(w) is not None
    # 目标行仍在 note 里可追溯
    assert "2030-01-01T15:00:00" in (w["note"] or "")


async def test_timer_target_around_ttl_boundary(task_env):
    """边界：目标 ≈ created+TTL —— 略小于 TTL 走 A，略大于 TTL 走 B。"""
    pid = task_env["project_id"]
    wc = WaitContractService()
    # A 侧：目标 = now + TTL - 5s（< TTL）
    target_a = _now_ms() + TTL_MS - 5000
    created_a = await wc.replace_waits(
        pid, EXEC, [{"kind": "timer", "ref": f"{target_a}"}], phase="waiting"
    )
    assert created_a[0]["expiresAt"] == target_a
    assert wait_wakeup_reason(created_a[0]) == "target_reached"

    # B 侧：目标 = now + TTL + 5s（> TTL，压线不假装到点）
    target_b = _now_ms() + TTL_MS + 5000
    created_b = await wc.replace_waits(
        pid, EXEC, [{"kind": "timer", "ref": f"{target_b}"}], phase="waiting"
    )
    assert created_b[0]["expiresAt"] <= target_b - 4000  # 被封顶提前
    assert wait_wakeup_reason(created_b[0]) == "ttl_cap"


async def test_unparseable_timer_ref_starts_at_base_ttl(task_env):
    """C：平台内部 timer（quota_reset 等）**首票**仍走基础 TTL。

    （原名 `..._keeps_legacy_ttl` —— P0-B 后再叫「legacy TTL」已不准确：现在是
    「首票 = 基础 TTL（阶梯 level 0 乘数 ×1）」，此后按档位放大。）
    P0-B（2026-09-13 改）：原断言「不打标记」已不成立 —— 无目标 ref 现在也要打
    `ttl_expire` 以便计入退避档位。**但首票仍是基础 TTL** 这条「防退避误伤新票」
    的判定必须保留。
    """
    pid = task_env["project_id"]
    wc = WaitContractService()
    before = _now_ms()
    created = await wc.replace_waits(
        pid, EXEC, [{"kind": "timer", "ref": "quota_reset"}], phase="waiting"
    )
    w = created[0]
    assert before + TTL_MS - 2000 <= w["expiresAt"] <= before + TTL_MS + 5000
    assert wait_wakeup_reason(w) == "ttl_expire"


async def test_numeric_note_not_taken_as_past_target(task_env, monkeypatch):
    """P2-1（审计）：ref 解析失败回退 note，note="30" 按 epoch = 1970 ——
    过去时刻不采信：维持旧 TTL 排队，绝不立即假唤醒 + target_reached。"""
    pid = task_env["project_id"]
    svc = GameTimeService(pid)
    wc = WaitContractService()
    before = _now_ms()
    created = await wc.replace_waits(
        pid,
        EXEC,
        [{"kind": "timer", "ref": "quota_reset", "note": "30"}],
        phase="waiting",
    )
    w = created[0]
    assert w["expiresAt"] > before  # 不再是 1970 的立即到期
    assert before + TTL_MS - 2000 <= w["expiresAt"] <= before + TTL_MS + 5000
    # P0-B（2026-09-13）：无目标 ref 现在标 ttl_expire（关键：**不是**
    # target_reached —— 后者会让唤醒文案谎称「目标已到」）
    assert wait_wakeup_reason(w) == "ttl_expire"
    # 且真实唤醒路径不会立刻把它当 target_reached 发出去
    send = AsyncMock()
    trigger = AsyncMock()
    monkeypatch.setattr("hiveweave.services.inbox.InboxService.send_message", send)
    monkeypatch.setattr(GameTimeService, "_watchdog_trigger", trigger)
    handled = await svc.recover_wait_timeouts(pid)
    assert handled["expired_processed"] is False
    send.assert_not_awaited()
    svc.cancel_wait_recovery_timers(pid)


# ── 唤醒文案区分 ──────────────────────────────────────────


async def test_ttl_cap_wake_message_says_target_not_reached(task_env, monkeypatch):
    """ttl_cap 唤醒文案明说「TTL 上限唤醒，目标未到，续等或改 ScheduledAlarm」。"""
    pid = task_env["project_id"]
    svc = GameTimeService(pid)
    wc = WaitContractService()
    created = await wc.replace_waits(
        pid,
        EXEC,
        [{"kind": "timer", "ref": "2030-01-01T15:00:00Z"}],
        phase="waiting",
    )
    assert wait_wakeup_reason(created[0]) == "ttl_cap"
    # 强制到期，走真实唤醒路径
    conn = await _wait_conn(pid)
    cur = await conn.execute(
        "UPDATE agent_waits SET expires_at = ? WHERE id = ?",
        [_now_ms() - 1000, created[0]["id"]],
    )
    await conn.commit()
    await cur.close()

    send = AsyncMock()
    trigger = AsyncMock()
    monkeypatch.setattr("hiveweave.services.inbox.InboxService.send_message", send)
    monkeypatch.setattr(GameTimeService, "_watchdog_trigger", trigger)

    await svc.recover_wait_timeouts(pid)

    send.assert_awaited_once()
    body = send.await_args.kwargs["message"]
    assert "wakeup_reason=ttl_cap" in body
    assert "2030-01-01T15:00:00" in body  # 目标时刻明示
    assert "commit_turn(waiting_on)" in body
    assert "schedule_alarm" in body


async def test_target_reached_wake_message_labeled(task_env, monkeypatch):
    """target_reached 唤醒文案带 wakeup_reason=target_reached 标注。"""
    pid = task_env["project_id"]
    svc = GameTimeService(pid)
    wc = WaitContractService()
    target = _now_ms() + 60_000
    created = await wc.replace_waits(
        pid, EXEC, [{"kind": "timer", "ref": f"{target}"}], phase="waiting"
    )
    assert wait_wakeup_reason(created[0]) == "target_reached"
    conn = await _wait_conn(pid)
    cur = await conn.execute(
        "UPDATE agent_waits SET expires_at = ? WHERE id = ?",
        [_now_ms() - 1000, created[0]["id"]],
    )
    await conn.commit()
    await cur.close()

    send = AsyncMock()
    trigger = AsyncMock()
    monkeypatch.setattr("hiveweave.services.inbox.InboxService.send_message", send)
    monkeypatch.setattr(GameTimeService, "_watchdog_trigger", trigger)

    await svc.recover_wait_timeouts(pid)

    send.assert_awaited_once()
    body = send.await_args.kwargs["message"]
    assert "wakeup_reason=target_reached" in body
    assert "ttl_cap" not in body


# ── B 方案：timer 长挂账指数退避（2026-09-11）────────────────
FAR = "2030-01-01T15:00:00Z"  # 远在基础 TTL 之外，必走 ttl_cap 分支


async def _simulate_real_timeout(pid: str, agent_id: str) -> None:
    """把活动等待模拟成「真的超时过」：cleared_at = expires_at。

    与 clear_expired 的判据一致（到点清才计入退避档位）。
    """
    conn = await _wait_conn(pid)
    cur = await conn.execute(
        "UPDATE agent_waits SET cleared_at = expires_at "
        "WHERE agent_id = ? AND cleared_at IS NULL AND expires_at IS NOT NULL",
        [agent_id],
    )
    await conn.commit()
    await cur.close()


def test_timer_backoff_ttl_ladder_and_env_tunable(monkeypatch):
    """默认阶梯 ×1/×4/×24/×96，末档饱和；非法/超大配置安全降级。"""
    assert timer_backoff_ttl_ms(60_000, 0) == 60_000
    assert timer_backoff_ttl_ms(60_000, 1) == 60_000 * 4
    assert timer_backoff_ttl_ms(60_000, 2) == 60_000 * 24
    assert timer_backoff_ttl_ms(60_000, 3) == 60_000 * 96
    assert timer_backoff_ttl_ms(60_000, 99) == 60_000 * 96  # 饱和
    assert timer_backoff_ttl_ms(60_000, -5) == 60_000  # 负档按 0 处理

    monkeypatch.setattr(settings, "wait_timer_backoff_multipliers", "1,2,3")
    assert timer_backoff_ttl_ms(60_000, 1) == 120_000
    assert timer_backoff_ttl_ms(60_000, 9) == 180_000

    monkeypatch.setattr(settings, "wait_timer_backoff_multipliers", "junk,,0,-3")
    assert timer_backoff_ttl_ms(60_000, 1) == 60_000 * 4  # 全非法 → 回默认

    # 超大乘数 clamp 到上限（否则 now+eff_ttl 溢出 int64，等待静默不落库）
    monkeypatch.setattr(settings, "wait_timer_backoff_multipliers", "1,999999999999")
    assert timer_backoff_ttl_ms(60_000, 1) == WAIT_TIMER_BACKOFF_MAX_MS


def test_timer_rounds_key_normalizes_absolute_refs():
    """绝对时刻 ref 按目标归一键（容忍 `Z` / `+00:00` / 精度漂移）。"""
    a = _timer_rounds_key("2030-01-01T15:00:00Z")
    b = _timer_rounds_key("2030-01-01T15:00:00+00:00")
    c = _timer_rounds_key("2030-01-01T15:00:00.000Z")
    assert a == b == c, "同一目标的不同写法必须同键，否则重挂即丢档位"
    assert _timer_rounds_key("2040-01-01T00:00:00Z") != a
    # 相对时刻（HH:MM）每次解析都变 → 按原串，不归一
    assert _timer_rounds_key("23:59Z") == "r:23:59Z"
    # 平台内部 ref 解析不了 → 按原串
    assert _timer_rounds_key("quota_reset") == "r:quota_reset"
    assert _timer_rounds_key("") == ""


async def test_timer_backoff_escalates_after_each_real_timeout(task_env):
    """每真超时一次升一档：15min → 1h → 6h → 24h（饱和）→ 24h。"""
    pid = task_env["project_id"]
    wc = WaitContractService()
    expected = [TTL_MS, TTL_MS * 4, TTL_MS * 24, TTL_MS * 96, TTL_MS * 96]
    for i, want in enumerate(expected):
        before = _now_ms()
        created = await wc.replace_waits(
            pid, EXEC, [{"kind": "timer", "ref": FAR}], phase="waiting"
        )
        assert len(created) == 1
        w = created[0]
        assert wait_wakeup_reason(w) == "ttl_cap", f"第 {i} 轮应仍标 ttl_cap"
        ttl = w["expiresAt"] - before
        assert want - 3000 <= ttl <= want + 5000, f"第 {i} 轮 ttl={ttl} want={want}"
        await _simulate_real_timeout(pid, EXEC)


async def test_timer_backoff_applies_to_unparseable_refs(task_env):
    """P0-B（2026-09-13）：**无目标 ref**（quota_reset 等）也必须走退避阶梯。

    现场（TEST_DSH_55）：额度冻结的 8 天窗口里，quota_reset 这张票每 15min 醒
    一次、连醒 19 次、19 个回合全部必败。成因是退避阶梯只在「有可解析目标」
    分支求值 ⇒ 对无目标 ref **完全不可达**。本测试钉住「无目标也退避」。

    阳性对照（必须能转红）：把 wait_contract 里 target_ms is None 分支改回
    `exp = now + ttl_ms` 并去掉 ttl_expire 标记 → 本用例在第 1 轮断言失败。
    """
    pid = task_env["project_id"]
    wc = WaitContractService()
    expected = [TTL_MS, TTL_MS * 4, TTL_MS * 24, TTL_MS * 96]
    for i, want in enumerate(expected):
        before = _now_ms()
        created = await wc.replace_waits(
            pid, EXEC, [{"kind": "timer", "ref": "quota_reset"}], phase="waiting"
        )
        assert len(created) == 1
        w = created[0]
        assert wait_wakeup_reason(w) == "ttl_expire", f"第 {i} 轮应标 ttl_expire"
        ttl = w["expiresAt"] - before
        assert want - 3000 <= ttl <= want + 5000, (
            f"第 {i} 轮 ttl={ttl} want={want}（无目标 ref 未走退避阶梯）"
        )
        await _simulate_real_timeout(pid, EXEC)


async def test_timer_backoff_ignores_premature_clear(task_env):
    """被提前清掉（主动重挂 / 事件唤醒后重挂）不算超时，档位不升。"""
    pid = task_env["project_id"]
    wc = WaitContractService()
    for i in range(3):
        before = _now_ms()
        created = await wc.replace_waits(
            pid, EXEC, [{"kind": "timer", "ref": FAR}], phase="waiting"
        )
        ttl = created[0]["expiresAt"] - before
        assert TTL_MS - 3000 <= ttl <= TTL_MS + 5000, f"第 {i} 轮应停基础档"
        # 不模拟超时：下一次 replace_waits 会把上一行提前清掉


async def test_timer_backoff_only_counts_ttl_cap_timeouts(task_env):
    """只有 ttl_cap 行的真超时计入档位；target_reached 行超时不算。"""
    pid = task_env["project_id"]
    wc = WaitContractService()
    await wc.replace_waits(
        pid, EXEC, [{"kind": "timer", "ref": FAR}], phase="waiting"
    )
    await _simulate_real_timeout(pid, EXEC)  # FAR 档位 → 1

    near = _now_ms() + 60_000
    made = await wc.replace_waits(
        pid, EXEC, [{"kind": "timer", "ref": f"{near}"}], phase="waiting"
    )
    assert wait_wakeup_reason(made[0]) == "target_reached"
    await _simulate_real_timeout(pid, EXEC)  # target_reached 行不该被计入

    before = _now_ms()
    again = await wc.replace_waits(
        pid, EXEC, [{"kind": "timer", "ref": FAR}], phase="waiting"
    )
    assert wait_wakeup_reason(again[0]) == "ttl_cap"
    ttl = again[0]["expiresAt"] - before
    assert TTL_MS * 4 - 3000 <= ttl <= TTL_MS * 4 + 5000, "应仍为 ×4，未被虚增"


async def test_timer_backoff_survives_ref_format_drift(task_env):
    """重挂时 ref 换个写法（`Z` → `+00:00`）不该丢档位 —— 否则退避静默失效。"""
    pid = task_env["project_id"]
    wc = WaitContractService()
    await wc.replace_waits(
        pid,
        EXEC,
        [{"kind": "timer", "ref": "2030-01-01T15:00:00Z"}],
        phase="waiting",
    )
    await _simulate_real_timeout(pid, EXEC)
    before = _now_ms()
    drifted = await wc.replace_waits(
        pid,
        EXEC,
        [{"kind": "timer", "ref": "2030-01-01T15:00:00+00:00"}],
        phase="waiting",
    )
    ttl = drifted[0]["expiresAt"] - before
    assert TTL_MS * 4 - 3000 <= ttl <= TTL_MS * 4 + 5000, "格式漂移后应仍为 ×4"


async def test_timer_backoff_never_overshoots_target(task_env):
    """退避窗够到目标即转 target_reached —— 只会少醒，不会漏醒。

    目标 30min：档 0（15min）封顶 → ttl_cap；真超时一次后档 1（1h）
    已盖过目标 → 必须改按目标排队，而不是"多等 1h 才醒"。
    """
    pid = task_env["project_id"]
    wc = WaitContractService()
    target = _now_ms() + 30 * 60 * 1000
    ref = f"{target}"
    first = await wc.replace_waits(
        pid, EXEC, [{"kind": "timer", "ref": ref}], phase="waiting"
    )
    assert wait_wakeup_reason(first[0]) == "ttl_cap"  # 档 0：15min < 目标
    assert first[0]["expiresAt"] < target
    await _simulate_real_timeout(pid, EXEC)

    second = await wc.replace_waits(
        pid, EXEC, [{"kind": "timer", "ref": ref}], phase="waiting"
    )
    w = second[0]
    assert wait_wakeup_reason(w) == "target_reached"  # 档 1（1h）盖过目标
    assert w["expiresAt"] == parse_timer_target_ms(ref)
