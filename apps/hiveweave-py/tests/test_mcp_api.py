"""MCP REST 层（api/mcp.py）契约测试。

覆盖：
- servers CRUD 往返（POST upsert → GET → DELETE，真实临时 Meta DB）
- bind/unbind 往返（真实 per-project DB + agent_router 注册）
- 绑定不存在的 server → 400
- tools 端点对未配置 server → 503 + reason
- supervisor invalidate / invalidate_agent 缺失时静默跳过（懒导入 + getattr 防御）

supervisor 的 invalidate 系列在夹具中 mock 掉 —— invalidate(force=True) 会
真实连 MCP 服务器拉工具表，测试必须离线且可断言调用。
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

PROJECT_ID = "mcp-api-test-project"
WORKSPACE = None  # 夹具内赋值（tmp_path）
AGENT_ID = "mcp-api-test-agent"

_SERVERS_URL = "/api/mcp/servers"


@pytest.fixture
async def mcp_env(tmp_path: Path, monkeypatch):
    """真实临时 Meta DB + per-project DB + agent 路由 + mock 掉 supervisor。"""
    from hiveweave.api.mcp import router as mcp_router
    from hiveweave.db import meta as meta_db
    from hiveweave.db import project as project_db
    from hiveweave.services import mcp as mcp_mod
    from hiveweave.services import mcp_supervisor
    from hiveweave.services.agent_router import AgentRoute, agent_router

    # ── 真实临时 Meta DB（照抄 test_code_audit_ledger.py 的隔离方式）──
    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()

    # ── 数据层模块状态复位（连接缓存 / 工具表）──
    # 注：mcp_servers 已归位到 `db/schema.META_DB_TABLES`（建库即建表），
    # 不再需要手工复位 schema 标记 —— 原来那行 `mcp_mod._schema_ready = False`
    # 恰恰是「标记跨库世代存活」缺陷存在的证据（换 Meta DB 后必须人工清标记，
    # 漏一次就 no such table）。
    mcp_mod.mcp_service._connections.clear()
    mcp_supervisor.reset_for_tests()

    # ── supervisor invalidate 系列打桩（避免真实连 MCP 服务器）──
    # raising=False：invalidate_agent 在并行会话的 supervisor 中可能尚未落地
    invalidate_mock = AsyncMock(return_value=None)
    invalidate_agent_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(mcp_supervisor, "invalidate", invalidate_mock, raising=False)
    monkeypatch.setattr(
        mcp_supervisor, "invalidate_agent", invalidate_agent_mock, raising=False
    )

    # ── 项目路由：真实 meta DB projects 行 + per-project DB + agent 注册 ──
    workspace = str(tmp_path / "workspace")
    now_ms = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "mcp-api-test-project", workspace, now_ms],
    )
    await project_db.ensure_project_db(workspace)
    # 先缓存 agent → workspace（project_db.execute 依赖 get_project_db_for_agent）
    project_db._agent_cache[AGENT_ID] = workspace
    await project_db.execute(
        AGENT_ID,
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'executor', 'active', ?, ?)",
        [AGENT_ID, "MA", PROJECT_ID, AGENT_ID, now_ms, now_ms],
    )
    agent_router.register(
        AgentRoute(
            agent_id=AGENT_ID,
            project_id=PROJECT_ID,
            workspace_path=workspace,
            short_id="MA",
            name=AGENT_ID,
            role="executor",
            status="active",
        )
    )
    project_db._agent_cache[AGENT_ID] = workspace

    app = FastAPI()
    app.include_router(mcp_router)

    yield {
        "invalidate": invalidate_mock,
        "invalidate_agent": invalidate_agent_mock,
    }

    # ── 清理内存态（DB 连接由 conftest autouse 夹具统一关闭）──
    agent_router.clear_project(PROJECT_ID)
    project_db._agent_cache.pop(AGENT_ID, None)
    mcp_mod.mcp_service._connections.clear()
    mcp_supervisor.reset_for_tests()


async def _client() -> AsyncClient:
    from hiveweave.api.mcp import router as mcp_router

    app = FastAPI()
    app.include_router(mcp_router)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_servers_crud_roundtrip(mcp_env):
    async with await _client() as client:
        # ── 初始为空 ──
        r = await client.get(_SERVERS_URL)
        assert r.status_code == 200
        assert r.json() == {"servers": []}

        # ── 创建（http）──
        payload = {
            "name": "search",
            "transport": "http",
            "url": "http://127.0.0.1:9/mcp",
            "args": ["--verbose"],
            "env": {"TOKEN": "t1"},
        }
        r = await client.post(_SERVERS_URL, json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        # snake + camel 双字段
        assert body["server"]["name"] == "search"
        assert body["server"]["enabled"] is True
        assert body["server"]["createdAt"] == body["server"]["created_at"]

        # ── 列表可读回 ──
        r = await client.get(_SERVERS_URL)
        servers = r.json()["servers"]
        assert len(servers) == 1
        assert servers[0]["transport"] == "http"
        assert servers[0]["url"] == "http://127.0.0.1:9/mcp"
        assert servers[0]["args"] == ["--verbose"]
        assert servers[0]["env"] == {"TOKEN": "t1"}

        # ── upsert 更新（同 name 覆盖 enabled）──
        r = await client.post(
            _SERVERS_URL,
            json={"name": "search", "transport": "http", "enabled": False},
        )
        assert r.status_code == 200
        r = await client.get(_SERVERS_URL)
        assert len(r.json()["servers"]) == 1
        assert r.json()["servers"][0]["enabled"] is False

        # ── 配置变更触发 supervisor.invalidate(name) ──
        mcp_env["invalidate"].assert_any_await("search")

        # ── 删除 + 二次删除 404 ──
        r = await client.delete(f"{_SERVERS_URL}/search")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "name": "search"}
        r = await client.delete(f"{_SERVERS_URL}/search")
        assert r.status_code == 404

        r = await client.get(_SERVERS_URL)
        assert r.json() == {"servers": []}

        # ── transport 非法值 → 422（Literal 校验）──
        r = await client.post(
            _SERVERS_URL, json={"name": "bad", "transport": "websocket"}
        )
        assert r.status_code == 422


async def test_tools_unconfigured_server_returns_503(mcp_env):
    async with await _client() as client:
        r = await client.get(f"{_SERVERS_URL}/never-configured/tools")
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert "reason" in detail
        assert "never-configured" in detail["reason"]


async def test_bind_unbind_roundtrip(mcp_env):
    async with await _client() as client:
        # 先配置 server
        r = await client.post(
            _SERVERS_URL,
            json={"name": "fs", "transport": "http", "url": "http://127.0.0.1:9"},
        )
        assert r.status_code == 200

        agent_url = f"/api/mcp/agents/{AGENT_ID}"

        # ── 初始未绑定 ──
        r = await client.get(agent_url)
        assert r.status_code == 200
        body = r.json()
        assert body["servers"] == []
        assert body["agentId"] == AGENT_ID

        # ── 绑定 ──
        r = await client.post(f"{agent_url}/bind", json={"server": "fs"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        r = await client.get(agent_url)
        assert r.json()["servers"] == ["fs"]

        # 绑定变更触发 supervisor.invalidate_agent(agent_id)
        mcp_env["invalidate_agent"].assert_any_await(AGENT_ID)

        # ── 重复绑定 → 400 ──
        r = await client.post(f"{agent_url}/bind", json={"server": "fs"})
        assert r.status_code == 400

        # ── 解绑 ──
        r = await client.post(f"{agent_url}/unbind", json={"server": "fs"})
        assert r.status_code == 200
        r = await client.get(agent_url)
        assert r.json()["servers"] == []

        # ── 解绑未绑定的 → 400 ──
        r = await client.post(f"{agent_url}/unbind", json={"server": "fs"})
        assert r.status_code == 400


async def test_bind_unknown_server_rejected(mcp_env):
    async with await _client() as client:
        r = await client.post(
            f"/api/mcp/agents/{AGENT_ID}/bind", json={"server": "ghost"}
        )
        assert r.status_code == 400
        assert "ghost" in r.json()["detail"]


async def test_bind_unknown_agent_rejected(mcp_env):
    async with await _client() as client:
        r = await client.post(
            _SERVERS_URL,
            json={"name": "fs", "transport": "http", "url": "http://127.0.0.1:9"},
        )
        assert r.status_code == 200
        r = await client.post(
            "/api/mcp/agents/no-such-agent/bind", json={"server": "fs"}
        )
        assert r.status_code == 400
        assert "no-such-agent" in r.json()["detail"]


async def test_supervisor_missing_functions_skipped(
    tmp_path: Path, monkeypatch
):
    """supervisor 模块/函数未落地时不炸 —— 懒导入 + getattr 防御。"""
    from hiveweave.db import meta as meta_db
    from hiveweave.db import project as project_db
    from hiveweave.services import mcp as mcp_mod
    from hiveweave.services.agent_router import AgentRoute, agent_router

    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()
    mcp_mod._schema_ready = False
    mcp_mod.mcp_service._connections.clear()

    workspace = str(tmp_path / "workspace")
    now_ms = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "p", workspace, now_ms],
    )
    await project_db.ensure_project_db(workspace)
    # 先缓存 agent → workspace（project_db.execute 依赖 get_project_db_for_agent）
    project_db._agent_cache[AGENT_ID] = workspace
    await project_db.execute(
        AGENT_ID,
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'executor', 'active', ?, ?)",
        [AGENT_ID, "MA", PROJECT_ID, AGENT_ID, now_ms, now_ms],
    )
    agent_router.register(
        AgentRoute(
            agent_id=AGENT_ID,
            project_id=PROJECT_ID,
            workspace_path=workspace,
            short_id="MA",
            name=AGENT_ID,
            role="executor",
            status="active",
        )
    )
    project_db._agent_cache[AGENT_ID] = workspace

    # 模拟「模块存在但函数尚未落地」
    monkeypatch.delattr("hiveweave.services.mcp_supervisor.invalidate", raising=False)
    monkeypatch.delattr(
        "hiveweave.services.mcp_supervisor.invalidate_agent", raising=False
    )

    try:
        async with await _client() as client:
            r = await client.post(
                _SERVERS_URL,
                json={"name": "fs", "transport": "http", "url": "http://127.0.0.1:9"},
            )
            assert r.status_code == 200
            r = await client.post(
                f"/api/mcp/agents/{AGENT_ID}/bind", json={"server": "fs"}
            )
            assert r.status_code == 200
            assert r.json()["ok"] is True
    finally:
        agent_router.clear_project(PROJECT_ID)
        project_db._agent_cache.pop(AGENT_ID, None)
        mcp_mod.mcp_service._connections.clear()
