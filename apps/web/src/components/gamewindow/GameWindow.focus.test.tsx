/**
 * GameWindow —— FE-16 专注模式的窗层行为（设计规格 §8.5）
 *
 * 用真实 WinBox（jsdom 可挂载，见组件测试底注）：锁的是**窗口壳**的契约，
 * 不是面板内容 ——
 *   1. 专注进出只改几何，**内容不重挂载**（§8.5 规则 4：本地态不丢）；
 *   2. 标题栏「专注」按钮存在且可切换（图标钮有可读名称，§8.7）；
 *   3. Esc 退出专注 —— 仅本窗专注时监听（§8.5 / §8.2 Esc 只关层）；
 *   4. 最小化态回写 store（菜单显示用），且最小化几何不写入正常尺寸
 *      （MINIMIZED_GEOM_MAX 判据，§6.3）。
 */
import { fireEvent, render } from "@testing-library/react";
import { act } from "react";
import { useEffect } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import GameWindow from "./GameWindow";
import { useGameWindowStore, type GameWindowState } from "./store";

function makeWin(patch: Partial<GameWindowState> = {}): GameWindowState {
  return {
    id: "chat",
    kind: "chat",
    title: "聊天 · 甲",
    payload: { agentId: "agent-A" },
    geom: { x: 612, y: 94, w: 400, h: 662 }, // 1024×768 视口下的停靠位
    minimized: false,
    ...patch,
  };
}

function resetStore() {
  useGameWindowStore.setState({
    windows: [],
    focusSignal: {},
    pinnedAgent: {},
    focusModeId: null,
    preFocusGeom: {},
    layoutResetNonce: 0,
  });
}

beforeEach(() => {
  localStorage.clear();
  resetStore();
  document.body.innerHTML = "";
});

// 内容不重挂载的探针：挂载一次计数一次
let mountCount = 0;
function Probe() {
  useEffect(() => {
    mountCount += 1;
  }, []);
  return <div data-testid="probe">probe</div>;
}

describe("GameWindow —— FE-16 专注模式", () => {
  it("进出专注内容不重挂载（同一窗口改几何，key/实例稳定）", () => {
    mountCount = 0;
    const win = makeWin();
    useGameWindowStore.setState({ windows: [win] });
    const { getByTestId, unmount } = render(
      <GameWindow win={win}>
        <Probe />
      </GameWindow>,
    );
    expect(getByTestId("probe")).toBeTruthy();
    expect(mountCount).toBe(1);

    act(() => {
      useGameWindowStore.getState().enterFocus("chat");
    });
    act(() => {
      useGameWindowStore.getState().exitFocus("chat");
    });
    // 专注期间 payload 换人（单例 retarget）也不重挂载窗口壳
    act(() => {
      useGameWindowStore.getState().enterFocus("chat");
    });
    expect(mountCount).toBe(1); // 只在首次渲染挂载过一次
    expect(getByTestId("probe")).toBeTruthy();
    unmount();
  });

  it("标题栏专注按钮：存在、有可读名称、点击切换专注态", () => {
    const win = makeWin();
    useGameWindowStore.setState({ windows: [win] });
    const { unmount } = render(
      <GameWindow win={win}>
        <div>body</div>
      </GameWindow>,
    );

    const ctrl = document.querySelector(".hw-win-focus-ctrl") as HTMLElement;
    expect(ctrl).toBeTruthy();
    expect(ctrl.getAttribute("aria-label")).toContain("专注"); // §8.7 图标钮可读名称

    act(() => {
      fireEvent.click(ctrl);
    });
    expect(useGameWindowStore.getState().focusModeId).toBe("chat");
    expect(ctrl.getAttribute("aria-pressed")).toBe("true");

    act(() => {
      fireEvent.click(ctrl);
    });
    expect(useGameWindowStore.getState().focusModeId).toBe(null);
    expect(ctrl.getAttribute("aria-pressed")).toBe("false");
    unmount();
  });

  it("Esc 退出专注；非专注时 Esc 不关窗也不产生副作用", () => {
    const win = makeWin();
    useGameWindowStore.setState({ windows: [win] });
    const { unmount } = render(
      <GameWindow win={win}>
        <div>body</div>
      </GameWindow>,
    );

    // 非专注态：Esc 无操作
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(useGameWindowStore.getState().focusModeId).toBe(null);
    expect(useGameWindowStore.getState().windows).toHaveLength(1); // 不误关窗口

    // 专注态：Esc 退出专注（窗口保留，几何由 exitFocus 恢复）
    act(() => {
      useGameWindowStore.getState().enterFocus("chat");
    });
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(useGameWindowStore.getState().focusModeId).toBe(null);
    expect(useGameWindowStore.getState().windows).toHaveLength(1);
    unmount();
  });

  it("焦点在已打开面板菜单（role=menu）内时 Esc 只关菜单，不连带走退出专注", () => {
    const win = makeWin();
    useGameWindowStore.setState({ windows: [win] });
    const { unmount } = render(
      <GameWindow win={win}>
        <div>body</div>
      </GameWindow>,
    );
    act(() => {
      useGameWindowStore.getState().enterFocus("chat");
    });

    // 模拟菜单内元素的 Escape（target 落在 role="menu" 内）——聚焦态必须保留
    const menuTarget = document.createElement("button");
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    menu.appendChild(menuTarget);
    document.body.appendChild(menu);
    act(() => {
      menuTarget.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(useGameWindowStore.getState().focusModeId).toBe("chat");

    // 焦点回到窗外 ⇒ 同样的 Escape 正常退出专注
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(useGameWindowStore.getState().focusModeId).toBe(null);
    menu.remove();
    unmount();
  });

  it("最小化态回写 store；最小化几何（底部细条）不写入正常几何", () => {
    const win = makeWin();
    useGameWindowStore.setState({ windows: [win] });
    const { unmount } = render(
      <GameWindow win={win}>
        <div>body</div>
      </GameWindow>,
    );

    const before = useGameWindowStore.getState().windows[0].geom;
    const minBtn = document.querySelector(".wb-min") as HTMLElement;
    expect(minBtn).toBeTruthy();

    act(() => {
      fireEvent.click(minBtn); // winbox minimize：resize(h=header)+move 带 skip，仍触发回调
    });
    expect(useGameWindowStore.getState().windows[0].minimized).toBe(true);
    const duringMin = useGameWindowStore.getState().windows[0].geom;
    expect(duringMin).toEqual(before); // 底部细条几何（h≈36）未进 store（§6.3）

    act(() => {
      fireEvent.click(minBtn); // 再点 = restore
    });
    expect(useGameWindowStore.getState().windows[0].minimized).toBe(false);
    expect(useGameWindowStore.getState().windows[0].geom).toEqual(before);
    unmount();
  });

  it("最小化状态下进入专注 ⇒ 聚焦信号先恢复再占满（exit 后回到进入前几何）", () => {
    const win = makeWin();
    useGameWindowStore.setState({ windows: [win] });
    const { unmount } = render(
      <GameWindow win={win}>
        <div>body</div>
      </GameWindow>,
    );

    const minBtn = document.querySelector(".wb-min") as HTMLElement;
    act(() => {
      fireEvent.click(minBtn);
    });
    expect(useGameWindowStore.getState().windows[0].minimized).toBe(true);

    act(() => {
      useGameWindowStore.getState().enterFocus("chat");
    });
    expect(useGameWindowStore.getState().windows[0].minimized).toBe(false); // 已被拉回
    expect(useGameWindowStore.getState().windows[0].geom.w).toBe(1000); // 占满 1024−2×12

    act(() => {
      useGameWindowStore.getState().exitFocus("chat");
    });
    expect(useGameWindowStore.getState().windows[0].geom).toEqual(win.geom);
    unmount();
  });
});
