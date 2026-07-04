import { useState, useEffect, useCallback } from "react";
import { getAgentTraces } from "../api";
import type { TraceTurn, TraceEvent } from "../api";

// ── Types ──────────────────────────────────────────────────────

interface RawMessage {
  role: string;
  content?: string | null;
  tool_calls?: any[];
  tool_call_id?: string;
  reasoning_content?: string;
  reasoning?: string;
  thinking?: string;
  thinking_content?: string;
}

// ── Role styles ────────────────────────────────────────────────

const roleStyles: Record<string, { border: string; bg: string; text: string; label: string }> = {
  user:       { border: "border-blue-500/40",   bg: "bg-blue-500/10",   text: "text-blue-300",   label: "USER" },
  assistant:  { border: "border-emerald-500/40", bg: "bg-emerald-500/10", text: "text-emerald-300", label: "ASSISTANT" },
  tool:       { border: "border-amber-500/40",   bg: "bg-amber-500/10",  text: "text-amber-300",  label: "TOOL" },
  system:     { border: "border-gray-500/40",     bg: "bg-gray-500/10",   text: "text-gray-300",   label: "SYSTEM" },
};

function getRoleStyle(role: string) {
  return roleStyles[role] || { border: "border-gray-600/40", bg: "bg-gray-600/10", text: "text-gray-400", label: role.toUpperCase() };
}

// ── Helpers ────────────────────────────────────────────────────

function formatTime(ms: number): string {
  const d = new Date(ms);
  return d.toLocaleTimeString("zh-CN", { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + "…" : s;
}

function extractReasoning(msg: RawMessage): string | null {
  return msg.reasoning_content || msg.reasoning || msg.thinking || msg.thinking_content || null;
}

// ── Message renderer ───────────────────────────────────────────

function MessageView({ msg, index }: { msg: RawMessage; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const [showThinking, setShowThinking] = useState(false);
  const style = getRoleStyle(msg.role);
  const reasoning = extractReasoning(msg);
  const content = msg.content || "";
  const hasToolCalls = msg.tool_calls && msg.tool_calls.length > 0;

  return (
    <div className={`rounded border ${style.border} ${style.bg} p-2`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-1">
        <span className={`text-[10px] font-bold ${style.text} px-1.5 py-0.5 rounded`}>
          {style.label}
        </span>
        <span className="text-[10px] text-gray-500">#{index}</span>
        {hasToolCalls && (
          <span className="text-[10px] text-amber-400">
            {msg.tool_calls!.length} tool_call{msg.tool_calls!.length > 1 ? "s" : ""}
          </span>
        )}
        {reasoning && (
          <button
            onClick={() => setShowThinking(!showThinking)}
            className="text-[10px] text-purple-400 hover:text-purple-300"
          >
            {showThinking ? "隐藏思考" : "查看思考"} ({reasoning.length} chars)
          </button>
        )}
      </div>

      {/* Reasoning */}
      {reasoning && showThinking && (
        <div className="mb-2 p-2 rounded bg-purple-950/30 border border-purple-500/20 text-xs text-purple-300 whitespace-pre-wrap break-all max-h-60 overflow-y-auto">
          {reasoning}
        </div>
      )}

      {/* Content */}
      {content && (
        <div
          className={`text-xs text-gray-200 whitespace-pre-wrap break-all ${
            expanded ? "" : "max-h-32 overflow-hidden"
          }`}
        >
          {content}
        </div>
      )}
      {content && content.length > 800 && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-[10px] text-accent hover:underline mt-1"
        >
          {expanded ? "收起" : `展开 (${content.length} chars)`}
        </button>
      )}

      {/* Tool calls */}
      {hasToolCalls && (
        <div className="mt-2 space-y-1">
          {msg.tool_calls!.map((tc, i) => (
            <ToolCallView key={i} tc={tc} />
          ))}
        </div>
      )}
    </div>
  );
}

function ToolCallView({ tc }: { tc: any }) {
  const [expanded, setExpanded] = useState(false);
  const name = tc.function?.name || tc.name || "unknown";
  const args = tc.function?.arguments || tc.arguments || "{}";
  const id = tc.id || tc.function?.id || "";

  let parsedArgs: any = args;
  try {
    if (typeof args === "string") parsedArgs = JSON.parse(args);
  } catch { /* keep raw */ }

  const argsStr = typeof parsedArgs === "string" ? parsedArgs : JSON.stringify(parsedArgs, null, 2);

  return (
    <div className="rounded border border-amber-500/30 bg-amber-950/20 p-2">
      <div className="flex items-center gap-2">
        <span className="text-xs font-mono text-amber-400">🔧 {name}</span>
        {id && <span className="text-[10px] text-gray-600 truncate">{truncate(id, 30)}</span>}
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-[10px] text-gray-500 hover:text-gray-300 ml-auto"
        >
          {expanded ? "收起" : "展开参数"}
        </button>
      </div>
      {expanded && (
        <pre className="mt-1 text-[11px] text-amber-200/80 whitespace-pre-wrap break-all max-h-40 overflow-y-auto font-mono">
          {argsStr}
        </pre>
      )}
    </div>
  );
}

// ── Turn card ──────────────────────────────────────────────────

function TurnCard({ turn }: { turn: TraceTurn }) {
  const [expanded, setExpanded] = useState(false);
  const messages: RawMessage[] = turn.raw_messages || [];

  const userMsg = messages.find(m => m.role === "user");
  const summary = userMsg?.content
    ? truncate(userMsg.content.replace(/\n/g, " "), 80)
    : `(空轮次 #${turn.turn_index})`;

  return (
    <div className="rounded-lg border border-surface-border bg-surface-card overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 p-3 hover:bg-surface-hover transition-colors text-left"
      >
        <span className="text-xs font-mono text-gray-500 w-16 shrink-0">
          Turn {turn.turn_index}
        </span>
        <span className="text-xs text-gray-300 flex-1 truncate">{summary}</span>
        <span className="text-[10px] text-gray-500 shrink-0">
          {messages.length} msgs · ~{turn.approx_tokens} tok
        </span>
        <span className="text-[10px] text-gray-600 shrink-0">{formatTime(turn.created_at)}</span>
        <svg
          className={`w-4 h-4 text-gray-500 transition-transform shrink-0 ${expanded ? "rotate-180" : ""}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {expanded && (
        <div className="p-3 space-y-2 border-t border-surface-border">
          {messages.map((msg, i) => (
            <MessageView key={i} msg={msg} index={i} />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Event row ──────────────────────────────────────────────────

function EventRow({ event }: { event: TraceEvent }) {
  const [expanded, setExpanded] = useState(false);
  const p = event.payload || {};
  const isRound = event.event_type === "llm_round";

  let label = event.event_type;
  let color = "text-gray-400";
  if (event.event_type === "llm_round") { label = `Round ${p.round_num ?? "?"}`; color = "text-cyan-300"; }
  else if (event.event_type === "chat_start") { label = "对话开始"; color = "text-blue-300"; }
  else if (event.event_type === "chat_done") { label = "对话完成"; color = "text-green-300"; }
  else if (event.event_type === "llm_fail") { label = "LLM 失败"; color = "text-red-300"; }

  return (
    <div className="rounded border border-surface-border bg-surface-card p-2">
      <div className="flex items-center gap-2">
        <span className={`text-[10px] font-bold ${color}`}>{label}</span>
        <span className="text-[10px] text-gray-600">{formatTime(event.created_at)}</span>
        {isRound && (
          <span className="text-[10px] text-gray-500 ml-auto">
            {p.input_tokens != null && `in:${p.input_tokens} `}
            {p.output_tokens != null && `out:${p.output_tokens} `}
            {p.total_tokens != null && `total:${p.total_tokens}`}
          </span>
        )}
        {event.event_type === "chat_done" && (
          <span className="text-[10px] text-gray-500 ml-auto">
            {p.duration_ms != null && `${(p.duration_ms / 1000).toFixed(1)}s `}
            {p.tokens != null && `${p.tokens} tok`}
          </span>
        )}
        {event.event_type === "chat_start" && p.message_length != null && (
          <span className="text-[10px] text-gray-500 ml-auto">{p.message_length} chars</span>
        )}
      </div>
      {isRound && (
        <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-gray-500">
          {p.model && <span>model: {p.model}</span>}
          {p.finish_reason && <span>finish: {p.finish_reason}</span>}
          {p.msg_count != null && <span>msgs: {p.msg_count}</span>}
          {p.text_len != null && <span>text: {p.text_len} chars</span>}
          {p.tool_count != null && <span>tools: {p.tool_count}</span>}
        </div>
      )}
    </div>
  );
}

// ── Main panel ─────────────────────────────────────────────────

export default function MonitorPanel({ agentId }: { agentId: string }) {
  const [traces, setTraces] = useState<{ turns: TraceTurn[]; events: TraceEvent[] }>({ turns: [], events: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [subTab, setSubTab] = useState<"turns" | "events">("turns");
  const [autoRefresh, setAutoRefresh] = useState(true);

  const fetchTraces = useCallback(async () => {
    try {
      const data = await getAgentTraces(agentId);
      setTraces(data);
      setError(null);
    } catch (e: any) {
      setError(e.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    let mounted = true;
    let interval: ReturnType<typeof setInterval> | null = null;

    const doFetch = async () => {
      try {
        const data = await getAgentTraces(agentId);
        if (!mounted) return;
        setTraces(data);
        setError(null);
      } catch (e: any) {
        if (!mounted) return;
        setError(e.message || "加载失败");
      } finally {
        if (mounted) setLoading(false);
      }
    };

    setLoading(true);
    doFetch();

    if (autoRefresh) {
      interval = setInterval(doFetch, 3000);
    }

    return () => {
      mounted = false;
      if (interval) clearInterval(interval);
    };
  }, [agentId, autoRefresh]);

  // Compute stats
  const llmRounds = traces.events.filter(e => e.event_type === "llm_round");
  const totalInput = llmRounds.reduce((s, e) => s + (e.payload.input_tokens || 0), 0);
  const totalOutput = llmRounds.reduce((s, e) => s + (e.payload.output_tokens || 0), 0);
  const totalTokens = llmRounds.reduce((s, e) => s + (e.payload.total_tokens || 0), 0);
  const chatDones = traces.events.filter(e => e.event_type === "chat_done");
  const totalDuration = chatDones.reduce((s, e) => s + (e.payload.duration_ms || 0), 0);

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center text-gray-500 text-sm">
        加载监控数据...
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-full flex flex-col items-center justify-center text-red-400 text-sm">
        <p>{error}</p>
        <button onClick={fetchTraces} className="mt-2 text-xs text-accent hover:underline">
          重试
        </button>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header — stats */}
      <div className="px-3 py-2 border-b border-surface-border bg-surface-card">
        <div className="flex items-center gap-3 text-[11px] text-gray-400">
          <span>轮次: <span className="text-gray-200 font-mono">{traces.turns.length}</span></span>
          <span>LLM 调用: <span className="text-cyan-300 font-mono">{llmRounds.length}</span></span>
          <span>Tokens: <span className="text-gray-200 font-mono">{totalTokens}</span></span>
          <span className="text-gray-600">(in:{totalInput} out:{totalOutput})</span>
          <span>耗时: <span className="text-gray-200 font-mono">{(totalDuration / 1000).toFixed(1)}s</span></span>
          <label className="ml-auto flex items-center gap-1 cursor-pointer">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={e => setAutoRefresh(e.target.checked)}
              className="w-3 h-3"
            />
            <span className="text-[10px]">自动刷新</span>
          </label>
          <button onClick={fetchTraces} className="text-[10px] text-accent hover:underline">
            刷新
          </button>
        </div>
      </div>

      {/* Sub-tabs */}
      <div className="px-3 py-1.5 border-b border-surface-border bg-surface-card flex gap-1">
        <button
          onClick={() => setSubTab("turns")}
          className={`px-2.5 py-1 text-[11px] rounded transition-colors ${
            subTab === "turns" ? "bg-accent/20 text-accent" : "text-gray-400 hover:text-gray-200"
          }`}
        >
          对话轮次 ({traces.turns.length})
        </button>
        <button
          onClick={() => setSubTab("events")}
          className={`px-2.5 py-1 text-[11px] rounded transition-colors ${
            subTab === "events" ? "bg-accent/20 text-accent" : "text-gray-400 hover:text-gray-200"
          }`}
        >
          LLM 调用明细 ({traces.events.length})
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {subTab === "turns" ? (
          traces.turns.length === 0 ? (
            <div className="text-center text-gray-500 text-sm py-8">
              暂无对话轮次数据
            </div>
          ) : (
            traces.turns.map(turn => (
              <TurnCard key={turn.id} turn={turn} />
            ))
          )
        ) : (
          traces.events.length === 0 ? (
            <div className="text-center text-gray-500 text-sm py-8">
              暂无 LLM 调用事件
            </div>
          ) : (
            [...traces.events].reverse().map(event => (
              <EventRow key={event.id} event={event} />
            ))
          )
        )}
      </div>
    </div>
  );
}
