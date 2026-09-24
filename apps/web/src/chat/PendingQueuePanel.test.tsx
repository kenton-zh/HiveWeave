import { describe, it, expect, vi, beforeEach } from "vitest";
import { act } from "react";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { PendingQueuePanel } from "./PendingQueuePanel";
import { __resetQueueStoreForTests, enqueuePending, useQueueStore } from "./queueStore";

describe("PendingQueuePanel — 待发队列呈现（§8.3/§8.5）", () => {
  beforeEach(() => {
    __resetQueueStoreForTests();
  });

  it("队列为空时不渲染", () => {
    const { container } = render(
      <PendingQueuePanel projectId="p1" agentId="A" agentName="甲" onRetry={vi.fn()} onCancel={vi.fn()} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("渲染内容+接收人+状态；queued-local 与 accepted 视觉区分；插话有标记；他人条目不渲染", () => {
    enqueuePending({ projectId: "p1", agentId: "A", text: "第一条", attachments: [], mode: "normal" });
    const acc = enqueuePending({ projectId: "p1", agentId: "A", text: "已在服务器", attachments: [], mode: "normal" });
    useQueueStore.getState().markSending(acc.clientMessageId);
    useQueueStore.getState().markAccepted(acc.clientMessageId);
    enqueuePending({
      projectId: "p1",
      agentId: "A",
      text: "插话优先",
      attachments: [],
      mode: "interrupt",
      initialState: "sending",
    });
    // 不属于该成员/项目的条目不渲染
    enqueuePending({ projectId: "p1", agentId: "B", text: "别人的", attachments: [], mode: "normal" });

    render(
      <PendingQueuePanel projectId="p1" agentId="A" agentName="甲" onRetry={vi.fn()} onCancel={vi.fn()} />
    );
    expect(screen.getAllByTestId("queue-item")).toHaveLength(3);
    expect(screen.getByText("第一条")).toBeInTheDocument();
    expect(screen.getByText("已在服务器")).toBeInTheDocument();
    expect(screen.getByText("插话优先")).toBeInTheDocument();
    expect(screen.getAllByText("→ 甲")).toHaveLength(3);
    expect(screen.getByText("仅本地暂存")).toBeInTheDocument();
    expect(screen.getByText("服务器已接收")).toBeInTheDocument();
    expect(screen.getByText("发送中")).toBeInTheDocument();
    expect(screen.getByText("插话")).toBeInTheDocument();
    expect(screen.queryByText("别人的")).not.toBeInTheDocument();
  });

  it("纯图片条目显示图片占位文案", () => {
    enqueuePending({ projectId: "p1", agentId: "A", text: "", attachments: ["img1", "img2"], mode: "normal" });
    render(
      <PendingQueuePanel projectId="p1" agentId="A" agentName="甲" onRetry={vi.fn()} onCancel={vi.fn()} />
    );
    expect(screen.getByText("（图片 ×2）")).toBeInTheDocument();
  });

  it("动作：queued-local 可取消、failed 可重试+移除、accepted 仅移除记录、sending 无动作", () => {
    const onRetry = vi.fn();
    const onCancel = vi.fn();
    const queued = enqueuePending({ projectId: "p1", agentId: "A", text: "待发", attachments: [], mode: "normal" });
    const failed = enqueuePending({
      projectId: "p1",
      agentId: "A",
      text: "失败的插话",
      attachments: [],
      mode: "interrupt",
      initialState: "sending",
    });
    useQueueStore.getState().markFailed(failed.clientMessageId, "未获服务器确认");
    const accepted = enqueuePending({ projectId: "p1", agentId: "A", text: "已送达", attachments: [], mode: "normal" });
    useQueueStore.getState().markSending(accepted.clientMessageId);
    useQueueStore.getState().markAccepted(accepted.clientMessageId);
    const sending = enqueuePending({
      projectId: "p1",
      agentId: "A",
      text: "发送中条目",
      attachments: [],
      mode: "interrupt",
      initialState: "sending",
    });

    render(
      <PendingQueuePanel projectId="p1" agentId="A" agentName="甲" onRetry={onRetry} onCancel={onCancel} />
    );
    const rows = screen.getAllByTestId("queue-item");
    expect(rows).toHaveLength(4);

    // queued-local：取消
    fireEvent.click(within(rows[0]).getByTestId("queue-cancel"));
    expect(onCancel).toHaveBeenCalledWith(queued.clientMessageId);
    expect(within(rows[0]).queryByTestId("queue-retry")).not.toBeInTheDocument();

    // failed：重试 + 移除，错误信息可见
    expect(screen.getByText("未获服务器确认")).toBeInTheDocument();
    fireEvent.click(within(rows[1]).getByTestId("queue-retry"));
    expect(onRetry).toHaveBeenCalledWith(failed.clientMessageId);
    fireEvent.click(within(rows[1]).getByTestId("queue-cancel"));
    expect(onCancel).toHaveBeenCalledWith(failed.clientMessageId);

    // accepted：只有「移除记录」（不撤回语义），无重试
    expect(within(rows[2]).getByTestId("queue-cancel")).toBeInTheDocument();
    expect(within(rows[2]).queryByTestId("queue-retry")).not.toBeInTheDocument();

    // sending：无任何动作
    expect(within(rows[3]).queryByTestId("queue-cancel")).not.toBeInTheDocument();
    expect(within(rows[3]).queryByTestId("queue-retry")).not.toBeInTheDocument();
    expect(sending.state).toBe("sending");
  });

  it("重试后 failed 翻回 queued-local（配合 useChatSend.requeue 契约的视图层验证）", () => {
    const e = enqueuePending({ projectId: "p1", agentId: "A", text: "待重试", attachments: [], mode: "normal" });
    useQueueStore.getState().markSending(e.clientMessageId);
    useQueueStore.getState().markFailed(e.clientMessageId, "超时");
    render(
      <PendingQueuePanel projectId="p1" agentId="A" agentName="甲" onRetry={vi.fn()} onCancel={vi.fn()} />
    );
    expect(screen.getByText("发送失败")).toBeInTheDocument();
    // 视图订阅 store：外部 requeue 后面板即时反映（React 更新需 act 包裹）。
    act(() => {
      useQueueStore.getState().requeue(e.clientMessageId);
    });
    expect(screen.getByText("仅本地暂存")).toBeInTheDocument();
    expect(screen.queryByText("发送失败")).not.toBeInTheDocument();
  });
});
