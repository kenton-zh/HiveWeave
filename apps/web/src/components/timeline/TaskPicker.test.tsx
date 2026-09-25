/**
 * TaskPicker 搜索与直达边界（FE-10 / SR-03，方案 §9.6，验收 T-09/T-16）。
 *
 * 锁死契约：
 *  1. 输入过滤候选，方向键移动高亮、回车选中高亮项 —— 纯标题回车
 *     绝不把输入当 task_id 跳伪 ID（T-09）；
 *  2. 无匹配显示「无匹配任务」，回车不跳转；
 *  3. 只有完整 task_id（UUID / 已知 id 精确相等）才直达（归档入口保留）；
 *  4. Esc 只收起下拉，搜索词保留；选中后搜索词也保留；
 *  5. 候选随 timelineVersion 失效信号刷新。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../api", () => ({ listTasks: vi.fn() }));
vi.mock("../../navigation/commands", () => ({ openTask: vi.fn() }));

import { listTasks } from "../../api";
import { openTask } from "../../navigation/commands";
import TaskPicker, { isFullTaskId } from "./TaskPicker";
import { useAppStore } from "../../store";
import type { TaskSummary } from "./types";

const T1: TaskSummary = {
  id: "11111111-1111-4111-8111-111111111111",
  title: "实现登录接口",
  status: "running",
};
const T2: TaskSummary = {
  id: "22222222-2222-4222-8222-222222222222",
  title: "修复缩放方向",
  status: "closed",
};
const ARCHIVED_ID = "a3b3c3d3-e3f3-4a3b-8c3d-3e3f3a3b3c3d";

function input() {
  return screen.getByLabelText("搜索任务或按完整 task_id 直达") as HTMLInputElement;
}

async function openPicker() {
  render(<TaskPicker />);
  await waitFor(() => expect(listTasks).toHaveBeenCalled());
  fireEvent.focus(input());
  await screen.findByText("实现登录接口");
}

describe("TaskPicker 键盘候选选择（T-09）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      selectedProjectId: "p-1",
      selectedTaskId: null,
      timelineVersion: 0,
    });
    vi.mocked(listTasks).mockResolvedValue([T1, T2]);
  });

  it("输入标题过滤候选，回车选中高亮项（不生成伪 ID）", async () => {
    await openPicker();
    fireEvent.change(input(), { target: { value: "登录" } });
    expect(screen.queryByText("修复缩放方向")).not.toBeInTheDocument();
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(openTask).toHaveBeenCalledWith(T1.id); // 真 ID，不是「登录」
    expect(openTask).not.toHaveBeenCalledWith("登录");
  });

  it("方向键移动高亮（aria-activedescendant 跟随），回车选中当前项", async () => {
    await openPicker();
    expect(input().getAttribute("aria-activedescendant")).toBe("task-picker-opt-0");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().getAttribute("aria-activedescendant")).toBe("task-picker-opt-1");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().getAttribute("aria-activedescendant")).toBe("task-picker-opt-0");
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(openTask).toHaveBeenCalledWith(T1.id);
  });

  it("高亮不会越界（连按 ArrowDown 钳制在最后一行）", async () => {
    await openPicker();
    for (let i = 0; i < 10; i++) fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().getAttribute("aria-activedescendant")).toBe("task-picker-opt-1");
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(openTask).toHaveBeenCalledWith(T2.id);
  });

  it("无匹配显示「无匹配任务」，回车不跳伪 ID（T-09 关键回归）", async () => {
    await openPicker();
    fireEvent.change(input(), { target: { value: "根本不存在的标题xyz" } });
    expect(screen.getByText("无匹配任务")).toBeInTheDocument();
    expect(screen.queryByText(/直达完整 task_id/)).not.toBeInTheDocument();
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(openTask).not.toHaveBeenCalled();
  });

  it("残缺 ID（UUID 前缀片段）不直达", async () => {
    await openPicker();
    fireEvent.change(input(), { target: { value: "33333333-3333-4333-83" } });
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(openTask).not.toHaveBeenCalled();
  });

  it("完整 task_id 直达：不在候选列表（归档任务）也能打开", async () => {
    await openPicker();
    fireEvent.change(input(), { target: { value: ARCHIVED_ID } });
    expect(screen.getByText(/直达完整 task_id/)).toBeInTheDocument();
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(openTask).toHaveBeenCalledWith(ARCHIVED_ID);
  });

  it("大写 UUID 直达归一化为小写（task id 落库为小写 uuid4）", async () => {
    await openPicker();
    fireEvent.change(input(), { target: { value: ARCHIVED_ID.toUpperCase() } });
    expect(screen.getByText(/直达完整 task_id/)).toBeInTheDocument();
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(openTask).toHaveBeenCalledWith(ARCHIVED_ID); // 小写
    expect(openTask).not.toHaveBeenCalledWith(ARCHIVED_ID.toUpperCase());
  });

  it("与已知任务 id 精确相等也算完整 ID（非 UUID 形态兜底）", () => {
    expect(isFullTaskId("custom-task-42", [{ ...T1, id: "custom-task-42" }])).toBe(true);
    expect(isFullTaskId("custom-task-4", [{ ...T1, id: "custom-task-42" }])).toBe(false);
    expect(
      isFullTaskId(ARCHIVED_ID, [T1, T2]),
    ).toBe(true);
    expect(isFullTaskId("", [T1])).toBe(false);
  });

  it("Esc 只收起下拉、保留搜索词（§8.2 边界）；重新聚焦可恢复列表", async () => {
    await openPicker();
    fireEvent.change(input(), { target: { value: "登录" } });
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    fireEvent.keyDown(input(), { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(input().value).toBe("登录"); // 不清词
    fireEvent.focus(input());
    expect(await screen.findByText("实现登录接口")).toBeInTheDocument();
  });

  it("选中后返回列表：搜索词保留，不因 pick 清空", async () => {
    await openPicker();
    fireEvent.change(input(), { target: { value: "登录" } });
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(openTask).toHaveBeenCalledWith(T1.id);
    expect(input().value).toBe("登录");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("点击候选打开对应任务", async () => {
    await openPicker();
    fireEvent.click(screen.getByText("修复缩放方向"));
    expect(openTask).toHaveBeenCalledWith(T2.id);
  });

  it("候选随 timelineVersion 失效信号刷新（不只项目变化拉一次）", async () => {
    render(<TaskPicker />);
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(1));
    useAppStore.setState({ timelineVersion: 1 });
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(2));
    useAppStore.setState({ timelineVersion: 2 });
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(3));
  });

  it("项目变化时先清旧列表再拉新（防误选旧项目任务）", async () => {
    render(<TaskPicker />);
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(1));
    useAppStore.setState({ selectedProjectId: "p-2" });
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(2));
    // 清空动作在拉取前发生：此处至少保证第二次拉取用的是新项目 id
    expect(vi.mocked(listTasks).mock.calls[1][0]).toBe("p-2");
  });
});
