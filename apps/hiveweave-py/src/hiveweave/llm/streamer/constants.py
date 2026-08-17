"""Streamer constants and LLM concurrency semaphore."""
from __future__ import annotations

import asyncio
import os as _os


def session_wall_clock_enabled() -> bool:
    """Opt-in session TOTAL/HARD wrap. Default off (no turn budget).

    Product: long work may run a long time. Stop on stream-idle, per-tool
    declared timeout, job_kill / cancel — not 540/570/600. Tests that still
    exercise the old gates set ``HIVEWEAVE_STREAM_SESSION_WALL_CLOCK=1``.
    """
    raw = (
        _os.environ.get("HIVEWEAVE_STREAM_SESSION_WALL_CLOCK", "0") or "0"
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")

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

# DSH compaction-basic: pressure at 0.8 × window, retain tail 0.16 × window.
# HiveWeave scales against *usable input* (window − thinking − safety buffer)
# because 0.8 × 1M would 400 before we compact.
WORKING_SET_PRESSURE_RATIO = 0.8
"""循环内工作集压力线：input ≥ usable × 此值才改写前缀（prune / 摘要）。"""

WORKING_SET_RETAIN_RATIO = 0.16
"""压力压缩后从尾按 token 保留的原文比例（滞回，避免每步都压）。"""

WORKING_SET_SUMMARY_MAX_TOKENS = 8192
"""步边界摘要输出帽（对齐 DSH compaction-basic maxTokens）。"""

WORKING_SET_CHECKPOINT_MARKER = "[Working-set checkpoint]"
"""循环内摘要节点标记。不要用跨回合 SUMMARY_MARKER，以免写入 compacted_prefix。"""

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

# ── 硬截止结构性执行（2026-08-07 slack-clone 压测根因修复）────────
# 根因：HARD_TOTAL_TIMEOUT_S 此前只在 tool_loop 轮次【之间】检查，而单轮
# 时长无界 —— 一次 LLM 流式（有事件就续命，思考模型可流数分钟）+ 一批并行
# 工具（bash 120s / question 200s；spawn_subagent 已改为 off-turn 立即返回）。在 t≈560s 进入一轮可冲到
# 700s+，越过 agent SAFETY_TIMEOUT(600s) 被整体 cancel（run interrupted →
# 90s 冷却 → resume 重建上下文）。实测 Aria 两次 run 在恰好 600s 被杀，
# 且所有长 turn 都贴着 570~600s 边缘运行。HARD↔SAFETY 仅 30s 余量，远小于
# 单轮最坏时长 —— 只靠轮间检查，「HARD < SAFETY」是统计成立而非结构成立。
# 以下三个闸口把硬截止变成结构保证：streamer 承诺在 HARD 之前返回。
MIN_ROUND_BUDGET_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_MIN_ROUND_BUDGET_S", "120") or "120"
)
"""开启新一轮 LLM 请求所需的最低剩余硬预算（秒）。

剩余预算低于此值不再开新轮，直接走预算耗尽优雅收口（产出保留）。
依据：思考模型首 chunk 可达 FIRST_CHUNK_TIMEOUT_S(90s)，剩余预算买不起
一轮有意义产出时开新轮只会被中途切断，白烧 token。"""

TURN_STREAM_CUT_GRACE_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_CUT_GRACE_S", "20") or "20"
)
"""流式中途预算切断提前量（秒）：读到 hard_deadline − 本值即收口。

保留已累积文本、丢弃不完整 tool_calls（同 finish_reason=length 语义），
留出 merge/记账/返回时间。必须 < HARD↔SAFETY 的 30s 余量。"""

MIN_TOOL_BUDGET_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_MIN_TOOL_BUDGET_S", "15") or "15"
)
"""执行工具批所需的最低剩余预算（秒）。低于此值跳过执行直接优雅收口 —
即便执行也会被预算帽压到近乎立即超时，产出的错误结果只会再烧一轮 LLM。
被丢弃的本批 tool_calls 由下次唤醒重新发起，无副作用。"""

TOOL_BUDGET_GRACE_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_TOOL_GRACE_S", "15") or "15"
)
"""工具批执行后的记账/返回预留（秒）。工具实际超时帽 = 剩余硬预算 − 本值。"""

SUMMARY_MIN_BUDGET_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_SUMMARY_MIN_BUDGET_S", "45") or "45"
)
"""强制收尾总结调用（stall/no_text/max_rounds 的非流式总结）所需的最低
剩余预算（秒）。低于此值跳过总结 LLM 调用直接走 fallback 文案 —— 总结
调用 read 超时 95s，无帽可在 t≈560s 触发时冲过 HARD 直至 SAFETY 强杀，
把三道闸口建立的结构保证打穿（2026-08-08 多子代审计发现）。"""

# ── 结构约束断言：env 误配直接 import 失败，不许静默破坏 ──────────
# 「HARD < SAFETY」「切断提前量 < HARD↔SAFETY 余量」是本文件的结构性承诺；
# 只靠注释约束会被 env 覆盖静默击穿（审计 2026-08-08）。
# 注意：余量不是常量 30s，而是 600 − HARD_TOTAL_TIMEOUT_S（HARD 本身 env
# 可调）——断言必须引用实际余量，否则 HARD=590 时 grace=20 落在 SAFETY
# 之后，assert 全绿但结构保证已击穿（审计 2026-08-08 P2）。
assert HARD_TOTAL_TIMEOUT_S < 600.0, (
    "HARD_TOTAL_TIMEOUT_S 必须 < agent SAFETY_TIMEOUT(600s)，"
    "否则轮间硬检查失去意义"
)
assert 0 < TURN_STREAM_CUT_GRACE_S < 600.0 - HARD_TOTAL_TIMEOUT_S, (
    "TURN_STREAM_CUT_GRACE_S 必须为正且 < HARD↔SAFETY 的实际余量 "
    "(600 - HARD_TOTAL_TIMEOUT_S)"
)
assert 0 < MIN_ROUND_BUDGET_S < HARD_TOTAL_TIMEOUT_S, (
    "MIN_ROUND_BUDGET_S 必须在 (0, HARD) 内 —— ≥HARD 轮闸门恒关、"
    "<=0 闸门失效"
)
assert MIN_TOOL_BUDGET_S > 0 and TOOL_BUDGET_GRACE_S > 0
# context.py wait_s = max(5.0, remain − 5.0) 要求总结闸口 ≥10s，
# 否则 floor 5s 与「截止前留 5s 记账」无法同时成立（审计 2026-08-08 P2）。
assert SUMMARY_MIN_BUDGET_S >= 10.0, (
    "SUMMARY_MIN_BUDGET_S 必须 >= 10s（总结等待钳制的双 5s 预留）"
)

# ── 疏通层：预算可见性（2026-08-08）────────────────────────────────
# 三道闸口是堵截兜底；疏通层的职责是让 agent 在撞闸【之前】就能自我
# pacing。此前 agent 对 turn 预算完全盲视，直到软截止（~95% 处）才收到
# 唯一一次提示 —— 长 turn 里启动全量测试/大重构等重型操作纯属不知情。
# 剩余硬预算首次低于本阈值时注入一次温和提示，把「规划收口」的决策权
# 提前交还给 agent（疏通），闸口只在它不收口时才动手（堵截兜底）。
BUDGET_PACING_HINT_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_PACING_HINT_S", "300") or "300"
)
"""预算 pacing 提示的剩余阈值（秒）。默认 300 ≈ 硬预算一半，一次性注入。"""

assert 0 < BUDGET_PACING_HINT_S < HARD_TOTAL_TIMEOUT_S, (
    "BUDGET_PACING_HINT_S 必须在 (0, HARD) 内 —— >=HARD 每个 turn 第 2 轮"
    "起就注入提示，纯白烧 token"
)

FIRST_CHUNK_TIMEOUT_S = 90.0
"""首 chunk 超时（TS 防线②，thinking 模型首 token 可能 60-90s）。"""

IDLE_TIMEOUT_S = float(
    _os.environ.get("HIVEWEAVE_STREAM_IDLE_TIMEOUT_S", "300") or "300"
)
"""后续 chunk 空闲看门狗（默认 5min）。真停滞才杀，不是整轮写码到点必杀。"""

# Socket read must outlive the idle watchdog so httpx does not kill first.
STREAM_SOCKET_READ_TIMEOUT_S = IDLE_TIMEOUT_S + 30.0

LLM_QUEUE_PING_S = 15.0
"""While waiting for the global LLM semaphore, fire ``llm_queue`` this often
so zombie sweep does not treat a healthy waiter as a silent stream.
Must stay well below ``STREAMING_ZOMBIE_TIMEOUT_MS`` (default 5 min).
"""


def stream_chunk_wait_s(*, got_event: bool) -> float:
    """Seconds to wait for the next SSE event (first chunk vs idle)."""
    return FIRST_CHUNK_TIMEOUT_S if not got_event else IDLE_TIMEOUT_S

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
"""Legacy default. Streamer no longer wraps undeclared tools in this wait_for.
Declared budgets live in ``hiveweave.tools.timeout_policy.DECLARED_TIMEOUT_S``.
"""

# question waits on human answer (QUESTION_TIMEOUT_S=180) — must outlive that.
_QUESTION_TOOL_TIMEOUT_S = 200.0

# spawn_subagent returns immediately (off-turn job).
_SUBAGENT_TOOL_TIMEOUT_S = TOOL_EXECUTION_TIMEOUT_S

