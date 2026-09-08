/**
 * ErrorBoundary — 白屏防御围栏的行为测试。
 * 核心：懒加载面板渲染抛错时错误被限制在围栏内（不再卸根白屏），
 * overlay 可传 fallback={null} 静默降级。
 */
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import type { ReactElement } from "react";

import ErrorBoundary from "./ErrorBoundary";

function Boom({ message }: { message: string }): ReactElement {
  throw new Error(message);
}

describe("ErrorBoundary", () => {
  it("子组件正常时原样渲染", () => {
    render(
      <ErrorBoundary label="面板甲">
        <div>正常内容</div>
      </ErrorBoundary>,
    );
    expect(screen.getByText("正常内容")).toBeTruthy();
  });

  it("渲染抛错被限制在围栏内，展示 label+错误信息+重载按钮", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary label="Goals">
        <Boom message="chunk load failed: GoalsPanel-abc.js" />
      </ErrorBoundary>,
    );
    expect(screen.getByText(/「Goals」面板出错了/)).toBeTruthy();
    expect(screen.getByText(/GoalsPanel-abc\.js/)).toBeTruthy();
    expect(screen.getByText("重载页面")).toBeTruthy();
    // 崩溃子组件不出现
    expect(screen.queryByText("正常内容")).toBeNull();
    spy.mockRestore();
  });

  it("传 fallback={null} 时静默降级（overlay 场景）", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { container } = render(
      <ErrorBoundary label="对话框" fallback={null}>
        <Boom message="dialog chunk failed" />
      </ErrorBoundary>,
    );
    expect(container.innerHTML).toBe("");
    spy.mockRestore();
  });
});
