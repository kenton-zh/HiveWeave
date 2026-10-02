"""T21：ask(reply_required) 无回复升级 SLA 回归.

S16 事故锚点（2026-09）: 霜叶向牧云发 reply_required ask（二选一决策），
牧云反刍卡死 3 小时不答，霜叶无限等待；跨级求救被指挥链 ACL 硬禁 ——
一人卡死整条线，平台没有任何上报出口。

修复面（本文件钉死）:
  - src/hiveweave/services/ask_escalation.py（新增：SLA 两段式升级扫描）
  - src/hiveweave/services/game_time.py（tick STALL_CHECK_TICKS 槽位尾部追加）

判据（变异验证：删对应分支 → 对应用例失败）:
  - 两段式：L1 → asker 的组织 parent；超一倍时长后下一轮扫描 L2 →
    grandparent（直到 CEO 根为止）；每轮至多补一档
  - 幂等：显式 idempotency_key = ask-esc:{contract}:L{n}，重复扫描不追加
  - 关闭豁免：合约被 reply_to 关闭 ⇒ 永不升级
  - 未到期豁免：age < SLA ⇒ 不升级
  - 无处可升：asker 是根（CEO）⇒ 跳过
"""

from __future__ import annotations

import tempfile
import time
import uuid
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services.ask_escalation import (
    check_ask_escalation,
    escalation_key,
)

PROJECT_ID = "test-ask-esc-project"
CEO_ID = "askesc-ceo"
MID_ID = "askesc-mid"
LEAF_ID = "askesc-leaf"
PEER_ID = "askesc-peer"

_AGENTS: dict[str, dict] = {
    CEO_ID: {"id": CEO_ID, "name": "总裁", "parent_id": None,
             "is_archived": 0, "status": "active"},
    MID_ID: {"id": MID_ID, "name": "牧云", "parent_id": CEO_ID,
             "is_archived": 0, "status": "active"},
    LEAF_ID: {"id": LEAF_ID, "name": "霜叶", "parent_id": MID_ID,
              "is_archived": 0, "status": "active"},
    PEER_ID: {"id": PEER_ID, "name": "同侪", "parent_id": MID_ID,
              "is_archived": 0, "status": "active"},
}


@pytest.fixture
async def env(monkeypatch):
    monkeypatch.setenv("HIVEWEAVE_ASK_ESCALATION_MIN", "1")  # 1 分钟，测试快
    with tempfile.TemporaryDirectory() as workspace_path:

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        for aid in (CEO_ID, MID_ID, LEAF_ID, PEER_ID):
            project_db._agent_cache[aid] = workspace_path

        conn = await ensure_project_db(workspace_path)
        ts = _now_ms()
        for aid, spec in _AGENTS.items():
            await conn.execute(
                "INSERT INTO agents (id, project_id, name, role, parent_id, "
                "status, permission_type, created_at, updated_at) "
                "VALUES (?, ?, ?, 'executor', ?, 'active', 'executor', ?, ?)",
                [aid, PROJECT_ID, spec["name"], spec["parent_id"], ts, ts])
        await conn.commit()

        async def fake_meta_get_agent_by_id(aid: str):
            return dict(_AGENTS[aid]) if aid in _AGENTS else None

        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_by_id",
                  fake_meta_get_agent_by_id),
        ):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            live = project_db._cache.pop(workspace_path, None)
            for aid in (CEO_ID, MID_ID, LEAF_ID, PEER_ID):
                project_db._agent_cache.pop(aid, None)
        if live is not None:
            try:
                await live.close()
            except Exception:
                pass


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _insert_open_ask(
    env,
    *,
    from_agent_id: str = LEAF_ID,
    to_agent_id: str = PEER_ID,
    age_ms: int,
    message: str = "M4 材质走方案 A 还是方案 B？二选一，需要你拍板",
) -> str:
    """插入一条未关闭的 reply_required ask，返回 reply_contract_id。"""
    conn = await ensure_project_db(env["workspace"])
    cid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
        "created_at, message_type, expect_report, priority, task_id, wake, "
        "delivered, wake_category, reply_contract_id, reply_to) "
        "VALUES (?, ?, ?, ?, 0, ?, 'ask', 1, 'normal', NULL, 1, 0, NULL, "
        "?, NULL)",
        [str(uuid.uuid4()), from_agent_id, to_agent_id, message,
         _now_ms() - age_ms, cid])
    await conn.commit()
    return cid


async def _close_contract(env, contract_id: str) -> None:
    """写一条带 reply_to 的回下行，模拟 ask 已被回复（合约关闭）。"""
    conn = await ensure_project_db(env["workspace"])
    await conn.execute(
        "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
        "created_at, message_type, expect_report, priority, task_id, wake, "
        "delivered, wake_category, reply_contract_id, reply_to) "
        "VALUES (?, ?, ?, '方案 B', 0, ?, 'normal', 0, 'normal', NULL, 0, "
        "0, NULL, NULL, ?)",
        [str(uuid.uuid4()), PEER_ID, LEAF_ID, _now_ms(), contract_id])
    await conn.commit()


async def _inbox_rows(env, agent_id: str) -> list[dict]:
    conn = await ensure_project_db(env["workspace"])
    cur = await conn.execute(
        "SELECT id, message, message_type, priority, wake, idempotency_key "
        "FROM inbox WHERE to_agent_id = ? ORDER BY created_at ASC",
        [agent_id])
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


# ── 两段式：L1 → parent；再超一倍 → grandparent（CEO）──────────


async def test_two_stage_escalation_chain(env):
    """超 SLA 先升 parent（L1）；下一轮扫描再升 CEO（L2）；此后静默。"""
    cid = await _insert_open_ask(env, age_ms=5 * 60 * 1000)  # 已超 2×SLA

    # 第一轮：即便已过 2×SLA，也只投 L1（梯子一档一档走）
    sent = await check_ask_escalation(PROJECT_ID)
    assert [s["level"] for s in sent] == [1]
    assert sent[0]["escalated_to"] == MID_ID
    mid_rows = await _inbox_rows(env, MID_ID)
    assert len(mid_rows) == 1
    assert "[ASK ESCALATION]" in mid_rows[0]["message"]
    assert "你的下属在等待决策" in mid_rows[0]["message"]
    assert "方案 A 还是方案 B" in mid_rows[0]["message"]  # ask 摘要在场
    assert mid_rows[0]["wake"] == 1
    assert mid_rows[0]["priority"] == "urgent"
    assert mid_rows[0]["message_type"] == "escalation"
    assert await _inbox_rows(env, CEO_ID) == []

    # 第二轮：L2 → CEO（MID 的上级 = 根）
    sent = await check_ask_escalation(PROJECT_ID)
    assert [s["level"] for s in sent] == [2]
    assert sent[0]["escalated_to"] == CEO_ID
    ceo_rows = await _inbox_rows(env, CEO_ID)
    assert len(ceo_rows) == 1
    assert "[ASK ESCALATION]" in ceo_rows[0]["message"]
    # L3 不存在：链深只有 2 级（leaf → mid → ceo），到 CEO 为止
    assert await check_ask_escalation(PROJECT_ID) == []
    assert len(await _inbox_rows(env, MID_ID)) == 1
    assert len(await _inbox_rows(env, CEO_ID)) == 1


async def test_young_ask_not_escalated(env):
    """age < SLA ⇒ 不升级。"""
    await _insert_open_ask(env, age_ms=10 * 1000)
    assert await check_ask_escalation(PROJECT_ID) == []
    assert await _inbox_rows(env, MID_ID) == []
    assert await _inbox_rows(env, CEO_ID) == []


# ── 幂等 ─────────────────────────────────────────────────────


async def test_idempotent_per_contract_level(env):
    """同一合约同一档只投一次（显式幂等键）；键形状钉死。"""
    cid = await _insert_open_ask(env, age_ms=90 * 1000)  # SLA(60s) < age < 2×SLA
    await check_ask_escalation(PROJECT_ID)
    rows = await _inbox_rows(env, MID_ID)
    assert len(rows) == 1
    assert rows[0]["idempotency_key"] == escalation_key(cid, 1)
    assert rows[0]["idempotency_key"] == f"ask-esc:{cid}:L1"
    # 重扫：L1 已投（幂等跳过）、L2 未到期 ⇒ 零新增
    assert await check_ask_escalation(PROJECT_ID) == []
    assert len(await _inbox_rows(env, MID_ID)) == 1
    assert await _inbox_rows(env, CEO_ID) == []


# ── 关闭豁免：被回复的 ask 永不升级 ──────────────────────────


async def test_closed_contract_never_escalates(env):
    cid = await _insert_open_ask(env, age_ms=5 * 60 * 1000)
    await _close_contract(env, cid)
    assert await check_ask_escalation(PROJECT_ID) == []
    assert await _inbox_rows(env, MID_ID) == []
    assert await _inbox_rows(env, CEO_ID) == []


# ── 无处可升：asker 是根（CEO）⇒ 跳过 ────────────────────────


async def test_ceo_asker_has_no_escalation_target(env):
    await _insert_open_ask(
        env, from_agent_id=CEO_ID, to_agent_id=MID_ID, age_ms=5 * 60 * 1000)
    assert await check_ask_escalation(PROJECT_ID) == []
    # 无升级 notice（ask 本身在 MID 收件箱，但不会有 escalation 行）
    for aid in (MID_ID, CEO_ID):
        rows = [r for r in await _inbox_rows(env, aid)
                if r["message_type"] == "escalation"]
        assert rows == []


# ── tick 接线（STALL_CHECK_TICKS 槽位尾部追加，故障不炸 tick）──


async def test_tick_calls_ask_escalation(env):
    from hiveweave.services.game_time import GameTimeService
    from hiveweave.services import game_time

    game_time._states.clear()
    game_time._states[PROJECT_ID] = {
        "alarms": [],
        "tick_count": game_time.STALL_CHECK_TICKS - 1,
        "real_started_at": _now_ms() // 1000,
        "current_game_seconds": 0,
        "silence_trackers": {},
        "stall_trackers": {},
    }
    called = {"n": 0}

    async def fake_check(pid):
        called["n"] += 1
        assert pid == PROJECT_ID
        return []

    svc = GameTimeService()
    try:
        with patch(
            "hiveweave.services.ask_escalation.check_ask_escalation",
            fake_check,
        ):
            await svc.tick(PROJECT_ID)
        assert called["n"] == 1

        async def boom(pid):
            raise RuntimeError("scan exploded")

        with patch(
            "hiveweave.services.ask_escalation.check_ask_escalation",
            boom,
        ):
            # 不抛 —— 独立 try/except 吞掉，tick 正常收尾
            await svc.tick(PROJECT_ID)
    finally:
        game_time._states.clear()


# ── 纯函数：SLA env 读取 ─────────────────────────────────────


def test_sla_env_parsing(monkeypatch):
    from hiveweave.services.ask_escalation import _sla_ms

    monkeypatch.setenv("HIVEWEAVE_ASK_ESCALATION_MIN", "45")
    assert _sla_ms() == 45 * 60 * 1000
    monkeypatch.setenv("HIVEWEAVE_ASK_ESCALATION_MIN", "junk")
    assert _sla_ms() == 30 * 60 * 1000
    monkeypatch.delenv("HIVEWEAVE_ASK_ESCALATION_MIN", raising=False)
    assert _sla_ms() == 30 * 60 * 1000
    monkeypatch.setenv("HIVEWEAVE_ASK_ESCALATION_MIN", "0")
    assert _sla_ms() >= 60 * 1000  # 钳到 ≥1 分钟
