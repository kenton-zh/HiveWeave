import { create } from "zustand";
import { getSocket } from "../api";

/**
 * 模块级停止注册表 + 停止状态机（docs/前端美化与交互优化详细方案 §8.7 / §14.5）。
 *
 * 为什么不能依赖 useChatSend 里的发送句柄：ChatPanel 以 key=agentId 重挂，
 * useAgentChannelLifecycle 在切换会话时清空 streamAbortRef（防 TEST6 陈旧
 * cancel）——切走再切回后点「停止」，本地句柄已经是 null，要么停不掉要么
 * 不确定停的是哪个 run。注册表活在模块作用域，键是 agentId：停止请求永远
 * 带 agentId 定位，不依赖「当前页面是不是最初发起流的页面」。
 *
 * 取消投递方式：phoenix.js 的 Socket.channels 数组里按 topic 找该 agent 的
 * **当前存活** channel，直接 push("cancel")。不调用注册的旧 abort 闭包 ——
 * 它闭包捕获的是发送时刻的 channel（切走后已被 leave，push 无效），且
 * abort() 会删掉 ws 层唯一的 handler 槽位，误杀切回后的被动订阅。
 *
 * 「已停止」只来自后端侧确认（done/error 事件、或权威 processing 状态回读），
 * 超时未确认转 uncertain + 可重试，绝不提前宣布成功。
 */

export type StopPhase = "none" | "stopping" | "uncertain" | "stopped";

export interface StopState {
  phase: StopPhase;
  /** 本次停止请求 id（§14.5：请求 ID 用于去重和追踪）。 */
  requestId: string;
  requestedAt: number;
}

interface RegisteredRun {
  runId: string;
  abort: () => void;
  registeredAt: number;
}

export const STOP_CONFIRM_TIMEOUT_MS = 15_000;
/** 确认的最短驻留：点下停止到后端真实收口存在事件往返，过早的“不在处理中”
 *  读数可能是竞态，不允许瞬间宣布成功。 */
export const MIN_STOP_CONFIRM_MS = 800;
/** 「已停止」确认提示的停留时长，之后回落 none（按钮回到空闲态）。 */
const STOPPED_DISPLAY_MS = 6_000;

/** agentId → 当前（最新一次）可停止的 run。随会话/run 存亡，不随用户切换清除。 */
const runRegistry = new Map<string, RegisteredRun>();
const timers = new Map<string, { timeout?: ReturnType<typeof setTimeout>; reset?: ReturnType<typeof setTimeout>; confirm?: ReturnType<typeof setTimeout> }>();

interface StopStoreState {
  states: Record<string, StopState>;
  apply: (agentId: string, state: StopState | undefined) => void;
}

const useStopStore = create<StopStoreState>((set) => ({
  states: {},
  apply: (agentId, state) =>
    set((prev) => {
      const next = { ...prev.states };
      if (state === undefined) delete next[agentId];
      else next[agentId] = state;
      return { states: next };
    }),
}));

function clearTimers(agentId: string, which: Array<"timeout" | "reset" | "confirm">): void {
  const entry = timers.get(agentId);
  if (!entry) return;
  for (const w of which) {
    const t = entry[w];
    if (t !== undefined) {
      clearTimeout(t);
      entry[w] = undefined;
    }
  }
}

function makeRequestId(): string {
  return `stp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** 在 ws 层当前存活的 agent channel 上直接投递 cancel（唯一可信投递面）。 */
export function pushCancelToLiveChannel(agentId: string): boolean {
  try {
    const socket = getSocket() as unknown as { channels?: Array<{ topic?: string; push?: (event: string, payload: object) => void }> };
    const channels = socket?.channels;
    if (!Array.isArray(channels)) return false;
    const channel = channels.find((c) => c?.topic === `agent:${agentId}`);
    if (!channel?.push) return false;
    channel.push("cancel", {});
    return true;
  } catch {
    return false;
  }
}

/** turn 开始时登记（useChatSend 发起 streamChat 前调用）。返回 runId。 */
export function registerRun(agentId: string, abort: () => void): string {
  const runId = `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  runRegistry.set(agentId, { runId, abort, registeredAt: Date.now() });
  // 新 turn 启动 → 清掉上一轮的「已停止」展示（stopping/uncertain 不动：
  // 那属于上一轮，由确认/超时路径收口）。
  const cur = useStopStore.getState().states[agentId];
  if (cur?.phase === "stopped") {
    clearTimers(agentId, ["reset"]);
    useStopStore.getState().apply(agentId, undefined);
  }
  return runId;
}

/**
 * run 结束时注销；给出 runId 且与登记不符（该 agent 已被更新一轮接管）时不动，
 * 只清理调用方自己登记的那一轮。runId 为 null/undefined = 直接收口最新登记。
 */
export function unregisterRun(agentId: string, runId?: string | null): void {
  const entry = runRegistry.get(agentId);
  if (!entry) return;
  if (runId && entry.runId !== runId) return;
  runRegistry.delete(agentId);
}

/**
 * 点击停止：进入「正在停止」，向该 agent 当前存活 channel 投递 cancel。
 * 不检查注册表里是否有条目 —— 后台触发/恢复的 turn 未必有前端登记，
 * 只要用户能点到停止就按 agentId 投递（§8.7 返回后台会话后仍可停止）。
 */
export function requestStop(agentId: string, timeoutMs: number = STOP_CONFIRM_TIMEOUT_MS): string {
  const requestId = makeRequestId();
  clearTimers(agentId, ["timeout", "confirm"]);
  useStopStore.getState().apply(agentId, {
    phase: "stopping",
    requestId,
    requestedAt: Date.now(),
  });
  const t = setTimeout(() => {
    const cur = useStopStore.getState().states[agentId];
    if (cur?.phase !== "stopping" || cur.requestId !== requestId) return;
    useStopStore.getState().apply(agentId, { ...cur, phase: "uncertain" });
  }, timeoutMs);
  const entry = timers.get(agentId) ?? {};
  entry.timeout = t;
  timers.set(agentId, entry);
  pushCancelToLiveChannel(agentId);
  return requestId;
}

/**
 * 后端侧确认到达（done/error 事件、或 processing 权威回读翻负）。
 * 仅当处于 stopping/uncertain 才翻转 —— 正常跑完的 turn 不该显示「已停止」。
 */
export function confirmStopped(agentId: string): void {
  const cur = useStopStore.getState().states[agentId];
  if (!cur || (cur.phase !== "stopping" && cur.phase !== "uncertain")) return;
  clearTimers(agentId, ["timeout", "confirm"]);
  const elapsed = Date.now() - cur.requestedAt;
  const doConfirm = () => {
    const latest = useStopStore.getState().states[agentId];
    if (!latest || (latest.phase !== "stopping" && latest.phase !== "uncertain")) return;
    useStopStore.getState().apply(agentId, { ...latest, phase: "stopped" });
    clearTimers(agentId, ["reset"]);
    const reset = setTimeout(() => {
      const s = useStopStore.getState().states[agentId];
      if (s?.phase === "stopped") useStopStore.getState().apply(agentId, undefined);
    }, STOPPED_DISPLAY_MS);
    const entry = timers.get(agentId) ?? {};
    entry.reset = reset;
    timers.set(agentId, entry);
  };
  if (elapsed < MIN_STOP_CONFIRM_MS) {
    const t = setTimeout(doConfirm, MIN_STOP_CONFIRM_MS - elapsed);
    const entry = timers.get(agentId) ?? {};
    entry.confirm = t;
    timers.set(agentId, entry);
  } else {
    doConfirm();
  }
}

/**
 * run 收口（done/error/busy）统一出口：注销注册表条目 + 若有停止在途则确认。
 */
export function notifyRunEnded(agentId: string, runId?: string | null): void {
  unregisterRun(agentId, runId);
  confirmStopped(agentId);
}

/** 外部权威信号（如 store.processingAgents 回读）声明该 agent 已不在运行。 */
export function isStopPending(agentId: string): boolean {
  const phase = useStopStore.getState().states[agentId]?.phase;
  return phase === "stopping" || phase === "uncertain";
}

/** React 订阅：该 agent 的停止阶段（原始选择器返回 string，引用稳定）。 */
export function useStopPhase(agentId: string | null): StopPhase {
  return useStopStore((s) => (agentId ? s.states[agentId]?.phase ?? "none" : "none"));
}

/** 非渲染路径读取（测试/命令式判断）。 */
export function getStopPhase(agentId: string): StopPhase {
  return useStopStore.getState().states[agentId]?.phase ?? "none";
}

/** 测试专用：清空注册表、计时器与状态（模块是全局单例）。 */
export function __resetStopRegistryForTests(): void {
  for (const [agentId, entry] of timers) {
    for (const t of Object.values(entry)) {
      if (t !== undefined) clearTimeout(t);
    }
    timers.delete(agentId);
  }
  runRegistry.clear();
  useStopStore.setState({ states: {} });
}
