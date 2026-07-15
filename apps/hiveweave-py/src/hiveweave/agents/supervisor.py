"""AgentManager — 管理所有 agent task，崩溃重启。

契约 04: 多 Agent 编排 (supervisor 部分)
- 管理所有 agent 的生命周期: start / stop / restart
- 崩溃重启: max_restarts=5, max_seconds=60（对齐 Elixir DynamicSupervisor）
- 项目启动时为所有持久化 agent 启动 task

对应 Elixir:
- AgentSupervisor (DynamicSupervisor) — 管理 agent GenServer 进程
- ProjectSupervisor — 项目级 supervisor，spawn_agents/1 启动所有持久化 agent

Python 映射:
- Agent 是对象（不是长驻进程），LLM 调用是短生命周期 asyncio.Task
- AgentManager 是全局注册表，管理 Agent 对象
- 崩溃重启: Agent._run_llm 内部 try/except 处理异常，AgentManager 跟踪崩溃频率
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from hiveweave.agents.agent import Agent, AgentState
from hiveweave.db import meta as meta_db

log = structlog.get_logger(__name__)

# ── 常量（对齐 Elixir agent_supervisor.ex:28-30）─────────────

MAX_RESTARTS = 5
"""单个 agent 在 MAX_SECONDS 内最大重启次数。"""

MAX_SECONDS = 60
"""重启频率统计窗口（秒）。"""


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

        self._crash_history: dict[str, list[float]] = {}
        """agent_id → 崩溃时间戳列表（用于重启频率限制）。"""

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

        # 清理崩溃历史
        self._crash_history.pop(agent_id, None)

        log.info("agent_stopped", agent_id=agent_id)

    async def restart_agent(self, agent_id: str) -> Agent | None:
        """重启崩溃的 agent。

        对齐 Elixir DynamicSupervisor 的 :transient 重启策略。
        检查重启频率：如果 MAX_SECONDS 内重启超过 MAX_RESTARTS 次，拒绝重启。

        Args:
            agent_id: 要重启的 agent ID

        Returns:
            新的 Agent 实例，或 None（如果拒绝重启）
        """
        # 记录崩溃时间
        if not self._should_restart(agent_id):
            log.error(
                "restart_rate_limited",
                agent_id=agent_id,
                max_restarts=MAX_RESTARTS,
                window_seconds=MAX_SECONDS,
                msg="agent crashed too many times, refusing to restart",
            )
            return None

        # 获取现有 agent 的配置
        old_agent = self._agents.get(agent_id)
        if old_agent is None:
            log.warning("restart_agent_not_found", agent_id=agent_id)
            return None

        project_id = old_agent.project_id
        config = old_agent.config

        # R11: stop_agent 会清空 _crash_history，但重启需要保留崩溃历史
        # 才能让频率限制（MAX_SECONDS 内 MAX_RESTARTS 次）真正生效。
        # 否则每次重启都清零，频率限制永远不触发。
        saved_history = self._crash_history.get(agent_id, [])

        # 停止旧 agent
        await self.stop_agent(agent_id)

        # 恢复崩溃历史（stop_agent 已清空）
        self._crash_history[agent_id] = saved_history

        # 启动新 agent
        new_agent = await self.start_agent(agent_id, project_id, config)

        log.info(
            "agent_restarted",
            agent_id=agent_id,
            restart_count=len(self._crash_history.get(agent_id, [])),
        )

        return new_agent

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
            conn = await project_db.get_project_db_by_project_id(project_id)
            if conn is None:
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

    # ── 崩溃跟踪 ─────────────────────────────────────────────

    def _should_restart(self, agent_id: str) -> bool:
        """检查 agent 是否应该被重启（频率限制）。

        对齐 Elixir DynamicSupervisor 的 max_restarts/max_seconds 语义。
        如果 MAX_SECONDS 内重启次数 >= MAX_RESTARTS，返回 False。

        Args:
            agent_id: Agent ID

        Returns:
            True = 可以重启, False = 超过频率限制
        """
        now = time.time()
        history = self._crash_history.setdefault(agent_id, [])

        # 清理过期记录
        cutoff = now - MAX_SECONDS
        history[:] = [t for t in history if t > cutoff]

        # 检查频率
        if len(history) >= MAX_RESTARTS:
            return False

        # 记录本次崩溃
        history.append(now)
        return True

    def _record_crash(self, agent_id: str) -> None:
        """记录 agent 崩溃（用于频率统计）。"""
        now = time.time()
        history = self._crash_history.setdefault(agent_id, [])
        cutoff = now - MAX_SECONDS
        history[:] = [t for t in history if t > cutoff]
        history.append(now)

    def get_crash_count(self, agent_id: str) -> int:
        """获取 agent 在 MAX_SECONDS 窗口内的崩溃次数。"""
        now = time.time()
        history = self._crash_history.get(agent_id, [])
        cutoff = now - MAX_SECONDS
        recent = [t for t in history if t > cutoff]
        return len(recent)

    # ── 状态查询 ─────────────────────────────────────────────

    def get_status(self, agent_id: str) -> str | None:
        """获取 agent 状态字符串。"""
        agent = self._agents.get(agent_id)
        if agent is None:
            return None
        return agent.status.value

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
