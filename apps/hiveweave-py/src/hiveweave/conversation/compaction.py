"""Conversation compaction — summarize old turns when history exceeds budget.

契约 03: 对话历史与压缩
- 85% 阈值触发压缩
- LLM 生成结构化摘要（Goal/Constraints/Progress/Decisions/Next Steps/Critical Context/Relevant Files）
- 摘要存入独立 compacted_prefix_cache，不混入 history（RECONCILE A1 修正）
- LLM 失败时回退到硬截断
"""

from typing import Awaitable, Callable

import structlog

from hiveweave.conversation.token_utils import (
    COMPACTION_BUFFER,
    PRESERVE_RECENT_MAX,
    PRESERVE_RECENT_MIN,
    TOOL_OUTPUT_MAX_CHARS,
    estimate_tokens,
    estimate_tokens_for_messages,
)

logger = structlog.get_logger()

# ── 常量 ────────────────────────────────────────────────────
COMPACTION_TRIGGER_RATIO = 0.85
SUMMARY_TEMPERATURE = 0.3
SUMMARY_MAX_TOKENS = 2000

# 摘要消息特殊标记 — store 据此识别并提取到 compacted_prefix_cache
SUMMARY_MARKER = "[Earlier conversation summary]"

# LLM 回调类型：(prompt: str) -> summary_text | None
LLMCallback = Callable[[str], Awaitable[str | None]]


class Compaction:
    """对话历史压缩逻辑。"""

    def check_overflow(self, total_tokens: int, context_window: int) -> int | None:
        """检查是否需要压缩，返回目标 budget 或 None。

        当 total_tokens > (context_window - COMPACTION_BUFFER) * 0.85 时触发。
        """
        if context_window <= 0:
            return None
        budget = context_window - COMPACTION_BUFFER
        if budget <= 0:
            return None
        if total_tokens > budget * COMPACTION_TRIGGER_RATIO:
            return budget
        return None

    def should_compact(self, total_tokens: int, context_window: int) -> bool:
        """判断是否达到 85% 压缩阈值。"""
        return self.check_overflow(total_tokens, context_window) is not None

    async def compact(
        self,
        messages: list[dict],
        target_budget: int,
        llm_callback: LLMCallback | None = None,
    ) -> list[dict]:
        """压缩消息列表：LLM 摘要旧消息 + 保留近期消息。

        返回 [summary_msg] + to_keep（近期消息）。
        LLM 失败或无回调时回退到硬截断。
        摘要消息用 role=system + SUMMARY_MARKER 标记，store 负责提取到独立缓存。
        """
        if not messages:
            return messages

        # 确定分割点（消息条数量纲，对齐 Elixir）
        recent_count = min(
            PRESERVE_RECENT_MAX,
            max(PRESERVE_RECENT_MIN, len(messages) // 3),
        )
        split_idx = max(0, len(messages) - recent_count)
        old_messages = messages[:split_idx]
        recent_messages = messages[split_idx:]

        if not old_messages:
            # 无旧消息可压缩 — 直接硬截断
            return self._trim_to_budget(messages, target_budget)

        # 构建 LLM 摘要 prompt 并调用
        summary = None
        if llm_callback is not None:
            prompt = self._build_compaction_prompt(
                self._format_for_summary(old_messages)
            )
            try:
                summary = await llm_callback(prompt)
            except Exception as e:
                logger.warning("compaction_llm_failed", error=str(e))
                summary = None

        if not summary:
            logger.info(
                "compaction_fallback_trim",
                old_count=len(old_messages),
            )
            return self._trim_to_budget(messages, target_budget)

        logger.info(
            "compaction_done",
            old_count=len(old_messages),
            summary_chars=len(summary),
        )
        summary_msg = {
            "role": "system",
            "content": (
                f"{SUMMARY_MARKER}\n\n{summary}\n\n"
                "---\nBelow is the recent conversation:"
            ),
        }
        return [summary_msg] + recent_messages

    # ── 摘要 prompt 构建 ─────────────────────────────────────

    @staticmethod
    def _build_compaction_prompt(transcript: str) -> str:
        """构建结构化摘要 prompt（OpenCode compaction 模板）。"""
        return (
            "Create a concise anchored summary from the conversation history below.\n\n"
            "## Summary Format (preserve ALL sections)\n"
            "### Goal\n(What is the user trying to accomplish?)\n\n"
            "### Constraints & Preferences\n(Technical constraints, style preferences, requirements)\n\n"
            "### Progress\n- **Done**: (Completed work)\n"
            "- **In Progress**: (Current tasks)\n"
            "- **Blocked**: (Blockers with reasons)\n\n"
            "### Key Decisions\n(Important decisions and their rationale)\n\n"
            "### Next Steps\n(What needs to happen next)\n\n"
            "### Critical Context\n(Any other context the assistant needs to continue effectively)\n\n"
            "### Relevant Files\n(Important file paths mentioned)\n\n"
            "## Rules\n"
            "- Use concise bullet points\n"
            "- Preserve exact file paths, commands, error strings\n"
            "- Do NOT mention the summarization process itself\n"
            "- Keep all sections even if empty (write \"None\" if applicable)\n\n"
            f"## Conversation to summarize:\n{transcript}"
        )

    @staticmethod
    def _format_for_summary(messages: list[dict]) -> str:
        """格式化消息为摘要 transcript，工具输出截断到 TOOL_OUTPUT_MAX_CHARS。"""
        parts = []
        for m in messages:
            role = m.get("role", "unknown")
            content = _safe_content(m.get("content"))
            if len(content) > TOOL_OUTPUT_MAX_CHARS:
                content = (
                    content[:TOOL_OUTPUT_MAX_CHARS]
                    + f"...[truncated {len(content) - TOOL_OUTPUT_MAX_CHARS} chars]"
                )
            tool_info = ""
            tcs = m.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                lines = []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    name = fn.get("name", "unknown")
                    args = fn.get("arguments", "") or ""
                    if len(args) > 200:
                        args = args[:200] + "..."
                    lines.append(f"  - {name}({args})")
                tool_info = "\n[Tool calls:\n" + "\n".join(lines) + "]"
            parts.append(f"[{role}]: {content}{tool_info}")
        return "\n\n".join(parts)

    @staticmethod
    def _trim_to_budget(messages: list[dict], budget: int) -> list[dict]:
        """硬截断回退：从最旧消息开始移除直到在预算内（不拆 tool 对）。"""
        if budget <= 0:
            return messages[-2:] if len(messages) > 2 else list(messages)
        result = list(messages)
        total = estimate_tokens_for_messages(result)
        while result and total > budget:
            # 尝试成对移除 tool_calls + tool_result
            drop_count = 1
            if len(result) > 1 and "tool_calls" in result[0] and "tool_call_id" in result[1]:
                drop_count = 2
            dropped = estimate_tokens_for_messages(result[:drop_count])
            result = result[drop_count:]
            total -= dropped
        return result


def _safe_content(content) -> str:
    """规范化 content 为字符串（多模态 content 可能是列表）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in content
        )
    return str(content)


async def resolve_compactor_callback(agent_id: str) -> LLMCallback | None:
    """解析 agent 的 compactor LLM 回调。

    查询 Meta DB 获取 agent 的模型（或首个活跃模型），构建 OpenAI 兼容回调。
    无可用模型时返回 None（compact 将回退到硬截断）。
    """
    from hiveweave.db import meta as meta_db

    try:
        # 1. 取 agent 的 model_id
        agent_row = await meta_db.query_one(
            "SELECT model_id FROM agents WHERE id = ? LIMIT 1", [agent_id]
        )
        model_id = agent_row["model_id"] if agent_row else None

        # 2. 按 model_id 取模型，否则取首个活跃模型
        model = None
        if model_id:
            model = await meta_db.query_one(
                "SELECT model_id, base_url, api_key FROM llm_models WHERE id = ? LIMIT 1",
                [model_id],
            )
        if model is None:
            model = await meta_db.query_one(
                "SELECT model_id, base_url, api_key FROM llm_models "
                "WHERE is_active = 1 ORDER BY created_at ASC LIMIT 1",
                [],
            )
        if model is None or not model["base_url"]:
            return None

        base_url = str(model["base_url"]).rstrip("/")
        api_key = str(model["api_key"] or "")
        model_name = str(model["model_id"])

        async def callback(prompt: str) -> str | None:
            return await _call_compactor_llm(base_url, api_key, model_name, prompt)

        return callback
    except Exception as e:
        logger.warning("resolve_compactor_failed", agent_id=agent_id, error=str(e))
        return None


async def _call_compactor_llm(
    base_url: str, api_key: str, model: str, prompt: str
) -> str | None:
    """调用 OpenAI 兼容 API 生成摘要。"""
    import httpx

    url = f"{base_url}/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": SUMMARY_TEMPERATURE,
        "max_tokens": SUMMARY_MAX_TOKENS,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("compactor_llm_http_error", status=resp.status_code)
            return None
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        content = choices[0].get("message", {}).get("content")
        return content if content else None
    except Exception as e:
        logger.warning("compactor_llm_call_failed", error=str(e))
        return None
