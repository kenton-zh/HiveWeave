/**
 * OfficeWorkspace —— 导航命令接线回归（FE-01 / FE-02 / FE-07；设计方案 §5.3/§9.2/§14.3）
 *
 * 锁住的接线（验收 T-01 / T-02 / T-03 / T-07）：
 *
 *   1. **选中成员 ⇒ 打开/聚焦其聊天窗**（点人的核心心智不变）
 *   2. **同一个人重复点击 = 聚焦/恢复**（T-01/T-02）—— 不再依赖
 *      selectedAgentId 变化（UX-01 根因：旧实现把开窗挂在 ID 变化上，
 *      「点甲→关聊天→再点甲」像失效）。旧测试用 null 中转绕过同 ID 问题，
 *      这里改为真实同 ID 重复动作（直接调用 OfficeView 点击时执行的
 *      同一条显式命令 —— 命令化的意义就是调用即生效）。
 *   3. **换人 = 同一单例窗换内容**（43d6256 单例契约：gameWindowId 恒为 kind）
 *   4. **详情窗跟随与固定**（T-03）：默认跟随选中；固定后选择他人不覆盖、
 *      标题明确显示「已固定」；解除后恢复跟随
 *   5. **统一任务打开**（T-07）：办公室形态点任务 ⇒ 任务窗打开且 payload 正确
 *
 * ⚠ 刻意 mock 掉 OfficeView（PixiJS，jsdom 跑不了 WebGL）与 GameWindowLayer
 * （会 new WinBox 操作真实 DOM）。本测试验的是**接线与命令语义**，不是渲染。
 * 最小化态由 WinBox DOM 持有，store 层的「恢复」契约 = 聚焦信号递增
 * （GameWindow 收到 focusNonce 后执行 wb.restore()+wb.focus()）。
 */
import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../store";
import { rememberAgentNames } from "../navigation/agentNames";
import {
  openAgentChat,
  openAgentDetailWindow,
  openTask,
  setAgentDetailPinned,
} from "../navigation/commands";
import { useGameWindowStore } from "./gamewindow/store";

vi.mock("./OfficeView", () => ({ default: () => null }));
vi.mock("./gamewindow/GameWindowLayer", () => ({ default: () => null }));

import OfficeWorkspace from "./OfficeWorkspace";

function gw() {
  return useGameWindowStore.getState();
}
function windows() {
  return useGameWindowStore.getState().windows;
}
function windowByKind(kind: string) {
  return useGameWindowStore.getState().windows.find((w) => w.kind === kind);
}

describe("OfficeWorkspace —— 统一导航命令（FE-01/FE-02/FE-07）", () => {
  beforeEach(() => {
    useGameWindowStore.setState({ windows: [], focusSignal: {}, pinnedAgent: {} });
    useAppStore.setState({ selectedAgentId: null, selectedProjectId: null, selectedTaskId: null });
    rememberAgentNames([
      { id: "agent-A", name: "甲" },
      { id: "agent-B", name: "乙" },
    ]);
  });

  // ── FE-01（T-01 / T-02） ────────────────────────────────────────

  it("T-01 选甲→关聊天→再点甲 ⇒ 聊天重开（同 ID 重复点击不再失效）", async () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    // 选甲（store action 驱动，与旧测试同款）⇒ effect 调命令开窗
    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-A" });
    });
    await waitFor(() => expect(windows()).toHaveLength(1));

    // 用户关掉聊天窗
    act(() => {
      useGameWindowStore.getState().closeKind("chat");
    });
    expect(windows()).toHaveLength(0);

    // 再点同一个人 —— OfficeView 场景点击执行的同一命令（同 ID 无状态变化可等）
    act(() => {
      openAgentChat("agent-A");
    });
    await waitFor(() => expect(windows()).toHaveLength(1));
    expect(windows()[0].kind).toBe("chat");
    expect(windows()[0].payload.agentId).toBe("agent-A");
  });

  it("T-02 最小化→再点甲 ⇒ 发恢复置顶信号（focusSignal 递增），窗口总数不变", async () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-A" });
    });
    await waitFor(() => expect(windows()).toHaveLength(1));
    const nonceBefore = gw().focusSignal["chat"] ?? 0;

    // 用户把窗最小化后再点同一个人：store 层无法表达 WinBox 的最小化态，
    // 恢复契约 = 重复命令必发聚焦信号（GameWindow 据此 restore+focus）
    act(() => {
      openAgentChat("agent-A");
    });

    await waitFor(() => expect(gw().focusSignal["chat"]).toBeGreaterThan(nonceBefore));
    expect(windows()).toHaveLength(1); // 不新开第二扇
    expect(windows()[0].payload.agentId).toBe("agent-A");
    expect(windows()[0].title).toContain("甲"); // 对象名进标题（§6.3）
  });

  it("T-02b 换人复用同一个 chat 窗（单例契约：窗口内换内容，不叠窗）", async () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-A" });
    });
    await waitFor(() => expect(windows()).toHaveLength(1));
    expect(windows()[0].payload.agentId).toBe("agent-A");

    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-B" });
    });
    // 关键断言：窗口**不增加**，而是同一个窗口的内容换成乙
    await waitFor(() => expect(windows()[0].payload.agentId).toBe("agent-B"));
    expect(windows()).toHaveLength(1);
    expect(windows()[0].kind).toBe("chat");
  });

  // ── FE-07（T-03） ───────────────────────────────────────────────

  it("T-03 详情固定乙→选甲 ⇒ 详情仍乙且有固定标记；解除→选甲 ⇒ 跟随甲", async () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    // 开乙的详情窗（HUD「详情」入口的命令）
    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-B" });
      openAgentDetailWindow("agent-B");
    });
    await waitFor(() => expect(windowByKind("agent")).toBeTruthy());
    expect(windowByKind("agent")!.payload.agentId).toBe("agent-B");

    // 固定乙 ⇒ pin 入 store，标题立即带固定标记
    act(() => {
      setAgentDetailPinned(true);
    });
    expect(gw().pinnedAgent.agent).toBe("agent-B");
    expect(windowByKind("agent")!.title).toContain("已固定");
    expect(windowByKind("agent")!.title).toContain("乙");

    // 选甲：聊天跟随甲，详情仍乙（payload 不被覆盖 = 不重挂载的 key 语义）
    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-A" });
    });
    await waitFor(() =>
      expect(windowByKind("chat")?.payload.agentId).toBe("agent-A"),
    );
    expect(windowByKind("agent")!.payload.agentId).toBe("agent-B");
    expect(windowByKind("agent")!.title).toContain("已固定");

    // 固定期 HUD「详情」也只聚焦，不静默换成甲
    act(() => {
      openAgentDetailWindow("agent-A");
    });
    expect(windowByKind("agent")!.payload.agentId).toBe("agent-B");

    // 解除固定 ⇒ 立即恢复跟随当前选中（甲），标题回到跟随态
    act(() => {
      setAgentDetailPinned(false);
    });
    await waitFor(() => expect(windowByKind("agent")!.payload.agentId).toBe("agent-A"));
    expect(windowByKind("agent")!.title).not.toContain("已固定");
    expect(gw().pinnedAgent.agent).toBeUndefined();
  });

  it("T-03b 详情未固定时默认跟随：开乙详情→选甲 ⇒ 详情跟随甲", async () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-B" });
      openAgentDetailWindow("agent-B");
    });
    await waitFor(() => expect(windowByKind("agent")).toBeTruthy());

    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-A" });
    });
    // 跟随 = 静默换内容（retarget）：不发聚焦信号（z 序不被打扰）
    await waitFor(() => expect(windowByKind("agent")!.payload.agentId).toBe("agent-A"));
    const nonce = gw().focusSignal["agent"] ?? 0;
    expect(nonce).toBe(1); // 只有 openAgentDetailWindow 那 1 次，跟随不追加
  });

  it("T-03c 详情窗关闭 ⇒ 固定一并清除（pin 不比窗口活得久）", async () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    act(() => {
      useAppStore.setState({ selectedAgentId: "agent-B" });
      openAgentDetailWindow("agent-B");
      setAgentDetailPinned(true);
    });
    expect(gw().pinnedAgent.agent).toBe("agent-B");

    act(() => {
      useGameWindowStore.getState().closeKind("agent");
    });
    expect(gw().pinnedAgent.agent).toBeUndefined();
  });

  // ── FE-02（T-07） ───────────────────────────────────────────────

  it("T-07 办公室模式点任务 ⇒ 任务窗打开且 payload 正确；换任务同窗换内容", async () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    // 时间线任务条 / 任务搜索点击时执行的统一命令
    act(() => {
      openTask("task-1");
    });
    await waitFor(() => expect(windowByKind("task")).toBeTruthy());
    expect(windowByKind("task")!.payload.taskId).toBe("task-1");
    expect(useAppStore.getState().selectedTaskId).toBe("task-1");

    // 再点另一个任务：同一单例窗换 payload，不叠第二扇
    act(() => {
      openTask("task-2");
    });
    await waitFor(() => expect(windowByKind("task")!.payload.taskId).toBe("task-2"));
    expect(windows().filter((w) => w.kind === "task")).toHaveLength(1);
    expect(useAppStore.getState().selectedTaskId).toBe("task-2");
  });

  it("T-07b openTask(null)（清除选中）不开任务窗、清空选中", () => {
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    act(() => {
      openTask(null);
    });
    expect(windowByKind("task")).toBeUndefined();
    expect(useAppStore.getState().selectedTaskId).toBe(null);
  });
});
