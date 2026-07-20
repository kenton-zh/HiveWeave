"""AgentManager — 管理所有 agent task。

契约 04: 多 Agent 编排 (supervisor 部分)
- 管理所有 agent 的生命周期: start / stop
- 项目启动时为所有持久化 agent 启动 task

对应 Elixir:
- AgentSupervisor (DynamicSupervisor) — 管理 agent GenServer 进程
- ProjectSupervisor — 项目级 supervisor，spawn_agents/1 启动所有持久化 agent

Python 映射:
- Agent 是对象（不是长驻进程），LLM 调用是短生命周期 asyncio.Task
- AgentManager 是全局注册表，管理 Agent 对象
- 崩溃恢复由 Agent._consecutive_errors + _escalate_turn_interruption 处理（见 agent.py）
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from hiveweave.agents.agent import Agent, AgentState
from hiveweave.db import meta as meta_db

log = structlog.get_logger(__name__)


class AgentManager:
    """管理所有 agent task。

    全局单例（模块级 agent_manager）。
    对应 Elixir AgentSupervisor + ProjectSupervisor 的合并。

    用法::

        from hiveweave.agents.supervisor import agent_manager

        # 启动单个 agent
        agent = await agent_manager.start_agent(agent_id, project_id, config)

        # 项目启动时启动所有持久化 agent
        await agent_manager.start_project_agents(project_id)

        # 获取 agent
        agent = agent_manager.get_agent(agent_id)

        # 停止 agent
        await agent_manager.stop_agent(agent_id)
    """

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        """agent_id → Agent 实例。"""

    # ── 公共 API ─────────────────────────────────────────────

    async def start_agent(
        self,
        agent_id: str,
        project_id: str,
        config: dict,
        *,
        on_status_change=None,
        on_stream_event=None,
    ) -> Agent:
        """启动一个 agent。

        如果 agent 已存在，返回现有实例。
        对齐 Elixir agent_supervisor.ex:37 start_agent/2。

        Args:
            agent_id: Agent UUID
            project_id: 项目 UUID
            config: agent 配置 dict（来自 Meta DB agents 表）
            on_status_change: 状态变更回调
            on_stream_event: 流事件回调

        Returns:
            Agent 实例
        """
        # 已存在 → 返回现有
        existing = self._agents.get(agent_id)
        if existing is not None:
            log.debug("start_agent_exists", agent_id=agent_id)
            return existing

        # 创建新 Agent
        agent = Agent(
            agent_id=agent_id,
            project_id=project_id,
            config=config,
            on_status_change=on_status_change,
            on_stream_event=on_stream_event,
        )
        self._agents[agent_id] = agent

        log.info(
            "agent_started",
            agent_id=agent_id,
            project_id=project_id,
            name=config.get("name"),
            role=config.get("role"),
        )

        return agent

    async def stop_agent(self, agent_id: str) -> None:
        """停止一个 agent。

        取消所有正在运行的任务，从注册表移除。
        对齐 Elixir agent_supervisor.ex:50 stop_agent/2。
        """
        agent = self._agents.pop(agent_id, None)
        if agent is None:
            log.debug("stop_agent_not_found", agent_id=agent_id)
            return

        # 取消正在运行的任务
        try:
            await agent.cancel()
        except Exception as e:
            log.error(
                "stop_agent_cancel_failed",
                agent_id=agent_id,
                error=str(e),
            )

        log.info("agent_stopped", agent_id=agent_id)

    def get_agent(self, agent_id: str) -> Agent | None:
        """获取 agent 实例。"""
        return self._agents.get(agent_id)

    def list_processing(self) -> list[tuple[str, str]]:
        """列出所有 processing 状态的 agent。

        Returns:
            [(agent_id, project_id), ...]
        """
        return [
            (agent.id, agent.project_id)
            for agent in self._agents.values()
            if agent.status == AgentState.PROCESSING
        ]

    def list_all(self) -> list[Agent]:
        """列出所有已注册的 agent。"""
        return list(self._agents.values())

    async def start_project_agents(self, project_id: str) -> None:
        """项目启动时为所有持久化 agent 启动 task。

        对齐 Elixir project_supervisor.ex:67 spawn_agents/1。
        从 per-project DB 查询 agents，为每个创建 Agent 实例。
        """
        try:
            from hiveweave.db import project as project_db
            try:
                conn = await project_db.get_project_db_by_project_id(project_id)
            except project_db.ProjectDbError:
                log.info("start_project_agents_none", project_id=project_id)
                return

            # P0: heal orphan approved tasks (approved→verifying|closed) before agents wake
            try:
                from hiveweave.services.task import TaskService

                migrated = await TaskService().migrate_orphan_approved(project_id)
                if migrated.get("verifying") or migrated.get("closed"):
                    log.info(
                        "orphan_approved_migrated",
                        project_id=project_id,
                        **migrated,
                    )
            except Exception as e:
                log.warning(
                    "orphan_approved_migrate_failed",
                    project_id=project_id,
                    error=str(e),
                )

            # P2: heal executor worktrees before first LLM turn
            try:
                from hiveweave.services.git_worktree import (
                    heal_project_executor_worktrees,
                )

                healed = await heal_project_executor_worktrees(project_id)
                if healed.get("recovered") or healed.get("failed"):
                    log.info(
                        "worktree_heal_before_start",
                        project_id=project_id,
                        **healed,
                    )
            except Exception as e:
                log.warning(
                    "worktree_heal_before_start_failed",
                    project_id=project_id,
                    error=str(e),
                )

            # P0: 孤儿 worktree/残留分支回收对账（delete-safe 链路的最后一道）。
            # try/except 只告警不阻断启动 — reconcile 失败时 agents 照常唤醒。
            try:
                from hiveweave.services.git_worktree import reconcile_worktrees

                workspace_path = await meta_db.get_project_workspace(project_id)
                if workspace_path:
                    reconciled = await reconcile_worktrees(workspace_path)
                    if (
                        reconciled.get("pruned")
                        or reconciled.get("removed_dirs")
                        or reconciled.get("deleted_branches")
                        or reconciled.get("preserved_branches")
                        or reconciled.get("errors")
                    ):
                        log.info(
                            "worktree_reconcile_on_start",
                            project_id=project_id,
                            **reconciled,
                        )
            except Exception as e:
                log.warning(
                    "worktree_reconcile_on_start_failed",
                    project_id=project_id,
                    error=str(e),
                )

            # 同步 stale project_id — per-project DB 中的 agents.project_id
            # 可能在项目重建后残留旧值，导致按当前 project_id 查不到 agents
            try:
                cursor = await conn.execute(
                    "SELECT id FROM agents WHERE project_id != ? AND status = 'active'",
                    [project_id],
                )
                stale = await cursor.fetchall()
                await cursor.close()
                if stale:
                    log.warning(
                        "start_agents_stale_project_id",
                        project_id=project_id,
                        stale_count=len(stale),
                    )
                    await conn.execute(
                        "UPDATE agents SET project_id = ? WHERE project_id != ?",
                        [project_id, project_id],
                    )
                    await conn.commit()
                    log.info(
                        "start_agents_stale_project_id_fixed",
                        fixed_count=len(stale),
                    )
            except Exception as e:
                log.warning("start_agents_sync_project_id_failed", error=str(e))

            cursor = await conn.execute(
                "SELECT id, project_id, name, role, permission_type as role_type, backstory, "
                "model_id, goal, permission_mode, bound_skills, "
                "allowed_tools, denied_tools, ask_tools, "
                "short_id, status "
                "FROM agents WHERE project_id = ? AND status = 'active'",
                [project_id],
            )
            rows = await cursor.fetchall()
            await cursor.close()

            if not rows:
                log.info("start_project_agents_none", project_id=project_id)
                return

            started = 0
            skipped = 0

            for row in rows:
                agent_id = row["id"]

                # 跳过已存在的
                if agent_id in self._agents:
                    skipped += 1
                    continue

                config = dict(row)

                # 设置流式事件回调 — 连接到 WebSocket 广播
                # 没有这些回调，agent.chat() 不会广播 stream_chunk 事件，
                # 前端永远收不到响应，120 秒后超时
                from hiveweave.realtime.event_bus import create_agent_callbacks
                on_status, on_stream = create_agent_callbacks(
                    agent_id, project_id)

                try:
                    await self.start_agent(
                        agent_id, project_id, config,
                        on_status_change=on_status,
                        on_stream_event=on_stream,
                    )
                    started += 1
                except Exception as e:
                    log.error(
                        "start_project_agent_failed",
                        agent_id=agent_id,
                        project_id=project_id,
                        error=str(e),
                    )

            log.info(
                "start_project_agents_done",
                project_id=project_id,
                started=started,
                skipped=skipped,
                total=len(rows),
            )

        except Exception as e:
            log.error(
                "start_project_agents_error",
                project_id=project_id,
                error=str(e),
                exc_info=True,
            )

    async def stop_project_agents(self, project_id: str) -> None:
        """停止项目下所有 agent。"""
        agent_ids = [
            agent_id
            for agent_id, agent in self._agents.items()
            if agent.project_id == project_id
        ]

        for agent_id in agent_ids:
            await self.stop_agent(agent_id)

        log.info(
            "stop_project_agents_done",
            project_id=project_id,
            stopped=len(agent_ids),
        )

    # ── 状态查询 ─────────────────────────────────────────────

    def is_busy(self, agent_id: str) -> bool:
        """检查 agent 是否正在处理。"""
        agent = self._agents.get(agent_id)
        if agent is None:
            return False
        return agent.status == AgentState.PROCESSING


# ── 全局单例 ────────────────────────────────────────────────

agent_manager = AgentManager()
"""全局 AgentManager 实例。

对应 Elixir 的 AgentSupervisor 模块级注册名。
所有 agent 操作通过此单例进行。
"""
