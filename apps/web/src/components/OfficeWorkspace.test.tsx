/**
 * OfficeWorkspace —— 办公室主界面的接线回归（2026-09-22）
 *
 * 背景：ADR-011 要求「办公室升唯一主界面，功能面板变游戏窗口」。本测试锁住
 * 两条最容易在重构中被弄断的接线：
 *
 *   1. **选中 agent ⇒ 自动打开该 agent 的聊天窗**（用户明确要求的核心交互：
 *      「点击某个人就能弹出和那个人的聊天面板」）
 *   2. **重复选中同一 agent 不重复开窗**，只发聚焦信号（否则每点一次多一个窗）
 *
 * ⚠ 刻意 mock 掉 OfficeView（PixiJS，jsdom 跑不了 WebGL）与 GameWindowLayer
 * （会 new WinBox 操作真实 DOM）。本测试验的是**接线**，不是渲染 —— 窗口渲染
 * 由 `gamewindow/` 自身的逻辑负责。
 */
import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../store";
import { useGameWindowStore } from "./gamewindow/store";

vi.mock("./OfficeView", () => ({ default: () => null }));
vi.mock("./gamewindow/GameWindowLayer", () => ({ default: () => null }));

import OfficeWorkspace from "./OfficeWorkspace";

function windows() {
  return useGameWindowStore.getState().windows;
}

describe("OfficeWorkspace —— 选中 agent 自动开聊天窗", () => {
  beforeEach(() => {
    useGameWindowStore.setState({ windows: [], focusSignal: {} });
    useAppStore.setState({ selectedAgentId: null, selectedProjectId: null, selectedTaskId: null });
  });

  it("selectedAgentId 变化时打开该 agent 的 chat 窗口", async () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);
    expect(windows()).toHaveLength(0);

    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-42" });
    });

    await waitFor(() => {
      expect(windows()).toHaveLength(1);
    });
    expect(windows()[0].kind).toBe("chat");
    expect(windows()[0].payload.agentId).toBe("agent-42");
  });

  it("重复选中同一 agent 不重复开窗（只发聚焦信号）", async () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-42" });
    });
    await waitFor(() => expect(windows()).toHaveLength(1));
    const nonceAfterFirst = useGameWindowStore.getState().focusSignal["chat:agent-42"];

    // 再点同一个人：应当只 bump 聚焦信号，窗口数不变
    act(() => {
      useAppStore.setState({ selectedAgentId: null });
    });
    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-42" });
    });

    await waitFor(() => {
      expect(useGameWindowStore.getState().focusSignal["chat:agent-42"]).toBeGreaterThan(
        nonceAfterFirst,
      );
    });
    expect(windows()).toHaveLength(1);
  });

  it("切换到另一个 agent 会开出第二个 chat 窗（互不干扰）", async () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-A" });
    });
    await waitFor(() => expect(windows()).toHaveLength(1));

    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-B" });
    });
    await waitFor(() => expect(windows()).toHaveLength(2));

    const ids = windows().map((w) => w.payload.agentId).sort();
    expect(ids).toEqual(["agent-A", "agent-B"]);
  });
});
