/**
 * ProjectRunControls（FE-05 / UX-06 / 方案 §12.3）
 *
 * 顶栏与旧工作台共用这一个组件实例逻辑，这里锁：
 *   1. 从 store 现有数据源（projects[].isStarted）渲染运行状态；
 *   2. 点击 ⇒ 调用现有 api（activate/deactivateProject）并刷新项目列表；
 *   3. 失败 ⇒ 行内「可重试」提示，再次点击走重试且成功后清除；
 *   4. 未选项目 ⇒ 不渲染。
 *
 * api 整体 mock；store 用真实实例（组件本就只消费 store 现有 action/state）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({
  activateProject: vi.fn(),
  deactivateProject: vi.fn(),
  getProjects: vi.fn(),
}));

import { activateProject, deactivateProject, getProjects, type Project } from "../api";
import ProjectRunControls from "./ProjectRunControls";
import { useAppStore } from "../store";

const P1: Project = {
  id: "p-1",
  name: "项目一",
  isStarted: true,
  createdAt: 0,
};

function setStateWith(project: Project | null) {
  useAppStore.setState({
    selectedProjectId: project?.id ?? null,
    projects: project ? [project] : [],
    toasts: [],
  });
}

describe("ProjectRunControls —— 项目运行开关", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setStateWith(P1);
  });

  it("从 store 渲染运行状态：上班中", () => {
    render(<ProjectRunControls />);
    expect(screen.getByTestId("project-run-toggle")).toHaveTextContent("上班中");
    expect(screen.queryByTestId("project-run-error")).not.toBeInTheDocument();
  });

  it("未选中项目时不渲染", () => {
    setStateWith(null);
    const { container } = render(<ProjectRunControls />);
    expect(container).toBeEmptyDOMElement();
  });

  it("点击下班：调用现有 deactivateProject 并刷新项目列表，状态翻转", async () => {
    vi.mocked(deactivateProject).mockResolvedValueOnce(false as never);
    vi.mocked(getProjects).mockResolvedValueOnce([{ ...P1, isStarted: false }] as never);

    render(<ProjectRunControls />);
    fireEvent.click(screen.getByTestId("project-run-toggle"));

    await waitFor(() =>
      expect(useAppStore.getState().projects[0].isStarted).toBe(false),
    );
    expect(deactivateProject).toHaveBeenCalledTimes(1);
    expect(deactivateProject).toHaveBeenCalledWith("p-1");
    expect(activateProject).not.toHaveBeenCalled();
    // 刷新后状态展示翻转
    await waitFor(() =>
      expect(screen.getByTestId("project-run-toggle")).toHaveTextContent("已下班"),
    );
  });

  it("失败：行内可重试提示，重试成功后提示清除", async () => {
    vi.mocked(deactivateProject)
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce(false as never);
    vi.mocked(getProjects).mockResolvedValue([{ ...P1, isStarted: false }] as never);

    render(<ProjectRunControls />);
    fireEvent.click(screen.getByTestId("project-run-toggle"));

    // 失败提示可见，开关保持可用（即重试入口）
    expect(await screen.findByTestId("project-run-error")).toHaveTextContent("可重试");
    expect(screen.getByTestId("project-run-toggle")).not.toBeDisabled();

    // 重试成功：提示清除，状态刷新
    fireEvent.click(screen.getByTestId("project-run-toggle"));
    await waitFor(() =>
      expect(useAppStore.getState().projects[0].isStarted).toBe(false),
    );
    await waitFor(() =>
      expect(screen.queryByTestId("project-run-error")).not.toBeInTheDocument(),
    );
    expect(deactivateProject).toHaveBeenCalledTimes(2);
  });
});
