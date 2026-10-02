"""TEST_DSH_77：中层休眠形态（下属全员闲置 + 零派活）唤醒缺口回归.

事故锚点（TEST_DSH_77 实测两次停摆, 2026-09）:
  技术负责人（coordinator, CEO 直属）完成设计稿合并 + 招齐 6 executor 后，
  自己名下无已派任务、下属全员闲置 —— ``has_open_work=false`` ⇒ 延续唤醒
  链被掐断（ADR-001「force 唤醒三层链少接一层就蒸发」），中层再也不会
  醒来派活；运营侧 nudge 只能解一次。

修复面（本文件钉死）:
  - src/hiveweave/services/tasks/obligations.py
      (has_dormant_idle_team 五条件形态 + has_open_work 增量判定)
  - src/hiveweave/services/game_time.py
      (_check_silent_agents 合法 idle 闸门 Gate B 闭式复核 + notice 投递)
  - src/hiveweave/services/health_supervisor.py
      (_should_skip_agent 降级兜底同构复核)
  - src/hiveweave/agents/trigger.py
      (maybe_notify_dormant_lead 幂等 notice + build_trigger_context
       force 处方块)

变异验证约定（删掉修复代码对应测试必须失败）:
  - 删 has_dormant_idle_team / has_open_work 增量分支 → form1 失败
  - Gate B 换回无条件 skip → watchdog 用例失败
  - notice 幂等键去掉 → idempotent 用例失败
  - 幂等键去掉下属名下任务总数换代 → generation 用例失败

T28（D79 木棉，2026-09）增量:
  条件 3 被自派任务击穿 —— 中层把骨架任务派给自己（claimed 永久占位
  obligations）⇒ ``if obligations: return False`` 短路 ⇒ [DORMANT TEAM]
  结构性不可达（反刍 3h 无人升级、8 executor 全闲置）。修复：条件 3 放
  宽为「名下任务**全部**长时间零进展（``dormant_stuck_own_task``，env
  ``HIVEWEAVE_DORMANT_OWN_TASK_STALL_MS`` 默认 30 分钟）且下属全闲置 ⇒
  仍判 dormant」；notice/上下文追加拆活后缀；3.7 处方块不再被
  ``not blocks`` 短路。对应 form7-form10 + d79 上下文用例。
"""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.realtime.event_bus import status_event_bus
from hiveweave.services import game_time
from hiveweave.services import task as task_mod
from hiveweave.services import wait_contract as wait_contract_module
from hiveweave.services.game_time import GameTimeService
from hiveweave.services.task import TaskService

PROJECT_ID = "test-dsh77-project"
CEO_ID = "dsh77-ceo"
MID_ID = "dsh77-mid"
MID2_ID = "dsh77-mid2"  # 非 CEO 直属的中层（form 6）
EXEC_A = "dsh77-exec-a"
EXEC_B = "dsh77-exec-b"
EXEC_C = "dsh77-exec-c"  # MID2 的下属


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        wait_contract_module._migrated.clear()
        task_mod._migrated.clear()
        for aid in (CEO_ID, MID_ID, MID2_ID, EXEC_A, EXEC_B, EXEC_C):
            project_db._agent_cache[aid] = workspace_path

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
            for aid in (CEO_ID, MID_ID, MID2_ID, EXEC_A, EXEC_B, EXEC_C):
                project_db._agent_cache.pop(aid, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


# ── Helpers ─────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _insert_agent(
    env, agent_id, name, *, parent_id=None, role="executor",
    permission_type="executor",
):
    conn = await ensure_project_db(env["workspace_path"])
    ts = _now_ms()
    await conn.execute(
        "INSERT INTO agents (id, project_id, name, role, parent_id, status, "
        "permission_type, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        [agent_id, PROJECT_ID, name, role, parent_id, permission_type,
         ts, ts])
    await conn.commit()


async def _insert_idle_org(env, *, second_mid=False):
    """CEO + 中层 + 2 名 active executor，零任务（D77 休眠现场形状）。"""
    await _insert_agent(env, CEO_ID, "总裁", role="ceo",
                        permission_type="ceo")
    await _insert_agent(env, MID_ID, "雾岚", parent_id=CEO_ID,
                        role="coordinator", permission_type="coordinator")
    if second_mid:
        await _insert_agent(env, MID2_ID, "深层中层", parent_id=MID_ID,
                            role="coordinator", permission_type="coordinator")
    await _insert_agent(env, EXEC_A, "甲", parent_id=MID_ID)
    await _insert_agent(env, EXEC_B, "乙", parent_id=MID_ID)
    if second_mid:
        await _insert_agent(env, EXEC_C, "丙", parent_id=MID2_ID)


async def _insert_task(
    env, *, status, assignee_id=None, creator_id=CEO_ID, claimed_at=None,
    title="dsh77 task", archived=0, updated_at=None, reviewer_id=None,
):
    task_mod._migrated.clear()
    await task_mod._ensure_schema(PROJECT_ID)
    conn = await ensure_project_db(env["workspace_path"])
    old = _now_ms() - 40 * 60 * 1000
    ts = updated_at if updated_at is not None else old
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, status, progress, "
        "creator_id, assignee_id, created_at, updated_at, is_archived, "
        "claimed_at, reviewer_id) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), PROJECT_ID, title, status, creator_id,
         assignee_id, old, ts, archived,
         old if claimed_at is None else claimed_at, reviewer_id])
    await conn.commit()


async def _inbox_rows(env, agent_id):
    conn = await ensure_project_db(env["workspace_path"])
    cur = await conn.execute(
        "SELECT id, message, message_type, priority, wake, read, "
        "idempotency_key FROM inbox WHERE to_agent_id = ?",
        [agent_id])
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


# ── 六形态 ──────────────────────────────────────────────────


async def test_form1_dormant_mid_has_work_and_notice_idempotent(env):
    """形态 1：中层 + active executor 下属全员闲置 + 零派任务 ⇒ 有活，
    且 notice 注入一次并幂等（同批下属 + 零派任务状态只投一次）。"""
    await _insert_idle_org(env)
    svc = TaskService()

    # 判定：闭式源 + has_open_work 增量
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is True
    assert await svc.has_open_work(PROJECT_ID, MID_ID) is True

    # notice：第一次投递
    from hiveweave.agents.trigger import maybe_notify_dormant_lead

    assert await maybe_notify_dormant_lead(PROJECT_ID, MID_ID) is True
    rows = await _inbox_rows(env, MID_ID)
    assert len(rows) == 1
    assert "[DORMANT TEAM]" in rows[0]["message"]
    assert "dispatch_task" in rows[0]["message"]
    assert rows[0]["wake"] == 1
    assert (rows[0]["idempotency_key"] or "").startswith("dormant-lead:")

    # 幂等：同状态二次调用不追加
    assert await maybe_notify_dormant_lead(PROJECT_ID, MID_ID) is True
    assert len(await _inbox_rows(env, MID_ID)) == 1


async def test_form2_dispatch_stops_applicability(env):
    """形态 2：派活后（哪怕 created 未认领）⇒ 形态不适用，has_open_work
    回到 False（唤醒压力自然解除——自消散性）。"""
    await _insert_idle_org(env)
    svc = TaskService()
    await _insert_task(env, status="created", assignee_id=EXEC_A,
                       creator_id=MID_ID, claimed_at=None)
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is False
    assert await svc.has_open_work(PROJECT_ID, MID_ID) is False


async def test_form3_executor_never_applies(env):
    """形态 3：executor（无下属）永远不适用；唤醒节奏零变化。"""
    await _insert_idle_org(env)
    svc = TaskService()
    assert await svc.has_dormant_idle_team(PROJECT_ID, EXEC_A) is False
    assert await svc.has_open_work(PROJECT_ID, EXEC_A) is False


async def test_form4_mid_with_own_open_task_not_applicable(env):
    """形态 4：中层名下有**推进中**（updated_at 新鲜）的未结任务 ⇒ 形态
    不适用（走账面义务路径）。T28 后「零进展占位」的变体见 form7。"""
    await _insert_idle_org(env)
    svc = TaskService()
    await _insert_task(env, status="running", assignee_id=MID_ID,
                       creator_id=CEO_ID, updated_at=_now_ms())
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is False
    # has_open_work 经账面义务判 True —— 与形态无关
    assert await svc.has_open_work(PROJECT_ID, MID_ID) is True


async def test_form5_children_all_busy_not_applicable(env):
    """形态 5：下属全忙（S15 健康项目形状：下属名下有 running 任务）
    ⇒ 形态不适用；has_open_work 回归零变化（False）。"""
    await _insert_idle_org(env)
    svc = TaskService()
    old = _now_ms() - 40 * 60 * 1000
    await _insert_task(env, status="running", assignee_id=EXEC_A,
                       creator_id=MID_ID, claimed_at=old)
    await _insert_task(env, status="claimed", assignee_id=EXEC_B,
                       creator_id=MID_ID, claimed_at=old)
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is False
    assert await svc.has_open_work(PROJECT_ID, MID_ID) is False


async def test_form6_non_ceo_parent_not_applicable(env):
    """形态 6：coordinator 套 coordinator（非 CEO 直属）不适用。"""
    await _insert_idle_org(env, second_mid=True)
    svc = TaskService()
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID2_ID) is False
    assert await svc.has_open_work(PROJECT_ID, MID2_ID) is False
    # CEO 直属的 MID 仍适用（同库对照）
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is True


# ── T28（D79 木棉）：条件 3 放宽 —— 自派零进展占位仍判 dormant ──


async def test_form7_self_assigned_stalled_task_still_dormant(env):
    """形态 7（D79 真实形状）：中层把骨架任务派给自己（claimed 占位
    obligations，40 分钟零进展）+ 8 executor 全闲置 ⇒ 仍判 dormant，
    notice 追加「你名下任务…分钟无进展」后缀；幂等键语义不变
    （dormant-lead:*，状态变化自然换代）。"""
    await _insert_idle_org(env)
    svc = TaskService()
    await _insert_task(env, status="claimed", assignee_id=MID_ID,
                       creator_id=MID_ID)  # updated_at 默认 40min 前 ⇒ 零进展
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is True
    # has_open_work 语义不变：自派任务本就走账面义务分支判 True
    assert await svc.has_open_work(PROJECT_ID, MID_ID) is True

    from hiveweave.agents.trigger import maybe_notify_dormant_lead

    assert await maybe_notify_dormant_lead(PROJECT_ID, MID_ID) is True
    rows = await _inbox_rows(env, MID_ID)
    assert len(rows) == 1
    assert "[DORMANT TEAM]" in rows[0]["message"]
    assert "分钟无进展" in rows[0]["message"]
    assert "拆分派活或求助" in rows[0]["message"]
    assert (rows[0]["idempotency_key"] or "").startswith("dormant-lead:")
    assert rows[0]["wake"] == 1
    # 幂等语义不变：同状态二次调用不追加
    assert await maybe_notify_dormant_lead(PROJECT_ID, MID_ID) is True
    assert len(await _inbox_rows(env, MID_ID)) == 1


async def test_form8_progressing_own_task_not_dormant(env):
    """T28 防误报：名下任务 progress 在涨（updated_at 新鲜）⇒ 正常执行中，
    不判 dormant。"""
    await _insert_idle_org(env)
    svc = TaskService()
    await _insert_task(env, status="running", assignee_id=MID_ID,
                       creator_id=MID_ID, updated_at=_now_ms())
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is False


async def test_form9_review_obligation_not_dormant(env):
    """T28 防误报：真实待审义务（reviewer 窗口）= 有可行动作 ⇒ 不判
    dormant（哪怕 executor 下属全员零被派）。"""
    await _insert_idle_org(env)
    svc = TaskService()
    await _insert_task(env, status="submitted", assignee_id=None,
                       creator_id=EXEC_A, reviewer_id=MID_ID)
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is False


async def test_form10_stall_threshold_env_tunable(env, monkeypatch):
    """T28：零进展阈值 env 可调 —— 调大后同一占位任务不再判卡死。"""
    from hiveweave.services.tasks import obligations as obligations_mod

    await _insert_idle_org(env)
    svc = TaskService()
    await _insert_task(env, status="claimed", assignee_id=MID_ID,
                       creator_id=MID_ID)  # 40 分钟零进展
    monkeypatch.setattr(
        obligations_mod, "DORMANT_OWN_TASK_STALL_MS", 60 * 60 * 1000)
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is False
    monkeypatch.setattr(
        obligations_mod, "DORMANT_OWN_TASK_STALL_MS", 30 * 60 * 1000)
    assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is True


# ── 边界钉子 ────────────────────────────────────────────────


async def test_active_run_blocks_form(env):
    """条件 5：自己 processing 中 ⇒ 形态不适用。"""
    await _insert_idle_org(env)
    svc = TaskService()
    with patch(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(MID_ID, PROJECT_ID)],
    ):
        assert await svc.has_dormant_idle_team(PROJECT_ID, MID_ID) is False


async def test_wait_parking_still_wins_over_dormant_form(env):
    """防风暴关键：未过期 wait 契约（合法停泊）冻结一切 —— 休眠形态
    不得穿透 wait 负项（commit_turn(waiting) 的中层不被误醒）。"""
    await _insert_idle_org(env)
    wait_contract_module._migrated.clear()
    from hiveweave.services.wait_contract import wait_contract_service

    await wait_contract_service.list_all_active(PROJECT_ID)
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO agent_waits (id, agent_id, project_id, kind, ref, "
        "expires_at, created_at) VALUES (?, ?, ?, 'task', 'dsh77', ?, ?)",
        [str(uuid.uuid4()), MID_ID, PROJECT_ID,
         _now_ms() + 10 * 60 * 1000, _now_ms()])
    await conn.commit()
    svc = TaskService()
    assert await svc.has_open_work(PROJECT_ID, MID_ID) is False


async def test_notice_generation_advances_after_dispatch_cycle(env):
    """幂等键换代：下属名下历史任务总数增长（一轮完整派活-收口后同批
    休眠复发）⇒ 新键 ⇒ 重新投递（状态变化自然失效）。"""
    await _insert_idle_org(env)
    from hiveweave.agents.trigger import maybe_notify_dormant_lead

    assert await maybe_notify_dormant_lead(PROJECT_ID, MID_ID) is True
    assert len(await _inbox_rows(env, MID_ID)) == 1
    # 一轮派活收口：closed 任务行仍在（历史总数 +1）⇒ 键换代
    await _insert_task(env, status="closed", assignee_id=EXEC_A,
                       creator_id=MID_ID)
    # 注意：closed 是终态 ⇒ 形态仍成立（下属依旧零在派任务）
    assert await TaskService().has_dormant_idle_team(
        PROJECT_ID, MID_ID) is True
    assert await maybe_notify_dormant_lead(PROJECT_ID, MID_ID) is True
    rows = await _inbox_rows(env, MID_ID)
    assert len(rows) == 2
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]


# ── 唤醒链接线 ──────────────────────────────────────────────


def _complete_agent_mock(aid, role="executor"):
    return SimpleNamespace(
        disposition="complete",
        project_id=PROJECT_ID,
        config={"role": role},
    )


class _StartedMock:
    def __init__(self, started=1):
        from hiveweave.db import meta as meta_db

        async def fake_query_one(sql, params=None):
            if "is_started" in sql:
                return {"is_started": started}
            return None

        self._patcher = patch.object(meta_db, "query_one", fake_query_one)

    def __enter__(self):
        self._patcher.start()
        return self

    def __exit__(self, *args):
        self._patcher.stop()


async def test_watchdog_gate_b_wakes_dormant_mid_with_notice(
        env, monkeypatch):
    """Gate B 接线：obligations/ask/wait 全空的中层休眠形态不得在
    「合法 idle」闸门被豁免 —— 必须投递处方 notice 且被 force 唤醒
    （修复前在此二次蒸发，D77 两次停摆的机制根因）。变异：Gate B
    换回无条件 skip → 本测试失败。"""
    old = _now_ms() - 40 * 60 * 1000
    conn = await ensure_project_db(env["workspace_path"])
    for aid, name, parent, role, perm in (
        (CEO_ID, "总裁", None, "ceo", "ceo"),
        (MID_ID, "雾岚", CEO_ID, "coordinator", "coordinator"),
        (EXEC_A, "甲", MID_ID, "executor", "executor"),
        (EXEC_B, "乙", MID_ID, "executor", "executor"),
    ):
        await conn.execute(
            "INSERT INTO agents (id, project_id, name, role, parent_id, "
            "status, permission_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            [aid, PROJECT_ID, name, role, parent, perm, old, old])
    await conn.commit()

    game_time._states.clear()
    game_time._states[PROJECT_ID] = {
        "silence_trackers": {},
        "stall_trackers": {},
        "current_game_seconds": 0,
    }
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda aid: _complete_agent_mock(aid, "coordinator"))
    mock_coord = AsyncMock()
    mock_sub = AsyncMock()
    mock_bus = AsyncMock()
    mock_send = AsyncMock()
    try:
        with _StartedMock(1), \
             patch("hiveweave.agents.trigger.trigger_coordinator",
                   mock_coord), \
             patch("hiveweave.agents.trigger.trigger_subordinate",
                   mock_sub), \
             patch.object(status_event_bus, "publish_stream_event",
                          mock_bus), \
             patch("hiveweave.services.inbox.InboxService.send_message",
                   mock_send):
            await GameTimeService()._check_silent_agents(PROJECT_ID)
    finally:
        game_time._states.clear()

    # 处方 notice 已投递（幂等键 + wake 文案）
    assert mock_send.await_count >= 1
    kwargs = mock_send.await_args.kwargs
    assert kwargs.get("to_agent_id") == MID_ID
    assert "[DORMANT TEAM]" in (kwargs.get("message") or "")
    assert (kwargs.get("idempotency_key") or "").startswith("dormant-lead:")
    # 中层被穿透唤醒且 force=True
    coord_calls = [c for c in mock_coord.await_args_list
                   if c.args and c.args[0] == MID_ID]
    assert coord_calls, "休眠中层必须被唤醒"
    assert all(c.kwargs.get("force") is True for c in coord_calls)
    # executor 保持零变化：不被唤醒（合法 idle 豁免不动）。
    # （CEO 被 project_has_unresolved_work 的「待命叶子」项目根仲裁
    # 唤醒是**既有设计**，与本修复无关 —— 不在断言范围。）
    sub_calls = [c for c in (mock_coord.await_args_list
                             + mock_sub.await_args_list)
                 if c.args and c.args[0] in (EXEC_A, EXEC_B)]
    assert not sub_calls


async def test_force_trigger_context_carries_prescription(env):
    """build_trigger_context 处方块：force + 账面义务空 + blocks 全空时，
    休眠中层拿到处方指引（唤醒在最后一米不蒸发）；非休眠 → 无块。"""
    from hiveweave.agents.trigger import build_trigger_context

    await _insert_idle_org(env)
    agent_record = {
        "id": MID_ID, "project_id": PROJECT_ID,
        "name": "雾岚", "role": "coordinator",
    }
    with patch("hiveweave.agents.supervisor.agent_manager.get_agent",
               return_value=None):
        result = await build_trigger_context(
            agent_record, "coordinator", force=True)
    assert result is not None
    context = result[0]
    assert "[DORMANT TEAM]" in context
    assert "dispatch_task" in context

    # 对照：派活后（形态不适用）→ 无块 → None（不空转烧 token）
    await _insert_task(env, status="created", assignee_id=EXEC_A,
                       creator_id=MID_ID, claimed_at=None)
    with patch("hiveweave.agents.supervisor.agent_manager.get_agent",
               return_value=None):
        result2 = await build_trigger_context(
            agent_record, "coordinator", force=True)
    assert result2 is None


async def test_force_trigger_context_d79_shape_carries_prescription(env):
    """T28：自派零进展占位形态下，force 唤醒上下文必须带处方 —— 即使
    3.6 的 Open Work 块已存在（旧闸门 `not blocks` 会把处方整个吞掉，
    D79 反刍 36 次唤醒拿不到解药的机制根因）。"""
    from hiveweave.agents.trigger import build_trigger_context

    await _insert_idle_org(env)
    await _insert_task(env, status="claimed", assignee_id=MID_ID,
                       creator_id=MID_ID)  # 自派占位 + 40 分钟零进展
    agent_record = {
        "id": MID_ID, "project_id": PROJECT_ID,
        "name": "木棉", "role": "coordinator",
    }
    with patch("hiveweave.agents.supervisor.agent_manager.get_agent",
               return_value=None):
        result = await build_trigger_context(
            agent_record, "coordinator", force=True)
    assert result is not None
    context = result[0]
    # Open Work（账面义务）与 DORMANT TEAM 处方同屏
    assert "Open Work" in context
    assert "[DORMANT TEAM]" in context
    # 追加自派卡死后缀
    assert "分钟无进展" in context
    assert "拆分派活或求助" in context
