import { isValidElement, memo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import "./MarkdownText.css";

/**
 * text 段 markdown 渲染（P0，学 DSH 的安全策略，见 docs/前端设计规格.md §10.1）。
 *
 * 安全基线（对齐 DSH ui-primitives/markdown/render.tsx 的 Untrusted-output policy）：
 * - 渲染通道是 hast→React（react-markdown），全程无 HTML 字符串解析、无
 *   dangerouslySetInnerHTML；且**不装 rehype-raw**，raw HTML 被默认丢弃
 *   （DSH 渲染为字面文本，我们更严格——差异记录在设计稿 §3.2）。
 * - 链接/图片目的地过协议白名单 http/https/mailto（`safeUrlTransform`，
 *   对应 DSH render.tsx sanitizeUrl:44-59）；相对/解析失败一律拒绝
 *   （DSH render.tsx:55-57 明确相对地址同禁）。
 * - 图片额外要求绝对 http(s)（DSH render.tsx remoteImageUrl:61-69），其余
 *   降级为 alt 文本；渲染时 referrerPolicy=no-referrer（DSH render.tsx:514-517）。
 * - 外链统一 target=_blank + rel="noopener noreferrer"（DSH render.tsx:477）。
 *
 * 流式：react-markdown/remark 对未闭合 ``` 围栏按 CommonMark「延伸至文末」
 * 渐进渲染，闭合后自动复原；text_delta 并段逻辑在 useChatMessages 不变，
 * 本组件只做 per-chunk 全量解析（消息量级足够，增量冻结解析留 P2）。
 */

/** DSH 同款协议白名单：绝对 http/https/mailto 之外一律拒（含相对地址）。
 *  返回 WHATWG 归一化产物（toString）而非原始串：含 \n/\t 控制符的 href
 *  落 DOM 前已被清洗，不给下游任何「带控制符的活 URL」。 */
function safeUrlTransform(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" || parsed.protocol === "mailto:"
      ? parsed.toString()
      : "";
  } catch {
    return "";
  }
}

/** 图片 src 白名单：绝对 http(s)（比链接的 mailto 更紧，DSH 同款双重门槛）。 */
function remoteHttpUrl(url: string | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const protocol = new URL(url).protocol;
    return protocol === "http:" || protocol === "https:" ? url : undefined;
  } catch {
    return undefined;
  }
}

const components: Components = {
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
  // href 已过 urlTransform：空 = 协议被拒/相对地址 → 降级纯文本（不留 <a href="">）。
  a: ({ children, href }) => {
    if (!href) return <span>{children}</span>;
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  },
  img: ({ alt, src }) => {
    const httpSrc = remoteHttpUrl(typeof src === "string" ? src : undefined);
    if (!httpSrc) return <span className="hw-md-img-alt">{alt}</span>;
    return (
      <img src={httpSrc} alt={alt ?? ""} loading="lazy" decoding="async" referrerPolicy="no-referrer" />
    );
  },
};

/** 从 pre>code 子元素提取 ``` 围栏语言（react-markdown 塞进 code.className）。 */
function codeLanguage(node: ReactNode): string {
  if (isValidElement(node)) {
    const cls = (node.props as { className?: string }).className ?? "";
    const m = /language-([\w+-]+)/.exec(cls);
    if (m) return m[1];
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      const lang = codeLanguage(child);
      if (lang) return lang;
    }
  }
  return "";
}

/**
 * P2 代码块（2026-09-08）：语言标签 + 一键复制。复制走
 * navigator.clipboard；无权限/不可用（旧 WebView2、jsdom）静默失败——
 * 不弹错、不崩 UI，按钮回落「复制」态。语法高亮（shiki）按设计稿 P2
 * 另行评估（重依赖 + 异步高亮器，与本组件同步渲染模型不合，未纳入）。
 */
function CodeBlock({ children }: { children?: ReactNode }) {
  const preRef = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);
  const lang = codeLanguage(children);

  async function onCopy() {
    const text = preRef.current?.textContent ?? "";
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return;
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="hw-md-code">
      <div className="hw-md-code-bar">
        <span className="hw-md-code-lang">{lang || "text"}</span>
        <button type="button" className="hw-md-code-copy" onClick={onCopy}>
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre ref={preRef}>{children}</pre>
    </div>
  );
}

/**
 * P1-2 性能兜底（审计 2026-09-05）：text_delta 每个 delta 触发整段 O(n)
 * 重解析，一条流累计 O(n²)——50–200KB 超长消息（常见于超长代码块）在流式
 * 末期单次解析可达 20–100ms 掉帧。超过阈值的 text 段直接降级纯文本渲染
 * （等价原 <p whitespace-pre-wrap>），不跑 markdown 管线。阈值取 64KB：
 * 远超正常聊天消息体量，又把最坏单次解析压到个位数 ms。
 */
export const MARKDOWN_PLAIN_TEXT_THRESHOLD = 64 * 1024;

export const MarkdownText = memo(function MarkdownText({ content }: { content: string }) {
  if (content.length > MARKDOWN_PLAIN_TEXT_THRESHOLD) {
    return (
      <div className="hw-md">
        <p className="whitespace-pre-wrap">{content}</p>
      </div>
    );
  }
  return (
    <div className="hw-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        urlTransform={safeUrlTransform}
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
