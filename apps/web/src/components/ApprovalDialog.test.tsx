/**
 * ApprovalDialog 呈现与安全契约（FE-08 / 方案 §11.3「审批要看得懂在批准什么」）
 *
 * 锁四条最容易回退的语义：
 *   1. §11.3 最小字段集全部呈现：发起成员、请求操作（工具名）、说明、
 *      影响范围（参数摘要）、批准/拒绝按钮语义明确（仅本次生效 vs 本次不执行）；
 *   2. 畸形条目（FE-08 归一层标记 malformed）→「数据异常」占位可见，
 *      且不渲染批准/拒绝按钮（不可误批），好行不受影响；
 *   3. 处理成功必须明确确认（res.ok）才移除条目；not-ok / 抛错 ⇒ 保留可重试；
 *   4. 提交在途 ⇒ 全部批准/拒绝按钮禁用（防重复提交；后端已幂等兜底）。
 *
 * api 层整体 mock；归一层本身（snake_case → camelCase）的单测见
 * src/api/approvals.test.ts —— 本文件消费的是归一后的 DTO。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({
  getPendingApprovals: vi.fn(),
  respondToApproval: vi.fn(),
}));

import { getPendingApprovals, respondToApproval } from "../api";
import ApprovalDialog from "./ApprovalDialog";
import { useAppStore } from "../store";

// rest.ts 归一层输出的 PendingApproval 形状（真实后端行归一后的结果）
const OK_APPROVAL = {
  id: "req-1",
  agentId: "agent-93d33bb76df6",
  toolName: "bash",
  toolArguments: '{"command": "Remove-Item -Recurse ./build"}',
  description: "清理构建产物（契约测试样例）",
  status: "pending",
  createdAt: 1727200000000,
  malformed: false,
  missingFields: [],
};

const MALFORMED = {
  id: "malformed-1",
  agentId: "agent-93d33bb76df6",
  toolName: "",
  toolArguments: "{}",
  description: "",
  status: "pending",
  createdAt: 0,
  malformed: true,
  missingFields: ["tool_name"],
};

const AGENT_ID = "agent-93d33bb76df6";

function renderDialog() {
  return render(<ApprovalDialog agentId={AGENT_ID} onClose={vi.fn()} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  useAppStore.setState({ pendingApprovals: {} });
});

describe("ApprovalDialog —— §11.3 呈现最小集", () => {
  it("呈现发起成员 / 请求操作 / 说明 / 影响范围（参数），按钮语义明确", async () => {
    vi.mocked(getPendingApprovals).mockResolvedValue([OK_APPROVAL] as never);
    renderDialog();

    // 发起成员（载荷里的 agentId，不是对话框 prop 的摆设）
    expect(await screen.findByText(AGENT_ID)).toBeInTheDocument();
    // 请求操作（工具名）
    expect(screen.getByText("bash")).toBeInTheDocument();
    // 说明（为什么需要权限）
    expect(screen.getByText(/清理构建产物/)).toBeInTheDocument();
    // 影响范围（参数摘要）
    expect(screen.getByText(/Remove-Item -Recurse \.\/build/)).toBeInTheDocument();
    expect(screen.getByText("影响范围（参数）")).toBeInTheDocument();
    // 批准/拒绝按钮语义明确
    expect(
      screen.getByRole("button", { name: "批准（仅本次）" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "拒绝（本次不执行）" }),
    ).toBeInTheDocument();
  });
});

describe("ApprovalDialog —— 畸形条目可见且不可误批（FE-08）", () => {
  it("malformed 条目渲染数据异常占位并列缺失字段，好行照常可操作", async () => {
    vi.mocked(getPendingApprovals).mockResolvedValue([
      OK_APPROVAL,
      MALFORMED,
    ] as never);
    renderDialog();

    expect(await screen.findByTestId("approval-malformed")).toBeInTheDocument();
    expect(screen.getByText(/数据异常/)).toBeInTheDocument();
    // 缺失字段名可见（后端源字段名）
    expect(screen.getByText(/tool_name/)).toBeInTheDocument();
    // 只有好行有一对操作按钮，畸形行没有
    expect(
      screen.getAllByRole("button", { name: "批准（仅本次）" }),
    ).toHaveLength(1);
    expect(
      screen.getAllByRole("button", { name: "拒绝（本次不执行）" }),
    ).toHaveLength(1);
  });

  it("全部畸形 ⇒ 无任何批准/拒绝按钮（不把异常当待办渲染成可操作卡片）", async () => {
    vi.mocked(getPendingApprovals).mockResolvedValue([MALFORMED] as never);
    renderDialog();

    expect(await screen.findByTestId("approval-malformed")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "批准（仅本次）" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "拒绝（本次不执行）" }),
    ).not.toBeInTheDocument();
  });
});

describe("ApprovalDialog —— 处理成功明确确认 + 重复提交防护", () => {
  it("批准成功（ok:true）：调用 respond 且条目从列表移除", async () => {
    vi.mocked(getPendingApprovals).mockResolvedValue([OK_APPROVAL] as never);
    vi.mocked(respondToApproval).mockResolvedValue({
      ok: true,
      status: "resolved",
    } as never);
    renderDialog();

    fireEvent.click(
      await screen.findByRole("button", { name: "批准（仅本次）" }),
    );

    await waitFor(() =>
      expect(respondToApproval).toHaveBeenCalledWith(
        "req-1",
        true,
        false,
        undefined,
      ),
    );
    await waitFor(() =>
      expect(screen.queryByText("bash")).not.toBeInTheDocument(),
    );
  });

  it("respond 抛错：条目保留（可重试），不静默消失", async () => {
    vi.mocked(getPendingApprovals).mockResolvedValue([OK_APPROVAL] as never);
    vi.mocked(respondToApproval).mockRejectedValueOnce(new Error("HTTP 500"));
    renderDialog();

    fireEvent.click(
      await screen.findByRole("button", { name: "批准（仅本次）" }),
    );

    await waitFor(() => expect(respondToApproval).toHaveBeenCalled());
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "批准（仅本次）" }),
      ).not.toBeDisabled(),
    );
    expect(screen.getByText("bash")).toBeInTheDocument();
  });

  it("respond 返回 ok:false：条目保留（处理未确认成功）", async () => {
    vi.mocked(getPendingApprovals).mockResolvedValue([OK_APPROVAL] as never);
    vi.mocked(respondToApproval).mockResolvedValue({ ok: false } as never);
    renderDialog();

    fireEvent.click(
      await screen.findByRole("button", { name: "拒绝（本次不执行）" }),
    );

    await waitFor(() => expect(respondToApproval).toHaveBeenCalled());
    expect(screen.getByText("bash")).toBeInTheDocument();
  });

  it("提交在途：批准/拒绝按钮全部禁用（防重复提交）", async () => {
    vi.mocked(getPendingApprovals).mockResolvedValue([OK_APPROVAL] as never);
    let resolveRespond!: (v: Awaited<ReturnType<typeof respondToApproval>>) => void;
    vi.mocked(respondToApproval).mockImplementation(
      () =>
        new Promise<Awaited<ReturnType<typeof respondToApproval>>>((res) => {
          resolveRespond = res;
        }),
    );
    renderDialog();

    fireEvent.click(
      await screen.findByRole("button", { name: "批准（仅本次）" }),
    );

    // 在途时两个动作按钮都变为「处理中...」且全部禁用（防重复提交）
    await waitFor(() => {
      const busy = screen.getAllByRole("button", { name: "处理中..." });
      expect(busy).toHaveLength(2);
      busy.forEach((b) => expect(b).toBeDisabled());
    });

    resolveRespond({ ok: true, status: "resolved" });
  });
});
