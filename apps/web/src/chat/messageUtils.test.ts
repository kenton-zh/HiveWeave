import { describe, it, expect } from "vitest";
import { beginStreamRound } from "./messageUtils";
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
