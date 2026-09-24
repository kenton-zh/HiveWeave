import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  __resetQueueStoreForTests,
  consumeReloadLossNotice,
  countQueued,
  enqueuePending,
  hasQueuedForAgent,
  hasSendableContent,
  takeNextQueued,
  useQueueStore,
} from "./queueStore";

const A = { projectId: "p1", agentId: "A" } as const;

describe("queueStore — 模块级消息事务队列（§14.4）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    __resetQueueStoreForTests();
  });

  afterEach(() => {
    vi.useRealTimers();
    __resetQueueStoreForTests();
  });

  it("enqueue 生成完整消息包：id/项目/成员/附件快照/初始态/时间戳", () => {
    const attachments = ["img1"];
    const e = enqueuePending({ ...A, text: "hi", attachments, mode: "normal" });
    expect(e.clientMessageId).toMatch(/^qm-/);
    expect(e.projectId).toBe("p1");
    expect(e.agentId).toBe("A");
    expect(e.state).toBe("queued-local");
    expect(e.createdAt).toBeGreaterThan(0);
    // SR-07：附件是入列时刻的快照，源数组后续变化不影响包内附件。
    attachments.push("img2");
    expect(useQueueStore.getState().entries[0].attachments).toEqual(["img1"]);
  });

  it("projectId+agentId 键控：按项目+成员过滤，互不串", () => {
    enqueuePending({ ...A, text: "x", attachments: [], mode: "normal" });
    enqueuePending({ projectId: "p1", agentId: "B", text: "y", attachments: [], mode: "normal" });
    enqueuePending({ projectId: "p2", agentId: "A", text: "z", attachments: [], mode: "normal" });
    const entries = useQueueStore.getState().entries;
    expect(countQueued(entries, "p1", "A")).toBe(1);
    expect(countQueued(entries, "p1", "B")).toBe(1);
    expect(countQueued(entries, "p2", "A")).toBe(1);
    expect(countQueued(entries, "p2", "B")).toBe(0);
    expect(hasQueuedForAgent(entries, "p1", "A")).toBe(true);
    expect(hasQueuedForAgent(entries, "p9", "Z")).toBe(false);
  });

  it("takeNextQueued 原子出队：最早 queued-local 置 sending；不重复投递、不跨成员取", () => {
    enqueuePending({ ...A, text: "1", attachments: [], mode: "normal" });
    enqueuePending({ ...A, text: "2", attachments: [], mode: "normal" });
    enqueuePending({ projectId: "p1", agentId: "B", text: "B1", attachments: [], mode: "normal" });

    const first = takeNextQueued("p1", "A");
    expect(first?.text).toBe("1");
    expect(first?.state).toBe("sending");
    expect(useQueueStore.getState().entries[0].state).toBe("sending");

    // sending 已被占位，不会再次取出；顺序保持 FIFO。
    const second = takeNextQueued("p1", "A");
    expect(second?.text).toBe("2");
    expect(takeNextQueued("p1", "A")).toBeUndefined();
    expect(takeNextQueued("p1", "C")).toBeUndefined();
    // B 的条目仍在
    expect(takeNextQueued("p1", "B")?.text).toBe("B1");
  });

  it("状态机守卫：accepted/failed 只从 sending 进入；requeue 只对 failed 生效", () => {
    const e = enqueuePending({ ...A, text: "m", attachments: [], mode: "normal" });
    const store = useQueueStore.getState();
    store.markAccepted(e.clientMessageId);
    expect(useQueueStore.getState().entries[0].state).toBe("queued-local");
    store.markFailed(e.clientMessageId, "x");
    expect(useQueueStore.getState().entries[0].state).toBe("queued-local");

    store.markSending(e.clientMessageId);
    store.markFailed(e.clientMessageId, "超时");
    expect(useQueueStore.getState().entries[0].state).toBe("failed");
    expect(useQueueStore.getState().entries[0].errorMessage).toBe("超时");
    store.markAccepted(e.clientMessageId); // failed 不回 accepted
    expect(useQueueStore.getState().entries[0].state).toBe("failed");

    store.requeue(e.clientMessageId);
    expect(useQueueStore.getState().entries[0].state).toBe("queued-local");
    expect(useQueueStore.getState().entries[0].errorMessage).toBeUndefined();
  });

  it("accepted 是信息性记录：15s 后自动清理；插话条目同样适用", () => {
    const e = enqueuePending({ ...A, text: "m", attachments: [], mode: "normal" });
    const store = useQueueStore.getState();
    store.markSending(e.clientMessageId);
    store.markAccepted(e.clientMessageId);
    expect(useQueueStore.getState().entries).toHaveLength(1);
    vi.advanceTimersByTime(15_000);
    expect(useQueueStore.getState().entries).toHaveLength(0);
  });

  it("removeQueuedLocalsForAgent 只清该成员本地暂存；accepted/sending 保留", () => {
    enqueuePending({ ...A, text: "local", attachments: [], mode: "normal" });
    const acc = enqueuePending({ ...A, text: "acc", attachments: [], mode: "normal" });
    useQueueStore.getState().markSending(acc.clientMessageId);
    useQueueStore.getState().markAccepted(acc.clientMessageId);
    enqueuePending({ projectId: "p1", agentId: "B", text: "other", attachments: [], mode: "normal" });

    useQueueStore.getState().removeQueuedLocalsForAgent("p1", "A");
    const left = useQueueStore.getState().entries;
    expect(left).toHaveLength(2);
    expect(left.map((e) => e.text).sort()).toEqual(["acc", "other"]);
  });

  it("刷新丢失提示：入列置位、清空复位；提示一次性（不随重挂反复弹）", () => {
    // 上一个页面会话入列（置位 storage 标记）。
    enqueuePending({ ...A, text: "m", attachments: [], mode: "normal" });
    // 刷新后的新模块实例首次询问 → 提示一次。
    expect(consumeReloadLossNotice()).toBe(true);
    // 同实例（含组件重挂）不再重复提示。
    expect(consumeReloadLossNotice()).toBe(false);

    // 队列清空 → storage 标记复位；复位消费标记后（模拟又一次刷新）应为 false。
    useQueueStore.getState().remove(useQueueStore.getState().entries[0].clientMessageId);
    __resetQueueStoreForTests();
    expect(consumeReloadLossNotice()).toBe(false);

    // 再复位消费标记（模拟又一次刷新）→ 新会话入列后应再次收到提示。
    __resetQueueStoreForTests();
    enqueuePending({ ...A, text: "m2", attachments: [], mode: "normal" });
    expect(consumeReloadLossNotice()).toBe(true);
  });

  it("hasSendableContent：正文或附件其一即合法；纯空白不算正文", () => {
    expect(hasSendableContent("", [])).toBe(false);
    expect(hasSendableContent("   ", [])).toBe(false);
    expect(hasSendableContent("hi", [])).toBe(true);
    expect(hasSendableContent("", ["img"])).toBe(true);
    expect(hasSendableContent("  ", ["img"])).toBe(true);
  });
});
