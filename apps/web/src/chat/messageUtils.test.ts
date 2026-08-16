import { describe, it, expect } from "vitest";
import {
  appendToolCallSegment,
  beginStreamRound,
  draftFromStreamingMessage,
  nextBadgePopToken,
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
});
