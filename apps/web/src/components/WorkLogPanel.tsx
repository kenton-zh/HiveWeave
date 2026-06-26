import { useState, useEffect, useRef } from "react";
import { getWorkLogs } from "../api";
import { useAppStore } from "../store";
import type { ActivityEntry } from "../store";

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

const activityColors: Record<string, { bg: string; text: string; icon: string }> = {
  thinking: { bg: "bg-purple-500/10", text: "text-purple-300", icon: "思考" },
  text: { bg: "bg-blue-500/10", text: "text-blue-300", icon: "文本" },
  tool_use: { bg: "bg-amber-500/10", text: "text-amber-300", icon: "工具" },
  tool_result: { bg: "bg-green-500/10", text: "text-green-300", icon: "结果" },
  done: { bg: "bg-emerald-500/10", text: "text-emerald-300", icon: "完成" },
  error: { bg: "bg-red-500/10", text: "text-red-300", icon: "错误" },
};

/** Merge thinking events per conversation — tool calls don't break the session. */
function mergeThinkingSessions(events: ActivityEntry[]): (ActivityEntry & { isSession?: boolean })[] {
  const merged: (ActivityEntry & { isSession?: boolean })[] = [];
  let currentSession: (ActivityEntry & { isSession?: boolean }) | null = null;

  for (const e of events) {
    if (e.type === "thinking") {
      if (currentSession) {
        // Append to current thinking session
        currentSession.content = (currentSession.content || "") + (e.content || "");
        currentSession.timestamp = e.timestamp;
      } else {
        currentSession = { ...e, content: e.content || "", isSession: true };
        merged.push(currentSession);
      }
    } else if (e.type === "done" || e.type === "error") {
      // Conversation boundary — close the session
      currentSession = null;
      merged.push(e);
    } else {
      // tool_use, tool_result, text — pass through, don't break thinking session
      merged.push(e);
    }
  }
  return merged;
}

function ThinkingSession({ entry }: { entry: ActivityEntry }) {
  const [expanded, setExpanded] = useState(false);
  const content = entry.content || "";
  const preview = content.slice(0, 100);

  return (
    <div className="px-4 py-1.5 border-b border-surface-border/30">
      <div
        className="flex items-start gap-2 cursor-pointer hover:bg-surface-border/10 rounded px-1 -mx-1 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="text-[10px] font-medium px-1.5 py-0.5 rounded whitespace-nowrap bg-purple-500/10 text-purple-300 shrink-0 mt-0.5">
          思考
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-gray-500">{entry.agentName}</span>
            <span className="text-[10px] text-gray-600">{formatTime(entry.timestamp)}</span>
            <span className="text-[10px] text-gray-500">{expanded ? "▲" : "▼"}</span>
          </div>
          {!expanded && (
            <div className="text-[11px] text-gray-400 truncate mt-0.5 leading-relaxed">
              {preview}{content.length > 100 ? "…" : ""}
            </div>
          )}
        </div>
      </div>
      {expanded && (
        <div className="mt-1.5 ml-6 p-2 rounded bg-surface-alt border border-surface-border text-[11px] text-gray-300 leading-relaxed whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
          {content}
        </div>
      )}
    </div>
  );
}

function ActivityLog({ agentId }: { agentId?: string | null }) {
  const activityFeed = useAppStore((s) => s.activityFeed);
  const clearActivity = useAppStore((s) => s.clearActivity);
  const bottomRef = useRef<HTMLDivElement>(null);

  const filtered = agentId ? activityFeed.filter((e) => e.agentId === agentId) : activityFeed;
  const merged = mergeThinkingSessions(filtered);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [merged.length]);

  if (merged.length === 0) return null;

  return (
    <div className="border-t border-surface-border bg-surface-card shrink-0">
      <div className="px-6 py-2 flex items-center justify-between border-b border-surface-border">
        <span className="text-xs font-medium text-gray-400">Live Activity</span>
        <button onClick={clearActivity} className="text-xs text-gray-500 hover:text-gray-300 transition-colors">Clear</button>
      </div>
      <div className="max-h-80 overflow-y-auto">
        {merged.map((e, i) => {
          if (e.isSession) {
            return <ThinkingSession key={`ts-${i}`} entry={e} />;
          }
          const c = activityColors[e.type] || activityColors.text;
          const summary =
            e.type === "text" ? (e.content || "").slice(0, 120) :
            e.type === "tool_use" ? `${e.toolName || "?"}` + (e.toolInput ? ` ${e.toolInput.slice(0, 60)}` : "") :
            e.type === "tool_result" ? `${e.toolName || "?"} ${(e.toolResult || "").slice(0, 80)}` :
            e.type === "error" ? (e.errorMessage || "error").slice(0, 120) :
            e.type === "done" ? "完成" : "";
          return (
            <div key={i} className="px-4 py-1.5 hover:bg-surface-border/10 transition-colors border-b border-surface-border/30">
              <div className="flex items-start gap-2">
                <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded whitespace-nowrap shrink-0 mt-0.5 ${c.bg} ${c.text}`}>{c.icon}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] text-gray-500">{e.agentName}</span>
                    <span className="text-[10px] text-gray-600">{formatTime(e.timestamp)}</span>
                  </div>
                  <span className="text-[11px] text-gray-300 truncate block">{summary}</span>
                </div>
              </div>
            </div>
          );
        })}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

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

export default WorkLogPanel;
