"""MCP 连接监督与工具桥（45 轮立项 #9，DSH mcp-client 语义移植）。

数据层在 ``services/mcp.py``（配置 CRUD / 绑定 / 传输 / call_tool）；
本模块补三层：

1. **工具表**：``mcp__<server>__<raw>`` 公开名 → (server, raw, schema)。
   公开名只出现在 wire 与权限名单上，调用时经表反解 rawName——闭包持
   raw、raw 永不下发（DSH tools.ts createExecutor 同款）。
2. **两阶段原子同步**（DSH syncTools :143-193）：Phase 1 拉全量（失败
   保留上一代）；Phase 2 原子换代（先删该 server 旧条目再插新一代）。
3. **退避/降级**（DSH connection.ts :40-45/:203-215）：失败指数退避
   500ms→30s（``next_allowed_ts`` 闸门，不睡调用方），连续失败
   ``max_attempts`` 次该 server 降级——工具从表里摘除（绑定 agent 立即
   不可见）直到下次成功同步或显式 invalidate；存活超稳定窗（=max_delay）
   失败计数归零重开预算。

与 DSH 的已知偏差（有意）：未消费 list_changed 推送——现传输层是严格
请求/响应协议，收通知需要 reader task 重构；以 **TTL 访问刷新（默认
5min）+ 变更触发 invalidate** 覆盖同需求，留待后续。

失败哲学：一切 best-effort fail-open（同步失败 ≠ 平台故障），但
**降级是 fail-closed 可见**——工具从表消失，agent 看不到而非调用时报错。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ── 公开名规则（DSH packages/mcp/mcp-client/src/tools.ts:48；2026-09-16 复核）────
MAX_PUBLIC_NAME_LENGTH = 64
_HASH_LENGTH = 12
# server 名与 raw 名共用的合法字符集；其余换 _
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")

# ── 退避/降级参数（DSH RECONNECT_DEFAULTS :40-45）──────────────
INITIAL_DELAY_MS = 500
MAX_DELAY_MS = 30_000
MAX_ATTEMPTS = 10
# 工具表 TTL：访问间隔超此值则刷新（补偿未消费 list_changed）
TOOL_TTL_S = 300.0


@dataclass
class McpToolEntry:
    server: str
    raw_name: str
    description: str
    input_schema: dict


@dataclass
class _ServerSyncState:
    attempt: int = 0
    next_allowed_ts: float = 0.0
    last_success_ts: float = 0.0
    degraded: bool = False


def public_tool_name(server: str, raw_name: str) -> str:
    """构造 ``mcp__<server>__<raw>`` 公开名；超长/被规范化加 hash 后缀。

    DSH tools.ts:111-117 同款：非法字符换 _；结果超 64 字符或与原意
    不一致（有字符被换掉）→ 追加 12 位 sha256 后缀防碰撞。
    """
    safe_server = _SAFE_RE.sub("_", server)
    safe_raw = _SAFE_RE.sub("_", raw_name)
    base = f"mcp__{safe_server}__{safe_raw}"
    if len(base) <= MAX_PUBLIC_NAME_LENGTH and safe_server == server and (
        safe_raw == raw_name
    ):
        return base
    import hashlib

    digest = hashlib.sha256(f"{server}::{raw_name}".encode()).hexdigest()
    return f"{base[:MAX_PUBLIC_NAME_LENGTH - _HASH_LENGTH - 1]}_{digest[:_HASH_LENGTH]}"


# ── 模块级状态（进程内；重启即空，按需重同步）──────────────────
_tool_table: dict[str, McpToolEntry] = {}
_server_tools: dict[str, list[str]] = {}  # server -> [public_name]
_states: dict[str, _ServerSyncState] = {}


def _state(server: str) -> _ServerSyncState:
    st = _states.get(server)
    if st is None:
        st = _ServerSyncState()
        _states[server] = st
    return st


def get_tool_entry(public_name: str) -> McpToolEntry | None:
    return _tool_table.get(public_name)


def server_tool_names(server: str) -> list[str]:
    return list(_server_tools.get(server, []))


def reset_for_tests() -> None:
    _tool_table.clear()
    _server_tools.clear()
    _states.clear()


async def _fetch_tools(server: str) -> list[dict]:
    from hiveweave.services.mcp import mcp_service

    tools = await mcp_service.list_tools(server)
    normalized: list[dict] = []
    seen_raw: set[str] = set()
    for t in tools or []:
        raw = str(t.get("name") or "").strip()
        if not raw:
            continue
        if raw in seen_raw:
            # server 内重名 → 整代判无效（DSH 两阶段 Phase 1 语义）
            raise ValueError(f"duplicate tool name '{raw}' on server {server}")
        seen_raw.add(raw)
        schema = t.get("inputSchema")
        normalized.append({
            "raw": raw,
            "description": str(t.get("description") or ""),
            "schema": schema if isinstance(schema, dict) else {},
        })
    return normalized


async def sync_server(server: str, *, force: bool = False) -> bool:
    """同步单 server 工具表；成功 True，失败/退避中/降级 False。

    两阶段：拉全量成功才原子换代（先删旧条目再插新）；失败走退避闸门，
    连续 ``MAX_ATTEMPTS`` 次失败 → 降级（摘除该 server 全部工具，fail-
    closed 可见）。``force`` 跳过 TTL/退避闸门（显式 invalidate 用）。
    attempt 只在成功 / invalidate 时清零——重试由调用方（ensure 的
    TTL/门控）驱动，不存在进程内紧循环，无需稳定窗归零（审计 H1：以
    last_success 锚定归零会让「曾成功过的 server 挂掉后永不降级、退避
    钉死 500ms」）。降级的自愈 = invalidate（改配置/改绑定/REST 触达）。
    """
    st = _state(server)
    if not force and time.monotonic() < st.next_allowed_ts:
        return False
    try:
        tools = await _fetch_tools(server)
    except Exception as e:
        st.attempt += 1
        delay_ms = min(
            MAX_DELAY_MS, INITIAL_DELAY_MS * (2 ** max(0, st.attempt - 1))
        )
        st.next_allowed_ts = time.monotonic() + delay_ms / 1000
        if st.attempt >= MAX_ATTEMPTS:
            _drop_server(server)
            st.degraded = True
            log.warning(
                "mcp_server_degraded",
                server=server,
                attempts=st.attempt,
                error=str(e)[:200],
            )
        else:
            log.info(
                "mcp_sync_retry_scheduled",
                server=server,
                attempt=st.attempt,
                delay_ms=delay_ms,
                error=str(e)[:200],
            )
        return False

    # Phase 2 原子换代
    _drop_server(server)
    names: list[str] = []
    for t in tools:
        public = public_tool_name(server, t["raw"])
        _tool_table[public] = McpToolEntry(
            server=server,
            raw_name=t["raw"],
            description=t["description"],
            input_schema=t["schema"],
        )
        names.append(public)
    _server_tools[server] = names
    st.attempt = 0
    st.degraded = False
    st.last_success_ts = time.time()
    st.next_allowed_ts = 0.0
    log.info("mcp_tools_synced", server=server, tools=len(names))
    return True


def _drop_server(server: str) -> None:
    for name in _server_tools.get(server, []):
        _tool_table.pop(name, None)
    _server_tools.pop(server, None)


async def invalidate(server: str) -> None:
    """配置/绑定变更触发的强制重同步（跳过退避闸门）。"""
    st = _state(server)
    st.degraded = False
    st.attempt = 0
    st.next_allowed_ts = 0.0
    await sync_server(server, force=True)


async def forget(server: str) -> None:
    """server 删除后的表清理（审计 M1）：摘工具 + 清状态。

    删除后走 invalidate 会因 "not configured" 进失败保留上一代路径——
    幽灵工具永久可见。forget 直接摘除，绑定 agent 的工具立即不可见。
    """
    _drop_server(server)
    _states.pop(server, None)


async def invalidate_agent(agent_id: str) -> None:
    """绑定变更后按需刷新该 agent 的工具表（ensure 的强制版）。

    绑定变更不改变 server 工具集本身，只改变可见方——这里把该 agent
    绑定的 server 全部强制重同步一遍（跳过退避闸门），fail-open。
    """
    try:
        for server in await agent_bound_servers(agent_id):
            await invalidate(server)
    except Exception as e:
        log.debug("mcp_invalidate_agent_failed", agent_id=agent_id, error=str(e))


async def ensure_agent_tools_synced(agent_id: str) -> None:
    """agent 视角按需同步：绑定的 server 过 TTL/退避闸门刷新。

    best-effort：绑定为空 / 全部降级 → 静默返回（工具不可见即降级语义）。
    """
    try:
        from hiveweave.services.mcp import mcp_service

        bound = await mcp_service.get_bound_mcp(agent_id)
    except Exception as e:
        log.debug("mcp_bound_lookup_failed", agent_id=agent_id, error=str(e))
        return
    now = time.monotonic()
    for server in bound or []:
        st = _state(str(server))
        if st.degraded:
            continue
        synced_names = _server_tools.get(str(server))
        fresh = (
            synced_names
            and st.last_success_ts > 0
            and (time.time() - st.last_success_ts) < TOOL_TTL_S
            and now >= st.next_allowed_ts
        )
        if fresh:
            continue
        await sync_server(str(server))


async def agent_bound_servers(agent_id: str) -> list[str]:
    try:
        from hiveweave.services.mcp import mcp_service

        return [str(s) for s in (await mcp_service.get_bound_mcp(agent_id))]
    except Exception:
        return []


async def agent_can_use(agent_id: str, public_name: str) -> bool:
    """绑定即用判定：server 在该 agent 绑定列表且工具在表。"""
    entry = _tool_table.get(public_name)
    if entry is None:
        return False
    bound = await agent_bound_servers(agent_id)
    return entry.server in bound
