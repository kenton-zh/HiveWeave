import { useEffect, useState } from "react";
import type { ContextMarkerKind, ToolCall } from "./types";
import { toolCategories } from "./constants";
import {
  estimateTokens,
} from "./tokenEstimate";
import {
  extractToolFilePath,
  formatToolInputHint,
  toolResultFirstLine,
} from "./messageUtils";

/**
 * chat 事件行层（FE-13 · 前端设计规格 §10.1.4 内容四分层）。
 *
 * 四类内容的后两类落点 —— 从 MessageBubble.tsx 抽出为领域模块，让分层契约
 * 在代码结构上可读（气泡本体 = 用户消息 + Agent 业务回复两层，仍在
 * MessageBubble；工具调用 / 系统与协作事件 = 本文件的扁平事件行）：
 *
 * | 层               | 组件                              | 视觉权重             |
 * |------------------|-----------------------------------|----------------------|
 * | 工具调用         | ToolCallRow / FileToolChip        | 单行事件条，可展开   |
 * | 系统与协作事件   | ThinkingBlock / RoundBoundaryRow / ContextMarkerRow | 最轻，可折叠 |
 *
 * 不是删运行记录，而是让用户先读到有意义的信息、仍可追溯证据。
 */

/** DSH 风格思考行：单行 `Think · 摘要`，点击展开全文。
 *
 * 原实现是紫框 details 卡片，占大量纵向空间，与工具卡片、外层气泡形成
 * 三层嵌套 —— 「显示太混乱」的主要来源。改为与工具行同构的扁平事件行，
 * 靠图标 + 字重区分类型，而不是靠边框和背景色。
 */
export function ThinkingBlock({ content }: { content: string }) {
  const [open, setOpen] = useState(false);
  const preview = content.replace(/\s+/g, " ").trim();
  return (
    <div className="my-0.5">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="w-full flex items-center gap-2 text-left py-1 px-1 -mx-1 rounded-gm hover:bg-g-bg-muted/70 transition-colors"
      >
        <svg
          className="w-3 h-3 shrink-0 text-g-purple-vivid"
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
        <span className="text-[11px] font-medium text-g-purple-vivid shrink-0">Think</span>
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
        <div className="mt-1 ml-5 border-l border-g-purple pl-2.5">
          <div className="text-[11px] text-g-fg-3 whitespace-pre-wrap break-words max-h-64 overflow-y-auto leading-relaxed font-mono select-text">
            {content}
          </div>
        </div>
      )}
    </div>
  );
}

export function ToolStatusIcon({ status }: { status?: ToolCall["status"] }) {
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
        className="w-3 h-3 text-g-red-vivid shrink-0"
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
      className="w-3 h-3 text-g-green-vivid shrink-0"
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
export function FileToolChip({ call, path }: { call: ToolCall; path: string }) {
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
    <div className="my-0.5 flex items-center gap-2 py-1 px-1 -mx-1 rounded-gm" data-file-chip="">
      <svg
        className="w-3 h-3 shrink-0 text-g-fg-3"
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
            className={`text-[11px] truncate min-w-0 ${isError ? "text-g-red" : "text-g-fg-4"}`}
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
          className="p-0.5 rounded-gm text-g-fg-4 hover:text-g-fg-2 hover:bg-g-bg-muted/70 transition-colors"
        >
          {copied ? (
            <svg
              className="w-3 h-3 text-g-green-vivid"
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
 * DSH 风格工具行：单行 `● tool_name · 参数 ✓`，点击展开入参与结果。
 *
 * 去掉了原本的圆角卡片 + 边框 + 背景填充 —— 在气泡内再套卡片是三层
 * 嵌套的第三层。状态改由左侧色点 + 右侧图标承担（不单靠颜色：图标
 * 形状本身可区分成功/失败/进行中，满足无障碍要求）。
 *
 * FE-13（§10.1.4）：默认收起；`live`（流式中）且本工具 `running` 时
 * **自动展开**参数，结果一到（status 翻 ok/error）自动收回 —— 让用户
 * 跟上「正在执行什么」，又不让历史长块常驻占屏。用户手动点过行头后
 * 以用户意图为准（pinned），自动开合不再干预。
 */
export function ToolCallRow({ call, live }: { call: ToolCall; live?: boolean }) {
  const [showDetail, setShowDetail] = useState(false);
  // 用户手动开合过 → 自动开合退出，交给用户（本行实例生命周期内有效）。
  const [userToggled, setUserToggled] = useState(false);
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
  const autoOpen = !!live && call.status === "running";
  const open = userToggled ? showDetail : autoOpen || showDetail;
  const resultText =
    call.result && call.result.length > RESULT_RENDER_MAX
      ? call.result.slice(0, RESULT_RENDER_MAX) + "\n…（结果已截断）"
      : call.result;
  return (
    <div className="my-0.5" data-tool-row={call.tool}>
      <button
        type="button"
        onClick={() => {
          if (!hasDetail) return;
          setUserToggled(true);
          setShowDetail(!open);
        }}
        aria-expanded={hasDetail ? open : undefined}
        disabled={!hasDetail}
        className={`w-full flex items-center gap-2 text-left py-1 px-1 -mx-1 rounded-gm transition-colors ${
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
        <span className="ml-auto shrink-0 flex items-center gap-1">
          {/* 可展开提示（§10.1.4「可展开」要可发现）：箭头随开合旋转 */}
          {hasDetail && (
            <svg
              className={`w-3 h-3 text-g-fg-4 transition-transform duration-200 shrink-0 ${open ? "rotate-180" : ""}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2.5}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
            </svg>
          )}
          <ToolStatusIcon status={call.status} />
        </span>
      </button>
      {open && hasDetail && (
        <div className="mt-1 ml-3.5 space-y-1.5 border-l border-g-border pl-2.5">
          {call.input && Object.keys(call.input).length > 0 && (
            <pre className="text-[10px] text-g-yellow whitespace-pre-wrap break-all font-mono leading-relaxed max-h-56 overflow-y-auto select-text">
              {JSON.stringify(call.input, null, 2)}
            </pre>
          )}
          {resultText && (
            <pre
              className={`text-[10px] whitespace-pre-wrap break-all font-mono leading-relaxed max-h-56 overflow-y-auto select-text ${
                call.status === "error" ? "text-g-red" : "text-g-fg-3"
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
 * 轮次分隔线（round_boundary 段）：多轮 tool-loop 的轮与轮之间。
 * live（draft 的 beginStreamRound）与持久化（metadata.segments 的
 * build_display_segments）产同 kind 段 —— 此处同一分支渲染，done reload
 * 不再丢轮次分隔。样式弱化（轮次是节奏信息，非边界警告）：居中细线 +
 * 小字「第 N 轮」，round 0 起号故显示 N+1。
 */
export function RoundBoundaryRow({ round }: { round?: number }) {
  const label = typeof round === "number" ? `第 ${round + 1} 轮` : "新一轮";
  return (
    <div className="my-3 flex items-center gap-2" role="separator" aria-label={label}>
      <span className="h-px flex-1 bg-g-border" aria-hidden="true" />
      <span className="text-[10px] font-medium text-g-fg-4 shrink-0 select-none">{label}</span>
      <span className="h-px flex-1 bg-g-border" aria-hidden="true" />
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
export function ContextMarkerRow({ kind, content }: { kind: ContextMarkerKind; content: string }) {
  const isCompaction = kind === "compaction";
  const label = isCompaction ? "上下文已压缩" : "旧工具输出已移除";
  return (
    // role="note" 而非 separator：separator 语义是"无内容分隔符"，屏幕
    // 阅读器会跳过下方解释文字（恰是最需要传达的信息）。容器不设
    // aria-label，否则会覆盖内部文本；视觉分隔线整体 aria-hidden。
    <div className="my-4 hw-msg-in" role="note">
      <div className="flex items-center gap-2">
        <span
          className="h-px flex-1 bg-gradient-to-r from-transparent to-g-yellow-vivid"
          aria-hidden="true"
        />
        <span className="flex items-center gap-1.5 rounded-full border border-g-yellow bg-g-yellow-bg px-2.5 py-1 shrink-0">
          <svg
            className="w-3 h-3 text-g-yellow shrink-0"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
            aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 8h16M7 12h10M10 16h4" />
          </svg>
          <span className="text-[10px] font-semibold text-g-yellow">{label}</span>
        </span>
        <span className="h-px flex-1 bg-gradient-to-l from-transparent to-g-yellow-vivid" aria-hidden="true" />
      </div>
      <p className="mt-1.5 text-center text-[10px] leading-relaxed text-g-fg-4">{content}</p>
    </div>
  );
}
