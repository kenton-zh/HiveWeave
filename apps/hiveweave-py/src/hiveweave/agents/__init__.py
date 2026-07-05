"""Agent orchestration — 多 agent 编排系统（契约 04）。

批次 3 核心模块，整合批次 1（LLM/Tools/Conversation）和批次 2（Services/Prompts）。

导出:
    Agent          — 单个 agent 的 asyncio task 封装（状态机 + chat/trigger/cancel）
    AgentState     — Agent 状态枚举（IDLE / PROCESSING）
    AgentManager   — 管理所有 agent task（启动/停止/崩溃重启）
    agent_manager  — 全局 AgentManager 单例
    trigger_subordinate — 触发下属 executor
    trigger_coordinator — 触发 coordinator
    build_trigger_context — 构建触发上下文消息

模块依赖链（无循环）:
    __init__ → agent.py → trigger.py → supervisor.py → agent.py
    agent.py 在方法内延迟导入 trigger.py 的函数
    trigger.py 在函数内延迟导入 supervisor.py 的单例
"""

from hiveweave.agents.agent import Agent, AgentState
from hiveweave.agents.supervisor import AgentManager, agent_manager
from hiveweave.agents.trigger import (
    build_trigger_context,
    trigger_coordinator,
    trigger_subordinate,
)

__all__ = [
    "Agent",
    "AgentState",
    "AgentManager",
    "agent_manager",
    "trigger_subordinate",
    "trigger_coordinator",
    "build_trigger_context",
]
