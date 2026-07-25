"""ETHOS 提示词体系 — 契约 13.

三层提示词架构：
  1. ETHOS 共享层（三原则 + 角色纪律四件套）
  2. 角色类型约束层（coordinator / executor）
  3. 角色专属剧本层（CEO / HR / Generic + 6 种 Executor 子类型）

消息布局（DeepSeek prefix cache 友好）：
    [System 1] identity prompt（静态，同一 agent 跨 turn 不变）
    [history]   conversation history（过滤 system 消息）
    [System 2] context prompt（动态，每轮重建）
    [user]      当前消息（前缀 "[来自: 用户] "）

模块导出：
  - build_identity_prompt:   静态身份提示词（第 1 条 system 消息）
  - build_context_prompt:    动态上下文提示词（第 2 条 system 消息）
  - build_involvement_block: 用户参与度段
  - build_coordinator_script: coordinator 角色剧本（CEO / HR / Generic）
  - build_executor_script:    executor 角色剧本（6 子函数分发）
  - format_goals_block:       企业目标 workbook 段
  - normalize_involvement_level: 参与度级别规整
  - is_chinese_model:         中文模型检测
"""

from hiveweave.prompts.context import build_context_prompt
from hiveweave.prompts.coordinator import build_coordinator_script
from hiveweave.prompts.executor import build_executor_script
from hiveweave.prompts.goals import format_goals_block
from hiveweave.prompts.identity import (
    build_identity_prompt,
    is_chinese_model,
)
from hiveweave.prompts.involvement import (
    build_involvement_block,
    normalize_involvement_level,
)

__all__ = [
    "build_identity_prompt",
    "build_context_prompt",
    "build_involvement_block",
    "build_coordinator_script",
    "build_executor_script",
    "format_goals_block",
    "normalize_involvement_level",
    "is_chinese_model",
]
