"""AgentRouter — in-memory agent_id → project_id routing.

替代 Meta DB 中的 agent_index 表。启动时遍历所有 per-project DB 重建路由表。
create_agent / delete_agent 时同步更新内存映射。

另有**瞬态身份**一族（`register_transient` 等）：承载**不落库的运行时 id**
（当前唯一生产者 = `sub-*` 子代理）。它们不在 `rebuild()` 的扫描范围内，
所以只在正式路由里找它们的调用方**必然解析失败**（D67-1 实测：
`event_audit` 对子代理 23/23 全丢）。两族**物理隔离**，互不污染。

性能: O(1) 查找，启动时 O(N) 重建（N = 所有项目的 agent 总数）。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# 瞬态身份（**不落库的运行时 id**）登记上限。
# 现存唯一生产者是 `tools/subagent.py` 的 `sub-<parent>-<suffix>` —— 它
# **运行时临时生成、从不入库**，所以 `rebuild()`/`register_project()` 永远
# 看不到它，而一切按 agent_id 路由的 DB 访问对它**必然解析失败**。
# 正常路径在子代理结束时 `unregister_transient`；本上限只是异常路径
# （进程被取消 / 父协程崩溃）的兜底，按**登记先后**驱逐最旧的。
_TRANSIENT_MAX = 512


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
        # 瞬态身份（见 `_TRANSIENT_MAX` 注释）：agent_id → project_id。
        # ⚠ **与 `_routes` 物理隔离**，且**刻意不参与** `list_active_routes` /
        # `get_project_agent_ids` / `_project_agents` —— 否则一个短命的
        # `sub-*` 会作为"幽灵成员"漏进组织面与前端名册。
        self._transient: OrderedDict[str, str] = OrderedDict()

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

    async def register_project(self, project_id: str) -> int:
        """把项目 DB 里已存在但路由表缺失的 agent 幂等补注册（收养路径）。

        收养（跨机搬迁/新 Meta DB 重新登记）沿用旧 agent id 且 seed 幂等
        跳过 create_agent → 路由表无人登记旧 id → 一切按 agent_id 路由的
        查询（chat/todos/inbox/记忆…）抛 ProjectDbError，前端表现为
        「团队在、聊天空白」（2026-09-08 EXE 收养老项目实锤）。

        幂等：已在表中的 agent 跳过（不覆盖 create_agent 登记的字段）。
        Returns: 本次新注册数量。
        """
        from hiveweave.db import meta as meta_db
        from hiveweave.db import project as project_db

        ws_row = await meta_db.query_one(
            "SELECT workspace_path FROM projects WHERE id = ?", [project_id]
        )
        if not ws_row or not ws_row["workspace_path"]:
            return 0
        ws = ws_row["workspace_path"]
        try:
            conn = await project_db.get_project_db_by_project_id(project_id)
            cursor = await conn.execute(
                "SELECT id, short_id, name, role, status FROM agents "
                "WHERE status = 'active'",
            )
            rows = await cursor.fetchall()
            await cursor.close()
        except Exception as e:
            log.warning(
                "agent_router_register_project_failed",
                project_id=project_id,
                error=str(e),
            )
            return 0

        added = 0
        for row in rows:
            r = dict(row)
            aid = r["id"]
            if aid in self._routes:
                continue
            sid = r.get("short_id") or ""
            self._routes[aid] = AgentRoute(
                agent_id=aid,
                project_id=project_id,
                workspace_path=ws,
                short_id=sid,
                name=r.get("name", ""),
                role=r.get("role", ""),
                status=r.get("status", "active"),
            )
            if sid:
                self._short_ids[sid] = aid
            self._project_agents.setdefault(project_id, []).append(aid)
            added += 1
        if added:
            log.info(
                "agent_router_project_registered",
                project_id=project_id,
                registered=added,
            )
        return added

    def reset_for_tests(self) -> None:
        """清空全部路由（仅测试用——生产路由生命周期由 rebuild/register 管理）。"""
        self._routes.clear()
        self._short_ids.clear()
        self._project_agents.clear()
        self._transient.clear()

    def get_project_id(self, agent_id: str) -> str | None:
        """agent_id → project_id，O(1) 查找。"""
        route = self._routes.get(agent_id)
        return route.project_id if route else None

    # ── 瞬态身份（不落库的运行时 id，如 `sub-*` 子代理）────────────
    # 为什么需要单独一族方法而不是把 `sub-*` 塞进 `_routes`：
    #   `_routes` 是**组织面的事实源**（`list_active_routes` 供 org/前端名册）；
    #   塞进去 ⇒ 每个子代理都会以"成员"身份出现并残留（它没有 name/role/短名，
    #   也永远不会被 `unregister` 之外的路径清理）。而我们要的只是**DB 路由**。
    # 判据性质：这是**状态判据** —— project_id 在**子代理产生的那一刻**由
    #   父的 `project_id` 写入（`tools/subagent.py::spawn_subagent_tool`），
    #   不是运行时去猜 id 的字面形状（形如 `sub-<parent>-<suffix>` 的字符串
    #   解析属文本判据，换前缀即失效 ⇒ 不入此族）。

    def register_transient(self, agent_id: str, project_id: str) -> None:
        """登记**运行时临时身份** → 所属项目（唯一消费者：取证类 DB 路由）。

        幂等：重复登记同一 `agent_id` 覆盖并刷新 LRU 次序。
        有界：超过 `_TRANSIENT_MAX` 驱逐**最早登记**的（异常路径兜底）。
        """
        aid = str(agent_id or "").strip()
        pid = str(project_id or "").strip()
        if not aid or not pid:
            # 空值不得进表：否则"查得到但查出来是空串"会把调用方的
            # `or ""` 判空逻辑变成哑弹（劣化成"看起来路由成功"）。
            return
        self._transient[aid] = pid
        self._transient.move_to_end(aid)
        while len(self._transient) > _TRANSIENT_MAX:
            evicted, _ = self._transient.popitem(last=False)
            # **warning 而非 debug**（审计 P2-b）：驱逐意味着"有生产者漏了
            # `unregister_transient`"（`_work` 的 `finally` 是唯一的正常出口）
            # —— 它是一条**告警信号**，而且此刻被逐掉的那条身份的后续事件会
            # 解析失败（症状回到 D67-1 本身）。debug 级等于把信号藏起来。
            log.warning(
                "agent_router.transient_evicted",
                agent_id=evicted,
                reason="超过 _TRANSIENT_MAX —— 生产者可能漏了 unregister_transient",
                cap=_TRANSIENT_MAX,
            )

    def unregister_transient(self, agent_id: str) -> None:
        """注销瞬态身份（子代理结束时调用；不在表内为无害空操作）。"""
        self._transient.pop(str(agent_id or "").strip(), None)

    def resolve_transient_project_id(self, agent_id: str) -> str | None:
        """**只查瞬态表**：`agent_id` → project_id（正式路由不在此列）。

        刻意不回落 `get_project_id` —— 两条通道各自可断言，消费方按次序
        组合（见 `services/event_audit.py::_resolve_project_id`），
        免得"两个判据取其一"变成"谁都负责、谁都说不清"。
        """
        return self._transient.get(str(agent_id or "").strip())

    def transient_count(self) -> int:
        """当前登记的瞬态身份数量（供有界性断言/诊断）。"""
        return len(self._transient)

    def clear_transient_for_project(self, project_id: str) -> int:
        """摘掉指向某项目的全部瞬态身份（项目删除时用）。"""
        pid = str(project_id or "").strip()
        gone = [k for k, v in self._transient.items() if v == pid]
        for k in gone:
            self._transient.pop(k, None)
        return len(gone)

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
        # 瞬态身份同批摘掉：项目没了，子代理的事件不该再往一个已删项目的
        # workspace 上写（那边只会 raise ProjectDbError，徒增噪声）。
        self.clear_transient_for_project(project_id)
        if agent_ids:
            log.info(
                "agent_router_project_cleared",
                project_id=project_id,
                cleared=len(agent_ids),
            )


# 全局单例
agent_router = AgentRouter()
