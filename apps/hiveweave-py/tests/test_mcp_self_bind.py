"""MCP 自助绑定（09-08）：bind_mcp/unbind_mcp 工具 + MCP_BIND 能力位。

覆盖：工具函数行为（自绑/跨目标/未配置 server/缺参）、能力硬门
（executor/qa/coordinator/hr 可绑、CEO 拒）、可见集（新工具进各 family
的模型工具表，CEO 不可见）、TOOL_PARAM_SCHEMAS 三件套注册。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services import mcp_supervisor as sup
from hiveweave.services.permission import PermissionService
from hiveweave.services.policy import (
    Capability,
    capabilities_for,
    infer_role_family,
    tool_hard_deny,
)
from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS
from hiveweave.tools.org_tools import (
    BindMcpParams,
    UnbindMcpParams,
    bind_mcp_tool,
    unbind_mcp_tool,
)
from hiveweave.tools.result import ToolResult


@pytest.fixture(autouse=True)
def _clean_supervisor():
    sup.reset_for_tests()
    yield
    sup.reset_for_tests()


def _agent(**kwargs) -> dict:
    base = {
        "id": "a1",
        "name": "扳手",
        "role": "MCP环境技术负责人",
        "permission_type": "executor",
        "permission_mode": "readwrite",
        "allowed_tools": "[]",
        "denied_tools": "[]",
        "ask_tools": "[]",
    }
    base.update(kwargs)
    return base


async def _seed_server_tools(server: str = "fs") -> None:
    """经 sync_server 灌一张工具表（mock tools/list，真实换代路径）。"""
    mock = AsyncMock(return_value=[
        {"name": "read", "description": "Read a file",
         "inputSchema": {"type": "object"}},
        {"name": "query", "description": "Query",
         "inputSchema": {"type": "object"}},
    ])
    with patch(
        "hiveweave.services.mcp.mcp_service.list_tools", new=mock
    ):
        assert await sup.sync_server(server) is True


# ── 工具函数行为 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_mcp_self_success_reports_tool_count():
    await _seed_server_tools("fs")
    bind = AsyncMock(return_value={"ok": True, "server": "fs"})
    inval = AsyncMock()
    with patch("hiveweave.services.mcp.mcp_service.bind_mcp", new=bind), \
         patch("hiveweave.services.mcp_supervisor.invalidate_agent", new=inval):
        res = await bind_mcp_tool(
            BindMcpParams(serverName="fs"), "a1", "", ctx=None
        )
    assert res.success, res.error
    bind.assert_awaited_once_with("a1", "fs")
    inval.assert_awaited_once_with("a1")
    assert "2 tools visible next turn" in res.output


@pytest.mark.asyncio
async def test_bind_mcp_unsynced_server_still_ok_with_hint():
    """绑定成功但 server 不可达（表空）→ 仍 ok，带同步提示，不谎报可见。"""
    bind = AsyncMock(return_value={"ok": True, "server": "ghost"})
    inval = AsyncMock()
    with patch("hiveweave.services.mcp.mcp_service.bind_mcp", new=bind), \
         patch("hiveweave.services.mcp_supervisor.invalidate_agent", new=inval):
        res = await bind_mcp_tool(
            BindMcpParams(serverName="ghost"), "a1", "", ctx=None
        )
    assert res.success, res.error
    assert "not synced yet" in res.output


@pytest.mark.asyncio
async def test_bind_mcp_cross_target_resolves_via_org():
    await _seed_server_tools("fs")
    org = SimpleNamespace(resolve_agent=AsyncMock(return_value={"id": "t1"}))
    bind = AsyncMock(return_value={"ok": True})
    inval = AsyncMock()
    with patch("hiveweave.services.mcp.mcp_service.bind_mcp", new=bind), \
         patch("hiveweave.services.mcp_supervisor.invalidate_agent", new=inval):
        res = await bind_mcp_tool(
            BindMcpParams(target="扳手", serverName="fs"),
            "a1", "", ctx=SimpleNamespace(org=org),
        )
    assert res.success, res.error
    org.resolve_agent.assert_awaited_once_with("扳手")
    bind.assert_awaited_once_with("t1", "fs")
    inval.assert_awaited_once_with("t1")


@pytest.mark.asyncio
async def test_bind_mcp_unknown_target_errors():
    org = SimpleNamespace(resolve_agent=AsyncMock(return_value=None))
    bind = AsyncMock()
    with patch("hiveweave.services.mcp.mcp_service.bind_mcp", new=bind):
        res = await bind_mcp_tool(
            BindMcpParams(target="ghost", serverName="fs"),
            "a1", "", ctx=SimpleNamespace(org=org),
        )
    assert not res.success
    assert "Agent not found" in (res.error or "")
    bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_mcp_ceo_target_hard_refused():
    """审计 M1：任何持 MCP_BIND 的 agent 都不得把 server 绑到 CEO 行上
    （mcp__ 绑定即 allow，CEO 被绑即得执行通道=穿透组织铁律）。"""
    org = SimpleNamespace(resolve_agent=AsyncMock(
        return_value={"id": "ceo-1", "role": "ceo",
                      "permission_type": "coordinator"}
    ))
    bind = AsyncMock()
    with patch("hiveweave.services.mcp.mcp_service.bind_mcp", new=bind):
        res = await bind_mcp_tool(
            BindMcpParams(target="归零", serverName="fs"),
            "a1", "", ctx=SimpleNamespace(org=org),
        )
    assert not res.success
    assert "CEO" in (res.error or "")
    bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_mcp_cross_target_without_org_ctx_errors():
    bind = AsyncMock()
    with patch("hiveweave.services.mcp.mcp_service.bind_mcp", new=bind):
        res = await bind_mcp_tool(
            BindMcpParams(target="someone", serverName="fs"),
            "a1", "", ctx=None,
        )
    assert not res.success
    assert "ctx.org is missing" in (res.error or "")
    bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_mcp_unconfigured_server_passthrough_error():
    bind = AsyncMock(
        return_value={"ok": False, "error": "MCP server 'x' not configured"}
    )
    inval = AsyncMock()
    with patch("hiveweave.services.mcp.mcp_service.bind_mcp", new=bind), \
         patch("hiveweave.services.mcp_supervisor.invalidate_agent", new=inval):
        res = await bind_mcp_tool(
            BindMcpParams(serverName="x"), "a1", "", ctx=None
        )
    assert not res.success
    assert "not configured" in (res.error or "")
    inval.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_mcp_missing_server_name():
    """缺参首道拦截在 pydantic required；空串落到工具层 guard。"""
    with pytest.raises(ValueError):
        BindMcpParams()
    res = await bind_mcp_tool(BindMcpParams(serverName=""), "a1", "", ctx=None)
    assert not res.success
    assert "serverName" in (res.error or "")


@pytest.mark.asyncio
async def test_unbind_mcp_success():
    inval = AsyncMock()
    unbind = AsyncMock(return_value={"ok": True})
    with patch("hiveweave.services.mcp.mcp_service.unbind_mcp", new=unbind), \
         patch("hiveweave.services.mcp_supervisor.invalidate_agent", new=inval):
        res = await unbind_mcp_tool(
            UnbindMcpParams(serverName="fs"), "a1", "", ctx=None
        )
    assert res.success, res.error
    unbind.assert_awaited_once_with("a1", "fs")
    inval.assert_awaited_once_with("a1")
    assert "unbound" in res.output


@pytest.mark.asyncio
async def test_unbind_mcp_failure_does_not_invalidate():
    unbind = AsyncMock(
        return_value={"ok": False, "error": "MCP server 'fs' is not bound"}
    )
    inval = AsyncMock()
    with patch("hiveweave.services.mcp.mcp_service.unbind_mcp", new=unbind), \
         patch("hiveweave.services.mcp_supervisor.invalidate_agent", new=inval):
        res = await unbind_mcp_tool(
            UnbindMcpParams(serverName="fs"), "a1", "", ctx=None
        )
    assert not res.success
    assert "not bound" in (res.error or "")
    inval.assert_not_awaited()


# ── 能力硬门（policy）与可见集（permission） ─────────────────


def test_mcp_bind_capability_matrix():
    # 四族工作角色持 MCP_BIND；CEO 不持（绑定=开执行通道，CEO 无执行通道）
    for family in ("hr", "coordinator", "executor", "qa"):
        agent = _agent(
            role=family if family != "executor" else "模块工程师",
            permission_type="coordinator" if family in ("hr", "coordinator") else "executor",
        )
        assert infer_role_family(agent) == family, family
        assert Capability.MCP_BIND in capabilities_for(agent), family
        assert tool_hard_deny(agent, "bind_mcp") is None, family
        assert tool_hard_deny(agent, "unbind_mcp") is None, family
        # list_available_mcp 随 MCP_BIND 放宽（原 STAFFING 仅 HR）
        assert tool_hard_deny(agent, "list_available_mcp") is None, family

    ceo = _agent(role="ceo", permission_type="coordinator")
    assert Capability.MCP_BIND not in capabilities_for(ceo)
    assert tool_hard_deny(ceo, "bind_mcp")
    assert tool_hard_deny(ceo, "unbind_mcp")
    assert tool_hard_deny(ceo, "list_available_mcp")


def test_mcp_bind_visible_sets_cover_binders_not_ceo():
    svc = PermissionService()
    for family, role, ptype in (
        ("executor", "模块工程师", "executor"),
        ("qa", "测试工程师", "executor"),
        ("coordinator", "架构师", "coordinator"),
        ("hr", "HR", "coordinator"),
    ):
        names = set(svc.get_tools_for_agent(
            _agent(role=role, permission_type=ptype)
        ))
        assert {"bind_mcp", "unbind_mcp", "list_available_mcp"} <= names, family

    ceo_names = set(svc.get_tools_for_agent(
        _agent(role="ceo", permission_type="coordinator")
    ))
    assert "bind_mcp" not in ceo_names
    assert "unbind_mcp" not in ceo_names


def test_mcp_bind_tools_schema_registered():
    """三件套：@tool 注册 + TOOL_PARAM_SCHEMAS + canonical camelCase。"""
    for name in ("bind_mcp", "unbind_mcp"):
        schema = TOOL_PARAM_SCHEMAS.get(name)
        assert schema, f"{name} missing from TOOL_PARAM_SCHEMAS"
        assert "serverName" in schema["properties"], name
        assert schema.get("required") == ["serverName"], name
