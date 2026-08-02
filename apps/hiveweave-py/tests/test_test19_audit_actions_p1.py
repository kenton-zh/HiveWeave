"""TEST19 审计行动清单 ③-⑥ regression tests.

③ 归档/清扫推送恢复指引 —— archive_task 立即向 assignee+creator 推送
   带恢复指引的消息（含 reason_code 差异化），且与 relay 幂等 key 相同。
④ commit_turn 门禁可行动化 —— REJECTED 消息带编号步骤清单 +
   结构化 gates/actions 字段（ToolResult.err 支持 extra）。
⑤ 任务状态变更主动推送 —— relay 补齐 claimed/running/blocked/verifying
   recipient（此前静默，注释声称的 direct path 不存在）。
⑥ 证据落点官方约定 —— dispatch 注入 [EVIDENCE LOCATION]；identity
   prompt 有官方证据目录说明。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hiveweave.services.task import TaskService
from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


# ── ③ 归档推送恢复指引 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_pushes_guidance_to_assignee_and_creator(task_env):
    """归档后 assignee + creator 立即收到恢复指引（非 actor 除外）。"""
    from hiveweave.services.inbox import InboxService

    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Doomed work", "desc", creator_id=COORD, assignee_id=EXEC
    )
    inbox = InboxService()
    sent = {}

    async def fake_send(from_agent_id, to_agent_id, message, **kw):
        sent[to_agent_id] = (message, kw)

    with patch.object(inbox, "send_message", fake_send):
        with patch("hiveweave.services.inbox.InboxService", return_value=inbox):
            await ts.archive_task(
                pid, tid, archived_by="coord-other",
                reason="no longer needed",
                reason_code="agent_cancel",
            )

    assert EXEC in sent, "assignee must be notified"
    assert COORD in sent, "creator must be notified"
    msg, kw = sent[EXEC]
    assert "[TASK ARCHIVED]" in msg
    assert "恢复指引" in msg
    assert "create_task" in msg
    assert kw["message_type"] == "task_event"
    assert kw["wake"] is False
    assert kw["idempotency_key"].startswith("task_event:")


@pytest.mark.asyncio
async def test_archive_skips_archiver_actor_and_duplicate_guidance(task_env):
    """actor（archived_by）不给自己推；duplicate_cleanup 有专属指引。"""
    from hiveweave.services.inbox import InboxService

    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Verify dup", "desc", creator_id=COORD, assignee_id=EXEC
    )
    inbox = InboxService()
    sent = {}

    async def fake_send(from_agent_id, to_agent_id, message, **kw):
        sent[to_agent_id] = (message, kw)

    with patch.object(inbox, "send_message", fake_send):
        with patch("hiveweave.services.inbox.InboxService", return_value=inbox):
            await ts.archive_task(
                pid, tid, archived_by=EXEC,
                reason="duplicate VERIFY closed; sibling xyz succeeded",
                reason_code="duplicate_cleanup",
            )

    assert EXEC not in sent, "archiver (assignee) must not self-notify"
    assert COORD in sent
    msg, _ = sent[COORD]
    assert "重复" in msg
    assert "无需恢复" in msg


@pytest.mark.asyncio
async def test_archive_guidance_uses_relay_idempotency_key(task_env):
    """直接推送与 relay 用相同 idempotency key → relay 不会重复投递。"""
    from hiveweave.services.inbox import InboxService

    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Doomed", "desc", creator_id=COORD, assignee_id=EXEC
    )
    inbox = InboxService()
    keys = []

    async def fake_send(from_agent_id, to_agent_id, message, **kw):
        keys.append(kw["idempotency_key"])

    with patch.object(inbox, "send_message", fake_send):
        with patch("hiveweave.services.inbox.InboxService", return_value=inbox):
            await ts.archive_task(
                pid, tid, archived_by="coord-other",
                reason="r", reason_code="agent_cancel",
            )

    event_id = keys[0].split(":")[1]
    assert keys == [f"task_event:{event_id}:{EXEC}",
                    f"task_event:{event_id}:{COORD}"]


# ── ④ commit_turn 可行动化 ───────────────────────────────────


def test_tool_result_err_supports_extra_fields():
    """ToolResult.err 可携带结构化 extra（gates/actions）。"""
    from hiveweave.tools.result import ToolResult

    r = ToolResult.err("boom", gates=["X"], actions={"X": "do X"})
    assert r.success is False
    assert r.error == "boom"
    d = r.to_dict()
    assert d["gates"] == ["X"]
    assert d["actions"] == {"X": "do X"}


@pytest.mark.asyncio
async def test_commit_turn_rejected_message_has_numbered_steps(task_env):
    """REJECTED 消息 = 编号步骤清单 + 结构化 gates/actions。

    UNCOMMITTED_WORKTREE 是恒硬拒 gate（HARD_COMMIT_GATE_CODES），
    用 patch pre_check_exit_gates 构造该违规。"""
    from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

    with patch(
        "hiveweave.db.meta.get_agent_project_id",
        return_value=task_env["project_id"],
    ):
        with patch(
            "hiveweave.services.turn_exit.pre_check_exit_gates",
            return_value=["UNCOMMITTED_WORKTREE"],
        ):
            result = await commit_turn_tool(
                CommitTurnParams(
                    phase="done_slice",
                    summary="all done",
                    waiting_on=None,
                    result=None,
                    extensions=None,
                ),
                agent_id=EXEC,
                workspace=task_env["workspace"],
                ctx=None,
            )

    assert result.success is False
    assert "commit_turn REJECTED" in (result.error or "")
    assert "1)" in (result.error or "")
    assert "动作:" in (result.error or "")
    d = result.to_dict()
    assert "UNCOMMITTED_WORKTREE" in d["gates"]
    assert "git_worktree_checkpoint" in d["actions"]["UNCOMMITTED_WORKTREE"]


# ── ⑤ relay 状态推送补齐 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_relay_notifies_creator_for_claimed_running_blocked(task_env):
    """claimed/running/blocked/verifying 事件现在推给 creator（此前静默）。

    claimed 由 creator 亲自 claim 时 actor=creator → 被 actor 过滤跳过
    （正确，无需自我通知）；running 由 assignee 发起 → creator 收到。"""
    from hiveweave.services.org import OrgService
    from hiveweave.services.task_event_relay import TaskEventRelay
    from hiveweave.services.tasks.events import TaskEventService

    pid = task_env["project_id"]
    org = OrgService()
    await org.create_agent(
        {
            "id": COORD,
            "project_id": pid,
            "name": "Coord",
            "role": "coordinator",
            "permission_type": "coordinator",
            "status": "active",
        },
        bootstrap=True,
    )
    await org.create_agent(
        {
            "id": EXEC,
            "project_id": pid,
            "name": "Exec",
            "role": "executor",
            "permission_type": "executor",
            "status": "active",
            "parent_id": COORD,
        },
        bootstrap=True,
    )

    ts = TaskService()
    tid = await ts.create_task(
        pid, "Progress", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)

    events = await TaskEventService().get_undelivered(pid, limit=50)
    types = {e["event_type"] for e in events}
    assert "task.claimed" in types
    assert "task.running" in types

    inbox = TaskEventRelay()
    sent = {}

    async def fake_send(from_agent_id, to_agent_id, message, **kw):
        sent[(kw.get("task_id"), to_agent_id)] = (message, kw)

    from hiveweave.services.inbox import InboxService as IB

    real = IB()
    with patch.object(real, "send_message", fake_send):
        with patch("hiveweave.services.task_event_relay.InboxService", return_value=real):
            for ev in events:
                await inbox._process_one(pid, ev)

    # running（actor=exec-1）→ creator（coord-1）收到
    assert (tid, COORD) in sent, "creator must receive running"
    msgs = [m for (_, to), (m, _) in sent.items() if to == COORD]
    assert any("RUNNING" in m for m in msgs)
    # claimed 事件来自 create_task（assign=claim），actor=creator 自己 →
    # 被 actor 过滤正确跳过（无需自我通知）。
    assert not any("CLAIMED" in m for m in msgs), (
        "creator claiming own task must not self-notify"
    )

    # 各事件类型的 recipient 规则（直接测 _determine_recipients）
    relay = TaskEventRelay()
    for ev_type in ("task.claimed", "task.running", "task.blocked",
                    "task.verifying"):
        recips = await relay._determine_recipients(
            pid, ev_type, tid, actor_id=EXEC, payload={}
        )
        assert COORD in recips, f"{ev_type} must notify creator"
        assert EXEC not in recips, f"{ev_type} must skip actor"


@pytest.mark.asyncio
async def test_relay_archived_message_has_recovery_guidance(task_env):
    """relay 兜底路径的 archived 消息也带恢复指引。"""
    from hiveweave.services.task_event_relay import TaskEventRelay

    relay = TaskEventRelay()
    msg = relay._build_message(
        "task.archived",
        "abcdef1234567890",
        {"title": "Old work"},
    )
    assert "[TASK ARCHIVED]" in msg
    assert "恢复指引" in msg
    assert "create_task" in msg


@pytest.mark.asyncio
async def test_relay_message_carries_task_title(task_env):
    """relay 消息带任务标题（transition 事件 payload 为空，标题取自行）。"""
    from hiveweave.services.org import OrgService
    from hiveweave.services.task_event_relay import TaskEventRelay
    from hiveweave.services.tasks.events import TaskEventService

    pid = task_env["project_id"]
    org = OrgService()
    await org.create_agent(
        {
            "id": COORD,
            "project_id": pid,
            "name": "Coord",
            "role": "coordinator",
            "permission_type": "coordinator",
            "status": "active",
        },
        bootstrap=True,
    )
    await org.create_agent(
        {
            "id": EXEC,
            "project_id": pid,
            "name": "Exec",
            "role": "executor",
            "permission_type": "executor",
            "status": "active",
            "parent_id": COORD,
        },
        bootstrap=True,
    )

    ts = TaskService()
    tid = await ts.create_task(
        pid, "Titled Task", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.start_task(pid, tid)

    events = await TaskEventService().get_undelivered(pid, limit=50)
    relay = TaskEventRelay()
    for ev in events:
        if ev["event_type"] == "task.running":
            msg = relay._build_message(
                ev["event_type"], ev["task_id"], {},
                title=(await relay._get_task(pid, ev["task_id"]))["title"],
            )
            assert "Titled Task" in msg
            return
    raise AssertionError("task.running event not found")


@pytest.mark.asyncio
async def test_archive_direct_push_and_relay_are_idempotent(task_env):
    """直接推送后 relay 对 archived 跳过 → inbox 恰好 1 条（无竞态覆盖）。"""
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.org import OrgService
    from hiveweave.services.task import TaskEventService
    from hiveweave.services.task_event_relay import TaskEventRelay

    pid = task_env["project_id"]
    org = OrgService()
    await org.create_agent(
        {
            "id": COORD,
            "project_id": pid,
            "name": "Coord",
            "role": "coordinator",
            "permission_type": "coordinator",
            "status": "active",
        },
        bootstrap=True,
    )
    await org.create_agent(
        {
            "id": EXEC,
            "project_id": pid,
            "name": "Exec",
            "role": "executor",
            "permission_type": "executor",
            "status": "active",
            "parent_id": COORD,
        },
        bootstrap=True,
    )

    ts = TaskService()
    tid = await ts.create_task(
        pid, "Doomed", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.archive_task(
        pid, tid, archived_by="coord-other",
        reason="gone", reason_code="agent_cancel",
    )

    # relay 再处理同一归档事件：应跳过（close 已直推详指引）
    svc = TaskEventService()
    events = await svc.get_undelivered(pid, limit=50)
    archived_events = [e for e in events if e["event_type"] == "task.archived"]
    assert archived_events, "archive event must exist"
    relay = TaskEventRelay()
    for ev in archived_events:
        await relay._process_one(pid, ev)

    # 真实 inbox：EXEC（被归档任务 assignee）恰好 1 条
    bg = await InboxService().get_undelivered_background(EXEC)
    arch_msgs = [r for r in bg if "TASK ARCHIVED" in (r.get("message") or "")]
    assert len(arch_msgs) == 1, (
        f"expected exactly 1 archived notice, got {len(arch_msgs)}"
    )
    assert "恢复指引" in arch_msgs[0]["message"]
    assert "create_task" in arch_msgs[0]["message"]
    assert "废弃" in arch_msgs[0]["message"] or "重新创建" in arch_msgs[0]["message"]


# ── ⑥ 证据落点约定 ──────────────────────────────────────────


def test_identity_prompt_declares_official_evidence_location():
    """identity prompt 声明官方证据落点 .hiveweave/reports/<task-shortId>/。"""
    from hiveweave.prompts.identity import _SYSTEM_DIR_BLOCK

    assert "Official evidence location" in _SYSTEM_DIR_BLOCK
    assert ".hiveweave/reports/" in _SYSTEM_DIR_BLOCK
    assert "tool_outputs/" in _SYSTEM_DIR_BLOCK


def test_dispatch_verify_guidance_includes_evidence_location():
    """dispatch VERIFY GUIDANCE 注入证据落点约定。"""
    import inspect

    from hiveweave.services import dispatch as dispatch_mod

    src = inspect.getsource(dispatch_mod)
    assert "[EVIDENCE LOCATION]" in src
    assert ".hiveweave/reports/" in src
