"""P0-2: CEO done_slice 项目级义务门。

CEO「无待办」不能只看自己名下任务：项目有 submitted 待审 / verifying
待收口 / 待命叶子未派活时，commit_turn(done_slice) 必须被拒（同步预检
+ backstop 同口径）；waiting 等其他 phase 与非 CEO 角色不受影响。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.turn_exit import (
    ExitContext,
    ceo_project_pending_obligations,
    evaluate_turn_exit,
    pop_ceo_project_pending_details,
    pre_check_exit_gates,
)
from hiveweave.services.turn_session import (
    HARD_COMMIT_GATE_CODES,
    clear_pending_turn_result,
    set_pending_turn_result,
)

AGENT_ID = "exit-agent"
PROJECT_ID = "test-ceo-project-pending"
CEO_ID = "ceo-uuid-0001"
LEAF_ID = "leaf-uuid-0001"


def _ctx(**kwargs):
    base = dict(
        agent_id=AGENT_ID,
        project_id=PROJECT_ID,
        tool_calls=[],
        pending_inbox_msgs=[],
        unreplied_asks=[],
        open_task_obligations=[],
        tasks_advanced=set(),
    )
    base.update(kwargs)
    return ExitContext(**base)


def _commit(agent_id: str, phase: str, **extra):
    payload = {"phase": phase, "summary": "slice done"}
    payload.update(extra)
    set_pending_turn_result(agent_id, payload)


# ── 纯 gate（evaluate_turn_exit，无 DB）─────────────────────


def test_done_slice_blocked_when_ceo_project_pending():
    _commit(AGENT_ID, "done_slice")
    try:
        decision = evaluate_turn_exit(
            _ctx(ceo_project_pending=["项目有 2 个 submitted 任务待审查"])
        )
    finally:
        clear_pending_turn_result(AGENT_ID)

    assert decision.ok is False
    assert "CEO_PROJECT_PENDING" in decision.violations
    # 修复语义：retrigger 让 CEO 去推进，而不是按账本停泊
    assert decision.should_repair is True
    assert decision.should_park is False
    # 提示必须带具体 pending 明细
    assert "项目有 2 个 submitted 任务待审查" in decision.hint


def test_done_slice_ok_when_no_project_pending():
    _commit(AGENT_ID, "done_slice")
    try:
        decision = evaluate_turn_exit(_ctx(ceo_project_pending=[]))
    finally:
        clear_pending_turn_result(AGENT_ID)

    assert decision.ok is True
    assert decision.violations == []


def test_waiting_phase_not_blocked_by_project_pending():
    """waiting 是合法等待出口 — 项目级门只拦 done_slice。"""
    _commit(
        AGENT_ID,
        "waiting",
        waiting_on=[{"kind": "task", "ref": "task-aaaaaaaa"}],
    )
    try:
        decision = evaluate_turn_exit(
            _ctx(ceo_project_pending=["项目有 1 个 submitted 任务待审查"])
        )
    finally:
        clear_pending_turn_result(AGENT_ID)

    assert "CEO_PROJECT_PENDING" not in decision.violations
    assert decision.ok is True


def test_in_progress_phase_not_blocked_by_project_pending():
    _commit(AGENT_ID, "in_progress")
    try:
        decision = evaluate_turn_exit(
            _ctx(ceo_project_pending=["项目有 1 个 verifying 任务待收口"])
        )
    finally:
        clear_pending_turn_result(AGENT_ID)

    assert "CEO_PROJECT_PENDING" not in decision.violations


def test_ceo_project_pending_is_hard_commit_gate():
    """同步预检必须首次命中即硬拒（不走 soft-pass）。"""
    assert "CEO_PROJECT_PENDING" in HARD_COMMIT_GATE_CODES


# ── DB 层（ceo_project_pending_obligations + pre_check）─────


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _ceo_row():
    return {
        "id": CEO_ID,
        "role": "ceo",
        "permission_type": "coordinator",
        "status": "active",
    }


async def _insert_agent(
    ws: str,
    agent_id: str,
    *,
    role: str = "签到排行榜工程师",
    permission_type: str = "executor",
    short_id: str = "A101",
    name: str = "甲由",
    status: str = "active",
    created_at: int | None = None,
):
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status,"
        " permission_type, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            agent_id,
            short_id,
            PROJECT_ID,
            name,
            role,
            status,
            permission_type,
            created_at if created_at is not None else now,
            now,
        ],
    )
    await conn.commit()


async def _insert_task(
    ws: str,
    task_id: str,
    *,
    status: str,
    creator_id: str = "someone-else",
    assignee_id: str | None = None,
    evidence: dict | None = None,
):
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, creator_id, assignee_id,"
        " status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [task_id, PROJECT_ID, "t", creator_id, assignee_id, status, now, now],
    )
    if evidence is not None:
        await conn.execute(
            "UPDATE tasks SET evidence = ? WHERE id = ?",
            [json.dumps(evidence), task_id],
        )
    await conn.commit()


@pytest.mark.asyncio
async def test_non_ceo_never_has_project_pending(env):
    """非 CEO 角色不受影响：即使项目有 submitted 任务也返回 []。"""
    await _insert_task(env["workspace_path"], "t-sub", status="submitted")
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(
            return_value={
                "id": LEAF_ID,
                "role": "签到排行榜工程师",
                "permission_type": "executor",
                "status": "active",
            }
        ),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, LEAF_ID)
    assert pending == []


@pytest.mark.asyncio
async def test_ceo_submitted_task_pending(env):
    await _insert_task(env["workspace_path"], "t-sub", status="submitted")
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert any("submitted" in p for p in pending)


@pytest.mark.asyncio
async def test_ceo_verifying_task_pending(env):
    await _insert_task(env["workspace_path"], "t-vfy", status="verifying")
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert any("verifying" in p for p in pending)


@pytest.mark.asyncio
async def test_ceo_idle_leaf_pending(env):
    """active 叶子超过宽限期且名下零任务 → 待派活。"""
    old = int(time.time() * 1000) - 20 * 60 * 1000
    await _insert_agent(
        env["workspace_path"], LEAF_ID, short_id="A102", name="乙希",
        created_at=old,
    )
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert any("待命叶子" in p and "乙希" in p for p in pending)


@pytest.mark.asyncio
async def test_ceo_leaf_with_task_not_flagged(env):
    """叶子名下有未归档任务 → 不算待命。"""
    old = int(time.time() * 1000) - 20 * 60 * 1000
    await _insert_agent(
        env["workspace_path"], LEAF_ID, short_id="A103", name="丙旦",
        created_at=old,
    )
    await _insert_task(
        env["workspace_path"], "t-run", status="running", assignee_id=LEAF_ID
    )
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert not any("待命叶子" in p for p in pending)


@pytest.mark.asyncio
async def test_ceo_young_leaf_within_grace_not_flagged(env):
    """招聘 10 分钟宽限期内的叶子不拦收工。"""
    young = int(time.time() * 1000) - 60 * 1000
    await _insert_agent(
        env["workspace_path"], LEAF_ID, short_id="A104", name="丁而",
        created_at=young,
    )
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert not any("待命叶子" in p for p in pending)


@pytest.mark.asyncio
async def test_ceo_clean_project_allows_done_slice(env):
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert pending == []


@pytest.mark.asyncio
async def test_ceo_pending_fail_open_on_db_error():
    """项目 DB 解析失败 → fail-open 返回 []，绝不阻塞收工。"""
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ), patch(
        "hiveweave.db.meta.get_project_workspace",
        new=AsyncMock(return_value=None),
    ):
        pending = await ceo_project_pending_obligations(
            "no-such-project", CEO_ID
        )
    assert pending == []


@pytest.mark.asyncio
async def test_pre_check_done_slice_flags_ceo_project_pending(env):
    """同步预检：CEO 名下无任务但项目有 submitted → done_slice 被拒。"""
    await _insert_task(env["workspace_path"], "t-sub", status="submitted")
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_outstanding_ask_senders",
        new=AsyncMock(return_value=set()),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_sent_recipients_since",
        new=AsyncMock(return_value=set()),
    ):
        violations = await pre_check_exit_gates(
            CEO_ID, PROJECT_ID, phase="done_slice"
        )
    assert "CEO_PROJECT_PENDING" in violations
    # 明细暂存供 commit_turn 拒绝消息使用
    details = pop_ceo_project_pending_details(CEO_ID)
    assert any("submitted" in d for d in details)


@pytest.mark.asyncio
async def test_pre_check_waiting_phase_skips_ceo_project_check(env):
    """waiting phase 不触发项目级检查（合法等待出口）。"""
    await _insert_task(env["workspace_path"], "t-sub", status="submitted")
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_outstanding_ask_senders",
        new=AsyncMock(return_value=set()),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_sent_recipients_since",
        new=AsyncMock(return_value=set()),
    ):
        violations = await pre_check_exit_gates(
            CEO_ID, PROJECT_ID, phase="waiting"
        )
    assert "CEO_PROJECT_PENDING" not in violations


# ── N1: 待命叶子 LIMIT 放宽（coordinator 挤占不再漏判）──────────


@pytest.mark.asyncio
async def test_ceo_idle_leaf_not_missed_behind_many_coordinators(env):
    """12 个 coordinator 排在前面时，真实待命叶子仍必须被识别。

    修复前查询 LIMIT 10：前 10 行全是 coordinator → Python 侧过滤后
    叶子被漏判；LIMIT 50 后所有行都能进过滤。
    """
    old = int(time.time() * 1000) - 20 * 60 * 1000
    for i in range(12):
        await _insert_agent(
            env["workspace_path"],
            f"coord-n1-{i:03d}",
            role="前端协调工程师",
            permission_type="coordinator",
            short_id=f"C{i:03d}",
            name=f"协调{i}",
            created_at=old,
        )
    await _insert_agent(
        env["workspace_path"], LEAF_ID, short_id="A105", name="戊当",
        created_at=old,
    )
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert any("待命叶子" in p and "戊当" in p for p in pending)


# ── N3: CEO 项目级明细残留清理（commit_turn 工具层）─────────────


def _seed_pending_details(agent_id: str, details: list[str]) -> None:
    """向 advisory dict 植入陈旧明细（模拟上次 done_slice 被拒后的残留）。"""
    from hiveweave.services import turn_exit as turn_exit_module

    turn_exit_module._ceo_project_pending_details[agent_id] = list(details)


@pytest.mark.asyncio
async def test_commit_turn_accept_clears_stale_ceo_pending_details():
    """上次 done_slice 被拒残留的明细，waiting 成功提交后必须清掉。"""
    from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

    agent = "ceo-n3-accept"
    _seed_pending_details(agent, ["项目有 1 个 submitted 任务待审查"])
    params = CommitTurnParams(
        phase="waiting",
        summary="waiting for user",
        waiting_on=[{"kind": "user", "ref": "user"}],
    )
    try:
        with patch(
            "hiveweave.db.meta.get_agent_project_id",
            new_callable=AsyncMock,
            return_value="proj-n3",
        ), patch(
            "hiveweave.services.turn_exit.pre_check_exit_gates",
            new_callable=AsyncMock,
            return_value=[],
        ):
            r = await commit_turn_tool(params, agent, ".")
    finally:
        clear_pending_turn_result(agent)

    assert r.success is True
    assert pop_ceo_project_pending_details(agent) == []


@pytest.mark.asyncio
async def test_commit_turn_soft_pass_clears_stale_ceo_pending_details():
    """soft-pass（接受但带提醒）路径同样清理明细残留。"""
    from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

    agent = "ceo-n3-soft"
    _seed_pending_details(agent, ["项目有 2 个 verifying 任务待收口"])
    params = CommitTurnParams(
        phase="waiting",
        summary="waiting on peer",
        waiting_on=[{"kind": "agent", "ref": "流火"}],
    )
    try:
        with patch(
            "hiveweave.db.meta.get_agent_project_id",
            new_callable=AsyncMock,
            return_value="proj-n3",
        ), patch(
            "hiveweave.services.turn_exit.pre_check_exit_gates",
            new_callable=AsyncMock,
            return_value=["WAIT_WITHOUT_ASK"],
        ):
            r = await commit_turn_tool(params, agent, ".")
    finally:
        clear_pending_turn_result(agent)

    assert r.success is True
    assert "SOFT WARNING" in (r.output or "")
    assert pop_ceo_project_pending_details(agent) == []


@pytest.mark.asyncio
async def test_commit_turn_hard_reject_composes_details_then_clears():
    """hard 拒绝保持「先取明细拼消息、再清」的顺序。"""
    from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

    agent = "ceo-n3-hard"
    _seed_pending_details(agent, ["项目有 1 个 submitted 任务待审查"])
    params = CommitTurnParams(phase="done_slice", summary="done")
    try:
        with patch(
            "hiveweave.db.meta.get_agent_project_id",
            new_callable=AsyncMock,
            return_value="proj-n3",
        ), patch(
            "hiveweave.services.turn_exit.pre_check_exit_gates",
            new_callable=AsyncMock,
            return_value=["CEO_PROJECT_PENDING"],
        ):
            r = await commit_turn_tool(params, agent, ".")
    finally:
        clear_pending_turn_result(agent)

    assert r.success is False
    assert "REJECTED" in (r.error or "")
    assert "项目有 1 个 submitted 任务待审查" in (r.error or "")
    assert pop_ceo_project_pending_details(agent) == []


# ── E4: 未解决的 FAIL 判定（复盘致命链一：FAIL 不许被收工吞掉）──────


@pytest.mark.asyncio
async def test_ceo_open_fail_verdict_pending(env):
    """项目有 open 的 verdict=FAIL 终验 → pending 明细含 FAIL。"""
    await _insert_task(
        env["workspace_path"],
        "t-fail",
        status="running",
        evidence={"verdict": "FAIL", "blocking_issues": ["/_admin 404"]},
    )
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert any("FAIL" in p and "verdict" in p for p in pending)


@pytest.mark.asyncio
async def test_ceo_closed_fail_verdict_not_pending(env):
    """FAIL 终验已 closed/cancelled → 不拦 CEO 收工。"""
    await _insert_task(
        env["workspace_path"],
        "t-fail-closed",
        status="closed",
        evidence={"verdict": "FAIL", "blocking_issues": ["x"]},
    )
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert not any("FAIL" in p and "verdict" in p for p in pending)


@pytest.mark.asyncio
async def test_ceo_pass_or_unverdict_verdict_not_pending(env):
    """verdict=PASS 或无 verdict → 不触发 FAIL 检查。"""
    await _insert_task(
        env["workspace_path"],
        "t-pass",
        status="running",
        evidence={"verdict": "PASS", "tests_passed": True},
    )
    await _insert_task(
        env["workspace_path"], "t-legacy", status="running", evidence={}
    )
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert not any("FAIL" in p and "verdict" in p for p in pending)


@pytest.mark.asyncio
async def test_pre_check_done_slice_flags_open_fail_verdict(env):
    """预检层：open FAIL 时 CEO done_slice 同步预检被拒并带明细。"""
    await _insert_task(
        env["workspace_path"],
        "t-fail-pre",
        status="running",
        evidence={"verdict": "FAIL", "blocking_issues": ["/x"]},
    )
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_outstanding_ask_senders",
        new=AsyncMock(return_value=set()),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_sent_recipients_since",
        new=AsyncMock(return_value=set()),
    ):
        violations = await pre_check_exit_gates(
            CEO_ID, PROJECT_ID, phase="done_slice"
        )
    assert "CEO_PROJECT_PENDING" in violations
    details = pop_ceo_project_pending_details(CEO_ID)
    assert any("FAIL" in d for d in details)
