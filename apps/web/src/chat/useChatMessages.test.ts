import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useCallback, useRef, useState } from "react";
import { useChatMessages } from "./useChatMessages";
import { useAppStore } from "../store";
import type { ChatMessage, StreamDraft } from "./types";
import { getAgent, getChatMessages, markMessagesRead, subscribeAgentStream } from "../api";

vi.mock("../api", () => ({
  getAgent: vi.fn(async (id: string) => ({ id, name: id, role: "ceo", status: "idle" })),
  getChatMessages: vi.fn(async () => []),
  markMessagesRead: vi.fn(async () => ({})),
  subscribeAgentStream: vi.fn(() => () => {}),
}));

function useHarness(agentId: string | null) {
  const activeAgentIdRef = useRef<string | null>(agentId);
  activeAgentIdRef.current = agentId;
  const streamDraftRef = useRef<StreamDraft | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const updateStreamDraft = useCallback(() => {}, []);
  const setThinkingElapsed = useCallback(() => {}, []);
  return useChatMessages({
    agentId,
    streamDraft: null,
    streamDraftRef,
    updateStreamDraft,
    isStreaming,
    setIsStreaming,
    setThinkingElapsed,
    activeAgentIdRef,
  });
}

const letterA: ChatMessage = {
  id: "t-a",
  role: "team",
  content: "A letter",
  timestamp: 1,
  isRead: true,
};

const dbLetterA = {
  id: "t-a",
  role: "team",
  content: "A letter",
  created_at: 1,
  isRead: true,
  isBackground: true,
};

describe("useChatMessages agent switch cache", () => {
  beforeEach(() => {
    useAppStore.getState().clearChatSessions();
    vi.mocked(getChatMessages).mockReset();
    vi.mocked(getChatMessages).mockResolvedValue([]);
    vi.mocked(getAgent).mockClear();
    vi.mocked(markMessagesRead).mockClear();
    vi.mocked(subscribeAgentStream).mockClear();
  });

  afterEach(() => {
    useAppStore.getState().clearChatSessions();
  });

  it("keeps A's 团队沟通 while B is still loading, then restores A on switch back", async () => {
    useAppStore.getState().setChatMessages("A", [letterA]);
    let resolveB: (rows: unknown[]) => void = () => {};
    const pendingB = new Promise<unknown[]>((resolve) => {
      resolveB = resolve;
    });
    vi.mocked(getChatMessages).mockImplementation(async (id: string) => {
      if (id === "A") return [dbLetterA];
      return pendingB;
    });

    const { rerender, result } = renderHook(({ id }) => useHarness(id), {
      initialProps: { id: "A" },
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.teamMessages.some((m) => m.id === "t-a")).toBe(true);

    act(() => {
      rerender({ id: "B" });
    });
    expect(useAppStore.getState().chatSessions.A?.some((m) => m.id === "t-a")).toBe(true);
    expect(useAppStore.getState().chatSessions.B?.some((m) => m.id === "t-a")).toBeFalsy();
    expect(result.current.teamMessages.some((m) => m.id === "t-a")).toBe(false);

    await act(async () => {
      resolveB([]);
      await Promise.resolve();
    });
    expect(useAppStore.getState().chatSessions.A?.some((m) => m.id === "t-a")).toBe(true);

    await act(async () => {
      rerender({ id: "A" });
      await Promise.resolve();
    });
    expect(result.current.teamMessages.some((m) => m.id === "t-a")).toBe(true);
  });

  it("does not empty A's transcript when org tree refreshes", async () => {
    useAppStore.getState().setChatMessages("A", [letterA]);
    vi.mocked(getChatMessages).mockResolvedValue([dbLetterA]);
    const { result } = renderHook(({ id }) => useHarness(id), { initialProps: { id: "A" } });
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.teamMessages.some((m) => m.id === "t-a")).toBe(true);

    await act(async () => {
      useAppStore.getState().refreshOrgTree();
      await Promise.resolve();
    });
    expect(result.current.teamMessages.some((m) => m.id === "t-a")).toBe(true);
    expect(useAppStore.getState().chatSessions.A?.some((m) => m.id === "t-a")).toBe(true);
  });
});
