import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useRef } from "react";
import { useChatSend } from "./useChatSend";
import { streamChat } from "../api";
import type { StreamDraft } from "./types";
import type { ChatEvent } from "../api/ws";

vi.mock("../api", () => ({
  streamChat: vi.fn(() => ({ abort: vi.fn() })),
  joinAgentChannel: vi.fn(() => Promise.resolve()),
}));

type HarnessProps = {
  agentId: string | null;
  isStreaming: boolean;
  isAgentProcessing: boolean;
};

/**
 * Minimal harness mirroring ChatPanel's wiring: stable refs, agentId as a prop
 * (ChatPanel is NOT remounted on agent switch — same hook instance survives).
 */
function useHarness(props: HarnessProps) {
  const activeAgentIdRef = useRef<string | null>(props.agentId);
  activeAgentIdRef.current = props.agentId;
  const streamDraftRef = useRef<StreamDraft | null>(null);
  const stickToBottomRef = useRef(true);
  return useChatSend({
    agentId: props.agentId,
    activeAgentIdRef,
    streamDraftRef,
    updateStreamDraft: () => {},
    isStreaming: props.isStreaming,
    setIsStreaming: () => {},
    isAgentProcessing: props.isAgentProcessing,
    loadMessagesFromDb: vi.fn(async () => true),
    setMessages: () => {},
    refreshOrgTree: () => {},
    thinkingElapsed: null,
    setThinkingElapsed: () => {},
    stickToBottomRef,
  });
}

describe("useChatSend — per-agent send queue", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(streamChat).mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("never auto-sends a queued message to a different agent after switching chats", () => {
    const { result, rerender } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: true, isAgentProcessing: true },
    });

    // Agent A is busy → message is parked in the queue.
    act(() => result.current.setInput("把团队扩散一下"));
    act(() => result.current.handleSend());
    expect(streamChat).not.toHaveBeenCalled();
    expect(result.current.queuedCount).toBe(1);

    // Switch to idle agent B — the old bug drained A's queue into B here.
    rerender({ agentId: "B", isStreaming: false, isAgentProcessing: false });
    expect(streamChat).not.toHaveBeenCalled();
    // Banner counts only the viewed agent's entries.
    expect(result.current.queuedCount).toBe(0);

    // Switch back to A, now idle → the parked message drains to A only.
    rerender({ agentId: "A", isStreaming: false, isAgentProcessing: false });
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamChat).mock.calls[0][0]).toBe("A");
    expect(vi.mocked(streamChat).mock.calls[0][1]).toBe("把团队扩散一下");
    expect(result.current.queuedCount).toBe(0);
  });

  it("handleStop clears only the viewed agent's queued entries", () => {
    const { result, rerender } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: true, isAgentProcessing: true },
    });

    // Park one message for A (busy) and one for B (busy).
    act(() => result.current.setInput("msg for A"));
    act(() => result.current.handleSend());
    rerender({ agentId: "B", isStreaming: true, isAgentProcessing: true });
    act(() => result.current.setInput("msg for B"));
    act(() => result.current.handleSend());
    expect(result.current.queuedCount).toBe(1);
    expect(streamChat).not.toHaveBeenCalled();

    // Stop on B clears B's entry only; A's entry stays parked.
    act(() => result.current.handleStop());
    expect(result.current.queuedCount).toBe(0);

    rerender({ agentId: "A", isStreaming: true, isAgentProcessing: true });
    expect(result.current.queuedCount).toBe(1);
    expect(streamChat).not.toHaveBeenCalled();
  });

  it("delayed resend timer stays parked when the user switched chats within 300ms", () => {
    let onEvent: (event: ChatEvent) => void = () => {};
    vi.mocked(streamChat).mockImplementation((_id, _msg, _imgs, cb) => {
      onEvent = cb;
      return { abort: vi.fn() };
    });

    const { result, rerender } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: false, isAgentProcessing: false },
    });

    // First message sends immediately; a second one is parked while A streams.
    act(() => result.current.setInput("first"));
    act(() => result.current.handleSend());
    expect(streamChat).toHaveBeenCalledTimes(1);
    rerender({ agentId: "A", isStreaming: true, isAgentProcessing: true });
    act(() => result.current.setInput("second"));
    act(() => result.current.handleSend());
    expect(result.current.queuedCount).toBe(1);

    // A's stream completes → 300ms resend timer armed; user switches to B.
    act(() => onEvent({ type: "done", data: "" }));
    rerender({ agentId: "B", isStreaming: false, isAgentProcessing: false });
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(streamChat).toHaveBeenCalledTimes(1);

    // Back on idle A, the parked message drains to A.
    rerender({ agentId: "A", isStreaming: false, isAgentProcessing: false });
    expect(streamChat).toHaveBeenCalledTimes(2);
    expect(vi.mocked(streamChat).mock.calls[1][0]).toBe("A");
    expect(vi.mocked(streamChat).mock.calls[1][1]).toBe("second");
  });
});
