import { describe, it, expect } from "vitest";
import {
  appendToolCallSegment,
  beginStreamRound,
  draftFromStreamingMessage,
  isTeamChannelMessage,
  mapDbToChatMessages,
  mergeStreamDraftIntoMessages,
  nextBadgePopToken,
  sanitizeMessagesForCache,
  shouldWriteChatCache,
  streamEventBackgroundFlag,
  streamEventIsBackground,
} from "./messageUtils";
import type { StreamDraft } from "./types";

describe("beginStreamRound", () => {
  it("keeps tool chips and drops prior-round narration", () => {
    const draft: StreamDraft = {
      assistantId: "a1",
      segments: [
        { type: "thinking", content: "plan v1" },
        { type: "text", content: "用户选了方案2" },
        { type: "tool_call", tool: { tool: "get_tasks", input: {} } },
        { type: "thinking", content: "plan v2" },
        { type: "text", content: "用户选了方案2" },
        { type: "tool_call", tool: { tool: "bash", input: { command: "git rev-parse HEAD" } } },
      ],
    };
    const next = beginStreamRound(draft);
    expect(next.assistantId).toBe("a1");
    expect(next.segments).toEqual([
      { type: "tool_call", tool: { tool: "get_tasks", input: {} } },
      { type: "tool_call", tool: { tool: "bash", input: { command: "git rev-parse HEAD" } } },
    ]);
  });

  it("is a no-op when the draft has only tools", () => {
    const draft: StreamDraft = {
      assistantId: "a1",
      segments: [{ type: "tool_call", tool: { tool: "send_message", input: { recipients: ["Vera"] } } }],
    };
    expect(beginStreamRound(draft).segments).toEqual(draft.segments);
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

