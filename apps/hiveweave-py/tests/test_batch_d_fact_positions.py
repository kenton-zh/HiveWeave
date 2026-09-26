"""批 D 第 2 步：事实位接线 + 记账（审计报告 §6 第 2 步）的守卫测试。

七组断言（对应任务 1-6 + 第 3 步验收机检的前置事实位）：
1. 异常出口落位断言（含 is_platform_bug 判定表）；
2. 命令三正交位独立断言（exit=0 但 output_empty=1 不再假成功，P2-20）；
3. stall_reason 落 agent_runs 断言；
4. timeout_layer + partial_usage 接账断言；
5. compaction duration_ms>0 断言；
6. context 两条旁路 client.post 记账断言；
7. 结构化签名聚类断言（同 tool+同事实位 → 同签名，distinct_hitters 能攒到
   2）+ 记忆读侧自动注入断言。

上游对照（三件套见各用例 docstring）：
- DSH ``docs/defensive-patterns.md:7-9``（HEAD 477b4f420）：Report orthogonal
  outcomes independently —— Surface each independent fact on its own。
- DSH ``packages/core/session/src/repair.ts:16-21``：具名恢复码按事实归类。
- pi ``packages/coding-agent/src/core/cache-stats.ts:56-90``（2b0a123de）：
  归因四列各自成列，不嵌套文本推断。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 任务 1：异常出口事实位 ───────────────────────────────────────────


class TestPlatformBugClassification:
    """is_platform_bug 判定表（单一实现在 tool_exec，streaming 共用）。"""

    def test_platform_side_exceptions(self):
        from hiveweave.llm.streamer.tool_exec import is_platform_bug_exception

        # 73 项目 38 条 _ConfinedDevProc 的形态：AttributeError = 平台 bug。
        assert is_platform_bug_exception(AttributeError("no attr")) is True
        assert is_platform_bug_exception(KeyError("k")) is True
        assert is_platform_bug_exception(OSError("disk")) is True
        assert is_platform_bug_exception(AssertionError("inv")) is True
        # FileNotFoundError / PermissionError 是 OSError 子类 ⇒ 平台侧。
        assert is_platform_bug_exception(FileNotFoundError("x")) is True

    def test_tool_business_exceptions(self):
        from hiveweave.llm.streamer.tool_exec import is_platform_bug_exception

        assert is_platform_bug_exception(ValueError("bad input")) is False

    def test_unclassified_is_none(self):
        """表外异常 = 未判定（None），调用方不落库（宁可留 NULL 不臆断）。"""
        from hiveweave.llm.streamer.tool_exec import is_platform_bug_exception

        assert is_platform_bug_exception(RuntimeError("weird")) is None
        assert is_platform_bug_exception(Exception("base")) is None

    def test_exception_fact_flags_shape(self):
        from hiveweave.llm.streamer.tool_exec import exception_fact_flags

        flags = exception_fact_flags(AttributeError("_ConfinedDevProc x"))
        assert flags == {
            "exception_type": "AttributeError",
            "is_platform_bug": True,
        }
        # 未判定 ⇒ 只带 exception_type，is_platform_bug 不写（None 不落）。
        flags2 = exception_fact_flags(RuntimeError("r"))
        assert flags2 == {"exception_type": "RuntimeError"}


def _make_streaming_agent(execute_side_effect):
    """与 test_p0_3_orphan_root_cause 同款最小 agent 桩。"""
    agent = SimpleNamespace()
    agent.id = "a1"
    agent.project_id = "p1"
    agent._current_run_id = "run-1"
    agent._run_step_counter = 0

    async def _ws():
        return "/tmp/ws"

    agent._get_workspace_path = _ws
    agent._stop_heartbeat = lambda: None

    ledger = AsyncMock()
    ledger.record_step_start = AsyncMock(return_value="step-1")
    agent._run_ledger = ledger

    executor = AsyncMock()
    executor.execute = AsyncMock(side_effect=execute_side_effect)
    agent._tool_executor = executor
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,expected_type,expected_bug",
    [
        (AttributeError("_ConfinedDevProc has no attr"), "AttributeError", True),
        (OSError("spawn failed"), "OSError", True),
        (ValueError("bad value"), "ValueError", False),
        (RuntimeError("unclassified"), "RuntimeError", None),
    ],
)
async def test_exception_exit_lands_structured_facts(
    monkeypatch, exc, expected_type, expected_bug
):
    """异常出口 → record_step_end 携带 exception_type / is_platform_bug。

    行级落点在 streaming.py 的 except（那里有 step_id，是行关闭唯一点）；
    判定单一实现在 tool_exec。None（未判定）必须原样透传（不落臆断值）。
    """
    from hiveweave.agents import streaming as st

    agent = _make_streaming_agent(exc)
    monkeypatch.setattr(st, "broadcast_stream_event", lambda *a, **k: None)
    monkeypatch.setattr(
        st.meta_db, "get_project_workspace", AsyncMock(return_value="/tmp/ws")
    )
    with pytest.raises(type(exc)):
        await st.on_tool_call(agent, "read_file", '{"filePath":"x"}', "call-1")

    agent._run_ledger.record_step_end.assert_awaited()
    kw = agent._run_ledger.record_step_end.await_args.kwargs
    assert kw["status"] == "failed"
    assert kw["exception_type"] == expected_type
    assert kw["is_platform_bug"] is expected_bug


# ── 任务 2：命令三正交位（P2-20）─────────────────────────────────────


def _seed_sandbox_mocks(monkeypatch):
    from hiveweave.tools import bash as bash_mod

    monkeypatch.setattr(bash_mod, "_run_sandboxed", None)  # 占位，下面替换
    monkeypatch.delattr(bash_mod, "_run_sandboxed", raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stdout,exit_code,expect_empty",
    [
        ("", 0, True),  # exit=0 且空输出 —— P2-20 的假成功形态，位上可辨
        ("hello\n", 0, False),  # exit=0 且有产出 —— 与上一行**不同形**
    ],
)
async def test_exit0_empty_output_orthogonal_bits(
    monkeypatch, tmp_path, stdout, exit_code, expect_empty
):
    """三正交位**互相独立**上报：exit_code / output_empty / truncated。

    上游戒律（DSH defensive-patterns.md:7-9，HEAD 477b4f420 原话）：
    「Surface each independent fact (timedOut, signal, exitCode) on its own;
    never nest one flag's report inside another's branch, or a caller reads a
    cut-short run as a clean success.」—— 空输出的成功不再与有产出的成功
    在数据里同形。
    """
    from hiveweave.tools import bash as bash_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    async def _fake_run_sandboxed(*args, **kw):
        return {
            "output": stdout,
            "stdout": stdout,
            "stderr": "",
            "exit_code": exit_code,
            "timed_out": False,
            "error": None,
        }

    monkeypatch.setattr(bash_mod, "_run_sandboxed", _fake_run_sandboxed)
    result = await bash_mod.execute_bash(
        command="echo hi",
        workdir="",
        workspace_path=str(ws),
        timeout_ms=15000,
        agent_id=None,
    )
    assert result["success"] is True
    # 三个独立事实并排存在，绝不嵌套：
    assert result["exit_code"] == 0
    assert result["output_empty"] is expect_empty
    assert result["truncated"] is False
    if expect_empty:
        # 假成功通道从此可机检：exit=0 ∧ output_empty=1 同时可读。
        assert result["exit_code"] == 0 and result["output_empty"]


@pytest.mark.asyncio
async def test_nonzero_exit_carries_bits_and_fact(monkeypatch, tmp_path):
    from hiveweave.tools import bash as bash_mod

    ws = tmp_path / "ws"
    ws.mkdir()

    async def _fake_run_sandboxed(*args, **kw):
        return {
            "output": "assert 1 == 2 failed",
            "stdout": "assert 1 == 2 failed",
            "stderr": "",
            "exit_code": 3,
            "timed_out": False,
            "error": None,
        }

    monkeypatch.setattr(bash_mod, "_run_sandboxed", _fake_run_sandboxed)
    result = await bash_mod.execute_bash(
        command="pytest -q",
        workdir="",
        workspace_path=str(ws),
        timeout_ms=15000,
        agent_id=None,
    )
    assert result["success"] is False
    assert result["exit_code"] == 3
    assert result["output_empty"] is False
    assert result["fact"] == "command_failed"


@pytest.mark.asyncio
async def test_huge_output_marks_truncated(monkeypatch, tmp_path):
    """truncated 位：输出超 1MB 帽 ⇒ truncated=1（与 exit_code 正交）。"""
    from hiveweave.tools import bash as bash_mod

    big = "x" * (2 * 1024 * 1024)
    ws = tmp_path / "ws"
    ws.mkdir()

    async def _fake_run_sandboxed(*args, **kw):
        return {
            "output": big,
            "stdout": big,
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "error": None,
        }

    monkeypatch.setattr(bash_mod, "_run_sandboxed", _fake_run_sandboxed)
    result = await bash_mod.execute_bash(
        command="cat big.log",
        workdir="",
        workspace_path=str(ws),
        timeout_ms=15000,
        agent_id=None,
    )
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_run_command_orthogonal_bits(monkeypatch, tmp_path):
    """P2-1：run_command 两出口同样落三正交位（此前 exit_code 有了、
    output_empty/truncated 恒 NULL）。空输出 + exit=0 的假成功形态可辨。"""
    from types import SimpleNamespace

    from hiveweave.tools import bash as bash_mod

    ws = tmp_path / "ws"
    ws.mkdir()

    async def _fake_validate(*args, **kw):
        return False, ""

    async def _fake_spawn_decision(project_id):
        return SimpleNamespace(confined=False)

    async def _fake_run_sandboxed(*args, **kw):
        return {"output": "", "stdout": "", "stderr": "",
                "exit_code": 0, "timed_out": False, "error": None}

    monkeypatch.setattr(
        bash_mod, "_validate_command_safety_resolved", _fake_validate
    )
    monkeypatch.setattr(bash_mod, "_pwsh_effective_shell", lambda **kw: False)
    monkeypatch.setattr(
        bash_mod, "_pwsh_dialect_gate", lambda command, confined: None
    )
    monkeypatch.setattr(
        "hiveweave.services.acl_sandbox.policy.resolve_spawn_decision",
        _fake_spawn_decision,
    )
    monkeypatch.setattr(
        "hiveweave.services.eval_seal.sealed_bash_deny_for_workspace",
        lambda ws_path, cmd: None,
    )
    monkeypatch.setattr(bash_mod, "_run_sandboxed", _fake_run_sandboxed)
    result = await bash_mod.execute_run_command(
        command="echo done",
        cwd="",
        timeout_ms=15000,
        workspace_path=str(ws),
        agent_id=None,
    )
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["output_empty"] is True
    assert result["truncated"] is False


# ── DB-backed 夹具（照 test_upstream_recovery 的真实 per-project DB 模式）──

PROJECT_ID = "bd-proj-0001"
AGENT_ID = "bd-exec-0001"


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    from hiveweave.db import meta as meta_db

    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()
    yield
    await meta_db.close_meta_db()


@pytest.fixture
async def seeded_project(tmp_path):
    """Meta projects 行 + per-project DB（agent 行）+ 内存路由。"""
    from hiveweave.db import meta as meta_db
    from hiveweave.db import project as project_db

    ws = str(tmp_path / "ws")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "bd-test", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    cur = await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "model_id, created_at) VALUES (?, 'BD', ?, 'BdExec', 'executor', "
        "'active', 'gpt-x', ?)",
        [AGENT_ID, PROJECT_ID, now],
    )
    await cur.close()
    await conn.commit()
    from hiveweave.services.agent_router import AgentRoute, agent_router

    agent_router.reset_for_tests()
    agent_router.register(
        AgentRoute(
            agent_id=AGENT_ID,
            project_id=PROJECT_ID,
            workspace_path=ws,
            short_id="BD",
            name="BdExec",
            role="executor",
            status="active",
        )
    )
    yield conn
    agent_router.reset_for_tests()


async def _seed_run(
    conn,
    run_id: str = "bd-run-1",
    status: str = "completed",
) -> None:
    cur = await conn.execute(
        "INSERT INTO agent_runs (id, agent_id, activation_id, status, "
        "lease_expires_at, budget_llm_calls, budget_tool_calls, "
        "budget_elapsed_ms, actual_llm_calls, actual_tool_calls, started_at) "
        "VALUES (?, ?, NULL, ?, 0, 50, 100, 600000, 0, 0, ?)",
        [run_id, AGENT_ID, status, int(time.time() * 1000)],
    )
    await cur.close()
    await conn.commit()


# ── run_steps 新列落库（任务 1/2 的行级验收）─────────────────────────


@pytest.mark.asyncio
async def test_run_steps_new_fact_columns_landed(seeded_project):
    """exception_type / is_platform_bug / exit_code / output_empty /
    truncated 五列随 record_step_end 落库；未确定位保持 NULL。"""
    from hiveweave.services.run_ledger import RunLedger

    ledger = RunLedger()
    step_a = await ledger.record_step_start(
        AGENT_ID, "bd-run-1", 0, "tool_call",
        tool_name="start_dev_server", tool_call_id="call-a",
    )
    await ledger.record_step_end(
        AGENT_ID, step_a, status="failed",
        error="AttributeError: x",
        exception_type="AttributeError", is_platform_bug=True,
    )
    step_b = await ledger.record_step_start(
        AGENT_ID, "bd-run-1", 1, "tool_call",
        tool_name="bash", tool_call_id="call-b",
    )
    await ledger.record_step_end(
        AGENT_ID, step_b, status="completed",
        exit_code=0, output_empty=True, truncated=False,
    )
    cur = await seeded_project.execute(
        "SELECT exception_type, is_platform_bug, exit_code, output_empty, "
        "truncated FROM run_steps WHERE tool_call_id = 'call-a'"
    )
    row = (await cur.fetchall())[0]
    assert row[0] == "AttributeError"
    assert row[1] == 1  # 平台 bug —— 38 条 AttributeError 的应落形态
    # 未确定的位保持 NULL（不臆断）：
    assert row[2] is None and row[3] is None and row[4] is None
    cur = await seeded_project.execute(
        "SELECT exit_code, output_empty, truncated, exception_type, "
        "is_platform_bug FROM run_steps WHERE tool_call_id = 'call-b'"
    )
    row = (await cur.fetchall())[0]
    # 三正交位并排：exit=0 ∧ output_empty=1 —— 假成功不再不可机检。
    assert (row[0], row[1], row[2]) == (0, 1, 0)
    assert row[3] is None and row[4] is None


# ── 任务 3：stall_reason 落 agent_runs ───────────────────────────────


@pytest.mark.asyncio
async def test_stall_reason_lands_on_agent_runs(seeded_project):
    """completion 的 stall_break 消费分支走 set_run_fact(stall_reason=…)
    —— 这里钉 ledger 写口：值可落、可读、跨 run 可机检。"""
    from hiveweave.services.run_ledger import RunLedger

    await _seed_run(seeded_project)
    ledger = RunLedger()
    await ledger.set_run_fact(AGENT_ID, "bd-run-1", stall_reason="tool_failed")
    cur = await seeded_project.execute(
        "SELECT stall_reason FROM agent_runs WHERE id = 'bd-run-1'"
    )
    row = (await cur.fetchall())[0]
    assert row[0] == "tool_failed"

    # 未知键仍被白名单拒绝（既有的守卫语义不回退）。
    await ledger.set_run_fact(AGENT_ID, "bd-run-1", bogus_fact="x")
    cur = await seeded_project.execute(
        "SELECT stall_reason FROM agent_runs WHERE id = 'bd-run-1'"
    )
    assert (await cur.fetchall())[0][0] == "tool_failed"


# ── 任务 4：timeout_layer + partial_usage 接账 ──────────────────────


class _DeadStreamExc(Exception):
    """模拟批 B 判死异常：partial_usage + timeout_layer 随行。"""


@pytest.mark.asyncio
async def test_partial_usage_recorded_with_timeout_layer(seeded_project):
    """code_audit except 的接账缝：断流已收 usage 进 llm_usage，
    timeout_layer 随行落新列（批 B 审计点名的丢弃点）。"""
    from hiveweave.services.code_audit import _record_partial_audit_usage

    exc = _DeadStreamExc("idle timeout")
    setattr(exc, "partial_usage", {"prompt_tokens": 1000, "completion_tokens": 20})
    setattr(exc, "timeout_layer", "idle")

    meta = await _record_partial_audit_usage(
        project_id=PROJECT_ID,
        agent_id=AGENT_ID,
        chosen=None,  # 造不出 provider 时降级 None —— openai 形状兜底
        exc=exc,
        timeout_layer="idle",
    )
    assert meta["partial_usage_recorded"] is True
    assert meta["partial_total_tokens"] == 1020
    cur = await seeded_project.execute(
        "SELECT request_type, timeout_layer, input_tokens, output_tokens, "
        "total_tokens FROM llm_usage WHERE agent_id = ?",
        [AGENT_ID],
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "oneshot"
    assert r[1] == "idle"
    assert (r[2], r[3], r[4]) == (1000, 20, 1020)


@pytest.mark.asyncio
async def test_no_partial_usage_records_nothing(seeded_project):
    from hiveweave.services.code_audit import _record_partial_audit_usage

    exc = _DeadStreamExc("no usage attached")
    setattr(exc, "timeout_layer", "total")
    meta = await _record_partial_audit_usage(
        project_id=PROJECT_ID,
        agent_id=AGENT_ID,
        chosen=None,
        exc=exc,
        timeout_layer="total",
    )
    assert meta == {}
    cur = await seeded_project.execute(
        "SELECT COUNT(*) FROM llm_usage WHERE agent_id = ?", [AGENT_ID]
    )
    assert (await cur.fetchall())[0][0] == 0


@pytest.mark.asyncio
async def test_record_rounds_timeout_layer_passthrough(seeded_project):
    """正常行 timeout_layer 恒 NULL；带键的行落值（缺键 → NULL 不臆断）。"""
    from hiveweave.services.token_meter import token_meter

    await token_meter.record_rounds(
        AGENT_ID, PROJECT_ID,
        [{"input": 10, "output": 5, "cache_read": 0, "cache_creation": 0,
          "total": 15, "duration_ms": 7}],
        request_type="main",
    )
    await token_meter.record_rounds(
        AGENT_ID, PROJECT_ID,
        [{"input": 10, "output": 5, "cache_read": 0, "cache_creation": 0,
          "total": 15, "duration_ms": 7, "timeout_layer": "first_chunk"}],
        request_type="main",
    )
    cur = await seeded_project.execute(
        "SELECT timeout_layer FROM llm_usage WHERE agent_id = ? "
        "ORDER BY rowid", [AGENT_ID],
    )
    rows = await cur.fetchall()
    assert rows[0][0] is None
    assert rows[1][0] == "first_chunk"


# ── 任务 5：compaction 计时 + 旁路记账 ───────────────────────────────


@pytest.mark.asyncio
async def test_record_compaction_duration_ms(seeded_project):
    """duration_ms 字面量 0 → 占位符：传实测值即落，不再恒 0。"""
    from hiveweave.services.token_meter import token_meter

    await token_meter.record_compaction(
        agent_id=AGENT_ID, model_id="m1", input_tokens=100, output_tokens=10,
        duration_ms=4321,
    )
    cur = await seeded_project.execute(
        "SELECT request_type, duration_ms FROM llm_usage WHERE agent_id = ?",
        [AGENT_ID],
    )
    r = (await cur.fetchall())[0]
    assert r[0] == "compaction_conversation"
    assert r[1] == 4321


class _FakeResp:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict):
        self._payload = payload

    async def post(self, *a, **k):
        await asyncio.sleep(0.001)  # 让 duration_ms 有非零可测的量
        return _FakeResp(self._payload)

    async def aclose(self):
        pass


class _FakeProvider:
    """ProviderConfig 的最小鸭子桩（build_url/headers/body/client）。"""

    api_format = SimpleNamespace(value="openai")
    model_name = "fake-model"

    def build_url(self) -> str:
        return "http://fake/v1/chat/completions"

    def build_headers(self, session_id=None) -> dict:
        return {}

    def build_body(self, **kw):
        return {"model": "fake", "messages": kw.get("messages") or []}

    def build_client(self):
        return self._client


_SUMMARY_PAYLOAD = {
    "choices": [{"message": {"content": "summary text"}}],
    "usage": {"prompt_tokens": 500, "completion_tokens": 40},
}


@pytest.mark.asyncio
async def test_working_set_head_bypass_records_usage(seeded_project):
    """context._summarize_working_set_head（旁路 client.post #1）→
    llm_usage 记账（request_type=compaction_working_set，duration 实测）。"""
    from hiveweave.llm.streamer.context import ContextMixin
    from hiveweave.services.token_meter import token_meter

    provider = _FakeProvider()
    provider._client = _FakeClient(_SUMMARY_PAYLOAD)
    text = await ContextMixin._summarize_working_set_head(
        SimpleNamespace(), provider, "transcript", session_id=AGENT_ID,
        agent_id=AGENT_ID,
    )
    assert text == "summary text"
    cur = await seeded_project.execute(
        "SELECT request_type, model_id, input_tokens, duration_ms, "
        "project_id FROM llm_usage WHERE agent_id = ?",
        [AGENT_ID],
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "compaction_working_set"
    assert rows[0][1] == "fake-model"
    assert rows[0][2] == 500
    assert rows[0][3] > 0  # 实测计时（≥1ms）
    # P1-1（独立审计）：project_id 必须非空 —— Token 页四类消费方全按
    # project_id 过滤，NULL 行等于没修盲区。
    assert rows[0][4] == PROJECT_ID
    summary = await token_meter.agent_summary(PROJECT_ID, AGENT_ID)
    assert summary["llm_calls"] == 1
    assert summary["input_tokens"] == 500


@pytest.mark.asyncio
async def test_max_rounds_summary_bypass_records_usage(seeded_project):
    """context._make_max_rounds_summary（旁路 client.post #2）→
    llm_usage 记账（request_type=turn_summary）。"""
    from hiveweave.llm.streamer.context import ContextMixin

    mixin = ContextMixin()

    async def _fake_fire(on_delta, event):
        pass

    mixin._fire_delta = _fake_fire  # 生产环境由 Streamer 混入提供
    provider = _FakeProvider()
    provider._client = _FakeClient(_SUMMARY_PAYLOAD)
    text = await mixin._make_max_rounds_summary(
        AGENT_ID, provider, [{"role": "user", "content": "hi"}], None,
        reason="max_rounds",
    )
    assert text == "summary text"
    cur = await seeded_project.execute(
        "SELECT request_type, total_tokens, project_id FROM llm_usage "
        "WHERE agent_id = ?",
        [AGENT_ID],
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "turn_summary"
    assert rows[0][1] == 540
    # P1-1（独立审计）：同上 —— 行必须带 project_id 才进 Token 页。
    assert rows[0][2] == PROJECT_ID


# ── 任务 6：结构化签名聚类 + 读侧自动注入 ────────────────────────────


def _fake_memory_service():
    """带内存存储的 MemoryService 桩（同 project 只存一行的 upsert 语义）。"""
    store: dict[str, dict] = {}

    svc = MagicMock()

    async def _get(pid):
        return list(store.values())

    async def _save(**kw):
        mid = kw.get("module_id")
        entry = {
            "type": "failure_signature",
            "content": kw.get("content"),
            "metadata": kw.get("metadata") or {},
            "module_id": mid,
            "source_agent_id": kw.get("source_agent_id"),
            "updated_at": int(time.time() * 1000),
            "created_at": int(time.time() * 1000),
        }
        prev = store.get(mid or "")
        if prev:  # upsert：保留 created_at
            entry["created_at"] = prev["created_at"]
        store[mid or ""] = entry
        return mid or "mem"

    svc.get_project_memories = _get
    svc.save_memory = _save
    return svc, store


@pytest.mark.asyncio
async def test_structured_signature_clusters_same_tool_and_bits(monkeypatch):
    """同 tool + 同事实位 → 同签名（module_id 相同）；连续两个不同 agent
    触发同签名异常 ⇒ 条目 metadata.distinct_hitters 真实攒到 2 人
    （P1-2：走 record_exception_failure_signature 全链，不手工调 merge）。"""
    from hiveweave.services import failure_signature as fs

    svc, store = _fake_memory_service()
    monkeypatch.setattr(
        "hiveweave.services.memory.MemoryService", MagicMock(return_value=svc)
    )
    monkeypatch.setattr(
        fs, "_project_id_of_agent", AsyncMock(return_value="proj")
    )

    rec_a = await fs.record_exception_failure_signature(
        agent_id="agent-A", tool_name="start_dev_server",
        exc=AttributeError("'job' object has no attribute 'exit_code'"),
        fact_flags={"exception_type": "AttributeError",
                    "is_platform_bug": True},
    )
    assert rec_a["written"] is True
    assert rec_a["sig"] == "platform_bug::start_dev_server::AttributeError"

    # 另一个 agent、**不同文案**的同根因平台 bug → 同一条目（聚类生效）。
    rec_b = await fs.record_exception_failure_signature(
        agent_id="agent-B", tool_name="start_dev_server",
        exc=AttributeError("totally different wording"),
        fact_flags={"exception_type": "AttributeError",
                    "is_platform_bug": True},
    )
    assert rec_b["module_id"] == rec_a["module_id"]
    assert rec_b["preexisting"] is True
    assert rec_b["preexisting_source"] == "agent-A"
    entry = store[rec_a["module_id"]]
    assert entry["metadata"]["hit_count"] == 2

    # P1-2 真断言：distinct_hitters 由 record 路径自动喂入（此前异常路径
    # 只增 hit_count 不聚人 ⇒ 组织升级 3/5/8 梯度对平台 bug 失灵）。
    hitters = [str(h) for h in entry["metadata"].get("distinct_hitters") or []]
    assert set(hitters) == {"agent-A", "agent-B"}
    # distinct_hitter_count 是**溢出计数**（集合截断后才 >0）——两人都在
    # 集合内 ⇒ 溢出 0、总数 = len(distinct_hitters) = 2。
    assert int(entry["metadata"].get("distinct_hitter_count") or 0) == 0
    assert len(hitters) == 2


@pytest.mark.asyncio
async def test_unclassified_exception_falls_back_to_text_signature(monkeypatch):
    """表外异常（is_platform_bug 判不出）→ 文本签名 fallback（不塌成 tool）。"""
    from hiveweave.services import failure_signature as fs

    svc, _store = _fake_memory_service()
    monkeypatch.setattr(
        "hiveweave.services.memory.MemoryService", MagicMock(return_value=svc)
    )
    monkeypatch.setattr(
        fs, "_project_id_of_agent", AsyncMock(return_value="proj")
    )
    rec = await fs.record_exception_failure_signature(
        agent_id="agent-A", tool_name="bash",
        exc=RuntimeError("some distinct runtime wording that is long enough"),
        fact_flags={"exception_type": "RuntimeError"},  # 无 is_platform_bug
    )
    assert rec["written"] is True
    # 结构化签名为 None ⇒ 回落 signature_of 的文本归一化结果。
    assert rec["sig"] == fs.signature_of(
        "RuntimeError: some distinct runtime wording that is long enough"
    )


@pytest.mark.asyncio
async def test_recent_failure_memories_hint_injection(seeded_project, monkeypatch):
    """读侧自动注入：最近失败的工具命中共享签名条目 ⇒ 注入含解法文案。"""
    from hiveweave.services import failure_signature as fs
    from hiveweave.services.memory import MemoryService
    from hiveweave.services.run_ledger import RunLedger

    await _seed_run(seeded_project, "bd-run-hint", status="completed")
    ledger = RunLedger()
    step = await ledger.record_step_start(
        AGENT_ID, "bd-run-hint", 0, "tool_call",
        tool_name="bash", tool_call_id="call-h",
    )
    await ledger.record_step_end(
        AGENT_ID, step, status="failed", error="boom",
    )

    sig_entry = {
        "type": "failure_signature",
        "content": (
            "[失败签名] tool=bash | unix-only pipeline rejected\n"
            "根因提示: 方言不兼容\n"
            "已验证解法: 改写为 pwsh 的 Select-Object 写法"
        ),
        "metadata": {
            "signature": "unix-only pipeline rejected",
            "tool_name": "bash",
            "solution_status": "verified",
        },
        "updated_at": int(time.time() * 1000),
    }

    async def _fake_get(self, pid):
        return [sig_entry]

    monkeypatch.setattr(MemoryService, "get_project_memories", _fake_get)

    hint = await fs.recent_failure_memories_hint(AGENT_ID, PROJECT_ID)
    assert "[团队失败记忆]" in hint
    assert "tool=bash" in hint
    assert "Select-Object" in hint  # 解法原文随注入


@pytest.mark.asyncio
async def test_recent_failure_memories_hint_skips_mirror_entries(
    seeded_project, monkeypatch,
):
    """纯镜子条目（无解法）不注入（P2-1 空态噪声同判据）；无最近失败
    ⇒ 不注入。"""
    from hiveweave.services import failure_signature as fs
    from hiveweave.services.memory import MemoryService
    from hiveweave.services.run_ledger import RunLedger

    await _seed_run(seeded_project, "bd-run-hint2", status="completed")
    ledger = RunLedger()
    step = await ledger.record_step_start(
        AGENT_ID, "bd-run-hint2", 0, "tool_call",
        tool_name="bash", tool_call_id="call-h2",
    )
    await ledger.record_step_end(AGENT_ID, step, status="failed", error="boom")

    mirror = {
        "type": "failure_signature",
        "content": "[失败签名] tool=bash | some error text only\n根因提示: 见错误原文",
        "metadata": {"signature": "some error text only", "tool_name": "bash"},
        "updated_at": int(time.time() * 1000),
    }

    async def _fake_get(self, pid):
        return [mirror]

    monkeypatch.setattr(MemoryService, "get_project_memories", _fake_get)
    hint = await fs.recent_failure_memories_hint(AGENT_ID, PROJECT_ID)
    assert hint == ""

    # 无最近失败（另一 agent id 无 run_steps）⇒ 不注入。
    hint2 = await fs.recent_failure_memories_hint("no-such-agent", PROJECT_ID)
    assert hint2 == ""


# ── schema 自检：新列在正典迁移里（旧库打开自动补列的静态保证）───────


def test_schema_migrations_register_new_columns():
    from hiveweave.db import schema

    stmts = "\n".join(schema.PROJECT_DB_TABLES)
    for col in (
        "exception_type", "is_platform_bug", "exit_code",
        "output_empty", "truncated",
    ):
        assert f"ADD COLUMN {col}" in stmts, col
    assert "ADD COLUMN timeout_layer" in stmts
    assert "stall_reason TEXT" in stmts  # agent_runs 正典 DDL
    # 启动自检（fail-loud 防迁移断裂）：
    assert "exception_type" in schema.PROJECT_DB_COLUMN_CHECKS["run_steps"]
    assert "truncated" in schema.PROJECT_DB_COLUMN_CHECKS["run_steps"]
    assert "timeout_layer" in schema.PROJECT_DB_COLUMN_CHECKS["llm_usage"]


def test_new_columns_added_to_legacy_db():
    """旧库（无新列）经迁移补列后 record_step_end 不再 no such column。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE run_steps (id TEXT PRIMARY KEY, run_id TEXT, "
        "step_index INTEGER, step_type TEXT, status TEXT, started_at INTEGER)"
    )
    from hiveweave.db import schema

    for sql in schema.PROJECT_DB_TABLES:
        s = sql.strip().upper()
        if s.startswith("ALTER TABLE RUN_STEPS"):
            conn.execute(sql)
    conn.execute(
        "INSERT INTO run_steps (id, run_id, step_index, step_type, status, "
        "started_at) VALUES ('s1', 'r1', 0, 'tool_call', 'running', 1)"
    )
    conn.execute(
        "UPDATE run_steps SET exception_type = ?, is_platform_bug = ?, "
        "exit_code = ?, output_empty = ?, truncated = ? WHERE id = 's1'",
        ["AttributeError", 1, 0, 1, 0],
    )
    row = conn.execute(
        "SELECT exception_type, is_platform_bug, exit_code, output_empty, "
        "truncated FROM run_steps WHERE id = 's1'"
    ).fetchone()
    assert row == ("AttributeError", 1, 0, 1, 0)
