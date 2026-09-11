"""VERIFY 时长比监控（verify_efficiency）回归。

口径：docs/2026-09-05/verify-efficiency-metric.md。真实 per-project DB
（写路径造数 + readonly mode=ro 读），覆盖：正常比值/flag、有效为 0（∞）、
改派两段都计、非 VERIFY 不进报告、limit 生效。
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.verify_efficiency import verify_efficiency_report

PROJECT_ID = "test-verify-efficiency"
COORD = "coord-1"
EXEC1 = "exec-1"
EXEC2 = "exec-2"

MIN_MS = 60 * 1000


@pytest.fixture
async def task_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        from hiveweave.services.tasks import db as tasks_db

        task_module._migrated.clear()
        tasks_db._migrated.clear()
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        # 只读池句柄也必须关：Windows 下 mode=ro 连接锁住 data.db，
        # TemporaryDirectory 清理会 PermissionError（同 test_timeline_aggregation）。
        try:
            await project_db._close_readonly_pool(workspace_path)
        except Exception:
            pass


async def _conn(env):
    return await project_db.ensure_project_db(env["workspace"])


async def _set_clock(env, task_id, *, created_at, claimed_at, closed_at):
    conn = await _conn(env)
    await conn.execute(
        "UPDATE tasks SET created_at = ?, claimed_at = ?, closed_at = ? "
        "WHERE id = ?",
        [created_at, claimed_at, closed_at, task_id],
    )
    await conn.commit()


async def _add_run(env, agent_id, status, started_at, ended_at):
    conn = await _conn(env)
    await conn.execute(
        "INSERT INTO agent_runs (id, agent_id, status, started_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), agent_id, status, started_at, ended_at],
    )
    await conn.commit()


async def _spawn_closed_verify(
    env, ts, *, title="VERIFY: UI", claimer=EXEC1, t0=1_000_000_000_000
):
    """造一个 closed VERIFY：真实生命周期 approve（自动 close）→ 改钟控制窗口。"""
    pid = env["project_id"]
    # VERIFY: 前缀是 system 保留（伪造门 crud.py），系统 spawn 走 source="system"
    tid = await ts.create_task(
        pid, title, "verify it", creator_id=COORD, source="system"
    )
    await ts.claim_task(pid, tid, claimer)  # created → claimed（写 claimed_at + 事件）
    await ts.start_task(pid, tid)
    await ts.submit_task(pid, tid, evidence={"verdict": "PASS"})
    await ts.start_review(pid, tid, reviewer_id=COORD)
    # reviewing → approved；VERIFY approve 自动 close（review.py _close_verify_and_parent）
    await ts.review_task(pid, tid, "approve", reviewer_id=COORD)
    await _set_clock(
        env,
        tid,
        created_at=t0,
        claimed_at=t0 + 60 * MIN_MS,
        closed_at=t0 + 360 * MIN_MS,
    )
    return tid


@pytest.mark.asyncio
async def test_normal_ratio_and_flag(task_env):
    """60min run / 360min 墙钟 → ratio=6.0，flag ratio_gt_3。"""
    env = task_env
    ts = TaskService()
    t0 = 1_000_000_000_000
    tid = await _spawn_closed_verify(env, ts, t0=t0)
    await _add_run(
        env, EXEC1, "completed", t0 + 60 * MIN_MS, t0 + 120 * MIN_MS
    )  # 60min 有效

    rep = await verify_efficiency_report(env["project_id"])
    assert rep["count"] == 1
    row = rep["tasks"][0]
    assert row["task_id"] == tid
    assert row["total_minutes"] == 360.0
    assert row["effective_minutes"] == 60.0
    assert row["ratio"] == 6.0
    assert row["ratio_display"] == "6.00"
    assert "ratio_gt_3" in row["stale_flags"]
    assert row["assignees"] == [EXEC1]


@pytest.mark.asyncio
async def test_zero_effective_activity_is_infinity(task_env):
    """无 run → ratio=None / "∞" / flag no_effective_activity。"""
    env = task_env
    ts = TaskService()
    await _spawn_closed_verify(env, ts)

    rep = await verify_efficiency_report(env["project_id"])
    row = rep["tasks"][0]
    assert row["effective_minutes"] == 0.0
    assert row["ratio"] is None
    assert row["ratio_display"] == "∞"
    assert "no_effective_activity" in row["stale_flags"]


@pytest.mark.asyncio
async def test_error_runs_excluded_and_running_clamped(task_env):
    """status=error 的 run 不计；残留 running clamp 到窗口终点。"""
    env = task_env
    ts = TaskService()
    t0 = 2_000_000_000_000
    tid = await _spawn_closed_verify(env, ts, title="VERIFY: api", t0=t0)
    await _add_run(
        env, EXEC1, "error", t0 + 60 * MIN_MS, t0 + 120 * MIN_MS
    )  # 排除
    await _add_run(
        env, EXEC1, "running", t0 + 90 * MIN_MS, t0 + 999 * MIN_MS
    )  # clamp 到 closed_at=360min → 270min 有效

    rep = await verify_efficiency_report(env["project_id"])
    row = rep["tasks"][0]
    assert row["task_id"] == tid
    assert row["effective_minutes"] == 270.0
    assert row["total_minutes"] == 360.0
    assert row["ratio"] == round(360.0 / 270.0, 2)


@pytest.mark.asyncio
async def test_reassign_counts_both_segments(task_env):
    """改派：前后两任 assignee 的 run 都计，reassigned flag。"""
    env = task_env
    ts = TaskService()
    t0 = 3_000_000_000_000
    pid = env["project_id"]
    tid = await ts.create_task(
        pid, "VERIFY: rework", "v", creator_id=COORD, source="system"
    )
    await ts.claim_task(pid, tid, EXEC1)
    await ts.reassign_task(
        pid, tid, new_assignee_id=EXEC2, reassigned_by=COORD, reason="stall"
    )
    await ts.start_task(pid, tid)
    await ts.submit_task(pid, tid, evidence={"verdict": "PASS"})
    await ts.start_review(pid, tid, reviewer_id=COORD)
    await ts.review_task(pid, tid, "approve", reviewer_id=COORD)
    await _set_clock(
        env,
        tid,
        created_at=t0,
        claimed_at=t0 + 60 * MIN_MS,
        closed_at=t0 + 360 * MIN_MS,
    )
    await _add_run(
        env, EXEC1, "completed", t0 + 60 * MIN_MS, t0 + 90 * MIN_MS
    )  # 第一段 30min
    await _add_run(
        env, EXEC2, "completed", t0 + 200 * MIN_MS, t0 + 230 * MIN_MS
    )  # 第二段 30min

    rep = await verify_efficiency_report(env["project_id"])
    row = rep["tasks"][0]
    assert row["effective_minutes"] == 60.0
    assert row["ratio"] == 6.0
    assert row["assignees"] == [EXEC1, EXEC2]
    assert row["reassignments"] == 1
    assert "reassigned" in row["stale_flags"]
    assert "ratio_gt_3" in row["stale_flags"]


@pytest.mark.asyncio
async def test_open_ended_run_clamped_to_window_end(task_env):
    """残留 running（ended_at=NULL）clamp 到 closed_at，不虚计也不记 0。"""
    env = task_env
    ts = TaskService()
    t0 = 5_000_000_000_000
    tid = await _spawn_closed_verify(
        env, ts, title="VERIFY: orphan run", t0=t0
    )
    conn = await _conn(env)
    await conn.execute(
        "INSERT INTO agent_runs (id, agent_id, status, started_at, ended_at) "
        "VALUES (?, ?, 'running', ?, NULL)",
        [str(uuid.uuid4()), EXEC1, t0 + 150 * MIN_MS],
    )
    await conn.commit()

    rep = await verify_efficiency_report(env["project_id"])
    row = rep["tasks"][0]
    assert row["task_id"] == tid
    # 150min → closed_at 360min = 210min 有效（clamp 到窗口终点）
    assert row["effective_minutes"] == 210.0
    assert row["ratio"] == round(360.0 / 210.0, 2)


@pytest.mark.asyncio
async def test_null_ended_run_started_before_window(task_env):
    """P1-1：NULL-ended 且 started 早于窗口起点 → SQL 预过滤不得丢行，
    clamp 满窗贡献（claimed_at→closed_at 全长）。"""
    env = task_env
    ts = TaskService()
    t0 = 6_000_000_000_000
    tid = await _spawn_closed_verify(
        env, ts, title="VERIFY: early orphan", t0=t0
    )
    conn = await _conn(env)
    # started=+10min 早于 claimed_at=+60min，ended_at=NULL
    await conn.execute(
        "INSERT INTO agent_runs (id, agent_id, status, started_at, ended_at) "
        "VALUES (?, ?, 'running', ?, NULL)",
        [str(uuid.uuid4()), EXEC1, t0 + 10 * MIN_MS],
    )
    await conn.commit()

    rep = await verify_efficiency_report(env["project_id"])
    row = rep["tasks"][0]
    assert row["task_id"] == tid
    # clamp 满窗：+60min → +360min = 300min 有效（cap 到窗口长度）
    assert row["effective_minutes"] == 300.0
    assert row["total_minutes"] == 360.0
    assert row["ratio"] == 1.2
    assert "ratio_gt_3" not in row["stale_flags"]


@pytest.mark.asyncio
async def test_clock_backwards_window_is_anomalous(task_env):
    """P2-2：closed_at < created_at（时钟倒挂）→ anomalous_window，
    effective=0，不得落进 ratio=0.00 无 flag 的假象。"""
    env = task_env
    ts = TaskService()
    t0 = 7_000_000_000_000
    tid = await _spawn_closed_verify(
        env, ts, title="VERIFY: clock skew", t0=t0
    )
    conn = await _conn(env)
    await conn.execute(
        "UPDATE tasks SET created_at = ?, closed_at = ? WHERE id = ?",
        [t0, t0 - 60 * MIN_MS, tid],  # closed 早于 created
    )
    await conn.commit()

    rep = await verify_efficiency_report(env["project_id"])
    row = rep["tasks"][0]
    assert row["task_id"] == tid
    assert "anomalous_window" in row["stale_flags"]
    assert row["effective_minutes"] == 0.0
    assert row["ratio"] is None


@pytest.mark.asyncio
async def test_scanned_and_truncated_observability(task_env):
    """P2-1：扫描触顶 _SCAN_LIMIT 且过滤后不足 limit → truncated=true。"""
    env = task_env
    ts = TaskService()
    pid = env["project_id"]
    # 无 assignee → created 态（created→closed 合法；claimed→closed 非法转移）
    plain = await ts.create_task(pid, "Plain task", "d", creator_id=COORD)
    await ts.close_task(pid, plain, skip_merge_gate=True)
    await _spawn_closed_verify(env, ts, title="VERIFY: only-one")

    with patch(
        "hiveweave.services.tasks.verify_efficiency._SCAN_LIMIT", 2
    ):
        rep = await verify_efficiency_report(pid, limit=5)
    # scanned=2（触顶）、过滤后仅 1 条 VERIFY < limit=5 → truncated
    assert rep["scanned"] == 2
    assert rep["truncated"] is True
    assert rep["count"] == 1

    with patch(
        "hiveweave.services.tasks.verify_efficiency._SCAN_LIMIT", 2
    ):
        rep2 = await verify_efficiency_report(pid, limit=1)
    # 过滤后 >= limit（取满 1 条）→ 报告按定义完整，不算截断
    assert rep2["truncated"] is False


@pytest.mark.asyncio
async def test_non_verify_and_limit(task_env):
    """非 VERIFY closed 任务不进报告；limit 只取最近 N 条。"""
    env = task_env
    ts = TaskService()
    pid = env["project_id"]
    # 非 VERIFY closed 任务（assign=claim 直达 claimed；close 需过 merge gate → skip）
    plain = await ts.create_task(
        pid, "Fix bug", "d", creator_id=COORD, assignee_id=EXEC1
    )
    await ts.start_task(pid, plain)
    await ts.submit_task(pid, plain, evidence={"tests_passed": True})
    await ts.start_review(pid, plain, reviewer_id=COORD)
    await ts.review_task(pid, plain, "approve", reviewer_id=COORD)
    await ts.close_task(pid, plain, skip_merge_gate=True)
    # 三条 closed VERIFY，closed_at 递增
    tids = []
    for i in range(3):
        t0 = 4_000_000_000_000 + i * 1_000_000
        tids.append(
            await _spawn_closed_verify(
                env, ts, title=f"VERIFY: item{i}", t0=t0
            )
        )

    rep = await verify_efficiency_report(pid)
    assert rep["count"] == 3
    assert plain not in [r["task_id"] for r in rep["tasks"]]
    assert all(r["title"].startswith("VERIFY:") for r in rep["tasks"])

    rep2 = await verify_efficiency_report(pid, limit=2)
    assert rep2["count"] == 2
    # 按 closed_at DESC：最近两条 = item2, item1
    assert [r["task_id"] for r in rep2["tasks"]] == [tids[2], tids[1]]


@pytest.mark.asyncio
async def test_empty_project_report(task_env):
    """无 closed VERIFY → count=0（scanned/truncated 可观测位归零）。"""
    env = task_env
    rep = await verify_efficiency_report(env["project_id"])
    assert rep == {
        "project_id": env["project_id"],
        "count": 0,
        "tasks": [],
        "scanned": 0,
        "truncated": False,
    }
