import { describe, it, expect } from "vitest";
import {
  appendToolCallSegment,
  applyToolResult,
  beginStreamRound,
  draftFromStreamingMessage,
  isTeamChannelMessage,
  mapDbToChatMessages,
  mergeStreamDraftIntoMessages,
  nextBadgePopToken,
  sanitizeMessagesForCache,
  settledMessageHasSegments,
  shouldWriteChatCache,
  streamEventBackgroundFlag,
  streamEventIsBackground,
  tryParseToolCalls,
} from "./messageUtils";
import type { ChatMessage, StreamDraft } from "./types";

describe("beginStreamRound", () => {
  it("round>=1 时插入轮次分隔段，且不丢弃任何已有内容", () => {
    const draft: StreamDraft = {
      assistantId: "a1",
      segments: [
        { type: "thinking", content: "plan v1" },
        { type: "text", content: "用户选了方案2" },
        { type: "tool_call", tool: { tool: "get_tasks", input: {} } },
      ],
    };
    const next = beginStreamRound(draft, 1);
    expect(next.assistantId).toBe("a1");
    // 全量保留（DSH 整轮视图）+ 尾部分隔段
    expect(next.segments.slice(0, 3)).toEqual(draft.segments);
    expect(next.segments[3]).toEqual({
      type: "text",
      content: "\n\n—— 第 2 轮 ——\n\n",
    });
    // 原 draft 不被突变
    expect(draft.segments).toHaveLength(3);
  });

  it("round 0（首轮）与缺号 no-op——首轮无需分隔", () => {
    const draft: StreamDraft = {
      assistantId: "a1",
      segments: [{ type: "text", content: "首轮旁白" }],
    };
    expect(beginStreamRound(draft, 0)).toBe(draft);
    expect(beginStreamRound(draft, undefined)).toBe(draft);
  });

  it("分隔段是 text 段——后续 text_delta 自然并入，轮内旁白连续", () => {
    let draft: StreamDraft = {
      assistantId: "a1",
      segments: [{ type: "text", content: "第一轮旁白" }],
    };
    draft = beginStreamRound(draft, 1);
    // 模拟 text_delta 合并逻辑（last 为 text → merge）
    const last = draft.segments[draft.segments.length - 1];
    expect(last.type).toBe("text");
    const merged = ((last as { content?: string }).content || "") + "第二轮旁白开始";
    expect(merged).toContain("—— 第 2 轮 ——");
    expect(merged.endsWith("第二轮旁白开始")).toBe(true);
  });
});

describe("appendToolCallSegment", () => {
  const empty: StreamDraft = { assistantId: "a1", segments: [] };

  it("dedups by toolCallId on replay", () => {
    const once = appendToolCallSegment(empty, { tool: "get_tasks", input: {} }, "tc-1");
    const twice = appendToolCallSegment(once, { tool: "get_tasks", input: { x: 1 } }, "tc-1");
    expect(twice.segments).toHaveLength(1);
    expect(twice.segments[0].tool).toEqual({ tool: "get_tasks", input: {}, id: "tc-1" });
    expect(twice).toBe(once);
  });

  it("appends twice when id is missing (repeat calls of the same tool)", () => {
    const once = appendToolCallSegment(empty, { tool: "bash", input: { command: "ls" } });
    const twice = appendToolCallSegment(once, { tool: "bash", input: { command: "ls" } });
    expect(twice.segments).toHaveLength(2);
    expect(twice.segments.every((s) => s.tool?.tool === "bash" && !s.tool?.id)).toBe(true);
  });

  it("keeps distinct ids as separate chips", () => {
    const once = appendToolCallSegment(empty, { tool: "read_file", input: { path: "a" } }, "tc-a");
    const twice = appendToolCallSegment(once, { tool: "read_file", input: { path: "b" } }, "tc-b");
    expect(twice.segments).toHaveLength(2);
    expect(twice.segments.map((s) => s.tool?.id)).toEqual(["tc-a", "tc-b"]);
  });
});

describe("applyToolResult", () => {
  const draft: StreamDraft = {
    assistantId: "a1",
    segments: [
      { type: "tool_call", tool: { tool: "bash", input: {}, id: "c1", status: "running" } },
      { type: "tool_call", tool: { tool: "bash", input: {}, id: "c2", status: "running" } },
      { type: "tool_call", tool: { tool: "read_file", input: {}, status: "running" } },
    ],
  };

  it("updates the segment matched by tool_call_id (id path, no name needed)", () => {
    const next = applyToolResult(draft, "c1", undefined, false, "boom");
    expect(next.segments[0].tool?.status).toBe("error");
    expect(next.segments[0].tool?.result).toBe("boom");
    expect(next.segments[1].tool?.status).toBe("running");
    // 非 target 段对象不可变（浅拷贝更新）
    expect(next.segments[1]).toBe(draft.segments[1]);
  });

  it("falls back to name with hiveweave__ prefix stripped, last running match only", () => {
    const two: StreamDraft = {
      assistantId: "a1",
      segments: [
        { type: "tool_call", tool: { tool: "bash", input: {}, status: "running" } },
        { type: "tool_call", tool: { tool: "bash", input: {}, status: "running" } },
      ],
    };
    const next = applyToolResult(two, undefined, "hiveweave__bash", true, "done");
    // 只更新最后一个 running 段（并行同名不写串）
    expect(next.segments[0].tool?.status).toBe("running");
    expect(next.segments[1].tool?.status).toBe("ok");
    expect(next.segments[1].tool?.result).toBe("done");
  });

  it("is idempotent on duplicate tool_result events (finished segments untouched)", () => {
    const once = applyToolResult(draft, "c1", undefined, true, "ok");
    const twice = applyToolResult(once, "c1", "bash", false, "second event");
    // 已完结段不被第二个完成信号覆盖（双广播路径幂等）
    expect(twice.segments[0].tool?.status).toBe("ok");
    expect(twice.segments[0].tool?.result).toBe("ok");
  });

  it("does not mis-write when id is missing but segments have ids", () => {
    // 事件无 id、段全带 id：按名称兜底仅在段无 id 或名称匹配时命中——
    // 此处段名 bash/read_file 与事件名 bash 匹配 → 更新最后一个 bash 段
    const next = applyToolResult(draft, undefined, "bash", true, "r");
    expect(next.segments[1].tool?.status).toBe("ok");
    expect(next.segments[0].tool?.status).toBe("running");
  });

  it("returns the same draft when nothing matches", () => {
    const next = applyToolResult(draft, "nope", undefined, true, "x");
    expect(next).toBe(draft);
  });
});

describe("tryParseToolCalls", () => {
  it("defaults legacy persisted calls to ok (no eternal spinner)", () => {
    const calls = tryParseToolCalls(
      JSON.stringify([{ id: "c1", type: "function", function: { name: "bash", arguments: "{}" } }]),
    );
    expect(calls[0].status).toBe("ok");
  });

  it("maps ok:false (tool_history failure mark) to error", () => {
    const calls = tryParseToolCalls(
      JSON.stringify([{ id: "c1", type: "function", function: { name: "bash", arguments: "{}" }, ok: false }]),
    );
    expect(calls[0].status).toBe("error");
  });
});

describe("nextBadgePopToken", () => {
  it("does not pop on first observation / remount", () => {
    expect(nextBadgePopToken(null, 5, 0)).toEqual({ token: 0, lastSeen: 5 });
  });

  it("pops only when count increments", () => {
    expect(nextBadgePopToken(3, 4, 0)).toEqual({ token: 1, lastSeen: 4 });
    expect(nextBadgePopToken(4, 4, 1)).toEqual({ token: 1, lastSeen: 4 });
    expect(nextBadgePopToken(4, 2, 1)).toEqual({ token: 1, lastSeen: 2 });
  });
});

describe("draftFromStreamingMessage", () => {
  it("omits tool chips when live replay will supply them", () => {
    const draft = draftFromStreamingMessage(
      {
        id: "m1",
        role: "assistant",
        content: "hi",
        timestamp: 1,
        toolCalls: [{ tool: "bash", input: { command: "ls" } }],
        _thinking: "plan",
      },
      { includeTools: false },
    );
    expect(draft.segments.map((s) => s.type)).toEqual(["thinking", "text"]);
  });

  it("copies isBackground onto the draft", () => {
    const draft = draftFromStreamingMessage({
      id: "m1",
      role: "assistant",
      content: "",
      timestamp: 1,
      isBackground: true,
    });
    expect(draft.isBackground).toBe(true);
  });
});

describe("isTeamChannelMessage", () => {
  it("is the letter tray: team rows and background user, not tool-loop assistants", () => {
    expect(
      isTeamChannelMessage({
        id: "a",
        role: "assistant",
        content: "chip",
        timestamp: 1,
        isBackground: true,
      }),
    ).toBe(false);
    expect(
      isTeamChannelMessage({
        id: "u",
        role: "user",
        content: "wake",
        timestamp: 1,
        isBackground: true,
      }),
    ).toBe(true);
    expect(
      isTeamChannelMessage({
        id: "fg",
        role: "assistant",
        content: "hello",
        timestamp: 1,
        isBackground: false,
      }),
    ).toBe(false);
    expect(
      isTeamChannelMessage({
        id: "t",
        role: "team",
        content: "peer",
        timestamp: 1,
      }),
    ).toBe(true);
  });
});

describe("streamEventIsBackground", () => {
  it("defaults true and honors payload flags", () => {
    expect(streamEventIsBackground(undefined)).toBe(true);
    expect(streamEventIsBackground({ is_background: false })).toBe(false);
    expect(streamEventIsBackground({ isBackground: true })).toBe(true);
    expect(streamEventIsBackground({ role: "assistant" })).toBe(true);
  });

  it("returns undefined when the payload has no background flag", () => {
    expect(streamEventBackgroundFlag({ role: "assistant" })).toBeUndefined();
    expect(streamEventBackgroundFlag({ is_background: false })).toBe(false);
    expect(streamEventBackgroundFlag({ is_background: 0 })).toBe(false);
    expect(streamEventBackgroundFlag({ isBackground: 1 })).toBe(true);
  });
});

describe("mergeStreamDraftIntoMessages", () => {
  it("overlays chips onto a background assistant without touching others", () => {
    const msgs = [
      { id: "u1", role: "user" as const, content: "hi", timestamp: 1, isBackground: false },
      { id: "a1", role: "assistant" as const, content: "", timestamp: 2, isBackground: true },
    ];
    const merged = mergeStreamDraftIntoMessages(
      msgs,
      {
        assistantId: "a1",
        isBackground: true,
        segments: [{ type: "tool_call", tool: { tool: "hire_agent", input: {} } }],
      },
      { isStreaming: true },
    );
    expect(merged[0].isBackground).toBe(false);
    expect(merged[1].isBackground).toBe(true);
    expect(merged[1].toolCalls?.map((t) => t.tool)).toEqual(["hire_agent"]);
    expect(merged[1].isStreaming).toBe(true);
  });

  it("does not latch background true when the draft omits the flag", () => {
    const msgs = [
      { id: "a1", role: "assistant" as const, content: "", timestamp: 1, isBackground: false },
    ];
    const merged = mergeStreamDraftIntoMessages(
      msgs,
      { assistantId: "a1", segments: [{ type: "text", content: "hi" }] },
      { isStreaming: true },
    );
    expect(merged[0].isBackground).toBe(false);
    expect(merged[0].content).toBe("hi");
  });
});

describe("mapDbToChatMessages", () => {
  it("treats snake_case is_background as team-channel background", () => {
    const mapped = mapDbToChatMessages([
      { id: "d1", role: "user", content: "digest", created_at: 1, is_background: 1 },
      { id: "t1", role: "team", content: "letter", created_at: 2, is_background: 0 },
    ]);
    expect(mapped[0].isBackground).toBe(true);
    expect(mapped[1].isBackground).toBe(false);
    expect(isTeamChannelMessage(mapped[0])).toBe(true);
    expect(isTeamChannelMessage(mapped[1])).toBe(true);
  });

  it("restores thinking + tool_call segments from metadata.segments (DSH block timeline)", () => {
    const mapped = mapDbToChatMessages([
      {
        id: "a1",
        role: "assistant",
        content: "答复",
        metadata: JSON.stringify({
          segments: [
            { type: "thinking", content: "先分析" },
            { type: "text", content: "开始处理" },
            { type: "tool_call", tool: "read_file", id: "c1", input: { path: "a.py" }, status: "ok", result: "body" },
            { type: "thinking", content: "再总结" },
          ],
        }),
      },
    ]);
    expect(mapped[0]._segments?.map((s) => s.type)).toEqual([
      "thinking",
      "text",
      "tool_call",
      "thinking",
    ]);
    expect(mapped[0]._segments?.[0]).toEqual({ type: "thinking", content: "先分析" });
    expect(mapped[0]._segments?.[3]).toEqual({ type: "thinking", content: "再总结" });
    expect(mapped[0]._segments?.[2].tool?.tool).toBe("read_file");
  });
});

describe("shouldWriteChatCache", () => {
  const team = { id: "t1", role: "team" as const, content: "letter", timestamp: 1 };

  it("does not write the previous person's snapshot into the new agent slot", () => {
    expect(
      shouldWriteChatCache({
        agentId: "B",
        messagesOwnerId: "A",
        persistReady: true,
        next: [team],
        existing: undefined,
      }),
    ).toBe(false);
  });

  it("does not persist a loading-empty list over a populated session", () => {
    expect(
      shouldWriteChatCache({
        agentId: "A",
        messagesOwnerId: "A",
        persistReady: false,
        next: [],
        existing: [team],
      }),
    ).toBe(false);
    expect(
      shouldWriteChatCache({
        agentId: "A",
        messagesOwnerId: "A",
        persistReady: true,
        next: [],
        existing: [team],
      }),
    ).toBe(false);
  });

  it("writes once the loaded transcript belongs to the viewed agent", () => {
    expect(
      shouldWriteChatCache({
        agentId: "A",
        messagesOwnerId: "A",
        persistReady: true,
        next: [team],
        existing: undefined,
      }),
    ).toBe(true);
  });
});

describe("sanitizeMessagesForCache", () => {
  it("drops empty finished assistants and clears streaming flags", () => {
    const next = sanitizeMessagesForCache([
      { id: "t1", role: "team", content: "letter", timestamp: 1, isStreaming: true },
      { id: "a1", role: "assistant", content: "", timestamp: 2, isStreaming: false },
      {
        id: "a2",
        role: "assistant",
        content: "",
        timestamp: 3,
        isStreaming: false,
        toolCalls: [{ tool: "bash", input: {} }],
      },
    ]);
    expect(next.map((m) => m.id)).toEqual(["t1", "a2"]);
    expect(next[0].isStreaming).toBe(false);
    expect(next[1].toolCalls?.[0].tool).toBe("bash");
  });
});


describe("settledMessageHasSegments（八轮收口竞态 fetch-then-swap 判据）", () => {
  const mk = (over: Partial<ChatMessage> & { id: string }): ChatMessage => ({
    role: "assistant",
    content: "",
    timestamp: Date.now(),
    ...over,
  });

  it("带非空 _segments 的消息放行", () => {
    const msgs = [mk({ id: "m1", _segments: [{ type: "text", content: "hi" }] })];
    expect(settledMessageHasSegments(msgs, "m1")).toBe(true);
  });

  it("无 _segments 的中途快照不放行（旁白拼接平文本）", () => {
    const msgs = [mk({ id: "m1", content: "旁白旁白旁白" })];
    expect(settledMessageHasSegments(msgs, "m1")).toBe(false);
    expect(settledMessageHasSegments(msgs, "m1")).toBe(false);
  });

  it("目标消息不存在（未走流式的纯文本路径）放行", () => {
    expect(settledMessageHasSegments([mk({ id: "other" })], "m1")).toBe(true);
    expect(settledMessageHasSegments([], null)).toBe(true);
  });
});
