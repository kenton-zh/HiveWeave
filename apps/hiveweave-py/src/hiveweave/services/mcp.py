"""MCP service — manages MCP server connections (stdio + HTTP).

契约 10: MCP 与技能（MCP 部分）
- MCP 服务器配置 CRUD：add_server / remove_server / list_servers / get_server
  （持久化到 Meta DB mcp_servers 表）
- bind_mcp / unbind_mcp / get_bound_mcp（agent.mcp_servers，Meta DB）
- call_tool via JSON-RPC（httpx for HTTP，asyncio subprocess for stdio）
- 30s 超时（契约 10: receive_timeout=30_000）
- stdio 子进程生命周期管理（spawn / terminate / restart）
- mcp_call 从 mcp_servers 表读取配置（不硬编码 URL — 修复 Elixir 已知问题）

权限门禁（resolve_and_update_agent，由 tool_executor 层强制）：
- 与 bind_skill 完全相同：自身 / 直属下属 / CEO+HR 项目内任意 agent；跨项目拒绝
- 本服务只做数据层操作，权限校验由上游 tool_executor 负责

移植自 TS packages/core/src/mcp/mcp-service.ts。
不依赖官方 mcp SDK（用 httpx + asyncio 原生实现 JSON-RPC，保持依赖轻量）。
"""

import asyncio
import json
import os
import time
import uuid
from typing import Any

import httpx
import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

# 契约 10: MCP 调用 30s 超时（Elixir receive_timeout=30_000）
MCP_CALL_TIMEOUT = 30.0
# stdio 子进程关闭超时
_STDIO_CLOSE_TIMEOUT = 5.0

# mcp_servers 表 schema（契约 10；schema.py 未定义，本服务懒创建）
_MCP_SERVERS_DDL = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    transport TEXT NOT NULL DEFAULT 'http',
    command TEXT DEFAULT '',
    args TEXT DEFAULT '[]',
    env TEXT DEFAULT '{}',
    url TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at INTEGER
)
"""

# 幂等迁移标记：mcp_servers 表是否已确认存在
_schema_ready = False


async def _ensure_schema() -> None:
    """创建 mcp_servers 表（幂等）。

    schema.py 未包含此表定义，本服务首次访问时 CREATE TABLE IF NOT EXISTS。
    """
    global _schema_ready
    if _schema_ready:
        return
    await meta_db.execute(_MCP_SERVERS_DDL)
    _schema_ready = True


# ── JSON 解析辅助 ───────────────────────────────────────────


def _parse_json_list(json_str: str | None) -> list[str]:
    """解析 JSON 字符串为字符串列表；非列表/异常返回 []。"""
    if not json_str:
        return []
    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            return [str(x) for x in data]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _extract_text(content: Any) -> str:
    """从 MCP tools/call 结果的 content 字段提取文本。

    content 为 [{type, text/resource/...}, ...] 列表，拼接为字符串。
    """
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                ctype = c.get("type")
                if ctype == "text":
                    parts.append(c.get("text", ""))
                elif ctype == "resource":
                    uri = (c.get("resource") or {}).get("uri", "")
                    parts.append(f"[resource: {uri}]")
                else:
                    parts.append(json.dumps(c, ensure_ascii=False))
            else:
                parts.append(str(c))
        return "\n".join(parts)
    return str(content)


# ── 传输层抽象 ───────────────────────────────────────────────


class _HttpTransport:
    """HTTP/SSE 传输：用 httpx 发 JSON-RPC POST。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self._client: httpx.AsyncClient | None = None
        self._req_id = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=MCP_CALL_TIMEOUT)
        return self._client

    async def call(self, method: str, params: dict | None = None) -> Any:
        """发送 JSON-RPC 请求并返回 result 字段。"""
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params or {},
        }
        client = await self._get_client()
        resp = await client.post(self.url, json=payload)
        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result") if isinstance(data, dict) else data

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class _StdioTransport:
    """stdio 传输：spawn 子进程，stdin/stdout 传换行分隔的 JSON-RPC。

    契约 10 已知问题 E1：Elixir 无 stdio，Python 需自管子进程生命周期。
    本实现：首次调用时 spawn，close 时 terminate（5s 优雅关闭后 kill）。
    异常退出（returncode != None）时下次调用自动重启。
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.command = command
        self.args = args or []
        self.env = env
        self.cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        # 串行化 stdin/stdout 访问，避免并发请求交错
        self._lock = asyncio.Lock()

    async def _ensure_proc(self) -> asyncio.subprocess.Process:
        """获取活跃子进程；已退出则重启。"""
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        # 合并环境变量（子进程需要 PATH 等）
        env = {**os.environ, **(self.env or {})}
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        self._proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.cwd or None,
            **windows_no_window_kwargs(),
        )
        log.info(
            "mcp_stdio_spawned",
            command=self.command,
            pid=self._proc.pid,
        )
        return self._proc

    async def call(self, method: str, params: dict | None = None) -> Any:
        """发送 JSON-RPC（换行分隔），读取一行响应。"""
        async with self._lock:
            proc = await self._ensure_proc()
            self._req_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": method,
                "params": params or {},
            }
            line = json.dumps(payload) + "\n"
            assert proc.stdin is not None
            proc.stdin.write(line.encode())
            await proc.stdin.drain()
            assert proc.stdout is not None
            raw = await asyncio.wait_for(
                proc.stdout.readline(), timeout=MCP_CALL_TIMEOUT
            )
            if not raw:
                raise RuntimeError(
                    "MCP stdio: empty response (process may have exited)"
                )
            data = json.loads(raw.decode())
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"MCP error: {data['error']}")
            return data.get("result") if isinstance(data, dict) else data

    async def close(self) -> None:
        """优雅关闭子进程（terminate → 5s 等待 → kill）。"""
        if self._proc is None:
            return
        if self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(
                    self._proc.wait(), timeout=_STDIO_CLOSE_TIMEOUT
                )
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass  # best-effort
        self._proc = None


# ── McpService ──────────────────────────────────────────────


class McpService:
    """MCP server registry & tool invocation (stdio + HTTP).

    配置持久化在 Meta DB mcp_servers 表；连接缓存（transport 实例）在内存。
    """

    def __init__(self) -> None:
        # server_name → transport 实例
        self._connections: dict[str, _HttpTransport | _StdioTransport] = {}

    # ── 服务器配置 CRUD ──────────────────────────────────────

    async def add_server(
        self,
        name: str,
        transport: str = "http",
        command: str = "",
        url: str = "",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> dict:
        """新增或更新 MCP 服务器配置（upsert 语义）。

        契约 10: mcp_configure（admin only 由路由层强制）。
        """
        await _ensure_schema()
        existing = await self.get_server(name)
        now_ms = int(time.time() * 1000)
        args_json = json.dumps(args or [])
        env_json = json.dumps(env or {})
        enabled_int = 1 if enabled else 0

        if existing is not None:
            await meta_db.execute(
                "UPDATE mcp_servers SET transport=?, command=?, args=?, "
                "env=?, url=?, enabled=? WHERE name=?",
                [transport, command, args_json, env_json, url, enabled_int, name],
            )
        else:
            await meta_db.execute(
                "INSERT INTO mcp_servers "
                "(id, name, transport, command, args, env, url, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(uuid.uuid4()), name, transport, command,
                    args_json, env_json, url, enabled_int, now_ms,
                ],
            )
        # 配置变更后断开旧连接，下次调用时按新配置重连
        await self._disconnect(name)
        log.info("mcp_server_added", name=name, transport=transport)
        return {"ok": True, "name": name}

    async def remove_server(self, name: str) -> dict:
        """删除 MCP 服务器配置并断开连接。"""
        await _ensure_schema()
        await self._disconnect(name)
        await meta_db.execute("DELETE FROM mcp_servers WHERE name=?", [name])
        log.info("mcp_server_removed", name=name)
        return {"ok": True, "name": name}

    async def list_servers(self) -> list[dict]:
        """列出所有已配置的 MCP 服务器（按创建时间正序）。"""
        await _ensure_schema()
        rows = await meta_db.query(
            "SELECT * FROM mcp_servers ORDER BY created_at ASC"
        )
        return [self._row_to_config(r) for r in rows]

    async def get_server(self, name: str) -> dict | None:
        """取单个 MCP 服务器配置。"""
        await _ensure_schema()
        row = await meta_db.query_one(
            "SELECT * FROM mcp_servers WHERE name=? LIMIT 1", [name]
        )
        return self._row_to_config(row) if row else None

    # ── Agent 绑定 ──────────────────────────────────────────

    async def bind_mcp(self, agent_id: str, server_name: str) -> dict:
        """绑定 MCP 服务器到 agent（修改 agent.mcp_servers）。

        契约 10: 权限门禁与 bind_skill 相同（由 tool_executor 强制）。
        """
        # 校验服务器已配置
        server = await self.get_server(server_name)
        if server is None:
            return {"ok": False, "error": f"MCP server '{server_name}' not configured"}

        agent = await meta_db.get_agent_by_id(agent_id)
        if agent is None:
            return {"ok": False, "error": f"Agent '{agent_id}' not found"}

        bound = await self.get_bound_mcp(agent_id)
        if server_name in bound:
            return {"ok": False, "error": f"MCP server '{server_name}' already bound"}

        bound.append(server_name)
        now_ms = int(time.time() * 1000)
        await project_db.execute(
            agent_id,
            "UPDATE agents SET mcp_servers=?, updated_at=? WHERE id=?",
            [json.dumps(bound), now_ms, agent_id],
        )
        log.info("mcp_bound", agent_id=agent_id, server=server_name)
        return {"ok": True, "server": server_name}

    async def unbind_mcp(self, agent_id: str, server_name: str) -> dict:
        """解绑 MCP 服务器。"""
        bound = await self.get_bound_mcp(agent_id)
        if server_name not in bound:
            return {"ok": False, "error": f"MCP server '{server_name}' is not bound"}
        bound.remove(server_name)
        now_ms = int(time.time() * 1000)
        await project_db.execute(
            agent_id,
            "UPDATE agents SET mcp_servers=?, updated_at=? WHERE id=?",
            [json.dumps(bound), now_ms, agent_id],
        )
        log.info("mcp_unbound", agent_id=agent_id, server=server_name)
        return {"ok": True, "server": server_name}

    async def get_bound_mcp(self, agent_id: str) -> list[str]:
        """获取 agent 当前已绑定的 MCP 服务器名列表。"""
        row = await project_db.query_one(
            agent_id, "SELECT mcp_servers FROM agents WHERE id=? LIMIT 1", [agent_id]
        )
        if row is None:
            return []
        return _parse_json_list(row["mcp_servers"])

    # ── 连接管理 ─────────────────────────────────────────────

    async def _get_connection(
        self, name: str
    ) -> _HttpTransport | _StdioTransport:
        """获取或创建到 MCP 服务器的连接（缓存复用）。"""
        if name in self._connections:
            return self._connections[name]

        config = await self.get_server(name)
        if config is None:
            raise RuntimeError(f"MCP server '{name}' not configured")
        if not config["enabled"]:
            raise RuntimeError(f"MCP server '{name}' is disabled")

        if config["transport"] == "stdio":
            if not config["command"]:
                raise RuntimeError(f"stdio server '{name}' requires a command")
            transport: _HttpTransport | _StdioTransport = _StdioTransport(
                command=config["command"],
                args=config.get("args") or [],
                env=config.get("env"),
            )
        else:
            if not config["url"]:
                raise RuntimeError(f"HTTP server '{name}' requires a URL")
            transport = _HttpTransport(url=config["url"])

        self._connections[name] = transport
        return transport

    async def _disconnect(self, name: str) -> None:
        """断开并移除某个服务器的缓存连接。"""
        conn = self._connections.pop(name, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass  # best-effort

    async def disconnect_all(self) -> None:
        """断开所有连接（服务停止时调用）。"""
        names = list(self._connections.keys())
        for name in names:
            await self._disconnect(name)

    # ── 工具调用 ─────────────────────────────────────────────

    async def list_tools(self, server_name: str) -> list[dict]:
        """列出某个 MCP 服务器的所有工具（自动连接）。"""
        conn = await self._get_connection(server_name)
        result = await asyncio.wait_for(
            conn.call("tools/list", {}), timeout=MCP_CALL_TIMEOUT
        )
        if not isinstance(result, dict):
            return []
        tools = result.get("tools", []) or []
        return [
            {
                "serverName": server_name,
                "name": t.get("name"),
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema", {}),
            }
            for t in tools
            if isinstance(t, dict)
        ]

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: dict | None = None,
    ) -> str:
        """调用 MCP 工具（30s 超时），返回提取的文本。

        契约 10: mcp_call — POST JSON-RPC tools/call，解析 result.content。
        """
        conn = await self._get_connection(server_name)
        result = await asyncio.wait_for(
            conn.call(
                "tools/call",
                {"name": tool_name, "arguments": args or {}},
            ),
            timeout=MCP_CALL_TIMEOUT,
        )

        if not isinstance(result, dict):
            return str(result)

        # isError 标志（MCP 协议）
        if result.get("isError"):
            return f"MCP Error: {_extract_text(result.get('content'))}"

        content = result.get("content")
        if not content:
            return "(no output)"
        return _extract_text(content)

    async def list_all_tools(self) -> list[dict]:
        """列出所有已启用 MCP 服务器的工具（best-effort，离线服务器跳过）。"""
        servers = await self.list_servers()
        all_tools: list[dict] = []
        for s in servers:
            if not s["enabled"]:
                continue
            try:
                tools = await self.list_tools(s["name"])
                all_tools.extend(tools)
            except Exception as e:
                log.warning(
                    "mcp_list_tools_failed",
                    server=s["name"],
                    error=str(e),
                )
        return all_tools

    async def list_available_mcp(self) -> str:
        """列出已配置的 MCP 服务器（格式化字符串，工具输出）。"""
        servers = await self.list_servers()
        if not servers:
            return "No MCP servers currently registered in the system."

        lines: list[str] = []
        for s in servers:
            endpoint = s.get("url") or s.get("command") or "(no endpoint)"
            status = "enabled" if s["enabled"] else "disabled"
            lines.append(
                f"- **{s['name']}**: {endpoint} [{s['transport']}, {status}]"
            )

        return (
            "Available MCP Servers:\n\n"
            + "\n".join(lines)
            + "\n\nTo bind an MCP server to an agent, use `bind_mcp` "
            "with the server name."
        )

    # ── 行转换 ───────────────────────────────────────────────

    @staticmethod
    def _row_to_config(row: Any) -> dict:
        """将 DB 行转为 MCP 服务器配置 dict（解析 args/env JSON）。"""
        d = dict(row)
        try:
            args = json.loads(d.get("args") or "[]")
        except json.JSONDecodeError:
            args = []
        try:
            env = json.loads(d.get("env") or "{}")
        except json.JSONDecodeError:
            env = {}
        return {
            "id": d.get("id"),
            "name": d.get("name"),
            "transport": d.get("transport", "http"),
            "command": d.get("command", ""),
            "args": args,
            "env": env,
            "url": d.get("url", ""),
            "enabled": bool(d.get("enabled", 1)),
            "created_at": d.get("created_at"),
        }


# 模块级单例（对齐 TS mcpService；无状态配置在 DB，连接缓存在实例）
mcp_service = McpService()
