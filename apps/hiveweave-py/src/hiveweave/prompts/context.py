"""build_context_prompt — 动态上下文提示词（契约 13）.

每轮从 memories / handoffs / goals / involvement / skills 重建。
放在 history 之后（非之前）以保护 LLM API 的 prefix cache：
    [System 1 identity] → [history] → [System 2 context] → [user]

纯函数设计：caller 预取 memories / handoffs / goals / bound_skills 后传入。
不访问 DB，不阻塞网络（build_active_skills_section 仅查外部 + 内置，不查 ClawHub）。

段顺序（与 Elixir streamer.ex build_context_prompt 一致）：
    involvement → goals → memory → skills
handoffs 段为 Python 迁移新增（Elixir 未实现，契约 13 §build_context_prompt 列出）。
空段自动跳过；全空返回 ""（caller 决定是否跳过 System 2）。

移植自 Elixir streamer.ex: build_context_prompt + build_memory_block。

R8 契约说明：本模块返回 str 而非 dict（{"role": "system", "content": ...}）。
caller 需要自行包装为 system message dict，以便灵活控制消息布局（如 prefix cache
分槽、跳过空 context 段等）。见 agent.py:_build_messages 中的用法。
"""

from __future__ import annotations

import json
from typing import Any

from hiveweave.prompts.goals import format_goals_block
from hiveweave.prompts.involvement import (
    build_involvement_block,
    normalize_involvement_level,
)
from hiveweave.services.skill_registry import SkillRegistryService


# memory / handoff 内容截断长度（与 memory_service._TRUNCATE_LEN 一致）
_TRUNCATE_LEN: int = 200


def build_context_prompt(
    agent_id: str,
    memories: list[dict] | None,
    handoffs: list[dict] | None,
    *,
    goals: dict[str, Any] | None = None,
    involvement_level: str | None = None,
    bound_skills: list[str] | str | None = None,
    memory_text: str | None = None,
    project_rules: str | None = None,
) -> str:
    """构建动态上下文提示词（第 2 条 system 消息内容）。

    参数：
        agent_id:           agent ID（保留参数，便于 caller 一致调用）
        memories:           agent 私有记忆列表（已预取）；每项含 type / content
        handoffs:           交接记录列表（已预取）；每项含 type / summary / content
        goals:              企业目标 dict（已预取）；None 不注入 goals workbook
        involvement_level:  参与度级别（high/medium/low，或 legacy 自由文本）；
                            None 不注入 involvement 段
        bound_skills:       已绑定技能：list[str] 或 JSON 字符串或 None
        memory_text:        预构建的记忆段文本（优先于 memories 列表；
                            caller 可直接传 memory_service.build_agent_context 输出）
        project_rules:      项目特有规则（从 charter 加载）；None 或空不注入

    返回：
        上下文提示词字符串。空则返回 ""（caller 应跳过 System 2）。
        caller 负责包装为 `{"role": "system", "content": <返回值>}`。

    段顺序：project_rules → involvement → goals → memory(含 handoffs) → skills
    """
    parts: list[str] = []

    # 0. Project Rules（项目特有约束，CEO 摸底后填入 charter）
    if project_rules and project_rules.strip():
        parts.append(f"## Project Rules\n{project_rules.strip()}")

    # 1. User Involvement
    if involvement_level:
        level = normalize_involvement_level(involvement_level)
        parts.append(build_involvement_block(level))

    # 2. Goals workbook（仅 caller 判定 dirty 后传入）
    if goals:
        parts.append(format_goals_block(goals))

    # 3. Memory + Handoffs
    mem_block = _format_memory_block(memories, handoffs, memory_text)
    if mem_block:
        parts.append(mem_block)

    # 4. Active Skills（仅摘要，全文运行时 read_skill 加载）
    skill_block = _build_skills_section(bound_skills)
    if skill_block:
        parts.append(skill_block)

    return "\n\n".join(parts)


# ── Helpers ─────────────────────────────────────────────────


def _format_memory_block(
    memories: list[dict] | None,
    handoffs: list[dict] | None,
    memory_text: str | None,
) -> str:
    """构建 memory 段。

    优先使用 caller 预构建的 memory_text（如 memory_service.build_agent_context
    的输出，含 Project Constitution / Private Working Memory / Archived 三层）。
    否则从 memories + handoffs 列表自行渲染。

    全空时返回 ""（caller 跳过）。
    """
    blocks: list[str] = []

    if memory_text:
        # caller 预构建的完整 memory 段（已含三层结构）
        blocks.append(memory_text)
    elif memories:
        items = "\n".join(
            f"- [{m.get('type', 'fact')}] {_truncate(m.get('content', ''))}"
            for m in memories
        )
        blocks.append(f"## Your Private Working Memory\n{items}")

    if handoffs:
        items = "\n".join(
            f"- [{h.get('type', 'handoff')}] "
            f"{_truncate(h.get('summary') or h.get('content', ''))}"
            for h in handoffs
        )
        blocks.append(f"## Handoffs (from predecessors)\n{items}")

    if not blocks:
        return ""
    return "\n\n".join(blocks)


def _build_skills_section(bound_skills: list[str] | str | None) -> str:
    """构建 Active Skills 摘要段。

    接受 list[str] 或 JSON 字符串（与 agents.bound_skills 列格式一致）。
    委托 SkillRegistryService.build_active_skills_section（仅查外部 + 内置，
    不查 ClawHub，避免 prompt 构建时阻塞网络）。
    """
    if bound_skills is None:
        return ""
    if isinstance(bound_skills, str):
        return SkillRegistryService.build_active_skills_section(bound_skills)
    # list[str] → 序列化为 JSON 字符串交给 build_active_skills_section
    if not bound_skills:
        return ""
    return SkillRegistryService.build_active_skills_section(json.dumps(bound_skills))


def _truncate(text: str | None, length: int = _TRUNCATE_LEN) -> str:
    """截断长文本，与 memory_service._TRUNCATE_LEN 保持一致。"""
    if not text:
        return ""
    if len(text) > length:
        return text[:length] + "..."
    return text
