"""Token estimation and budget calculation utilities.

契约 03: 对话历史与压缩
- char-ratio 启发式：4 chars/token EN, ~1.0 CJK
- 对齐 Elixir token_utils.ex + TS token-utils.ts
- 工具输出超限截断并保存到临时文件（OpenCode ToolOutputStore 模式）
"""

import hashlib
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import structlog

logger = structlog.get_logger()

# ── 常量（契约 03 constants）─────────────────────────────────
COMPACTION_BUFFER = 20_000
PRESERVE_RECENT_MIN = 10
PRESERVE_RECENT_MAX = 30
TAIL_TURNS = 2
PRUNE_PROTECT_TOKENS = 40_000
# 与 `ContextMixin._PRUNE_MINIMUM_TOKENS` 是**同一个对象**（2026-09-21 收口；
# 此前只是「数值恰好相等」+ 注释声称对齐）。持久化裁剪只在
# 溢出改写点回写（completion 3.5 节），阈值必须覆盖 in-loop prune 的触发带
# （10k-20k），否则 DB 回写 no-op → 下一 run 读到未裁剪原文 → 前缀 miss。
PRUNE_MINIMUM_TOKENS = 10_000

# 裁剪后写进 ``content`` 的占位符。
#
# ⚠ **单一源**（2026-09-21）：in-loop 裁剪与持久化裁剪必须用**同一个**字符串
# —— 旧的持久化实现里是硬编码字面量，且「已裁剪 → 停止」的判据也各写一份，
# 改一处就漏一处（本仓最高频复发形态：同一算法两次实现，本轮是第 5 次）。
# 取证/消费方请引用本常量（或 `context_marker` 的枚举），不要复制字面量、
# 更不要按「长度 33」这类形态判据去认它。
PRUNE_PLACEHOLDER = "[Old tool result content cleared]"
TOOL_OUTPUT_MAX_CHARS = 2_000

# 有效上下文封顶（HIVEWEAVE_EFFECTIVE_CONTEXT_WINDOW，0 = 关闭封顶）。
# TEST_DSH_33 实证：模型行声明 context_window=1M，压缩线
# (1M - COMPACTION_BUFFER) * 0.70 ≈ 686K，而实测单请求 prompt 峰值仅 409K ——
# 压缩/裁剪链路整轮不触发，compacted_prefix 恒为空。声明值是计费上限，不是
# 「能有效利用」的上限；预算按 min(声明值, 本封顶) 取。
# 默认 256_000：与主流大窗口档位（262144）同量级，且减去 COMPACTION_BUFFER
# 后压缩线落在 165K 附近（远高于 PRUNE_PROTECT_TOKENS 40K，压缩早于 in-loop
# prune 生效）；同时对 max_output=128K 的模型仍留出正输入预算。
EFFECTIVE_CONTEXT_CAP = 256_000

# 工具输出智能截断限制（镜像 OpenCode ToolOutputStore）
# 分层：工具侧先收成短契约；此处只是最后兜底（须按行+字节双封顶）
TOOL_OUTPUT_MAX_LINES = 2_000
TOOL_OUTPUT_MAX_BYTES = 51_200  # 50 KB
PREVIEW_HEAD_LINES = 20
PREVIEW_TAIL_LINES = 5
PREVIEW_TAIL_THRESHOLD = 25  # only include tail if total > 25 lines
PREVIEW_LINE_MAX_CHARS = 500  # single-line dumps must not defeat line truncation
PREVIEW_MAX_CHARS = 4_000  # total preview budget returned to the model

# CJK 检测范围（对齐 TS token-utils.ts）
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text) -> int:
    """估算字符串的 token 数。

    char-ratio 启发式：
    - 非 CJK：~4 chars/token
    - CJK：~1.0 chars/token（实测混元/Claude 约 0.8-1.2 chars/token）
    保守高估 ~10-15%，确保不超模型硬限制。
    """
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk_count = len(_CJK_RE.findall(text))
    non_cjk = len(text) - cjk_count
    return math.ceil(non_cjk / 4 + cjk_count / 1.0)


def estimate_tokens_for_messages(messages: list) -> int:
    """估算消息列表的总 token 数（含 tool_calls arguments + images）。

    Images are counted so tool-loop trim/prune cannot ignore multi-MB
    screenshot payloads (vision inject). Heuristic: ~1 token / 512 decoded
    bytes, floor 256 per image (overestimate preferred to under-trim).
    """
    if not messages:
        return 0
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content") or ""
        if isinstance(content, list):
            # 多模态 content — 拼接 text 部分 + image_url data URIs
            text_bits: list[str] = []
            for p in content:
                if not isinstance(p, dict):
                    text_bits.append(str(p))
                    continue
                if p.get("type") == "text":
                    text_bits.append(str(p.get("text") or ""))
                elif p.get("type") == "image_url":
                    url = ""
                    iu = p.get("image_url")
                    if isinstance(iu, dict):
                        url = str(iu.get("url") or "")
                    elif isinstance(iu, str):
                        url = iu
                    # data:image/...;base64,XXXX
                    if "base64," in url:
                        b64 = url.split("base64,", 1)[1]
                        nbytes = len(b64) * 3 // 4
                        total += max(256, nbytes // 512)
                    else:
                        total += 256
            content = "".join(text_bits)
        total += estimate_tokens(content)
        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            total += estimate_tokens(fn.get("arguments") or "")
        # Internal HiveWeave image payloads on tool/user messages
        for img in msg.get("images") or []:
            if not isinstance(img, dict):
                continue
            data = img.get("data") or ""
            if not data:
                continue
            nbytes = len(data) * 3 // 4
            total += max(256, nbytes // 512)
    return total


def effective_context_cap() -> int:
    """当前生效的有效上下文封顶（0 = 不封顶）。

    读 ``HIVEWEAVE_EFFECTIVE_CONTEXT_WINDOW``（每次调用读环境，便于测试与
    运行时调整）。非法/负值回退默认；显式 0 表示关闭封顶（信任声明值）。
    """
    raw = os.environ.get("HIVEWEAVE_EFFECTIVE_CONTEXT_WINDOW")
    if raw is None or not raw.strip():
        return EFFECTIVE_CONTEXT_CAP
    try:
        cap = int(raw.strip())
    except ValueError:
        logger.warning("effective_context_cap_invalid", value=raw)
        return EFFECTIVE_CONTEXT_CAP
    if cap < 0:
        logger.warning("effective_context_cap_negative", value=cap)
        return EFFECTIVE_CONTEXT_CAP
    return cap


def resolve_effective_context_window(context_window: int) -> int:
    """把模型声明的 context_window 收敛到有效上限。

    声明值 = 计费/API 上限；封顶 = 平台愿意真正填满的上限。压缩与裁剪
    的预算一律走本函数，避免 1M 之类的声明把整条压缩链路抬到永不触发
    （TEST_DSH_33：实测 prompt 峰值 409K < 686K 压缩线，压缩零触发）。

    ``context_window <= 0`` 原样返回，交由调用方的既有非法配置语义处理
    （streamer ``_input_budget`` 硬失败不得被本函数吞掉）。
    """
    if context_window <= 0:
        return context_window
    cap = effective_context_cap()
    if cap <= 0:
        return context_window
    return min(context_window, cap)


def calculate_history_budget(messages: list, context_window: int) -> int:
    """计算对话历史可用 token 预算。

    budget = effective(context_window) - COMPACTION_BUFFER
    messages 参数预留给未来按静态 prompt 扣减的扩展（当前仅减 buffer）。
    """
    if context_window <= 0:
        return 0
    return max(resolve_effective_context_window(context_window) - COMPACTION_BUFFER, 0)


def truncate_tool_output(
    output: str, max_lines: int = TOOL_OUTPUT_MAX_LINES, max_bytes: int = TOOL_OUTPUT_MAX_BYTES
) -> str:
    """智能截断工具输出。超限时保存完整内容到临时文件，返回 head+tail 预览。

    镜像 OpenCode ToolOutputStore 模式：保留头部（结构/上下文）和尾部（结果/结论），
    中间用省略标记替换，附带完整输出的临时文件路径。

    预览按行 + 按字符双封顶——单行超长 JSON/HTML 残片不能击穿行截断
    （TEST19: list_available_skills 单行 73KB → 行截断形同虚设）。
    阈值仍按字节（TOOL_OUTPUT_MAX_BYTES）；预览预算按字符（PREVIEW_*_CHARS）。
    """
    if not isinstance(output, str):
        output = str(output)
    if not output:
        return output

    lines = output.split("\n")
    byte_size = len(output.encode("utf-8"))

    if len(lines) <= max_lines and byte_size <= max_bytes:
        return output

    file_path = _save_tool_output(output)
    return build_tool_output_preview(output, file_path)


def build_tool_output_preview(output: str, file_path: str) -> str:
    """Build a line+char dual-capped preview for an already-saved large output.

    Always keeps the ``Full output saved to …`` marker so the mid-layer
    contract (disk + handle) survives the total preview budget.
    """
    lines = output.split("\n")
    byte_size = len(output.encode("utf-8", errors="replace"))

    def _cap_line(line: str) -> str:
        if len(line) <= PREVIEW_LINE_MAX_CHARS:
            return line
        return line[: PREVIEW_LINE_MAX_CHARS - 1] + "…"

    marker = (
        f"\n\n... [output truncated: {len(lines)} lines, {byte_size} bytes. "
        f"Full output saved to {file_path}] ...\n\n"
    )
    # Reserve marker (+ small tail budget) so total-cap never drops the handle
    tail_budget = PREVIEW_LINE_MAX_CHARS * PREVIEW_TAIL_LINES + 64
    head_budget = max(512, PREVIEW_MAX_CHARS - len(marker) - tail_budget)

    head_lines = [_cap_line(l) for l in lines[:PREVIEW_HEAD_LINES]]
    head = "\n".join(head_lines)
    if len(head) > head_budget:
        head = head[: head_budget - 1].rstrip() + "…"

    tail = ""
    if len(lines) > PREVIEW_TAIL_THRESHOLD:
        tail_lines = [_cap_line(l) for l in lines[-PREVIEW_TAIL_LINES:]]
        tail = "\n".join(tail_lines)
        if len(tail) > tail_budget:
            tail = tail[: tail_budget - 1].rstrip() + "…"

    preview = head + marker + tail
    if len(preview) > PREVIEW_MAX_CHARS:
        # Last resort: keep marker + as much head as fits; drop tail
        keep = PREVIEW_MAX_CHARS - len(marker) - 24
        if keep < 64:
            return marker.strip() + "\n... [preview capped] ..."
        preview = head[:keep].rstrip() + marker + "... [preview capped] ..."
    return preview


def _save_tool_output(output: str) -> str:
    """保存工具输出到临时文件，返回文件路径。"""
    tmp_dir = Path(tempfile.gettempdir()) / "hiveweave_tool_output"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    filename = f"tool_{int(time.time() * 1000)}_{hash(output) & 0xFFFF:x}.txt"
    full_path = tmp_dir / filename
    try:
        full_path.write_text(output, encoding="utf-8")
    except OSError as e:
        logger.warning("save_tool_output_failed", error=str(e))
    return str(full_path)


def cleanup_tool_outputs() -> None:
    """清理 7 天前的工具输出临时文件。"""
    tmp_dir = Path(tempfile.gettempdir()) / "hiveweave_tool_output"
    if not tmp_dir.exists():
        return
    now = time.time()
    max_age = 7 * 86400  # 7 天
    for f in tmp_dir.iterdir():
        try:
            if now - f.stat().st_mtime > max_age:
                f.unlink()
        except OSError:
            pass


def compute_prefix_hash(content: str) -> str:
    """计算前缀内容的 SHA-256 哈希（前缀缓存漂移检测）。"""
    if not isinstance(content, str):
        content = str(content)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# ── prune 选址：单一实现（两处调用点共用）─────────────────────
#
# 背景（2026-09-21 收口）：同一套「逆序挑可裁剪 tool 输出」的算法在
# `llm/streamer/context.py`（in-loop 临时裁剪）与 `conversation/store.py`
# （持久化裁剪，写 cache+DB）各写了一份，且**已经漂移**：
#   · 持久化版多一条「压缩摘要边界」停止条件；
#   · 占位符一处是常量、一处是硬编码字面量；
#   · 阈值靠注释「与 streamer 对齐」（声明同源 ≠ 同源）。
# 本仓反复吃亏的形态就是「同一算法两次实现（已 5 次）」，故收口成下面这一
# 个函数：**选址**共用，**apply 各写**（两处的 apply 合法地不同）。


@dataclass(frozen=True)
class PrunePlan:
    """一次 prune 的选址结果（纯数据，不碰消息本体）。

    ``indices`` 空有两种**不同**的成因，调用方要分开记日志/区分处置：
    ``candidate_count == 0`` = 没有候选；``candidate_count > 0`` 但
    ``indices == ()`` = 候选收益不足 ``minimum_tokens``。
    """

    indices: tuple[int, ...]   # 逆序扫描 ⇒ **降序**（越靠后越新）
    prune_tokens: int
    protected_tokens: int
    turns: int
    candidate_count: int


def select_prune_indices(
    messages: list[dict],
    *,
    protect_tokens: int = PRUNE_PROTECT_TOKENS,
    minimum_tokens: int = PRUNE_MINIMUM_TOKENS,
    placeholder: str = PRUNE_PLACEHOLDER,
    stop_predicate: Callable[[dict], bool] | None = None,
    min_messages: int = 6,
) -> PrunePlan:
    """逆序选出可裁剪的 tool 结果下标（**降序**：越靠后越新）。

    规则（**消息数下限这条规则也归本函数** —— 调用点不要再自己写一遍
    `len(messages) < 6`：否则默认值哪天改了也没人会发现，正是本次要治的
    「声明与实际不同源」）：

    1. 消息数 < ``min_messages``（默认 6）⇒ 空计划；
    2. 逆序遍历，``role == "assistant"`` 计一轮；最近 2 轮内的消息不动；
    3. ``stop_predicate`` 命中即**停止**（调用方声明自己的边界：持久化路径要
       停在「压缩摘要边界」，in-loop 路径不需要）；
    4. 非 tool 结果（无 ``tool_call_id``）跳过；
    5. 已带占位符 ⇒ **停止**（更早的都已裁过）；
    6. 累计受保护 token 超 ``protect_tokens`` 之后的旧 tool 输出进候选；
    7. 候选总量 < ``minimum_tokens`` ⇒ 收益不足，返回 ``indices=()``
       （此时 ``candidate_count > 0``，与「无候选」可区分）。

    ⚠ 只选址、不改写。in-loop 的 apply 还要丢 ``images`` 并标记 context-rewrite，
    持久化的 apply 要 in-place 改 cache + 入写队列 —— 那两步**故意不共用**。
    """
    if len(messages) < min_messages:
        return PrunePlan((), 0, 0, 0, 0)

    indices: list[int] = []
    protected = 0
    turns = 0

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        role = msg.get("role", "")

        if role == "assistant":
            turns += 1
        if turns < 2:
            continue

        if stop_predicate is not None and stop_predicate(msg):
            break

        if "tool_call_id" not in msg:
            continue

        # 已被裁剪过 → 停止（更早的也已裁剪）
        if msg.get("content") == placeholder:
            break

        tokens = estimate_tokens_for_messages([msg])
        new_protected = protected + tokens
        if new_protected <= protect_tokens:
            protected = new_protected
        else:
            indices.append(i)

    if not indices:
        return PrunePlan((), 0, protected, turns, 0)

    prune_tokens = sum(
        estimate_tokens_for_messages([messages[i]]) for i in indices
    )
    if prune_tokens < minimum_tokens:
        # 候选非空但收益不足 —— `candidate_count` 让调用方与「无候选」分开
        return PrunePlan((), prune_tokens, protected, turns, len(indices))
    return PrunePlan(tuple(indices), prune_tokens, protected, turns, len(indices))
