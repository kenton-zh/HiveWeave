import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MarkdownText } from "./MarkdownText";

/**
 * P2 代码块（2026-09-08）：围栏语言标签 + 复制按钮。
 * 安全基线不变（无 innerHTML 通道）；复制在 jsdom 无 clipboard 时静默回落。
 */

describe("MarkdownText 代码块 P2", () => {
  it("围栏代码渲染语言标签与复制按钮", () => {
    render(<MarkdownText content={"```python\nprint('hi')\n```"} />);
    expect(screen.getByText("python")).toBeTruthy();
    expect(screen.getByRole("button", { name: "复制" })).toBeTruthy();
  });

  it("无语言围栏回落 text 标签", () => {
    render(<MarkdownText content={"```\nplain\n```"} />);
    expect(screen.getByText("text")).toBeTruthy();
  });

  it("点击复制不崩（jsdom 无 clipboard → 静默回落）", () => {
    render(<MarkdownText content={"```js\nconst a = 1;\n```"} />);
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    expect(screen.getByRole("button", { name: "复制" })).toBeTruthy();
  });

  it("普通文本不渲染代码壳", () => {
    render(<MarkdownText content="hello world" />);
    expect(screen.queryByRole("button", { name: "复制" })).toBeNull();
  });
});
