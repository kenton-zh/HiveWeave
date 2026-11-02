"""A3 事实位接线（批 3，报告 §2-A3）的集成向守卫。

三组强制触发 + 断言对应位被盖（判据：「触发路径跑过吗」——每个用例都打在
**真实生产路径**上，不直接 UPDATE 造假行）：

1. **被扫断 vs 真失败**（timeout_kind='swept'）：
   - create_activation 孤儿清扫：非超时 run 的被扫步骤盖 'swept'；
     整轮总超时 run 仍盖 'turn'（F7 既有行为，钉住先后顺序）；
   - startup_sweep（进程死亡 / 收尾回收）的被扫步骤盖 'swept'。
2. **attestation_mismatch**：真实 submit_task_tool 证据门拒绝 →
   finalize_tool_result 漏斗盖 denied_by → record_step_end 落库可查。
3. **cross_level**：真实 org_span._validate_span 拒绝文案 → 漏斗盖
   denied_by='cross_level'（钉住「生产者文案 ↔ 漏斗 needle」的对齐，
   防文案漂移把签名表打哑）。

上游对照：DSH ``packages/core/session/src/repair.ts:14-18``（具名恢复码）；
P0-3 的闭合枚举纪律（``denied_by IS NULL`` = 未记录，与每格不同形）。
"""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services import task as task_module
from hiveweave.services.run_ledger import RunLedger, sweep_stale_agent_runs
from hiveweave.tools.fact_positions import (
    classify_denied_by_text,
    finalize_tool_result,
)
from hiveweave.tools.result import ToolResult
from hiveweave.tools.tasks.submit import SubmitTaskParams, submit_task_tool

PROJECT_ID = "test-a3-wiring"
COORD_ID = "coord-a3"
EXEC_ID = "exec-a3"


# ── 1. 被扫断步骤：timeout_kind='swept' ─────────────────────────────


class _FakeProjectDb:
    """最小内存 stand-in：承接 run_ledger 的 execute/query/迁移路径。"""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE agent_runs ("
            "id TEXT PRIMARY KEY, agent_id TEXT, activation_id TEXT, "
            "status TEXT NOT NULL DEFAULT 'running', started_at INTEGER, "
            "ended_at INTEGER, error_reason TEXT, result_summary TEXT, "
            "upstream_retry_attempt INTEGER, stall_reason TEXT, "
            "empty_stream INTEGER DEFAULT 0, cache_verdict TEXT, "
            "cache_drifts TEXT, orphan_steps INTEGER DEFAULT 0, "
            "inflight_request_started_at INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE agent_activations ("
            "id TEXT PRIMARY KEY, agent_id TEXT, trigger_type TEXT, "
            "trigger_source TEXT, trigger_detail TEXT, inbox_msg_ids TEXT, "
            "interrupted_run_id TEXT, checkpoint_summary TEXT, created_at INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE run_steps ("
            "id TEXT PRIMARY KEY, run_id TEXT, step_index INTEGER, "
            "step_type TEXT, tool_name TEXT, tool_call_id TEXT, "
            "tool_args_hash TEXT, tool_args_excerpt TEXT, "
            "status TEXT NOT NULL DEFAULT 'pending', result_hash TEXT, "
            "result_size INTEGER, result_excerpt TEXT, error TEXT, "
            "started_at INTEGER, ended_at INTEGER, duration_ms INTEGER, "
            "runner_failed INTEGER DEFAULT 0, command_failed INTEGER DEFAULT 0, "
            "injection_applied INTEGER DEFAULT 0, timeout_kind TEXT, "
            "timeout_ms INTEGER, outcome_unknown INTEGER DEFAULT 0, "
            "not_started INTEGER DEFAULT 0, started INTEGER DEFAULT 0)"
        )
        self.conn.commit()

    async def execute(self, agent_id: str, sql: str, params=None) -> None:
        self.conn.execute(sql, params or [])
        self.conn.commit()

    async def query(self, agent_id: str, sql: str, params=None):
        return self.conn.execute(sql, params or []).fetchall()

    async def query_one(self, agent_id: str, sql: str, params=None):
        rows = await self.query(agent_id, sql, params)
        return rows[0] if rows else None

    async def schema_marker_key_for_agent(self, agent_id: str):
        return ("fake-a3-ws", 1)

    def step(self, step_id: str) -> dict:
        self.conn.row_factory = sqlite3.Row
        row = self.conn.execute(
            "SELECT * FROM run_steps WHERE id = ?", [step_id]
        ).fetchone()
        return dict(row) if row else {}


async def _seed_orphan(
    fake: _FakeProjectDb, *, run_status: str, error_reason: str, started: int
) -> str:
    """一条已死 run + 一条滞留 running 的孤儿步骤（record_step_start 的形状）。"""
    fake.conn.execute(
        "INSERT INTO agent_runs (id, agent_id, status, error_reason, started_at) "
        "VALUES (?, ?, ?, ?, 1000)",
        ["run-1", "a1", run_status, error_reason],
    )
    fake.conn.execute(
        "INSERT INTO run_steps (id, run_id, step_index, step_type, tool_name, "
        "status, started_at, started) "
        "VALUES (?, 'run-1', 0, 'tool_call', 'bash', 'running', 2000, ?)",
        ["step-1", started],
    )
    fake.conn.commit()
    return "step-1"


@pytest.mark.asyncio
async def test_activation_sweep_stamps_swept_on_non_timeout_run():
    """非超时死亡的 run：被扫步骤盖 timeout_kind='swept'（修「被扫断 vs 真失败」混同）。"""
    fake = _FakeProjectDb()
    await _seed_orphan(
        fake, run_status="error", error_reason="upstream 500: region error", started=1
    )
    with patch("hiveweave.services.run_ledger.project_db", fake):
        ledger = RunLedger()
        await ledger.create_activation("a1", "timer")
    step = fake.step("step-1")
    assert step["status"] == "error"
    assert step["outcome_unknown"] == 1
    assert step["timeout_kind"] == "swept", step["timeout_kind"]


@pytest.mark.asyncio
async def test_activation_sweep_keeps_turn_for_total_timeout_run():
    """整轮总超时的 run：F7 的 'turn' 优先于 'swept'（钉住两条 UPDATE 的顺序）。"""
    fake = _FakeProjectDb()
    await _seed_orphan(
        fake, run_status="interrupted", error_reason="请求总超时（600s）", started=1
    )
    with patch("hiveweave.services.run_ledger.project_db", fake):
        ledger = RunLedger()
        await ledger.create_activation("a1", "timer")
    step = fake.step("step-1")
    assert step["timeout_kind"] == "turn", step["timeout_kind"]


@pytest.mark.asyncio
async def test_activation_sweep_stamps_swept_on_never_dispatched_step():
    """从未派发（started=0）的被扫步骤同样盖 'swept' —— 终态是清扫员写的。"""
    fake = _FakeProjectDb()
    await _seed_orphan(
        fake, run_status="interrupted", error_reason="cancelled by user", started=0
    )
    with patch("hiveweave.services.run_ledger.project_db", fake):
        ledger = RunLedger()
        await ledger.create_activation("a1", "timer")
    step = fake.step("step-1")
    assert step["not_started"] == 1
    assert step["timeout_kind"] == "swept", step["timeout_kind"]


class _AsyncCursor:
    """aiosqlite 游标的最小 async 形态（fetchall/close 都要可 await）。"""

    def __init__(self, cur: sqlite3.Cursor) -> None:
        self._cur = cur

    async def fetchall(self):
        return self._cur.fetchall()

    async def close(self) -> None:
        pass


class _FakeSweepConn:
    """startup_sweep 的共享连接桩：真实 sqlite + commit 记账。"""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE agent_runs ("
            "id TEXT PRIMARY KEY, agent_id TEXT, status TEXT, started_at INTEGER, "
            "lease_expires_at INTEGER, "
            "ended_at INTEGER, error_reason TEXT, result_summary TEXT, "
            "actual_llm_calls INTEGER DEFAULT 0, "
            "inflight_request_started_at INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE run_steps ("
            "id TEXT PRIMARY KEY, run_id TEXT, step_index INTEGER, "
            "step_type TEXT, tool_name TEXT, status TEXT, error TEXT, "
            "started_at INTEGER, ended_at INTEGER, started INTEGER, "
            "outcome_unknown INTEGER DEFAULT 0, not_started INTEGER DEFAULT 0, "
            "timeout_kind TEXT)"
        )
        self.conn.execute(
            "INSERT INTO agent_runs (id, agent_id, status, started_at) "
            "VALUES ('run-z', 'a1', 'running', 1000)"
        )
        self.conn.execute(
            "INSERT INTO run_steps (id, run_id, step_index, step_type, "
            "tool_name, status, started_at, started) "
            "VALUES ('step-z', 'run-z', 0, 'tool_call', 'bash', 'running', 2000, 1)"
        )
        self.conn.commit()

    async def execute(self, sql: str, params=None):
        cur = self.conn.execute(sql, params or [])
        return _AsyncCursor(cur)

    async def commit(self) -> None:
        self.conn.commit()

    def step(self, step_id: str) -> dict:
        self.conn.row_factory = sqlite3.Row
        row = self.conn.execute(
            "SELECT * FROM run_steps WHERE id = ?", [step_id]
        ).fetchone()
        return dict(row) if row else {}


@pytest.mark.asyncio
async def test_startup_sweep_stamps_swept():
    """startup_sweep（进程死亡遗留）：被扫步骤盖 timeout_kind='swept'。"""
    conn = _FakeSweepConn()

    async def fake_ensure(ws):
        return conn

    @asynccontextmanager
    async def _lock_cm(ws):
        yield

    async def fake_lock(ws):
        return _lock_cm(ws)

    with (
        patch.object(project_db, "ensure_project_db", fake_ensure),
        patch.object(project_db, "get_workspace_write_lock", fake_lock),
    ):
        n = await sweep_stale_agent_runs("/tmp/fake-a3-ws")
    assert n == 1
    step = conn.step("step-z")
    assert step["status"] == "error"
    assert step["outcome_unknown"] == 1
    assert step["timeout_kind"] == "swept", step["timeout_kind"]


# ── 2/3. 拒绝类回执：denied_by 走漏斗统一盖 ─────────────────────────


def test_classify_denied_by_text_needles():
    """签名表单元：命中两类拒因；空/无关文案返回 None（不猜）。"""
    assert (
        classify_denied_by_text(
            "submit_task attestation gate failed (code_audit_visual): "
            "no matching attestation for kind browse_e2e"
        )
        == "attestation_mismatch"
    )
    assert (
        classify_denied_by_text("Cannot approve: attestation gate failed (p): x")
        == "attestation_mismatch"
    )
    assert (
        classify_denied_by_text("拒绝跨级派活：只能联系直属下属。") == "cross_level"
    )
    assert (
        classify_denied_by_text("拒绝跨级沟通：只能联系直属下属/上级/同级。")
        == "cross_level"
    )
    assert classify_denied_by_text("") is None
    assert classify_denied_by_text("tests failed: 3 assertions") is None


def test_funnel_stamps_denied_by_and_validates_enum():
    """漏斗：显式位透传、表外值丢弃、成功与普通失败不沾。"""
    out = finalize_tool_result(
        "submit_task",
        {"success": False, "error": "submit_task attestation gate failed (p): x"},
    )
    assert out["denied_by"] == "attestation_mismatch"
    out = finalize_tool_result(
        "dispatch_task",
        ToolResult.err("拒绝跨级派活：只能联系直属下属。"),
    )
    assert out["denied_by"] == "cross_level"
    # 显式盖的位（acl/disk 构造点）原样透传，不被文案层覆盖
    out = finalize_tool_result(
        "bash",
        {
            "success": False,
            "blocked": True,
            "fact": "runner_failed",
            "denied_by": "outside_boundary",
            "error": "写入被沙箱拒绝（授权树 D:\\x 之外）",
        },
    )
    assert out["denied_by"] == "outside_boundary"
    # 表外值 = 丢弃（NULL = 未记录，闭合枚举纪律）
    out = finalize_tool_result(
        "x", {"success": False, "denied_by": "bogus_kind", "error": "boom"}
    )
    assert out.get("denied_by") is None
    # 成功结果（message_team 部分成功回执带 "Cross-level blocked"）不算拒绝
    out = finalize_tool_result(
        "message_team",
        {"success": True, "output": "Messages sent. Cross-level blocked: [a]"},
    )
    assert out.get("denied_by") is None


def test_real_cross_level_text_hits_funnel():
    """生产者对齐钉线：org_span **真实**拒绝文案必须命中漏斗 needle。

    若 `_validate_span` 的文案被改写而签名表没跟，本测试先红 —— 这正是
    「文案判据会随措辞整体失效」的护栏：改文案的人被迫同时改判据。
    """
    from hiveweave.services.org_span import validate_dispatch_span

    class _StubOrg:
        async def get_agent(self, agent_id):
            if agent_id == "leaf-1":  # 发起者：P1 下的叶子
                return {"id": agent_id, "name": "叶子一", "parent_id": "p1",
                        "status": "active"}
            # 目标：另一棵子树（P2）的叶子 ⇒ 跨级
            return {"id": agent_id, "name": "叶子二", "parent_id": "p2",
                    "status": "active"}

    import asyncio

    denial = asyncio.run(validate_dispatch_span("leaf-1", "leaf-2", _StubOrg()))
    assert denial and denial.startswith("拒绝跨级派活"), denial
    out = finalize_tool_result(
        "dispatch_task", ToolResult.err(denial)  # dispatch_task_tool:268 的原样出口
    )
    assert out["denied_by"] == "cross_level", out.get("denied_by")
    assert out.get("error", "").startswith("拒绝跨级派活")


# ── 端到端：真实 submit 拒绝 → 漏斗 → run_steps 落库 ─────────────────


@pytest.fixture
async def a3_env():
    """temp 项目库 + 假 agent 行（同 test_gate_intelligence_pack 的 env 形态）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid):
            return PROJECT_ID if aid in (COORD_ID, EXEC_ID) else None

        _FAKE_AGENTS = {
            COORD_ID: {
                "id": COORD_ID, "name": "协调员", "short_id": "C01",
                "parent_id": None, "permission_type": "coordinator",
                "role": "架构师", "status": "active",
            },
            EXEC_ID: {
                "id": EXEC_ID, "name": "执行者", "short_id": "E01",
                "parent_id": COORD_ID, "permission_type": "executor",
                "role": "engineer", "status": "active",
            },
        }

        async def fake_get_agent_by_id(aid):
            return _FAKE_AGENTS.get(aid)

        att_module._migrated.clear()
        task_module._migrated.clear()
        project_db._agent_cache.pop(COORD_ID, None)
        project_db._agent_cache.pop(EXEC_ID, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace", fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id", fake_get_agent_project_id),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
        ):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
                "executor_id": EXEC_ID,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(COORD_ID, None)
        project_db._agent_cache.pop(EXEC_ID, None)


@pytest.mark.asyncio
async def test_attestation_denial_lands_denied_by_in_run_steps(a3_env):
    """强制触发一次 attestation 拒绝：工具出口 → 漏斗 → record_step_end → DB。"""
    from hiveweave.services.task import TaskService

    svc = TaskService()
    tid = await svc.create_task(
        PROJECT_ID, "A3 接线验收任务", "d",
        creator_id=COORD_ID,
        assignee_id=EXEC_ID,
        tags=["generic_tests"],
        source="agent",
    )
    await svc.claim_task(PROJECT_ID, tid, EXEC_ID, bypass_verify_serialize=True)
    await svc.start_task(PROJECT_ID, tid)

    async def _fake_verify_ids(project_id, ids, **kwargs):
        return False, "no matching attestation for kind bash_test"

    with ExitStack() as stack:
        stack.enter_context(
            patch("hiveweave.tools.helpers.get_project_id",
                  new=AsyncMock(return_value=PROJECT_ID))
        )
        stack.enter_context(
            patch("hiveweave.services.attestation.required_attestation_kinds",
                  return_value=frozenset({"bash_test"}))
        )
        stack.enter_context(
            patch("hiveweave.services.attestation.has_valid_waiver",
                  new=AsyncMock(return_value=False))
        )
        stack.enter_context(
            patch("hiveweave.services.attestation.attestation_service.find_recent_for_agent",
                  new=AsyncMock(return_value=[]))
        )
        stack.enter_context(
            patch("hiveweave.services.attestation.attestation_service.verify_ids",
                  new=AsyncMock(side_effect=_fake_verify_ids))
        )
        result = await submit_task_tool(
            SubmitTaskParams(task_id=tid, summary="done", tests_passed=True),
            EXEC_ID, a3_env["workspace_path"],
        )

    # ① 工具出口：真实证据门拒绝（needle 前置条件）
    assert result.success is False
    assert "attestation gate failed" in (result.error or ""), result.error

    # ② 漏斗盖戳（executor normalize 尾的生产路径）
    stamped = finalize_tool_result("submit_task", result)
    assert stamped["denied_by"] == "attestation_mismatch", stamped.get("denied_by")

    # ③ 落库（agents/streaming.py:386 的消费形态原样重放）→ run_steps 可查
    ledger = RunLedger()
    step_id = await ledger.record_step_start(EXEC_ID, "run-a3", 0, "tool_call",
                                             tool_name="submit_task")
    assert step_id
    await ledger.record_step_end(
        EXEC_ID, step_id, status="failed",
        error=stamped.get("error"),
        denied_by=stamped.get("denied_by"),
    )
    rows = await project_db.query(
        EXEC_ID,
        "SELECT denied_by, timeout_kind, status FROM run_steps WHERE id = ?",
        [step_id],
    )
    assert rows, "run_steps 行丢失"
    assert rows[0]["denied_by"] == "attestation_mismatch", rows[0]
    assert rows[0]["timeout_kind"] is None  # 真失败：不被 swept/turn 沾染
