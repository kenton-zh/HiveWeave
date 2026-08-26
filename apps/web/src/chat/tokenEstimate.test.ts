import { describe, expect, it } from "vitest";
import {
  estimateMessageTokens,
  estimateSegmentTokens,
  estimateTokens,
} from "./tokenEstimate";
import type { ChatMessage } from "./types";

describe("estimateTokens", () => {
  it("空值返回 0", () => {
    expect(estimateTokens("")).toBe(0);
    expect(estimateTokens(null)).toBe(0);
    expect(estimateTokens(undefined)).toBe(0);
  });

  it("英文按 ~4 chars/token", () => {
    expect(estimateTokens("abcd")).toBe(1);
    expect(estimateTokens("hello world")).toBe(3); // 11 chars → ceil(2.75)
  });

  it("CJK 按 ~1 char/token", () => {
    expect(estimateTokens("你好世界")).toBe(4);
  });

  it("中英混排分段计费", () => {
    // "你好" = 2 CJK + "ab" = 2 non-CJK → ceil(0.5 + 2) = 3
    expect(estimateTokens("你好ab")).toBe(3);
  });

  it("CJK 标点（U+3000-303F）按 CJK 计费", () => {
    // 。，、《》共 5 字 → 5；不落入 non-CJK /4
    expect(estimateTokens("。，、《》")).toBe(5);
  });

  it("全角字符（U+FF00-FFEF）按 CJK 计费", () => {
    // Ａ！？３ 个全角 → 3
    expect(estimateTokens("Ａ！？")).toBe(3);
  });

  it("前后端黄金向量（锁 token_utils.py estimate_tokens 对齐）", () => {
    // 期望值由后端 hiveweave.conversation.token_utils.estimate_tokens 实算（2026-08-26）：
    // uv run python -c "from hiveweave.conversation.token_utils import estimate_tokens as e; print(e(...))"
    expect(estimateTokens("你好世界，hello world!")).toBe(8); // 5 CJK(含，) + 12 non-CJK → ceil(3)+5
    expect(estimateTokens("def main():\n    print('hi')")).toBe(7); // 24 chars → ceil(6)
    expect(estimateTokens("混合Mixed文本123")).toBe(6); // 4 CJK + 8 non-CJK → ceil(2)+4
  });
});

describe("estimateSegmentTokens", () => {
  it("只统计 text/thinking，跳过 tool_call", () => {
    const tokens = estimateSegmentTokens([
      { type: "text", content: "abcd" },
      { type: "thinking", content: "思考" },
      { type: "tool_call", tool: { tool: "read_file", input: { path: "x" } } },
    ]);
    expect(tokens).toBe(1 + 2);
  });

  it("空/undefined segments 返回 0", () => {
    expect(estimateSegmentTokens(undefined)).toBe(0);
    expect(estimateSegmentTokens([])).toBe(0);
  });
});

describe("estimateMessageTokens", () => {
  it("无 segments 时统计 content + _thinking", () => {
    const msg: ChatMessage = {
      id: "m1",
      role: "assistant",
      content: "abcd",
      timestamp: 0,
      _thinking: "想想",
    };
    expect(estimateMessageTokens(msg)).toBe(1 + 2);
  });

  it("segments 含 thinking 时不再重复计 _thinking", () => {
    const msg: ChatMessage = {
      id: "m2",
      role: "assistant",
      content: "",
      timestamp: 0,
      _thinking: "想想",
      _segments: [{ type: "thinking", content: "想想" }],
    };
    expect(estimateMessageTokens(msg)).toBe(2);
  });

  it("segments 不含 thinking 时补计 _thinking（legacy 兜底）", () => {
    const msg: ChatMessage = {
      id: "m3",
      role: "assistant",
      content: "",
      timestamp: 0,
      _thinking: "想想",
      _segments: [{ type: "text", content: "abcd" }],
    };
    expect(estimateMessageTokens(msg)).toBe(1 + 2);
  });
});
