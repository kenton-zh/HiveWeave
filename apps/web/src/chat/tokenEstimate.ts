import type { ChatMessage, MsgSegment } from "./types";

/**
 * 前端 token 估算 —— 镜像后端 conversation/token_utils.py 的 estimate_tokens。
 * char-ratio 启发式：非 CJK ~4 chars/token，CJK ~1.0 chars/token。
 * 前后端必须同一把尺子，否则 UI 显示与压缩预算口径不一致。
 * 已知微差：JS length 按 UTF-16 code unit（emoji 计 2），Python len 按
 * code point（计 1）——启发式本身 ±15%，忽略。
 */
const CJK_RE = /[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]/g;

export function estimateTokens(text: string | null | undefined): number {
  if (!text) return 0;
  const cjkCount = (text.match(CJK_RE) || []).length;
  const nonCjk = text.length - cjkCount;
  return Math.ceil(nonCjk / 4 + cjkCount);
}

/** 统计 segments 中模型实际生成的文本（text + thinking），不含工具入参/结果。 */
export function estimateSegmentTokens(segments: MsgSegment[] | undefined): number {
  if (!segments) return 0;
  let total = 0;
  for (const seg of segments) {
    if ((seg.type === "text" || seg.type === "thinking") && seg.content) {
      total += estimateTokens(seg.content);
    }
  }
  return total;
}

/** 整条消息（持久化或流式合并后）的生成 token 估算。 */
export function estimateMessageTokens(msg: ChatMessage): number {
  if (msg._segments && msg._segments.length > 0) {
    const segTokens = estimateSegmentTokens(msg._segments);
    // segments 已含 thinking 时 _thinking 只是 legacy 兜底，不重复计。
    const hasThinking = msg._segments.some((s) => s.type === "thinking" && s.content);
    return segTokens + (hasThinking ? 0 : estimateTokens(msg._thinking));
  }
  return estimateTokens(msg.content) + estimateTokens(msg._thinking);
}
