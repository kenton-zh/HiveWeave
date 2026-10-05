"""批24 · 平台侧五件小修的回归钉。

- 修复1 交付完成清全项目等待：``mark_delivery_complete`` 成功 ⇒ 该项目全部
  活跃 agent_waits 清空 + 每个受影响 agent 收到 ``[PROJECT DELIVERED]``
  （wake=1，幂等键 per-agent）；blocked 路径 waits 不动。
- 修复2 WAIT_TIMEOUT 出口文案：项目已交付且被唤醒 agent 名下无义务 ⇒
  正文替换为出口文案（不重登等待、直接停）；非交付态原文逐字保留。
- 修复3 空唤醒断路器：同一 agent 连续 3 次（HIVEWEAVE_IDLE_WAKE_BREAKER）
  空唤醒 ⇒ 抑制，非关键唤醒源被 chat() 入口跳过；新任务派入等真实外部
  事件强制解除；有义务 agent 永不进抑制。
- 修复4 dependsOn 建单即拒：依赖 ref 非本项目真实 task id ⇒ 整单拒绝
  （fail-closed），回执点名坏 ref + 处方；真实 id 放行；混合只点名假的。
- 修复5 user_pings read_at 回填：answer_question 作答 ⇒ 关联 ping 落
  read_at（幂等）；mark-read 走 COALESCE 首读为准。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.agents.agent import (
    _IDLE_WAKE_STATE,
    Agent,
    _idle_wake_is_external_event,
    idle_wake_force_clear,
    idle_wake_record_turn,
    idle_wake_suppressed,
)
from hiveweave.agents.types import AgentState
from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.services.game_time import GameTimeService
from hiveweave.services.org import OrgService
from hiveweave.services.task import TaskService
from hiveweave.services.wait_contract import wait_contract_service
from hiveweave.tools import misc_tools as mt
from hiveweave.tools.misc_tools import mark_delivery_complete_tool
from hiveweave.tools.task_tools import DispatchTaskParams, dispatch_task_tool

# ── 共用夹具：tmp meta DB + per-project DB + agent_router ────────


async def _make_project(tmp_path, project_id: str, agent_specs: list[dict]):
    """meta projects 行 + per-project DB（agents 行）+ 内存路由。"""
    ws = str(tmp_path / f"ws-{project_id}")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, is_started, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        [project_id, f"p-{project_id}", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    from hiveweave.services.agent_router import AgentRoute, agent_router

    for spec in agent_specs:
        cur = await conn.execute(
            "INSERT INTO agents (id, short_id, project_id, name, role, status, "
            "permission_type, parent_id, model_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, 'gpt-x', ?)",
            [
                spec["id"],
                spec["short_id"],
                project_id,
                spec.get("name", spec["id"][:12]),
                spec.get("role", "executor"),
                spec.get("permission_type", "executor"),
                spec.get("parent_id"),
                now,
            ],
        )
        await cur.close()
        agent_router.register(
            AgentRoute(
                agent_id=spec["id"],
                project_id=project_id,
                workspace_path=ws,
                short_id=spec["short_id"],
                name=spec.get("name", spec["id"][:12]),
                role=spec.get("role", "executor"),
                status="active",
            )
        )
    await conn.commit()
    return conn


async def _seed_wait(project_id: str, agent_id: str, *, kind: str = "agent",
                     ref: str = "peer-agent", expires_in_ms: int | None = 60_000,
                     wait_id: str | None = None) -> str:
    wid = wait_id or str(uuid.uuid4())
    now = int(time.time() * 1000)
    conn = await project_db.get_project_db_by_project_id(project_id)
    cur = await conn.execute(
        "INSERT INTO agent_waits (id, agent_id, project_id, kind, ref, "
        "wake_on, expires_at, created_at) VALUES (?, ?, ?, ?, ?, '[]', ?, ?)",
        [
            wid,
            agent_id,
            project_id,
            kind,
            ref,
            now + expires_in_ms if expires_in_ms is not None else None,
            now,
        ],
    )
    await cur.close()
    await conn.commit()
    return wid


async def _wait_rows(project_id: str) -> list[dict]:
    conn = await project_db.get_project_db_by_project_id(project_id)
    cur = await conn.execute("SELECT * FROM agent_waits ORDER BY created_at")
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    return rows


async def _inbox_rows(conn, agent_id: str, needle: str | None = None) -> list[dict]:
    cur = await conn.execute(
        "SELECT * FROM inbox WHERE to_agent_id = ? ORDER BY created_at",
        [agent_id],
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    if needle is not None:
        return [r for r in rows if needle in str(r.get("message") or "")]
    return rows


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    """meta DB 钉到本用例临时路径（照 test_batch22 模式）。"""
    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()
    yield
    await meta_db.close_meta_db()


@pytest.fixture(autouse=True)
def _cleanup_router_and_breaker():
    from hiveweave.services.agent_router import agent_router

    yield
    agent_router.reset_for_tests()
    _IDLE_WAKE_STATE.clear()


# ══ 修复1：交付完成清全项目等待 ═══════════════════════════════


@pytest.fixture
async def delivery_project(tmp_path):
    """干净账本（可交付）+ 两个 agent 各挂一条活跃 wait。"""
    ceo = "b24-ceo-delivery"
    leaf = "b24-leaf-delivery"
    conn = await _make_project(
        tmp_path,
        "b24-proj-delivery",
        [
            {"id": ceo, "short_id": "B24C", "role": "ceo",
             "permission_type": "ceo"},
            {"id": leaf, "short_id": "B24L", "role": "executor",
             "permission_type": "executor", "parent_id": ceo},
        ],
    )
    await _seed_wait("b24-proj-delivery", ceo, ref=leaf)
    await _seed_wait("b24-proj-delivery", leaf, ref="some-task-id",
                     kind="task")
    return SimpleNamespace(conn=conn, ceo=ceo, leaf=leaf,
                           project_id="b24-proj-delivery")


async def _clear_ledger(env) -> None:
    """清空交付判据面（本夹具本就无任务/未读，占位以便将来扩展）。"""
    return None


async def test_delivery_complete_clears_all_waits_and_notifies(delivery_project):
    """交付成功 ⇒ waits 全清 + 每个 agent 收到 [PROJECT DELIVERED]（wake=1）。"""
    env = delivery_project
    result = await mark_delivery_complete_tool(
        mt.MarkDeliveryCompleteParams(), env.ceo, "/tmp"
    )
    assert result.success is True, result.output

    rows = await _wait_rows(env.project_id)
    assert rows, "wait 行应存在（软清，不是删行）"
    assert all(r["cleared_at"] is not None for r in rows), (
        "交付成功后该项目全部活跃 agent_waits 必须被清空"
    )
    active = await wait_contract_service.list_all_active(env.project_id)
    assert active == []

    for aid in (env.ceo, env.leaf):
        msgs = await _inbox_rows(env.conn, aid, "[PROJECT DELIVERED]")
        assert len(msgs) == 1, f"{aid} 必须收到恰好一条交付清等通知"
        assert msgs[0]["wake"], "通知必须 wake=1（立即唤醒出口）"

    # 幂等：重复标记（先手动清判据面不变）不再重投通知
    result2 = await mark_delivery_complete_tool(
        mt.MarkDeliveryCompleteParams(), env.ceo, "/tmp"
    )
    assert result2.success is True
    for aid in (env.ceo, env.leaf):
        msgs = await _inbox_rows(env.conn, aid, "[PROJECT DELIVERED]")
        assert len(msgs) == 1, "幂等键 per-agent：重复标记不得重投"


async def test_delivery_blocked_keeps_waits_untouched(tmp_path):
    """交付被拒（blocked 路径）⇒ waits 不动、无通知。"""
    ceo = "b24-ceo-blocked"
    conn = await _make_project(
        tmp_path,
        "b24-proj-blocked",
        [{"id": ceo, "short_id": "B24B", "role": "ceo",
          "permission_type": "ceo"}],
    )
    pid = "b24-proj-blocked"
    # 留一个 approved 未 closed 任务 ⇒ LEDGER_APPROVED_OPEN blocker
    now = int(time.time() * 1000)
    cur = await conn.execute(
        "INSERT INTO tasks (id, project_id, title, description, creator_id, "
        "status, created_at, updated_at, is_archived) "
        "VALUES (?, ?, 't', '', ?, 'approved', ?, ?, 0)",
        [str(uuid.uuid4()), pid, ceo, now, now],
    )
    await cur.close()
    await conn.commit()
    await _seed_wait(pid, ceo, ref="someone")

    result = await mark_delivery_complete_tool(
        mt.MarkDeliveryCompleteParams(), ceo, "/tmp"
    )
    assert result.success is False
    blocked_text = result.error or result.output
    assert "blocked" in blocked_text or "未达完成" in blocked_text

    rows = await _wait_rows(pid)
    assert len(rows) == 1 and rows[0]["cleared_at"] is None, (
        "blocked 路径不得清 waits"
    )
    assert await _inbox_rows(conn, ceo, "[PROJECT DELIVERED]") == []


# ══ 修复2：WAIT_TIMEOUT 交付态出口文案 ════════════════════════

_EXIT_TEXT = "项目已交付且你名下无义务"


async def test_wait_timeout_delivery_state_exit_copy(tmp_path):
    """交付态 + 无义务 agent 的 wait 到期 ⇒ 出口文案，不再引导 resume。"""
    ceo = "b24-ceo-wt-delivered"
    conn = await _make_project(
        tmp_path,
        "b24-proj-wt-a",
        [{"id": ceo, "short_id": "B24A", "role": "ceo",
          "permission_type": "ceo"}],
    )
    pid = "b24-proj-wt-a"
    await mt._write_delivery_state(pid, "complete", {"tasks_total": 3,
                                                     "tasks_closed": 3})
    await _seed_wait(pid, ceo, ref="b24-leaf-wt", expires_in_ms=-1_000)

    await GameTimeService()._process_wait_contracts(pid)

    msgs = await _inbox_rows(conn, ceo, "[WAIT_TIMEOUT]")
    assert len(msgs) == 1
    body = str(msgs[0]["message"])
    assert _EXIT_TEXT in body
    assert "不要重新登记" in body and "直接停止输出" in body
    assert "Resume work or re-establish a wait" not in body, (
        "交付态出口文案不得残留原文的续等引导"
    )


async def test_wait_timeout_non_delivery_keeps_original_copy(tmp_path):
    """非交付态 ⇒ 原文案逐字保留（Resume work or re-establish a wait）。"""
    ceo = "b24-ceo-wt-fresh"
    conn = await _make_project(
        tmp_path,
        "b24-proj-wt-b",
        [{"id": ceo, "short_id": "B24F", "role": "ceo",
          "permission_type": "ceo"}],
    )
    pid = "b24-proj-wt-b"
    await _seed_wait(pid, ceo, ref="b24-leaf-wt", expires_in_ms=-1_000)

    await GameTimeService()._process_wait_contracts(pid)

    msgs = await _inbox_rows(conn, ceo, "[WAIT_TIMEOUT]")
    assert len(msgs) == 1
    body = str(msgs[0]["message"])
    assert "Resume work or re-establish a wait" in body
    assert _EXIT_TEXT not in body


# ══ 修复3：空唤醒断路器 ═══════════════════════════════════════

_B24_AGENT = "b24-breaker-agent"


class _FakeAgentForChatGate:
    """仅携带 chat() 到断路器闸门所需属性的最小假体（stub 构造安全）。"""

    def __init__(self, agent_id: str, project_id: str):
        self.id = agent_id
        self.project_id = project_id
        self.status = AgentState.IDLE
        self.disposition = "runnable"
        self._lock = asyncio.Lock()
        self._message_queue: list = []
        self._ensure_watcher_alive = lambda: None

    def try_clear_resume_suppressed(self, opts: dict | None = None) -> bool:
        return False


@pytest.fixture
async def breaker_project(tmp_path, monkeypatch):
    """is_started=1 的项目（chat() 闸门前的 Bug K 检查要过）。"""
    ws = str(tmp_path / "ws-breaker")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, is_started, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        ["b24-proj-breaker", "p-breaker", ws, now],
    )
    monkeypatch.setenv("HIVEWEAVE_IDLE_WAKE_BREAKER", "3")
    return "b24-proj-breaker"


async def test_three_empty_wakes_then_fourth_suppressed(
    breaker_project, monkeypatch
):
    """① 3 次空唤醒后第 4 次被抑制（chat() 入口早退，不启动 LLM）。"""
    monkeypatch.setattr(
        "hiveweave.agents.helpers.rate_limit.balance_exhausted_remaining",
        lambda: 0,
    )
    from hiveweave.services.system_state import system_state

    monkeypatch.setattr(system_state, "paused", lambda: False)

    for _ in range(3):
        idle_wake_record_turn(_B24_AGENT, empty=True)
    assert idle_wake_suppressed(_B24_AGENT)

    fake = _FakeAgentForChatGate(_B24_AGENT, breaker_project)
    result = await Agent.chat(
        fake, "[WAIT_TIMEOUT] your wait expired",
        opts={"trigger": True, "source": "trigger"},
    )
    assert result == {"ok": True, "suppressed": "idle_wake_breaker"}, (
        "第 4 次非关键唤醒必须被断路器抑制"
    )


async def test_new_task_dispatch_forces_breaker_clear(breaker_project):
    """② 新任务派入（task 类 trigger）⇒ 强制解除抑制。"""
    for _ in range(3):
        idle_wake_record_turn(_B24_AGENT, empty=True)
    assert idle_wake_suppressed(_B24_AGENT)

    external_opts = {
        "trigger": True, "source": "task", "task_id": "t-123",
        "message_type": "task",
    }
    assert _idle_wake_is_external_event(external_opts)
    # chat() 闸门对外部事件走的正是 idle_wake_force_clear
    idle_wake_force_clear(_B24_AGENT, reason="task")
    assert not idle_wake_suppressed(_B24_AGENT)

    # 人类直聊（非 trigger）同样是外部事件
    assert _idle_wake_is_external_event({"source": "chat"})
    # @点名（peer ask）是外部事件
    assert _idle_wake_is_external_event({
        "trigger": True, "source": "trigger", "wake_category": "ask",
    })


async def test_agent_with_obligations_never_enters_suppression():
    """③ 有义务 / 有产出的 agent 永不进抑制（连击即清零）。"""
    for _ in range(2):
        idle_wake_record_turn(_B24_AGENT, empty=True)
    # 本轮有工具调用 / 有义务 ⇒ 非空回合
    idle_wake_record_turn(_B24_AGENT, empty=False)
    assert not idle_wake_suppressed(_B24_AGENT)
    # 再次连续空唤醒需要重新从 0 累计
    idle_wake_record_turn(_B24_AGENT, empty=True)
    assert not idle_wake_suppressed(_B24_AGENT)
    idle_wake_record_turn(_B24_AGENT, empty=True)
    idle_wake_record_turn(_B24_AGENT, empty=True)
    assert idle_wake_suppressed(_B24_AGENT)


async def test_breaker_threshold_env_override(breaker_project, monkeypatch):
    """阈值 env HIVEWEAVE_IDLE_WAKE_BREAKER 生效（含非法值回落默认 3）。"""
    from hiveweave.agents.agent import idle_wake_breaker_threshold

    monkeypatch.setenv("HIVEWEAVE_IDLE_WAKE_BREAKER", "2")
    assert idle_wake_breaker_threshold() == 2
    idle_wake_record_turn(_B24_AGENT, empty=True)
    idle_wake_record_turn(_B24_AGENT, empty=True)
    assert idle_wake_suppressed(_B24_AGENT)

    _IDLE_WAKE_STATE.pop(_B24_AGENT, None)
    monkeypatch.setenv("HIVEWEAVE_IDLE_WAKE_BREAKER", "not-a-number")
    assert idle_wake_breaker_threshold() == 3


async def test_sole_advancer_skips_suppressed_agent(breaker_project, monkeypatch):
    """idle_wakeup.maybe_wake_sole_advancer 跳过抑制中 agent。"""
    from hiveweave.services import idle_wakeup as iw

    idle_wake_record_turn(_B24_AGENT, empty=True)
    idle_wake_record_turn(_B24_AGENT, empty=True)
    idle_wake_record_turn(_B24_AGENT, empty=True)

    async def _fake_sole(project_id, *, exclude_agent_ids=frozenset()):
        return {"agent_id": _B24_AGENT, "role": "executor",
                "obligations": [], "ask_senders": []}

    fired: list[str] = []

    async def _fake_trigger(aid, *, force=False):
        fired.append(aid)

    monkeypatch.setattr(iw, "compute_sole_advancer", _fake_sole)
    monkeypatch.setattr(iw, "_wake_budget_allows",
                        AsyncMock(return_value=True))
    monkeypatch.setattr(
        "hiveweave.agents.trigger.trigger_subordinate", _fake_trigger
    )
    monkeypatch.setattr(
        "hiveweave.agents.trigger.trigger_coordinator", _fake_trigger
    )
    got = await iw.maybe_wake_sole_advancer(breaker_project)
    assert got is None and fired == [], "抑制中 agent 不得被 sole-advancer 唤醒"

    # 解除后可唤醒
    idle_wake_force_clear(_B24_AGENT, reason="task")
    got = await iw.maybe_wake_sole_advancer(breaker_project)
    assert got == _B24_AGENT and fired == [_B24_AGENT]


# ══ 修复4：dependsOn 占位符建单即拒 ═══════════════════════════

B24_PID = "b24-proj-dep"
B24_COORD = "b24-dep-coordinator"
B24_EXEC = "b24-dep-executor"
_REAL_ID = "11111111-2222-3333-4444-555555555555"
_PLACEHOLDER = "task-dep-placeholder-2"


def _dep_patches(require_side_effect):
    """dispatch_task_tool 的门禁环境（照 test_dispatch_readonly_reminder）。"""

    async def _ga(aid: str):
        if aid == B24_EXEC:
            return {
                "id": B24_EXEC, "permission_type": "executor",
                "role": "工程师", "parent_id": B24_COORD, "name": "Exec",
            }
        if aid == B24_COORD:
            return {
                "id": B24_COORD, "permission_type": "coordinator",
                "role": "lead", "parent_id": None, "name": "Lead",
            }
        return None

    dispatch_ok = {
        "success": True, "task_id": "new-task-1", "handoff_id": "h1",
        "from_agent_id": B24_COORD, "to_agent_id": B24_EXEC,
        "description": "x", "worktree_path": None, "worktree_short_id": None,
    }
    return (
        patch("hiveweave.tools.helpers.get_project_id",
              AsyncMock(return_value=B24_PID)),
        patch("hiveweave.tools.helpers.resolve_agent_id",
              AsyncMock(return_value=B24_EXEC)),
        patch.object(OrgService, "get_agent", AsyncMock(side_effect=_ga)),
        patch.object(TaskService, "require_task_id",
                     AsyncMock(side_effect=require_side_effect)),
        patch.object(TaskService, "find_structured_open_dup",
                     AsyncMock(return_value=None)),
        patch.object(TaskService, "find_similar_open_task",
                     AsyncMock(return_value=None)),
        patch.object(
            __import__("hiveweave.services.dispatch", fromlist=[
                "DispatchService"]).DispatchService,
            "dispatch_task",
            AsyncMock(return_value=dict(dispatch_ok)),
        ),
    )


def _require_resolver(real_ids: set[str]):
    async def _side_effect(project_id: str, ref: str) -> str:
        if ref in real_ids:
            return ref
        raise ValueError(f"Task not found: {ref}. Copy the full id=… from get_tasks.")
    return _side_effect


async def test_depends_on_placeholder_rejects_whole_dispatch():
    """① 占位符 ⇒ 整单拒绝且回执点名 + 处方；底层 dispatch 不被调用。"""
    patches = _dep_patches(_require_resolver({_REAL_ID}))
    params = DispatchTaskParams(
        target="B24L", task="实现 X", submitGate="unit",
        dependsOn=[_PLACEHOLDER],
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patches[5], patches[6]:
        result = await dispatch_task_tool(params, B24_COORD, "/tmp")
    assert result.success is False
    receipt = result.error or result.output
    assert _PLACEHOLDER in receipt, "回执必须点名坏 ref"
    assert "处方" in receipt
    assert receipt.index("拒绝派单") < receipt.index(_PLACEHOLDER)


async def test_depends_on_real_task_id_passes():
    """② 真实 task id ⇒ 校验通过，派单照常。"""
    patches = _dep_patches(_require_resolver({_REAL_ID}))
    params = DispatchTaskParams(
        target="B24L", taskId=_REAL_ID, dependsOn=[_REAL_ID],
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patches[5], patches[6]:
        result = await dispatch_task_tool(params, B24_COORD, "/tmp")
    assert result.success is True, result.output


async def test_depends_on_mixed_rejects_naming_only_fake():
    """③ 混合（真 + 假）⇒ 拒绝，且只点名假的。"""
    patches = _dep_patches(_require_resolver({_REAL_ID}))
    params = DispatchTaskParams(
        target="B24L", task="实现 X", submitGate="unit",
        dependsOn=[_REAL_ID, _PLACEHOLDER],
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patches[5], patches[6]:
        result = await dispatch_task_tool(params, B24_COORD, "/tmp")
    assert result.success is False
    receipt = result.error or result.output
    assert _PLACEHOLDER in receipt
    assert _REAL_ID not in receipt, "真实 id 不得被点名"


# ══ 修复5：user_pings read_at 回填 ════════════════════════════

B5_PID = "b24-proj-pings"
B5_AGENT = "b24-pings-ceo"


@pytest.fixture
async def pings_project(tmp_path):
    conn = await _make_project(
        tmp_path,
        B5_PID,
        [{"id": B5_AGENT, "short_id": "B24P", "role": "ceo",
          "permission_type": "ceo"}],
    )
    return conn


async def _seed_question_and_ping(conn, qid: str, *, status: str = "pending",
                                  answer: str | None = None) -> str:
    now = int(time.time() * 1000)
    cur = await conn.execute(
        "INSERT INTO questions (id, agent_id, project_id, question, options, "
        "answer, status, created_at) VALUES (?, ?, ?, ?, '[]', ?, ?, ?)",
        [qid, B5_AGENT, B5_PID, "怎么走？", answer, status, now],
    )
    await cur.close()
    ping_id = str(uuid.uuid4())
    cur = await conn.execute(
        "INSERT INTO user_pings (id, project_id, from_agent_id, message, "
        "is_read, created_at, question_id, status) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, 'pending')",
        [ping_id, B5_PID, B5_AGENT, "怎么走？", now, qid],
    )
    await cur.close()
    await conn.commit()
    return ping_id


async def _ping_row(conn, qid: str) -> dict:
    cur = await conn.execute(
        "SELECT * FROM user_pings WHERE question_id = ?", [qid]
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    return dict(row)


async def test_answer_backfills_ping_read_at(pings_project):
    """作答 ⇒ read_at 落值；同答案重复提交幂等（read_at 不变）。"""
    from hiveweave.api.chat import QuestionAnswerBody, answer_question

    conn = pings_project
    qid = "b24-q-readat-1"
    await _seed_question_and_ping(conn, qid)
    assert (await _ping_row(conn, qid))["read_at"] is None

    resp = await answer_question(
        qid, QuestionAnswerBody(answer="走 A", agentId=B5_AGENT)
    )
    assert resp["ok"] is True
    ping = await _ping_row(conn, qid)
    assert ping["read_at"] is not None, "作答必须回填 read_at"
    first_ts = ping["read_at"]
    assert ping["status"] == "answered"

    # 幂等：同答案重复提交（idempotent 早退）不改变 read_at
    await asyncio.sleep(0.01)
    resp2 = await answer_question(
        qid, QuestionAnswerBody(answer="走 A", agentId=B5_AGENT)
    )
    assert resp2.get("idempotent") is True
    assert (await _ping_row(conn, qid))["read_at"] == first_ts


async def test_answer_backfill_does_not_overwrite_existing_read_at(
    pings_project,
):
    """read_at 已有值（用户先点了已读）⇒ 作答回填不覆盖首读时刻。"""
    from hiveweave.api.chat import QuestionAnswerBody, answer_question

    conn = pings_project
    qid = "b24-q-readat-2"
    ping_id = await _seed_question_and_ping(conn, qid)
    earlier = int(time.time() * 1000) - 60_000
    cur = await conn.execute(
        "UPDATE user_pings SET read_at = ?, is_read = 1 WHERE id = ?",
        [earlier, ping_id],
    )
    await cur.close()
    await conn.commit()

    await answer_question(
        qid, QuestionAnswerBody(answer="走 B", agentId=B5_AGENT)
    )
    assert (await _ping_row(conn, qid))["read_at"] == earlier


async def test_mark_read_backfills_read_at_idempotent(pings_project):
    """mark-read ⇒ read_at 落值；重复 mark-read 首读为准（COALESCE）。"""
    from hiveweave.api.communications import mark_ping_read

    conn = pings_project
    qid = "b24-q-readat-3"
    ping_id = await _seed_question_and_ping(conn, qid)

    resp = await mark_ping_read(ping_id, agentId=B5_AGENT)
    assert resp["ok"] is True
    first_ts = (await _ping_row(conn, qid))["read_at"]
    assert first_ts is not None

    await asyncio.sleep(0.01)
    await mark_ping_read(ping_id, agentId=B5_AGENT)
    assert (await _ping_row(conn, qid))["read_at"] == first_ts, (
        "重复 mark-read 不得覆盖首读时刻"
    )
