/**
 * QuestionDialog 交互契约（UX-05 / 方案 §11.2「关闭不等于回答」，验收 T-10/T-11）
 *
 * 锁四条最容易回退的语义：
 *   1. 关闭（叉/Esc/遮罩）= 纯 UI 收起，**零业务请求**（旧实现会偷发一段
 *      「暂时跳过」替代答案 —— 回归此行为即测试失败）；
 *   2. 收起后问题仍可找回（收起入口），草稿不丢；
 *   3. 「跳过此问题」= 用户显式点击才发送，且恰好一次，并提示后果；
 *   4. 提交失败：不关窗、输入保留、错误可见、可重试成功（T-11）。
 *
 * api 层整体 mock：断言 `answerQuestion` 的调用次数与参数即「业务请求」判据。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({
  getQuestions: vi.fn(),
  answerQuestion: vi.fn(),
}));
vi.mock("../api/ws", () => ({ getJoinedLobbyChannel: vi.fn(() => null) }));

import { answerQuestion, getQuestions } from "../api";
import QuestionDialog from "./QuestionDialog";
import { useAppStore } from "../store";

const Q1 = {
  id: "q-1",
  agentId: "agent-1",
  agentName: "CEO",
  question: "采用方案 A 还是方案 B？",
  options: ["方案 A", "方案 B"],
  status: "pending" as const,
  createdAt: Date.now(),
};

function mockPending() {
  vi.mocked(getQuestions).mockResolvedValue([Q1] as never);
}

const QUESTION_TEXT = "采用方案 A 还是方案 B？";

async function renderWithQuestion() {
  mockPending();
  render(<QuestionDialog />);
  // 等首次拉取完成、问题渲染出来
  expect(await screen.findByText(QUESTION_TEXT)).toBeInTheDocument();
}

describe("QuestionDialog —— 关闭不等于回答（UX-05）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({ selectedProjectId: "p-1", questionVersion: 0, toasts: [] });
  });

  it.each([
    ["点叉号", () => fireEvent.click(screen.getByTestId("question-dialog-close"))],
    ["按 Esc", () => fireEvent.keyDown(window, { key: "Escape" })],
    ["点遮罩", () => fireEvent.click(screen.getByTestId("question-overlay"))],
  ])("%s：纯 UI 关闭，零业务请求，可从收起入口找回且草稿保留", async (_label, close) => {
    await renderWithQuestion();

    // 先输入草稿，验证关闭不清草稿
    const input = screen.getByPlaceholderText("或输入自定义回答...");
    fireEvent.change(input, { target: { value: "我的草稿答案" } });

    close();

    await waitFor(() =>
      expect(screen.queryByText(QUESTION_TEXT)).not.toBeInTheDocument(),
    );
    // 核心契约：关闭不发送任何业务提交请求
    expect(answerQuestion).not.toHaveBeenCalled();
    expect(getQuestions).not.toHaveBeenCalledWith(
      expect.objectContaining({ status: "answered" }),
    );

    // 找回：收起入口出现，点击重新打开，草稿仍在，依旧没有业务请求
    fireEvent.click(screen.getByTestId("question-reopen"));
    expect(await screen.findByText(QUESTION_TEXT)).toBeInTheDocument();
    expect(
      (screen.getByPlaceholderText("或输入自定义回答...") as HTMLInputElement)
        .value,
    ).toBe("我的草稿答案");
    expect(answerQuestion).not.toHaveBeenCalled();
  });

  it("点「跳过此问题」：恰好发送一次跳过动作，并提示已跳过及后果", async () => {
    await renderWithQuestion();
    vi.mocked(answerQuestion).mockResolvedValueOnce({} as never);

    fireEvent.click(screen.getByTestId("question-skip"));

    await waitFor(() => expect(answerQuestion).toHaveBeenCalledTimes(1));
    expect(answerQuestion).toHaveBeenCalledWith(
      "q-1",
      expect.stringContaining("跳过"),
      "agent-1",
    );
    // 问题移出弹窗
    await waitFor(() =>
      expect(screen.queryByText(QUESTION_TEXT)).not.toBeInTheDocument(),
    );
    // 后果提示（toast）：已跳过 + Agent 会继续其他工作
    await waitFor(() =>
      expect(
        useAppStore
          .getState()
          .toasts.some((t) => t.message.includes("已跳过该问题")),
      ).toBe(true),
    );
  });

  it("提交失败（T-11）：输入保留、错误可见、不关窗，重试成功后移出", async () => {
    await renderWithQuestion();
    vi.mocked(answerQuestion)
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce({} as never);

    const input = screen.getByPlaceholderText("或输入自定义回答...");
    fireEvent.change(input, { target: { value: "选 A，理由如下" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    // 失败：错误可见、问题仍在（未关窗）、草稿保留
    expect(await screen.findByTestId("question-error")).toHaveTextContent(
      "回答发送失败",
    );
    expect(screen.getByText(QUESTION_TEXT)).toBeInTheDocument();
    expect(
      (screen.getByPlaceholderText("或输入自定义回答...") as HTMLInputElement)
        .value,
    ).toBe("选 A，理由如下");

    // 重试成功：问题移出，总共恰好两次请求
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(screen.queryByText(QUESTION_TEXT)).not.toBeInTheDocument(),
    );
    expect(answerQuestion).toHaveBeenCalledTimes(2);
    expect(answerQuestion).toHaveBeenLastCalledWith(
      "q-1",
      "选 A，理由如下",
      "agent-1",
    );
  });
});
