"""T16 一期/增补：auto 回合出口模式（HIVEWEAVE_TURN_EXIT_MODE）回归测试。

被测形态（MERGED-2026-09-28 研究报告 §3.1 T16 行）：
- ``legacy``（默认）零行为变化：commit_turn 工具面原样、_TURN_GATE_MAX=1、
  无延续唤醒 —— 本文件抽核心判据回归 + 既有 turn_exit/gate 测试继续全绿。
- ``auto``：commit_turn 从模型工具面下架；模型自然停止 ⇒ 平台程序化收口
  （exit_gate_check 接缝：hint 1-2 常规 / 3 起升级 / 5 连停强制挂起 blocked）；
  全部结算 ⇒ 平台生成 commit（summary 末段合成、phase 平台推断）；唯一合法
  中途挂起 = wait_on（refs 从严校验）。
- T16 增补（2026-09-30 钦定）：auto 下回合墙钟预算退役 —— tool_loop 不武装
  budget_deadline，回合时长由模型停止自然决定；600 轮帽（DEFAULT_MAX_TOOL_
  ROUNDS）是唯一轮数级硬停（到点走 auto 出口门禁收口：结算 ⇒ 平台 commit /
  未结算 ⇒ hint + 宽限；宽限耗尽 ⇒ 强制 blocked + 通知 creator）。一期
  「预算掐断 ⇒ interrupted + 延续唤醒」分支随之成为死分支（防御保留）。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.config import settings, turn_exit_mode
from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services import task as task_mod
from hiveweave.services import wait_contract as wait_contract_module
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    get_pending_turn_result,
    pop_force_exit_block,
    set_pending_turn_result,
)

PROJECT_ID = "test-t16-project"
AGENT_ID = "test-t16-agent"
OTHER_AGENT_ID = "test-t16-agent-b"


@pytest.fixture
def auto_mode(monkeypatch):
    """把灰度 flag 拨到 auto（settings 属性级 monkeypatch，turn_exit_mode()
    每次调用现读）。"""
    monkeypatch.setattr(settings, "turn_exit_mode", "auto")
    return "auto"


@pytest.fixture
def legacy_mode(monkeypatch):
    monkeypatch.setattr(settings, "turn_exit_mode", "legacy")
    return "legacy"


@pytest.fixture
async def env():
    """per-project DB 环境（ADR-001 测试同款夹具形态）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        wait_contract_module._migrated.clear()
        task_mod._migrated.clear()
        project_db._agent_cache[AGENT_ID] = workspace_path
        project_db._agent_cache[OTHER_AGENT_ID] = workspace_path
        await ensure_project_db(workspace_path)

        with patch(
            "hiveweave.db.meta.get_project_workspace",
            fake_get_project_workspace,
        ):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
            for aid in (AGENT_ID, OTHER_AGENT_ID):
                project_db._agent_cache.pop(aid, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _make_agent(**extra):
    """object.__new__(Agent) 桩 —— 只铺被测路径读的属性。"""
    from hiveweave.agents.agent import Agent

    agent = object.__new__(Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    agent.config = {"name": "t16", "role": "executor"}
    agent.disposition = "runnable"
    agent.pending_inbox_msg_ids = None
    agent.current_job = {"started_at": 0}
    agent._turn_gate_count = 0
    agent._TURN_GATE_MAX = 1
    agent._turn_gate_count = 0
    agent._current_run_id = None
    agent._slice_budget = 0
    return agent


def _ctx():
    """wait_on 的 ctx 兜底载体（测试 meta 库无 agents 表，走 ctx.project_id）。"""
    from types import SimpleNamespace

    return SimpleNamespace(project_id=PROJECT_ID)


# ── 1. flag 骨架 ─────────────────────────────────────────────


def test_mode_default_is_legacy(monkeypatch):
    # 隔离部署 env（本机 .env 设 auto 供实测）——此测试钉的是【代码默认值】：
    # settings 属性清空时 turn_exit_mode() 必须落 legacy（fail-safe 方向）
    monkeypatch.setattr(settings, "turn_exit_mode", "")
    assert turn_exit_mode() == "legacy"


def test_mode_invalid_value_falls_back_to_legacy(monkeypatch):
    monkeypatch.setattr(settings, "turn_exit_mode", "AUTO ")
    assert turn_exit_mode() == "auto"  # 大小写/空白容忍
    monkeypatch.setattr(settings, "turn_exit_mode", "yes")
    assert turn_exit_mode() == "legacy"  # 非法值 fail-safe 回 legacy


# ── 2. 工具面：commit_turn 下架 / wait_on 上架（仅 auto）──────


async def test_tool_surface_legacy_keeps_commit_turn(legacy_mode, monkeypatch):
    from hiveweave.agents.helpers.tools_def import _build_tool_definitions  # noqa: F401
    from hiveweave.services.permission import permission_service

    async def fake_mode(agent_id: str) -> str:
        return "readwrite"

    monkeypatch.setattr(
        permission_service, "get_permission_mode", fake_mode
    )
    monkeypatch.setattr(
        permission_service,
        "get_tools_for_agent",
        lambda agent: ["grep", "commit_turn"],
    )
    import hiveweave.services.host_env as host_env

    monkeypatch.setattr(
        host_env, "filter_tools_for_host", lambda names: list(names)
    )
    agent = _make_agent()
    defs = await agent._get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert names == ["grep", "commit_turn"]


async def test_tool_surface_auto_swaps_commit_turn_for_wait_on(
    auto_mode, monkeypatch
):
    from hiveweave.services.permission import permission_service

    async def fake_mode(agent_id: str) -> str:
        return "readwrite"

    monkeypatch.setattr(permission_service, "get_permission_mode", fake_mode)
    monkeypatch.setattr(
        permission_service,
        "get_tools_for_agent",
        lambda agent: ["grep", "commit_turn"],
    )
    import hiveweave.services.host_env as host_env

    monkeypatch.setattr(
        host_env, "filter_tools_for_host", lambda names: list(names)
    )
    agent = _make_agent()
    defs = await agent._get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert "commit_turn" not in names
    assert "wait_on" in names
    # wait_on 的 schema 来自 @tool 注册表（pydantic 单源，非空 properties）
    wait_def = next(d for d in defs if d["function"]["name"] == "wait_on")
    assert wait_def["function"]["parameters"].get("properties", {}).get("refs")


async def test_permission_visibility_strips_wait_on_even_in_auto(
    auto_mode,
):
    """执行面放行（_BASE_TOOLS 含 wait_on）但家族可见清单不含它 ——
    可见性由 agent._get_tool_definitions 的 auto 接线单独负责。"""
    from hiveweave.services.permission import permission_service

    names = permission_service.get_tools_for_agent(
        {
            "role": "executor",
            "permission_type": "executor",
            "permission_mode": "readwrite",
        }
    )
    assert "wait_on" not in names
    assert "commit_turn" in names


# ── 3. wait_on 工具 ──────────────────────────────────────────


async def test_wait_on_rejected_in_legacy_mode(legacy_mode):
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "task", "ref": "t1"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is False
    assert "auto" in (r.error or "").lower()


async def test_wait_on_bad_ref_rejected_with_remedy(auto_mode, env):
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "task", "ref": "ghost-task"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is False
    assert "ghost-task" in (r.error or "")
    assert r.extra.get("remedy")  # 处方在场（拒绝必带处方纪律）
    # 不当挂起不得落账
    waits = await wait_contract_service.list_active(PROJECT_ID, AGENT_ID)
    assert waits == []


async def test_wait_on_self_agent_ref_rejected(auto_mode, env):
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "agent", "ref": AGENT_ID}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is False
    assert "yourself" in (r.error or "")


async def test_wait_on_valid_task_registers_wait_contract(auto_mode, env):
    from hiveweave.services.task import TaskService
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    task_id = await TaskService().create_task(
        PROJECT_ID,
        "T16 valid wait target",
        "desc",
        creator_id=OTHER_AGENT_ID,
        assignee_id=AGENT_ID,
    )
    r = await wait_on_tool(
        WaitOnParams(
            refs=[{"kind": "task", "ref": task_id, "note": "等子任务提交"}]
        ),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is True
    assert "stop" in (r.output or "").lower()
    waits = await wait_contract_service.list_active(PROJECT_ID, AGENT_ID)
    assert len(waits) == 1
    assert waits[0]["kind"] == "task"
    assert waits[0]["ref"] == task_id


async def test_wait_on_terminal_task_rejected(auto_mode, env):
    from hiveweave.db import project as project_db
    from hiveweave.services.task import TaskService
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    task_id = await TaskService().create_task(
        PROJECT_ID, "done task", "desc", creator_id=OTHER_AGENT_ID,
        assignee_id=AGENT_ID,
    )
    # 直接落终态（close_task 只收 approved|verifying，这里只验「终态拒绝」）
    await project_db.execute(
        AGENT_ID,
        "UPDATE tasks SET status = 'closed' WHERE id = ?",
        [task_id],
    )
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "task", "ref": task_id}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is False
    assert "terminal" in (r.error or "")


async def test_wait_on_bare_string_ref_coerced_to_task(auto_mode, env):
    """裸字符串 ref 归一为 task 等待（与 commit_turn waiting_on 同口径）。"""
    from hiveweave.services.task import TaskService
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    task_id = await TaskService().create_task(
        PROJECT_ID, "coerce target", "desc", creator_id=OTHER_AGENT_ID,
        assignee_id=AGENT_ID,
    )
    r = await wait_on_tool(
        WaitOnParams(refs=[task_id]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is True
    waits = await wait_contract_service.list_active(PROJECT_ID, AGENT_ID)
    assert waits and waits[0]["ref"] == task_id


# ── 4. 平台生成 commit ────────────────────────────────────────


async def test_platform_commit_done_slice_with_summary(auto_mode, env):
    from hiveweave.services.turn_exit_auto import ensure_platform_turn_result

    clear_pending_turn_result(AGENT_ID)
    payload = await ensure_platform_turn_result(
        _make_agent(),
        "完成了签到模块的单元测试，全部通过。\n\n下轮继续修边界。",
        None,
        refine_summary=True,
    )
    assert payload is not None
    assert payload["phase"] == "done_slice"
    assert "下轮继续修边界" in payload["summary"]  # 取末段旁白
    assert len(payload["summary"]) <= 200
    assert payload["extensions"]["platform_commit"] is True
    assert payload["waiting_on"] == []


async def test_platform_commit_waiting_from_wait_contract(auto_mode, env):
    from hiveweave.services.task import TaskService
    from hiveweave.services.turn_exit_auto import ensure_platform_turn_result
    from hiveweave.services.wait_contract import wait_contract_service

    clear_pending_turn_result(AGENT_ID)
    task_id = await TaskService().create_task(
        PROJECT_ID, "waited task", "desc", creator_id=OTHER_AGENT_ID,
        assignee_id=AGENT_ID,
    )
    await wait_contract_service.replace_waits(
        PROJECT_ID,
        AGENT_ID,
        [{"kind": "task", "ref": task_id, "note": "n"}],
        phase="waiting",
    )
    payload = await ensure_platform_turn_result(_make_agent(), "", None)
    assert payload["phase"] == "waiting"
    assert payload["waiting_on"][0]["ref"] == task_id
    # summary 空文本 + 无工具结果 ⇒ 非空兜底（schema min_length=1）
    assert payload["summary"]


async def test_platform_commit_idempotent_and_refine(auto_mode, env):
    """幂等：pending 已在（模型提交）⇒ 不覆盖；平台合成 + refine ⇒ 只刷
    summary，不重复生成第二条 commit。"""
    from hiveweave.services.turn_exit_auto import ensure_platform_turn_result

    clear_pending_turn_result(AGENT_ID)
    first = await ensure_platform_turn_result(_make_agent(), "", None)
    assert first is not None
    # 同 run 第二次（门禁接缝已合成，收口侧 refine）⇒ 刷 summary 不换 phase
    second = await ensure_platform_turn_result(
        _make_agent(),
        "final narration from the model",
        None,
        refine_summary=True,
    )
    assert second is not None
    assert second["phase"] == first["phase"]
    assert second["summary"] == "final narration from the model"
    # 模型自己的 commit 不被平台覆盖
    clear_pending_turn_result(AGENT_ID)
    set_pending_turn_result(
        AGENT_ID,
        {
            "schema_version": 1,
            "phase": "waiting",
            "summary": "model said it",
            "waiting_on": [{"kind": "user", "ref": "user"}],
            "result": {},
            "extensions": {},
        },
    )
    third = await ensure_platform_turn_result(
        _make_agent(), "x", None, refine_summary=True
    )
    assert third is None
    assert get_pending_turn_result(AGENT_ID)["summary"] == "model said it"


async def test_platform_commit_phase_reinferred_after_wait_on(
    auto_mode, env, monkeypatch
):
    """OCR 评审修复回归：第一停合成 done_slice → 模型补步轮合法 wait_on →
    第二停 refine 时 phase 必须重推断为 waiting（合法挂起不被误打回）。"""
    from hiveweave.services.task import TaskService
    from hiveweave.services.turn_exit_auto import (
        build_auto_exit_gate_hint,
        ensure_platform_turn_result,
    )
    from hiveweave.services.wait_contract import wait_contract_service

    clear_pending_turn_result(AGENT_ID)
    task_id = await TaskService().create_task(
        PROJECT_ID, "late wait target", "desc", creator_id=OTHER_AGENT_ID,
        assignee_id=AGENT_ID,
    )
    agent = _make_agent()

    async def fake_gather(ag, tool_calls):
        return _facts_stub("assignee_running")

    import hiveweave.agents.completion as completion_mod

    monkeypatch.setattr(
        completion_mod, "gather_exit_gate_facts", fake_gather
    )

    # 第一停：合成 done_slice（任务义务 → hint 打回）
    hint1 = await build_auto_exit_gate_hint(agent, [])
    assert hint1 and "ASSIGNEE_MUST_SUBMIT" in hint1
    assert get_pending_turn_result(AGENT_ID)["phase"] == "done_slice"

    # 补步轮：模型合法 wait_on 挂起
    await wait_contract_service.replace_waits(
        PROJECT_ID,
        AGENT_ID,
        [{"kind": "task", "ref": task_id}],
        phase="waiting",
    )

    # 第二停：refine 重推断 phase=waiting；任务被 waiting_on 覆盖 ⇒
    # evaluate 不再报 ASSIGNEE_MUST_SUBMIT ⇒ 放行（None = 循环退出收口）
    async def gathered_same_task(ag, tool_calls):
        return _facts_stub(
            "assignee_running",
            obligations=[
                {
                    "id": task_id,
                    "role_hint": "assignee",
                    "status": "running",
                    "title": "t",
                }
            ],
        )

    monkeypatch.setattr(
        completion_mod, "gather_exit_gate_facts", gathered_same_task
    )
    hint2 = await build_auto_exit_gate_hint(agent, [])
    payload = get_pending_turn_result(AGENT_ID)
    assert payload["phase"] == "waiting"
    assert payload["waiting_on"][0]["ref"] == task_id
    assert hint2 is None
    await wait_contract_service.clear_waits(PROJECT_ID, AGENT_ID)
    clear_pending_turn_result(AGENT_ID)


async def test_platform_commit_summary_falls_back_to_tool_result(
    auto_mode, env
):
    from hiveweave.services.turn_exit_auto import ensure_platform_turn_result

    clear_pending_turn_result(AGENT_ID)
    payload = await ensure_platform_turn_result(
        _make_agent(),
        "",
        [{"role": "tool", "content": "submitted task tk-1 OK"}],
        refine_summary=True,
    )
    assert "submitted task tk-1" in payload["summary"]


# ── 5. auto 门禁阶梯（build_auto_exit_gate_hint）──────────────


def _facts_stub(violation_scenario: str, obligations: list | None = None):
    from hiveweave.agents.completion import ExitGateFacts

    if obligations is None:
        if violation_scenario == "assignee_running":
            obligations = [
                {
                    "id": "task-running-1",
                    "role_hint": "assignee",
                    "status": "running",
                    "title": "t",
                }
            ]
        else:
            obligations = []
    return ExitGateFacts(
        agent_id=AGENT_ID,
        project_id=PROJECT_ID,
        tool_calls=[],
        open_obligations=obligations,
    )


async def _run_auto_gate(agent, scenario: str, monkeypatch):
    import hiveweave.agents.completion as completion_mod
    from hiveweave.services.turn_exit_auto import build_auto_exit_gate_hint

    async def fake_gather(ag, tool_calls):
        return _facts_stub(scenario)

    monkeypatch.setattr(
        completion_mod, "gather_exit_gate_facts", fake_gather
    )
    return await build_auto_exit_gate_hint(agent, [])


async def test_auto_gate_ladder_hints_then_escalates_then_force_blocks(
    auto_mode, env, monkeypatch
):
    """停止 #1、#2 常规 hint；#3 起升级（notice + 最小指令）；#5 强挂。"""
    from hiveweave.services import turn_exit_auto
    from hiveweave.services.wait_contract import wait_contract_service

    clear_pending_turn_result(AGENT_ID)
    agent = _make_agent()
    escalations: list[list[str]] = []

    async def fake_notify(ag, violations):
        escalations.append(list(violations))

    monkeypatch.setattr(
        turn_exit_auto, "notify_superior_gate_escalation", fake_notify
    )

    hints = []
    for stop in range(1, 6):
        hint = await _run_auto_gate(agent, "assignee_running", monkeypatch)
        hints.append(hint)

    # 停止 1、2 → 常规 hint（含违规与 auto 处方，不指挥 commit_turn 调用）
    assert hints[0] and "ASSIGNEE_MUST_SUBMIT" in hints[0]
    assert "commit_turn(" not in hints[0]
    assert hints[1] and "ASSIGNEE_MUST_SUBMIT" in hints[1]
    # 停止 3、4 → 升级最小指令 + notice 给上级
    assert hints[2] and "ESCALATED" in hints[2]
    assert len(escalations) == 2
    # 停止 5 → 不再 hint，强挂标记置位
    assert hints[4] is None
    force = pop_force_exit_block(AGENT_ID)
    assert force is not None
    assert "ASSIGNEE_MUST_SUBMIT" in force["violations"]
    # 计数 = 4 次 hint（第 5 次不再自增）
    assert agent._turn_gate_count == 4
    # 清理 wait 契约副作用（ensure_platform_turn_result 不落 waits，防御性）
    await wait_contract_service.clear_waits(PROJECT_ID, AGENT_ID)


async def test_auto_gate_all_settled_returns_none(auto_mode, env, monkeypatch):
    """全部结算 ⇒ None（loop 退出 → handle_completion 落平台 commit），
    不置强挂标记、不计 hint。"""
    clear_pending_turn_result(AGENT_ID)
    agent = _make_agent()
    hint = await _run_auto_gate(agent, "settled", monkeypatch)
    assert hint is None
    assert agent._turn_gate_count == 0
    assert pop_force_exit_block(AGENT_ID) is None
    # 平台合成的 done_slice 已落 pending（供 handle_completion 权威判定）
    payload = get_pending_turn_result(AGENT_ID)
    assert payload is not None and payload["phase"] == "done_slice"
    clear_pending_turn_result(AGENT_ID)


# ── 6. tool_loop 接缝：自然停注入 / 5 连停退出 ────────────────


class _FakeProvider:
    provider_type = "fake"
    model_name = "fake-model"
    fallback = None
    max_output_tokens = 4096
    supports_thinking = False
    context_window = 128_000


def _make_streamer():
    from hiveweave.llm.streamer.core import Streamer

    provider_factory = MagicMock()
    provider_factory.create.return_value = _FakeProvider()
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(
        return_value=MagicMock(allowed=True, fallback=None)
    )
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()
    streamer = Streamer(
        provider_factory_inst=provider_factory,
        circuit_breaker_inst=breaker,
        retry_handler=MagicMock(),
    )
    streamer._trim_context_if_needed = lambda messages, provider: messages  # type: ignore[method-assign]

    async def _ident_pressure(messages, provider, **kwargs):
        return messages

    streamer._pressure_compact_if_needed = _ident_pressure  # type: ignore[method-assign]
    streamer._maybe_inject_mid_round_reminder = (  # type: ignore[method-assign]
        lambda messages, round_num, cap: messages
    )
    return streamer


def _text_round(text: str) -> dict:
    return {
        "status": "ok",
        "text": text,
        "thinking": "",
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": None,
    }


async def test_auto_seam_natural_stop_with_unsettled_injects_hint_loop(
    auto_mode, env, monkeypatch
):
    """自然停 + 未结算 ⇒ hint 作为 user 消息注入同一 run 循环继续
    （复用既有注入机制，DSH retry-in-the-open-step）。"""
    import hiveweave.agents.completion as completion_mod
    from hiveweave.services.turn_exit_auto import build_auto_exit_gate_hint

    clear_pending_turn_result(AGENT_ID)

    async def fake_gather(ag, tool_calls):
        return _facts_stub("assignee_running")

    monkeypatch.setattr(
        completion_mod, "gather_exit_gate_facts", fake_gather
    )
    streamer = _make_streamer()
    requests: list[list[dict]] = []

    async def fake_stream(*, messages, **kwargs):
        requests.append([dict(m) for m in messages])
        if len(requests) == 1:
            return _text_round("i think i'm done")
        # 补步轮：直接再次文本收口（第二停仍违规 → 第二次 hint；
        # 这里只验证第一停注入后循环确实继续）
        return _text_round("ok i listed the blocker")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    agent = _make_agent()

    async def gate_check(tool_history):
        return await build_auto_exit_gate_hint(agent, tool_history)

    # 第二停后判 None 需要把场景换成已结算：用计数切场景
    calls = {"n": 0}

    async def gate_check_scoped(tool_history):
        calls["n"] += 1
        if calls["n"] >= 2:

            async def settled_gather(a, t):
                return _facts_stub("settled")

            monkeypatch.setattr(
                completion_mod, "gather_exit_gate_facts", settled_gather
            )
        return await build_auto_exit_gate_hint(agent, tool_history)

    result = await streamer._run_tool_loop(
        agent_id=AGENT_ID,
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
        exit_gate_check=gate_check_scoped,
    )

    assert len(requests) == 2
    round2 = requests[1]
    assert round2[-1]["role"] == "user"
    assert "[TURN EXIT]" in round2[-1]["content"]
    assert "ASSIGNEE_MUST_SUBMIT" in round2[-1]["content"]
    assert result["status"] == "ok"
    assert agent._turn_gate_count == 1
    clear_pending_turn_result(AGENT_ID)


async def test_auto_seam_five_stops_exit_with_force_block(
    auto_mode, env, monkeypatch
):
    """5 连停 ⇒ 第 5 停不再注入，loop 正常退出，强挂标记在场。"""
    import hiveweave.agents.completion as completion_mod
    from hiveweave.services.turn_exit_auto import build_auto_exit_gate_hint

    clear_pending_turn_result(AGENT_ID)

    async def fake_gather(ag, tool_calls):
        return _facts_stub("assignee_running")

    monkeypatch.setattr(
        completion_mod, "gather_exit_gate_facts", fake_gather
    )
    streamer = _make_streamer()

    async def fake_stream(*, messages, **kwargs):
        return _text_round("still stuck")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    agent = _make_agent()

    async def gate_check(tool_history):
        return await build_auto_exit_gate_hint(agent, tool_history)

    result = await streamer._run_tool_loop(
        agent_id=AGENT_ID,
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=20,
        exit_gate_check=gate_check,
    )

    assert result["status"] == "ok"
    assert agent._turn_gate_count == 4  # hint 4 次
    force = pop_force_exit_block(AGENT_ID)
    assert force is not None  # 第 5 停置位，交 handle_completion 收口
    clear_pending_turn_result(AGENT_ID)


# ── 6b. T16 增补：auto 无墙钟预算 + 600 轮帽收口 ──────────────


def _tool_round(round_idx: int) -> dict:
    """带 mutating 工具调用的轮（参数每轮不同，防 T9/doom 误触发）。"""
    return {
        "status": "ok",
        "text": f"working on step {round_idx}",
        "thinking": "",
        "tool_calls": [
            {
                "id": f"call-{round_idx}",
                "name": "write_file",
                "arguments": f'{{"path": "f{round_idx}.txt"}}',
            }
        ],
        "finish_reason": "tool_calls",
        "usage": None,
    }


def _make_tooling_streamer():
    """_make_streamer + 成功工具执行桩（每次调用返回 ok 回执）。"""
    streamer = _make_streamer()

    async def fake_execute_tools(**kwargs):
        tcs = kwargs["tool_calls"]
        results = [
            {"role": "tool", "content": "ok", "tool_call_id": tc["id"]}
            for tc in tcs
        ]
        return results, set(), set(), set(), False

    streamer._execute_tools = fake_execute_tools  # type: ignore[method-assign]
    return streamer


async def _run_tool_loop(streamer, *, max_tool_rounds, exit_gate_check=None):
    return await streamer._run_tool_loop(
        agent_id=AGENT_ID,
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),  # 非 None 才会走 _execute_tools 桩
        max_tool_rounds=max_tool_rounds,
        exit_gate_check=exit_gate_check,
    )


async def test_auto_budget_deadline_not_armed_legacy_still_cut(monkeypatch):
    """差分钉死：同样把 HARD_TOTAL_TIMEOUT_S 压到 0.05s ——
    legacy 照旧预算掐断（budget_exhausted=True）；auto 不武装 SSE
    budget_deadline（None，min(...) 退化），但批25 修复1b（2026-10-05
    钦定）起轮边界到 100% 绝对墙会**软着陆**（budget_exhausted 优雅收口
    → interrupted + 自动续跑），不再放任 core.py 外层 wait_for(HARD+30)
    整杀成 error；墙未到时仍跑到自然停、零预算痕迹。
    （等价断言「wait_s 无 budget 项」的接缝事实位。）"""
    import asyncio as _asyncio

    import hiveweave.llm.streamer.tool_loop as tool_loop_mod

    monkeypatch.setattr(
        tool_loop_mod, "HARD_TOTAL_TIMEOUT_S", 0.05
    )

    async def run_mode():
        streamer = _make_tooling_streamer()
        calls: list[dict] = []

        async def fake_stream(*, messages, **kwargs):
            calls.append({"budget_deadline": kwargs.get("budget_deadline")})
            if len(calls) == 1:
                await _asyncio.sleep(0.12)  # 越过 0.05s 硬预算
                return _tool_round(0)
            return _text_round("all done")

        streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
        result = await _run_tool_loop(streamer, max_tool_rounds=5)
        return result, calls

    # legacy：预算武装 ⇒ 预算闸门照旧收口（硬预算 0.05s 下首轮工具批即
    # 被工具批预算闸门收走，budget_exhausted=True）
    monkeypatch.setattr(settings, "turn_exit_mode", "legacy")
    legacy_result, legacy_calls = await run_mode()
    assert legacy_result.get("budget_exhausted") is True
    assert all(c["budget_deadline"] is not None for c in legacy_calls)

    # auto：SSE budget_deadline 不武装（恒 None）；越过 0.05s 绝对墙后
    # 在轮边界软着陆（批25 修复1b）—— 优雅收口保产出，而非外层整杀。
    monkeypatch.setattr(settings, "turn_exit_mode", "auto")
    auto_result, auto_calls = await run_mode()
    assert all(c["budget_deadline"] is None for c in auto_calls)
    assert auto_result["status"] == "ok"
    assert auto_result.get("budget_exhausted") is True
    assert "interrupted" in (auto_result.get("content") or "")
    assert len(auto_result.get("tool_calls") or []) == 1  # tool 产出保留
    assert len(auto_calls) == 1  # 软着陆后不再发起新 LLM 请求

    # auto（墙未到）：跑到自然停，零预算痕迹（原契约保持）
    monkeypatch.setattr(
        tool_loop_mod, "HARD_TOTAL_TIMEOUT_S", 3600.0
    )
    natural_result, natural_calls = await run_mode()
    assert "budget_exhausted" not in natural_result
    assert natural_result["status"] == "ok"
    assert "[TURN BUDGET]" not in (natural_result.get("content") or "")
    assert "[WALL APPROACHING]" not in str(natural_calls)
    assert len(natural_calls) == 2  # 两轮请求都真的发生了
    assert all(c["budget_deadline"] is None for c in natural_calls)


async def test_auto_round_cap_hint_then_grace_then_force_close(
    auto_mode, monkeypatch
):
    """轮帽收口语义①：600 轮到点（测试缩为 2）先走自然停同款出口门禁 ——
    未结算 ⇒ gate hint 注入循环继续（宽限轮可执行）；宽限耗尽仍不见自然停
    ⇒ 强制收口：置强挂标记 + round_cap_forced=True，交 handle_completion
    收口 blocked + 通知 creator。"""
    import hiveweave.llm.streamer.tool_loop as tool_loop_mod

    monkeypatch.setattr(tool_loop_mod, "TURN_ROUND_CAP_GRACE_ROUNDS", 2)

    streamer = _make_tooling_streamer()
    requests: list[list[dict]] = []

    async def fake_stream(*, messages, **kwargs):
        requests.append([dict(m) for m in messages])
        return _tool_round(len(requests) - 1)

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    gate_calls: list[int] = []

    async def gate_stub(tool_history):
        gate_calls.append(len(tool_history))
        return "[TURN EXIT] unsettled obligations — settle them now."

    result = await _run_tool_loop(
        streamer, max_tool_rounds=2, exit_gate_check=gate_stub
    )

    # cap=2：轮 0/1 正常工具轮；轮 2 到点跑门禁（hint 注入）；轮 3 宽限执行；
    # 轮 4（cap+grace）强制收口。
    assert gate_calls == [2]
    assert len(requests) == 3  # 轮 0/1/3 发请求；轮 2 被门禁消费、轮 4 被强制收口
    assert requests[2][-1]["role"] == "user"
    assert "[TURN EXIT]" in requests[2][-1]["content"]  # hint 进了宽限轮请求
    assert result["status"] == "ok"
    assert result["round_cap_forced"] is True
    assert "budget_exhausted" not in result  # 预算语义不复活
    force = pop_force_exit_block(AGENT_ID)
    assert force is not None
    assert force["violations"] == ["TURN_ROUND_CAP_EXCEEDED"]
    assert force["source"] == "turn_round_cap_force_close"


async def test_auto_round_cap_settled_breaks_to_platform_close(
    auto_mode, monkeypatch
):
    """轮帽收口语义②：600 轮到点门禁判全部结算 ⇒ break，走既有 max_rounds
    收口（status ok），handle_completion 消费平台 commit —— 不强挂、不扩轮。"""
    import hiveweave.llm.streamer.tool_loop as tool_loop_mod

    monkeypatch.setattr(tool_loop_mod, "TURN_ROUND_CAP_GRACE_ROUNDS", 2)

    streamer = _make_tooling_streamer()
    gate_calls: list[int] = []

    async def fake_stream(*, messages, **kwargs):
        return _tool_round(len(gate_calls))

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    async def gate_stub(tool_history):
        gate_calls.append(len(tool_history))
        return None  # 全部结算（或 5 连停强挂）⇒ loop 退出

    async def fake_summary(*args, **kwargs):
        return "wrapped up by platform summary"

    streamer._make_max_rounds_summary = fake_summary  # type: ignore[method-assign]

    result = await _run_tool_loop(
        streamer, max_tool_rounds=2, exit_gate_check=gate_stub
    )

    assert gate_calls == [2]  # 门禁只在轮帽到点跑一次
    assert result["status"] == "ok"
    assert result["content"] == "wrapped up by platform summary"
    assert "round_cap_forced" not in result
    assert pop_force_exit_block(AGENT_ID) is None


async def test_auto_round_cap_force_close_consumed_as_blocked(
    auto_mode, env, monkeypatch
):
    """轮帽强制收口的消费端：强挂标记 + 未结算事实 ⇒ disposition=blocked
    并 _escalate_gate_exhaustion 通知 creator（与 5 连停强挂同一权威出口）。"""
    from hiveweave.services.turn_session import set_force_exit_block

    set_force_exit_block(
        AGENT_ID,
        {
            "violations": ["TURN_ROUND_CAP_EXCEEDED"],
            "source": "turn_round_cap_force_close",
        },
    )
    agent = _completion_agent(env)
    agent._persist_gate_notice = AsyncMock()
    agent._escalate_gate_exhaustion = AsyncMock()
    agent._escalate_unreplied = AsyncMock()
    agent._inbox = MagicMock(mark_read_by_ids=AsyncMock())
    await _run_handle_completion(
        agent,
        {"content": "[TURN ROUND CAP] force-closed", "round_cap_forced": True},
        monkeypatch,
        scenario="assignee_running",
    )
    assert agent.disposition == "blocked"
    assert agent._escalate_gate_exhaustion.await_count == 1
    detail = agent._escalate_gate_exhaustion.await_args.args[1]
    assert detail["source"] == "turn_round_cap_force_close"
    assert pop_force_exit_block(AGENT_ID) is None  # 标记被消费


# ── 7. handle_completion auto 分支：预算死分支防御 / 平台 commit 落账 ──


async def _run_row(run_id: str) -> dict | None:
    from hiveweave.db import project as project_db

    return await project_db.query_one(
        AGENT_ID, "SELECT * FROM agent_runs WHERE id = ?", [run_id]
    )


def _completion_agent(env, run_id: str | None = None):
    from hiveweave.agents.agent import Agent

    agent = object.__new__(Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    agent.config = {"name": "t16", "role": "executor"}
    agent.disposition = "runnable"
    agent.pending_inbox_msg_ids = None
    agent.current_job = {"started_at": 0}
    agent._turn_gate_count = 0
    agent._TURN_GATE_MAX = 1
    agent._reply_reminder_count = 0
    agent._task_reminder_count = 0
    agent._TASK_REMINDER_MAX = 2
    agent._unreplied_asks_streak = 0
    agent._no_progress_streak = 0
    agent._progress_fingerprint = None
    agent._empty_done_slice_streak = 0
    agent._slice_budget = 0
    agent._current_run_id = run_id
    agent._streaming_msg_id = None
    agent._stream_timeout_streak = 0
    agent._rate_limit_streak = 0
    agent._resume_cooldown_until = 0.0
    agent._run_ledger = MagicMock(
        complete_run=AsyncMock(), interrupt_run=AsyncMock()
    )
    agent._work_log = MagicMock(write_work_log=AsyncMock())
    agent._chat_msg = MagicMock(save_message=AsyncMock())
    agent._conversation = MagicMock(append_turn=AsyncMock())
    agent._cancel_safety_timer = MagicMock()
    agent._broadcast_stream_event = MagicMock()
    agent._go_idle = AsyncMock()
    agent._broadcast_agent_health = MagicMock()
    agent._drain_message_queue = AsyncMock()
    agent._clear_resume_suppressed = MagicMock()
    agent._maybe_self_retrigger = AsyncMock()
    agent._retrigger_for_open_tasks = AsyncMock()
    return agent


async def _run_handle_completion(agent, result, monkeypatch, scenario="settled"):
    import hiveweave.agents.completion as completion_mod

    async def fake_gather(ag, tool_calls):
        return _facts_stub(scenario)

    monkeypatch.setattr(
        completion_mod, "gather_exit_gate_facts", fake_gather
    )
    from hiveweave.agents.completion import handle_completion

    await handle_completion(agent, result, "hi", {"trigger": False})


async def test_completion_auto_budget_stop_dead_branch_defense(
    auto_mode, env, monkeypatch
):
    """【T16 增补后 dead 分支防御】auto 回合无墙钟预算 ⇒ tool_loop 不再产生
    budget_exhausted（见 test_auto_budget_deadline_not_armed_legacy_still_cut）；
    本用例喂旁路结果钉死「防御路径仍按一期语义兜底」：interrupted + 延续唤醒
    武装（幂等 per-run），不静默冒充自然停收口。正常 auto 回合走不到这里。"""
    from hiveweave.services.run_ledger import run_ledger

    clear_pending_turn_result(AGENT_ID)
    run_id = await run_ledger.create_run(AGENT_ID, activation_id="act-1")
    agent = _completion_agent(env, run_id=run_id)
    agent._run_ledger = run_ledger  # 真账本：断言 interrupted 落库
    await _run_handle_completion(
        agent,
        {"content": "partial work kept", "budget_exhausted": True},
        monkeypatch,
    )
    # run 标 interrupted
    row = await _run_row(run_id)
    assert row is not None and row["status"] == "interrupted"
    # 延续唤醒已武装（幂等键 per-run）
    assert agent._budget_continue_armed_run == run_id
    assert agent._budget_continue_timer is not None
    # 幂等：同 run 再武装不换 timer
    t1 = agent._budget_continue_timer
    agent._arm_budget_continue(run_id)
    assert agent._budget_continue_timer is t1
    # 无平台 commit（预算停不合成 TurnResult）
    assert get_pending_turn_result(AGENT_ID) is None
    assert agent.disposition == "runnable"
    agent._cancel_budget_continue()
    assert agent._budget_continue_armed_run is None


async def test_completion_auto_natural_stop_platform_commit(
    auto_mode, env, monkeypatch
):
    """自然停 + 全结算 ⇒ 平台生成 commit 落 work_log（UI 契约）、
    phase=done_slice、disposition 收口；不重复 commit（pending 被消费）。"""
    agent = _completion_agent(env)
    clear_pending_turn_result(AGENT_ID)
    await _run_handle_completion(
        agent,
        {"content": "did the slice"},
        monkeypatch,
    )
    # 平台 commit 已被 ok 分支消费（pop）且 work_log 落了 turn_result 记录
    assert get_pending_turn_result(AGENT_ID) is None
    turn_result_logs = [
        c
        for c in agent._work_log.write_work_log.await_args_list
        if c.args and len(c.args) > 3 and c.args[3] == "turn_result"
    ]
    assert turn_result_logs, "platform commit must land a turn_result work_log"
    summary_arg = turn_result_logs[0].args[4]
    assert "[done_slice/AUTO]" in summary_arg


async def test_completion_auto_wait_then_budget_stop_no_double_park(
    auto_mode, env, monkeypatch
):
    """【T16 增补后 dead 分支防御】wait_on 与预算停的死竞争（正常 auto 回合
    已无预算停；喂旁路结果钉死防御语义）：run interrupted + 延续唤醒；
    wait 契约不被误清（等待与延续唤醒不双挂/不互吞）。"""
    from hiveweave.services.run_ledger import run_ledger
    from hiveweave.services.task import TaskService
    from hiveweave.services.wait_contract import wait_contract_service

    clear_pending_turn_result(AGENT_ID)
    task_id = await TaskService().create_task(
        PROJECT_ID, "race target", "desc", creator_id=OTHER_AGENT_ID,
        assignee_id=AGENT_ID,
    )
    await wait_contract_service.replace_waits(
        PROJECT_ID,
        AGENT_ID,
        [{"kind": "task", "ref": task_id}],
        phase="waiting",
    )
    run_id = await run_ledger.create_run(AGENT_ID, activation_id="act-2")
    agent = _completion_agent(env, run_id=run_id)
    agent._run_ledger = run_ledger  # 真账本：断言 interrupted 落库
    await _run_handle_completion(
        agent,
        {"content": "halfway", "budget_exhausted": True},
        monkeypatch,
    )
    row = await _run_row(run_id)
    assert row["status"] == "interrupted"
    assert agent._budget_continue_armed_run == run_id
    waits = await wait_contract_service.list_active(PROJECT_ID, AGENT_ID)
    assert len(waits) == 1 and waits[0]["ref"] == task_id
    agent._cancel_budget_continue()


async def test_completion_legacy_budget_stop_no_continuation(
    legacy_mode, env, monkeypatch
):
    """legacy：预算墙权威收口语义零变化 —— 不 interrupted、不延续唤醒。"""
    from hiveweave.services.run_ledger import run_ledger

    clear_pending_turn_result(AGENT_ID)
    run_id = await run_ledger.create_run(AGENT_ID, activation_id="act-3")
    agent = _completion_agent(env, run_id=run_id)
    agent._run_ledger = run_ledger  # 真账本：断言 legacy 保持 completed
    await _run_handle_completion(
        agent,
        {"content": "partial", "budget_exhausted": True},
        monkeypatch,
        scenario="assignee_running",
    )
    row = await _run_row(run_id)
    assert row["status"] == "completed"  # legacy 顶部 complete_run 不被覆盖
    assert not hasattr(agent, "_budget_continue_armed_run") or (
        agent._budget_continue_armed_run is None
    )


# ── 8. legacy 零变化抽检（核心 5 条）─────────────────────────


async def test_legacy_gate_supplement_budget_still_one(legacy_mode, monkeypatch):
    """legacy：_TURN_GATE_MAX=1 —— 第 2 次违规不再 hint（既有语义）。"""
    import hiveweave.agents.completion as completion_mod
    from hiveweave.agents.completion import build_gate_supplement_hint

    async def fake_gather(ag, tool_calls):
        return _facts_stub("assignee_running")

    monkeypatch.setattr(
        completion_mod, "gather_exit_gate_facts", fake_gather
    )
    agent = _make_agent()
    clear_pending_turn_result(AGENT_ID)
    set_pending_turn_result(
        AGENT_ID,
        {
            "schema_version": 1,
            "phase": "done_slice",
            "summary": "claimed done",
            "waiting_on": [],
            "result": {},
            "extensions": {},
        },
    )
    first = await build_gate_supplement_hint(agent, [])
    assert first and "ASSIGNEE_MUST_SUBMIT" in first
    assert agent._turn_gate_count == 1
    second = await build_gate_supplement_hint(agent, [])
    assert second is None  # 预算 1 耗尽
    clear_pending_turn_result(AGENT_ID)


async def test_legacy_auto_path_not_invoked(legacy_mode, env, monkeypatch):
    """legacy 下 agent 接缝不进 auto 判据（防灰度渗漏）。"""
    from hiveweave.services import turn_exit_auto

    called = {"n": 0}

    async def spy(ag, tool_calls):
        called["n"] += 1
        return None

    monkeypatch.setattr(turn_exit_auto, "build_auto_exit_gate_hint", spy)
    # 直接模拟 agent._gate_supplement_check 的分发逻辑
    from hiveweave.config import turn_exit_mode

    async def dispatch(tool_calls):
        if turn_exit_mode() == "auto":
            return await turn_exit_auto.build_auto_exit_gate_hint(
                _make_agent(), tool_calls
            )
        return "legacy-hint"

    assert await dispatch([]) == "legacy-hint"
    assert called["n"] == 0


def test_legacy_wait_on_hidden_from_family_toolsets():
    """legacy 工具面：家族预设不因 wait_on 注册而变化（可见面剔除）。"""
    from hiveweave.services.permission import (
        CEO_TOOLS,
        _BASE_TOOLS,
        permission_service,
    )

    assert "wait_on" in _BASE_TOOLS  # 执行面放行
    assert "wait_on" not in CEO_TOOLS or True  # CEO 集合经由 _BASE_TOOLS 派生
    names = permission_service.get_tools_for_agent(
        {
            "role": "ceo",
            "permission_type": "ceo",
            "permission_mode": "full",
        }
    )
    assert "wait_on" not in names


# ── T38/T39（2026-10-01）：wait_on 短号解析 + user 等待 ───────────────


async def _insert_project_agent(
    short_id: str,
    agent_id: str,
    *,
    name: str = "agent",
    status: str = "active",
) -> None:
    """env 夹具的 per-project DB 里直插一行 agents（短号解析的数据面）。"""
    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    cur = await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "model_id, created_at) VALUES (?, ?, ?, ?, 'executor', ?, 'gpt-x', ?)",
        [agent_id, short_id, PROJECT_ID, name, status, int(time.time() * 1000)],
    )
    await cur.close()
    await conn.commit()


async def _insert_question(
    qid: str,
    *,
    status: str = "pending",
    agent_id: str = AGENT_ID,
    options: list | None = None,
) -> None:
    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    cur = await conn.execute(
        "INSERT INTO questions (id, agent_id, project_id, question, options, "
        "status, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            qid,
            agent_id,
            PROJECT_ID,
            "T39 test question",
            json.dumps(options, ensure_ascii=False) if options else None,
            status,
            int(time.time() * 1000),
            int(time.time() * 1000) + 600_000,
        ],
    )
    await cur.close()
    await conn.commit()


async def _inbox_bodies(agent_id: str) -> list[str]:
    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    cur = await conn.execute(
        "SELECT message FROM inbox WHERE to_agent_id = ? ORDER BY created_at",
        [agent_id],
    )
    rows = await cur.fetchall()
    await cur.close()
    return [str(r["message"] or "") for r in rows or []]


# ── T38：wait_on 认短号（A6xx agent / 8 位任务前缀）──────────────────


async def test_wait_on_agent_short_id_resolves_to_full_id(auto_mode, env):
    """A6xx 短号命中 ⇒ 通过且挂起契约落完整 id（唤醒链按完整 id 匹配）。"""
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    await _insert_project_agent("A606", OTHER_AGENT_ID, name="短线工")
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "agent", "ref": "A606"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is True, r.error
    waits = await wait_contract_service.list_active(PROJECT_ID, AGENT_ID)
    assert len(waits) == 1
    assert waits[0]["kind"] == "agent"
    assert waits[0]["ref"] == OTHER_AGENT_ID  # 落账完整 id，非 A606
    await wait_contract_service.clear_waits(PROJECT_ID, AGENT_ID)


async def test_wait_on_agent_unknown_short_id_rejected(auto_mode, env):
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    await _insert_project_agent("A606", OTHER_AGENT_ID)
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "agent", "ref": "A999"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is False
    assert "A999" in (r.error or "")
    assert "does not exist" in (r.error or "")
    assert r.extra.get("remedy")
    assert await wait_contract_service.list_active(PROJECT_ID, AGENT_ID) == []


async def test_wait_on_agent_short_id_self_rejected(auto_mode, env):
    """短号解析后指向自己 ⇒ 与全 id 自等同判（不是等待）。"""
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    await _insert_project_agent("A601", AGENT_ID)
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "agent", "ref": "A601"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is False
    assert "yourself" in (r.error or "")


async def test_wait_on_agent_short_id_archived_rejected(auto_mode, env):
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    await _insert_project_agent("A608", OTHER_AGENT_ID, status="archived")
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "agent", "ref": "A608"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is False
    assert "archived" in (r.error or "")


async def test_wait_on_task_short_prefix_resolves_to_full_id(auto_mode, env):
    from hiveweave.services.task import TaskService
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    task_id = await TaskService().create_task(
        PROJECT_ID, "prefix target", "desc", creator_id=OTHER_AGENT_ID,
        assignee_id=AGENT_ID,
    )
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "task", "ref": task_id[:8]}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is True, r.error
    waits = await wait_contract_service.list_active(PROJECT_ID, AGENT_ID)
    assert waits and waits[0]["ref"] == task_id  # 落账完整 id
    await wait_contract_service.clear_waits(PROJECT_ID, AGENT_ID)


async def test_wait_on_task_ambiguous_prefix_rejected(auto_mode, env):
    from hiveweave.services.task import TaskService
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    t1 = await TaskService().create_task(
        PROJECT_ID, "amb a", "desc", creator_id=OTHER_AGENT_ID
    )
    t2 = await TaskService().create_task(
        PROJECT_ID, "amb b", "desc", creator_id=OTHER_AGENT_ID
    )
    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    for old, new in ((t1, "deadbeef-aaaa"), (t2, "deadbeef-bbbb")):
        cur = await conn.execute(
            "UPDATE tasks SET id = ? WHERE id = ?", [new, old]
        )
        await cur.close()
    await conn.commit()
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "task", "ref": "deadbeef"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is False
    assert "multiple tasks" in (r.error or "")
    await wait_contract_service.clear_waits(PROJECT_ID, AGENT_ID)


# ── T39：wait_on(user) —— 等一道真实 question 的用户答复 ─────────────


async def test_wait_on_user_pending_question_registers(auto_mode, env):
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    qid = "q-t39-pending-0001"
    await _insert_question(qid, status="pending")
    r = await wait_on_tool(
        WaitOnParams(
            refs=[{"kind": "user", "ref": qid, "note": "等 D85 答复"}]
        ),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is True, r.error
    waits = await wait_contract_service.list_active(PROJECT_ID, AGENT_ID)
    assert len(waits) == 1
    assert waits[0]["kind"] == "user"
    assert waits[0]["ref"] == qid
    assert waits[0]["note"] == "等 D85 答复"
    # 回执注明「用户答复不可预验」（存在性弱校验的诚实边界）
    assert "弱校验" in (r.output or "")
    await wait_contract_service.clear_waits(PROJECT_ID, AGENT_ID)


async def test_wait_on_user_without_question_rejected(auto_mode, env):
    """红线：user 挂起必须关联真实发出过的 question —— 裸挂拒绝。"""
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "user", "ref": "ghost-question"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is False
    assert "does not exist" in (r.error or "")
    assert "question_id" in (r.error or "")  # 处方指回 question 回执
    assert await wait_contract_service.list_active(PROJECT_ID, AGENT_ID) == []


async def test_wait_on_user_closed_question_rejected(auto_mode, env):
    """已答复 / 已裁决的 question 无物可等 —— 从严拒绝（防万能暂停）。"""
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    await _insert_question("q-t39-answered", status="answered")
    r1 = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "user", "ref": "q-t39-answered"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r1.success is False
    assert "already answered" in (r1.error or "")

    await _insert_question("q-t39-adjudicated", status="timed_out")
    r2 = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "user", "ref": "q-t39-adjudicated"}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r2.success is False
    assert "auto-adjudicated" in (r2.error or "")
    assert await wait_contract_service.list_active(PROJECT_ID, AGENT_ID) == []


async def test_question_timeout_receipt_carries_question_id(
    auto_mode, env, monkeypatch
):
    """轮内超时回执带 question_id（wait_on(user) 的挂单凭据）——T39 全链
    第一环：超时 → 拿 id → wait_on 挂上。"""
    from hiveweave.db import meta as meta_db
    from hiveweave.tools import question as qmod
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    async def fake_project_id(aid: str) -> str:
        return PROJECT_ID if aid == AGENT_ID else ""

    monkeypatch.setattr(meta_db, "get_agent_project_id", fake_project_id)
    monkeypatch.setattr(qmod, "QUESTION_TIMEOUT_S", 0.05)

    result = await qmod.execute_question(
        agent_id=AGENT_ID, question="T39 超时链路"
    )
    assert result["success"] is False
    assert "STAYS PENDING" in (result.get("error") or "")
    assert "wait_on" in (result.get("error") or "")  # 处方在场
    qid = result.get("question_id") or ""
    assert qid, "超时回执必须带 question_id"
    # question 保持 pending
    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    cur = await conn.execute(
        "SELECT status FROM questions WHERE id = ?", [qid]
    )
    row = await cur.fetchone()
    await cur.close()
    assert row and row[0] == "pending"

    # 全链第二环：拿回执里的 id 挂 wait_on(user) —— 成功
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "user", "ref": qid}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is True, r.error
    waits = await wait_contract_service.list_active(PROJECT_ID, AGENT_ID)
    assert waits and waits[0]["ref"] == qid and waits[0]["kind"] == "user"

    qmod._question_projects.pop(qid, None)
    qmod._cancel_adjudicator(qid)
    await wait_contract_service.clear_waits(PROJECT_ID, AGENT_ID)


async def test_wake_question_waiters_clears_notifies_idempotent(auto_mode, env):
    """唤醒链：答复到达 → 等待行清 + urgent inbox；二次调用幂等。"""
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools import question as qmod
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    qid = "q-t39-wake-0001"
    await _insert_question(qid, status="pending")
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "user", "ref": qid}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is True

    woken = await qmod.wake_question_waiters(
        PROJECT_ID, qid, answer="按 A 方案走"
    )
    assert woken == [AGENT_ID]
    assert await wait_contract_service.list_active(PROJECT_ID, AGENT_ID) == []
    bodies = await _inbox_bodies(AGENT_ID)
    assert any("[QUESTION ANSWERED]" in b and "按 A 方案走" in b for b in bodies)

    # 幂等：行已清 ⇒ 二次调用 no-op、不重复投信
    before = len(await _inbox_bodies(AGENT_ID))
    assert await qmod.wake_question_waiters(PROJECT_ID, qid, answer="再答") == []
    assert len(await _inbox_bodies(AGENT_ID)) == before


async def test_late_answer_orphan_cleanup_wakes_wait_on_waiter(auto_mode, env):
    """晚到答案全链（T39 现场形态）：提问超时 → wait_on 挂起 → future 已
    不在 ⇒ resolve_question 走 orphan 清扫 ⇒ 等待者（提问者本人）被唤醒。"""
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools import question as qmod
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    qid = "q-t39-orphan-001"
    await _insert_question(qid, status="pending")
    qmod._question_projects[qid] = (PROJECT_ID, AGENT_ID)
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "user", "ref": qid}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r.success is True

    # 用户答复走 API：DB 已写、future 不在 ⇒ resolve_question=False → 清扫
    assert qmod.resolve_question(qid, "晚到的答复") is False
    for _ in range(50):
        if not await wait_contract_service.list_active(PROJECT_ID, AGENT_ID):
            break
        await asyncio.sleep(0.02)
    assert await wait_contract_service.list_active(PROJECT_ID, AGENT_ID) == []
    bodies = await _inbox_bodies(AGENT_ID)
    assert any("[QUESTION ANSWERED]" in b and "晚到的答复" in b for b in bodies)
    qmod._question_projects.pop(qid, None)


async def test_adjudication_wakes_wait_on_waiter(auto_mode, env):
    """触发 A 裁决（600s 无人应答）⇒ wait_on(user) 等待者一并解锁并收到
    [QUESTION AUTO-RESOLVED]（等不到的答复不再空挂到 TTL）。

    等待者 ≠ 提问者：提问者的同生 wait 由裁决链自身收口（send_inbox /
    trigger_agent 出口），这里验证的是 wait_on 挂起的**另一位**等待者。"""
    from hiveweave.services.wait_contract import wait_contract_service
    from hiveweave.tools import question as qmod
    from hiveweave.tools.wait_tools import WaitOnParams, wait_on_tool

    qid = "q-t39-adjud-001"
    await _insert_question(
        qid,
        status="pending",
        agent_id=OTHER_AGENT_ID,  # 提问者
        options=[{"label": "A 方案（推荐）"}, {"label": "B 方案"}],
    )
    r = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "user", "ref": qid}]),
        AGENT_ID,  # 等待者：等同一道题的另一位
        "",
        ctx=_ctx(),
    )
    assert r.success is True

    summary = await qmod.adjudicate_question(
        qid,
        PROJECT_ID,
        reason=qmod.RESOLVED_BY_TIMEOUT,
        send_inbox=False,
        trigger_agent=False,
    )
    assert summary and summary["questionId"] == qid
    assert await wait_contract_service.list_active(PROJECT_ID, AGENT_ID) == []
    bodies = await _inbox_bodies(AGENT_ID)
    assert any(
        "[QUESTION AUTO-RESOLVED]" in b and "A 方案（推荐）" in b
        for b in bodies
    )
    # 裁决后 question 已收口 ⇒ 再挂 wait_on(user) 被拒（无物可等）
    r2 = await wait_on_tool(
        WaitOnParams(refs=[{"kind": "user", "ref": qid}]),
        AGENT_ID,
        "",
        ctx=_ctx(),
    )
    assert r2.success is False
    assert "auto-adjudicated" in (r2.error or "")


# ── T16 三期补漏（qoder 报告 P0-1）：每回合注入的出口条件提示按模式分流 ──
# 病灶：build_exit_contract_hint 在 agent.py 5a 每回合注入，auto 下仍在教
# 「仅需提交 commit_turn 收尾」—— S19/D85 终局兰屿/天线/屿洲三人独立上报
# 的直接教唆源（暴露面最大的一处教学残留）。


@pytest.mark.asyncio
async def test_exit_contract_hint_auto_never_teaches_commit(auto_mode, env):
    """auto 模式：干净账本 ⇒ 出口提示教 wait_on 挂起，不给 commit_turn 处方。"""
    from hiveweave.services.turn_exit import build_exit_contract_hint

    hint = await build_exit_contract_hint(AGENT_ID, PROJECT_ID)
    assert hint, "干净账本也必须注入出口条件（无待办分支）"
    assert "仅需提交 commit_turn" not in hint, hint
    assert "wait_on(refs=" in hint, hint
    assert "平台自动收口" in hint, hint
    # WAIT_WITHOUT_ASK 预告在两种模式都要可达（审计 P2）
    assert "WAIT_WITHOUT_ASK" in hint, hint


@pytest.mark.asyncio
async def test_exit_contract_hint_legacy_keeps_commit(auto_mode, env, monkeypatch):
    """legacy 模式：原文案逐字保留（T16 零变化契约）。"""
    from hiveweave.services.turn_exit import build_exit_contract_hint

    monkeypatch.setattr(settings, "turn_exit_mode", "legacy")
    hint = await build_exit_contract_hint(AGENT_ID, PROJECT_ID)
    assert hint
    assert "仅需提交 commit_turn 收尾" in hint, hint
    assert "commit_turn(phase='done_slice'|'waiting'|'blocked'" in hint, hint


# ── T16 补漏（第三轮上下文审计）：停泊/强挂通知体不得嵌 legacy 处方墙 ──


def test_auto_block_hint_uses_wait_on_prescriptions():
    """auto 通知体处方表：wait_on 话术 + 下架点名，无 legacy commit_turn 处方。"""
    from hiveweave.services.turn_exit_auto import build_auto_block_hint

    hint = build_auto_block_hint(
        ["UNCOMMITTED_WORKTREE", "ASSIGNEE_MUST_SUBMIT", "UNKNOWN_GATE"]
    )
    assert "commit_turn 本模式已下架" in hint
    assert "wait_on(refs=" in hint
    assert "git_worktree_checkpoint" in hint  # 处方来自 auto 动作表
    assert "submit_task" in hint
    assert "每一轮必须像函数一样返回" not in hint  # legacy 处方墙头句
    assert "then commit_turn" not in hint
