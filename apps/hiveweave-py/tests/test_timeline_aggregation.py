"""Timeline 聚合聚焦测试（Timeline v4 §4.2 / §4.3 / §4.5）。

覆盖：
- 切段算法快照：rework 回环 / reassign / unclaim / blocked 保 assignee；
- 窗口裁剪与 ongoing 段；
- 末段校准仅对实时窗口生效（历史窗口 calibrate=False）；
- 团队活动端点：全局事件预算（truncated 时保留最新 + 窗口头部种子事件）、
  if_changed_since 短路契约、非法窗口 ValueError；
- 单任务事件流 happy path（真实写路径 create→start）；
- work_logs 预算（limit 保留最新、返回仍升序）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.timeline import TimelineService

PROJECT_ID = "test-timeline-agg"
COORD = "coord-1"
EXEC = "exec-1"


def _ev(
    ts: int,
    event_type: str,
    to_status: str,
    *,
    from_status: str | None = None,
    actor: str | None = None,
    payload: dict | None = None,
) -> dict:
    """构造 _segment_task 接受的事件行（dict 形态）。"""
    return {
        "created_at": ts,
        "event_type": event_type,
        "to_status": to_status,
        "from_status": from_status,
        "actor_id": actor,
        "payload": payload or {},
    }


def _task(**over) -> dict:
    base = {
        "id": "t1",
        "title": "T",
        "status": "running",
        "assignee_id": "a1",
        "creator_id": "c1",
        "reviewer_id": "r1",
        "created_at": 1000,
        "claimed_at": None,
        "closed_at": None,
        "archived_at": None,
        "updated_at": 9000,
        "is_archived": 0,
    }
    base.update(over)
    return base


# ── 切段算法：纯函数快照 ─────────────────────────────────────


def test_segment_full_lifecycle_with_rework():
    """rework 回环：reviewing→running 产生独立段，末段被校准。"""
    svc = TimelineService()
    events = [
        _ev(1100, "task.claimed", "claimed", actor="a1",
            payload={"assignee_id": "a1"}),
        _ev(2000, "task.running", "running", actor="a1"),
        _ev(3000, "task.submitted", "submitted", actor="a1"),
        _ev(3100, "task.reviewing", "reviewing", actor="r1"),
        _ev(4000, "task.running", "running", actor="a1",
            payload={"reason_code": "review_rework"}),
        _ev(5000, "task.submitted", "submitted", actor="a1"),
    ]
    segs = svc._segment_task(
        _task(status="running"), events, 0, 10_000, calibrate=True
    )
    got = [
        (s["status"], s["assignee_id"], s["started_at"], s["ended_at"])
        for s in segs
    ]
    assert got == [
        ("created", None, 1000, 1100),
        ("claimed", "a1", 1100, 2000),
        ("running", "a1", 2000, 3000),
        ("submitted", "a1", 3000, 3100),
        ("reviewing", "a1", 3100, 4000),  # review 不动 assignee 游标
        ("running", "a1", 4000, 5000),
        ("running", "a1", 5000, None),  # 末段校准回 tasks.status
    ]
    assert segs[-1]["ongoing"] is True


def test_segment_reassign_and_unclaim():
    """assignee 游标：reassigned → to_assignee；unclaim → None。"""
    svc = TimelineService()
    events = [
        _ev(1100, "task.claimed", "claimed", actor="a1",
            payload={"assignee_id": "a1"}),
        _ev(2000, "task.reassigned", "claimed",
            payload={"to_assignee": "a2"}),
        _ev(3000, "task.created", "created", from_status="claimed"),
    ]
    segs = svc._segment_task(
        _task(status="created", assignee_id=None),
        events, 0, 10_000, calibrate=True,
    )
    got = [
        (s["status"], s["assignee_id"], s["started_at"], s["ended_at"])
        for s in segs
    ]
    assert got == [
        ("created", None, 1000, 1100),
        ("claimed", "a1", 1100, 2000),
        ("claimed", "a2", 2000, 3000),
        ("created", None, 3000, None),
    ]


def test_segment_window_clipping():
    """窗口裁剪：窗外段丢弃，跨界段夹取，进行中段 ongoing。"""
    svc = TimelineService()
    events = [
        _ev(1100, "task.claimed", "claimed", actor="a1",
            payload={"assignee_id": "a1"}),
        _ev(2000, "task.running", "running", actor="a1"),
        _ev(8000, "task.submitted", "submitted", actor="a1"),
    ]
    # 窗口 [2500, 7000]：running 段 [2000,8000) 夹成 [2500,7000]
    segs = svc._segment_task(
        _task(status="submitted"), events, 2500, 7000, calibrate=True
    )
    assert len(segs) == 1
    s = segs[0]
    assert (s["status"], s["started_at"], s["ended_at"]) == (
        "running", 2500, 7000
    )
    assert s["ongoing"] is False  # 终点 8000 已知，只是被窗口截齐

    # 进行中的非终态任务：末段 ended_at=None + ongoing
    segs2 = svc._segment_task(
        _task(status="running"), events[:2], 0, 10_000, calibrate=True
    )
    assert segs2[-1]["ended_at"] is None
    assert segs2[-1]["ongoing"] is True


def test_segment_calibration_only_for_live_window():
    """历史窗口（calibrate=False）不得用 tasks 表当前值改写末段。"""
    svc = TimelineService()
    events = [
        _ev(1100, "task.claimed", "claimed", actor="a1",
            payload={"assignee_id": "a1"}),
        _ev(2000, "task.running", "running", actor="a1"),
    ]
    # tasks 表当前值：status=running / assignee=a2（比事件新）
    live = svc._segment_task(
        _task(assignee_id="a2"), events, 0, 10_000, calibrate=True
    )
    hist = svc._segment_task(
        _task(assignee_id="a2"), events, 0, 10_000, calibrate=False
    )
    assert live[-1]["assignee_id"] == "a2"   # 实时窗口校准
    assert hist[-1]["assignee_id"] == "a1"   # 历史窗口保持事件推导值


# ── 集成：真实 DB + 只读池 ───────────────────────────────────


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        from hiveweave.services.tasks import db as tasks_db

        task_module._migrated.discard(PROJECT_ID)
        tasks_db._migrated.discard(PROJECT_ID)

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
        # 只读池句柄也必须关：Windows 下 mode=ro 连接锁住 data.db，
        # TemporaryDirectory 清理会 PermissionError（close_all 在夹具之后）。
        try:
            await project_db._close_readonly_pool(workspace_path)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_task_timeline_end_to_end(env):
    """端点 1：真实写路径 create(assignee)→start 的事件回放。"""
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(pid, "Ship UI", "d", COORD, assignee_id=EXEC)
    await ts.start_task(pid, tid)

    res = await TimelineService().get_task_timeline(pid, tid, limit=500)
    assert res["task"]["id"] == tid
    assert res["truncated"] is False

    types = [e["type"] for e in res["events"]]
    # create+assign 直写 claimed（无独立 task.created 事件）或
    # create→claim 两事件，两种写路径都合法
    assert types[0] in ("task.created", "task.claimed")
    assert "task.running" in types
    # 升序 + 统一 schema 字段齐全
    tss = [e["ts"] for e in res["events"]]
    assert tss == sorted(tss)
    for e in res["events"]:
        for key in (
            "id", "ts", "type", "task_id", "from_status",
            "to_status", "title",
        ):
            assert key in e

    # 未知任务 → task=None（API 层转 404）
    missing = await TimelineService().get_task_timeline(pid, "nope")
    assert missing["task"] is None


@pytest.mark.asyncio
async def test_team_activity_truncated_keeps_newest_and_seeds(env):
    """端点 2：全局预算 truncated 时丢最旧、保最新，并补窗口头种子。"""
    from hiveweave.services.tasks.db import _ensure_schema, insert_task_event

    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(pid, "Seg", "d", COORD)  # 真事件 ts≈now
    await _ensure_schema(pid)

    # 把任务行回拨到合成时间轴（created_at=1000，窗口内可见）
    conn = await project_db.ensure_project_db(env["workspace_path"])
    await conn.execute(
        "UPDATE tasks SET created_at = 1000, status = 'running', "
        "assignee_id = 'a1' WHERE id = ?",
        [tid],
    )
    await conn.commit()

    # 合成事件（now_ms 受控）；1000/2000 会被 limit=3 挤掉
    for ts_ms, etype, fs, tos, payload in [
        (1000, "task.created", None, "created", None),
        (2000, "task.claimed", "created", "claimed",
         {"assignee_id": "a1"}),
        (4000, "task.submitted", "claimed", "submitted", None),
        (5000, "task.reviewing", "submitted", "reviewing", None),
        (6000, "task.running", "reviewing", "running",
         {"reason_code": "review_rework"}),
    ]:
        await insert_task_event(
            pid, tid, etype, fs, tos,
            actor_id="a1", payload=payload, now_ms=ts_ms,
        )

    svc = TimelineService()
    res = await svc.get_team_activity(
        pid, since_ms=3500, until_ms=7000, limit=3
    )
    assert res["changed"] is True
    assert res["truncated"] is True
    assert res["has_more_earlier"] is True
    assert res["window"] == {"since": 3500, "until": 7000}

    segs = [s for s in res["task_segments"] if s["task_id"] == tid]
    assert segs, "窗口内应有该任务的段"
    # 种子事件（claimed@2000）恢复了窗口头部：首段不是 created 而是
    # claimed/a1，且被夹到窗口起点 3500
    assert segs[0]["status"] == "claimed"
    assert segs[0]["assignee_id"] == "a1"
    assert segs[0]["started_at"] == 3500
    # 末段 = rework 后的 running，进行中
    assert segs[-1]["status"] == "running"
    assert segs[-1]["ongoing"] is True
    # truncated 丢最旧：窗口内不出现 reviewing 之前的无主 created 段
    assert all(s["status"] != "created" for s in segs)


@pytest.mark.asyncio
async def test_team_activity_if_changed_since_short_circuit(env):
    """if_changed_since 无变化 → O(1) 短路并回显窗口。"""
    ts = TaskService()
    pid = env["project_id"]
    await ts.create_task(pid, "A", "d", COORD)

    svc = TimelineService()
    res = await svc.get_team_activity(
        pid, since_ms=0, until_ms=10**13, if_changed_since=10**15
    )
    assert res == {
        "changed": False,
        "max_event_ts": res["max_event_ts"],
        "window": {"since": 0, "until": 10**13},
    }

    with pytest.raises(ValueError):
        await svc.get_team_activity(pid, since_ms=5000, until_ms=4000)


@pytest.mark.asyncio
async def test_work_logs_limit_keeps_newest_ascending(env):
    """work_logs 预算：limit 取最新，返回仍按升序。"""
    from hiveweave.services.dispatch import DispatchService

    pid = env["project_id"]
    conn = await project_db.ensure_project_db(env["workspace_path"])
    for i in range(5):
        await conn.execute(
            "INSERT INTO work_logs (id, agent_id, task_id, type, summary, "
            "created_at) VALUES (?, ?, ?, 'progress', ?, ?)",
            [f"wl-{i}", "a1", "t-x", f"log {i}", 1000 + i],
        )
    await conn.commit()

    ds = DispatchService()
    rows = await ds.get_work_logs_for_task(pid, "t-x", limit=2)
    assert [r["summary"] for r in rows] == ["log 3", "log 4"]
    full = await ds.get_work_logs_for_task(pid, "t-x")
    assert len(full) == 5
    assert [r["created_at"] for r in full] == sorted(
        r["created_at"] for r in full
    )


@pytest.mark.asyncio
async def test_task_timeline_single_source_overflow_flags_truncated(env):
    """端点 1：单源溢出必须 truncated=True（审计 P1-1 回归）。

    旧判法只看合并后 len(events) > limit：任务有 9 条 task_events、
    无其它来源时取最新 5 条，len==5 谎报未截断，最旧 4 条静默丢失。
    """
    from hiveweave.services.tasks.db import _ensure_schema, insert_task_event

    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(pid, "Flood", "d", COORD)
    await _ensure_schema(pid)

    # 合成 8 条同任务事件（+create 自身的 ≥1 条 = 单源 >5）
    for i in range(8):
        await insert_task_event(
            pid, tid, "task.progress", "running", "running",
            actor_id="a1", payload={"i": i}, now_ms=10_000 + i,
        )

    res = await TimelineService().get_task_timeline(pid, tid, limit=5)
    assert res["truncated"] is True
    assert len(res["events"]) == 5  # 回放保留近端


@pytest.mark.asyncio
async def test_patch_same_assignee_is_noop(env):
    """PATCH 同值 assignee 幂等：无状态副作用、无伪造事件（审计 P1-2 回归）。

    旧实现只判断字段是否出现：reviewing 任务重放同值 assignee 会被强制
    打回 claimed + progress 重置，并写入 from==to 的假 task.reassigned。
    """
    from hiveweave.api.tasks import TaskUpdate, update_task

    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(pid, "Patch", "d", COORD, assignee_id=EXEC)

    # 直接置成 reviewing（暴露旧代码的状态副作用）
    conn = await project_db.ensure_project_db(env["workspace_path"])
    await conn.execute(
        "UPDATE tasks SET status = 'reviewing' WHERE id = ?", [tid]
    )
    await conn.commit()

    res = await update_task(pid, tid, TaskUpdate(assigneeId=EXEC))
    assert res["success"] is True

    task_after = await ts.get_task(pid, tid)
    assert task_after["status"] == "reviewing"  # 未被打回 claimed
    assert task_after["assignee_id"] == EXEC

    tl = await TimelineService().get_task_timeline(pid, tid, limit=500)
    reassign_events = [
        e for e in tl["events"] if e["type"] == "task.reassigned"
    ]
    assert reassign_events == []  # 无伪造 reassigned 事件


@pytest.mark.asyncio
async def test_patch_empty_assignee_clears_via_update_not_reassign(env):
    """PATCH assigneeId=""（清空负责人）走 update_task 原路径而非 reassign_task。

    清空不是换人：reassign_task 对空 id 语义不适用（会校验失败或写脏数据）。
    审计 P1-1 回归——端点改道后不得破坏清空语义，也不得伪造事件。
    """
    from hiveweave.api.tasks import TaskUpdate, update_task

    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(pid, "PatchClear", "d", COORD, assignee_id=EXEC)

    res = await update_task(pid, tid, TaskUpdate(assigneeId=""))
    assert res["success"] is True

    tl = await TimelineService().get_task_timeline(pid, tid, limit=500)
    reassign_events = [
        e for e in tl["events"] if e["type"] == "task.reassigned"
    ]
    assert reassign_events == []  # 清空不产生 reassigned 事件


def test_work_log_echo_dedup_and_title():
    """平台转换回声 work_log（type='task_event'）去重与标题（视觉实测修复）。

    与 task_events 同状态 ±10s 的回声行丢弃（否则同一转换在事件流出现
    两行）；未被正式事件覆盖的回声保留且标题取 summary，不显示裸
    "task_event"；agent 手写日志标题逻辑不变（action 优先）。
    """
    from hiveweave.services.tasks.timeline import TimelineService

    svc = TimelineService()
    te_events = [{"to_status": "claimed", "ts": 1_000_000}]
    rows = [
        {"id": 1, "created_at": 1_000_500, "type": "task_event",
         "summary": "[claimed] task abc on assign", "action": None,
         "agent_id": "a1"},
        {"id": 2, "created_at": 2_000_000, "type": "task_event",
         "summary": "[running] task abc started", "action": None,
         "agent_id": "a1"},
        {"id": 3, "created_at": 3_000_000, "type": "note",
         "summary": "做了很多事", "action": "沉思", "agent_id": "a1"},
    ]
    out = svc._events_from_work_logs("t1", rows, te_events)
    assert [e["id"] for e in out] == [2, 3]  # 回声重复行被丢弃
    assert out[0]["title"] == "[running] task abc started"  # 不再裸 task_event
    assert out[1]["title"] == "沉思"  # 手写日志不受影响

    # 不传 task_events（兼容旧调用）：回声保留但标题仍是 summary
    out2 = svc._events_from_work_logs("t1", rows[:1])
    assert len(out2) == 1
    assert out2[0]["title"] == "[claimed] task abc on assign"
