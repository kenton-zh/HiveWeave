"""Dogfooding TEST8 审计修复回归 — P1-a / P1-b / P2。

覆盖：
1. P1-a 记忆读写回环：write_memory(moduleId) → read_memory(moduleId) 可读回；
   moduleId 作为 module_id 列过滤而非 scope；无 moduleId 返回全部 agent 记忆。
2. P1-b VERIFY 审门：creator=CEO 的 VERIFY，CEO 不再被误判为实现者；
   父任务实现者与 evidence.merged_by 合并人仍被拒。
3. P1-b merged_by 持久化：submit_task 不覆盖 spawn 时写入的 merged_by。
4. P2 digest 去重：TeamChatService.check_and_mark 窗口内重复只登记一次，
   不同内容/不同接收人不受影响。
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from hiveweave.services.memory import MemoryService
from hiveweave.services.task import TaskService
from hiveweave.services.team_chat import TeamChatService
from hiveweave.tools.orchestration_tools import (
    ReadMemoryParams,
    WriteMemoryParams,
    read_memory_tool,
    write_memory_tool,
)
from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401

CEO = "ceo-1"
MERGER = "merger-1"
QA = "qa-1"


@pytest.fixture(autouse=True)
def _clear_memory_cache():
    from hiveweave.services import inbox as inbox_module
    from hiveweave.services import memory as memory_module

    memory_module._cache.clear()
    inbox_module._migrated.clear()  # 按 agent 缓存，跨 tmpdir 测试会污染
    yield
    memory_module._cache.clear()
    inbox_module._migrated.clear()


# ── P1-a 记忆读写回环 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_write_read_roundtrip_with_module_id(task_env):
    """write_memory(moduleId=M8) → read_memory(moduleId=M8) 必须读回。"""
    pid = task_env["project_id"]
    with patch(
        "hiveweave.tools.orchestration_tools.get_project_id",
        return_value=pid,
    ):
        w = await write_memory_tool(
            WriteMemoryParams(content="persistence check", moduleId="M8"),
            agent_id=EXEC, workspace=task_env["workspace"],
        )
        assert w.success, w.error

        r = await read_memory_tool(
            ReadMemoryParams(moduleId="M8"),
            agent_id=EXEC, workspace=task_env["workspace"],
        )
        assert r.success, r.error
        assert "persistence check" in r.output
        # 渲染键名是 type 而非 category（不再显示 [?]）
        assert "[tool_written]" in r.output


@pytest.mark.asyncio
async def test_memory_module_id_filters_not_scope(task_env):
    """moduleId 是 module_id 列过滤：读 M8 不回 M9；不带 moduleId 读全部。"""
    pid = task_env["project_id"]
    with patch(
        "hiveweave.tools.orchestration_tools.get_project_id",
        return_value=pid,
    ):
        await write_memory_tool(
            WriteMemoryParams(content="for module eight", moduleId="M8"),
            agent_id=EXEC, workspace=task_env["workspace"],
        )
        await write_memory_tool(
            WriteMemoryParams(content="for module nine", moduleId="M9"),
            agent_id=EXEC, workspace=task_env["workspace"],
        )

        r8 = await read_memory_tool(
            ReadMemoryParams(moduleId="M8"),
            agent_id=EXEC, workspace=task_env["workspace"],
        )
        assert "for module eight" in r8.output
        assert "for module nine" not in r8.output

        rall = await read_memory_tool(
            ReadMemoryParams(), agent_id=EXEC, workspace=task_env["workspace"],
        )
        assert "for module eight" in rall.output
        assert "for module nine" in rall.output


@pytest.mark.asyncio
async def test_memory_service_get_agent_memories_module_filter(task_env):
    """服务层：scope 恒为 agent，module_id 独立过滤（读写对称）。"""
    pid = task_env["project_id"]
    mem = MemoryService()
    await mem.add_entry(EXEC, pid, "alpha", module_id="M8", tags=["t1"])
    await mem.add_entry(EXEC, pid, "beta", module_id="M9")

    only_m8 = await mem.get_agent_memories(EXEC, pid, "agent", module_id="M8")
    assert [m["content"] for m in only_m8] == ["alpha"]

    all_mems = await mem.get_agent_memories(EXEC, pid)
    assert {m["content"] for m in all_mems} == {"alpha", "beta"}


# ── P1-b VERIFY 审门 ─────────────────────────────────────


async def _make_verify_task(pid: str) -> str:
    """构造 creator=CEO、merged_by=MERGER、父实现者=EXEC 的 reviewing VERIFY。"""
    ts = TaskService()
    parent_id = await ts.create_task(
        pid, "Feature X", "d", creator_id=COORD, assignee_id=EXEC
    )
    verify_id = await ts.create_task(
        pid,
        "VERIFY: Feature X",
        "verify",
        creator_id=CEO,
        assignee_id=QA,
        parent_task_id=parent_id,
        tags=["verify", "mandatory", "post-merge"],
        evidence={"merged_by": MERGER},
    )
    await ts.claim_task(pid, verify_id, QA)
    await ts.start_task(pid, verify_id)
    await ts.submit_task(
        pid, verify_id, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, verify_id)
    return verify_id


@pytest.mark.asyncio
async def test_verify_gate_ceo_not_blocked_as_creator(task_env):
    """BUG-P1b：creator=CEO 的 VERIFY，CEO approve 不得被判为 implementer/merger。"""
    pid = task_env["project_id"]
    verify_id = await _make_verify_task(pid)
    async def fake_agent_project(agent_id: str):
        return pid

    with patch(
        "hiveweave.tools.task_tools.get_project_id", return_value=pid
    ), patch(
        "hiveweave.db.meta.get_agent_project_id", fake_agent_project
    ):
        r = await review_task_tool(
            ReviewTaskParams(taskId=verify_id, decision="approve"),
            agent_id=CEO, workspace=task_env["workspace"],
        )
    # 可能因 attestation 等后续门禁失败，但绝不能是 VERIFY 独立审门误判
    msg = (r.error or "") + r.output
    assert "VERIFY approval must come from" not in msg


@pytest.mark.asyncio
async def test_verify_gate_ceo_rework_succeeds(task_env):
    """CEO 对 creator=自己的 VERIFY 可正常 rework（端到端过门）。"""
    pid = task_env["project_id"]
    verify_id = await _make_verify_task(pid)
    async def fake_agent_project(agent_id: str):
        return pid

    with patch(
        "hiveweave.tools.task_tools.get_project_id", return_value=pid
    ), patch(
        "hiveweave.db.meta.get_agent_project_id", fake_agent_project
    ):
        r = await review_task_tool(
            ReviewTaskParams(taskId=verify_id, decision="rework",
                             feedback="please rerun"),
            agent_id=CEO, workspace=task_env["workspace"],
        )
    assert r.success, r.error


@pytest.mark.asyncio
async def test_verify_gate_implementer_still_blocked(task_env):
    """父任务实现者（parent.assignee） approve VERIFY 仍被拒。"""
    pid = task_env["project_id"]
    verify_id = await _make_verify_task(pid)
    async def fake_agent_project(agent_id: str):
        return pid

    with patch(
        "hiveweave.tools.task_tools.get_project_id", return_value=pid
    ), patch(
        "hiveweave.db.meta.get_agent_project_id", fake_agent_project
    ):
        r = await review_task_tool(
            ReviewTaskParams(taskId=verify_id, decision="approve"),
            agent_id=EXEC, workspace=task_env["workspace"],
        )
    assert not r.success
    assert "VERIFY approval must come from" in (r.error or "")


@pytest.mark.asyncio
async def test_verify_gate_merger_still_blocked(task_env):
    """evidence.merged_by 合并人 approve VERIFY 仍被拒。"""
    pid = task_env["project_id"]
    verify_id = await _make_verify_task(pid)
    async def fake_agent_project(agent_id: str):
        return pid

    with patch(
        "hiveweave.tools.task_tools.get_project_id", return_value=pid
    ), patch(
        "hiveweave.db.meta.get_agent_project_id", fake_agent_project
    ):
        r = await review_task_tool(
            ReviewTaskParams(taskId=verify_id, decision="approve"),
            agent_id=MERGER, workspace=task_env["workspace"],
        )
    assert not r.success
    assert "VERIFY approval must come from" in (r.error or "")


@pytest.mark.asyncio
async def test_submit_task_preserves_merged_by(task_env):
    """BUG-P1b：submit 覆盖 evidence 时保留 spawn 写入的 merged_by。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid,
        "VERIFY: X",
        "verify",
        creator_id=CEO,
        assignee_id=QA,
        tags=["verify"],
        evidence={"merged_by": MERGER},
    )
    await ts.claim_task(pid, tid, QA)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid, tid, evidence={"tests_passed": True, "summary": "32/32 pass"}
    )
    task = await ts.get_task(pid, tid)
    ev = task["evidence"]
    assert ev["merged_by"] == MERGER
    assert ev["summary"] == "32/32 pass"


# ── P2 digest 去重 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_and_mark_dedupes_within_window(task_env):
    """同一 digest 窗口内只登记一次；不同内容/接收人不受影响。"""
    pid = task_env["project_id"]

    async def fake_agent_project(agent_id: str):
        return pid

    svc = TeamChatService()
    with patch(
        "hiveweave.db.meta.get_agent_project_id", fake_agent_project
    ):
        first = await svc.check_and_mark(QA, COORD, QA, "digest body")
        second = await svc.check_and_mark(QA, COORD, QA, "digest body")
        other_content = await svc.check_and_mark(QA, COORD, QA, "digest v2")
        other_recipient = await svc.check_and_mark(EXEC, COORD, EXEC,
                                                   "digest body")
    assert first is False       # 首次：登记，可写库
    assert second is True       # 窗口内重复：跳过写库
    assert other_content is False
    assert other_recipient is False


@pytest.mark.asyncio
async def test_record_message_still_dedupes(task_env):
    """原 record_message 去重语义不回归。"""
    pid = task_env["project_id"]

    async def fake_agent_project(agent_id: str):
        return pid

    svc = TeamChatService()
    with patch(
        "hiveweave.db.meta.get_agent_project_id", fake_agent_project
    ):
        r1 = await svc.record_message(QA, COORD, QA, "hello")
        r2 = await svc.record_message(QA, COORD, QA, "hello")
    assert r1 == "ok"
    assert r2 == "duplicate"
