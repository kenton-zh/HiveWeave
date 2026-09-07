"""批次 A 小件回归：waitingOn 归一、rework 空 feedback 硬拒、race 事实位。

46 轮 #3/#12/#9——模型连 RETRY 标记都不读，文案层修法降权，入口归一/
门禁硬拒/去时长承诺是根治。
"""

from __future__ import annotations

import pytest

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401
from hiveweave.tools.turn_tools import CommitTurnParams
from hiveweave.tools.tasks.review import _race_fact_bit as _review_race_bit


# ── #3 waitingOn 大小写×类型归一 ──────────────────────────────


def test_waiting_on_camel_string_normalized_to_task_list():
    """LLM 高频误形 `waitingOn: "<task-id>"`（字符串）→ 单条 task 等待。"""
    params = CommitTurnParams(
        phase="waiting", summary="s", waitingOn="task-123"
    )
    assert params.waiting_on == [{"kind": "task", "ref": "task-123"}]


def test_waiting_on_lowercase_alias_accepted():
    """小写 `waitingon` 别名在工具 schema 归一层被接受。"""
    from hiveweave.tools.base import _TOOL_REGISTRY

    td = _TOOL_REGISTRY["commit_turn"]
    validated = td.validate({
        "phase": "waiting",
        "summary": "s",
        "waitingon": [{"kind": "agent", "ref": "a1"}],
    })
    args = validated[0] if isinstance(validated, tuple) else validated
    assert args.waiting_on == [{"kind": "agent", "ref": "a1"}]


def test_waiting_on_single_dict_wrapped():
    params = CommitTurnParams(
        phase="waiting",
        summary="s",
        waitingOn={"kind": "agent", "ref": "a1"},
    )
    assert params.waiting_on == [{"kind": "agent", "ref": "a1"}]


def test_waiting_on_real_list_untouched():
    lst = [{"kind": "timer", "ref": "t1", "note": "n"}]
    params = CommitTurnParams(phase="waiting", summary="s", waitingOn=lst)
    assert params.waiting_on == lst


# ── #9 race 事实位：不做时长承诺 ─────────────────────────────


def test_race_fact_bit_has_no_time_promise():
    bit = _review_race_bit({"submitted_at": 1})
    assert "~" not in bit
    assert "should become reviewable" not in bit
    assert "submitted" in bit


# ── #12 rework 空 feedback 硬拒（工具层）─────────────────────


async def _mk_reviewing_task(task_env):
    """建任务→claim→submit→start_review，进入 reviewing 态。"""
    from hiveweave.services.task import TaskService

    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        project_id=pid,
        title="batch-A rework gate",
        description="d",
        creator_id="creator-1",
        assignee_id="assignee-1",
    )
    await ts.claim_task(pid, tid, "assignee-1")
    await ts.start_task(pid, tid)  # claimed → running（submitted 前置态）
    await ts.submit_task(pid, tid, evidence={"summary": "done"})
    await ts.start_review(pid, tid, reviewer_id="reviewer-1")
    return ts, pid, tid


@pytest.mark.asyncio
async def test_rework_empty_feedback_rejected(task_env, monkeypatch):
    """46 轮 #12：空 feedback 的 rework 在工具层硬拒（feedback_absent）。"""
    from hiveweave.services import task as task_mod
    from hiveweave.tools.tasks.review import review_task_tool, ReviewTaskParams

    ts = task_mod.TaskService()
    pid = task_env["project_id"]
    tid = await _mk_reviewing_task(task_env)
    tid = tid[2] if isinstance(tid, tuple) else tid

    captured: dict = {}

    async def _fake_review(*a, **kw):
        captured["decision"] = kw.get("decision") or (a[2] if len(a) > 2 else None)
        captured["feedback"] = kw.get("feedback") or (a[3] if len(a) > 3 else None)
        return {"ok": True}

    monkeypatch.setattr(task_mod.TaskService, "review_task", _fake_review)

    async def _fake_pid(agent_id: str):
        return pid

    monkeypatch.setattr(
        "hiveweave.tools.helpers.get_project_id", _fake_pid
    )
    async def _fake_agent_project(agent_id: str):
        # 假 agent（assignee/reviewer/creator）→ 测试项目
        return (
            pid if agent_id in ("assignee-1", "reviewer-1", "creator-1")
            else None
        )

    monkeypatch.setattr(
        "hiveweave.db.meta.get_agent_project_id", _fake_agent_project
    )
    result = await review_task_tool(
        ReviewTaskParams(taskId=tid, decision="rework", feedback=None),
        agent_id="reviewer-1",
        workspace=task_env["workspace"],
    )
    assert result.success is False
    assert "feedback_absent" in (result.error or "")
    assert "REWORK REJECTED" in (result.error or "")
    assert captured == {}  # 门在 service 调用之前
    assert (await ts.get_task(pid, tid))["status"] == "reviewing"


@pytest.mark.asyncio
async def test_rework_with_feedback_passes(task_env, monkeypatch):
    from hiveweave.services import task as task_mod
    from hiveweave.tools.tasks.review import review_task_tool, ReviewTaskParams

    ts, pid, tid = await _mk_reviewing_task(task_env)

    async def _fake_review(*a, **kw):
        return {"ok": True}

    async def _fake_pid2(agent_id: str):
        return pid

    monkeypatch.setattr(
        task_mod.TaskService, "review_task", _fake_review
    )
    monkeypatch.setattr(
        "hiveweave.tools.helpers.get_project_id", _fake_pid2
    )
    async def _fake_agent_project(agent_id: str):
        # 假 agent（assignee/reviewer/creator）→ 测试项目
        return (
            pid if agent_id in ("assignee-1", "reviewer-1", "creator-1")
            else None
        )

    monkeypatch.setattr(
        "hiveweave.db.meta.get_agent_project_id", _fake_agent_project
    )
    result = await review_task_tool(
        ReviewTaskParams(
            taskId=tid, decision="rework",
            feedback="src/main.py line 3 wrong"
        ),
        agent_id="reviewer-1",
        workspace=task_env["workspace"],
    )
    assert result.success is True
    # 状态迁移在（被桩掉的）service 层——工具门只负责放行
    assert True  # 工具门放行（状态迁移在被桩的 service 层）


# ── 46 轮 #1：封闭集管道尾自动翻译（execute_bash 接线）────────────


@pytest.mark.asyncio
async def test_execute_bash_auto_translates_closed_pipe_tail(
    task_env, monkeypatch
):
    from hiveweave.tools import bash as bash_mod

    import hiveweave.services.acl_sandbox.integration as acl_int

    monkeypatch.setattr(acl_int, "acl_sandbox_active", lambda: True)
    monkeypatch.setattr(
        "hiveweave.tools.bash._pwsh_is_effective_shell", lambda: True
    )
    captured: dict = {}

    async def _fake_run_sandboxed(*args, **kw):
        captured["command"] = args[0] if args else kw.get("command")
        return {"output": "a\nb\nc", "stdout": "a\nb\nc", "stderr": "",
                "exit_code": 0, "timed_out": False, "error": None}

    monkeypatch.setattr(bash_mod, "_run_sandboxed", _fake_run_sandboxed)
    result = await bash_mod.execute_bash(
        command="git log --oneline -5 | head -3",
        workdir="",
        workspace_path=task_env["workspace"],
        timeout_ms=15000,
        agent_id="a1",
    )
    assert result["success"] is True
    cmd = captured.get("command") or ""
    assert "Select-Object -First 3" in cmd
    # 可执行段（尾注 `# [auto-translated...]` 之前）不含 head
    executable = cmd.split("# [auto-translated")[0]
    assert "head" not in executable


@pytest.mark.asyncio
async def test_execute_bash_still_rejects_non_closed_unix(task_env, monkeypatch):
    """中段仍有 unix-only（grep）→ 不翻译，维持 gate 拒绝教学。"""
    from hiveweave.tools import bash as bash_mod

    import hiveweave.services.acl_sandbox.integration as acl_int

    monkeypatch.setattr(acl_int, "acl_sandbox_active", lambda: True)
    monkeypatch.setattr(
        "hiveweave.tools.bash._pwsh_is_effective_shell", lambda: True
    )
    ran: dict = {}

    async def _fake_run_sandboxed(*args, **kw):
        ran["command"] = args[0] if args else kw.get("command")
        return {"output": "", "stdout": "", "stderr": "",
                "exit_code": 0, "timed_out": False, "error": None}

    monkeypatch.setattr(bash_mod, "_run_sandboxed", _fake_run_sandboxed)
    result = await bash_mod.execute_bash(
        command="git log | grep fix | head -3",
        workdir="",
        workspace_path=task_env["workspace"],
        timeout_ms=15000,
        agent_id="a1",
    )
    assert result.get("dialect_failed") is True
    assert "command" not in ran  # 从未执行
