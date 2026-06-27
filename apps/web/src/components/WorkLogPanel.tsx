import { useState, useEffect, useRef, useMemo } from "react";
import { getWorkLogs } from "../api";
import { useAppStore } from "../store";
import type { ActivityEntry } from "../store";

// ── Types ──────────────────────────────────────────────────────

interface WorkLog {
  id: string;
  type: string;
  summary: string;
  timestamp: number;
  details?: string;
}

const typeColors: Record<string, { bg: string; text: string }> = {
  task: { bg: "bg-blue-500/20", text: "text-blue-300" },
  decision: { bg: "bg-purple-500/20", text: "text-purple-300" },
  error: { bg: "bg-red-500/20", text: "text-red-300" },
  completion: { bg: "bg-green-500/20", text: "text-green-300" },
  delegation: { bg: "bg-amber-500/20", text: "text-amber-300" },
};

function formatTime(timestamp: number): string {
  const date = new Date(timestamp);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);

  if (diffMins < 1) return "刚刚";
  if (diffMins < 60) return `${diffMins} 分钟前`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours} 小时前`;
  return date.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

function formatClock(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ── Conversation Aggregation ───────────────────────────────────

/**
 * A conversation = all activity events between a "done" boundary and the next.
 * Each conversation aggregates: thinking + text + tool_use + tool_result.
 */
interface Conversation {
  id: string;
  agentId: string;
  agentName: string;
  startTime: number;
  endTime: number;
  events: ActivityEntry[];
  isLive: boolean;
}

function aggregateConversations(events: ActivityEntry[], filterAgentId?: string | null): Conversation[] {
  const filtered = filterAgentId
    ? events.filter((e) => e.agentId === filterAgentId)
    : events;

  const conversations: Conversation[] = [];
  let current: Conversation | null = null;

  for (const e of filtered) {
    // Start a new conversation on first event or when agent changes
    if (!current || current.agentId !== e.agentId) {
      if (current) conversations.push(current);
      current = {
        id: `${e.agentId}-${e.timestamp}`,
        agentId: e.agentId,
        agentName: e.agentName,
        startTime: e.timestamp,
        endTime: e.timestamp,
        events: [],
        isLive: true,
      };
    }

    current.events.push(e);
    current.endTime = e.timestamp;

    // "done" or "error" ends the conversation
    if (e.type === "done" || e.type === "error") {
      current.isLive = false;
      conversations.push(current);
      current = null;
    }
  }
  if (current) conversations.push(current);

  return conversations;
}

// ── Conversation Card ──────────────────────────────────────────

function ToolEntry({ entry }: { entry: ActivityEntry }) {
  const [expanded, setExpanded] = useState(false);
  const input = entry.toolInput || "";
  const result = entry.toolResult || "";

  return (
    <div className="ml-3 border-l border-amber-500/20 pl-2">
      <div
        className="flex items-center gap-1.5 cursor-pointer hover:bg-amber-500/5 rounded px-1 -mx-1 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-300 shrink-0">工具</span>
        <span className="text-[11px] text-gray-300 font-mono truncate">{entry.toolName}</span>
        {input && <span className="text-[10px] text-gray-500 truncate flex-1">{input.slice(0, 80)}{input.length > 80 ? "…" : ""}</span>}
        <span className="text-[10px] text-gray-600">{expanded ? "▲" : "▼"}</span>
      </div>
      {expanded && (
        <div className="mt-1 space-y-1">
          {input && (
            <div className="text-[10px] text-gray-400 bg-surface-alt rounded px-2 py-1 font-mono whitespace-pre-wrap break-all max-h-32 overflow-y-auto">
              <span className="text-amber-400/60">input: </span>{input}
            </div>
          )}
          {result && (
            <div className="text-[10px] text-gray-400 bg-surface-alt rounded px-2 py-1 font-mono whitespace-pre-wrap break-all max-h-32 overflow-y-auto">
              <span className="text-green-400/60">result: </span>{result}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ConversationCard({ conv }: { conv: Conversation }) {
  const [expanded, setExpanded] = useState(conv.isLive);
  const hasThinking = conv.events.some((e) => e.type === "thinking" || e.type === "thinking_delta");
  const hasText = conv.events.some((e) => e.type === "text" || e.type === "text_delta");
  const toolCount = conv.events.filter((e) => e.type === "tool_use").length;
  const hasError = conv.events.some((e) => e.type === "error");

  // If delta events exist, the finalized "text"/"thinking" event is a duplicate
  // (the backend flushes the full accumulated buffer as a final event).
  // Prefer delta entries (which accumulated incrementally) and skip the final duplicate.
  const hasTextDelta = conv.events.some((e) => e.type === "text_delta");
  const hasThinkingDelta = conv.events.some((e) => e.type === "thinking_delta");

  // Build preview: first text or thinking content (from delta if available, else finalized)
  const firstText = conv.events.find((e) => hasTextDelta ? e.type === "text_delta" : e.type === "text")?.content || "";
  const firstThink = conv.events.find((e) => hasThinkingDelta ? e.type === "thinking_delta" : e.type === "thinking")?.content || "";
  const preview = (firstText || firstThink).slice(0, 120);

  // Merge thinking content — use delta if available, skip finalized duplicate
  const thinkingContent = conv.events
    .filter((e) => hasThinkingDelta ? e.type === "thinking_delta" : (e.type === "thinking" || e.type === "thinking_delta"))
    .map((e) => e.content || "")
    .join("");

  // Merge text content — use delta if available, skip finalized duplicate
  const textContent = conv.events
    .filter((e) => hasTextDelta ? e.type === "text_delta" : (e.type === "text" || e.type === "text_delta"))
    .map((e) => e.content || "")
    .join("");

  const durationMs = conv.endTime - conv.startTime;
  const durationStr = durationMs > 1000 ? `${(durationMs / 1000).toFixed(1)}s` : "";

  return (
    <div className="border-b border-surface-border/30">
      {/* Header — always visible */}
      <div
        className="flex items-start gap-2 px-4 py-2 cursor-pointer hover:bg-surface-border/10 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        {/* Agent name + status dot */}
        <div className="flex items-center gap-1.5 shrink-0 mt-0.5">
          <span className={`w-1.5 h-1.5 rounded-full ${conv.isLive ? "bg-emerald-400 animate-pulse" : hasError ? "bg-red-400" : "bg-gray-500"}`} />
          <span className="text-[11px] font-medium text-gray-300 whitespace-nowrap">{conv.agentName}</span>
        </div>

        {/* Preview content */}
        <div className="min-w-0 flex-1">
          <div className="text-[11px] text-gray-400 truncate leading-relaxed">
            {preview}{preview.length >= 120 ? "…" : ""}
          </div>
        </div>

        {/* Badges */}
        <div className="flex items-center gap-1 shrink-0 mt-0.5">
          {hasThinking && <span className="text-[9px] px-1 py-0.5 rounded bg-purple-500/10 text-purple-300">思考</span>}
          {toolCount > 0 && <span className="text-[9px] px-1 py-0.5 rounded bg-amber-500/10 text-amber-300">{toolCount}工具</span>}
          {hasText && <span className="text-[9px] px-1 py-0.5 rounded bg-blue-500/10 text-blue-300">输出</span>}
          {durationStr && <span className="text-[9px] text-gray-600">{durationStr}</span>}
        </div>

        <span className="text-[10px] text-gray-600 shrink-0 mt-0.5">{formatClock(conv.startTime)}</span>
        <span className="text-[10px] text-gray-600 shrink-0 mt-0.5">{expanded ? "▲" : "▼"}</span>
      </div>

      {/* Expanded content — full conversation */}
      {expanded && (
        <div className="px-4 pb-2 space-y-1.5">
          {/* Thinking block */}
          {thinkingContent && (
            <ThinkingBlock content={thinkingContent} />
          )}

          {/* Events in order */}
          {conv.events
            .filter((e) => e.type === "tool_use" || e.type === "tool_result")
            .map((e, i) => (
              <ToolEntry key={`tool-${i}`} entry={e} />
            ))}

          {/* Text output block */}
          {textContent && (
            <div className="ml-3 border-l border-blue-500/20 pl-2">
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-300 shrink-0">输出</span>
              </div>
              <div className="mt-1 text-[11px] text-gray-300 leading-relaxed whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
                {textContent}
              </div>
            </div>
          )}

          {/* Error */}
          {conv.events.filter((e) => e.type === "error").map((e, i) => (
            <div key={`err-${i}`} className="ml-3 border-l border-red-500/20 pl-2">
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-red-500/10 text-red-300 shrink-0">错误</span>
              </div>
              <div className="mt-1 text-[11px] text-red-300 leading-relaxed">
                {e.errorMessage}
              </div>
            </div>
          ))}

          {/* Done marker */}
          {!conv.isLive && !hasError && (
            <div className="flex items-center gap-1.5 ml-3">
              <span className="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-300">完成</span>
              <span className="text-[9px] text-gray-600">{formatTime(conv.endTime)}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ThinkingBlock({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="ml-3 border-l border-purple-500/20 pl-2">
      <div
        className="flex items-center gap-1.5 cursor-pointer hover:bg-purple-500/5 rounded px-1 -mx-1 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-300 shrink-0">思考</span>
        {!expanded && (
          <span className="text-[11px] text-gray-400 truncate flex-1">
            {content.slice(0, 120)}{content.length > 120 ? "…" : ""}
          </span>
        )}
        <span className="text-[10px] text-gray-600 shrink-0">{expanded ? "▲" : "▼"}</span>
      </div>
      {expanded && (
        <div className="mt-1 text-[11px] text-gray-400 leading-relaxed whitespace-pre-wrap break-words max-h-48 overflow-y-auto bg-surface-alt rounded px-2 py-1.5">
          {content}
        </div>
      )}
    </div>
  );
}

// ── Live Activity Panel ─────────────────────────────────────────

export function ActivityLog({ agentId }: { agentId?: string | null }) {
  const activityFeed = useAppStore((s) => s.activityFeed);
  const clearActivity = useAppStore((s) => s.clearActivity);
  const bottomRef = useRef<HTMLDivElement>(null);

  const conversations = useMemo(
    () => aggregateConversations(activityFeed, agentId),
    [activityFeed, agentId],
  );

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [conversations.length]);

  if (conversations.length === 0) {
    return (
      <div className="border-t border-surface-border bg-surface-card shrink-0">
        <div className="px-6 py-4 flex items-center justify-between border-b border-surface-border">
          <span className="text-xs font-medium text-gray-400">Live Activity</span>
        </div>
        <div className="px-6 py-8 text-center">
          <p className="text-sm text-gray-500">暂无活动</p>
        </div>
      </div>
    );
  }

  return (
    <div className="border-t border-surface-border bg-surface-card shrink-0">
      {/* Header */}
      <div className="px-4 py-2 flex items-center justify-between border-b border-surface-border">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-gray-400">Live Activity</span>
          {conversations.some((c) => c.isLive) && (
            <span className="flex items-center gap-1 text-[10px] text-emerald-400">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              {conversations.filter((c) => c.isLive).length} 进行中
            </span>
          )}
        </div>
        <button onClick={clearActivity} className="text-xs text-gray-500 hover:text-gray-300 transition-colors">清空</button>
      </div>

      {/* Conversation list */}
      <div className="max-h-96 overflow-y-auto">
        {conversations.map((conv) => (
          <ConversationCard key={conv.id} conv={conv} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

// ── Work Log Panel ──────────────────────────────────────────────

function WorkLogPanel({ agentId }: { agentId: string | null }) {
  const [isOpen, setIsOpen] = useState(true);
  const [logs, setLogs] = useState<WorkLog[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!agentId || !isOpen) {
      setLogs([]);
      return;
    }

    async function fetchLogs() {
      setLoading(true);
      try {
        const data = await getWorkLogs(agentId!, 10);
        setLogs(data.logs || data || []);
      } catch (err) {
        console.error("Failed to fetch work logs:", err);
        setLogs([]);
      } finally {
        setLoading(false);
      }
    }

    fetchLogs();
  }, [agentId, isOpen]);

  if (!agentId) {
    return <ActivityLog />;
  }

  return (
    <div className="border-t border-surface-border bg-surface-card shrink-0">
      {/* Toggle Header */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full px-6 py-3 flex items-center justify-between hover:bg-surface-border/30 transition-colors"
      >
        <div className="flex items-center gap-2">
          <svg
            className="w-4 h-4 text-gray-400"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"
            />
          </svg>
          <span className="text-sm font-medium text-gray-300">Work Logs</span>
          {logs.length > 0 && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-surface-border text-gray-400">
              {logs.length}
            </span>
          )}
        </div>
        <svg
          className={`w-4 h-4 text-gray-400 transition-transform ${isOpen ? "rotate-180" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Log Content */}
      {isOpen && (
        <div className="max-h-64 overflow-y-auto border-t border-surface-border">
          {loading ? (
            <div className="px-6 py-4 flex justify-center">
              <div className="flex gap-1">
                <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
              </div>
            </div>
          ) : logs.length === 0 ? (
            <div className="px-6 py-8 text-center">
              <p className="text-sm text-gray-500">暂无工作日志</p>
            </div>
          ) : (
            <div className="divide-y divide-surface-border">
              {logs.map((log) => {
                const typeInfo = typeColors[log.type] || typeColors.task;
                return (
                  <div key={log.id} className="px-6 py-3 hover:bg-surface-border/20 transition-colors">
                    <div className="flex items-start gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <span
                            className={`text-[10px] font-medium px-2 py-0.5 rounded-full ${typeInfo.bg} ${typeInfo.text}`}
                          >
                            {log.type}
                          </span>
                          <span className="text-xs text-gray-500">
                            {formatTime(log.timestamp)}
                          </span>
                        </div>
                        <p className="text-sm text-gray-300 line-clamp-2">
                          {log.summary}
                        </p>
                        {log.details && (
                          <p className="text-xs text-gray-500 mt-1 line-clamp-2">
                            {log.details}
                          </p>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
      <ActivityLog agentId={agentId} />
    </div>
  );
}

/**
 * Standalone Live Activity bar — shows ALL agents' activity (no filter).
 * Designed to sit at the bottom of the left panel (Org Tree area).
 */
export function ActivityLogBar() {
  return <ActivityLog agentId={null} />;
}

export default WorkLogPanel;
