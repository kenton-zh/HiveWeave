import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useRef } from "react";
import { useChatSend } from "./useChatSend";
import { streamChat, pushInsert, getChatMessages } from "../api";
import { useAppStore } from "../store";
import { __resetQueueStoreForTests, useQueueStore } from "./queueStore";
import {
  __resetStopRegistryForTests,
  confirmStopped,
  getStopPhase,
} from "./stopRegistry";
import type { ChatMessage, StreamDraft } from "./types";
import type { ChatEvent } from "../api/ws";

const hoisted = vi.hoisted(() => ({
  /** 模拟 ws 层 Socket.channels：stopRegistry 的 cancel 投递面。 */
  channels: [] as Array<{ topic: string; push: ReturnType<typeof vi.fn> }>,
}));

vi.mock("../api", () => ({
  streamChat: vi.fn(() => ({ abort: vi.fn() })),
  joinAgentChannel: vi.fn(() => Promise.resolve()),
  pushInsert: vi.fn(),
  getChatMessages: vi.fn(async () => []),
  getSocket: vi.fn(() => ({ channels: hoisted.channels })),
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

function resetModules() {
  __resetQueueStoreForTests();
  __resetStopRegistryForTests();
  hoisted.channels.length = 0;
  useAppStore.getState().clearChatSessions();
}

describe("useChatSend — 模块级队列（FE-03：T-04/T-05/SR-06/SR-07/SR-08）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(streamChat).mockClear();
    vi.mocked(pushInsert).mockClear();
    vi.mocked(getChatMessages).mockClear();
    vi.mocked(getChatMessages).mockResolvedValue([] as never);
    resetModules();
  });

  afterEach(() => {
    vi.useRealTimers();
    resetModules();
  });

  it("T-04: 甲排队 → 组件重挂（registry key=agentId 重建）→ 队列完整且接收人仍是甲", () => {
    const first = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: true, isAgentProcessing: true },
    });

    // 甲运行中 → 补充要求入列（不入流）。
    act(() => first.result.current.setInput("补充要求"));
    act(() => first.result.current.handleSend());
    expect(streamChat).not.toHaveBeenCalled();
    expect(useQueueStore.getState().entries).toHaveLength(1);
    expect(useQueueStore.getState().entries[0]).toMatchObject({
      agentId: "A",
      text: "补充要求",
      state: "queued-local",
      mode: "normal",
    });

    // 模拟 registry 的 key=agentId 重建：卸载后全新 hook 实例切到乙。
    first.unmount();
    const second = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "B", isStreaming: false, isAgentProcessing: false },
    });
    // 队列不随组件销毁丢失；也绝不投给乙。
    expect(useQueueStore.getState().entries).toHaveLength(1);
    expect(streamChat).not.toHaveBeenCalled();

    // 切回甲（又一个新实例）且甲空闲 → drain 出队，接收人仍是甲。
    second.unmount();
    renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: false, isAgentProcessing: false },
    });
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamChat).mock.calls[0][0]).toBe("A");
    expect(vi.mocked(streamChat).mock.calls[0][1]).toBe("补充要求");
    // 出队即占位：条目进入 sending，不再被二次投递。
    expect(useQueueStore.getState().entries[0].state).toBe("sending");
  });

  it("T-05/SR-07: 两条不同附件的排队消息互不串，附件绑定当条消息", () => {
    let onEvent: (event: ChatEvent) => void = () => {};
    vi.mocked(streamChat).mockImplementation((_id, _msg, _imgs, cb) => {
      onEvent = cb;
      return { abort: vi.fn() };
    });

    const { result, rerender } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: true, isAgentProcessing: true },
    });

    act(() => result.current.setInput("第一条"));
    act(() => result.current.setImages(["data:image/png;base64,AAA"]));
    act(() => result.current.handleSend());
    // 入列成功才清输入框与附件（快照绑定当条消息，不再挂在输入框上）。
    expect(result.current.input).toBe("");
    expect(result.current.images).toEqual([]);

    act(() => result.current.setInput("第二条"));
    act(() => result.current.setImages(["data:image/png;base64,BBB"]));
    act(() => result.current.handleSend());

    const entries = useQueueStore.getState().entries;
    expect(entries).toHaveLength(2);
    expect(entries[0].attachments).toEqual(["data:image/png;base64,AAA"]);
    expect(entries[1].attachments).toEqual(["data:image/png;base64,BBB"]);

    // 甲空闲 → drain 第一条；done 后 300ms 重发第二条，附件各随各的消息。
    rerender({ agentId: "A", isStreaming: false, isAgentProcessing: false });
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamChat).mock.calls[0][2]).toEqual(["data:image/png;base64,AAA"]);

    act(() => onEvent({ type: "done", data: "" }));
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(streamChat).toHaveBeenCalledTimes(2);
    expect(vi.mocked(streamChat).mock.calls[1][0]).toBe("A");
    expect(vi.mocked(streamChat).mock.calls[1][1]).toBe("第二条");
    expect(vi.mocked(streamChat).mock.calls[1][2]).toEqual(["data:image/png;base64,BBB"]);
  });

  it("SR-06: 纯图片消息可发送（统一校验：有正文或有附件即合法）", () => {
    const { result } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: false, isAgentProcessing: false },
    });
    act(() => result.current.setImages(["data:image/png;base64,IMG"]));
    act(() => result.current.handleSend());
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamChat).mock.calls[0][2]).toEqual(["data:image/png;base64,IMG"]);
  });

  it("SR-06: 空正文且无附件不发送（两端一致拒绝）", () => {
    const { result } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: false, isAgentProcessing: false },
    });
    act(() => result.current.setInput("   "));
    act(() => result.current.handleSend());
    expect(streamChat).not.toHaveBeenCalled();
    expect(useQueueStore.getState().entries).toHaveLength(0);
  });

  it("取消：移除 queued-local 后不再被 drain", () => {
    const { result, rerender } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: true, isAgentProcessing: true },
    });
    act(() => result.current.setInput("将被取消"));
    act(() => result.current.handleSend());
    const id = useQueueStore.getState().entries[0].clientMessageId;

    act(() => result.current.cancelQueued(id));
    expect(useQueueStore.getState().entries).toHaveLength(0);

    rerender({ agentId: "A", isStreaming: false, isAgentProcessing: false });
    expect(streamChat).not.toHaveBeenCalled();
  });

  it("重试：failed 普通消息翻回 queued-local 并被 drain 重新投递", () => {
    const { result, rerender } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: true, isAgentProcessing: true },
    });
    act(() => result.current.setInput("要重试的"));
    act(() => result.current.handleSend());
    const id = useQueueStore.getState().entries[0].clientMessageId;

    // 模拟之前发送失败留下的 failed 条目。
    act(() => {
      useQueueStore.setState({
        entries: [{ ...useQueueStore.getState().entries[0], state: "failed", errorMessage: "超时" }],
      });
    });

    act(() => result.current.retryQueued(id));
    expect(useQueueStore.getState().entries[0].state).toBe("queued-local");

    rerender({ agentId: "A", isStreaming: false, isAgentProcessing: false });
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamChat).mock.calls[0][1]).toBe("要重试的");
  });

  it("停止本轮：清掉该成员 queued-local；accepted 不动（不假装撤回已送达消息）", () => {
    const { result } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: true, isAgentProcessing: true },
    });
    act(() => result.current.setInput("本地暂存"));
    act(() => result.current.handleSend());
    act(() => {
      useQueueStore.getState().enqueue({
        projectId: "",
        agentId: "A",
        text: "已在服务器",
        attachments: [],
        mode: "normal",
        initialState: "accepted",
      });
    });

    act(() => result.current.handleStop());
    const states = useQueueStore.getState().entries.map((e) => e.state);
    expect(states).toEqual(["accepted"]);
  });

  it("T-12: 切走再切回后点停止，cancel 投到该 agent 当前存活 channel 且进入停止状态机", () => {
    hoisted.channels.push({ topic: "agent:A", push: vi.fn() });

    // 第一次实例：发送消息（注册 run）。
    const first = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: false, isAgentProcessing: false },
    });
    act(() => first.result.current.setInput("go"));
    act(() => first.result.current.handleSend());
    expect(streamChat).toHaveBeenCalledTimes(1);

    // 切走（卸载 → useAgentChannelLifecycle 清本地句柄）再切回（新实例）。
    first.unmount();
    const second = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: true, isAgentProcessing: true },
    });
    // 本地 streamAbortRef 已被生命周期清理；停止必须仍命中注册表条目。
    expect(second.result.current.streamAbortRef.current).toBeNull();

    act(() => second.result.current.handleStop());
    expect(hoisted.channels[0].push).toHaveBeenCalledWith("cancel", {});
    expect(getStopPhase("A")).toBe("stopping");

    // 后端确认（done 事件）→ 「已停止」；确认前不得提前宣布。
    expect(getStopPhase("A")).not.toBe("stopped");
    const onEvent = vi.mocked(streamChat).mock.calls[0][3];
    act(() => onEvent({ type: "done", data: "" }));
    act(() => {
      vi.advanceTimersByTime(800); // 最短确认驻留
    });
    expect(getStopPhase("A")).toBe("stopped");
  });

  it("停止超时未确认 → uncertain（不提前宣布成功），可再次点击重试", () => {
    hoisted.channels.push({ topic: "agent:A", push: vi.fn() });
    const { result } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: false, isAgentProcessing: false },
    });
    act(() => result.current.setInput("go"));
    act(() => result.current.handleSend());
    act(() => result.current.handleStop());
    expect(getStopPhase("A")).toBe("stopping");

    act(() => {
      vi.advanceTimersByTime(15_000);
    });
    expect(getStopPhase("A")).toBe("uncertain");

    // 重试：新的停止请求回到 stopping。
    act(() => result.current.handleStop());
    expect(getStopPhase("A")).toBe("stopping");
    expect(hoisted.channels[0].push).toHaveBeenCalledTimes(2);
    // 后端确认到达（processing 回读 / done 事件）→ 最短驻留后「已停止」。
    act(() => confirmStopped("A"));
    act(() => {
      vi.advanceTimersByTime(800);
    });
    expect(getStopPhase("A")).toBe("stopped");
  });

  it("SR-08: 插话先入列再清输入；未获确认 10s 转 failed 保留原文；重试成功转 accepted", async () => {
    const { result } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: true, isAgentProcessing: true },
    });

    act(() => result.current.setInput("插话内容"));
    act(() => result.current.setImages(["data:image/png;base64,IMG1"]));
    act(() => result.current.handleInsert());
    expect(pushInsert).toHaveBeenCalledWith("A", "插话内容", ["data:image/png;base64,IMG1"]);
    expect(result.current.input).toBe("");
    expect(result.current.images).toEqual([]);
    expect(useQueueStore.getState().entries[0]).toMatchObject({
      agentId: "A",
      mode: "interrupt",
      state: "sending",
    });

    // DB 始终没有该内容 → 10s 超时转 failed（文本保留在队列面板可重试）。
    await act(async () => {
      vi.advanceTimersByTime(10_000);
      await Promise.resolve();
    });
    expect(useQueueStore.getState().entries[0].state).toBe("failed");
    expect(useQueueStore.getState().entries[0].text).toBe("插话内容");

    // 重试：这次 DB 里有内容（后端已落库）→ accepted。
    vi.mocked(getChatMessages).mockClear();
    vi.mocked(getChatMessages).mockResolvedValue([
      { id: "u1", role: "user", content: "插话内容" },
    ] as never);
    const id = useQueueStore.getState().entries[0].clientMessageId;
    act(() => result.current.retryQueued(id));
    expect(pushInsert).toHaveBeenCalledTimes(2);
    await act(async () => {
      vi.advanceTimersByTime(300);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(useQueueStore.getState().entries[0].state).toBe("accepted");
  });

  it("保留旧契约：切到空闲乙时不 drain 甲的排队；甲的条目只等甲的会话", () => {
    const { result, rerender } = renderHook((p: HarnessProps) => useHarness(p), {
      initialProps: { agentId: "A", isStreaming: true, isAgentProcessing: true },
    });
    act(() => result.current.setInput("把团队扩散一下"));
    act(() => result.current.handleSend());
    expect(streamChat).not.toHaveBeenCalled();

    rerender({ agentId: "B", isStreaming: false, isAgentProcessing: false });
    expect(streamChat).not.toHaveBeenCalled();

    rerender({ agentId: "A", isStreaming: false, isAgentProcessing: false });
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamChat).mock.calls[0][0]).toBe("A");
  });
});

describe("useChatSend — round_start", () => {
  beforeEach(() => {
    vi.mocked(streamChat).mockClear();
    resetModules();
  });

  afterEach(() => {
    resetModules();
  });

  it("keeps prior narration/tool chips and appends a round_boundary segment", () => {
    let onEvent: (event: ChatEvent) => void = () => {};
    vi.mocked(streamChat).mockImplementation((_id, _msg, _imgs, cb) => {
      onEvent = cb;
      return { abort: vi.fn() };
    });

    let draft: StreamDraft | null = null;
    function useRoundHarness() {
      const activeAgentIdRef = useRef<string | null>("A");
      const streamDraftRef = useRef<StreamDraft | null>(null);
      const stickToBottomRef = useRef(true);
      const updateStreamDraft = (
        updater: StreamDraft | null | ((prev: StreamDraft | null) => StreamDraft | null)
      ) => {
        draft = typeof updater === "function" ? updater(draft) : updater;
        streamDraftRef.current = draft;
      };
      return useChatSend({
        agentId: "A",
        activeAgentIdRef,
        streamDraftRef,
        updateStreamDraft,
        isStreaming: false,
        setIsStreaming: () => {},
        isAgentProcessing: false,
        loadMessagesFromDb: vi.fn(async () => true),
        setMessages: () => {},
        refreshOrgTree: () => {},
        thinkingElapsed: null,
        setThinkingElapsed: () => {},
        stickToBottomRef,
      });
    }

    const { result } = renderHook(() => useRoundHarness());
    act(() => result.current.setInput("go"));
    act(() => result.current.handleSend());
    expect(streamChat).toHaveBeenCalledTimes(1);

    act(() => {
      draft = {
        assistantId: "m1",
        segments: [
          { type: "thinking", content: "plan" },
          { type: "text", content: "用户选了方案2" },
          { type: "tool_call", tool: { tool: "get_tasks", input: {} } },
        ],
      };
    });
    act(() => onEvent({ type: "round_start", data: "1" }));
    const finalDraft = draft as StreamDraft | null;
    // 契约（B6 渲染统一 + round 号解析补齐）：round_start 不丢弃前轮
    // 旁白/thinking，尾部插入 round_boundary 段——与被动路径
    // （useChatMessages）同款，用户发起的 turn live 阶段也有轮次分隔。
    expect(finalDraft?.segments).toEqual([
      { type: "thinking", content: "plan" },
      { type: "text", content: "用户选了方案2" },
      { type: "tool_call", tool: { tool: "get_tasks", input: {} } },
      { type: "round_boundary", round: 1 },
    ]);
  });
});

// TEST_DSH_44 Bug#1：一个 turn 有两次 done——streamer 收尾 done（早于
// handle_completion 落库 metadata.segments）+ completion 落库后的权威 done。
// 第一次到达时 DB 快照无 segments，直接清 draft 会把整轮结构化渲染坍缩成
// 平铺文本。契约：gate（settledMessageHasSegments）失败保留 persisted
// draft，权威 done / 带segments 快照落地后才清。
describe("useChatSend — done 收口 gate（TEST_DSH_44 Bug#1）", () => {
  const noSegmentsRow: ChatMessage = {
    id: "m1",
    role: "assistant",
    content: "中途旁白拼接平文本",
    timestamp: 1,
    isStreaming: true,
  };
  const withSegmentsRow: ChatMessage = {
    id: "m1",
    role: "assistant",
    content: "中途旁白拼接平文本",
    timestamp: 1,
    _segments: [
      { type: "text", content: "第一轮旁白" },
      { type: "round_boundary", round: 1 },
      { type: "text", content: "第二轮旁白" },
    ],
  };

  function mountGateHarness() {
    let onEvent: (event: ChatEvent) => void = () => {};
    vi.mocked(streamChat).mockImplementation((_id, _msg, _imgs, cb) => {
      onEvent = cb;
      return { abort: vi.fn() };
    });
    let draft: StreamDraft | null = null;
    function useHarness() {
      const activeAgentIdRef = useRef<string | null>("A");
      const streamDraftRef = useRef<StreamDraft | null>(null);
      const stickToBottomRef = useRef(true);
      const updateStreamDraft = (
        updater: StreamDraft | null | ((prev: StreamDraft | null) => StreamDraft | null)
      ) => {
        draft = typeof updater === "function" ? updater(draft) : updater;
        streamDraftRef.current = draft;
      };
      return useChatSend({
        agentId: "A",
        activeAgentIdRef,
        streamDraftRef,
        updateStreamDraft,
        isStreaming: false,
        setIsStreaming: () => {},
        isAgentProcessing: false,
        loadMessagesFromDb: vi.fn(async () => true),
        setMessages: () => {},
        refreshOrgTree: () => {},
        thinkingElapsed: null,
        setThinkingElapsed: () => {},
        stickToBottomRef,
      });
    }
    // 单次 mount；后续通过 result 驱动（多次 renderHook 会建多套闭包）
    const rendered = renderHook(() => useHarness());
    return {
      result: rendered.result,
      fire: (event: ChatEvent) => onEvent(event),
      getDraft: () => draft,
    };
  }

  function startTurn(h: ReturnType<typeof mountGateHarness>) {
    act(() => h.result.current.setInput("go"));
    act(() => h.result.current.handleSend());
    act(() =>
      h.fire({
        type: "message_id",
        data: JSON.stringify({ role: "assistant", id: "m1" }),
      })
    );
    act(() => h.fire({ type: "text_delta", data: "第一轮旁白" }));
    expect(h.getDraft()?.assistantId).toBe("m1");
  }

  beforeEach(() => {
    vi.useFakeTimers();
    useAppStore.getState().clearChatSessions();
    vi.mocked(streamChat).mockClear();
    resetModules();
  });

  afterEach(() => {
    vi.useRealTimers();
    useAppStore.getState().clearChatSessions();
    resetModules();
  });

  it("done 时快照无 segments → draft 保留 persisted 不清空（重试耗尽也不坍缩）", async () => {
    const h = mountGateHarness();
    startTurn(h);

    useAppStore.getState().setChatMessages("A", [noSegmentsRow]);
    act(() => h.fire({ type: "done", data: "" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    // done#1（streamer 收尾，segments 未落库）：draft 切 persisted 留屏
    expect(h.getDraft()).not.toBeNull();
    expect(h.getDraft()?.persisted).toBe(true);

    // 5×400ms 重试耗尽，快照始终无 segments → 不清 draft（旧实现此处坍缩）
    for (let i = 0; i < 5; i++) {
      await act(async () => {
        vi.advanceTimersByTime(400);
        await Promise.resolve();
        await Promise.resolve();
      });
    }
    expect(h.getDraft()).not.toBeNull();
    expect(h.getDraft()?.persisted).toBe(true);
  });

  it("权威快照带 segments（第二次 done）→ 正常 swap 清 draft 回归", async () => {
    const h = mountGateHarness();
    startTurn(h);

    useAppStore.getState().setChatMessages("A", [noSegmentsRow]);
    act(() => h.fire({ type: "done", data: "" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(h.getDraft()).not.toBeNull();

    // 权威 done：completion 已把 metadata.segments 落库
    useAppStore.getState().setChatMessages("A", [withSegmentsRow]);
    act(() => h.fire({ type: "done", data: "" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(h.getDraft()).toBeNull();
  });

  it("done 时快照已带 segments → 单次 done 即 swap（正常路径回归）", async () => {
    const h = mountGateHarness();
    startTurn(h);

    useAppStore.getState().setChatMessages("A", [withSegmentsRow]);
    act(() => h.fire({ type: "done", data: "" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(h.getDraft()).toBeNull();
  });

  it("swap 重试窗口跨新 turn：旧 swap 终态不翻转/不清新 turn 的 live draft（P2-1）", async () => {
    const h = mountGateHarness();
    startTurn(h); // message_id m1 + text_delta

    useAppStore.getState().setChatMessages("A", [noSegmentsRow]);
    act(() => h.fire({ type: "done", data: "" })); // done#1 → m1 persisted，重试排程
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(h.getDraft()?.assistantId).toBe("m1");
    expect(h.getDraft()?.persisted).toBe(true);

    // done#1 之后用户立即手发下一条：新 turn 的 message_id(m2) 重建 draft
    // （主动路径 message_id 无条件重建；id 守卫同时把 done 翻转的目标
    // 切到 m2 —— 旧 turn 的 swap 链仍闭包在 m1 上）
    act(() =>
      h.fire({ type: "message_id", data: JSON.stringify({ role: "assistant", id: "m2" }) })
    );
    act(() => h.fire({ type: "text_delta", data: "新 turn 旁白" }));
    expect(h.getDraft()?.assistantId).toBe("m2");
    expect(h.getDraft()?.persisted).toBeUndefined();

    // 旧 turn（m1）的 swap 重试耗尽 → 终态不得翻转/清空 m2 的 live draft
    for (let i = 0; i < 5; i++) {
      await act(async () => {
        vi.advanceTimersByTime(400);
        await Promise.resolve();
        await Promise.resolve();
      });
    }
    expect(h.getDraft()).not.toBeNull();
    expect(h.getDraft()?.assistantId).toBe("m2");
    expect(h.getDraft()?.persisted).toBeUndefined();
    // 新 turn 的流式内容未丢
    expect(
      h.getDraft()?.segments.some((s) => s.type === "text" && s.content === "新 turn 旁白")
    ).toBe(true);
  });
});
