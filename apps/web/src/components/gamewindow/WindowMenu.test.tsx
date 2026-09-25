/**
 * WindowMenu —— 已打开面板菜单 / 一键归位入口（FE-16 §8.5 规则 3/4）
 *
 * 锁住的交互契约：
 *   1. 触发按钮开/关弹层（点按钮关菜单不得被 click toggle 重新打开 —— 回归锁）；
 *   2. 弹层列出已开窗口（标题/最小化/固定状态），点击行 = 聚焦该窗；
 *   3. 一键归位只走 resetLayout（业务窗口不清、payload 保留）；
 *   4. 空态有说明文字，不是空白。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import WindowMenuControl from "./WindowMenu";
import { useGameWindowStore, type GameWindowState } from "./store";

function makeWin(patch: Partial<GameWindowState>): GameWindowState {
  return {
    id: "chat",
    kind: "chat",
    title: "聊天 · 甲",
    payload: { agentId: "agent-A" },
    geom: { x: 612, y: 94, w: 400, h: 662 },
    minimized: false,
    ...patch,
  };
}

beforeEach(() => {
  localStorage.clear();
  useGameWindowStore.setState({
    windows: [],
    focusSignal: {},
    pinnedAgent: {},
    focusModeId: null,
    preFocusGeom: {},
    layoutResetNonce: 0,
  });
});

describe("WindowMenu —— 窗口管理控件", () => {
  it("触发按钮开/关弹层；再点同一按钮能关掉（不被 pointerdown-外关 + click 重新打开）", () => {
    render(<WindowMenuControl />);
    const trigger = screen.getByRole("button", { name: /面板/ });

    act(() => {
      fireEvent.click(trigger);
    });
    expect(screen.getByRole("menu")).toBeTruthy();

    act(() => {
      fireEvent.click(trigger); // 关：pointerdown 在 wrapper 内不触发外关，click toggle 关闭
    });
    expect(screen.queryByRole("menu")).toBe(null);
  });

  it("列出已开窗口（标题 + 最小化/固定状态），点击行聚焦该窗并收起弹层", () => {
    useGameWindowStore.setState({
      windows: [
        makeWin({ id: "chat", kind: "chat", title: "聊天 · 甲", minimized: true }),
        makeWin({
          id: "agent",
          kind: "agent",
          title: "详情 · 乙",
          payload: { agentId: "agent-B" },
        }),
      ],
      pinnedAgent: { agent: "agent-B" },
    });
    render(<WindowMenuControl />);
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /面板/ }));
    });

    expect(screen.getByText("聊天 · 甲")).toBeTruthy();
    expect(screen.getByText("最小化")).toBeTruthy();
    expect(screen.getByText("已固定")).toBeTruthy();
    expect(screen.getByText("详情 · 乙")).toBeTruthy();

    act(() => {
      fireEvent.click(screen.getByText("聊天 · 甲"));
    });
    // 聚焦 = focusSignal 递增（最小化恢复置顶的既有契约），弹层收起
    expect(useGameWindowStore.getState().focusSignal.chat).toBe(1);
    expect(screen.queryByRole("menu")).toBe(null);
  });

  it("一键归位：窗口保留、几何重落预设（payload 原样），nonce 递增", () => {
    useGameWindowStore.setState({ windows: [makeWin({ geom: { x: 30, y: 30, w: 400, h: 662 } })] });
    render(<WindowMenuControl />);
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /面板/ }));
    });
    act(() => {
      fireEvent.click(screen.getByText("一键归位"));
    });

    const state = useGameWindowStore.getState();
    expect(state.windows).toHaveLength(1); // 只重置布局
    expect(state.windows[0].payload).toEqual({ agentId: "agent-A" });
    expect(state.windows[0].geom).toEqual({ x: 612, y: 94, w: 400, h: 662 });
    expect(state.layoutResetNonce).toBe(1);
    expect(screen.queryByRole("menu")).toBe(null);
  });

  it("无打开窗口时弹层有空态说明，归位/关闭全部入口仍可见", () => {
    render(<WindowMenuControl />);
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /面板/ }));
    });
    expect(screen.getByText("没有打开的面板")).toBeTruthy();
    expect(screen.getByText("一键归位")).toBeTruthy();
    expect(screen.getByText("关闭全部窗口")).toBeTruthy();
  });
});
