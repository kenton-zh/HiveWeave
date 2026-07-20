"""AgentRouter — in-memory agent_id → project_id routing.

替代 Meta DB 中的 agent_index 表。启动时遍历所有 per-project DB 重建路由表。
create_agent / delete_agent 时同步更新内存映射。

性能: O(1) 查找，启动时 O(N) 重建（N = 所有项目的 agent 总数）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class AgentRoute:
    """轻量路由信息 — 替代 agent_index 表的一行。"""

    agent_id: str
    project_id: str
    workspace_path: str
    short_id: str
    name: str
    role: str
    status: str


class AgentRouter:
    """内存路由表 — agent_id → project_id + 展示字段。

    线程安全: asyncio 单线程模型，无需锁。
    生命周期: 后端启动时 rebuild()，create/delete agent 时 register/unregister。
    """

    def __init__(self) -> None:
        self._routes: dict[str, AgentRoute] = {}
        self._short_ids: dict[str, str] = {}  # short_id → agent_id
        self._project_agents: dict[str, list[str]] = {}  # project_id → [agent_id]

    async def rebuild(self) -> int:
        """启动时遍历所有 per-project DB 重建路由表。

        Returns:
            重建的 agent 路由数量。
        """
        from hiveweave.db import meta as meta_db
        from hiveweave.db import project as project_db

        self._routes.clear()
        self._short_ids.clear()
        self._project_agents.clear()

        projects = await meta_db.query("SELECT id, workspace_path FROM projects")
        total = 0
        for p in projects:
            pid = p["id"]
            ws = p["workspace_path"] or ""
            try:
                conn = await project_db.get_project_db_by_project_id(pid)
                cursor = await conn.execute(
                    "SELECT id, short_id, name, role, status FROM agents "
                    "WHERE status = 'active'",
                )
                rows = await cursor.fetchall()
                await cursor.close()
                for row in rows:
                    r = dict(row)
                    aid = r["id"]
                    sid = r.get("short_id") or ""
                    route = AgentRoute(
                        agent_id=aid,
                        project_id=pid,
                        workspace_path=ws,
                        short_id=sid,
                        name=r.get("name", ""),
                        role=r.get("role", ""),
                        status=r.get("status", "active"),
                    )
                    self._routes[aid] = route
                    if sid:
                        self._short_ids[sid] = aid
                    self._project_agents.setdefault(pid, []).append(aid)
                    total += 1
            except Exception as e:
                log.warning(
                    "agent_router_rebuild_project_failed",
                    project_id=pid,
                    error=str(e),
                )

        log.info("agent_router_rebuilt", total_agents=total, projects=len(projects))
        return total

    def get_project_id(self, agent_id: str) -> str | None:
        """agent_id → project_id，O(1) 查找。"""
        route = self._routes.get(agent_id)
        return route.project_id if route else None

    def get_route(self, agent_id: str) -> AgentRoute | None:
        """获取完整路由信息。"""
        return self._routes.get(agent_id)

    def get_workspace_path(self, agent_id: str) -> str | None:
        """agent_id → workspace_path。"""
        route = self._routes.get(agent_id)
        return route.workspace_path if route else None

    def find_by_short_id(self, short_id: str) -> AgentRoute | None:
        """short_id → AgentRoute，O(1) 查找。"""
        aid = self._short_ids.get(short_id)
        return self._routes.get(aid) if aid else None

    def find_by_uuid_prefix(self, prefix: str, limit: int = 5) -> list[AgentRoute]:
        """UUID 前缀匹配，O(N) 扫描但 N 通常很小。"""
        results: list[AgentRoute] = []
        for aid, route in self._routes.items():
            if aid.startswith(prefix):
                results.append(route)
                if len(results) >= limit:
                    break
        return results

    def get_project_agent_ids(self, project_id: str) -> list[str]:
        """获取项目下所有 agent_id。"""
        return list(self._project_agents.get(project_id, []))

    def list_all_short_ids(self) -> list[str]:
        """获取所有 short_id（用于 generate_short_id 全局唯一性检查）。"""
        return list(self._short_ids.keys())

    def list_active_routes(self) -> list[AgentRoute]:
        """列出所有活跃 agent 路由。"""
        return list(self._routes.values())

    def register(self, route: AgentRoute) -> None:
        """注册新 agent 路由（create_agent 时调用）。"""
        self._routes[route.agent_id] = route
        if route.short_id:
            self._short_ids[route.short_id] = route.agent_id
        self._project_agents.setdefault(route.project_id, []).append(route.agent_id)
        log.info(
            "agent_router_registered",
            agent_id=route.agent_id,
            name=route.name,
            project_id=route.project_id,
        )

    def update(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        role: str | None = None,
        status: str | None = None,
        short_id: str | None = None,
    ) -> None:
        """更新 agent 路由信息（update_agent 时调用）。"""
        route = self._routes.get(agent_id)
        if not route:
            return
        if name is not None:
            route.name = name
        if role is not None:
            route.role = role
        if status is not None:
            route.status = status
        if short_id is not None and short_id != route.short_id:
            if route.short_id:
                self._short_ids.pop(route.short_id, None)
            route.short_id = short_id
            self._short_ids[short_id] = agent_id

    def unregister(self, agent_id: str) -> None:
        """移除 agent 路由（delete_agent 时调用）。"""
        route = self._routes.pop(agent_id, None)
        if route:
            if route.short_id:
                self._short_ids.pop(route.short_id, None)
            agents = self._project_agents.get(route.project_id, [])
            if agent_id in agents:
                agents.remove(agent_id)
            log.info(
                "agent_router_unregistered",
                agent_id=agent_id,
                name=route.name,
                project_id=route.project_id,
            )

    def clear_project(self, project_id: str) -> None:
        """移除项目下所有 agent 路由（delete_project 时调用）。"""
        agent_ids = self._project_agents.pop(project_id, [])
        for aid in agent_ids:
            route = self._routes.pop(aid, None)
            if route and route.short_id:
                self._short_ids.pop(route.short_id, None)
        if agent_ids:
            log.info(
                "agent_router_project_cleared",
                project_id=project_id,
                cleared=len(agent_ids),
            )


# 全局单例
agent_router = AgentRouter()
