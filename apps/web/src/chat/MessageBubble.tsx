import { memo, useState } from "react";
import type { ChatMessage, ToolCall } from "./types";
import { CHAT_MOTION_CSS, toolCategories } from "./constants";
import { formatToolInputHint } from "./messageUtils";

function ThinkingBlock({ content }: { content: string }) {
  return (
    <details className="group/think my-3 overflow-hidden rounded-xl border border-purple-200/70 bg-purple-50/50 shadow-gm-sm">
      <summary className="flex items-center gap-2 px-3 py-2 cursor-pointer select-none hover:bg-purple-100/40 transition-colors">
        <svg className="w-3.5 h-3.5 text-purple-500 group-open/think:rotate-90 transition-transform duration-200" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
        <span className="w-5 h-5 rounded-md bg-purple-500/15 flex items-center justify-center shrink-0">
          <svg className="w-3.5 h-3.5 text-purple-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
          </svg>
        </span>
        <span className="text-xs font-medium text-purple-600">思考过程</span>
        <span className="text-[10px] text-purple-400/90 ml-auto">{content.length} 字</span>
      </summary>
      <div className="border-t border-purple-200/60 bg-white/60 px-3 py-2.5">
        <div className="text-xs text-g-fg-3 whitespace-pre-wrap break-words max-h-64 overflow-y-auto leading-relaxed font-mono text-[11px]">
          {content}
        </div>
      </div>
    </details>
  );
}

function ToolStatusIcon({ status }: { status?: ToolCall["status"] }) {
  if (status === "running" || !status) {
    return (
      <svg className="w-3 h-3 text-g-blue animate-spin shrink-0" fill="none" viewBox="0 0 24 24">
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
      </svg>
    );
  }
  if (status === "error") {
    return (
      <svg className="w-3 h-3 text-red-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
      </svg>
    );
  }
  return (
    <svg className="w-3 h-3 text-emerald-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
    </svg>
  );
}

// 结果二次截断兜底：后端流式 500 / 落库 2000 已截，此处再防
// legacy/异常大 payload 撑爆 DOM（阈值与后端 TOOL_RESULT_PERSIST_EXCERPT 对齐）。
const RESULT_RENDER_MAX = 2000;

function ToolCallRow({ call }: { call: ToolCall }) {
  const [showDetail, setShowDetail] = useState(false);
  const hint = formatToolInputHint(call.tool, call.input);
  const cat = toolCategories[call.tool];
  const catDot = cat ? cat.color.replace("text-", "bg-") : "bg-g-fg-4";
  const hasDetail =
    (call.input && Object.keys(call.input).length > 0) || !!call.result;
  const resultText =
    call.result && call.result.length > RESULT_RENDER_MAX
      ? call.result.slice(0, RESULT_RENDER_MAX) + "\n…（结果已截断）"
      : call.result;
  return (
    <div className="py-1.5 px-3 my-1 rounded-lg border border-g-border bg-g-bg-muted/60 text-[12px] transition-colors hover:bg-g-bg-muted hover:border-g-border-strong">
      <div
        className="flex items-center gap-2 cursor-pointer select-none"
        onClick={() => hasDetail && setShowDetail(!showDetail)}
      >
        <svg
          className={`w-3 h-3 text-g-fg-4 transition-transform duration-200 shrink-0 ${showDetail ? "rotate-90" : ""}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${catDot}`} />
        <span className="font-medium text-g-fg-2 font-mono text-[11px]">{call.tool}</span>
        {hint && <span className="text-g-fg-4 text-[11px] truncate">→ {hint}</span>}
        <span className="ml-auto flex items-center gap-1.5 shrink-0">
          <ToolStatusIcon status={call.status} />
        </span>
      </div>
      {showDetail && hasDetail && (
        <div className="mt-1.5 pl-5 space-y-1.5">
          {call.input && Object.keys(call.input).length > 0 && (
            <pre className="text-[10px] text-amber-600 whitespace-pre-wrap break-all font-mono leading-relaxed border-l border-g-border pl-2 max-h-56 overflow-y-auto">
              {JSON.stringify(call.input, null, 2)}
            </pre>
          )}
          {resultText && (
            <pre className={`text-[10px] whitespace-pre-wrap break-all font-mono leading-relaxed border-l pl-2 max-h-56 overflow-y-auto ${call.status === "error" ? "text-red-600 border-red-300" : "text-g-fg-3 border-g-border"}`}>
              {resultText}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

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

/** 非真人入站消息（agent 来信 / 系统注入 / 看门狗唤醒 digest）。 */
function InboundLetter({ msg, sourceName }: { msg: ChatMessage; sourceName?: string }) {
  const long = (msg.content?.length || 0) > 600 || msg.isContext === true;
  const [open, setOpen] = useState(!long);
  const source: "agent" | "system" | "watchdog" =
    msg.source === "agent" || msg.source === "watchdog" ? msg.source : "system";
  const time = new Date(msg.timestamp).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  return (
    <div className="flex justify-start my-2 hw-msg-in">
      <div className="w-full max-w-full rounded-2xl rounded-bl-md border border-g-border bg-white shadow-gm-sm overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-1.5 bg-g-bg-soft border-b border-g-border">
          <SourceBadge source={source} />
          {sourceName && (
            <span className="text-xs font-medium text-g-fg-2 truncate">{sourceName}</span>
          )}
          <span className="text-[10px] text-g-fg-4 ml-auto shrink-0">{time}</span>
        </div>
        <div className="px-3.5 py-2">
          <div
            className={`text-[13px] text-g-fg-2 leading-relaxed whitespace-pre-wrap break-words ${open ? "" : "line-clamp-4"}`}
          >
            {msg.content || "（无正文）"}
          </div>
          {long && (
            <button
              type="button"
              onClick={() => setOpen(!open)}
              className="mt-1 text-[11px] font-medium text-g-blue hover:text-indigo-700"
            >
              {open ? "收起" : "展开全文"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function MessageBubbleInner({ msg, isStreaming, thinkingElapsed, sourceName }: {
  msg: ChatMessage;
  isStreaming?: boolean;
  thinkingElapsed?: number | null;
  sourceName?: string;
}) {
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
  // 只在 _thinking 列（build_display_segments 不产 thinking 段）。
  // segments 已含 thinking 时跳过 _thinking，避免双渲染。
  const segmentsHaveThinking = segments.some((s) => s.type === "thinking" && s.content);
  const thinking = msg._thinking || "";

  const isUser = msg.role === "user";
  const isEmpty = !msg.content && !thinking && !hasSegments && (!msg.toolCalls || msg.toolCalls.length === 0);

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} my-2 hw-msg-in`}>
      <div
        className={isUser
          ? "max-w-[78%] rounded-2xl rounded-br-md px-4 py-2.5 text-[14px] leading-relaxed text-white shadow-gm-sm"
          : "w-full max-w-full text-[15px] leading-relaxed text-g-fg"
        }
        style={isUser ? { background: "linear-gradient(135deg, #5b54e8 0%, #4f46e5 55%, #4338ca 100%)" } : undefined}
      >
        {msg.images && msg.images.length > 0 && (
          <div className="flex gap-1.5 flex-wrap mb-3">
            {msg.images.map((url, i) => (
              <img key={i} src={url} className={`max-h-48 max-w-[200px] rounded-lg object-cover ${isUser ? "ring-1 ring-white/30" : "border border-g-border"}`} alt="" />
            ))}
          </div>
        )}

        {hasSegments ? (
          <div className="space-y-1">
            {!isUser && thinking && !segmentsHaveThinking && <ThinkingBlock content={thinking} />}
            {segments.map((seg, i) => {
              if (seg.type === "thinking" && seg.content) {
                return <ThinkingBlock key={i} content={seg.content} />;
              }
              if (seg.type === "text" && seg.content) {
                return <p key={i} className="whitespace-pre-wrap">{seg.content}</p>;
              }
              if (seg.type === "tool_call" && seg.tool) {
                return <ToolCallRow key={i} call={seg.tool} />;
              }
              return null;
            })}
          </div>
        ) : (
          <>
            {!isUser && thinking && <ThinkingBlock content={thinking} />}
            {msg.content && <p className="whitespace-pre-wrap">{msg.content}</p>}
            {!isUser && msg.toolCalls && msg.toolCalls.length > 0 && (
              <div className="mt-2 space-y-0.5">
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

        {!isUser && isEmpty && isStreaming && (
          <div className="flex items-center gap-2.5 py-1">
            {thinkingElapsed != null ? (
              <>
                <span className="flex gap-1.5">
                  {[0, 160, 320].map((d) => (
                    <span key={d} className="w-2 h-2 rounded-full bg-g-blue hw-typing-dot" style={{ animationDelay: `${d}ms` }} />
                  ))}
                </span>
                <span className="text-xs font-medium hw-thinking-shimmer">
                  思考中{thinkingElapsed > 0 ? ` · ${Math.floor(thinkingElapsed)}s` : ""}…
                </span>
              </>
            ) : (
              <span className="flex gap-1.5">
                {[0, 160, 320].map((d) => (
                  <span key={d} className="w-2 h-2 rounded-full bg-g-fg-4 hw-typing-dot" style={{ animationDelay: `${d}ms` }} />
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
