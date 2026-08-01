"""Streamer constants and LLM concurrency semaphore."""
from __future__ import annotations

import asyncio
import os as _os

MAX_TOOL_ROUNDS = 1_000_000
"""最大 tool loop 轮次 — 仅作极端安全网，实际由 doom loop 按工具分级保护。峰值复现真实死循环(同参数反复调用) 。"""

# DESIGN-2 / Magentic-One Progress Ledger: consecutive no-progress rounds
# force an outer-loop exit (commit / replan) instead of burning tokens.
TOOL_LOOP_STALL_LIMIT = 2
# Pure-readonly polling gets a higher runway (dogfood 2026-07-24: 61 stalls
# were mostly get_tasks/list_files). Failed/empty rounds still use limit=2.
TOOL_LOOP_READONLY_STALL_LIMIT = 8

MAX_TOOLS_PER_ROUND = 5
"""单轮工具调用数上限。对齐 Elixir streamer.ex:488。"""

EMPTY_RESPONSE_MAX_RETRIES = 3
"""空响应重试上限。契约 01: 最多 3 次。"""

EMPTY_RESPONSE_BACKOFF_MS = [5_000, 15_000, 45_000]
"""空响应退避序列（5s/15s/45s）。契约 01。"""

NO_TEXT_ROUNDS_THRESHOLD = 3
"""连续无文字轮次阈值: 3 轮后注入系统提示。"""

NO_TEXT_HINT_MAX = 5
"""无文字提示注入上限: 超过 5 次后强制结束 tool loop 走总结。

设计意图 (project_memory):
- 连续 3 轮只调工具不说话 → 第 1 次注入提示，计数重置
- 重复 5 次（共 ~18 轮）→ 第 5 次注入触发 break，强制走 _make_max_rounds_summary
- 给 executor 更多空间完成多文件写入（如初始化项目骨架需 10+ write_file）
- 仍可避免卡死的 agent 空转到 60/80 硬上限
"""

DEFAULT_PLACEHOLDER = "好的，开始处理。\n"
"""默认占位文本（UI 提示，不计为真实输出）。"""

MID_ROUND_REMINDER_RATIO = 0.8
"""中轮提醒注入时机: 80% 轮次时。"""

SAFETY_BUFFER_TOKENS = 20_000
"""上下文溢出检查的安全缓冲。

覆盖未计量开销：工具定义 JSON Schema（15-25K tokens）、system prompt 框架文本等。
旧值 4K 远不够，导致 token 估算认为还有空间但实际 API 已超限。
"""

# Hard trim is an API safety net near the true usable input ceiling.
# Soft compaction (COMPACTION_TRIGGER_RATIO=0.50) is a separate product policy
# that summarizes old turns earlier; do NOT conflate the two.
# Leave a small headroom so one large tool result does not immediately 400.
CONTEXT_TRIM_TRIGGER_RATIO = 0.95
"""输入预算占用达到该比例时才硬截断历史（保留 system 头 + 最近 turn）。"""

OUTPUT_TOKEN_GLOBAL_CAP = 32_000
"""非 reasoning 模型的 max_tokens 全局上限。"""

CONTINUE_SENTINEL = (
    "[HiveWeave runtime] Re-invoking this turn: the request did not end with a "
    "user message (usually after tool/assistant output). The platform treats "
    "your work as still open, so you are woken again to continue — finish "
    "outstanding steps or commit_turn. This is not a new human instruction."
)
"""Appended as a trailing user message on the HTTP request copy only.

Dual purpose:
1. Gateway FIX(gateway-tool-id-400): trailing non-user history can 400; a
   static user tail skips that check (see ``_stream_single_round``).
2. Model clarity: explain *why* this invocation exists so the agent does not
   invent a human ``(continue)`` / user wake. Content must stay constant.
"""

TOTAL_TIMEOUT_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_TOTAL_TIMEOUT_S", "540") or "540"
)
"""软预算切片（秒）。有工具/流式活动时续命，见 ``HARD_TOTAL_TIMEOUT_S``。

可通过 env ``HIVEWEAVE_STREAM_TOTAL_TIMEOUT_S`` 覆盖（默认 540）。

TEST21 M4: 不再用这一值对整段 tool loop 做硬杀；活跃长 turn 在硬上限内
续命，耗尽时优雅收口（保留 tool 产出）而非裸 ValueError。
"""

HARD_TOTAL_TIMEOUT_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_HARD_TIMEOUT_S", "570") or "570"
)
"""Turn 绝对硬上限（秒），须 < agent SAFETY_TIMEOUT（600s）。"""

ACTIVITY_EXTEND_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_ACTIVITY_EXTEND_S", "180") or "180"
)
"""每次有进展的工具轮后，软预算续命这么多秒（不超过硬上限）。"""

FIRST_CHUNK_TIMEOUT_S = 90.0
"""首 chunk 超时（TS 防线②，thinking 模型首 token 可能 60-90s）。"""

IDLE_TIMEOUT_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_IDLE_TIMEOUT_S", "120") or "120"
)
"""后续 chunk / 无活动 idle 超时（TEST21 M4：默认 120s，真停滞才杀）。"""

# ── Bug B fix: 全局 LLM 并发控制 ───────────────────────────
# 防止多 agent 同时打 LLM API 超过 provider 并发限制（默认 8）。
# Semaphore 在 HTTP 请求级别获取/释放，tool 执行期间不占槽。
_LLM_MAX_CONCURRENT = int(_os.environ.get("HIVEWEAVE_LLM_MAX_CONCURRENT", "8"))
_LLM_SEMAPHORE: asyncio.Semaphore | None = None


def _get_llm_semaphore() -> asyncio.Semaphore:
    """Lazy-init global LLM semaphore (must be created inside event loop)."""
    global _LLM_SEMAPHORE
    if _LLM_SEMAPHORE is None:
        _LLM_SEMAPHORE = asyncio.Semaphore(_LLM_MAX_CONCURRENT)
    return _LLM_SEMAPHORE

TOOL_EXECUTION_TIMEOUT_S = 120.0
"""单个工具执行超时。对齐 Elixir Task.yield(task, 120_000)。"""

# question waits on human answer (QUESTION_TIMEOUT_S=180) — must outlive that.
_QUESTION_TOOL_TIMEOUT_S = 200.0

# spawn_subagent 外层工具超时必须 > 子代理最大硬限(480s) + 余量
_SUBAGENT_TOOL_TIMEOUT_S = 500

