import { useState } from "react";
import type { ChatMessage, ToolCall } from "./types";
import { CHAT_MOTION_CSS, toolCategories } from "./constants";
import { formatToolInputHint } from "./messageUtils";

function ToolCallsBlock({ toolCalls }: { toolCalls: ToolCall[] }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded-lg border border-g-border bg-g-bg-muted/70 overflow-hidden shadow-gm-sm">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left text-[11px] text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted transition-colors"
      >
        <svg className={`w-3 h-3 text-g-fg-4 transition-transform duration-200 ${expanded ? "rotate-90" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
        <svg className="w-3.5 h-3.5 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085" />
        </svg>
        <span className="font-medium">工具调用</span>
        <span className="ml-auto text-[10px] font-semibold text-g-fg-3 bg-g-bg border border-g-border rounded-full px-1.5 py-px leading-none">{toolCalls.length}</span>
      </button>
      {expanded && (
        <div className="border-t border-g-border px-3 py-2 space-y-1">
          {toolCalls.map((tc, i) => {
            const hint = formatToolInputHint(tc.tool, tc.input);
            const cat = toolCategories[tc.tool];
            const dot = cat ? cat.color.replace("text-", "bg-") : "bg-amber-500";
            return (
              <div key={i} className="flex items-center gap-2 text-[11px] font-mono">
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dot}`} />
                <span className="text-g-fg-2">{tc.tool}</span>
                {hint && <span className="text-g-fg-4 truncate">— {hint}</span>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

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

function ToolCallInline({ name, input }: { name: string; input?: Record<string, any> }) {
  const [showArgs, setShowArgs] = useState(false);
  const hint = formatToolInputHint(name, input);
  let argsPreview = "";
  try {
    if (input && typeof input === "object" && Object.keys(input).length > 0) {
      const entries = Object.entries(input).slice(0, 3);
      argsPreview = entries.map(([k, v]) => {
        const val = typeof v === "string" ? (v.length > 50 ? v.slice(0, 50) + "…" : v) : JSON.stringify(v).slice(0, 50);
        return `${k}=${val}`;
      }).join(", ");
    }
  } catch { /* ignore */ }
  const cat = toolCategories[name];
  const catDot = cat ? cat.color.replace("text-", "bg-") : "bg-g-fg-4";
  return (
    <div className="py-1.5 px-3 my-1 rounded-lg border border-g-border bg-g-bg-muted/60 text-[12px] transition-colors hover:bg-g-bg-muted hover:border-g-border-strong">
      <div className="flex items-center gap-2 cursor-pointer select-none" onClick={() => setShowArgs(!showArgs)}>
        <svg className={`w-3 h-3 text-g-fg-4 transition-transform duration-200 ${showArgs ? "rotate-90" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${catDot}`} />
        <span className="font-medium text-g-fg-2 font-mono text-[11px]">{name}</span>
        {hint && <span className="text-g-fg-4 text-[11px] truncate">→ {hint}</span>}
        {argsPreview && <span className="text-g-fg-4/70 text-[10px] ml-auto truncate hidden sm:inline">{argsPreview}</span>}
      </div>
      {showArgs && input && Object.keys(input).length > 0 && (
        <pre className="mt-1.5 text-[10px] text-amber-600 whitespace-pre-wrap break-all font-mono leading-relaxed pl-5 border-l border-g-border">
          {JSON.stringify(input, null, 2)}
        </pre>
      )}
    </div>
  );
}

export function MessageBubble({ msg, isStreaming, thinkingElapsed }: { msg: ChatMessage; isStreaming?: boolean; thinkingElapsed?: number | null }) {
  if (msg.role === "system") {
    return (
      <div className="flex justify-center my-4 hw-msg-in">
        <div className="rounded-xl px-4 py-2 bg-g-bg-muted/80 border border-g-border text-g-fg-3 text-xs text-center leading-relaxed shadow-gm-sm">
          <p className="whitespace-pre-wrap">{msg.content}</p>
        </div>
      </div>
    );
  }

  const segments = msg._segments || [];
  const hasSegments = segments.length > 0;
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
        style={isUser ? { background: "linear-gradient(135deg, #4a8bff 0%, #4285f4 55%, #3574e2 100%)" } : undefined}
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
            {segments.map((seg, i) => {
              if (seg.type === "thinking" && seg.content) {
                return <ThinkingBlock key={i} content={seg.content} />;
              }
              if (seg.type === "text" && seg.content) {
                return <p key={i} className="whitespace-pre-wrap">{seg.content}</p>;
              }
              if (seg.type === "tool_call" && seg.tool) {
                return <ToolCallInline key={i} name={seg.tool.tool} input={seg.tool.input} />;
              }
              return null;
            })}
          </div>
        ) : (
          <>
            {!isUser && thinking && <ThinkingBlock content={thinking} />}
            {msg.content && <p className="whitespace-pre-wrap">{msg.content}</p>}
            {!isUser && msg.toolCalls && msg.toolCalls.length > 0 && (
              <div className="mt-2">
                <ToolCallsBlock toolCalls={msg.toolCalls} />
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

export function ChatMotionStyles() {
  return <style>{CHAT_MOTION_CSS}</style>;
}
