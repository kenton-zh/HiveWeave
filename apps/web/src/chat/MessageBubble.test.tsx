import { describe, it, expect } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MessageBubble } from "./MessageBubble";
import type { ChatMessage, MsgSegment } from "./types";

/**
 * 轮次分隔渲染统一（2026-09-05）：round_boundary 段由 live draft
 * （beginStreamRound）与持久化消息（后端 build_display_segments）产同
 * kind 段，MessageBubble 同一分支渲染 —— 这里断言两种来源渲染一致，
 * 且旧「—— 第 N 轮 ——」伪 text 标记不再以文本形式出现。
 */

function mkMsg(segments: MsgSegment[], over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "a1",
    role: "assistant",
    content: "",
    timestamp: 1,
    _segments: segments,
    ...over,
  };
}

describe("MessageBubble round_boundary 渲染（live==persisted）", () => {
  it("持久化 segments：round_boundary 渲染为带轮号标签的分隔线", () => {
    render(
      <MessageBubble
        msg={mkMsg([
          { type: "text", content: "第一轮旁白" },
          { type: "round_boundary", round: 1 },
          { type: "text", content: "第二轮旁白" },
        ])}
      />,
    );
    expect(screen.getByText("第一轮旁白")).toBeInTheDocument();
    expect(screen.getByText("第二轮旁白")).toBeInTheDocument();
    // round 0 起号 → 显示 N+1；separator role + aria-label 可达性
    const divider = screen.getByRole("separator", { name: "第 2 轮" });
    expect(divider).toBeInTheDocument();
    expect(screen.getByText("第 2 轮")).toBeInTheDocument();
  });

  it("live draft 与持久化同分支：同 kind 段渲染出同样的轮次分隔", () => {
    // live draft 的 _segments 由 mergeStreamDraftIntoMessages 原样携带，
    // 渲染路径与持久化消息完全一致 —— 用相同 segments 断言分隔线输出一致。
    const segments: MsgSegment[] = [
      { type: "text", content: "旁白A" },
      { type: "round_boundary", round: 2 },
      { type: "text", content: "旁白B" },
    ];
    const first = render(<MessageBubble msg={mkMsg(segments)} />);
    const persistedDivider = first.container.querySelector('[role="separator"]')?.outerHTML;
    first.unmount();
    const second = render(<MessageBubble msg={mkMsg(segments, { isStreaming: true })} />);
    const liveDivider = second.container.querySelector('[role="separator"]')?.outerHTML;
    expect(persistedDivider).toBeTruthy();
    // streaming 气泡额外有光标等装饰，但分隔线本身逐字节一致
    expect(liveDivider).toBe(persistedDivider);
    expect(second.getByText("第 3 轮")).toBeInTheDocument();
  });

  it("缺轮号的 round_boundary 渲染兜底文案，不抛错", () => {
    render(
      <MessageBubble
        msg={mkMsg([
          { type: "text", content: "旁白" },
          { type: "round_boundary" },
        ])}
      />,
    );
    expect(screen.getByRole("separator", { name: "新一轮" })).toBeInTheDocument();
  });
});

/**
 * P0 富文本（2026-09-05）：text 段经 MarkdownText 做 markdown 渲染。
 * 安全基线见 MarkdownText.tsx 头注释 / docs/2026-09-05/chat-rich-text-design.md §3.2：
 * 无 HTML 直通（不装 rehype-raw）、链接/图片协议白名单、外链 noopener。
 */
describe("MessageBubble text 段 markdown 渲染（P0）", () => {
  it("围栏代码块渲染为 pre>code 等宽块，内容逐字保留并带语言标记", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([{ type: "text", content: "看这段：\n```js\nconst a = 1;\nconsole.log(a);\n```" }])}
      />,
    );
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre!.textContent).toContain("const a = 1;");
    expect(pre!.textContent).toContain("console.log(a);");
    const code = pre!.querySelector("code");
    expect(code?.className).toContain("language-js");
  });

  it("行内代码渲染为独立 <code>，不进 pre", () => {
    render(<MessageBubble msg={mkMsg([{ type: "text", content: "运行 `npm test` 验证" }])} />);
    const inline = screen.getByText("npm test");
    expect(inline.tagName).toBe("CODE");
    expect(inline.closest("pre")).toBeNull();
  });

  it("http 外链带 target=_blank 与 rel=noopener noreferrer", () => {
    render(
      <MessageBubble
        msg={mkMsg([{ type: "text", content: "参考 [文档](https://example.com/docs) 说明" }])}
      />,
    );
    const anchor = screen.getByText("文档") as HTMLAnchorElement;
    expect(anchor.href).toBe("https://example.com/docs");
    expect(anchor.getAttribute("target")).toBe("_blank");
    expect(anchor.getAttribute("rel")).toContain("noopener");
    expect(anchor.getAttribute("rel")).toContain("noreferrer");
  });

  it("javascript: 链接被协议白名单拒绝：不产出 <a>，文本保留", () => {
    const { container } = render(
      <MessageBubble msg={mkMsg([{ type: "text", content: "[点我](javascript:alert(1)) 领奖" }])} />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText("点我")).toBeInTheDocument();
  });

  it("HTML 注入被转义：<img src=x onerror> 不执行、DOM 不出现 img 元素", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([
          { type: "text", content: '看看 <img src=x onerror="window.__hw_xss=1"> 这个' },
        ])}
      />,
    );
    expect((window as unknown as Record<string, unknown>).__hw_xss).toBeUndefined();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/看看/)).toBeInTheDocument();
  });

  it("流式中途未闭合围栏不崩：按 CommonMark 渐进渲染为代码块", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([{ type: "text", content: "代码如下：\n```python\nprint('hi'" }], {
          isStreaming: true,
        })}
      />,
    );
    const code = container.querySelector("pre code");
    expect(code).not.toBeNull();
    expect(code!.textContent).toContain("print('hi'");
  });
});

/**
 * P1 审计收尾（2026-09-05）：P1-1 user 消息 markdown 门控锁定、
 * P1-2 超长 text 段纯文本降级、safeUrlTransform 归一化加固三例。
 */
describe("MessageBubble text 段 P1 审计收尾", () => {
  it("P1-1 锁定：user 消息带 text segment 时仍纯文本渲染（markdown 不生效）", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([{ type: "text", content: "用户原文 **不加粗** `不代码`" }], { role: "user" })}
      />,
    );
    // markdown 语义元素一个都不允许出现
    expect(container.querySelector("strong, code, pre, h1")).toBeNull();
    // 字面文本原样保留（** 与反引号可见）
    expect(screen.getByText(/用户原文 \*\*不加粗\*\* `不代码`/)).toBeInTheDocument();
  });

  it("加固：data: URI 链接被协议白名单拒绝（不产出 <a>）", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([
          {
            type: "text",
            content:
              "[点我](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==) 领奖",
          },
        ])}
      />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText("点我")).toBeInTheDocument();
  });

  it("加固：jav&#9;ascript: 经 WHATWG 归一化（剥 TAB）后协议为 javascript: 被拒", () => {
    // &#9; 在 CommonMark link destination 中解码为真实 TAB；new URL() 会剥
    // TAB 得到 javascript: 协议 —— 白名单按归一化结果判定必须拒。
    const { container } = render(
      <MessageBubble
        msg={mkMsg([{ type: "text", content: "[点我](<jav&#9;ascript:alert(1)>) 领奖" }])}
      />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText("点我")).toBeInTheDocument();
  });

  it("加固：//evil.com 协议相对地址被拒（无 base 解析失败 → 不产出 <a>）", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([{ type: "text", content: "[点我](//evil.com/path) 领奖" }])}
      />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText("点我")).toBeInTheDocument();
  });

  it("P1-2：超过 64KB 阈值的 text 段降级纯文本，markdown 管线不跑", () => {
    const big = "# 大标题 **加粗**\n\n" + "x".repeat(70 * 1024);
    const { container } = render(<MessageBubble msg={mkMsg([{ type: "text", content: big }])} />);
    // markdown 会渲染出 h1/strong —— 纯文本降级后全部保持字面
    expect(container.querySelector("h1, strong, pre, code")).toBeNull();
    expect(container.textContent).toContain("# 大标题 **加粗**");
  });
});

/**
 * P1 富文本三件（2026-09-06）：附件 ref 建模渲染（合并 gallery + 文件附件
 * chip）与文件类工具的文件卡片（chip 替换 JSON <pre> 直出）。
 */
describe("MessageBubble 附件区（P1-①③：正文后合并 gallery + file chip）", () => {
  it("msg.images 与 attachments 里的 image 合并为一个 gallery（不各自成组）", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([{ type: "text", content: "看图" }], {
          images: ["data:image/png;base64,AAA"],
          attachments: [
            { kind: "image", name: "b.png", urlOrId: "https://example.com/b.png" },
            { kind: "file", name: "spec.md", urlOrId: "att_spec", bytes: 2048 },
          ],
        })}
      />,
    );
    const galleries = container.querySelectorAll("[data-hw-gallery]");
    expect(galleries).toHaveLength(1);
    expect(galleries[0].querySelectorAll("img")).toHaveLength(2);
    // gallery 在正文之后（附件区在正文后）
    const text = screen.getByText("看图");
    expect(text.compareDocumentPosition(galleries[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // file 附件 → chip（名称 + 大小），不走 gallery
    expect(screen.getByText("spec.md")).toBeInTheDocument();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
  });

  it("attachments 图片为不透明存储 id（非 URL）时不出碎图", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([], {
          attachments: [{ kind: "image", name: "a.png", urlOrId: "att_123" }],
        })}
      />,
    );
    expect(container.querySelector("[data-hw-gallery]")).toBeNull();
  });
});

describe("MessageBubble 文件卡片（P1-②：文件类工具 chip 替换 JSON <pre>）", () => {
  it("write_file：文件 chip（文件名 + result 首行摘要 + 复制按钮），无 JSON dump", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([
          {
            type: "tool_call",
            tool: {
              tool: "write_file",
              input: { filePath: "src/deep/nested/mod.ts", content: "…" },
              status: "ok",
              result: "Updated src/… hmm\n+2 lines",
            },
          },
        ])}
      />,
    );
    const chip = container.querySelector("[data-file-chip]")!;
    expect(chip).not.toBeNull();
    // 文件名剥路径（title 保留全路径）
    expect(chip.textContent).toContain("mod.ts");
    expect(chip.textContent).toContain("Updated src/… hmm");
    // JSON <pre> 直出被替换
    expect(container.querySelector("pre")).toBeNull();
    // 复制按钮存在
    expect(screen.getByRole("button", { name: "复制文件路径" })).toBeInTheDocument();
  });

  it("复制按钮写剪贴板（含全路径），成功后短暂反馈；失败静默不抛错", async () => {
    let written: string | null = null;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: (s: string) => { written = s; return Promise.resolve(); } },
    });
    const { container } = render(
      <MessageBubble
        msg={mkMsg([
          {
            type: "tool_call",
            tool: {
              tool: "edit_file",
              input: { filePath: "lib/a.py", old_string: "x", new_string: "y" },
              status: "ok",
              result: "Updated lib/a.py (+1 lines)",
            },
          },
        ])}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "复制文件路径" }));
    expect(written).toBe("lib/a.py");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "已复制文件路径" })).toBeInTheDocument(),
    );
    // 失败静默：clipboard 拒绝也不抛
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: () => Promise.reject(new Error("denied")) },
    });
    fireEvent.click(screen.getByRole("button", { name: "已复制文件路径" }));
  });

  it("失败工具：摘要取 error 首行并标红（text-g-red）", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([
          {
            type: "tool_call",
            tool: {
              tool: "read_file",
              input: { path: "nope.txt" },
              status: "error",
              result: "FileNotFoundError: nope.txt\n  at …",
            },
          },
        ])}
      />,
    );
    const chip = container.querySelector("[data-file-chip]")!;
    expect(chip.textContent).toContain("FileNotFoundError: nope.txt");
    expect(chip.textContent).toContain("nope.txt");
    expect(chip.querySelector(".text-g-red")).not.toBeNull();
  });

  it("非文件工具保持现状：仍有 JSON <pre> 展开，无文件 chip", () => {
    const { container } = render(
      <MessageBubble
        msg={mkMsg([
          {
            type: "tool_call",
            tool: { tool: "list_files", input: { dirPath: "src" }, status: "ok", result: "a.ts\nb.ts" },
          },
        ])}
      />,
    );
    expect(container.querySelector("[data-file-chip]")).toBeNull();
    // 标准行可展开 JSON —— 点行头后出现 pre
    fireEvent.click(container.querySelector("button")!);
    expect(container.querySelector("pre")?.textContent).toContain("dirPath");
  });
});

describe("MessageBubble 文件卡片（P1-②）：clipboard 缺失环境", () => {
  it("navigator.clipboard 为 undefined 时点复制不抛错（?. 链到 .then）", () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const { container } = render(
      <MessageBubble
        msg={mkMsg([
          {
            type: "tool_call",
            tool: { tool: "write_file", input: { filePath: "a.ts" }, status: "ok" },
          },
        ])}
      />,
    );
    expect(() =>
      fireEvent.click(screen.getByRole("button", { name: "复制文件路径" })),
    ).not.toThrow();
    expect(container.querySelector("[data-file-chip]")).not.toBeNull();
  });
});
