"""T10：「用户行动诉求 → 无任务落地」SLA 监控回归.

S15 事故锚点（2026-09）: 用户 12:40 发返修要求（Goals Workbook 更新进
chat），执行者只读不动 1 小时+，期间无任何告警 —— 休眠中层机制
（has_dormant_idle_team）只管「有团队没派活」，管不到「用户诉求没被
转成任何任务」。

修复面（本文件钉死）:
  - src/hiveweave/services/user_directive_sla.py（新增：启发式 + SLA 扫描）
  - src/hiveweave/services/game_time.py（tick STALL_CHECK_TICKS 槽位接线）

三态约定（变异验证：删对应分支 → 对应用例失败）:
  - 幂等：SLA 破口只投一次 n1（inbox idempotency_key 去重）
  - 升级：超 2×SLA 后下一轮扫描投 n2（收件 agent + CEO），至多两条
  - 白名单：寒暄/确认/纯疑问句永不触发（防误报）
  - 落地豁免：消息之后出现 新任务/派工/收件 agent 新 run ⇒ 不提醒
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services.user_directive_sla import (
    check_user_directive_sla,
    is_action_directive,
)

PROJECT_ID = "test-udsla-project"
CEO_ID = "udsla-ceo"
EXEC_ID = "udsla-exec"


@pytest.fixture
async def env(monkeypatch):
    monkeypatch.setenv("HIVEWEAVE_USER_DIRECTIVE_SLA_MIN", "1")  # 1 分钟，测试快
    with tempfile_dir() as workspace_path:

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        for aid in (CEO_ID, EXEC_ID):
            project_db._agent_cache[aid] = workspace_path

        conn = await ensure_project_db(workspace_path)
        ts = _now_ms()
        await conn.execute(
            "INSERT INTO agents (id, project_id, name, role, parent_id, "
            "status, permission_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, 'active', 'ceo', ?, ?)",
            [CEO_ID, PROJECT_ID, "总裁", "ceo", ts, ts])
        await conn.execute(
            "INSERT INTO agents (id, project_id, name, role, parent_id, "
            "status, permission_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', 'executor', ?, ?)",
            [EXEC_ID, PROJECT_ID, "甲", "executor", CEO_ID, ts, ts])
        await conn.commit()

        async def fake_query_one(sql, params=None):
            if "is_started" in sql:
                return {"is_started": 1}
            return None

        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.query_one", fake_query_one),
        ):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            live = project_db._cache.pop(workspace_path, None)
            for aid in (CEO_ID, EXEC_ID):
                project_db._agent_cache.pop(aid, None)
        if live is not None:
            try:
                await live.close()
            except Exception:
                pass


def tempfile_dir():
    import tempfile

    return tempfile.TemporaryDirectory()


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _insert_user_msg(env, content: str, *, age_ms: int,
                           agent_id: str = EXEC_ID,
                           source: str = "web") -> str:
    conn = await ensure_project_db(env["workspace"])
    msg_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO chat_messages (id, agent_id, role, content, "
        "is_streaming, is_background, is_read, is_context, metadata, "
        "created_at) VALUES (?, ?, 'user', ?, 0, 0, 1, 0, ?, ?)",
        [msg_id, agent_id, content,
         json.dumps({"source": source}), _now_ms() - age_ms])
    await conn.commit()
    return msg_id


async def _inbox_rows(env, agent_id: str) -> list[dict]:
    conn = await ensure_project_db(env["workspace"])
    cur = await conn.execute(
        "SELECT id, message, message_type, priority, wake, read, "
        "idempotency_key FROM inbox WHERE to_agent_id = ? "
        "ORDER BY created_at ASC",
        [agent_id])
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


# ── 启发式（白名单纯函数）────────────────────────────────────


def test_is_action_directive_three_states():
    # 行动动词 → 是诉求
    assert is_action_directive("请修复登录页的崩溃问题") is True
    assert is_action_directive("please fix the login crash") is True
    # 长（>50 字）无动词非疑问 → 是诉求
    assert is_action_directive(
        "关于本周的进展我想再确认一下整体的方向和下一步的安排，"
        "另外把相关的文档和结论同步给我一份作为参考，谢谢"
    ) is True
    # 白名单寒暄/确认 → 不是
    assert is_action_directive("收到，谢谢") is False
    assert is_action_directive("ok") is False
    assert is_action_directive("👍🎉") is False
    # 无动词的疑问句 → 不是（防长问题误报）
    assert is_action_directive(
        "能不能给我讲讲当前项目的整体架构和技术选型是怎样的？"
    ) is False
    # 空 Lutheran/纯标点 → 不是
    assert is_action_directive("") is False
    assert is_action_directive("。。。！") is False


# ── 幂等：SLA 破口只投一次 ───────────────────────────────────


async def test_sla_breach_notice_idempotent(env):
    """行动诉求超 SLA 无落地 → n1 投收件 agent 一次；SLA 与 2×SLA 之间
    重复扫描不追加（幂等窗口）。"""
    # 90s：SLA(60s) < elapsed < 2×SLA(120s) —— 只该有 n1
    await _insert_user_msg(env, "请修复导出功能的编码问题",
                           age_ms=90 * 1000)

    sent = await check_user_directive_sla(PROJECT_ID)
    assert sent == 1
    rows = await _inbox_rows(env, EXEC_ID)
    assert len(rows) == 1
    assert "[USER DIRECTIVE PENDING]" in rows[0]["message"]
    assert rows[0]["message_type"] == "platform_notice"
    assert rows[0]["wake"] == 1
    # 幂等：同状态再次扫描不追加
    assert await check_user_directive_sla(PROJECT_ID) == 0
    assert len(await _inbox_rows(env, EXEC_ID)) == 1


async def test_sla_key_shape_and_dedupe(env):
    """幂等键形状钉死：user-directive:{msg_id}:n1。"""
    msg_id = await _insert_user_msg(
        env, "请实现用户设置页的深色模式", age_ms=5 * 60 * 1000)
    await check_user_directive_sla(PROJECT_ID)
    rows = await _inbox_rows(env, EXEC_ID)
    assert rows[0]["idempotency_key"] == f"user-directive:{msg_id}:n1"


# ── 升级：超 2×SLA 再提醒一次（收件 agent + CEO）────────────


async def test_escalation_after_double_sla(env):
    """n1 已投且超 2×SLA 仍无落地 → 下一轮扫描投 n2（收件 agent + CEO），
    至多一轮，此后不再打扰。"""
    await _insert_user_msg(env, "请修复订单列表的分页错误", age_ms=10 * 60 * 1000)

    # 第一轮：只投 n1（升级梯子一档一档走，即便已过 2×SLA）
    assert await check_user_directive_sla(PROJECT_ID) == 1
    assert len(await _inbox_rows(env, EXEC_ID)) == 1

    # 第二轮：n2 → 收件 agent + CEO
    assert await check_user_directive_sla(PROJECT_ID) == 2
    exec_rows = await _inbox_rows(env, EXEC_ID)
    ceo_rows = await _inbox_rows(env, CEO_ID)
    assert len(exec_rows) == 2
    assert "[USER DIRECTIVE ESCALATED]" in exec_rows[1]["message"]
    assert len(ceo_rows) == 1
    assert "[USER DIRECTIVE ESCALATED]" in ceo_rows[0]["message"]

    # 第三轮：至多两条，静默
    assert await check_user_directive_sla(PROJECT_ID) == 0
    assert len(await _inbox_rows(env, EXEC_ID)) == 2
    assert len(await _inbox_rows(env, CEO_ID)) == 1


# ── 白名单 / 疑问句：永不触发 ────────────────────────────────


async def test_whitelist_and_questions_never_fire(env):
    await _insert_user_msg(env, "收到，谢谢", age_ms=60 * 60 * 1000)
    await _insert_user_msg(env, "你们现在整体进度怎么样？",
                           age_ms=60 * 60 * 1000)
    assert await check_user_directive_sla(PROJECT_ID) == 0
    assert await _inbox_rows(env, EXEC_ID) == []


# ── 落地豁免：出现 新任务/run ⇒ 不提醒 ──────────────────────


async def test_landing_evidence_suppresses_notice(env):
    ts = _now_ms()
    await _insert_user_msg(
        env, "请更新 Goals Workbook 里的验收标准", age_ms=30 * 60 * 1000)
    conn = await ensure_project_db(env["workspace"])
    # 消息之后 5 分钟出现了新任务（落地证据）
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, status, progress, "
        "creator_id, assignee_id, created_at, updated_at, is_archived) "
        "VALUES (?, ?, 't', 'running', 0, ?, ?, ?, ?, 0)",
        [str(uuid.uuid4()), PROJECT_ID, CEO_ID, EXEC_ID,
         ts - 25 * 60 * 1000, ts - 25 * 60 * 1000])
    await conn.commit()
    assert await check_user_directive_sla(PROJECT_ID) == 0
    assert await _inbox_rows(env, EXEC_ID) == []


async def test_recipient_run_counts_as_evidence(env):
    """收件 agent 有过执行回合（消费了消息）⇒ 不提醒（残余缺口归
    dwell/沉默看门狗，本监控不重复报警）。"""
    await _insert_user_msg(
        env, "请处理数据备份的告警", age_ms=30 * 60 * 1000)
    conn = await ensure_project_db(env["workspace"])
    await conn.execute(
        "INSERT INTO agent_runs (id, agent_id, status, started_at) "
        "VALUES (?, ?, 'completed', ?)",
        [str(uuid.uuid4()), EXEC_ID, _now_ms() - 20 * 60 * 1000])
    await conn.commit()
    assert await check_user_directive_sla(PROJECT_ID) == 0
    assert await _inbox_rows(env, EXEC_ID) == []


async def test_archived_recipient_does_not_block_scan(env):
    """按条隔离：最早一条候选的收件 agent 已 archived（send_message 抛
    ValueError）⇒ 跳过该条，后续候选照常投递（扫描不得整轮夭折）。"""
    archived_id = "udsla-archived"
    await _insert_user_msg(env, "请修复最老的那批工单",
                           age_ms=10 * 60 * 1000, agent_id=archived_id)
    await _insert_user_msg(env, "请修复最新的登录报错",
                           age_ms=5 * 60 * 1000, agent_id=EXEC_ID)

    async def fake_get_agent_by_id(aid: str):
        if aid == archived_id:
            return {"id": archived_id, "status": "archived", "name": "旧"}
        return None

    with patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id):
        sent = await check_user_directive_sla(PROJECT_ID)

    assert sent == 1  # 只有 EXEC 收到；archived 被隔离跳过
    rows = await _inbox_rows(env, EXEC_ID)
    assert len(rows) == 1
    assert "[USER DIRECTIVE PENDING]" in rows[0]["message"]


# ── tick 接线（hook 进 STALL_CHECK_TICKS 槽位，故障不炸 tick）──


async def test_tick_calls_user_directive_sla(env):
    """tick() 会调用 SLA 扫描；扫描抛错不影响 tick 其余看门狗。"""
    from hiveweave.services import game_time
    from hiveweave.services.game_time import GameTimeService

    game_time._states.clear()
    # tick() 先自增 tick_count 再判槽位 → 从 STALL_CHECK_TICKS - 1 起步
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
        return 0

    svc = GameTimeService()
    try:
        with patch(
            "hiveweave.services.user_directive_sla.check_user_directive_sla",
            fake_check,
        ):
            await svc.tick(PROJECT_ID)
        assert called["n"] == 1

        async def boom(pid):
            raise RuntimeError("scan exploded")

        with patch(
            "hiveweave.services.user_directive_sla.check_user_directive_sla",
            boom,
        ):
            # 不抛 —— 独立 try/except 吞掉，tick 正常收尾
            await svc.tick(PROJECT_ID)
    finally:
        game_time._states.clear()
