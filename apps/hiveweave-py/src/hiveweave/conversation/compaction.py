"""Conversation compaction — summarize old turns when history exceeds budget.

契约 03: 对话历史与压缩
- 70% 阈值触发压缩（产品策略，非模型能力上限；可用 HIVEWEAVE_COMPACTION_TRIGGER_RATIO 覆盖）
- LLM 生成结构化摘要（Goal/Constraints/Progress/Decisions/Next Steps/Critical Context/Relevant Files）
- 摘要存入独立 compacted_prefix_cache，不混入 history（RECONCILE A1 修正）
- LLM 失败时回退到硬截断
- 与 streamer 硬裁（CONTEXT_TRIM_TRIGGER_RATIO≈95%）正交：压缩早、硬裁晚
"""

from typing import Awaitable, Callable

import os
import structlog

from hiveweave.conversation.token_utils import (
    COMPACTION_BUFFER,
    PRESERVE_RECENT_MAX,
    PRESERVE_RECENT_MIN,
    TOOL_OUTPUT_MAX_CHARS,
    estimate_tokens,
    estimate_tokens_for_messages,
    resolve_effective_context_window,
)

logger = structlog.get_logger()

# ── 常量 ────────────────────────────────────────────────────
# 压缩触发比例（产品策略，非模型能力上限）。可用环境变量
# HIVEWEAVE_COMPACTION_TRIGGER_RATIO 覆盖（如 0.70 = 70%）。
# 现代大窗口模型（512K）下 50% 过早触发压缩会频繁丢弃历史；调高到
# 70% 让 agent 在更大占用下才压缩，减少不必要的历史浓缩。
COMPACTION_TRIGGER_RATIO = float(
    os.environ.get("HIVEWEAVE_COMPACTION_TRIGGER_RATIO", "0.70")
)


def _compaction_ratio() -> float:
    """Clamp the configured ratio into (0, 1) so a bad env value cannot
    trigger-everything (>=1) or never-compact (<=0)."""
    r = COMPACTION_TRIGGER_RATIO
    if r <= 0:
        return 0.50
    if r >= 1:
        return 0.99
    return r
SUMMARY_TEMPERATURE = 0.3
# 摘要预算回退值：模型行没有 max_output_tokens（旧数据/未探测）时使用。
# 对齐 opencode：压缩调用不设小 max_tokens，直接用模型配置的输出上限
# （TEST18 巡检 P0：曾用 2000 首试，reasoning 模型把预算花在思考链上
# 导致 content 空，成功率仅 7/24）。首试 = 模型上限；命中
# finish_reason=length + content 空时用相同预算幂等重试一次兜偶发失败。
SUMMARY_MAX_TOKENS_ESCALATED = 8000

# 摘要消息特殊标记 — store 据此识别并提取到 compacted_prefix_cache
# R10: 已验证与 Elixir streamer.ex:2260 完全一致 ——
#   Elixir: "[Earlier conversation summary]\n#{summary}"
#   Python: SUMMARY_MARKER + "\n\n" + summary
# marker 字符串本身逐字符相同，store 用 startswith(SUMMARY_MARKER) 识别。
SUMMARY_MARKER = "[Earlier conversation summary]"

# LLM 回调类型：(prompt: str) -> summary_text | None
LLMCallback = Callable[[str], Awaitable[str | None]]


class Compaction:
    """对话历史压缩逻辑。"""

    def check_overflow(self, total_tokens: int, context_window: int) -> int | None:
        """检查是否需要压缩，返回目标 budget 或 None。

        当 total_tokens > (effective_window - COMPACTION_BUFFER) * ratio 时触发，
        ratio 默认 0.70（可用 HIVEWEAVE_COMPACTION_TRIGGER_RATIO 覆盖）。
        effective_window = min(声明 context_window, HIVEWEAVE_EFFECTIVE_CONTEXT_WINDOW)
        —— 1M 之类的声明值不得把压缩线抬到永不触发（TEST_DSH_33）。
        """
        if context_window <= 0:
            return None
        budget = resolve_effective_context_window(context_window) - COMPACTION_BUFFER
        if budget <= 0:
            return None
        if total_tokens > budget * _compaction_ratio():
            return budget
        return None

    def should_compact(self, total_tokens: int, context_window: int) -> bool:
        """判断是否达到压缩阈值（默认 70%）。"""
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

        # 边界对齐：避免拆散 assistant(tool_calls) / tool(result) 对。
        # 如果分割点恰好落在 tool_calls 之后（下一条是 tool result），
        # 将 tool result 也纳入 old_messages（向前扩展 split_idx），
        # 或将 tool_calls 也纳入 recent_messages（向后收缩 split_idx）。
        # 选择后者（收缩）—— 保留完整的 tool 对在 recent 中更安全。
        if split_idx > 0 and split_idx < len(messages):
            prev = messages[split_idx - 1]
            curr = messages[split_idx]
            # case 1: prev 是 assistant(tool_calls), curr 是 tool(result) → 收缩
            if (
                "tool_calls" in prev
                and curr.get("role") == "tool"
            ):
                split_idx -= 1  # 把 assistant(tool_calls) 也纳入 recent
            # case 2: prev 是 tool(result), curr 是 assistant(tool_calls) → 不需调整（两段各自完整）
            # case 3: prev 的 tool_calls 对应的 result 在更前面 → 向前找到 result 纳入 old
            elif curr.get("role") == "tool" and "tool_calls" not in prev:
                # curr 是孤立 tool result — 检查它的 tool_call_id 是否在 old_messages 的末尾
                tc_id = curr.get("tool_call_id")
                if tc_id:
                    for i in range(split_idx - 1, max(split_idx - 5, -1), -1):
                        if i < 0:
                            break
                        tcs = messages[i].get("tool_calls") or []
                        if any(tc.get("id") == tc_id for tc in tcs):
                            split_idx = i  # 把 assistant(tool_calls) 及其 result 都纳入 recent
                            break

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
            # R9 fix: 格式对齐 Elixir streamer.ex:2260
            #   "[Earlier conversation summary]\n#{summary}"
            # store 通过 SUMMARY_MARKER 子串匹配提取，格式一致确保跨后端兼容。
            "content": f"{SUMMARY_MARKER}\n{summary}",
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


async def resolve_compactor_callback(
    agent_id: str, kind: str = "conversation"
) -> LLMCallback | None:
    """解析 agent 的 compactor LLM 回调。

    优先使用专用压缩模型（HIVEWEAVE_COMPACTOR_MODEL_ID，llm_models 表 id）；
    未配置或该模型不可用时回退到 agent 自己的模型（或首个活跃模型）。
    专用模型解耦 agent 主模型故障与压缩故障（TEST18 巡检 P0④：原实现
    compactor 硬绑 agent 同款模型，错误完全相关）。
    无可用模型时返回 None（compact 将回退到硬截断）。

    kind: 压缩类型（conversation | memory），用于 token metering 归类。
    """
    from hiveweave.config import settings
    from hiveweave.db import meta as meta_db
    from hiveweave.db import project as project_db

    async def _pick_model() -> tuple[dict | None, str | None]:
        # 1. 专用压缩模型（配置优先；须 active 且带 key）
        if settings.compactor_model_id:
            model = await meta_db.query_one(
                "SELECT model_id, base_url, api_key, max_output_tokens, "
                "provider_type, supports_thinking, thinking_format, "
                "default_reasoning_effort, context_window "
                "FROM llm_models "
                "WHERE id = ? AND is_active = 1 LIMIT 1",
                [settings.compactor_model_id],
            )
            if model and model["base_url"] and model["api_key"]:
                return dict(model), "dedicated"
        # 2. agent 的 model_id
        agent_row = await project_db.query_one(
            agent_id,
            "SELECT model_id FROM agents WHERE id = ? LIMIT 1", [agent_id]
        )
        model_id = agent_row["model_id"] if agent_row else None
        if model_id:
            model = await meta_db.query_one(
                "SELECT model_id, base_url, api_key, max_output_tokens, "
                "provider_type, supports_thinking, thinking_format, "
                "default_reasoning_effort, context_window "
                "FROM llm_models WHERE id = ? LIMIT 1",
                [model_id],
            )
            if model and model["base_url"] and model["api_key"]:
                return dict(model), "agent"
        # 3. 首个活跃模型
        model = await meta_db.query_one(
            "SELECT model_id, base_url, api_key, max_output_tokens, "
            "provider_type, supports_thinking, thinking_format, "
            "default_reasoning_effort, context_window "
            "FROM llm_models "
            "WHERE is_active = 1 ORDER BY created_at ASC LIMIT 1",
            [],
        )
        if model and model["base_url"] and model["api_key"]:
            return dict(model), "active"
        return None, None

    try:
        model, source = await _pick_model()
        if model is None:
            logger.warning(
                "compactor_model_unavailable",
                agent_id=agent_id,
                hint="no usable model (base_url/api_key empty, dedicated id invalid, "
                "or none active) - compaction will hard-trim",
            )
            return None

        model_name = str(model["model_id"])
        # 对齐 opencode：max_tokens 用模型配置的输出上限（无该列/为 0 时
        # 由 _call_compactor_llm 回退 SUMMARY_MAX_TOKENS_ESCALATED）
        max_output = int(model.get("max_output_tokens") or 0)
        logger.info(
            "compactor_model_resolved",
            agent_id=agent_id,
            source=source,
            model=model_name,
            max_output_tokens=max_output or SUMMARY_MAX_TOKENS_ESCALATED,
        )

        async def callback(prompt: str) -> str | None:
            return await _call_compactor_llm(
                model, prompt,
                max_tokens=max_output or None,
                agent_id=agent_id,
                kind=kind,
            )

        return callback
    except Exception as e:
        logger.warning("resolve_compactor_failed", agent_id=agent_id, error=str(e))
        return None


async def _call_compactor_llm(
    model: dict,
    prompt: str,
    max_tokens: int | None = None,
    agent_id: str | None = None,
    kind: str = "conversation",
) -> str | None:
    """Call the configured provider (Chat / Responses / Anthropic / Gemini).

    预算 = 模型配置的输出上限（resolve_compactor_callback 传入）；
    未传时回退 SUMMARY_MAX_TOKENS_ESCALATED。命中截断且 content 空时
    用相同预算幂等重试一次。

    agent_id 非空时做 token metering（F3：压缩绕过 Streamer，需单独打点）。
    """
    import httpx

    from hiveweave.llm.provider import provider_factory
    from hiveweave.llm.util import normalize_usage
    from hiveweave.llm.wire_endpoint import extract_nonstream_text
    from hiveweave.services.token_meter import token_meter

    model_name = str(model.get("model_id") or "")
    try:
        config = provider_factory.create(model)
    except ValueError as e:
        logger.warning("compactor_provider_invalid", error=str(e), model=model_name)
        return None
    provider = config.api_format.value
    url = config.build_url()
    headers = config.build_headers()
    budget = max_tokens if max_tokens is not None else SUMMARY_MAX_TOKENS_ESCALATED

    for attempt in (1, 2):
        body = config.build_body(
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            temperature=SUMMARY_TEMPERATURE,
            max_tokens=budget,
            tools=None,
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=body, headers=headers)
        except Exception as e:
            logger.warning("compactor_llm_call_failed", error=str(e))
            return None
        if resp.status_code != 200:
            logger.warning("compactor_llm_http_error", status=resp.status_code)
            return None
        try:
            data = resp.json()
        except Exception as e:
            logger.warning("compactor_llm_bad_json", error=str(e))
            return None
        content = extract_nonstream_text(data)
        usage = data.get("usage")
        if not usage and isinstance(data.get("response"), dict):
            usage = data["response"].get("usage")
        if agent_id and usage:
            try:
                details = (
                    usage.get("prompt_tokens_details")
                    or usage.get("input_tokens_details")
                )
                norm = normalize_usage(
                    {
                        "input": usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
                        "output": usage.get("completion_tokens") or usage.get("output_tokens") or 0,
                        "prompt_tokens_details": details,
                        "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
                        "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
                    },
                    provider,
                )
                if norm:
                    await token_meter.record_compaction(
                        agent_id=agent_id,
                        model_id=model_name,
                        input_tokens=norm["input"],
                        output_tokens=norm["output"],
                        cache_read_tokens=norm["cache_read"],
                        cache_creation_tokens=norm["cache_creation"],
                        kind=kind,
                        provider=provider,
                    )
            except Exception as meter_err:
                logger.warning("compactor_token_meter_failed",
                               agent_id=agent_id, error=str(meter_err))
        if content:
            return content
        truncated = _compaction_looks_truncated(data)
        if truncated and attempt == 1:
            logger.info(
                "compactor_llm_retry",
                model=model_name,
                max_tokens=budget,
                reason="truncated with empty content",
            )
            continue
        return None
    return None


def _compaction_looks_truncated(data: dict) -> bool:
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return choices[0].get("finish_reason") == "length"
    status = data.get("status")
    if not status and isinstance(data.get("response"), dict):
        status = data["response"].get("status")
    if status == "incomplete":
        return True
    stop = data.get("stop_reason")
    return stop == "max_tokens"


def provider_type_from_model(model: dict) -> str | None:
    """从模型行推断 provider_type（best-effort，token metering 用）。"""
    try:
        return str(model.get("provider_type") or "") or None
    except Exception:
        return None
