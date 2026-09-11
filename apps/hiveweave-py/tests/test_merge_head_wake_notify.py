"""duty 模型增强第二部分：新 HEAD 合流复核提醒（41 轮 QA 空等 HEAD）。

- ``_wake_dependent_tasks(merge_commit=...)`` → [DEPENDENCY MET] 升级为
  「依赖已合流进 MAIN（HEAD <sha8>），请基于新 HEAD 复核/重验后继续」；
  默认 None（review.py:149 / close.py:92 零改动路径）文案保持现状。
- ``reconcile_blocked_tasks`` 兜底：deps 满足驱动的解封从 deps 最近
  ``task.merged`` 事件取 merge_commit → 文案带 HEAD；取不到 → 现状文案。
- creator FYI：wake=False（解封 = 创建者无需行动，非职责信号），
  同一 commit 只提醒一次（显式 idempotency_key 只投一次契约，真库验证）。
- obligation merge fulfill：merge_commit 透传到解封通知。
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services.agent_router import AgentRoute, agent_router
from hiveweave.services.obligation import ObligationLedger
from hiveweave.services.task import TaskService, _execute
from hiveweave.services.tasks.db import insert_task_event

PROJECT_ID = "test-merge-head-wake"
CREATOR = "mh-creator-1"
WORKER = "mh-worker-1"

COMMIT = "abcdef1234567890"
COMMIT2 = "0123456789abcdef"


@pytest.fixture
async def env():
    """真实 per-project DB + agent_router 注册（对齐
    test_wait_notify_clears_agent_wait.py，真 inbox 可写）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        from hiveweave.services import inbox as inbox_mod
        from hiveweave.services import task as task_mod

        task_mod._migrated.clear()
        for aid in (CREATOR, WORKER):
            inbox_mod._migrated.clear()
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            await project_db.ensure_project_db(workspace_path)
            yield {
                "project_id": PROJECT_ID,
                "workspace": workspace_path,
            }

        agent_router.clear_project(PROJECT_ID)
        for aid in (CREATOR, WORKER):
            project_db._agent_cache.pop(aid, None)
            inbox_mod._migrated.clear()
        task_mod._migrated.clear()
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _seed_agent(env, agent_id: str, role: str) -> None:
    pid = env["project_id"]
    ws = env["workspace"]
    now = int(time.time() * 1000)
    from hiveweave.db.project import execute_by_project

    await execute_by_project(
        pid,
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        [agent_id, agent_id[:2].upper(), pid, agent_id, role, now, now],
    )
    agent_router.register(
        AgentRoute(
            agent_id=agent_id,
            project_id=pid,
            workspace_path=ws,
            short_id=agent_id[:2].upper(),
            name=agent_id,
            role=role,
            status="active",
        )
    )
    project_db._agent_cache[agent_id] = ws


@pytest.fixture(autouse=True)
async def team(env):
    await _seed_agent(env, CREATOR, "coordinator")
    await _seed_agent(env, WORKER, "executor")


async def _make_blocked_dependent(env, blocker_id: str) -> str:
    """creator=CREATOR / assignee=WORKER 的 blocked 任务，depends_on=blocker。"""
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(
        pid, "等待合流的任务", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    await ts.claim_task(pid, tid, WORKER)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "等依赖合流", depends_on_task_id=blocker_id)
    return tid


async def _set_status(env, tid: str, status: str) -> None:
    """直改状态（绕过 review 流程——approve 会经由 review.py:149 触发无
    commit 的 wake，抢先解封被测任务）。"""
    await _execute(
        env["project_id"],
        f"UPDATE tasks SET status = '{status}', updated_at = ? WHERE id = ?",
        [int(time.time() * 1000), tid],
    )


def _msg(call) -> str:
    """send_message 的 message 实参（位置或关键字两种形态）。"""
    if call.kwargs.get("message"):
        return str(call.kwargs["message"])
    return str(call.args[2]) if len(call.args) > 2 else ""


async def _inbox_rows(env, to_agent: str, key_prefix: str) -> list:
    conn = await project_db.ensure_project_db(env["workspace"])
    cur = await conn.execute(
        "SELECT id, message, wake, idempotency_key, read FROM inbox "
        "WHERE to_agent_id = ? AND idempotency_key LIKE ?",
        [to_agent, key_prefix + "%"],
    )
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def _inbox_rows_by_message(env, to_agent: str, needle: str) -> list:
    conn = await project_db.ensure_project_db(env["workspace"])
    cur = await conn.execute(
        "SELECT id, message, wake, idempotency_key FROM inbox "
        "WHERE to_agent_id = ? AND message LIKE ?",
        [to_agent, f"%{needle}%"],
    )
    rows = await cur.fetchall()
    await cur.close()
    return rows


# ── 1. merge_commit 传入 → HEAD 文案 + assignee 通知到达 ────


@pytest.mark.asyncio
async def test_wake_with_merge_commit_upgrades_text(env):
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    tid = await _make_blocked_dependent(env, blocker)
    await _set_status(env, blocker, "approved")

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()
        ),
    ):
        await ts._wake_dependent_tasks(pid, blocker, merge_commit=COMMIT)

    assert (await ts.get_task(pid, tid))["status"] == "running"
    met = [
        c for c in send.await_args_list
        if "[DEPENDENCY MET]" in _msg(c)
    ]
    assert met, send.await_args_list
    msg = _msg(met[0])
    assert WORKER == met[0].args[1]     # to_agent_id（位置实参）
    assert "abcdef12" in msg            # HEAD 前 8 位
    assert "新 HEAD" in msg             # 复核/重验语义
    assert "Continue work or submit_task." not in msg
    # 审计 D：显式同键（与 obligation 实现共用），防文案漂移双发
    assert met[0].kwargs.get("idempotency_key", "").startswith(
        f"dep-met:{tid}:{blocker}:abcdef123456"
    )
    # creator FYI 同步发出：wake=False + commit 前缀幂等键
    fyi = [
        c for c in send.await_args_list
        if "[DEPS MERGED FYI]" in _msg(c)
    ]
    assert fyi, send.await_args_list
    assert fyi[0].args[1] == CREATOR    # to_agent_id（位置实参）
    assert fyi[0].kwargs.get("wake") is False
    assert fyi[0].kwargs.get("idempotency_key", "").startswith(
        f"deps-merged-fyi:{tid}:abcdef123456"
    )


# ── 2. 默认 None → 文案保持现状（回归）──────────────────────


@pytest.mark.asyncio
async def test_wake_default_none_keeps_legacy_text(env):
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    tid = await _make_blocked_dependent(env, blocker)
    await _set_status(env, blocker, "approved")

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()
        ),
    ):
        # 不传 merge_commit —— review.py:149 / close.py:92 的调用形态
        await ts._wake_dependent_tasks(pid, blocker)

    assert (await ts.get_task(pid, tid))["status"] == "running"
    met = [
        c for c in send.await_args_list
        if "[DEPENDENCY MET]" in _msg(c)
    ]
    assert met
    msg = _msg(met[0])
    assert "Continue work or submit_task." in msg   # 现状文案
    assert "新 HEAD" not in msg and "HEAD" not in msg


# ── 3. reconcile 兜底从 task.merged 事件取 commit ───────────


@pytest.mark.asyncio
async def test_reconcile_fallback_uses_task_merged_event(env):
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    tid = await _make_blocked_dependent(env, blocker)
    await _set_status(env, blocker, "approved")
    await insert_task_event(
        pid, blocker, "task.merged", None, None,
        payload={"merge_commit": COMMIT2, "target_branch": "main"},
    )

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()
        ),
    ):
        woken = await ts.reconcile_blocked_tasks(pid)

    assert woken == 1
    assert (await ts.get_task(pid, tid))["status"] == "running"
    rec = [
        c for c in send.await_args_list
        if "[BLOCKED RECONCILED]" in _msg(c)
    ]
    assert rec
    msg = _msg(rec[0])
    assert "01234567" in msg              # task.merged 事件的 HEAD 前 8 位
    assert "新 HEAD" in msg


@pytest.mark.asyncio
async def test_reconcile_without_merged_event_keeps_legacy_text(env):
    """无 task.merged 事件（如非 git 交付）→ 现状文案，不编造 HEAD。"""
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    tid = await _make_blocked_dependent(env, blocker)
    await _set_status(env, blocker, "approved")

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()
        ),
    ):
        await ts.reconcile_blocked_tasks(pid)

    rec = [
        c for c in send.await_args_list
        if "[BLOCKED RECONCILED]" in _msg(c)
    ]
    assert rec
    msg = _msg(rec[0])
    assert "depends_on_met" in msg and "Continue or submit_task." in msg
    assert "新 HEAD" not in msg


# ── 4. creator FYI wake=False + 真 inbox 幂等 ───────────────


@pytest.mark.asyncio
async def test_creator_fyi_wake_false_and_idempotent_per_commit(env):
    """同 commit 第二次解封：creator FYI 不重发（显式幂等键只投一次）。"""
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    tid = await _make_blocked_dependent(env, blocker)
    await _set_status(env, blocker, "approved")

    # 第一次解封（真 inbox，不 patch）
    await ts._wake_dependent_tasks(pid, blocker, merge_commit=COMMIT)
    rows = await _inbox_rows(env, CREATOR, "deps-merged-fyi:")
    assert len(rows) == 1
    assert rows[0]["wake"] == 0                      # FYI，非职责信号
    assert "abcdef12" in rows[0]["message"]
    # assignee 通知到达且恰好一条
    met = await _inbox_rows(env, WORKER, "%")
    assert sum(1 for r in met if "[DEPENDENCY MET]" in r["message"]) == 1

    # 重新 block（同一 commit 再次满足）→ 第二次解封
    await _set_status(env, tid, "blocked")
    await ts._wake_dependent_tasks(pid, blocker, merge_commit=COMMIT)
    rows = await _inbox_rows(env, CREATOR, "deps-merged-fyi:")
    assert len(rows) == 1                            # 幂等：不再发第二条

    # 不同 commit → 新提醒（键含 commit 前缀）
    await _set_status(env, tid, "blocked")
    await ts._wake_dependent_tasks(pid, blocker, merge_commit=COMMIT2)
    rows = await _inbox_rows(env, CREATOR, "deps-merged-fyi:")
    assert len(rows) == 2
    assert any("01234567" in r["message"] for r in rows)


@pytest.mark.asyncio
async def test_creator_fyi_skipped_when_creator_is_assignee(env):
    """creator == assignee：assignee 已收 [DEPENDENCY MET]，不再发 FYI。"""
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=CREATOR
    )
    tid = await ts.create_task(
        pid, "自给自足", "d", creator_id=CREATOR, assignee_id=CREATOR
    )
    await ts.claim_task(pid, tid, CREATOR)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "等依赖", depends_on_task_id=blocker)
    await _set_status(env, blocker, "approved")

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()
        ),
    ):
        await ts._wake_dependent_tasks(pid, blocker, merge_commit=COMMIT)

    fyi = [
        c for c in send.await_args_list
        if "[DEPS MERGED FYI]" in _msg(c)
    ]
    assert not fyi


# ── 5. obligation merge fulfill 透传 commit ─────────────────


@pytest.mark.asyncio
async def test_obligation_merge_fulfill_passes_commit(env):
    """obligation.py fulfill("merge", merge_commit=...) → 依赖解封通知带
    HEAD；这是唯一知道 merge commit 的路径（misc_tools.py 传 hash）。"""
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    tid = await _make_blocked_dependent(env, blocker)
    await _set_status(env, blocker, "approved")
    await ObligationLedger().create(
        pid, CREATOR, "merge", task_id=blocker,
        context={"reason": "test", "source": "unit"},
    )

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()
        ),
    ):
        n = await ObligationLedger().fulfill(
            pid, blocker, "merge", merge_commit=COMMIT
        )

    assert n == 1
    assert (await ts.get_task(pid, tid))["status"] == "running"
    met = [
        c for c in send.await_args_list
        if "[DEPENDENCY MET]" in _msg(c)
    ]
    assert met, send.await_args_list
    msg = _msg(met[0])
    assert "abcdef12" in msg and "新 HEAD" in msg
    # 审计 D：obligation 路径同键（与 lifecycle 实现互斥双发）
    assert met[0].kwargs.get("idempotency_key", "").startswith(
        f"dep-met:{tid}:{blocker}:abcdef123456"
    )
    # creator FYI 也到达
    fyi = [
        c for c in send.await_args_list
        if "[DEPS MERGED FYI]" in _msg(c)
    ]
    assert fyi and fyi[0].kwargs.get("wake") is False


@pytest.mark.asyncio
async def test_obligation_merge_fulfill_without_commit_legacy_text(env):
    """fulfill 不带 merge_commit（close 安全网 / waive 等旧调用形态）→
    现状文案，行为不回归。"""
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    tid = await _make_blocked_dependent(env, blocker)
    await _set_status(env, blocker, "approved")
    await ObligationLedger().create(
        pid, CREATOR, "merge", task_id=blocker,
        context={"reason": "test", "source": "unit"},
    )

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()
        ),
    ):
        await ObligationLedger().fulfill(pid, blocker, "merge")

    assert (await ts.get_task(pid, tid))["status"] == "running"
    met = [
        c for c in send.await_args_list
        if "[DEPENDENCY MET]" in _msg(c)
    ]
    assert met
    msg = _msg(met[0])
    assert "Continue work or submit_task." in msg
    assert "新 HEAD" not in msg


# ── 审计修复回归 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_obligation_context_commit_used_without_explicit_commit(env):
    """审计 P1-A：义务 context_json 带 merge_commit 且 fulfill 不显式传
    merge_commit → context 扫描兜底生效，文案带 HEAD（证明扫描非死代码）。"""
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    tid = await _make_blocked_dependent(env, blocker)
    await _set_status(env, blocker, "approved")
    await ObligationLedger().create(
        pid, CREATOR, "merge", task_id=blocker,
        context={"reason": "test", "source": "unit", "merge_commit": COMMIT},
    )

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()
        ),
    ):
        await ObligationLedger().fulfill(pid, blocker, "merge")  # 不传 commit

    assert (await ts.get_task(pid, tid))["status"] == "running"
    met = [
        c for c in send.await_args_list
        if "[DEPENDENCY MET]" in _msg(c)
    ]
    assert met, send.await_args_list
    msg = _msg(met[0])
    assert "abcdef12" in msg and "新 HEAD" in msg


@pytest.mark.asyncio
async def test_reconcile_skips_empty_commit_uses_older_event(env):
    """审计 P1-B：最新 task.merged 事件 commit 为空串（payload 写入形态
    str(merge_commit or "")）→ 取较旧事件的非空 commit，不整体回退 None。"""
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    tid = await _make_blocked_dependent(env, blocker)
    await _set_status(env, blocker, "approved")
    now = int(time.time() * 1000)
    # 旧事件带真 commit，新事件 commit 空（如 already_up_to_date 短路写入）
    await insert_task_event(
        pid, blocker, "task.merged", None, None,
        payload={"merge_commit": COMMIT2}, now_ms=now - 1000,
    )
    await insert_task_event(
        pid, blocker, "task.merged", None, None,
        payload={"merge_commit": ""}, now_ms=now + 2000,
    )

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()
        ),
    ):
        await ts.reconcile_blocked_tasks(pid)

    rec = [
        c for c in send.await_args_list
        if "[BLOCKED RECONCILED]" in _msg(c)
    ]
    assert rec
    msg = _msg(rec[0])
    assert "01234567" in msg and "新 HEAD" in msg


@pytest.mark.asyncio
async def test_creator_fyi_none_commit_wording(env):
    """审计 P2-C：merge_commit=None（review/close 路径 deps 只是
    approved/closed）→ FYI 说「依赖已完成」，不冒认「合流」、不带 HEAD。"""
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    # 标题避开「合流」字样，防止污染「不冒认合流」的断言
    tid = await ts.create_task(
        pid, "等待上游的任务", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    await ts.claim_task(pid, tid, WORKER)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "等依赖", depends_on_task_id=blocker)
    await _set_status(env, blocker, "approved")

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate", new=AsyncMock()
        ),
    ):
        await ts._wake_dependent_tasks(pid, blocker)  # 无 commit

    fyi = [
        c for c in send.await_args_list
        if "[DEPS MERGED FYI]" in _msg(c)
    ]
    assert fyi, send.await_args_list
    msg = _msg(fyi[0])
    assert "依赖已完成" in msg and "自动解封" in msg
    assert "合流" not in msg
    assert "HEAD" not in msg


@pytest.mark.asyncio
async def test_dependency_met_explicit_key_shared_across_paths(env):
    """审计 D：obligation 与 lifecycle 双路径同一 (task, blocker, commit)
    用同一显式 idempotency_key —— 真 inbox 两次投递只落一条。"""
    ts = TaskService()
    pid = env["project_id"]
    blocker = await TaskService().create_task(
        pid, "Blocker", "d", creator_id=CREATOR, assignee_id=WORKER
    )
    tid = await _make_blocked_dependent(env, blocker)
    await _set_status(env, blocker, "approved")
    await ObligationLedger().create(
        pid, CREATOR, "merge", task_id=blocker,
        context={"reason": "test", "source": "unit"},
    )

    # 路径 1：obligation merge fulfill 唤醒（真 inbox）
    await ObligationLedger().fulfill(
        pid, blocker, "merge", merge_commit=COMMIT
    )
    rows = await _inbox_rows_by_message(env, WORKER, "[DEPENDENCY MET]")
    assert len(rows) == 1
    assert rows[0]["idempotency_key"].startswith(f"dep-met:{tid}:{blocker}:")

    # 路径 2：同 commit 下 lifecycle 唤醒（同键 → 判重，不落第二条）
    await _set_status(env, tid, "blocked")
    await ts._wake_dependent_tasks(pid, blocker, merge_commit=COMMIT)
    rows = await _inbox_rows_by_message(env, WORKER, "[DEPENDENCY MET]")
    assert len(rows) == 1
