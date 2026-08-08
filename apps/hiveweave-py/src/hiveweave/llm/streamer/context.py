"""Context prune / trim / mid-round reminder / max-rounds summary."""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from hiveweave.conversation.token_utils import estimate_tokens_for_messages
from hiveweave.llm.provider import ProviderConfig

from .constants import (
    CONTEXT_TRIM_TRIGGER_RATIO,
    MID_ROUND_REMINDER_RATIO,
    OUTPUT_TOKEN_GLOBAL_CAP,
    SAFETY_BUFFER_TOKENS,
    SUMMARY_MIN_BUDGET_S,
)
from .types import DeltaCallback

log = structlog.get_logger(__name__)


class ContextMixin:
    """Context management methods for Streamer."""

    if TYPE_CHECKING:
        max_tool_rounds: Any
        _fire_delta: Any

    #: Prune 保护窗口（token）— 最近工具输出保留原文
    _PRUNE_PROTECT_TOKENS = 40_000
    #: Prune 最低收益（token）— 候选总量不足此值则不执行
    _PRUNE_MINIMUM_TOKENS = 10_000
    #: Prune 占位符
    _PRUNE_PLACEHOLDER = "[Old tool result content cleared]"

    def _prune_old_tool_outputs(self, messages: list[dict]) -> list[dict]:
        """在 tool loop 中裁剪旧工具输出（OpenCode prune 模式，临时版）。

        逆序遍历：跳过最近 2 轮（assistant 消息计轮次），保护窗口(40K)外的
        旧 tool 输出替换为占位符。候选总量 > 10K 时才执行。
        """
        if len(messages) < 6:
            return messages

        to_prune_indices: list[int] = []
        protected = 0
        turns = 0

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            role = msg.get("role", "")

            if role == "assistant":
                turns += 1
            if turns < 2:
                continue

            if "tool_call_id" not in msg:
                continue

            # 已被裁剪过 → 停止
            if msg.get("content") == self._PRUNE_PLACEHOLDER:
                break

            tokens = estimate_tokens_for_messages([msg])
            new_protected = protected + tokens
            if new_protected <= self._PRUNE_PROTECT_TOKENS:
                protected = new_protected
            else:
                to_prune_indices.append(i)

        if not to_prune_indices:
            return messages

        prune_tokens = sum(
            estimate_tokens_for_messages([messages[i]]) for i in to_prune_indices
        )
        if prune_tokens < self._PRUNE_MINIMUM_TOKENS:
            return messages  # 收益不足

        result = list(messages)
        for i in to_prune_indices:
            pruned = {
                **result[i],
                "content": self._PRUNE_PLACEHOLDER,
            }
            # Drop multimodal payloads with pruned text — pixels are useless
            # once the observation text is gone, and they blow the context.
            pruned.pop("images", None)
            result[i] = pruned

        log.info(
            "tool_loop_prune",
            pruned_count=len(to_prune_indices),
            pruned_tokens=prune_tokens,
            protected_tokens=protected,
        )
        return result

    def _trim_context_if_needed(
        self,
        messages: list[dict],
        provider: ProviderConfig,
    ) -> list[dict]:
        """上下文溢出检查: 先 prune 旧工具输出，再估算 token，超 usable 则硬截断。

        对齐 Elixir trim_context_if_needed + OpenCode prune 模式。
        """
        # Step 1: Prune 旧工具输出（替换为占位符，不丢弃消息）
        messages = self._prune_old_tool_outputs(messages)
        # Step 1b: Keep only the newest screenshot payloads (base64 is huge)
        from hiveweave.services.vision import strip_images_from_messages

        messages = strip_images_from_messages(messages)

        max_output = provider.max_output_tokens
        if provider.supports_thinking:
            max_output = max(max_output, OUTPUT_TOKEN_GLOBAL_CAP)
        else:
            max_output = min(max_output, OUTPUT_TOKEN_GLOBAL_CAP)

        # 治本：不再用 max(负数, 8192) 掩盖非法配置。
        # 若 context_window - max_output - buffer <= 0，说明配置非法
        # （输出预算吃掉整个窗口），ProviderConfig 构造时本应已拦住。
        # 此处若仍触发 = DB 有脏数据绕过了构造校验，硬失败暴露问题，
        # 绝不静默 floor 到 8192 后带病发请求（那会导致 400 且原因难定位）。
        input_budget = provider.context_window - max_output - SAFETY_BUFFER_TOKENS
        if input_budget <= 0:
            raise ValueError(
                f"非法模型配置：context_window={provider.context_window:,} - "
                f"max_output={max_output:,} - safety_buffer={SAFETY_BUFFER_TOKENS:,} "
                f"= {input_budget}（输入预算 <= 0）。输出预算吃掉整个窗口，"
                f"请修复模型配置的 max_output_tokens。"
            )
        # 合法小窗口模型的兜底：input_budget > 0 但小于 8192 时，
        # 保证输入至少有 8192 可用（此时 max_output 会被 cap 到不超限）。
        usable = max(input_budget, 8_192)
        # Hard trim near usable ceiling (95%); soft compaction is separate (50%).
        trim_at = max(int(usable * CONTEXT_TRIM_TRIGGER_RATIO), 8_192)
        total = estimate_tokens_for_messages(messages)

        if total <= trim_at:
            # Still scrub broken pairs (e.g. short lists that never enter hard trim).
            return self._drop_orphan_tool_artifacts(messages)

        log.info(
            "context_overflow_trim",
            total=total,
            usable=usable,
            trim_at=trim_at,
            ratio=CONTEXT_TRIM_TRIGGER_RATIO,
        )

        # 只钉住开头连续的 system（identity / compacted_prefix）。
        # TEST18 根因：旧逻辑 head=messages[:2] 在无 compacted 时把历史首条
        # assistant(tool_calls) 钉进 head，随后从 tail 裁掉其 tool 回执 →
        # 网关 400「tool_calls must be followed by tool messages」。
        if len(messages) <= 4:
            return self._drop_orphan_tool_artifacts(messages)

        head_end = self._leading_system_count(messages)
        # 无 leading system 时仍要能裁；head 可为空。
        head = messages[:head_end]
        tail = messages[head_end:]

        # 从 tail 前端逐步裁剪直到 token 数回到阈值以下
        while len(tail) > 2 and estimate_tokens_for_messages(head + tail) > trim_at:
            # R3: 保持 tool_calls + tool_result 对的完整性，避免产生孤儿 tool_result
            # （没有对应 tool_calls 的 tool_result 会导致 API 400 错误）。
            # 原实现只检查相邻 2 条，多 tool_result 批次会留下孤儿。
            first = tail[0]
            drop = 1
            tcs = first.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                # assistant(tool_calls) — 连同其后所有同批 tool_result 一起裁剪
                drop = 1
                while drop < len(tail) and (
                    "tool_call_id" in tail[drop] or tail[drop].get("role") == "tool"
                ):
                    drop += 1
            elif first.get("tool_call_id") or first.get("role") == "tool":
                # 孤儿 tool_result（其 tool_calls 已被裁剪）— 裁剪它及后续同批 tool_result
                drop = 0
                while drop < len(tail) and (
                    "tool_call_id" in tail[drop] or tail[drop].get("role") == "tool"
                ):
                    drop += 1
            if drop <= 0:
                drop = 1
            tail = tail[drop:]

        trimmed = self._drop_orphan_tool_artifacts(head + tail)
        log.info("context_trimmed",
                 original=len(messages), trimmed=len(trimmed),
                 tokens=estimate_tokens_for_messages(trimmed))
        return trimmed

    @staticmethod
    def _leading_system_count(messages: list[dict]) -> int:
        """Count consecutive role=system messages at the front."""
        n = 0
        while n < len(messages) and messages[n].get("role") == "system":
            n += 1
        return n

    @staticmethod
    def _drop_orphan_tool_artifacts(messages: list[dict]) -> list[dict]:
        """Drop broken tool pairs left after hard trim.

        Keeps assistant(tool_calls) only when every tool_call_id has a following
        tool result in the immediate tool batch; drops orphan tool results.
        """
        out: list[dict] = []
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            tcs = m.get("tool_calls")
            if m.get("role") == "assistant" and isinstance(tcs, list) and tcs:
                needed = {
                    tc.get("id")
                    for tc in tcs
                    if isinstance(tc, dict) and tc.get("id")
                }
                j = i + 1
                found: set[str] = set()
                while j < n and (
                    messages[j].get("role") == "tool"
                    or messages[j].get("tool_call_id")
                ):
                    tid = messages[j].get("tool_call_id")
                    if tid:
                        found.add(tid)
                    j += 1
                if needed and needed <= found:
                    out.extend(messages[i:j])
                # else: skip broken assistant + any partial tools
                i = j
                continue
            if m.get("role") == "tool" or m.get("tool_call_id"):
                # Orphan tool result (no kept assistant ahead)
                i += 1
                continue
            out.append(m)
            i += 1
        return out

    # ── 中轮提醒 ────────────────────────────────────────────

    def _maybe_inject_mid_round_reminder(
        self,
        messages: list[dict],
        round_num: int,
        rounds_cap: int | None = None,
    ) -> list[dict]:
        """80% 轮次时注入「开始收尾」系统提示。"""
        cap = rounds_cap if rounds_cap else self.max_tool_rounds
        reminder_round = max(int(cap * MID_ROUND_REMINDER_RATIO), 1)
        if round_num == reminder_round and round_num < cap:
            rounds_left = cap - round_num
            log.info("inject_mid_round_reminder",
                     round=round_num, rounds_left=rounds_left)
            messages = messages + [{
                "role": "system",
                "content": (
                    f"⚠️ You have {rounds_left} tool calls remaining. "
                    "Start wrapping up: finish critical actions now and prepare a summary."
                ),
            }]
        return messages

    # ── 最大轮次总结 ────────────────────────────────────────

    # 总结请求失败时的如实 fallback（不冒充"达到轮数上限"）。
    # 未知 reason 兜底显示原文（与 prompt 分支的 else 风格一致）。
    _SUMMARY_FALLBACK_BY_REASON = {
        "max_rounds": "limit reached",
        "stall_break": "stalled",
        "no_text": "no text output",
    }

    def _summary_fallback(self, reason: str) -> str:
        label = self._SUMMARY_FALLBACK_BY_REASON.get(reason, reason)
        return (
            f"⚠️ Turn ended early: tool-loop {label}. "
            "Some tasks may be incomplete."
        )

    async def _make_max_rounds_summary(
        self,
        agent_id: str,
        provider: ProviderConfig,
        messages: list[dict],
        on_delta: DeltaCallback | None,
        reason: str = "max_rounds",
        budget_deadline: float | None = None,
    ) -> str:
        """回合强制收尾的总结调用（真实原因由 ``reason`` 说明）。

        三种场景共用：max_rounds（达到工具轮数上限）/ stall_break（tool
        loop 停滞，只调只读工具无进展）/ no_text（连续只调工具不说话）。
        fallback 文案必须如实反映 ``reason`` —— 不要冒充"达到轮数上限"。

        ``budget_deadline``（monotonic 刻度）是 turn 硬预算截止：总结也是
        LLM 调用（非流式 read=95s），无帽可在 t≈560s 触发时冲过 HARD 直至
        agent SAFETY_TIMEOUT 强杀（2026-08-08 审计发现——三道预算闸口的
        结构保证会被这条收尾路径打穿）。剩余不足时跳过总结走 fallback；
        允许时等待时长也钳在预算内。
        """
        if budget_deadline is not None:
            remain_s = budget_deadline - time.monotonic()
            if remain_s < SUMMARY_MIN_BUDGET_S:
                log.warning(
                    "summary_skipped_budget",
                    agent_id=agent_id,
                    reason=reason,
                    remain_s=round(remain_s, 1),
                )
                return self._summary_fallback(reason)
        if reason == "max_rounds":
            summary_prompt = (
                "CRITICAL — MAXIMUM TOOL ROUNDS REACHED\n\n"
                "You have reached the maximum number of tool calls for this turn. "
                "Tools are now disabled.\n\n"
                "You MUST respond with a text summary. Include:\n"
                "1. What you have accomplished so far\n"
                "2. What tasks remain incomplete\n"
                "3. Recommended next steps\n\n"
                "Respond with text ONLY. Do NOT attempt any tool calls."
            )
        elif reason == "stall_break":
            summary_prompt = (
                "CRITICAL — TOOL LOOP STALLED\n\n"
                "Your last several tool calls made no progress (only readonly / "
                "failed / duplicate calls). Tools are now disabled.\n\n"
                "You MUST respond with a text summary. Include:\n"
                "1. What you have accomplished so far\n"
                "2. What is blocking progress\n"
                "3. Recommended next steps\n\n"
                "Respond with text ONLY. Do NOT attempt any tool calls."
            )
        else:  # no_text
            summary_prompt = (
                "CRITICAL — NO TEXT OUTPUT\n\n"
                "You have called tools repeatedly without producing any text. "
                "Tools are now disabled.\n\n"
                "You MUST respond with a text summary. Include:\n"
                "1. What you have accomplished so far\n"
                "2. What tasks remain incomplete\n"
                "3. Recommended next steps\n\n"
                "Respond with text ONLY. Do NOT attempt any tool calls."
            )
        summary_messages = messages + [{"role": "user", "content": summary_prompt}]

        url = provider.build_url()
        headers = provider.build_headers()
        body = provider.build_body(
            messages=summary_messages,
            stream=False,
            temperature=0.3,
            tools=None,
        )

        client = provider.build_client()
        try:
            post_coro = client.post(
                url, headers=headers,
                content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            )
            if budget_deadline is not None:
                # 等待时长钳在预算内（留 5s 记账/返回），超时走 fallback —
                # 不让收尾路径成为预算结构保证的漏洞。
                wait_s = max(5.0, budget_deadline - time.monotonic() - 5.0)
                resp = await asyncio.wait_for(post_coro, timeout=wait_s)
            else:
                resp = await post_coro
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices") or []
                if choices:
                    content = choices[0].get("message", {}).get("content")
                    if content:
                        await self._fire_delta(on_delta, {
                            "type": "text_delta",
                            "content": content,
                            "delta_id": "summary",
                        })
                        return content
            log.warning("summary_request_failed", status=resp.status_code, reason=reason)
            return self._summary_fallback(reason)
        except Exception as e:
            log.warning("summary_request_error", error=str(e), reason=reason)
            return self._summary_fallback(reason)
        finally:
            await client.aclose()

