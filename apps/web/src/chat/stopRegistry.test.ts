import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  __resetStopRegistryForTests,
  confirmStopped,
  getStopPhase,
  notifyRunEnded,
  pushCancelToLiveChannel,
  registerRun,
  requestStop,
  unregisterRun,
} from "./stopRegistry";

const hoisted = vi.hoisted(() => ({
  /** 模拟 phoenix Socket.channels —— cancel 的投递面。 */
  channels: [] as Array<{ topic: string; push: ReturnType<typeof vi.fn> }>,
}));

vi.mock("../api", () => ({
  getSocket: vi.fn(() => ({ channels: hoisted.channels })),
}));

function liveChannel(topic: string) {
  const ch = { topic, push: vi.fn() };
  hoisted.channels.push(ch);
  return ch;
}

describe("stopRegistry — 模块级停止注册表与状态机（§14.5 / T-12）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    __resetStopRegistryForTests();
    hoisted.channels.length = 0;
  });

  afterEach(() => {
    vi.useRealTimers();
    __resetStopRegistryForTests();
  });

  it("requestStop：进入 stopping 并把 cancel 投到该 agent 当前存活 channel", () => {
    const ch = liveChannel("agent:A");
    expect(getStopPhase("A")).toBe("none");
    requestStop("A");
    expect(getStopPhase("A")).toBe("stopping");
    expect(ch.push).toHaveBeenCalledWith("cancel", {});
  });

  it("停止按 agentId 定位：切走再切回（新实例）后 requestStop 仍命中同一 channel", () => {
    const ch = liveChannel("agent:A");
    const runId = registerRun("A", () => {});
    // 模拟切走再切回：注册表条目不随组件销毁（仍可注销/停止）。
    requestStop("A");
    expect(ch.push).toHaveBeenCalledWith("cancel", {});
    // run 收口注销只作用于自己的 runId。
    unregisterRun("A", "run-其他轮次");
    notifyRunEnded("A", runId);
    expect(ch.push).toHaveBeenCalledTimes(1);
  });

  it("channel 未 join（切走后）投递失败也不崩，状态仍进 stopping 可重试", () => {
    expect(pushCancelToLiveChannel("ghost")).toBe(false);
    requestStop("ghost");
    expect(getStopPhase("ghost")).toBe("stopping");
  });

  it("确认前 UI 不得显示已停止；超时未确认 → uncertain；确认后 → stopped", () => {
    liveChannel("agent:A");
    requestStop("A");
    expect(getStopPhase("A")).toBe("stopping");
    expect(getStopPhase("A")).not.toBe("stopped");

    vi.advanceTimersByTime(15_000);
    expect(getStopPhase("A")).toBe("uncertain");

    confirmStopped("A");
    vi.advanceTimersByTime(800); // 最短确认驻留
    expect(getStopPhase("A")).toBe("stopped");
  });

  it("notifyRunEnded：后端 done/error 收口即确认停止；无停止在途时不产生 stopped", () => {
    liveChannel("agent:A");
    const runId = registerRun("A", () => {});
    requestStop("A");
    notifyRunEnded("A", runId);
    vi.advanceTimersByTime(800);
    expect(getStopPhase("A")).toBe("stopped");

    // 正常跑完的 turn（无停止在途）不留 stopped 状态。
    __resetStopRegistryForTests();
    registerRun("B", () => {});
    notifyRunEnded("B");
    vi.advanceTimersByTime(1_000);
    expect(getStopPhase("B")).toBe("none");
  });

  it("confirmStopped 过早调用受最短驻留保护：不瞬间宣布成功", () => {
    liveChannel("agent:A");
    requestStop("A");
    confirmStopped("A"); // 点击后立即收到 processing=false 读数
    expect(getStopPhase("A")).toBe("stopping");
    vi.advanceTimersByTime(799);
    expect(getStopPhase("A")).toBe("stopping");
    vi.advanceTimersByTime(1);
    expect(getStopPhase("A")).toBe("stopped");
  });

  it("stopped 展示 6s 后自动回落 none（按钮回空闲态）", () => {
    liveChannel("agent:A");
    requestStop("A");
    confirmStopped("A");
    vi.advanceTimersByTime(800);
    expect(getStopPhase("A")).toBe("stopped");
    vi.advanceTimersByTime(6_000);
    expect(getStopPhase("A")).toBe("none");
  });

  it("新 run 登记（下一轮开始）清除上一轮的 stopped 展示", () => {
    liveChannel("agent:A");
    requestStop("A");
    confirmStopped("A");
    vi.advanceTimersByTime(800);
    expect(getStopPhase("A")).toBe("stopped");
    registerRun("A", () => {});
    expect(getStopPhase("A")).toBe("none");
  });

  it("重试（再次 requestStop）刷新 requestId 并回到 stopping", () => {
    liveChannel("agent:A");
    requestStop("A");
    vi.advanceTimersByTime(15_000);
    expect(getStopPhase("A")).toBe("uncertain");
    const second = requestStop("A");
    expect(getStopPhase("A")).toBe("stopping");
    expect(second).toMatch(/^stp-/);
    // 旧的超时计时器不得再把新请求打回 uncertain。
    vi.advanceTimersByTime(15_000);
    expect(getStopPhase("A")).toBe("uncertain");
  });
});
