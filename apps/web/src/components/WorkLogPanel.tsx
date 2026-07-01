import { useState, useEffect, useRef, useMemo } from "react";
import { getWorkLogs } from "../api";
import { useAppStore } from "../store";
import type { ActivityEntry } from "../store";
import { mergeContentChunks } from "../utils/mergeDelta";

// ── Types ──────────────────────────────────────────────────────

interface WorkLog {
  id: string;
  type: string;
  summary: string;
  createdAt: number;
  details?: string;
}

const typeColors: Record<string, { bg: string; text: string }> = {
  task: { bg: "bg-blue-500/20", text: "text-blue-300" },
  decision: { bg: "bg-purple-500/20", text: "text-purple-300" },
  error: { bg: "bg-red-500/20", text: "text-red-300" },
  completion: { bg: "bg-green-500/20", text: "text-green-300" },
  discussion: { bg: "bg-sky-500/20", text: "text-sky-300" },
  delegation: { bg: "bg-amber-500/20", text: "text-amber-300" },
};

/** Friendly Chinese labels for log types */
const typeLabels: Record<string, string> = {
  task: "任务",
  decision: "决策",
  error: "异常",
  completion: "完成",
  discussion: "派单",
  delegation: "委派",
};

/** Extract a clean one-line summary (first line, max 100 chars) */
function cleanSummary(raw: string): string {
  // Strip leading markdown headers
  const firstLine = raw.replace(/^#+\s*/, "").split("\n")[0].trim();
  return firstLine.length > 100 ? firstLine.slice(0, 100) + "…" : firstLine;
}

/** Try to pretty-print JSON string; return null if not JSON */
function tryPrettyJson(raw: string): string | null {
  try {
    const parsed = JSON.parse(raw);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return null;
  }
}

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

/** Normalize tool input/result that may arrive as string OR object. The Elixir
 *  streamer sends a string but the lobby_channel forwards the raw `input` object
 *  from the SSE stream_event — both reach the activity feed. */
function asString(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function ToolEntry({ entry }: { entry: ActivityEntry }) {
  const [expanded, setExpanded] = useState(false);
  const input = asString(entry.toolInput);
  const result = asString(entry.toolResult);
  const inputPreview = input.length > 80 ? input.slice(0, 80) + "…" : input;

  return (
    <div className="ml-3 border-l border-amber-500/20 pl-2">
      <div
        className="flex items-center gap-1.5 cursor-pointer hover:bg-amber-500/5 rounded px-1 -mx-1 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-300 shrink-0">工具</span>
        <span className="text-[11px] text-gray-300 font-mono truncate">{entry.toolName}</span>
        {inputPreview && <span className="text-[10px] text-gray-500 truncate flex-1">{inputPreview}</span>}
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
  const hasError = conv.events.some((e) => e.type === "error");
  const toolCount = conv.events.filter((e) => e.type === "tool_use").length;

  // Cross-deltaId merge + LLM cumulative-chunk dedup. See mergeAcrossDeltaIds.
  const mergedText = useMemo(() => mergeAcrossDeltaIds(conv.events, "text_delta", "text"), [conv.events]);
  const mergedThinking = useMemo(() => mergeAcrossDeltaIds(conv.events, "thinking_delta", "thinking"), [conv.events]);

  const firstLine = (mergedText || mergedThinking).replace(/\n+/g, " ").trim();
  const preview = firstLine.length > 120 ? firstLine.slice(0, 120) + "…" : firstLine;
  const hasThinking = !!mergedThinking;
  const hasText = !!mergedText;

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
            {preview || (conv.isLive ? "正在处理…" : "")}
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
          {mergedThinking && (
            <ThinkingBlock content={mergedThinking} />
          )}

          {/* Events in order */}
          {conv.events
            .filter((e) => e.type === "tool_use" || e.type === "tool_result")
            .map((e, i) => (
              <ToolEntry key={`tool-${i}`} entry={e} />
            ))}

          {/* Text output block */}
          {mergedText && (
            <div className="ml-3 border-l border-blue-500/20 pl-2">
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-300 shrink-0">输出</span>
              </div>
              <div className="mt-1 text-[11px] text-gray-300 leading-relaxed whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
                {mergedText}
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

// ── Work Log Entry ──────────────────────────────────────────────

function WorkLogEntry({ log }: { log: WorkLog }) {
  const [expanded, setExpanded] = useState(false);
  const typeInfo = typeColors[log.type] || typeColors.task;
  const label = typeLabels[log.type] || log.type;
  const summary = cleanSummary(log.summary);

  // Determine if details have meaningful content
  const detailsRaw = log.details;
  const hasDetails = detailsRaw && detailsRaw !== "{}" && detailsRaw !== "";
  const prettyDetails = hasDetails ? (tryPrettyJson(detailsRaw) || detailsRaw) : null;

  // Check if full summary is longer than the cleaned one-line version
  const hasMoreSummary = log.summary.trim() !== summary;
  const canExpand = hasDetails || hasMoreSummary;

  return (
    <div className="px-4 py-3 hover:bg-surface-border/20 transition-colors">
      <div className="flex items-center gap-2 mb-1">
        <span className={`text-xs font-medium px-1.5 py-0.5 rounded ${typeInfo.bg} ${typeInfo.text}`}>
          {label}
        </span>
        <span className="text-xs text-gray-500">{formatTime(log.createdAt)}</span>
        {canExpand && (
          <button
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
            className="ml-auto text-xs text-gray-500 hover:text-gray-300 transition-colors px-2 py-0.5 rounded hover:bg-surface-border/40"
          >
            {expanded ? "收起 ▲" : "详情 ▼"}
          </button>
        )}
      </div>
      <p className="text-sm text-gray-300 leading-relaxed line-clamp-2">{summary}</p>

      {/* Expanded: full summary + details */}
      {expanded && (
        <div className="mt-2 space-y-2">
          {hasMoreSummary && (
            <div className="text-xs text-gray-400 bg-surface-alt rounded px-3 py-2 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
              {log.summary}
            </div>
          )}
          {prettyDetails && (
            <div className="text-xs text-gray-400 bg-surface-alt rounded px-3 py-2 font-mono whitespace-pre-wrap break-all max-h-56 overflow-y-auto">
              {prettyDetails}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Live Activity Panel ─────────────────────────────────────────

export function ActivityLog({ agentId }: { agentId?: string | null }) {
  const activityFeed = useAppStore((s) => s.activityFeed);
  const clearActivity = useAppStore((s) => s.clearActivity);

  const conversations = useMemo(
    () => aggregateConversations(activityFeed, agentId),
    [activityFeed, agentId],
  );

  // Group by agent: latest conversation first, plus a count of older turns
  const agentRows = useMemo(() => {
    const map = new Map<string, { latest: Conversation; olderCount: number }>();
    for (let i = conversations.length - 1; i >= 0; i--) {
      const c = conversations[i];
      const existing = map.get(c.agentId);
      if (!existing) {
        map.set(c.agentId, { latest: c, olderCount: 0 });
      } else {
        existing.olderCount++;
      }
    }
    return [...map.values()].sort((a, b) => {
      if (a.latest.isLive !== b.latest.isLive) return a.latest.isLive ? -1 : 1;
      return b.latest.endTime - a.latest.endTime;
    });
  }, [conversations]);

  const liveCount = agentRows.filter((r) => r.latest.isLive).length;

  if (agentRows.length === 0) {
    return (
      <div className="border-t border-surface-border bg-surface-card shrink-0">
        <div className="px-4 py-2 flex items-center justify-between">
          <span className="text-xs font-medium text-gray-500">Live Activity</span>
          <span className="text-xs text-gray-600">空闲</span>
        </div>
      </div>
    );
  }

  return (
    <div className="border-t border-surface-border bg-surface-card shrink-0">
      {/* Header */}
      <div className="px-4 py-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-gray-500">Live Activity</span>
          {liveCount > 0 && (
            <span className="flex items-center gap-1.5 text-xs text-emerald-400">
              <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              {liveCount} 运行中
            </span>
          )}
        </div>
        <button onClick={clearActivity} className="text-xs text-gray-500 hover:text-gray-300 transition-colors">清空</button>
      </div>

      {/* Agent rows — click to expand */}
      <div className="px-4 pb-3 space-y-1">
        {agentRows.map(({ latest: conv, olderCount }) => (
          <ActivityRow key={conv.agentId} conv={conv} olderCount={olderCount} />
        ))}
      </div>
    </div>
  );
}

function ActivityRow({ conv, olderCount }: { conv: Conversation; olderCount: number }) {
  const [expanded, setExpanded] = useState(true);
  const hasError = conv.events.some((e) => e.type === "error");

  // Merge text/thinking deltas across all deltaIds. The store already collapses
  // chunks with the same deltaId; here we join across deltaIds for the final view
  // and detect LLM-style cumulative chunks (next supersedes prev on prefix-match).
  const mergedText = useMemo(() => mergeAcrossDeltaIds(conv.events, "text_delta", "text"), [conv.events]);
  const mergedThinking = useMemo(() => mergeAcrossDeltaIds(conv.events, "thinking_delta", "thinking"), [conv.events]);

  const toolUseEvents = conv.events.filter((e) => e.type === "tool_use");
  const toolCount = toolUseEvents.length;
  const firstLine = (mergedText || mergedThinking).replace(/\n+/g, " ").trim();
  const preview = firstLine.length > 80 ? firstLine.slice(0, 80) + "…" : firstLine;

  return (
    <div className="rounded-lg bg-surface-alt/40 border border-surface-border/40">
      {/* Compact row — always visible, mirrors TRAE Work's "Agent · action" header */}
      <div
        className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-surface-alt/60 transition-colors rounded-t-lg"
        onClick={() => setExpanded(!expanded)}
      >
        <span className={`w-2 h-2 rounded-full shrink-0 ${conv.isLive ? "bg-emerald-400 animate-pulse" : hasError ? "bg-red-400" : "bg-gray-500"}`} />
        <span className="text-sm font-medium text-gray-200 truncate">{conv.agentName}</span>
        {conv.isLive && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300 shrink-0">运行中</span>
        )}
        {toolCount > 0 && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-300 shrink-0">{toolCount} 工具</span>
        )}
        {olderCount > 0 && (
          <span className="text-[10px] text-gray-600 shrink-0">+{olderCount}</span>
        )}
        <span className="text-[10px] text-gray-500 ml-auto shrink-0">{formatClock(conv.startTime)}</span>
        <span className="text-[10px] text-gray-600 shrink-0">{expanded ? "▲" : "▼"}</span>
      </div>

      {!expanded && preview && (
        <p className="px-3 pb-2 text-xs text-gray-500 truncate pl-7">{preview}</p>
      )}

      {/* Expanded — clean step list (think → tools → output), TRAE Work style */}
      {expanded && (
        <div className="px-3 pb-3 pl-7 space-y-1.5">
          {mergedThinking && (
            <details className="group">
              <summary className="text-[11px] text-purple-300 cursor-pointer list-none flex items-center gap-1.5 select-none">
                <span className="text-[9px] text-gray-600 group-open:rotate-90 transition-transform">▶</span>
                <span>思考</span>
                <span className="text-[9px] text-gray-600">{mergedThinking.length} 字符</span>
              </summary>
              <div className="mt-1 text-[11px] text-gray-400 bg-surface-alt/70 rounded px-2.5 py-1.5 whitespace-pre-wrap break-words max-h-32 overflow-y-auto leading-relaxed">
                {mergedThinking}
              </div>
            </details>
          )}

          {toolUseEvents.map((e, i) => (
            <ToolEntry key={`tool-${i}`} entry={e} />
          ))}

          {mergedText && (
            <details className="group" open={conv.isLive}>
              <summary className="text-[11px] text-blue-300 cursor-pointer list-none flex items-center gap-1.5 select-none">
                <span className="text-[9px] text-gray-600 group-open:rotate-90 transition-transform">▶</span>
                <span>输出</span>
                <span className="text-[9px] text-gray-600">{mergedText.length} 字符</span>
              </summary>
              <div className="mt-1 text-[12px] text-gray-200 bg-surface-alt/70 rounded px-2.5 py-2 whitespace-pre-wrap break-words max-h-64 overflow-y-auto leading-relaxed">
                {mergedText}
              </div>
            </details>
          )}

          {conv.events.filter((e) => e.type === "error").map((e, i) => (
            <div key={`err-${i}`}>
              <span className="text-[11px] text-red-300 mb-1 block">错误</span>
              <div className="text-[11px] text-red-300 bg-red-500/10 rounded px-2.5 py-1.5">
                {e.errorMessage}
              </div>
            </div>
          ))}

          {!conv.isLive && !hasError && (
            <div className="text-[10px] text-emerald-400 pt-0.5">完成 · {formatTime(conv.endTime)}</div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Merge text/thinking content across all deltaIds in a conversation.
 * Handles three cases the store's per-deltaId merge does not:
 *   1) Multiple rounds → multiple deltaIds → join the merged entries.
 *   2) LLM/SDK cumulative chunk (next chunk contains prev as prefix) → drop prev.
 *   3) Finalized "text" event present alongside deltas → skip (deltas already full).
 */
function mergeAcrossDeltaIds(
  events: ActivityEntry[],
  deltaType: "text_delta" | "thinking_delta",
  fallbackType: "text" | "thinking",
): string {
  const hasDeltas = events.some((e) => e.type === deltaType);
  const matching = events.filter((e) =>
    hasDeltas ? e.type === deltaType : e.type === deltaType || e.type === fallbackType,
  );
  if (matching.length === 0) return "";
  const texts = matching.map((e) => e.content || "").filter(Boolean);
  return mergeContentChunks(texts);
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
        <div className="max-h-80 overflow-y-auto border-t border-surface-border">
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
            <div className="divide-y divide-surface-border/50">
              {logs.map((log) => (
                <WorkLogEntry key={log.id} log={log} />
              ))}
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
