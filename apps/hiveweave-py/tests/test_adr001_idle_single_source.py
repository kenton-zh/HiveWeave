"""ADR-001: agent "idle" 单一判定源（has_open_work）回归测试.

被测文件:
  - src/hiveweave/services/tasks/obligations.py (get_open_work_obligations /
    has_open_work / TERMINAL_STATUSES)
  - src/hiveweave/services/game_time.py (_check_silent_agents complete 豁免、
    _watchdog_trigger R2 接线、project_has_unresolved_work)
  - src/hiveweave/services/turn_exit.py (完成闸消费闭式清单 → done_slice
    在 blocked/未来状态下不放行)

事故锚点（TEST_DSH_21 / Orion A178, 2026-08-21 19:32）:
  complete agent + 名下 running 任务 → 平台全链路静默 80min。
  本文件钉死：complete 不再是无条件免死金牌；assignee 侧闭式覆盖
  blocked / 未来新增状态；wait 停泊不误醒；派活中协调者不误醒。

变异验证约定（删掉修复代码对应测试必须失败）:
  - 删 has_open_work / 换回白名单 → test_has_open_work_* 系列失败
  - _watchdog_trigger 换回 get_actionable_obligations → R2b 用例失败
  - complete 分支换回无条件 continue → watchdog 用例失败
  - 从 TERMINAL_STATUSES 删终态成员 → R1 用例失败
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
from hiveweave.services.tasks.constants import TERMINAL_STATUSES
from hiveweave.services.turn_exit import ExitContext, evaluate_turn_exit
from hiveweave.services.turn_result import TurnResult

PROJECT_ID = "test-adr001-project"
CEO_ID = "test-adr001-ceo"
COORD_ID = "test-adr001-coord"
EXECUTOR_ID = "test-adr001-executor"
OTHER_EXECUTOR_ID = "test-adr001-executor-b"


@pytest.fixture(autouse=True)
def clean_states():
    game_time._states.clear()
    yield
    game_time._states.clear()


@pytest.fixture
async def env():
    from hiveweave.services import task as task_mod_

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        wait_contract_module._migrated.discard(PROJECT_ID)
        task_mod_._migrated.discard(PROJECT_ID)
        for aid in (CEO_ID, COORD_ID, EXECUTOR_ID, OTHER_EXECUTOR_ID):
            project_db._agent_cache[aid] = workspace_path

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
            for aid in (CEO_ID, COORD_ID, EXECUTOR_ID, OTHER_EXECUTOR_ID):
                project_db._agent_cache.pop(aid, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


# ── Helpers ─────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _insert_agent(env, agent_id, name, parent_id=None,
                        created_at=None, role="executor",
                        permission_type=None):
    conn = await ensure_project_db(env["workspace_path"])
    ts = created_at if created_at is not None else _now_ms()
    if permission_type is None:
        await conn.execute(
            "INSERT INTO agents (id, project_id, name, role, parent_id, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            [agent_id, PROJECT_ID, name, role, parent_id, ts, ts])
    else:
        await conn.execute(
            "INSERT INTO agents (id, project_id, name, role, parent_id, "
            "status, permission_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            [agent_id, PROJECT_ID, name, role, parent_id,
             permission_type, ts, ts])
    await conn.commit()


async def _insert_task(env, *, status, assignee_id=None, creator_id=CEO_ID,
                       reviewer_id=None, claimed_at=-1, title="ADR-001 task"):
    """Raw INSERT 任务行。claimed_at=-1 表示 NULL（未认领）。"""
    task_mod._migrated.discard(PROJECT_ID)
    await task_mod._ensure_schema(PROJECT_ID)
    tid = str(uuid.uuid4())
    old = _now_ms() - 40 * 60 * 1000
    conn = await ensure_project_db(env["workspace_path"])
    cols = ("id, project_id, title, status, progress, creator_id, "
            "assignee_id, created_at, updated_at, is_archived")
    vals = [tid, PROJECT_ID, title, status, 0, creator_id,
            assignee_id, old, old, 0]
    if reviewer_id is not None:
        cols += ", reviewer_id"
        vals.append(reviewer_id)
    if claimed_at != -1:
        cols += ", claimed_at"
        vals.append(claimed_at)
    ph = ",".join("?" for _ in vals)
    await conn.execute(
        f"INSERT INTO tasks ({cols}) VALUES ({ph})", vals)
    await conn.commit()
    return tid


async def _insert_wait(env, agent_id, expires_at):
    wait_contract_module._migrated.discard(PROJECT_ID)
    from hiveweave.services.wait_contract import wait_contract_service

    await wait_contract_service.list_all_active(PROJECT_ID)
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO agent_waits (id, agent_id, project_id, kind, ref, "
        "expires_at, created_at) VALUES (?, ?, ?, 'task', 'adr001', ?, ?)",
        [str(uuid.uuid4()), agent_id, PROJECT_ID, expires_at, _now_ms()])
    await conn.commit()


def _seed_state():
    state = {
        "silence_trackers": {},
        "stall_trackers": {},
        "current_game_seconds": 0,
    }
    game_time._states[PROJECT_ID] = state


def _health_events(mock_bus, kind=None):
    out = []
    for c in mock_bus.await_args_list:
        # publish_stream_event(agent_id, event) — 事件在第二参
        evt = (c.args[1] if len(c.args) > 1
               else c.kwargs.get("event", {}))
        if kind is None or evt.get("health") == kind:
            out.append(evt)
    return out


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


# ── §1 has_open_work：闭式判定源 ────────────────────────────


async def test_has_open_work_running_claimed_orion_case(env):
    """事故复现（Orion）：complete agent + running(已 claim) → 有活.

    变异：assignee 条件换回白名单/删 has_open_work → 本测试仍需通过
    running 白名单外的核心在 watchdog 接线用例；此处钉闭式语义本身。
    """
    old = _now_ms() - 40 * 60 * 1000
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    assert await TaskService().has_open_work(PROJECT_ID, EXECUTOR_ID) is True


async def test_has_open_work_terminal_statuses_not_open(env):
    """R1：未归档 completed/done/cancelled 不算开放义务.

    变异：从 TERMINAL_STATUSES 删任一终态成员 → 本测试失败。
    """
    assert {"closed", "cancelled", "completed", "done", "archived"} <= set(
        TERMINAL_STATUSES)
    old = _now_ms() - 40 * 60 * 1000
    for st in ("completed", "done", "cancelled", "closed"):
        await _insert_task(env, status=st, assignee_id=EXECUTOR_ID,
                           creator_id=COORD_ID, claimed_at=old)
    assert await TaskService().has_open_work(PROJECT_ID, EXECUTOR_ID) is False


async def test_has_open_work_blocked_and_future_status(env):
    """闭式覆盖：blocked（先 claim 后 block）与未来新增状态默认有活."""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_task(env, status="blocked", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    await _insert_task(env, status="escalated", assignee_id=OTHER_EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    svc = TaskService()
    assert await svc.has_open_work(PROJECT_ID, EXECUTOR_ID) is True
    assert await svc.has_open_work(PROJECT_ID, OTHER_EXECUTOR_ID) is True


async def test_has_open_work_created_unclaimed_not_open(env):
    """负空间：created（即便已指派、未 claim，claimed_at=NULL）不算."""
    await _insert_task(env, status="created", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=-1)
    assert await TaskService().has_open_work(PROJECT_ID, EXECUTOR_ID) is False


async def test_has_open_work_assignee_review_window_idle(env):
    """负空间：assignee 侧 submitted/reviewing/approved（球在
    reviewer/creator）→ 无活，不等审空醒."""
    old = _now_ms() - 40 * 60 * 1000
    for st in ("submitted", "reviewing", "approved"):
        await _insert_task(env, status=st, assignee_id=EXECUTOR_ID,
                           creator_id=COORD_ID, claimed_at=old)
    assert await TaskService().has_open_work(PROJECT_ID, EXECUTOR_ID) is False


async def test_has_open_work_non_verify_verifying_idle(env):
    """负空间：非 VERIFY 任务的 verifying（等 merged 结转）assignee 无活."""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_task(env, status="verifying", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old,
                       title="普通任务（非 VERIFY）")
    assert await TaskService().has_open_work(PROJECT_ID, EXECUTOR_ID) is False


async def test_has_open_work_creator_running_children_idle(env):
    """F1：派活中协调者（creator 持 running 子任务）→ idle 不误醒."""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    assert await TaskService().has_open_work(PROJECT_ID, COORD_ID) is False


async def test_has_open_work_reviewer_creator_windows(env):
    """reviewer/creator 义务窗口（状态敏感）仍构成开放工作."""
    old = _now_ms() - 40 * 60 * 1000
    # coordinator 是 creator：submitted 待审
    await _insert_task(env, status="submitted", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    assert await TaskService().has_open_work(PROJECT_ID, COORD_ID) is True
    # CEO 是指定 reviewer：reviewing 待收口
    await _insert_task(env, status="reviewing", assignee_id=OTHER_EXECUTOR_ID,
                       creator_id=COORD_ID, reviewer_id=CEO_ID, claimed_at=old)
    assert await TaskService().has_open_work(PROJECT_ID, CEO_ID) is True


async def test_has_open_work_wait_parking_freezes_and_expiry_reopens(env):
    """F2：未过期 wait 冻结义务（合法停泊）；过期后义务恢复."""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    svc = TaskService()
    # 未过期 wait → False
    await _insert_wait(env, EXECUTOR_ID, _now_ms() + 10 * 60 * 1000)
    assert await svc.has_open_work(PROJECT_ID, EXECUTOR_ID) is False
    # 过期 wait → True（义务恢复）
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute("UPDATE agent_waits SET expires_at = ?",
                       [_now_ms() - 1000])
    await conn.commit()
    assert await svc.has_open_work(PROJECT_ID, EXECUTOR_ID) is True


# ── §2 silent watchdog complete 豁免接线 ────────────────────


def _complete_agent_mock(aid, role="executor"):
    return SimpleNamespace(
        disposition="complete",
        project_id=PROJECT_ID,
        config={"role": role},
    )


async def _run_silent_check(monkeypatch, agents_mock):
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", agents_mock)
    mock_coord = AsyncMock()
    mock_sub = AsyncMock()
    mock_bus = AsyncMock()
    with _StartedMock(1), \
         patch("hiveweave.agents.trigger.trigger_coordinator", mock_coord), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_sub), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message",
               AsyncMock()):
        await GameTimeService()._check_silent_agents(PROJECT_ID)
    return mock_coord, mock_sub, mock_bus


async def test_watchdog_complete_running_claimed_wakes_with_force(
        env, monkeypatch):
    """事故主回归：complete + running(claimed) → 不豁免；穿透唤醒
    且 force=True（R2a）。变异：complete 分支换回无条件 continue、
    或 _watchdog_trigger 换回 get_actionable_obligations（R2b）→ 失败。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "猎户", role="executor",
                        created_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    _seed_state()

    mock_coord, mock_sub, mock_bus = await _run_silent_check(
        monkeypatch, lambda aid: _complete_agent_mock(aid, "executor"))

    errors = _health_events(mock_bus, "error")
    assert len(errors) == 1
    assert errors[0]["agentId"] == EXECUTOR_ID
    calls = (mock_coord.await_args_list + mock_sub.await_args_list)
    assert calls, "complete+running 必须被唤醒（修复前全链路静默）"
    assert all(c.kwargs.get("force") is True for c in calls), (
        "穿透唤醒必须 force=True（R2a：否则被 _watchdog_trigger 出口吞掉）"
    )


async def test_watchdog_complete_creator_running_children_stays_idle(
        env, monkeypatch):
    """F1 守护：complete 协调者，名下只有别人在推的 running 子任务
    （自己是 creator）→ 保持豁免安静。与上一条必须同时绿。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, COORD_ID, "烛岚", parent_id=CEO_ID,
                        role="tech lead", permission_type="coordinator",
                        created_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    _seed_state()

    mock_coord, mock_sub, mock_bus = await _run_silent_check(
        monkeypatch, lambda aid: _complete_agent_mock(aid, "tech lead"))

    assert _health_events(mock_bus) == []
    assert mock_coord.await_count == 0
    assert mock_sub.await_count == 0


async def test_watchdog_complete_root_arbitrates_unresolved_work(
        env, monkeypatch):
    """P0-1：complete 项目根（CEO，非任务义务人）+ 项目有 submitted
    （别人名下）→ 项目级"无人推进"判定唤醒 CEO 仲裁（force=True）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "归零", role="ceo", created_at=old)
    # submitted 任务：creator=coord，assignee=executor，reviewer 默认 creator
    # → CEO 无个人义务，但项目有"等审"的无人推进工作
    await _insert_task(env, status="submitted", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    _seed_state()

    mock_coord, mock_sub, mock_bus = await _run_silent_check(
        monkeypatch, lambda aid: _complete_agent_mock(aid, "ceo"))

    errors = _health_events(mock_bus, "error")
    assert len(errors) == 1
    assert errors[0]["agentId"] == CEO_ID
    calls = mock_coord.await_args_list + mock_sub.await_args_list
    assert calls and all(c.kwargs.get("force") is True for c in calls)


async def test_watchdog_complete_root_only_running_children_quiet(
        env, monkeypatch):
    """F1 同口径：complete 项目根 + 仅有 assignee 正常推进的 running
    子任务 → 安静（项目级判定"无人推进"形状不含 running）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "归零", role="ceo", created_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    _seed_state()

    mock_coord, mock_sub, mock_bus = await _run_silent_check(
        monkeypatch, lambda aid: _complete_agent_mock(aid, "ceo"))

    assert _health_events(mock_bus) == []
    assert mock_coord.await_count == 0
    assert mock_sub.await_count == 0


async def test_watchdog_trigger_nonforce_blocked_not_skipped(
        env, monkeypatch):
    """R2b：非 force 调用 _watchdog_trigger（756/822/859 催办路径）时，
    complete + 名下 blocked（先 claim 后 block）不得被 skip 判空吞掉。
    变异：出口换回 get_actionable_obligations 白名单 → blocked 不在
    清单 → skip → 本测试失败（R2a 的 force 掩蔽不了本用例）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_task(env, status="blocked", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda aid: _complete_agent_mock(aid, "executor"))
    mock_coord = AsyncMock()
    mock_sub = AsyncMock()
    with patch("hiveweave.agents.trigger.trigger_coordinator", mock_coord), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_sub):
        await GameTimeService()._watchdog_trigger(EXECUTOR_ID)
    assert (mock_coord.await_count + mock_sub.await_count) == 1


# ── R2a 端到端：force 穿透 trigger 上下文 complete-skip ──────


async def test_trigger_context_force_pierces_complete_skip(
        env, monkeypatch):
    """Orion 终局回归：complete + 已读 inbox + 无 ask/handoff + 仅
    assignee 义务 → build_trigger_context(force=False) 返回 None（skip），
    force=True 必须返回上下文（唤醒不蒸发）。变异：删 trigger.py
    complete-skip 的 `and not force` → force 分支失败。"""
    from hiveweave.agents import trigger as trigger_mod

    old = _now_ms() - 40 * 60 * 1000
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda aid: _complete_agent_mock(aid, "executor"))
    agent_record = {
        "id": EXECUTOR_ID, "project_id": PROJECT_ID, "name": "猎户",
        "role": "executor",
    }
    ctx = await trigger_mod.build_trigger_context(
        agent_record, "subordinate", force=True)
    assert ctx is not None, "force 唤醒不得在 trigger 上下文构建处蒸发"
    ctx_no_force = await trigger_mod.build_trigger_context(
        agent_record, "subordinate", force=False)
    assert ctx_no_force is None, "非 force 维持克制（分工例外口）"


# ── 第二入口：health_supervisor 兜底（game_time 停摆时）────


async def test_health_supervisor_wake_uses_force(env):
    """ADR 回归清单第 2 条：health_supervisor._wake_agent 必须以
    force=True 调 trigger——complete+busy 的兜底唤醒不得在 trigger
    complete-skip 出口蒸发。变异：_wake_agent 去掉 force=True → 失败。"""
    from hiveweave.services.health_supervisor import HealthSupervisor

    with patch("hiveweave.services.org.OrgService.get_agent",
               AsyncMock(return_value={"role": "executor"})), \
         patch("hiveweave.agents.trigger.trigger_subordinate",
               AsyncMock()) as mock_sub, \
         patch.object(status_event_bus, "publish_stream_event", AsyncMock()):
        svc = HealthSupervisor()
        # now_mono 取大值绕过 WAKE_COOLDOWN_S 冷却（_wake_ts 默认 0）
        await svc._wake_agent(EXECUTOR_ID, PROJECT_ID, _now_ms(), 1e9)
    assert mock_sub.await_count == 1
    assert mock_sub.await_args.kwargs.get("force") is True


# ── R4 完成闸消费闭式清单 ───────────────────────────────────


def _turn_result(phase="done_slice"):
    return TurnResult(phase=phase, summary="adr001 test")


def _gate_with_pending(agent_id=EXECUTOR_ID, obligations=None):
    """注入 pending TurnResult 后跑完成闸（evaluate 单参，内部读注册表）."""
    from hiveweave.services.turn_session import (
        pop_pending_turn_result,
        set_pending_turn_result,
    )

    ctx = ExitContext(
        agent_id=agent_id,
        project_id=PROJECT_ID,
        tool_calls=[],
        open_task_obligations=obligations or [],
    )
    tr = TurnResult(phase="done_slice", summary="adr001 gate test")
    set_pending_turn_result(agent_id, tr.to_persist_dict())
    try:
        return evaluate_turn_exit(ctx)
    finally:
        pop_pending_turn_result(agent_id)


async def test_open_work_obligations_include_blocked_and_future(env):
    """get_open_work_obligations：blocked / escalated 以 assignee 角色
    入清单（R4 数据源）。变异：assignee 谓词换回白名单 → 失败。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_task(env, status="blocked", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    await _insert_task(env, status="escalated", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    obs = await TaskService().get_open_work_obligations(
        PROJECT_ID, EXECUTOR_ID)
    statuses = {o["status"] for o in obs}
    assert {"blocked", "escalated"} <= statuses
    assert all(o.get("role_hint") == "assignee" for o in obs)


async def test_gate_done_slice_blocked_not_admitted(env):
    """R4：assignee 名下仅 blocked 任务 → done_slice 不放行
    （OPEN_TASKS_UNDECLARED）。变异：完成闸换回 get_actionable_obligations
    （白名单）→ obligations 为空 → gate 放行 → 本测试失败。"""
    blocked_task = {
        "id": "t-blocked", "status": "blocked", "role_hint": "assignee",
        "assignee_id": EXECUTOR_ID, "title": "x",
    }
    decision = _gate_with_pending(
        agent_id=EXECUTOR_ID, obligations=[blocked_task])
    assert not decision.ok
    assert "OPEN_TASKS_UNDECLARED" in decision.violations


async def test_gate_done_slice_clean_admitted(env):
    """对照：无开放义务 → done_slice 放行（闸不误伤正常收工）。"""
    decision = _gate_with_pending(agent_id=EXECUTOR_ID, obligations=[])
    assert decision.ok


def test_terminal_statuses_single_source():
    """R1 结构断言：唯一常量、五成员齐全（platform_state/timeline 引用它）。"""
    from hiveweave.services.platform_state import _SCOPE_CLOSED
    from hiveweave.services.tasks.timeline import _TERMINAL_STATUSES

    assert _SCOPE_CLOSED is TERMINAL_STATUSES or _SCOPE_CLOSED == TERMINAL_STATUSES
    assert _TERMINAL_STATUSES == TERMINAL_STATUSES


def test_completion_gate_consumes_closed_form_source():
    """R4 接线钉子：completion.py 完成闸的义务清单必须消费闭式单一判定源
    get_open_work_obligations（变异：换回 get_actionable_obligations →
    本测试失败）。handle_completion 全链路过重，此处按既有先例
    （test_audit_test6_ylgy_evening 预算口 source pin）钉消费口径。"""
    import inspect

    from hiveweave.agents import completion as completion_mod

    src = inspect.getsource(completion_mod)
    assert "get_open_work_obligations" in src
    gate_call = [ln for ln in src.splitlines()
                 if "obligations = await ts." in ln]
    assert gate_call, "完成闸必须显式查询义务清单"
    assert all("get_open_work_obligations" in ln for ln in gate_call), (
        "完成闸义务查询只准用闭式单一判定源（R4）"
    )


def test_completion_gate_feeds_narrow_resolved_set():
    """逃逸口接线钉子（DSH_22 场景A）：ExitContext.tasks_advanced 必须
    喂窄集 _task_ids_gate_resolved_this_turn（变异：换回宽集
    tasks_advanced → 本测试失败，逃逸口复活）。"""
    import inspect

    from hiveweave.agents import completion as completion_mod

    src = inspect.getsource(completion_mod)
    assert "_task_ids_gate_resolved_this_turn" in src
    assert "tasks_advanced=gate_resolved" in src
    # 宽集不得再直连 ExitContext
    assert "tasks_advanced=tasks_advanced" not in src


def test_gate_resolved_narrow_set_semantics():
    """窄集语义：只有**成功的**义务解除动作算数。变异方向：
    - 窄集换回宽集逻辑（claim/running 计入）→ 前两段断言失败；
    - 删 ok=False 过滤 → 失败调用段失败（逃逸换壳）。"""
    from hiveweave.agents.agent import Agent

    a = object.__new__(Agent)

    def tc(name, tid, status=None, ok=None):
        args = {"taskId": tid}
        if status:
            args["status"] = status
        entry = {"function": {"name": name, "arguments": args}}
        if ok is not None:
            entry["ok"] = ok
        return entry

    calls = [
        tc("claim_task", "t1"),
        tc("update_task_status", "t1", "running"),
        tc("update_task_status", "t2", "blocked"),
        tc("update_progress", "t3"),
        tc("dispatch_task", "t4"),
        tc("submit_task", "t5"),
        tc("review_task", "t6"),
        tc("close_task", "t7"),
        # 幻影路径：update_task_status 运行时到不了 review 窗口状态
        tc("update_task_status", "t8", "submitted"),
        # 失败调用（tool_loop 落账 ok=False）：不解除义务
        tc("submit_task", "t9", ok=False),
        tc("review_task", "t10", ok=False),
    ]
    # 宽集（活动量）：claim/running/blocked/progress/dispatch 全算
    broad = a._task_ids_advanced_this_turn(calls)
    assert {"t1", "t2", "t3", "t4", "t5", "t6", "t7"} <= broad
    # 窄集（义务解除）：只认成功的 submit/review/close；
    # t8 幻影状态 / t9 t10 失败调用一律排除
    narrow = a._task_ids_gate_resolved_this_turn(calls)
    assert narrow == {"t5", "t6", "t7"}


async def test_ship_nudge_terminal_statuses_converged(env):
    """R1 收尾：ship_nudge 终态集引用 TERMINAL_STATUSES——未归档
    completed/done/cancelled 不再被当"剩余开放任务"。变异：换回
    窄口径 {closed, cancelled} → 前半断言失败。"""
    from hiveweave.services.tasks.ship_nudge import (
        _has_remaining_open_tasks,
    )

    old = _now_ms() - 40 * 60 * 1000
    for st in ("completed", "done", "cancelled", "closed"):
        await _insert_task(env, status=st, assignee_id=EXECUTOR_ID,
                           creator_id=COORD_ID, claimed_at=old)
    assert await _has_remaining_open_tasks(PROJECT_ID) is False
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=COORD_ID, claimed_at=old)
    assert await _has_remaining_open_tasks(PROJECT_ID) is True
