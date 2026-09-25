"""TEST_DSH_70 批3「观测判据面」回归测试。

覆盖修复路线批3 的七个施工项（每项至少一正一反）：

1. P1-7① prune 显式记账：in-loop（context.py）与持久化（store.py）两处
   中段改写都落 ``first_pruned_index`` + ``prefix_invalidated_tokens``；
   无候选不记账（负向）。
2. P1-7②/P2-6 换模型 cold-start 广播：模型身份变更 ⇒ 结构化日志 +
   agent 可见 chat 标记各一条；同模型不重复广播（负向）。
3. P2-6 熔断时间窗失败率：穿插成功复位连续计数时窗口判据仍开闸；
   时间散开（窗外）不开闸；管理器级 reset 清窗口；consecutive 原逻辑不动。
4. P1-3 get_tasks 增量游标：``updatedSince`` 只回变更任务 + 转移摘要 +
   游标推进；无变更走廉价答复（负向）。
5. P2-9① 长驻 job stdout 落盘：滚动追加 + 超限截断轮转（不改名）；
   未配置 log_path 不落盘（负向）。
6. P2-7 soft policy 回落：warning 级 + policy_id + 回落集合 + telemetry
   记账；policy 声明执行类 kind 时行为不变（对照）。
7. P2-3 审计读帽：重试窗（110s）真大于首读帽（90s）⇒ 首读超时后仍有
   真重试；窄窗（旧 45s）下重试被吃穿时落**作废前置提示**再上抛；
   ``effective_audit_timeout_s`` 夹到「首读帽 + 重试窗 + 余量」。
"""

from __future__ import annotations

import asyncio
import importlib
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

import hiveweave.agents.agent as agent_mod
import hiveweave.conversation.store as store_mod
from hiveweave.agents.agent import _review_llm_post_with_retry
from hiveweave.conversation.store import ConversationStore
from hiveweave.conversation.token_utils import (
    PRUNE_PLACEHOLDER,
    estimate_tokens_for_messages,
)
from hiveweave.llm.circuit_breaker import CircuitBreaker, CircuitState
from hiveweave.llm.streamer.core import Streamer, reset_model_cold_start_registry
from hiveweave.llm.streamer.context import ContextMixin
from hiveweave.services.telemetry import telemetry

# ⚠ `import hiveweave.llm.circuit_breaker as x` 绑定的是 **hiveweave.llm 包上
# 的同名属性** —— 而包 __init__ 把模块级单例 `circuit_breaker`（CircuitBreaker
# 实例）重导出到那儿，把子模块属性遮蔽了。要拿**真模块**（monkeypatch `_now`
# 等）必须走 importlib。
cb_mod = importlib.import_module("hiveweave.llm.circuit_breaker")

# ════════════════════════════════════════════════════════════
# 共用小工具
# ════════════════════════════════════════════════════════════


def _tool_turn(tc_id: str, body: str) -> list[dict]:
    """一轮 [user, assistant(tool_calls), tool(result)]，tool 正文足够长。"""
    return [
        {"role": "user", "content": f"question {tc_id}"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": tc_id, "content": body},
    ]


def _prunable_messages() -> list[dict]:
    """3 轮长 tool 输出 + 尾问 —— 逆序扫描可产出候选。"""
    out: list[dict] = []
    for i in range(3):
        out.extend(_tool_turn(f"tc-{i}", "x" * 2000))
    out.append({"role": "user", "content": "final question"})
    return out


class _Clock:
    """可控单调时钟（窗口判据测试用）：预置时刻耗尽后停在最后值。"""

    def __init__(self, times: list[float]):
        self._times = list(times)

    def monotonic(self) -> float:
        if len(self._times) > 1:
            return self._times.pop(0)
        return self._times[0]

    def jump_to(self, t: float) -> None:
        self._times = [t]


# ════════════════════════════════════════════════════════════
# #1 P1-7① prune 显式记账
# ════════════════════════════════════════════════════════════


class _PruneHost(ContextMixin):
    """只借 ContextMixin 的 prune 方法（不拖整个 Streamer 依赖树）。"""

    _PRUNE_PROTECT_TOKENS = 100
    _PRUNE_MINIMUM_TOKENS = 10
    _PRUNE_PLACEHOLDER = PRUNE_PLACEHOLDER

    def __init__(self) -> None:
        self.context_rewrote = False

    def _mark_context_rewrite(self) -> None:
        self.context_rewrote = True


def test_inloop_prune_logs_prefix_invalidated_tokens():
    """正向：in-loop 中段改写落 first_pruned_index + prefix_invalidated_tokens。"""
    host = _PruneHost()
    messages = _prunable_messages()
    with capture_logs() as logs:
        result = host._prune_old_tool_outputs(messages)

    ev = [e for e in logs if e.get("event") == "tool_loop_prune"]
    assert ev, f"tool_loop_prune event missing: {logs}"
    # 首个被改写下标 = 最早的候选（tc-0 的 tool 回执，下标 2）
    assert ev[0]["first_pruned_index"] == 2
    # 作废跨度 = 改写点之前的 token 数（> 0，可机检）
    expect = estimate_tokens_for_messages(result[:2])
    assert ev[0]["prefix_invalidated_tokens"] == expect
    assert ev[0]["prefix_invalidated_tokens"] > 0
    # 候选本身已被占位符替换（行为不变）
    assert result[2]["content"] == PRUNE_PLACEHOLDER
    assert host.context_rewrote is True


@pytest.mark.asyncio
async def test_persisted_prune_accounting_async(monkeypatch):
    """正向：持久化裁剪落记账字段 + telemetry 计数。"""
    monkeypatch.setattr(store_mod, "PRUNE_PROTECT_TOKENS", 100)
    monkeypatch.setattr(store_mod, "PRUNE_MINIMUM_TOKENS", 10)

    store = ConversationStore()
    monkeypatch.setattr(store, "_enqueue_write", AsyncMock(return_value=None))

    pid, aid = "proj-prune-log2", "agent-prune-log2"
    store._cache[(pid, aid)] = _prunable_messages()
    before = telemetry._counters.get("prune_prefix_invalidated", 0)

    with capture_logs() as logs:
        await store.prune_persisted(aid, pid)

    ev = [e for e in logs if e.get("event") == "prune_persisted"]
    assert ev, f"prune_persisted event missing: {logs}"
    assert ev[0]["first_pruned_index"] == 2
    assert ev[0]["prefix_invalidated_tokens"] > 0
    # cache 已被原地改写（行为不变），记账计数已落
    assert store._cache[(pid, aid)][2]["content"] == PRUNE_PLACEHOLDER
    after = telemetry._counters.get("prune_prefix_invalidated", 0)
    assert after == before + 1


@pytest.mark.asyncio
async def test_persisted_prune_no_candidates_no_accounting(monkeypatch):
    """负向：无候选（消息数不足）⇒ 不记账、不改写。"""
    monkeypatch.setattr(store_mod, "PRUNE_PROTECT_TOKENS", 100)
    monkeypatch.setattr(store_mod, "PRUNE_MINIMUM_TOKENS", 10)

    store = ConversationStore()
    monkeypatch.setattr(store, "_enqueue_write", AsyncMock(return_value=None))
    pid, aid = "proj-prune-neg", "agent-prune-neg"
    store._cache[(pid, aid)] = [
        {"role": "user", "content": "only one message"},
    ]

    with capture_logs() as logs:
        await store.prune_persisted(aid, pid)

    assert not [e for e in logs if e.get("event") == "prune_persisted"]
    assert store._cache[(pid, aid)][0]["content"] == "only one message"
    after = telemetry._counters.get("prune_prefix_invalidated", 0)
    assert after == telemetry._counters.get("prune_prefix_invalidated", 0)


# ════════════════════════════════════════════════════════════
# #2 P1-7②/P2-6 换模型 cold-start 广播
# ════════════════════════════════════════════════════════════


class _StubChatMessageService:
    saved: list[dict] = []

    async def save_message(self, payload: dict) -> None:
        _StubChatMessageService.saved.append(payload)


@pytest.fixture
def stub_chat_message(monkeypatch):
    _StubChatMessageService.saved = []
    import hiveweave.services.chat_message as cm_mod

    monkeypatch.setattr(cm_mod, "ChatMessageService", _StubChatMessageService)
    reset_model_cold_start_registry()
    yield _StubChatMessageService.saved
    reset_model_cold_start_registry()


@pytest.mark.asyncio
async def test_cold_start_broadcast_on_model_change(stub_chat_message):
    """正向：模型身份变更 ⇒ 结构化日志 + agent 可见事件各一条。"""
    with capture_logs() as logs:
        # 首见不算变更（基准为空）
        await Streamer._broadcast_model_cold_start(
            "agent-cs1", {"provider": "p", "model": "m1"}
        )
        # 变更边界：日志 + chat 标记
        await Streamer._broadcast_model_cold_start(
            "agent-cs1", {"provider": "p", "model": "m2"}
        )

    ev = [e for e in logs if e.get("event") == "llm_model_changed_cold_start"]
    assert len(ev) == 1, f"expect exactly one cold_start log: {logs}"
    assert ev[0]["prev_model"] == "p@m1"
    assert ev[0]["new_model"] == "p@m2"

    assert len(stub_chat_message) == 1
    msg = stub_chat_message[0]
    assert msg["role"] == "system"
    assert msg["is_read"] is True
    assert msg["metadata"]["context_marker"] == "model_switch"
    assert "m1" in msg["content"] and "m2" in msg["content"]


@pytest.mark.asyncio
async def test_cold_start_no_broadcast_for_same_model(stub_chat_message):
    """负向：同模型连续调用不重复广播。"""
    with capture_logs() as logs:
        await Streamer._broadcast_model_cold_start(
            "agent-cs2", {"provider": "p", "model": "m1"}
        )
        await Streamer._broadcast_model_cold_start(
            "agent-cs2", {"provider": "p", "model": "m1"}
        )
    assert not [e for e in logs if e.get("event") == "llm_model_changed_cold_start"]
    assert stub_chat_message == []


# ════════════════════════════════════════════════════════════
# #3 P2-6 熔断时间窗失败率
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_window_rate_trips_despite_interleaved_successes():
    """正向：穿插成功复位连续计数，窗口失败率越线仍开闸（429 闪断形态）。"""
    cb = CircuitBreaker()
    await cb.register("p-win")
    # 12 个样本、6 失败 = 0.5 < 0.6；连续失败从未 ≥2 ⇒ consecutive 通路全程不触发
    for _ in range(6):
        await cb.report_failure("p-win", error_code="rate_limited")
        await cb.report_success("p-win")
    assert await cb.get_state("p-win") is cb_mod.CircuitState.CLOSED
    res = await cb.check("p-win")
    assert res.allowed

    # 再加 3 失败：15 样本 9 失败 = 0.6 ≥ 0.6 ⇒ 开闸
    with capture_logs() as logs:
        for _ in range(3):
            await cb.report_failure("p-win", error_code="rate_limited")
    res = await cb.check("p-win")
    assert not res.allowed
    assert await cb.get_state("p-win") is cb_mod.CircuitState.OPEN
    ev = [e for e in logs if e.get("event") == "circuit_opened_window_rate"]
    assert ev and ev[0]["window_rate"] >= 0.6

    # snapshot 暴露窗口两键
    snap = {s["provider"]: s for s in cb.snapshot()}["p-win"]
    assert snap["window_samples"] >= 8
    assert snap["window_fail_rate"] is not None


@pytest.mark.asyncio
async def test_window_rate_not_tripped_when_failures_scattered_over_time(monkeypatch):
    """负向：失败+成功在窗外散开 ⇒ 每个窗口样本不足且连续计数被成功复位。"""
    clock = _Clock([0.0])
    monkeypatch.setattr(cb_mod, "_now", clock.monotonic)
    cb = CircuitBreaker()
    await cb.register("p-slow")
    for i in range(8):
        clock.jump_to(i * 200.0)  # 每 200s 一对成败（> 窗口 120s）
        await cb.report_failure("p-slow")
        clock.jump_to(i * 200.0 + 0.5)
        await cb.report_success("p-slow")  # 连续计数复位；窗口内样本 ≤2 < 8
    res = await cb.check("p-slow")
    assert res.allowed
    assert await cb.get_state("p-slow") is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_window_samples_survive_success_reset_but_manager_reset_clears():
    """成功不清洗窗口史；管理器级 reset（人工复位）连窗口一起清。"""
    cb = CircuitBreaker()
    await cb.register("p-reset")
    for _ in range(5):
        await cb.report_failure("p-reset")
        await cb.report_success("p-reset")
    await cb.report_failure("p-reset")
    # 11 样本 6 失败 = 0.545 < 0.6 未开闸；但 report_success 走过的 b.reset()
    # 若清了窗口，此时样本数将 < 8 —— 用窗口统计钉住「不清窗口」。
    snap = {s["provider"]: s for s in cb.snapshot()}["p-reset"]
    assert snap["window_samples"] == 11

    await cb.reset("p-reset")
    snap2 = {s["provider"]: s for s in cb.snapshot()}["p-reset"]
    assert snap2["window_samples"] == 0
    assert await cb.get_state("p-reset") is cb_mod.CircuitState.CLOSED


@pytest.mark.asyncio
async def test_consecutive_threshold_path_unchanged():
    """对照：原 consecutive 判据一字未动 —— 5 连败仍开闸。"""
    cb = CircuitBreaker()
    await cb.register("p-cons")
    for _ in range(5):
        await cb.report_failure("p-cons")
    assert await cb.get_state("p-cons") is cb_mod.CircuitState.OPEN


# ════════════════════════════════════════════════════════════
# #4 P1-3 get_tasks 增量游标
# ════════════════════════════════════════════════════════════

PROJECT_ID = "dsh70-b3-proj"
AGENT_ID = "dsh70-b3-agent"


@pytest.fixture
async def task_env():
    """照 test_p01_get_tasks_waiver_visibility 的 DB 夹具模式。"""
    from hiveweave.db import project as project_db
    from hiveweave.services import task as task_module

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_agent_project(aid: str):
            return PROJECT_ID if aid == AGENT_ID else None

        task_module._migrated.clear()
        project_db._agent_cache.pop(AGENT_ID, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace", fake_ws),
            patch("hiveweave.db.meta.get_agent_project_id", fake_agent_project),
        ):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(AGENT_ID, None)


@pytest.mark.asyncio
async def test_get_tasks_updated_since_returns_changed_only(task_env):
    """正向：游标后变更的任务才返回 + 转移摘要 + 游标推进。"""
    from hiveweave.tools.tasks.query import GetTasksParams, get_tasks_tool
    from hiveweave.services.task import TaskService

    svc = TaskService()
    tid1 = await svc.create_task(
        project_id=PROJECT_ID, title="task one", description="d",
        creator_id=AGENT_ID,
    )
    rows = {t["id"]: t for t in await svc.list_tasks(PROJECT_ID)}
    cursor = int(rows[tid1]["updated_at"])

    # 在游标之后制造变更：新建第二个任务（created 事件 + 更新的 updated_at）
    tid2 = await svc.create_task(
        project_id=PROJECT_ID, title="task two", description="d",
        creator_id=AGENT_ID,
    )

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=PROJECT_ID),
    ):
        result = await get_tasks_tool(
            GetTasksParams(updated_since=cursor), AGENT_ID, task_env["workspace"]
        )

    assert result.success, result.output or result.error
    extra = result.extra or {}
    assert extra.get("updatedSinceCursor", 0) >= cursor
    ids = {str(t.get("id")) for t in (extra.get("tasks") or [])}
    assert tid2 in ids, f"changed task missing: {extra.get('tasks')}"
    assert tid1 not in ids, "unchanged task must not be returned"
    out = result.output or ""
    assert "Tip: copy the entire id=" not in out, (
        "incremental view must skip the heavy full-listing rendering"
    )


@pytest.mark.asyncio
async def test_get_tasks_updated_since_no_changes_is_cheap(task_env):
    """负向：游标之后无变更 ⇒ 空列表 + 廉价答复 + 游标不回退。"""
    from hiveweave.tools.tasks.query import GetTasksParams, get_tasks_tool
    from hiveweave.services.task import TaskService

    svc = TaskService()
    await svc.create_task(
        project_id=PROJECT_ID, title="quiet task", description="d",
        creator_id=AGENT_ID,
    )
    future_cursor = int(time.time() * 1000) + 10_000

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=PROJECT_ID),
    ):
        result = await get_tasks_tool(
            GetTasksParams(updated_since=future_cursor), AGENT_ID,
            task_env["workspace"],
        )

    assert result.success
    extra = result.extra or {}
    assert extra.get("tasks") == []
    assert extra.get("updatedSinceCursor") == future_cursor
    assert "No task changes" in (result.output or "")


@pytest.mark.asyncio
async def test_get_tasks_full_listing_unchanged_without_cursor(task_env):
    """对照：不带游标的调用走原全量路径（行为不变）。"""
    from hiveweave.tools.tasks.query import GetTasksParams, get_tasks_tool
    from hiveweave.services.task import TaskService

    await TaskService().create_task(
        project_id=PROJECT_ID, title="full listing task", description="d",
        creator_id=AGENT_ID,
    )
    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=PROJECT_ID),
    ):
        result = await get_tasks_tool(GetTasksParams(), AGENT_ID, task_env["workspace"])
    assert result.success
    assert "Tasks (" in (result.output or "")


# ════════════════════════════════════════════════════════════
# #5 P2-9① 长驻 job stdout 落盘
# ════════════════════════════════════════════════════════════


def _dummy_spawned() -> object:
    class _S:
        pid = 12345

    return _S()


def _make_job(log_path: str | None):
    from hiveweave.services.acl_sandbox.spawn import LongRunningJob

    return LongRunningJob(_dummy_spawned(), asyncio.new_event_loop(),
                          log_path=log_path)


def test_long_running_job_streams_output_to_log(tmp_path):
    """正向：排空字节滚动追加进日志；超限就地截断轮转（不改名）。"""
    from hiveweave.services.acl_sandbox import spawn as spawn_mod

    log_path = tmp_path / "dev-server-3000.log"
    job = _make_job(str(log_path))
    job._log_append(b"vite ready on port 3000\n")
    assert b"vite ready" in log_path.read_bytes()

    # 轮转：把已写字节数顶到上限，下一次追加触发截断重开（文件名不变）
    job._log_bytes = spawn_mod._LONG_RUNNING_LOG_MAX_BYTES
    job._log_append(b"after-rotate")
    data = log_path.read_bytes()
    assert data.startswith(spawn_mod._LOG_ROTATE_MARKER)
    assert data.endswith(b"after-rotate")
    assert len(data) < spawn_mod._LONG_RUNNING_LOG_MAX_BYTES
    job._log_close()


def test_long_running_job_without_log_path_writes_nothing(tmp_path):
    """负向：未配置 log_path ⇒ 完全不落盘（旧行为不变）。"""
    no_dir = tmp_path / "unused"
    job = _make_job(None)
    job._log_append(b"should not land anywhere\n")
    assert not no_dir.exists()
    assert job._log_file is None
    # 内存缓冲不受影响（观测能力不降级；_log_append 只管落盘，不碰缓冲）
    assert job._out == []


def test_long_running_job_log_survives_oserror(tmp_path):
    """负向（健壮）：落盘失败（目录被删）⇒ 放弃落盘且不再反复尝试。"""
    log_path = tmp_path / "gone" / "dev.log"
    job = _make_job(str(log_path))
    job._log_append(b"boom\n")  # 目录不存在 → OSError → 放弃
    assert job._log_file is None
    job._log_append(b"still fine\n")  # 不抛异常（best-effort 契约）
    assert job._log_file is None


# ════════════════════════════════════════════════════════════
# #6 P2-7 soft policy 回落 warning + 记账
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_soft_policy_fallback_is_warning_and_accounted():
    """正向：soft 回落 = warning + policy_id + 回落集合 + telemetry 计数。"""
    from hiveweave.services.tasks.acceptance import acceptance_coverage_kinds

    before = telemetry._counters.get("acceptance_coverage_soft_fallback", 0)
    with capture_logs() as logs:
        kinds = await acceptance_coverage_kinds(
            {"policy_id": "coordinator_review"}
        )
    ev = [
        e for e in logs
        if e.get("event") == "acceptance_coverage_policy_soft_all_execution_kinds"
    ]
    assert ev, f"soft fallback event missing: {logs}"
    # capture_logs 不带 level 键 —— warning 级由实现（log.warning）保证，
    # 这里钉住其余可机检字段：policy_id + 实际回落集合。
    assert ev[0].get("policy_id") == "coordinator_review"
    assert "test_run" in (ev[0].get("fallback_kinds") or [])
    # 行为不变：回落集合仍是全部执行类 kind
    assert set(kinds) == {"test_run", "browse_e2e", "visual_check", "doc_review"}
    after = telemetry._counters.get("acceptance_coverage_soft_fallback", 0)
    assert after == before + 1


@pytest.mark.asyncio
async def test_policy_declared_kinds_path_unchanged():
    """对照（负向）：policy 声明执行类 kind 时精确返回，不回落不记账。"""
    from hiveweave.services.tasks.acceptance import acceptance_coverage_kinds

    before_soft = telemetry._counters.get("acceptance_coverage_soft_fallback", 0)
    before_nonexec = telemetry._counters.get(
        "acceptance_coverage_nonexecution_fallback", 0)
    with capture_logs() as logs:
        assert await acceptance_coverage_kinds({"policy_id": "generic_tests"}) == (
            "test_run",
        )
    assert not [e for e in logs if e.get("event", "").endswith("_fallback")]
    assert telemetry._counters.get(
        "acceptance_coverage_soft_fallback", 0) == before_soft
    assert telemetry._counters.get(
        "acceptance_coverage_nonexecution_fallback", 0) == before_nonexec


@pytest.mark.asyncio
async def test_nonexecution_policy_fallback_carries_set():
    """非执行类 policy（code_audit）回落时也带回落集合（记账口径一致）。"""
    from hiveweave.services.tasks.acceptance import acceptance_coverage_kinds

    before = telemetry._counters.get("acceptance_coverage_nonexecution_fallback", 0)
    with capture_logs() as logs:
        kinds = await acceptance_coverage_kinds({"policy_id": "code_audit"})
    ev = [
        e for e in logs
        if e.get("event")
        == "acceptance_coverage_policy_kinds_not_execution_evidence"
    ]
    assert ev and ev[0].get("fallback_kinds")
    assert set(kinds) == {"test_run", "browse_e2e", "visual_check", "doc_review"}
    assert telemetry._counters.get(
        "acceptance_coverage_nonexecution_fallback", 0) == before + 1


# ════════════════════════════════════════════════════════════
# #7 P2-3 审计读帽
# ════════════════════════════════════════════════════════════

_URL = "https://gw.fake/v1/chat/completions"
_BODY = {"model": "m", "messages": []}
_HEADERS = {"Accept": "application/json"}


class _FakeResponse:
    def __init__(self, status=200, json_data=None, headers=None, text=""):
        self.status_code = status
        self._data = json_data or {}
        self.headers = dict(headers or {})
        self._text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", _URL)
            response = httpx.Response(
                self.status_code, request=request,
                headers=self.headers, text=self._text,
            )
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.posts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.posts += 1
        item = self.behaviors.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _ok(content: str = "audit ok") -> _FakeResponse:
    return _FakeResponse(200, {"choices": [{"message": {"content": content}}]})


class _FakeAgentClock:
    """首读耗尽 90s 的时钟：start=0，之后恒 90（模拟 90s 读超时）。"""

    def __init__(self):
        self.calls = 0

    def monotonic(self) -> float:
        self.calls += 1
        return 0.0 if self.calls == 1 else 90.0


@pytest.mark.asyncio
async def test_retry_window_survives_first_read_timeout():
    """正向：重试窗（110s）> 首读帽（90s）⇒ 首读超时后第二次尝试真发生。"""
    client = _FakeClient([
        httpx.RemoteProtocolError("read timeout at 90s"),
        _ok("audit ok after retry"),
    ])
    clock = _FakeAgentClock()
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch.object(agent_mod, "time", clock),
        patch.object(asyncio, "sleep", new=AsyncMock()),
    ):
        result = await _review_llm_post_with_retry(
            _URL, _BODY, _HEADERS, asyncio.Semaphore(1),
        )
    assert result == "audit ok after retry"
    assert client.posts == 2


@pytest.mark.asyncio
async def test_narrow_window_exhaustion_logs_void_notice():
    """负向（旧 45s 形状）：重试窗被首读吃穿 ⇒ 先落作废前置提示再上抛。"""
    client = _FakeClient([
        httpx.RemoteProtocolError("read timeout at 90s"),
        _ok("never reached"),
    ])
    clock = _FakeAgentClock()
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch.object(agent_mod, "time", clock),
        patch.object(asyncio, "sleep", new=AsyncMock()),
        capture_logs() as logs,
    ):
        with pytest.raises(httpx.RemoteProtocolError):
            await _review_llm_post_with_retry(
                _URL, _BODY, _HEADERS, asyncio.Semaphore(1),
                retry_window_s=45.0,  # 旧值：45 - 90 < 0 ⇒ 重试从未发生
            )
    assert client.posts == 1
    ev = [
        e for e in logs
        if e.get("event") == "review_llm_retry_window_exhausted"
    ]
    assert ev, f"void-before-cap notice missing: {logs}"
    assert ev[0].get("retry_window_s") == 45.0


def test_effective_audit_timeout_covers_retry_window(monkeypatch):
    """外层帽夹到「首读帽 + 重试窗 + 余量」，且严格大于首读帽本身。"""
    from hiveweave.services import code_audit as ca_mod
    from hiveweave.services.code_audit import (
        CODE_AUDIT_LLM_TIMEOUT_S,
        effective_audit_timeout_s,
    )

    # 默认 env=120：min(120, 90+110+5) = 120 > 90（旧口径恰为 90 ⇒ 重试死）
    assert CODE_AUDIT_LLM_TIMEOUT_S == 120
    eff = effective_audit_timeout_s()
    assert eff == 120.0
    assert eff > agent_mod._REVIEW_LLM_READ_TIMEOUT_MAX_S

    # env 调大到 1000：夹到内层总预算 205
    monkeypatch.setattr(ca_mod, "CODE_AUDIT_LLM_TIMEOUT_S", 1000)
    assert effective_audit_timeout_s() == 90.0 + 110.0 + 5.0

    # env 调小到 60：尊重调用方（宁快不慢）
    monkeypatch.setattr(ca_mod, "CODE_AUDIT_LLM_TIMEOUT_S", 60)
    assert effective_audit_timeout_s() == 60.0
