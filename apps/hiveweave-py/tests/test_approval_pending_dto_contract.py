"""FE-08 / SR-05：审批待办 DTO 契约测试（防前后端字段命名漂移）。

背景（前端美化方案 §11.4）：后端待审批查询返回 **snake_case**（SQLite 列名
经 ``dict(r)`` 原样透传，FastAPI 层不做字段改名），而前端曾按 camelCase 读取
⇒ 真实载荷下前端读到 undefined；前端 mock 全是理想化 camelCase 所以测试全绿。

权威侧取舍（FE-08 二选一）：**后端保持 snake_case**（Python/FastAPI 惯例 +
DB 列名即契约），**前端在 apps/web/src/api/rest.ts 归一层做一次 snake→camel
映射**（normalizePendingApproval / PENDING_APPROVAL_SOURCE_FIELDS）。

本文件与前端归一单测 ``apps/web/src/api/approvals.test.ts`` 各自写死**同一份
字段名清单**（逐字一致）：
- 后端改 SELECT 列名 / permission_requests 表结构 ⇒ 本文件红；
- 前端归一层期望漂移 ⇒ 前端测试红。
契约不可能被任何一边静默改掉。

另锁（§11.4 末条）：resolve_request 返回明确状态，重复提交幂等
（第二次提交返回 "already_resolved"，不重复写 DB）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

from hiveweave.api.permissions import (
    RespondBodyCompat,
    pending_for_agent,
    pending_for_project_path,
    respond_request_compat,
)
from hiveweave.db import meta as meta_db
from hiveweave.db.project import close_all, ensure_project_db
from hiveweave.services.agent_router import agent_router
from hiveweave.services.approval import _PendingEntry, approval_service

# ── 冻结契约：待审批行的字段名（snake_case，与前端清单逐字一致）──────
# 对应 apps/web/src/api/rest.ts::PENDING_APPROVAL_SOURCE_FIELDS 与
# apps/web/src/api/approvals.test.ts::PENDING_APPROVAL_FIELDS。
# 来源：approval_service 两条待审批查询的 SELECT 列清单（services/approval.py
# 的 get_pending_requests / get_project_pending）。改任何一个 ⇒ 两边测试同红。
PENDING_APPROVAL_FIELDS = frozenset(
    {
        "id",
        "agent_id",
        "tool_name",
        "tool_arguments",
        "description",
        "status",
        "created_at",
    }
)


async def _make_project_with_agent(tmp_path: Path) -> tuple[str, str, str]:
    """注册临时项目 + 真实 agent 行 + AgentRouter 路由，返回 (project_id, ws, agent_id)。

    get_pending_requests(agent_id) 经 project_db.query → AgentRouter 内存表
    （``_routes``）路由到 per-project DB（Meta DB 没有 agents 表），所以要走
    与生产一致的路径：project DB 插真实 agents 行 → register_project 补注册。
    """
    project_id = f"apprdto-{uuid.uuid4().hex[:12]}"
    ws = tmp_path / project_id
    ws.mkdir(parents=True, exist_ok=True)
    await meta_db.init_meta_db()
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [project_id, "Approval DTO Contract", str(ws), int(time.time() * 1000)],
    )
    agent_id = f"agent-{uuid.uuid4().hex[:12]}"
    conn = await ensure_project_db(str(ws))
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'executor', 'active', ?, ?)",
        [agent_id, agent_id[:4].upper(), project_id, "契约测试成员", now, now],
    )
    await conn.commit()
    await agent_router.register_project(project_id)
    return project_id, str(ws), agent_id


async def _insert_pending(
    ws: str, request_id: str, agent_id: str, project_id: str
) -> None:
    """往真实 per-project DB 插一条 pending 行（INSERT 形状与
    request_permission 逐字一致 —— 契约测的是真实序列化路径，不是假行）。"""
    conn = await ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        """INSERT INTO permission_requests
           (id, agent_id, project_id, tool_name, tool_arguments,
            description, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        [
            request_id,
            agent_id,
            project_id,
            "bash",
            '{"command": "Remove-Item -Recurse ./build"}',
            "清理构建产物（契约测试样例）",
            now,
            now,
        ],
    )
    await conn.commit()


@pytest.fixture
async def approval_env(tmp_path):
    project_id, ws, agent_id = await _make_project_with_agent(tmp_path)
    yield project_id, ws, agent_id
    # 定向摘掉本用例注册的路由（AgentRouter 无公开反注册；残留路由指向
    # 已删的 tmp 目录虽无害，但不能泄漏给同进程后续用例）
    agent_router._routes.pop(agent_id, None)
    agent_router._short_ids.pop(agent_id[:4].upper(), None)
    agents = agent_router._project_agents.get(project_id)
    if agents and agent_id in agents:
        agents.remove(agent_id)
    await close_all()


# ── 1. 服务层：两条待审批查询返回冻结的 snake_case 字段集 ────────────


async def test_get_pending_requests_field_names_are_frozen(approval_env):
    """按 agent 查询：字段名集合与冻结清单完全一致（不多不少）。"""
    _, ws, agent_id = approval_env
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    await _insert_pending(ws, request_id, agent_id, "p-ignored-by-query")

    rows = await approval_service.get_pending_requests(agent_id)

    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == set(PENDING_APPROVAL_FIELDS), (
        "待审批行字段集漂移 —— 请同步改 services/approval.py 的 SELECT、"
        "本清单与前端 rest.ts::PENDING_APPROVAL_SOURCE_FIELDS"
    )
    # 值透传（snake_case 键携带真实值，前端归一层靠这些键取数）
    assert row["id"] == request_id
    assert row["agent_id"] == agent_id
    assert row["tool_name"] == "bash"
    assert row["status"] == "pending"
    assert isinstance(row["created_at"], int)


async def test_get_project_pending_field_names_are_frozen(approval_env):
    """按项目查询：同样冻结（该项目内 pending 之外的行不出现）。"""
    project_id, ws, agent_id = approval_env
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    await _insert_pending(ws, request_id, agent_id, project_id)

    rows = await approval_service.get_project_pending(project_id)

    assert len(rows) == 1
    assert set(rows[0].keys()) == set(PENDING_APPROVAL_FIELDS)
    assert rows[0]["id"] == request_id


# ── 2. API 层：行原样透传，不做字段改名（归一只发生在前端）──────────


async def test_api_layer_returns_rows_unrenamed(approval_env):
    """pending_for_agent / pending_for_project_path 的 {requests: [...]} 包络
    里每行字段集 == 冻结清单 —— API 边界不是第二个『各自猜』的改名点。"""
    project_id, ws, agent_id = approval_env
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    await _insert_pending(ws, request_id, agent_id, project_id)

    resp_agent = await pending_for_agent(agent_id)
    resp_project = await pending_for_project_path(project_id)

    assert set(resp_agent.keys()) == {"requests"}
    assert set(resp_project.keys()) == {"requests"}
    for resp in (resp_agent, resp_project):
        assert len(resp["requests"]) == 1
        assert set(resp["requests"][0].keys()) == set(PENDING_APPROVAL_FIELDS)


# ── 3. 重复提交幂等：明确状态 + 第二次提交零副作用 ──────────────────


async def test_respond_twice_is_idempotent_with_explicit_status(approval_env):
    """第一次 resolve → status="resolved"；重复提交 → ok=True +
    status="already_resolved"，DB 行状态与 updated_at 不再变化。"""
    project_id, ws, agent_id = approval_env
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    await _insert_pending(ws, request_id, agent_id, project_id)

    future = asyncio.get_running_loop().create_future()
    approval_service._pending[request_id] = _PendingEntry(
        agent_id, project_id, future
    )
    try:
        first = await respond_request_compat(
            RespondBodyCompat(requestId=request_id, approved=True)
        )
        assert first == {
            "ok": True,
            "requestId": request_id,
            "status": "resolved",
        }
        await future  # request_permission 的等待方被唤醒

        conn = await ensure_project_db(ws)
        cursor = await conn.execute(
            "SELECT status, updated_at FROM permission_requests WHERE id = ?",
            [request_id],
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row["status"] == "approved"
        first_updated_at = row["updated_at"]

        second = await respond_request_compat(
            RespondBodyCompat(requestId=request_id, approved=True)
        )
        assert second == {
            "ok": True,
            "requestId": request_id,
            "status": "already_resolved",
        }

        cursor = await conn.execute(
            "SELECT status, updated_at FROM permission_requests WHERE id = ?",
            [request_id],
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row["status"] == "approved"
        assert row["updated_at"] == first_updated_at, (
            "重复提交不得重写 DB 行（幂等 = 零副作用）"
        )
    finally:
        approval_service._pending.pop(request_id, None)


async def test_respond_unknown_request_is_noop_already_resolved():
    """对不存在的请求 respond：不抛异常、ok=True、状态明确为
    already_resolved —— 前端拿得到可判别的结果，不是静默假成功。"""
    request_id = f"req-nonexistent-{uuid.uuid4().hex[:8]}"
    result = await respond_request_compat(
        RespondBodyCompat(requestId=request_id, approved=False)
    )
    assert result["ok"] is True
    assert result["status"] == "already_resolved"
