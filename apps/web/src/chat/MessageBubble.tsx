import { memo, useState } from "react";
import type { AttachmentRef, ChatMessage } from "./types";
import { CHAT_MOTION_CSS } from "./constants";
import { collectFileAttachments, collectMessageImages } from "./messageUtils";
import { estimateMessageTokens } from "./tokenEstimate";
import { MarkdownText } from "./MarkdownText";
import { ImageGallery } from "./ImageGallery";
import {
  ContextMarkerRow,
  RoundBoundaryRow,
  ThinkingBlock,
  ToolCallRow,
} from "./EventRows";

/**
 * 消息气泡（FE-13 · 前端设计规格 §10.1.4 内容四分层）。
 *
 * 四类内容在此汇聚，分层落点：
 * - 用户消息 / Agent 业务回复 → 本文件的气泡本体（MarkdownText 安全红线沿用）；
 * - 工具调用 / 系统与协作事件 → `chat/EventRows.tsx` 的扁平事件行
 *   （ToolCallRow / FileToolChip / ThinkingBlock / RoundBoundaryRow /
 *   ContextMarkerRow），轻量可追溯，不与对话等重。
 *
 * 滚动契约（§10.1.7）不受分层影响：本组件是纯展示，贴底跟随 / 上翻不拉回
 * / 流式不重挂载全部由 useChatMessages 的 stickToBottom + memo 保证。
 */

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
      className={`inline-flex max-w-[16rem] items-center gap-1.5 rounded-gm px-2 py-1 text-[11px] ${
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

/** 来源徽章 —— 气泡的核心职责：一眼看出这条消息来自谁。 */
function SourceBadge({ source }: { source: "agent" | "system" | "watchdog" }) {
  if (source === "watchdog") {
    return (
      <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-g-yellow-bg text-g-yellow border border-g-yellow shrink-0">
        看门狗
      </span>
    );
  }
  if (source === "agent") {
    return (
      <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-g-blue-bg text-g-blue border border-g-blue shrink-0">
        AGENT
      </span>
    );
  }
  return (
    <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-g-bg-muted text-g-fg-2 border border-g-border shrink-0">
      系统
    </span>
  );
}

/** 来源视觉样式：右缘色条 + 琥珀系淡底（输入侧统一色系，靠右微信式）。
 * 三个来源同底色系（bg-g-yellow-bg），只以色条+徽章区分来源——
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
        className={`w-full max-w-[88%] rounded-gmLg rounded-tr-gm border border-r-4 ${style.bar} bg-g-yellow-bg border-g-yellow overflow-hidden cursor-pointer transition-colors hover:bg-g-yellow-bg/70`}
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
          <div className="px-3.5 pb-2.5 pt-1 border-t border-g-yellow/70 select-text">
            <div className="text-[12px] text-g-fg-2 leading-relaxed whitespace-pre-wrap break-words max-h-[40vh] overflow-y-auto">
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
        <div className="rounded-gmLg px-4 py-2 bg-g-bg-muted/80 border border-g-border text-g-fg-3 text-xs text-center leading-relaxed shadow-gm-sm">
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
  // 「直播中」判据（FE-13 工具行自动展开用）：ChatPanel 传入的 isStreaming
  // prop（= msg.isStreaming || 本轮 draft 命中）与消息行自带标记任一命中。
  // 持久化消息两者皆无 ⇒ 工具行永远默认收起。
  const liveNow = !!isStreaming || !!msg.isStreaming;
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
            ? "max-w-[88%] rounded-gmLg rounded-br-gm px-4 py-2.5 text-[14px] leading-relaxed text-white shadow-gm-sm"
            : "w-full max-w-[88%] rounded-gmLg rounded-tl-gm border border-g-border bg-white px-3.5 py-2.5 text-[14px] leading-relaxed text-g-fg shadow-gm-sm"
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
              className="w-5 h-5 rounded-gm flex items-center justify-center text-[10px] font-bold text-white shadow-gm-sm shrink-0"
              style={{ background: "linear-gradient(135deg, #10b981 0%, #059669 100%)" }}
            >
              AI
            </span>
            <span className="text-[11px] font-semibold text-g-fg-2 truncate min-w-0">
              {agentName || "回复"}
            </span>
            {isStreaming && !isEmpty && (
              <span className="text-[10px] text-g-green font-medium shrink-0">· 生成中</span>
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
                // live=liveNow：流式中的当前工具自动展开（EventRows）。
                return (
                  <ToolCallRow key={seg.tool.id ?? `tool-${i}`} call={seg.tool} live={liveNow} />
                );
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
                  <ToolCallRow key={tc.id ?? i} call={tc} live={liveNow} />
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
              "mt-1.5 inline-flex items-center gap-1 rounded-gm px-1.5 py-0.5 text-[11px] " +
              (msg.deliveryBadge.state === "complete"
                ? "bg-g-green-bg text-g-green"
                : "bg-g-yellow-bg text-g-yellow")
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
