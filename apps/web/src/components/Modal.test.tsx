import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import Modal from "./Modal";

describe("Modal 原语（无障碍 + 叠加语义）", () => {
  it("渲染 dialog 语义，Escape 关闭", () => {
    const onClose = vi.fn();
    render(
      <Modal ariaLabel="测试弹窗" onClose={onClose}>
        <button>确定</button>
      </Modal>,
    );
    expect(screen.getByRole("dialog", { name: "测试弹窗" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("多弹窗叠加时仅最上层响应 Escape", () => {
    const closeA = vi.fn();
    const closeB = vi.fn();
    render(
      <Modal ariaLabel="A" onClose={closeA}>
        <button>a</button>
      </Modal>,
    );
    render(
      <Modal ariaLabel="B" onClose={closeB}>
        <button>b</button>
      </Modal>,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(closeB).toHaveBeenCalledTimes(1);
    expect(closeA).not.toHaveBeenCalled();
  });

  it("点遮罩关闭，点面板不关闭", () => {
    const onClose = vi.fn();
    render(
      <Modal ariaLabel="C" onClose={onClose}>
        <button>c</button>
      </Modal>,
    );
    const panel = screen.getByRole("dialog", { name: "C" });
    fireEvent.click(panel);
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(panel.parentElement as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("挂载后把焦点移入面板内首个可聚焦元素", () => {
    render(
      <Modal ariaLabel="D">
        <button>首要</button>
        <button>次要</button>
      </Modal>,
    );
    expect(screen.getByRole("button", { name: "首要" })).toHaveFocus();
  });
});
