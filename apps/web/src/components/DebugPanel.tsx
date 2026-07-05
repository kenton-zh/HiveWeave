import { useState, useEffect, useRef } from "react";
import { useAppStore, type DebugLogEntry } from "../store";

const CATEGORY_COLORS: Record<string, string> = {
  api: "text-blue-400",
  ws: "text-green-400",
  error: "text-red-400",
  info: "text-gray-400",
  state: "text-yellow-400",
};

const CATEGORY_LABELS: Record<string, string> = {
  api: "API",
  ws: "WS",
  error: "ERR",
  info: "INF",
  state: "STT",
};

function formatTime(ts: number): string {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

export default function DebugPanel() {
  const debugLogs = useAppStore((s) => s.debugLogs);
  const clearDebugLogs = useAppStore((s) => s.clearDebugLogs);
  const addDebugLog = useAppStore((s) => s.addDebugLog);
  const [filter, setFilter] = useState<string>("all");
  const [autoScroll, setAutoScroll] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (autoScroll) {
      endRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [debugLogs, autoScroll]);

  // Log initial mount
  useEffect(() => {
    addDebugLog({ category: "info", message: "DebugPanel mounted — logging started" });
  }, [addDebugLog]);

  const filtered = filter === "all"
    ? debugLogs
    : debugLogs.filter((l) => l.category === filter);

  const errorCount = debugLogs.filter((l) => l.category === "error").length;
  const apiCount = debugLogs.filter((l) => l.category === "api").length;
  const wsCount = debugLogs.filter((l) => l.category === "ws").length;

  return (
    <div className="h-full flex flex-col bg-surface-card">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-surface-border shrink-0">
        <span className="text-xs font-semibold text-white">调试日志</span>
        <span className="text-xs text-gray-500">({debugLogs.length})</span>
        {errorCount > 0 && (
          <span className="text-xs text-red-400 px-1.5 py-0.5 bg-red-500/10 rounded">
            {errorCount} 错误
          </span>
        )}
        <div className="flex-1" />
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="text-xs bg-surface-alt border border-surface-border rounded px-2 py-1 text-gray-300"
        >
          <option value="all">全部</option>
          <option value="api">API ({apiCount})</option>
          <option value="ws">WebSocket ({wsCount})</option>
          <option value="error">错误 ({errorCount})</option>
        </select>
        <button
          onClick={() => setAutoScroll(!autoScroll)}
          className={`text-xs px-2 py-1 rounded ${autoScroll ? "bg-accent/20 text-accent" : "text-gray-400"}`}
        >
          自动滚动
        </button>
        <button
          onClick={clearDebugLogs}
          className="text-xs px-2 py-1 text-gray-400 hover:text-red-400 rounded"
        >
          清除
        </button>
      </div>

      {/* Log entries */}
      <div className="flex-1 overflow-y-auto font-mono text-xs">
        {filtered.length === 0 ? (
          <div className="h-full flex items-center justify-center text-gray-600">
            暂无日志
          </div>
        ) : (
          filtered.map((entry) => (
            <div
              key={entry.id}
              className="border-b border-surface-border/50 hover:bg-surface-hover/30 cursor-pointer"
              onClick={() => setExpanded(expanded === entry.id ? null : entry.id)}
            >
              <div className="flex items-start gap-2 px-2 py-1">
                <span className="text-gray-600 shrink-0">{formatTime(entry.timestamp)}</span>
                <span className={`shrink-0 font-bold w-8 ${CATEGORY_COLORS[entry.category]}`}>
                  {CATEGORY_LABELS[entry.category]}
                </span>
                <span className="text-gray-300 break-all">
                  {entry.message}
                </span>
              </div>
              {expanded === entry.id && entry.data && (
                <div className="px-2 pb-2 pl-12">
                  <pre className="text-gray-500 whitespace-pre-wrap break-all text-xs bg-black/30 p-2 rounded">
                    {typeof entry.data === "string"
                      ? entry.data
                      : JSON.stringify(entry.data, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
