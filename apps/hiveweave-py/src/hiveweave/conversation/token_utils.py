"""Token estimation and budget calculation utilities.

契约 03: 对话历史与压缩
- char-ratio 启发式：4 chars/token EN, ~1.0 CJK
- 对齐 Elixir token_utils.ex + TS token-utils.ts
- 工具输出超限截断并保存到临时文件（OpenCode ToolOutputStore 模式）
"""

import hashlib
import math
import re
import tempfile
import time
from pathlib import Path

import structlog

logger = structlog.get_logger()

# ── 常量（契约 03 constants）─────────────────────────────────
COMPACTION_BUFFER = 20_000
PRESERVE_RECENT_MIN = 10
PRESERVE_RECENT_MAX = 30
TAIL_TURNS = 2
PRUNE_PROTECT_TOKENS = 40_000
PRUNE_MINIMUM_TOKENS = 20_000
TOOL_OUTPUT_MAX_CHARS = 2_000

# 工具输出智能截断限制（镜像 OpenCode ToolOutputStore）
TOOL_OUTPUT_MAX_LINES = 2_000
TOOL_OUTPUT_MAX_BYTES = 51_200  # 50 KB

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
    """估算消息列表的总 token 数（含 tool_calls arguments）。"""
    if not messages:
        return 0
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, list):
            # 多模态 content — 拼接 text 部分
            content = "".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
        total += estimate_tokens(content)
        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            total += estimate_tokens(fn.get("arguments") or "")
    return total


def calculate_history_budget(messages: list, context_window: int) -> int:
    """计算对话历史可用 token 预算。

    budget = context_window - COMPACTION_BUFFER
    messages 参数预留给未来按静态 prompt 扣减的扩展（当前仅减 buffer）。
    """
    if context_window <= 0:
        return 0
    return max(context_window - COMPACTION_BUFFER, 0)


def truncate_tool_output(
    output: str, max_lines: int = TOOL_OUTPUT_MAX_LINES, max_bytes: int = TOOL_OUTPUT_MAX_BYTES
) -> str:
    """智能截断工具输出。超限时保存完整内容到临时文件，返回 head+tail 预览。

    镜像 OpenCode ToolOutputStore 模式：保留头部（结构/上下文）和尾部（结果/结论），
    中间用省略标记替换，附带完整输出的临时文件路径。
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
    head_lines = lines[:20]
    tail_lines = lines[-5:] if len(lines) > 20 else []

    marker = (
        f"\n\n... [output truncated: {len(lines)} lines, {byte_size} bytes. "
        f"Full output saved to {file_path}] ...\n\n"
    )
    return "\n".join(head_lines + [marker] + tail_lines)


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
