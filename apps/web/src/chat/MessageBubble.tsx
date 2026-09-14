import { memo, useEffect, useState } from "react";
import type { AttachmentRef, ChatMessage, ContextMarkerKind, ToolCall } from "./types";
import { CHAT_MOTION_CSS, toolCategories } from "./constants";
import {
  collectFileAttachments,
  collectMessageImages,
  extractToolFilePath,
  formatToolInputHint,
  toolResultFirstLine,
} from "./messageUtils";
import { estimateMessageTokens, estimateTokens } from "./tokenEstimate";
import { MarkdownText } from "./MarkdownText";
import { ImageGallery } from "./ImageGallery";

/**
 * DSH 风格思考行：单行 `Think · 摘要`，点击展开全文。
 *
 * 原实现是紫框 details 卡片，占大量纵向空间，与工具卡片、外层气泡形成
 * 三层嵌套 —— 「显示太混乱」的主要来源。改为与工具行同构的扁平事件行，
 * 靠图标 + 字重区分类型，而不是靠边框和背景色。
 */
function ThinkingBlock({ content }: { content: string }) {
  const [open, setOpen] = useState(false);
  const preview = content.replace(/\s+/g, " ").trim();
  return (
    <div className="my-0.5">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="w-full flex items-center gap-2 text-left py-1 px-1 -mx-1 rounded-md hover:bg-g-bg-muted/70 transition-colors"
      >
        <svg
          className="w-3 h-3 shrink-0 text-purple-400"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M12 3a6 6 0 00-3.6 10.8V17a1 1 0 001 1h5.2a1 1 0 001-1v-3.2A6 6 0 0012 3z"
          />
        </svg>
        <span className="text-[11px] font-medium text-purple-500 shrink-0">Think</span>
        <span className="text-g-fg-4 text-[11px] shrink-0">·</span>
        <span className={`text-[11px] text-g-fg-4 min-w-0 ${open ? "hidden" : "truncate"}`}>
          {preview}
        </span>
        {open && (
          <span className="text-[10px] text-g-fg-4 ml-auto shrink-0">
            {estimateTokens(content)} tokens
          </span>
        )}
      </button>
      {open && (
        <div className="mt-1 ml-5 border-l border-purple-200 pl-2.5">
          <div className="text-[11px] text-g-fg-3 whitespace-pre-wrap break-words max-h-64 overflow-y-auto leading-relaxed font-mono select-text">
            {content}
          </div>
        </div>
      )}
    </div>
  );
}

function ToolStatusIcon({ status }: { status?: ToolCall["status"] }) {
  if (status === "running" || !status) {
    return (
      <svg
        className="w-3 h-3 text-g-blue animate-spin shrink-0"
        fill="none"
        viewBox="0 0 24 24"
        role="img"
        aria-label="执行中"
      >
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path
          className="opacity-75"
          fill="currentColor"
          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
        />
      </svg>
    );
  }
  if (status === "error") {
    return (
      <svg
        className="w-3 h-3 text-red-500 shrink-0"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2.5}
        role="img"
        aria-label="失败"
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
      </svg>
    );
  }
  return (
    <svg
      className="w-3 h-3 text-emerald-500 shrink-0"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2.5}
      role="img"
      aria-label="成功"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
    </svg>
  );
}

// 结果二次截断兜底：后端流式 500 / 落库 2000 已截，此处再防
// legacy/异常大 payload 撑爆 DOM（阈值与后端 TOOL_RESULT_PERSIST_EXCERPT 对齐）。
const RESULT_RENDER_MAX = 2000;

/** 复制成功的短暂反馈时长（ms）；期间图标换成对勾。 */
const COPIED_FEEDBACK_MS = 1500;

/**
 * P1 文件卡片（2026-09-06）：write_file / read_file / edit_file / apply_patch
 * 类工具调用渲染为文件 chip（图标 + 文件名 + 单行摘要 + 状态 + 复制路径），
 * 替换原 JSON `<pre>` 直出。摘要 = result 首个非空行（成功=「Updated x
 * (+2 lines)」类首行；失败=error 首行，红色）。「打开」按钮不做 —— 前端
 * 没有现成的打开本地文件机制，不为此造新 IPC 面，只留复制。
 */
function FileToolChip({ call, path }: { call: ToolCall; path: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), COPIED_FEEDBACK_MS);
    return () => window.clearTimeout(t);
  }, [copied]);
  const fileName = path.split(/[\\/]/).pop() || path;
  const summary = toolResultFirstLine(call.result);
  const isError = call.status === "error";
  const copy = () => {
    // 无剪贴板环境（jsdom / 非安全上下文）与权限拒绝一律静默。
    // 注意 ?. 必须链到 .then：clipboard 缺失时 writeText 短路为 undefined，
    // 对 undefined 调 .then 会抛 TypeError。
    navigator.clipboard?.writeText(path)?.then(
      () => setCopied(true),
      () => {},
    );
  };
  return (
    // 可达性：整行是展示性 chip，唯一交互是复制按钮（见下），行本身不带点击。
    <div className="my-0.5 flex items-center gap-2 py-1 px-1 -mx-1 rounded-md" data-file-chip="">
      <svg
        className="w-3 h-3 shrink-0 text-slate-500"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2}
        aria-hidden="true"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"
        />
        <path strokeLinecap="round" strokeLinejoin="round" d="M14 2v6h6" />
      </svg>
      <span className="font-mono text-[11px] font-medium text-g-fg-2 shrink-0" title={path}>
        {fileName}
      </span>
      {summary && (
        <>
          <span className="text-g-fg-4 text-[11px] shrink-0">·</span>
          <span
            className={`text-[11px] truncate min-w-0 ${isError ? "text-red-600" : "text-g-fg-4"}`}
            title={call.result}
          >
            {summary}
          </span>
        </>
      )}
      <span className="ml-auto shrink-0 flex items-center gap-1">
        <button
          type="button"
          aria-label={copied ? "已复制文件路径" : "复制文件路径"}
          title="复制路径"
          onClick={copy}
          className="p-0.5 rounded text-g-fg-4 hover:text-g-fg-2 hover:bg-g-bg-muted/70 transition-colors"
        >
          {copied ? (
            <svg
              className="w-3 h-3 text-emerald-500"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2.5}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
            </svg>
          ) : (
            <svg
              className="w-3 h-3"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
              aria-hidden="true"
            >
              <rect x="9" y="9" width="13" height="13" rx="2" />
              <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
            </svg>
          )}
        </button>
        <ToolStatusIcon status={call.status} />
      </span>
    </div>
  );
}

/**
 * 消息附件里的 file 附件 chip（AttachmentRef kind==="file"）：展示名 + 大小，
 * 纯展示不设交互（urlOrId 可能是不透明存储 id，复制/打开都无意义，等后端
 * 落地存储句柄再谈）。
 */
function FileAttachmentChip({ att, onUserSide }: { att: AttachmentRef; onUserSide: boolean }) {
  const size =
    typeof att.bytes === "number" && att.bytes > 0 ? formatBytes(att.bytes) : undefined;
  return (
    <span
      className={`inline-flex max-w-[16rem] items-center gap-1.5 rounded-lg px-2 py-1 text-[11px] ${
        onUserSide ? "bg-white/15 text-white ring-1 ring-white/30" : "border border-g-border bg-g-bg-muted/60 text-g-fg-2"
      }`}
      title={att.mediaType ? `${att.name} · ${att.mediaType}` : att.name}
    >
      <svg
        className="w-3 h-3 shrink-0 opacity-70"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2}
        aria-hidden="true"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M15.172 7l-8.586 8.586a2 2 0 102.828 2.828l6.414-6.414a4 4 0 10-5.656-5.656l-6.415 6.414a6 6 0 108.486 8.486L20 13"
        />
      </svg>
      <span className="truncate">{att.name}</span>
      {size && <span className="shrink-0 opacity-60">{size}</span>}
    </span>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * DSH 风格工具行：单行 `● tool_name · 参数 ✓`，点击展开入参与结果。
 *
 * 去掉了原本的圆角卡片 + 边框 + 背景填充 —— 在气泡内再套卡片是三层
 * 嵌套的第三层。状态改由左侧色点 + 右侧图标承担（不单靠颜色：图标
 * 形状本身可区分成功/失败/进行中，满足无障碍要求）。
 */
function ToolCallRow({ call }: { call: ToolCall }) {
  const [showDetail, setShowDetail] = useState(false);
  // P1 文件卡片：文件类工具（write_file/read_file/edit_file/apply_patch）且
  // 能从 input 提取到路径 → 文件 chip 替换整行（含 JSON <pre> 展开）；
  // 提取不到路径（apply_patch 空 patches 等）回落标准行，其他工具保持现状。
  // 注意分支在 useState 之后 —— 保持 hooks 顺序无条件。
  const filePath = extractToolFilePath(call.tool, call.input);
  if (filePath) {
    return <FileToolChip call={call} path={filePath} />;
  }
  const hint = formatToolInputHint(call.tool, call.input);
  const cat = toolCategories[call.tool];
  const catDot = cat ? cat.color.replace("text-", "bg-") : "bg-g-fg-4";
  const hasDetail = (call.input && Object.keys(call.input).length > 0) || !!call.result;
  const resultText =
    call.result && call.result.length > RESULT_RENDER_MAX
      ? call.result.slice(0, RESULT_RENDER_MAX) + "\n…（结果已截断）"
      : call.result;
  return (
    <div className="my-0.5">
      <button
        type="button"
        onClick={() => hasDetail && setShowDetail(!showDetail)}
        aria-expanded={hasDetail ? showDetail : undefined}
        disabled={!hasDetail}
        className={`w-full flex items-center gap-2 text-left py-1 px-1 -mx-1 rounded-md transition-colors ${
          hasDetail ? "hover:bg-g-bg-muted/70" : "cursor-default"
        }`}
      >
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${catDot}`} aria-hidden="true" />
        <span className="font-mono text-[11px] font-medium text-g-fg-2 shrink-0">{call.tool}</span>
        {hint && (
          <>
            <span className="text-g-fg-4 text-[11px] shrink-0">·</span>
            <span className="text-g-fg-4 text-[11px] truncate min-w-0">{hint}</span>
          </>
        )}
        <span className="ml-auto shrink-0 flex items-center">
          <ToolStatusIcon status={call.status} />
        </span>
      </button>
      {showDetail && hasDetail && (
        <div className="mt-1 ml-3.5 space-y-1.5 border-l border-g-border pl-2.5">
          {call.input && Object.keys(call.input).length > 0 && (
            <pre className="text-[10px] text-amber-600 whitespace-pre-wrap break-all font-mono leading-relaxed max-h-56 overflow-y-auto select-text">
              {JSON.stringify(call.input, null, 2)}
            </pre>
          )}
          {resultText && (
            <pre
              className={`text-[10px] whitespace-pre-wrap break-all font-mono leading-relaxed max-h-56 overflow-y-auto select-text ${
                call.status === "error" ? "text-red-600" : "text-g-fg-3"
              }`}
            >
              {resultText}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 上下文边界分界线（后端压缩/裁剪落地后发出的标记）。
 *
 * 存在理由：conversation_turns（模型真实上下文）会被压缩重写，而
 * chat_messages（本面板数据源）只追加。没有这条线，用户会看到模型
 * 早已忘记的历史并以为它还记得。DSH 用 compaction node 画同样的线。
 */
function ContextMarkerRow({ kind, content }: { kind: ContextMarkerKind; content: string }) {
  const isCompaction = kind === "compaction";
  const label = isCompaction ? "上下文已压缩" : "旧工具输出已移除";
  return (
    // role="note" 而非 separator：separator 语义是"无内容分隔符"，屏幕
    // 阅读器会跳过下方解释文字（恰是最需要传达的信息）。容器不设
    // aria-label，否则会覆盖内部文本；视觉分隔线整体 aria-hidden。
    <div className="my-4 hw-msg-in" role="note">
      <div className="flex items-center gap-2">
        <span
          className="h-px flex-1 bg-gradient-to-r from-transparent to-amber-300"
          aria-hidden="true"
        />
        <span className="flex items-center gap-1.5 rounded-full border border-amber-300 bg-amber-50 px-2.5 py-1 shrink-0">
          <svg
            className="w-3 h-3 text-amber-600 shrink-0"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
            aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 8h16M7 12h10M10 16h4" />
          </svg>
          <span className="text-[10px] font-semibold text-amber-700">{label}</span>
        </span>
        <span className="h-px flex-1 bg-gradient-to-l from-transparent to-amber-300" aria-hidden="true" />
      </div>
      <p className="mt-1.5 text-center text-[10px] leading-relaxed text-g-fg-4">{content}</p>
    </div>
  );
}

/**
 * 轮次分隔线（round_boundary 段）：多轮 tool-loop 的轮与轮之间。
 * live（draft 的 beginStreamRound）与持久化（metadata.segments 的
 * build_display_segments）产同 kind 段 —— 此处同一分支渲染，done reload
 * 不再丢轮次分隔。样式弱化（轮次是节奏信息，非边界警告）：居中细线 +
 * 小字「第 N 轮」，round 0 起号故显示 N+1。
 */
function RoundBoundaryRow({ round }: { round?: number }) {
  const label = typeof round === "number" ? `第 ${round + 1} 轮` : "新一轮";
  return (
    <div className="my-3 flex items-center gap-2" role="separator" aria-label={label}>
      <span className="h-px flex-1 bg-g-border" aria-hidden="true" />
      <span className="text-[10px] font-medium text-g-fg-4 shrink-0 select-none">{label}</span>
      <span className="h-px flex-1 bg-g-border" aria-hidden="true" />
    </div>
  );
}

/** 来源徽章 —— 气泡的核心职责：一眼看出这条消息来自谁。 */
function SourceBadge({ source }: { source: "agent" | "system" | "watchdog" }) {
  if (source === "watchdog") {
    return (
      <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-orange-100 text-orange-700 border border-orange-200 shrink-0">
        看门狗
      </span>
    );
  }
  if (source === "agent") {
    return (
      <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-indigo-50 text-indigo-600 border border-indigo-200 shrink-0">
        AGENT
      </span>
    );
  }
  return (
    <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-600 border border-gray-200 shrink-0">
      系统
    </span>
  );
}

/** 来源视觉样式：右缘色条 + 琥珀系淡底（输入侧统一色系，靠右微信式）。
 * 三个来源同底色系（bg-amber-50），只以色条+徽章区分来源——
 * "发给 AI 的消息"整体一种背景，与 AI 绿底输出成对照。
 */
const SOURCE_STYLES: Record<"agent" | "system" | "watchdog", { bar: string }> = {
  watchdog: { bar: "border-r-orange-400" },
  agent: { bar: "border-r-indigo-400" },
  system: { bar: "border-r-slate-400" },
};

/** 非真人入站消息（agent 来信 / 系统注入 / 看门狗唤醒 digest）。
 *
 * 微信式靠右（与用户气泡同侧）+ 琥珀底 + 右缘来源色条；
 * 默认折叠单行摘要，点击展开全文。
 */
function InboundLetter({ msg, sourceName }: { msg: ChatMessage; sourceName?: string }) {
  const [open, setOpen] = useState(false);
  const source: "agent" | "system" | "watchdog" =
    msg.source === "agent" || msg.source === "watchdog" ? msg.source : "system";
  const style = SOURCE_STYLES[source];
  const time = new Date(msg.timestamp).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
  // 摘要：压空白 + 剥 markdown 标题/列表记号（digest 常为 "## 标题\n{json}" 格式）
  const preview = (msg.content || "（无正文）")
    .replace(/\s+/g, " ")
    .replace(/(^|\s)#+\s*/g, "$1")
    .replace(/(^|\s)[-•]\s+/g, "$1")
    .trim();
  return (
    <div className="flex justify-end my-1.5 hw-msg-in">
      <div
        className={`w-full max-w-[88%] rounded-2xl rounded-tr-md border border-r-4 ${style.bar} bg-amber-50 border-amber-200 overflow-hidden cursor-pointer transition-colors hover:bg-amber-100/70`}
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen(!open);
          }
        }}
      >
        <div className="flex items-center gap-2 px-3 py-1.5 min-w-0">
          <SourceBadge source={source} />
          <span className="text-xs font-semibold text-g-fg-2 shrink-0 truncate max-w-[10rem]">
            {sourceName ||
              (source === "watchdog"
                ? "看门狗唤醒"
                : source === "agent"
                  ? "Agent 来信"
                  : "系统消息")}
          </span>
          <span className={`text-[11px] text-g-fg-3 truncate min-w-0 ${open ? "hidden" : ""}`}>
            {preview.slice(0, 60)}
            {preview.length > 60 ? "…" : ""}
          </span>
          <span className="text-[10px] text-g-fg-4 ml-auto shrink-0 flex items-center gap-1">
            {time}
            <svg
              className={`w-3 h-3 transition-transform duration-200 ${open ? "rotate-180" : ""}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2.5}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
            </svg>
          </span>
        </div>
        {open && (
          <div className="px-3.5 pb-2.5 pt-1 border-t border-amber-200/70 select-text">
            <div className="text-[13px] text-g-fg-2 leading-relaxed whitespace-pre-wrap break-words max-h-[40vh] overflow-y-auto">
              {msg.content || "（无正文）"}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function MessageBubbleInner({
  msg,
  isStreaming,
  thinkingElapsed,
  sourceName,
  agentName,
  streamStartedAt,
}: {
  msg: ChatMessage;
  isStreaming?: boolean;
  thinkingElapsed?: number | null;
  sourceName?: string;
  agentName?: string;
  /** 本轮流开始时间（仅流式中的目标气泡有值）——实时 tok/s 用。 */
  streamStartedAt?: number;
}) {
  // 上下文边界标记优先于普通 system 气泡：它是分界线，不是消息。
  if (msg._contextMarker) {
    return <ContextMarkerRow kind={msg._contextMarker} content={msg.content} />;
  }

  if (msg.role === "system") {
    return (
      <div className="flex justify-center my-4 hw-msg-in">
        <div className="rounded-xl px-4 py-2 bg-g-bg-muted/80 border border-g-border text-g-fg-3 text-xs text-center leading-relaxed shadow-gm-sm">
          <p className="whitespace-pre-wrap">{msg.content}</p>
        </div>
      </div>
    );
  }

  // 非真人入站（agent 来信 / 系统注入 / 看门狗 digest）→ 信件卡片
  const isInboundMail =
    msg.role === "user" &&
    (msg.source === "agent" || msg.source === "system" || msg.source === "watchdog");
  if (isInboundMail) {
    return <InboundLetter msg={msg} sourceName={sourceName} />;
  }

  const segments = msg._segments || [];
  const hasSegments = segments.length > 0;
  // live draft 的 thinking 段已在 segments 内；persisted 消息 thinking
  // 已由后端 build_display_segments 作为 thinking 段写入 metadata.segments
  // （DSH 整轮视图），_thinking 列仅作 legacy/兜底。
  // segments 已含 thinking 时跳过 _thinking，避免双渲染。
  const segmentsHaveThinking = segments.some((s) => s.type === "thinking" && s.content);
  const thinking = msg._thinking || "";

  const isUser = msg.role === "user";
  const isEmpty =
    !msg.content && !thinking && !hasSegments && (!msg.toolCalls || msg.toolCalls.length === 0);

  // P1 附件区（2026-09-06）：msg.images（既有通路）与 attachments 里
  // kind==="image" 的合并为**一个** gallery（学 DSH AssistantMarkdown.tsx:72-95
  // 连续 image 块并组），file 附件出文件 chip —— 都渲染在正文后。
  const galleryImages = collectMessageImages(msg);
  const fileAtts = collectFileAttachments(msg);

  // 生成统计：tokens 用与后端一致的 char-ratio 估算；速率流式期间实时
  // （draft.startedAt 起算），完成后用冻结的 _genStats.ms 做分母——分子
  // 恒为渲染时的估算，与显示的 "~N tok" 同口径。
  const genTokens = !isUser ? estimateMessageTokens(msg) : 0;
  let genRate: number | null = null;
  if (!isUser && genTokens > 0) {
    if (isStreaming && streamStartedAt) {
      const elapsedS = Math.max(0.5, (Date.now() - streamStartedAt) / 1000);
      genRate = genTokens / elapsedS;
    } else if (msg._genStats && msg._genStats.ms > 0) {
      genRate = genTokens / (msg._genStats.ms / 1000);
    }
  }

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} my-2 hw-msg-in`}>
      <div
        className={
          isUser
            ? "max-w-[88%] rounded-2xl rounded-br-md px-4 py-2.5 text-[14px] leading-relaxed text-white shadow-gm-sm"
            : "w-full max-w-[88%] rounded-2xl rounded-tl-md border border-g-border bg-white px-3.5 py-2.5 text-[14px] leading-relaxed text-g-fg shadow-gm-sm"
        }
        style={
          isUser
            ? { background: "linear-gradient(135deg, #5b54e8 0%, #4f46e5 55%, #4338ca 100%)" }
            : undefined
        }
      >
        {!isUser && (
          <div className="flex items-center gap-1.5 mb-1.5 pb-1.5 border-b border-g-border/70">
            <span
              className="w-5 h-5 rounded-md flex items-center justify-center text-[9px] font-bold text-white shadow-gm-sm shrink-0"
              style={{ background: "linear-gradient(135deg, #10b981 0%, #059669 100%)" }}
            >
              AI
            </span>
            <span className="text-[11px] font-semibold text-g-fg-2 truncate min-w-0">
              {agentName || "回复"}
            </span>
            {isStreaming && !isEmpty && (
              <span className="text-[10px] text-emerald-600 font-medium shrink-0">· 生成中</span>
            )}
            {genTokens > 0 && (
              <span className="text-[10px] text-g-fg-4 ml-auto shrink-0 font-mono">
                ~{genTokens.toLocaleString()} tok
                {genRate != null && ` · ${genRate.toFixed(1)} tok/s`}
              </span>
            )}
          </div>
        )}
        {hasSegments ? (
          <div>
            {!isUser && thinking && !segmentsHaveThinking && <ThinkingBlock content={thinking} />}
            {segments.map((seg, i) => {
              if (seg.type === "round_boundary") {
                // live 与持久化共用此分支（渲染统一）：轮次分隔线不参与
                // 文本流，永远独立成行。
                return <RoundBoundaryRow key={`round-${seg.round ?? i}`} round={seg.round} />;
              }
              if (seg.type === "thinking" && seg.content) {
                return <ThinkingBlock key={`think-${i}`} content={seg.content} />;
              }
              if (seg.type === "text" && seg.content) {
                // P1 安全门（审计 2026-09-05）：markdown 仅限 assistant text 段。
                // 当前没有 user 消息携带 segments 的路径，但未来任何路径挂上了，
                // 此门兜住「用户输入被静默按 markdown 语义渲染」——用户文本按
                // 字面显示不变。
                if (!isUser) {
                  // P0 富文本：text 段走 markdown 渲染（安全基线见 MarkdownText.tsx
                  // 头注释）。段落节奏由 .hw-md p{margin:.25rem 0} 保持原 my-1 观感。
                  return <MarkdownText key={`text-${i}`} content={seg.content} />;
                }
                return (
                  <p key={`text-${i}`} className="whitespace-pre-wrap my-1">
                    {seg.content}
                  </p>
                );
              }
              if (seg.type === "tool_call" && seg.tool) {
                // key 优先用 tool_call_id：ToolCallRow 持有展开状态，纯下标
                // key 在 tool-loop 多轮追加时会把展开的详情串到别的工具行上。
                return <ToolCallRow key={seg.tool.id ?? `tool-${i}`} call={seg.tool} />;
              }
              return null;
            })}
          </div>
        ) : (
          <>
            {!isUser && thinking && <ThinkingBlock content={thinking} />}
            {msg.content && <p className="whitespace-pre-wrap">{msg.content}</p>}
            {!isUser && msg.toolCalls && msg.toolCalls.length > 0 && (
              <div className="mt-1.5">
                {msg.toolCalls.map((tc, i) => (
                  <ToolCallRow key={tc.id ?? i} call={tc} />
                ))}
              </div>
            )}
          </>
        )}

        {!isUser && isStreaming && hasSegments && (
          <span className="inline-block w-[3px] h-4 rounded-full bg-g-blue ml-1 align-middle hw-stream-cursor" />
        )}

        {/* fixplan #8：交付状态徽章（后端挂 metadata.delivery_state）。
            「谎报在用户侧一眼可辨」就靠这一块 —— 缺了它，后端"不拦、只标注"
            的设计等于没落地（拆了旧闸门、换上用户看不见的标注 = 净亏）。 */}
        {!isUser && msg.deliveryBadge && (
          <div
            data-testid="delivery-badge"
            className={
              "mt-1.5 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] " +
              (msg.deliveryBadge.state === "complete"
                ? "bg-emerald-50 text-emerald-700"
                : "bg-amber-50 text-amber-700")
            }
            title={(msg.deliveryBadge.blockers || []).join("\n")}
          >
            {msg.deliveryBadge.state === "complete"
              ? `交付状态：已核验完成${
                  msg.deliveryBadge.deliveredAt
                    ? `（${msg.deliveryBadge.deliveredAt}）`
                    : ""
                }`
              : `交付状态：未标记完工${
                  (msg.deliveryBadge.blockers || []).length > 0
                    ? `（${(msg.deliveryBadge.blockers || []).length} 项待收口）`
                    : ""
                }`}
          </div>
        )}

        {/* P1 附件区：正文（含光标）之后 —— 图片合并 gallery，file 附件出 chip。 */}
        {galleryImages.length > 0 && (
          <ImageGallery images={galleryImages} align={isUser ? "end" : "start"} />
        )}
        {fileAtts.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {fileAtts.map((att, i) => (
              <FileAttachmentChip key={`${att.urlOrId}:${i}`} att={att} onUserSide={isUser} />
            ))}
          </div>
        )}

        {!isUser && isEmpty && isStreaming && (
          <div className="flex items-center gap-2.5 py-1">
            {thinkingElapsed != null ? (
              <>
                <span className="flex gap-1.5">
                  {[0, 160, 320].map((d) => (
                    <span
                      key={d}
                      className="w-2 h-2 rounded-full bg-g-blue hw-typing-dot"
                      style={{ animationDelay: `${d}ms` }}
                    />
                  ))}
                </span>
                <span className="text-xs font-medium hw-thinking-shimmer">
                  思考中{thinkingElapsed > 0 ? ` · ${Math.floor(thinkingElapsed)}s` : ""}…
                </span>
              </>
            ) : (
              <span className="flex gap-1.5">
                {[0, 160, 320].map((d) => (
                  <span
                    key={d}
                    className="w-2 h-2 rounded-full bg-g-fg-4 hw-typing-dot"
                    style={{ animationDelay: `${d}ms` }}
                  />
                ))}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export const MessageBubble = memo(MessageBubbleInner);

export function ChatMotionStyles() {
  return <style>{CHAT_MOTION_CSS}</style>;
}
