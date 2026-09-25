/**
 * FE-14「待我处理」聚合面板测试（设计方案 §11.1 / §15.3）
 *
 * 锁住的语义：
 *   1. 四组数据源 → 分组聚合 / 组计数 / 组内按等待起点升序 / 徽标总数；
 *   2. 点成员 ⇒ openAgentChat(agentId)；点任务 ⇒ openTask(taskId)；
 *      待授权「去审批」⇒ 复用 ApprovalDialog（面板内可批准/拒绝）；
 *   3. 空态（全量空 + 未选项目）与错误态区分：单组失败只标记该组
 *      （部分成功逐项显示），重试成功后清除 —— 错误不伪装成空列表；
 *   4. HUD 徽标 = 未处理总数（OfficeWorkspace 集成），面板开合切换。
 *
 * api 层整体 mock；navigation/commands mock 后断言调用参数。
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../api", () => ({
  getQuestions: vi.fn(),
  getProjectPendingApprovals: vi.fn(),
  listTasks: vi.fn(),
  getUserPings: vi.fn(),
  getProjectAlarms: vi.fn(),
}));
vi.mock("../../navigation/commands", () => ({
  openAgentChat: vi.fn(),
  openTask: vi.fn(),
  followAgentDetail: vi.fn(),
  openAgentDetailWindow: vi.fn(),
  setOfficeSurfaceActive: vi.fn(),
}));
vi.mock("../ApprovalDialog", () => ({
  default: ({ agentId, onClose }: { agentId: string; onClose: () => void }) => (
    <div>
      <span data-testid={`approval-dialog-stub-${agentId}`} />
      <button data-testid="approval-dialog-stub-close" onClick={onClose}>
        关闭审批弹窗
      </button>
    </div>
  ),
}));
// OfficeWorkspace 集成用例需要：PixiJS 场景与 WinBox 窗口层都进不了 jsdom
vi.mock("../OfficeView", () => ({ default: () => null }));
vi.mock("../gamewindow/GameWindowLayer", () => ({ default: () => null }));

import {
  getProjectAlarms,
  getProjectPendingApprovals,
  getQuestions,
  getUserPings,
  listTasks,
} from "../../api";
import { openAgentChat, openTask } from "../../navigation/commands";
import { rememberAgentNames } from "../../navigation/agentNames";
import OfficeWorkspace from "../OfficeWorkspace";
import { useAppStore } from "../../store";
import PendingPanel from "./PendingPanel";

const NOW = Date.now();

const Q_OLD = {
  id: "q-old",
  agentId: "agent-a",
  question: "老问题：用 A 还是 B？",
  status: "pending" as const,
  createdAt: NOW - 10 * 60_000,
};
const Q_NEW = {
  id: "q-new",
  agentId: "agent-b",
  question: "新问题：要不要发版？",
  status: "pending" as const,
  createdAt: NOW - 60_000,
};
const APPROVAL = {
  id: "ap-1",
  agentId: "agent-a",
  toolName: "hiveweave__bash_shell",
  toolArguments: "{}",
  description: "需要运行测试命令",
  status: "pending",
  createdAt: NOW - 2 * 60_000,
};
const TASK_OLDER = {
  id: "task-2",
  title: "任务乙（验收中）",
  status: "verifying",
  assignee_id: "agent-a",
  submitted_at: NOW - 50 * 60_000,
  updated_at: NOW - 50 * 60_000,
};
const TASK_NEWER = {
  id: "task-1",
  title: "任务甲（验收中）",
  status: "verifying",
  assignee_id: "agent-b",
  submitted_at: NOW - 30 * 60_000,
  updated_at: NOW - 30 * 60_000,
};
const PING = {
  id: "ping-1",
  agentId: "agent-c",
  content: "阻塞：等上游接口",
  timestamp: NOW - 45_000,
};
const ALARM = {
  id: "alarm-1",
  toAgentId: "agent-a",
  purpose: "检查构建结果",
  fireAtGameSeconds: 4600,
  fired: false,
  createdAt: NOW,
};

function mockSources() {
  vi.mocked(getQuestions).mockResolvedValue([Q_OLD, Q_NEW] as never);
  vi.mocked(getProjectPendingApprovals).mockResolvedValue([APPROVAL] as never);
  // 故意按新→旧返回，验证面板排序与后端返回顺序无关（最久未处理在前）
  vi.mocked(listTasks).mockResolvedValue([TASK_NEWER, TASK_OLDER] as never);
  vi.mocked(getUserPings).mockResolvedValue([PING] as never);
  vi.mocked(getProjectAlarms).mockResolvedValue({
    alarms: [ALARM],
    currentGameSeconds: 1000,
    realTimestamp: NOW,
  } as never);
}

describe("PendingPanel —— 待我处理聚合（FE-14）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      selectedProjectId: "p-1",
      questionVersion: 0,
      agentHealth: {},
      projects: [{ id: "p-1", name: "演示项目", createdAt: 0 }],
      toasts: [],
    });
    rememberAgentNames([
      { id: "agent-a", name: "甲" },
      { id: "agent-b", name: "乙" },
      { id: "agent-c", name: "丙" },
    ]);
  });

  it("四组聚合：分组呈现、组计数正确、组内最久未处理在前", async () => {
    mockSources();
    const onTotalChange = vi.fn();
    render(<PendingPanel open onClose={() => {}} onTotalChange={onTotalChange} />);

    expect(await screen.findByText("老问题：用 A 还是 B？")).toBeInTheDocument();

    // 组计数：2 问题 + 1 授权 + 2 验收 + 2 需关注（1 ping + 1 定时唤醒）
    expect(screen.getByTestId("pending-count-questions")).toHaveTextContent("2");
    expect(screen.getByTestId("pending-count-approvals")).toHaveTextContent("1");
    expect(screen.getByTestId("pending-count-acceptance")).toHaveTextContent("2");
    expect(screen.getByTestId("pending-count-attention")).toHaveTextContent("2");
    expect(screen.getByTestId("pending-filter-all")).toHaveTextContent("7");
    expect(onTotalChange).toHaveBeenCalledWith(7);

    // 组内排序：验收组后端故意返回新→旧，面板按等待起点升序 ⇒ 乙（50min）在甲（30min）前
    const acceptance = screen.getByTestId("pending-group-acceptance");
    const text = acceptance.textContent ?? "";
    expect(text.indexOf("任务乙（验收中）")).toBeLessThan(text.indexOf("任务甲（验收中）"));

    // 需关注组：ping 内容 + 定时唤醒（游戏时间 4600-1000=3600s ⇒ 约 1 小时）
    const attention = screen.getByTestId("pending-group-attention");
    expect(attention.textContent).toContain("阻塞：等上游接口");
    expect(attention.textContent).toContain("游戏时间约 1 小时后唤醒");
    // 定时唤醒无「已等待」语义 ⇒ 不显示等待时长
    expect(attention.textContent).toContain("进行中");

    // 待授权条目：成员 + 工具语义（描述为摘要）
    const approvals = screen.getByTestId("pending-group-approvals");
    expect(approvals.textContent).toContain("甲");
    expect(approvals.textContent).toContain("需要运行测试命令");
  });

  it("筛选：点「待验收」只显示该组，计数仍在筛选项上", async () => {
    mockSources();
    render(<PendingPanel open onClose={() => {}} />);

    expect(await screen.findByText("任务乙（验收中）")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("pending-filter-acceptance"));

    expect(screen.getByTestId("pending-group-acceptance")).toBeInTheDocument();
    expect(screen.queryByTestId("pending-group-questions")).not.toBeInTheDocument();
    expect(screen.queryByTestId("pending-group-approvals")).not.toBeInTheDocument();
    // 全部切回
    fireEvent.click(screen.getByTestId("pending-filter-all"));
    expect(screen.getByTestId("pending-group-questions")).toBeInTheDocument();
  });

  it("点成员 ⇒ openAgentChat(agentId)；点任务 ⇒ openTask(taskId)", async () => {
    mockSources();
    render(<PendingPanel open onClose={() => {}} />);

    expect(await screen.findByText("老问题：用 A 还是 B？")).toBeInTheDocument();

    // 点「甲」（待回答 / 待授权条目的成员芯片）
    const actorButtons = screen.getAllByTestId("pending-item-actor");
    const jia = actorButtons.find((b) => b.textContent === "甲");
    expect(jia).toBeTruthy();
    fireEvent.click(jia!);
    expect(openAgentChat).toHaveBeenCalledWith("agent-a");

    // 点验收组第一条「查看任务」⇒ 该组排序首条 = task-2
    const acceptance = screen.getByTestId("pending-group-acceptance");
    fireEvent.click(within(acceptance).getAllByTestId("pending-item-task")[0]);
    expect(openTask).toHaveBeenCalledWith("task-2");
  });

  it("待授权「去审批」⇒ 复用 ApprovalDialog（面板内可处理）；关闭后保持面板", async () => {
    mockSources();
    render(<PendingPanel open onClose={() => {}} />);

    expect(await screen.findByText("需要运行测试命令")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("pending-item-approve"));
    expect(screen.getByTestId("approval-dialog-stub-agent-a")).toBeInTheDocument();

    // 关闭审批弹窗 ⇒ 弹窗收起、面板保持
    fireEvent.click(screen.getByTestId("approval-dialog-stub-close"));
    await waitFor(() =>
      expect(screen.queryByTestId("approval-dialog-stub-agent-a")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("pending-panel")).toBeInTheDocument();
  });

  it("空态：四组全空 ⇒ 全量空态（有解释、可下一步），不是每组空白行", async () => {
    vi.mocked(getQuestions).mockResolvedValue([] as never);
    vi.mocked(getProjectPendingApprovals).mockResolvedValue([] as never);
    vi.mocked(listTasks).mockResolvedValue([] as never);
    vi.mocked(getUserPings).mockResolvedValue([] as never);
    vi.mocked(getProjectAlarms).mockResolvedValue({ alarms: [], currentGameSeconds: 0, realTimestamp: NOW } as never);

    render(<PendingPanel open onClose={() => {}} />);
    expect(await screen.findByTestId("empty-state")).toHaveTextContent("没有等待你处理的事项");
    expect(screen.queryAllByTestId("pending-item")).toHaveLength(0);
  });

  it("未选项目 ⇒ 项目口径空态；错误态：单组失败只标记该组且可重试恢复", async () => {
    // ① 未选项目：不发起任何拉取
    useAppStore.setState({ selectedProjectId: null });
    const first = render(<PendingPanel open onClose={() => {}} />);
    expect(await first.findByTestId("empty-state")).toHaveTextContent("未选择项目");
    expect(getQuestions).not.toHaveBeenCalled();
    first.unmount();

    // ② 单组失败（问题组 500）：其余组照常展示，失败组有错误 + 重试
    useAppStore.setState({ selectedProjectId: "p-1" });
    mockSources();
    vi.mocked(getQuestions).mockRejectedValue(new Error("HTTP 500"));
    const second = render(<PendingPanel open onClose={() => {}} />);

    expect(await second.findByTestId("pending-retry-questions")).toBeInTheDocument();
    expect(screen.getByTestId("pending-group-questions").textContent).toContain("加载失败");
    // 部分成功：其余组照常渲染（不被一个失败拖垮）
    expect(screen.getByTestId("pending-group-approvals").textContent).toContain("需要运行测试命令");

    // ③ 重试成功 ⇒ 错误清除、条目出现
    vi.mocked(getQuestions).mockResolvedValue([Q_OLD] as never);
    fireEvent.click(screen.getByTestId("pending-retry-questions"));
    expect(await screen.findByText("老问题：用 A 还是 B？")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByTestId("pending-retry-questions")).not.toBeInTheDocument(),
    );
    second.unmount();
  });

  it("需关注组聚合 store 运行错误（agent_health），且忽略其他项目的错误", async () => {
    mockSources();
    useAppStore.setState({
      agentHealth: {
        "agent-d": { health: "error", message: "LLM 连续 429", at: NOW - 60_000, projectId: "p-1" },
        "agent-x": { health: "error", message: "旧项目错误", at: NOW - 60_000, projectId: "p-other" },
      },
    });
    render(<PendingPanel open onClose={() => {}} />);

    const attention = await screen.findByTestId("pending-group-attention");
    expect(attention.textContent).toContain("成员运行出错");
    expect(attention.textContent).toContain("LLM 连续 429");
    expect(attention.textContent).not.toContain("旧项目错误");
    // 1 ping + 1 唤醒 + 1 运行错误
    expect(screen.getByTestId("pending-count-attention")).toHaveTextContent("3");
  });

  it("HUD 集成：徽标 = 未处理总数；按钮开合面板", async () => {
    mockSources();
    render(<OfficeWorkspace onExitToWorkbench={() => {}} />);

    // 徽标出现且计数 = 全部未处理（2+1+2+2）
    const badge = await screen.findByTestId("pending-hud-badge");
    expect(badge).toHaveTextContent("7");

    // 开：面板出现；关：面板收起（纯 UI，徽标保留）
    fireEvent.click(screen.getByTitle("查看所有等待你处理的事项（提问 / 授权 / 验收 / 提醒）"));
    expect(screen.getByTestId("pending-panel")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("pending-panel-close"));
    await waitFor(() => expect(screen.queryByTestId("pending-panel")).not.toBeInTheDocument());
    expect(screen.getByTestId("pending-hud-badge")).toBeInTheDocument();
  });
});
