import { create } from "zustand";

/**
 * 模块级聊天发送队列 —— 唯一事实源（docs/前端美化与交互优化详细方案 §8.5 / §14.4）。
 *
 * 为什么不能放在 useChatSend 的 useRef：gamewindow/registry.tsx 以
 * `key=agentId` 挂 ChatPanel，切换会话即销毁重建组件，本地 ref 随之丢失
 * ——「甲排队 → 切乙 → 切回甲，队列全丢」就是这条生命周期契约不一致。
 * 本 store 活在模块作用域，随页面（而非组件实例）存活；ChatPanel 与
 * useChatSend 都只订阅这里，不持有第二份会漂移的队列状态。
 *
 * 内存级实现，不做 localStorage 落盘（§14.4：落盘方案由产品另行确认）。
 * 唯一的存储写入是一个 sessionStorage 布尔标记（不含任何消息内容），
 * 用于「页面刷新，未发送的排队消息已丢失」的一次性提示 —— 刷新丢失
 * 不做静默（§8.5 第 7 条）。
 */

export type DeliveryState = "queued-local" | "sending" | "accepted" | "failed";
export type QueueMode = "normal" | "interrupt";

/** 一条待发/已发消息的完整事务包（§14.4 PendingMessage）。附件是入列时刻的快照，绑定当条消息。 */
export interface PendingMessage {
  clientMessageId: string;
  projectId: string;
  agentId: string;
  text: string;
  /** 图片 data-URL 快照（入列瞬间拷贝），发送时随本条消息走，不回读输入框。 */
  attachments: string[];
  mode: QueueMode;
  state: DeliveryState;
  createdAt: number;
  errorMessage?: string;
}

export interface EnqueueInput {
  projectId: string;
  agentId: string;
  text: string;
  attachments: string[];
  mode: QueueMode;
  /** 入列初始态；插话路径直接以 sending 起步，普通排队默认 queued-local。 */
  initialState?: DeliveryState;
  errorMessage?: string;
}

/** accepted 只是「服务器已接收」的信息性记录，保留一小段时间供 UI 区分，随后自动清理。 */
const ACCEPTED_RETENTION_MS = 15_000;

const RELOAD_MARKER_KEY = "hw_send_queue_had_pending";

interface QueueState {
  /** 插入序 = 队列位置（§8.3：展示队列位置）。 */
  entries: PendingMessage[];
  enqueue: (input: EnqueueInput) => PendingMessage;
  markSending: (clientMessageId: string) => void;
  markAccepted: (clientMessageId: string) => void;
  markFailed: (clientMessageId: string, errorMessage: string) => void;
  /** failed → queued-local（重试普通排队消息：回到待发，由 drain 重新投递）。 */
  requeue: (clientMessageId: string) => void;
  /** 用户移除（仅本地暂存 = 取消；accepted = 仅移除记录，不撤回已送达消息）。 */
  remove: (clientMessageId: string) => void;
  /** 停止本轮时清掉该成员仍在本地暂存的排队（§14.4：accepted 不动，不假装撤回）。 */
  removeQueuedLocalsForAgent: (projectId: string, agentId: string) => void;
}

function makeClientMessageId(): string {
  return `qm-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function isPendingState(state: DeliveryState): boolean {
  return state !== "accepted";
}

/** sessionStorage 只放「是否存在未发消息」布尔标记，绝不写消息内容/base64（§8.6 隐私线）。 */
function writeReloadMarker(hasPending: boolean): void {
  try {
    if (hasPending) {
      sessionStorage.setItem(RELOAD_MARKER_KEY, "1");
    } else {
      sessionStorage.removeItem(RELOAD_MARKER_KEY);
    }
  } catch {
    /* 隐私模式等 storage 不可用 → 静默降级为无提示 */
  }
}

function readReloadMarker(): boolean {
  try {
    return sessionStorage.getItem(RELOAD_MARKER_KEY) === "1";
  } catch {
    return false;
  }
}

/** 每模块实例只消费一次（组件以 key=agentId 重挂会多次询问，不能反复弹）。 */
let reloadNoticeConsumed = false;

/**
 * 新会话启动时询问：上一个页面会话是否有未发完的排队消息。
 * true = 有（它们已随刷新丢失，本 store 是空的）——调用方应展示一次性提示。
 */
export function consumeReloadLossNotice(): boolean {
  if (reloadNoticeConsumed) return false;
  reloadNoticeConsumed = true;
  return readReloadMarker();
}

/** accepted 记录的自动清理计时器（clientMessageId → timer）。 */
const acceptedTimers = new Map<string, ReturnType<typeof setTimeout>>();

function clearAcceptedTimer(id: string): void {
  const t = acceptedTimers.get(id);
  if (t !== undefined) {
    clearTimeout(t);
    acceptedTimers.delete(id);
  }
}

export const useQueueStore = create<QueueState>((set, get) => {
  const syncReloadMarker = (entries: PendingMessage[]) => {
    writeReloadMarker(entries.some((e) => isPendingState(e.state)));
  };

  const patch = (clientMessageId: string, mutate: (e: PendingMessage) => PendingMessage) => {
    const entries = get().entries;
    const idx = entries.findIndex((e) => e.clientMessageId === clientMessageId);
    if (idx < 0) return;
    const next = entries.slice();
    next[idx] = mutate(next[idx]);
    set({ entries: next });
    syncReloadMarker(next);
  };

  return {
    entries: [],

    enqueue: (input) => {
      const entry: PendingMessage = {
        clientMessageId: makeClientMessageId(),
        projectId: input.projectId,
        agentId: input.agentId,
        text: input.text,
        attachments: [...input.attachments],
        mode: input.mode,
        state: input.initialState ?? "queued-local",
        createdAt: Date.now(),
        errorMessage: input.errorMessage,
      };
      const next = [...get().entries, entry];
      set({ entries: next });
      syncReloadMarker(next);
      return entry;
    },

    markSending: (id) => {
      clearAcceptedTimer(id);
      patch(id, (e) => (e.state === "queued-local" || e.state === "failed"
        ? { ...e, state: "sending", errorMessage: undefined }
        : e));
    },

    markAccepted: (id) => {
      const entry = get().entries.find((e) => e.clientMessageId === id);
      if (!entry || entry.state !== "sending") return;
      patch(id, (e) => ({ ...e, state: "accepted", errorMessage: undefined }));
      clearAcceptedTimer(id);
      acceptedTimers.set(
        id,
        setTimeout(() => {
          acceptedTimers.delete(id);
          get().remove(id);
        }, ACCEPTED_RETENTION_MS)
      );
    },

    markFailed: (id, errorMessage) => {
      clearAcceptedTimer(id);
      patch(id, (e) => (e.state === "sending" ? { ...e, state: "failed", errorMessage } : e));
    },

    requeue: (id) => {
      patch(id, (e) =>
        e.state === "failed"
          ? { ...e, state: "queued-local", errorMessage: undefined }
          : e
      );
    },

    remove: (id) => {
      clearAcceptedTimer(id);
      const next = get().entries.filter((e) => e.clientMessageId !== id);
      set({ entries: next });
      syncReloadMarker(next);
    },

    removeQueuedLocalsForAgent: (projectId, agentId) => {
      const next = get().entries.filter(
        (e) =>
          !(
            e.projectId === projectId &&
            e.agentId === agentId &&
            e.state === "queued-local"
          )
      );
      if (next.length === get().entries.length) return;
      set({ entries: next });
      syncReloadMarker(next);
    },
  };
});

// ── 纯选择器（组件里先订阅 entries 再 useMemo 过滤，避免选择器每次返回新数组）──
// projectId 统一归一化：null → ""（useChatSend 入列与查询共用同一键空间）。

export function normalizeProjectId(projectId: string | null | undefined): string {
  return projectId ?? "";
}

export function entriesForAgent(
  entries: PendingMessage[],
  projectId: string | null,
  agentId: string | null
): PendingMessage[] {
  if (!agentId) return [];
  const pid = normalizeProjectId(projectId);
  return entries.filter((e) => e.projectId === pid && e.agentId === agentId);
}

/** 「已排队 N 条」口径：仍在本地暂存、等待投递的条数（sending/failed/accepted 不计）。 */
export function countQueued(entries: PendingMessage[], projectId: string | null, agentId: string | null): number {
  return entriesForAgent(entries, projectId, agentId).filter((e) => e.state === "queued-local")
    .length;
}

export function hasQueuedForAgent(entries: PendingMessage[], projectId: string | null, agentId: string | null): boolean {
  return countQueued(entries, projectId, agentId) > 0;
}

/** 统一发送校验（SR-06）：合法消息 = 有正文 **或** 有附件（本产品支持纯图片，
 * 后端 deliver_user_message / insert 路径都接受空正文 + images）。发送按钮
 * 的 disabled 与发送函数共用这一个判据，两端一致，不再「按钮能点、函数拒发」。
 */
export function hasSendableContent(text: string, attachments: string[]): boolean {
  return text.trim().length > 0 || attachments.length > 0;
}

/** 命令式入列（useChatSend 非渲染路径用；React 组件请走 store 订阅）。 */
export function enqueuePending(input: EnqueueInput): PendingMessage {
  return useQueueStore.getState().enqueue(input);
}

/**
 * 取出该成员队列中最早的一条 queued-local 消息并置为 sending（原子：取出即
 * 占位，drain 与手动发送共用，同一消息不会被投两次）。没有可发条目返回 undefined。
 */
export function takeNextQueued(
  projectId: string,
  agentId: string
): PendingMessage | undefined {
  const entries = useQueueStore.getState().entries;
  const entry = entries.find(
    (e) =>
      e.projectId === projectId &&
      e.agentId === agentId &&
      e.state === "queued-local"
  );
  if (!entry) return undefined;
  useQueueStore.getState().markSending(entry.clientMessageId);
  return { ...entry, state: "sending" };
}

/** 测试专用：清空全部队列状态与标记（模块是全局单例，用例之间必须复位）。 */
export function __resetQueueStoreForTests(): void {
  for (const id of [...acceptedTimers.keys()]) clearAcceptedTimer(id);
  useQueueStore.setState({ entries: [] });
  reloadNoticeConsumed = false;
  try {
    sessionStorage.removeItem(RELOAD_MARKER_KEY);
  } catch {
    /* ignore */
  }
}
