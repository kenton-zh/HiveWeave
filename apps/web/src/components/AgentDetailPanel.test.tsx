/**
 * AgentDetailPanel —— 编辑草稿与刷新隔离（FE-09，设计文档 §7.3）
 *
 * 锁住的行为：
 *  T-14  编辑中·已修改的字段在后台刷新回填时不被服务器值覆盖；未脏字段正常更新
 *  T-15  成员 A 的详情请求迟到（切到 B 后才返回）⇒ 整体丢弃，不覆盖 B 的资料
 *  保存语义：保存中禁重复提交；成功更新权威数据并退出编辑；
 *           失败留在编辑态、输入保留、可重试；取消恢复服务器值不自动提交
 *  服务器数据冲突：提示「此内容已被更新」并提供「加载最新内容」
 *  切换成员有未保存修改：弹确认（继续编辑=取消切换；放弃并切换=丢弃脏数据）
 */
import { render, screen, fireEvent, waitFor, within, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useAppStore } from "../store";

vi.mock("../api", () => ({
  getAgent: vi.fn(),
  updateAgent: vi.fn(),
  getPermissionRules: vi.fn(async () => ({
    permissionMode: "full",
    allowedTools: [],
    deniedTools: [],
    askTools: [],
    mcpServers: [],
    boundSkills: [],
  })),
  getModels: vi.fn(async () => []),
  getMcpServers: vi.fn(async () => []),
  bindAgentMcp: vi.fn(async () => ({})),
  unbindAgentMcp: vi.fn(async () => ({})),
}));

import AgentDetailPanel from "./AgentDetailPanel";
import { getAgent, updateAgent } from "../api";

const mockGetAgent = vi.mocked(getAgent);
const mockUpdateAgent = vi.mocked(updateAgent);

function agentFixture(overrides: Record<string, unknown> = {}) {
  return {
    id: "agent-a",
    shortId: "aaaa1111",
    name: "成员甲",
    role: "developer",
    status: "active",
    goal: "目标A",
    backstory: "背景A",
    parentId: null,
    projectId: "proj-1",
    permissionType: "executor",
    permissionMode: "full",
    allowedTools: [],
    deniedTools: [],
    askTools: [],
    mcpServers: [],
    boundSkills: [],
    modelId: null,
    reasoningEffort: null,
    createdAt: 1700000000000,
    updatedAt: 1700000000000,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/** 定位某个可编辑字段区块（label 所在 flex 行的父级）内部的查询工具 */
function fieldBlock(label: string) {
  const labelEl = screen.getByText(label, { selector: "label" });
  const row = labelEl.closest("div");
  const root = row?.parentElement;
  if (!root) throw new Error(`field block not found: ${label}`);
  return within(root as HTMLElement);
}

async function bumpOrgTree(version: number) {
  await act(async () => {
    useAppStore.setState({ orgTreeVersion: version });
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  // /api/chat/resolved-model 走原生 fetch，测试环境桩掉
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no network in test"))));
  mockGetAgent.mockResolvedValue(agentFixture());
  mockUpdateAgent.mockResolvedValue({});
  useAppStore.setState({
    orgTreeVersion: 0,
    processingAgents: [],
    agentActiveModel: {},
    selectedAgentId: "agent-a",
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AgentDetailPanel 编辑草稿与刷新隔离（FE-09）", () => {
  it("T-14: 编辑中已修改 ⇒ 刷新回填保留脏字段，非脏字段正常更新", async () => {
    render(<AgentDetailPanel agentId="agent-a" />);
    await screen.findByText("目标A");

    // 进入目标编辑并修改（脏）
    fireEvent.click(fieldBlock("目标").getByText("编辑"));
    const goalInput = screen.getByLabelText("目标") as HTMLTextAreaElement;
    fireEvent.change(goalInput, { target: { value: "我的未保存目标" } });
    expect(goalInput.value).toBe("我的未保存目标");

    // 组织状态变化触发刷新：服务器上目标没变、背景故事变了
    mockGetAgent.mockResolvedValue(agentFixture({ backstory: "背景A-新" }));
    await bumpOrgTree(1);

    // 非脏字段（背景故事，查看态）收到新数据
    await waitFor(() => expect(screen.getByText("背景A-新")).toBeTruthy());
    // 脏字段保留用户输入，未被服务器值覆盖；服务器目标值未变 ⇒ 无冲突提示
    expect((screen.getByLabelText("目标") as HTMLTextAreaElement).value).toBe("我的未保存目标");
    expect(screen.queryByText(/此内容已被更新/)).toBeNull();
  });

  it("编辑中未修改 ⇒ 草稿跟随最新服务器数据（可接收刷新）", async () => {
    render(<AgentDetailPanel agentId="agent-a" />);
    await screen.findByText("目标A");

    fireEvent.click(fieldBlock("背景故事").getByText("编辑"));
    const input = screen.getByLabelText("背景故事") as HTMLTextAreaElement;
    expect(input.value).toBe("背景A"); // 未做任何修改

    mockGetAgent.mockResolvedValue(agentFixture({ backstory: "背景A-新" }));
    await bumpOrgTree(1);

    await waitFor(() =>
      expect((screen.getByLabelText("背景故事") as HTMLTextAreaElement).value).toBe("背景A-新"),
    );
  });

  it("T-15: 成员 A 的详情请求迟到 ⇒ 返回时已切到 B ⇒ 不覆盖 B 的资料", async () => {
    const gateA = deferred<ReturnType<typeof agentFixture>>();
    mockGetAgent.mockImplementation(async (id: string) =>
      id === "agent-a"
        ? gateA.promise
        : agentFixture({ id: "agent-b", name: "成员乙", goal: "目标B", backstory: "背景B" }),
    );
    const { rerender } = render(<AgentDetailPanel agentId="agent-a" />);
    await waitFor(() => expect(mockGetAgent).toHaveBeenCalledWith("agent-a"));

    // 切到 B（无未保存修改 ⇒ 静默跟随）
    await act(async () => {
      rerender(<AgentDetailPanel agentId="agent-b" />);
    });
    await screen.findByText("成员乙");
    expect(screen.getByText("目标B")).toBeTruthy();

    // A 的响应此刻才迟到返回
    await act(async () => {
      gateA.resolve(agentFixture());
    });
    // （挂载时 orgTreeVersion 效应会并发抓取一次，次数不精确；核对最近一次请求对象即可）
    expect(mockGetAgent.mock.calls.at(-1)![0]).toBe("agent-b");

    // B 的资料未被 A 覆盖
    expect(screen.getByText("成员乙")).toBeTruthy();
    expect(screen.getByText("目标B")).toBeTruthy();
    expect(screen.queryByText("成员甲")).toBeNull();
    expect(screen.queryByText("目标A")).toBeNull();
  });

  it("保存中：禁重复提交（按钮禁用、输入只读），成功后更新权威数据并退出编辑", async () => {
    let resolveSave!: (v: unknown) => void;
    mockUpdateAgent.mockImplementation(
      () => new Promise((res) => { resolveSave = res; }),
    );
    render(<AgentDetailPanel agentId="agent-a" />);
    await screen.findByText("目标A");

    fireEvent.click(fieldBlock("目标").getByText("编辑"));
    fireEvent.change(screen.getByLabelText("目标"), { target: { value: "新目标" } });
    fireEvent.click(fieldBlock("目标").getByText("保存"));

    // 保存中：按钮禁用且显示「保存中...」，草稿保留可读
    const savingBtn = await waitFor(() => {
      const btn = fieldBlock("目标").getByText("保存中...") as HTMLButtonElement;
      expect(btn.disabled).toBe(true);
      return btn;
    });
    expect((screen.getByLabelText("目标") as HTMLTextAreaElement).value).toBe("新目标");
    expect(mockUpdateAgent).toHaveBeenCalledTimes(1);

    // 保存中连点不产生第二次提交
    fireEvent.click(savingBtn);
    expect(mockUpdateAgent).toHaveBeenCalledTimes(1);

    // 保存成功后面板会 refreshOrgTree 重新拉取权威数据 —— 服务器上已是新值
    mockGetAgent.mockResolvedValue(agentFixture({ goal: "新目标" }));
    await act(async () => {
      resolveSave({});
    });
    // 成功：退出编辑，权威数据显示新值，组织树已刷新
    await waitFor(() => expect(screen.getByText("新目标")).toBeTruthy());
    expect(screen.queryByLabelText("目标")).toBeNull();
    expect(mockUpdateAgent).toHaveBeenCalledTimes(1);
    expect(useAppStore.getState().orgTreeVersion).toBe(1);
  });

  it("保存失败：输入保留 + 错误可见 + 重试成功后退出编辑", async () => {
    mockUpdateAgent.mockRejectedValueOnce(new Error("网络错误"));
    render(<AgentDetailPanel agentId="agent-a" />);
    await screen.findByText("目标A");

    fireEvent.click(fieldBlock("目标").getByText("编辑"));
    fireEvent.change(screen.getByLabelText("目标"), { target: { value: "会失败的目标" } });
    fireEvent.click(fieldBlock("目标").getByText("保存"));

    // 失败：错误可见、仍在编辑态、输入保留、出现重试
    await screen.findByText(/保存失败：网络错误/);
    expect((screen.getByLabelText("目标") as HTMLTextAreaElement).value).toBe("会失败的目标");
    expect(fieldBlock("目标").getByText("重试")).toBeTruthy();
    expect(fieldBlock("目标").getByText("取消")).toBeTruthy();

    // 重试成功 ⇒ 更新权威数据并退出编辑（重试成功后刷新回来的权威数据也应是新值）
    mockUpdateAgent.mockResolvedValueOnce({});
    mockGetAgent.mockResolvedValue(agentFixture({ goal: "会失败的目标" }));
    fireEvent.click(fieldBlock("目标").getByText("重试"));
    await waitFor(() => expect(screen.getByText("会失败的目标")).toBeTruthy());
    expect(screen.queryByLabelText("目标")).toBeNull();
  });

  it("取消：恢复服务器值，不自动提交", async () => {
    render(<AgentDetailPanel agentId="agent-a" />);
    await screen.findByText("目标A");

    fireEvent.click(fieldBlock("目标").getByText("编辑"));
    fireEvent.change(screen.getByLabelText("目标"), { target: { value: "临时草稿" } });
    fireEvent.click(fieldBlock("目标").getByText("取消"));

    expect(screen.getByText("目标A")).toBeTruthy();
    expect(screen.queryByLabelText("目标")).toBeNull();
    expect(mockUpdateAgent).not.toHaveBeenCalled();
  });

  it("服务器数据冲突：提示「此内容已被更新」，可加载最新内容", async () => {
    render(<AgentDetailPanel agentId="agent-a" />);
    await screen.findByText("目标A");

    fireEvent.click(fieldBlock("目标").getByText("编辑"));
    fireEvent.change(screen.getByLabelText("目标"), { target: { value: "我的本地版本" } });

    // 服务器上目标被其他人改了
    mockGetAgent.mockResolvedValue(agentFixture({ goal: "服务器新目标" }));
    await bumpOrgTree(1);

    await screen.findByText(/此内容已被更新/);
    // 冲突时脏字段仍不被覆盖
    expect((screen.getByLabelText("目标") as HTMLTextAreaElement).value).toBe("我的本地版本");

    // 选择加载最新内容：放弃本地修改，停留在编辑态（未修改）
    fireEvent.click(screen.getByText("加载最新内容"));
    expect((screen.getByLabelText("目标") as HTMLTextAreaElement).value).toBe("服务器新目标");
    expect(screen.queryByText(/此内容已被更新/)).toBeNull();
    expect(mockUpdateAgent).not.toHaveBeenCalled();
  });

  it("切换成员有未保存修改 ⇒ 弹确认；继续编辑=取消切换且输入保留", async () => {
    const { rerender } = render(<AgentDetailPanel agentId="agent-a" />);
    await screen.findByText("目标A");

    fireEvent.click(fieldBlock("目标").getByText("编辑"));
    fireEvent.change(screen.getByLabelText("目标"), { target: { value: "未保存的修改" } });

    mockGetAgent.mockResolvedValue(
      agentFixture({ id: "agent-b", name: "成员乙", goal: "目标B", backstory: "背景B" }),
    );
    await act(async () => {
      rerender(<AgentDetailPanel agentId="agent-b" />);
    });

    // 弹确认而不是静默覆盖；面板仍显示 A
    await screen.findByText("有未保存的修改");
    expect(screen.queryByText("成员乙")).toBeNull();

    // 继续编辑：取消切换，全局选中回写为 A
    fireEvent.click(screen.getByText("继续编辑"));
    await waitFor(() => expect(useAppStore.getState().selectedAgentId).toBe("agent-a"));
    await act(async () => {
      rerender(<AgentDetailPanel agentId="agent-a" />);
    });
    expect((screen.getByLabelText("目标") as HTMLTextAreaElement).value).toBe("未保存的修改");
    expect(screen.queryByText("成员乙")).toBeNull();
  });

  it("切换成员确认弹窗选「放弃修改并切换」⇒ 脏数据丢弃，显示新成员", async () => {
    const { rerender } = render(<AgentDetailPanel agentId="agent-a" />);
    await screen.findByText("目标A");

    fireEvent.click(fieldBlock("目标").getByText("编辑"));
    fireEvent.change(screen.getByLabelText("目标"), { target: { value: "将丢弃的修改" } });

    mockGetAgent.mockResolvedValue(
      agentFixture({ id: "agent-b", name: "成员乙", goal: "目标B", backstory: "背景B" }),
    );
    await act(async () => {
      rerender(<AgentDetailPanel agentId="agent-b" />);
    });
    await screen.findByText("有未保存的修改");
    fireEvent.click(screen.getByText("放弃修改并切换"));

    await screen.findByText("成员乙");
    expect(screen.getByText("目标B")).toBeTruthy();
    expect(screen.queryByLabelText("目标")).toBeNull(); // 无残留编辑态
  });

  it("保存进行中放弃并切换 ⇒ 迟到的保存失败不污染新成员的字段状态", async () => {
    let rejectSave!: (e: unknown) => void;
    mockUpdateAgent.mockImplementation(
      () => new Promise((_res, rej) => { rejectSave = rej; }),
    );
    const { rerender } = render(<AgentDetailPanel agentId="agent-a" />);
    await screen.findByText("目标A");

    // 目标进入保存中
    fireEvent.click(fieldBlock("目标").getByText("编辑"));
    fireEvent.change(screen.getByLabelText("目标"), { target: { value: "在途保存" } });
    fireEvent.click(fieldBlock("目标").getByText("保存"));
    await screen.findByText("保存中...");

    // 保存在途时放弃并切换到 B
    mockGetAgent.mockResolvedValue(
      agentFixture({ id: "agent-b", name: "成员乙", goal: "目标B", backstory: "背景B" }),
    );
    await act(async () => {
      rerender(<AgentDetailPanel agentId="agent-b" />);
    });
    await screen.findByText("有未保存的修改");
    fireEvent.click(screen.getByText("放弃修改并切换"));
    await screen.findByText("成员乙");

    // A 的保存此刻才失败：B 的目标字段保持查看态，不弹错误、不进入编辑
    await act(async () => {
      rejectSave(new Error("迟到失败"));
    });
    await waitFor(() => expect(screen.getByText("目标B")).toBeTruthy());
    expect(screen.queryByText(/迟到失败/)).toBeNull();
    expect(screen.queryByLabelText("目标")).toBeNull();
    expect(screen.queryByText(/保存失败/)).toBeNull();
  });
});
