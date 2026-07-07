import { useState, useEffect, useCallback, useMemo } from "react";
import { getAgentTraces } from "../api";
import type { TraceTurn, TraceEvent, RawTraceMessage } from "../api";

// ── Helpers ──────────────────────────────────────────────────

function formatTime(ms: number): string {
  if (!ms) return "--";
  const d = new Date(ms);
  return d.toLocaleTimeString("zh-CN", { hour12: false }) + "." +
    String(d.getMilliseconds()).padStart(3, "0");
}

function tokenLabel(n: number | undefined | null): string {
  if (n == null) return "-";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

// ── Sub-components ──────────────────────────────────────────

function TokenBar({ input, output }: { input: number; output: number }) {
  const total = input + output;
  const inPct = total > 0 ? (input / total) * 100 : 50;
  return (
    <div className="flex items-center gap-2 text-[10px] font-mono">
      <span className="text-blue-400 shrink-0">in:{tokenLabel(input)}</span>
      <div className="flex-1 h-1.5 bg-gray-700 rounded-full overflow-hidden">
        <div className="flex h-full">
          <div className="bg-blue-500/60 h-full" style={{ width: `${inPct}%` }} />
          <div className="bg-emerald-500/60 h-full" style={{ width: `${100 - inPct}%` }} />
        </div>
      </div>
      <span className="text-emerald-400 shrink-0">out:{tokenLabel(output)}</span>
    </div>
  );
}

function ToolBadge({ name }: { name: string }) {
  return (
    <span className="inline-flex items-center gap-1 text-[10px] font-mono text-amber-400 bg-amber-500/10 border border-amber-500/25 px-1.5 py-0.5 rounded">
      🔧 {name}
    </span>
  );
}

function MessageView({ msg, idx }: { msg: RawTraceMessage; idx: number }) {
  const [showThinking, setShowThinking] = useState(false);
  const role = msg.role;
  const content = msg.content || "";
  const thinking = msg.reasoning_content || msg.thinking || "";
  const toolCalls: any[] = msg.tool_calls || [];

  if (role === "tool") {
    // Tool result — compact
    const preview = content.length > 200
      ? content.slice(0, 200) + "…"
      : content;
    return (
      <div className="ml-4 pl-3 border-l-2 border-amber-500/20 text-[10px] text-gray-500 font-mono">
        <span className="text-amber-500/60">→ result:</span>{" "}
        <span className="whitespace-pre-wrap break-all">{preview}</span>
      </div>
    );
  }

  if (role === "system") return null; // skip system messages

  const isAssistant = role === "assistant";

  return (
    <div className={isAssistant ? "" : ""}>
      {/* Thinking */}
      {thinking && (
        <details className="group mb-1">
          <summary
            onClick={(e) => { e.preventDefault(); setShowThinking(!showThinking); }}
            className="text-[10px] text-purple-400 cursor-pointer hover:text-purple-300 select-none flex items-center gap-1"
          >
            <span className="text-[9px] transition-transform" style={showThinking ? {} : {}}>
              {showThinking ? "▼" : "▶"}
            </span>
            💭 思考过程 ({thinking.length} chars)
          </summary>
          {showThinking && (
            <div className="mt-1 ml-4 pl-2 border-l-2 border-purple-500/20 text-[10px] text-purple-300/70 whitespace-pre-wrap break-words max-h-40 overflow-y-auto leading-relaxed">
              {thinking}
            </div>
          )}
        </details>
      )}

      {/* Content */}
      {content && (
        <div className="text-xs text-gray-200 whitespace-pre-wrap break-words leading-relaxed">
          {content}
        </div>
      )}

      {/* Tool calls */}
      {toolCalls.map((tc: any, i: number) => {
        const name = tc.function?.name || tc.name || "unknown";
        const hint = extractToolHint(name, tc.function?.arguments || tc.arguments || "{}");
        return (
          <div key={i} className="ml-2 mt-1 flex items-center gap-1.5">
            <ToolBadge name={name} />
            {hint && <span className="text-[10px] text-gray-600 truncate">{hint}</span>}
          </div>
        );
      })}
    </div>
  );
}

function extractToolHint(name: string, args: string | object): string {
  try {
    const a = typeof args === "string" ? JSON.parse(args) : args;
    const pick = (...keys: string[]): string => {
      for (const k of keys) {
        const v = (a as any)?.[k];
        if (typeof v === "string" && v.trim()) return v.trim().slice(0, 60);
      }
      return "";
    };
    switch (name) {
      case "read_file": return pick("path", "file_path");
      case "write_file": return pick("path", "file_path");
      case "list_files": return pick("path");
      case "grep": case "search_files": return pick("pattern", "query");
      case "bash": case "run_command": return pick("command", "cmd");
      case "message_agent": case "send_inbox": return pick("to_agent_id", "agent_name");
      case "create_agent": case "hire_agent": return pick("role", "name");
      case "websearch": return pick("query");
      default: return "";
    }
  } catch {
    return "";
  }
}

// ── Turn card ────────────────────────────────────────────────

function TurnCard({ turn, events, expanded, onToggle }: { turn: TraceTurn; events: TraceEvent[]; expanded: boolean; onToggle: () => void }) {
  const messages = turn.raw_messages || [];

  // Correlate LLM rounds with this turn's time window
  const turnEvents = useMemo(() => {
    if (!turn.created_at) return [];
    // Events within a reasonable window around the turn
    const tStart = turn.created_at - 5000;  // 5s before
    const tEnd = turn.created_at + 600000;  // 10min after (max)
    return events.filter(e =>
      e.event_type === "llm_round" &&
      e.created_at >= tStart &&
      e.created_at <= tEnd
    );
  }, [events, turn.created_at]);

  const totalInput = turnEvents.reduce((s, e) => s + (e.payload?.input_tokens || 0), 0);
  const totalOutput = turnEvents.reduce((s, e) => s + (e.payload?.output_tokens || 0), 0);

  // Detect intent from the first user message
  const userMsg = messages.find(m => m.role === "user" && !(m as any).is_background);
  const intent = userMsg?.content
    ? userMsg.content.replace(/\n/g, " ").slice(0, 120)
    : turn.summary || "(空轮次)";

  return (
    <div className="rounded-lg border border-surface-border bg-surface-card overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-3 p-3 hover:bg-surface-hover transition-colors text-left"
      >
        <span className="text-[10px] font-mono text-gray-500 w-12 shrink-0">
          #{turn.turn_index}
        </span>

        <div className="flex-1 min-w-0">
          <div className="text-xs text-gray-300 truncate">{intent}</div>
          <div className="flex items-center gap-3 mt-0.5">
            <span className="text-[10px] text-gray-500">
              {turn.message_count} msgs
            </span>
            {turn.tool_call_count > 0 && (
              <span className="text-[10px] text-amber-400">
                {turn.tool_call_count} tools
              </span>
            )}
            <span className="text-[10px] text-gray-600">
              ~{tokenLabel(turn.approx_tokens)} tok est.
            </span>
          </div>
        </div>

        {totalInput + totalOutput > 0 && (
          <div className="w-24 shrink-0">
            <TokenBar input={totalInput} output={totalOutput} />
          </div>
        )}

        <span className="text-[10px] text-gray-600 shrink-0">{formatTime(turn.created_at)}</span>
        <svg
          className={`w-4 h-4 text-gray-500 transition-transform shrink-0 ${expanded ? "rotate-180" : ""}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {expanded && (
        <div className="px-3 pb-3 border-t border-surface-border">
          {/* Token summary header */}
          {turnEvents.length > 0 && (
            <div className="py-2 mb-2 border-b border-surface-border/50">
              <div className="text-[10px] text-gray-500 mb-1">
                {turnEvents.length} LLM call{turnEvents.length > 1 ? "s" : ""}
                {" · "}
                total in:{tokenLabel(totalInput)} out:{tokenLabel(totalOutput)}
              </div>
              <div className="space-y-0.5">
                {turnEvents.map((ev, i) => (
                  <div key={ev.id} className="flex items-center gap-2 text-[10px] font-mono text-gray-600">
                    <span className="text-cyan-400 w-6">R{i}</span>
                    <span>in:{tokenLabel(ev.payload?.input_tokens)}</span>
                    <span>out:{tokenLabel(ev.payload?.output_tokens)}</span>
                    {ev.payload?.model && <span className="text-gray-700">{ev.payload.model}</span>}
                    {ev.payload?.finish_reason && (
                      <span className="text-gray-500">finish:{ev.payload.finish_reason}</span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Messages */}
          <div className="space-y-2">
            {messages.map((msg, i) => (
              <MessageView key={i} msg={msg} idx={i} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Event row (LLM call detail) ──────────────────────────────

function EventRow({ event }: { event: TraceEvent }) {
  const p = event.payload || {};
  const isRound = event.event_type === "llm_round";

  let label = event.event_type;
  let color = "text-gray-400";
  if (isRound) { label = `LLM Round`; color = "text-cyan-300"; }
  else if (event.event_type === "chat_start") { label = "对话开始"; color = "text-blue-300"; }
  else if (event.event_type === "chat_done") { label = "对话完成"; color = "text-green-300"; }
  else if (event.event_type === "llm_fail") { label = "LLM 失败"; color = "text-red-300"; }

  return (
    <div className="rounded-lg border border-surface-border bg-surface-card p-3">
      <div className="flex items-center gap-3">
        <span className={`text-[10px] font-bold ${color} w-16`}>{label}</span>
        {isRound && (
          <>
            <TokenBar input={p.input_tokens || 0} output={p.output_tokens || 0} />
            <div className="flex flex-wrap gap-2 text-[10px] text-gray-500">
              {p.model && <span>model: {p.model}</span>}
              {p.finish_reason && <span>finish: {p.finish_reason}</span>}
              {p.msg_count != null && <span>msgs: {p.msg_count}</span>}
              {p.tool_count != null && <span>tools: {p.tool_count}</span>}
            </div>
          </>
        )}
        {event.event_type === "chat_done" && (
          <span className="text-[10px] text-gray-500">
            {p.duration_ms != null && `${(p.duration_ms / 1000).toFixed(1)}s `}
            {p.tokens != null && `${p.tokens} tok`}
          </span>
        )}
        <span className="text-[10px] text-gray-600 ml-auto">{formatTime(event.created_at)}</span>
      </div>
    </div>
  );
}

// ── Main panel ───────────────────────────────────────────────

export default function MonitorPanel({ agentId }: { agentId: string }) {
  const [traces, setTraces] = useState<{ turns: TraceTurn[]; events: TraceEvent[] }>({ turns: [], events: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [subTab, setSubTab] = useState<"turns" | "events">("turns");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [expandedTurnIds, setExpandedTurnIds] = useState<Set<string>>(new Set());

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

  // Stats
  const llmRounds = traces.events.filter(e => e.event_type === "llm_round");
  const totalInput = llmRounds.reduce((s, e) => s + (e.payload?.input_tokens || 0), 0);
  const totalOutput = llmRounds.reduce((s, e) => s + (e.payload?.output_tokens || 0), 0);
  const totalTokens = totalInput + totalOutput;
  const chatDones = traces.events.filter(e => e.event_type === "chat_done");
  const totalDuration = chatDones.reduce((s, e) => s + (e.payload?.duration_ms || 0), 0);

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
    <div className="h-full flex flex-col bg-surface">
      {/* Header — Token-centric stats */}
      <div className="px-3 py-2 border-b border-surface-border bg-surface-card">
        <div className="flex items-center gap-4 text-[11px] text-gray-400">
          <span>对话轮次: <span className="text-gray-200 font-mono">{traces.turns.length}</span></span>
          <span>LLM 调用: <span className="text-cyan-300 font-mono">{llmRounds.length}</span></span>
          <span>
            Tokens:
            <span className="text-blue-400 font-mono ml-1">in:{tokenLabel(totalInput)}</span>
            <span className="text-gray-600 mx-1">/</span>
            <span className="text-emerald-400 font-mono">out:{tokenLabel(totalOutput)}</span>
            <span className="text-gray-600 mx-1">=</span>
            <span className="text-gray-200 font-mono">{tokenLabel(totalTokens)}</span>
          </span>
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
          LLM Token 明细 ({llmRounds.length})
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
            [...traces.turns].reverse().map(turn => (
              <TurnCard
                key={turn.id}
                turn={turn}
                events={traces.events}
                expanded={expandedTurnIds.has(turn.id)}
                onToggle={() => setExpandedTurnIds(prev => {
                  const next = new Set(prev);
                  if (next.has(turn.id)) next.delete(turn.id);
                  else next.add(turn.id);
                  return next;
                })}
              />
            ))
          )
        ) : (
          llmRounds.length === 0 ? (
            <div className="text-center text-gray-500 text-sm py-8">
              暂无 LLM Token 数据
            </div>
          ) : (
            [...llmRounds].reverse().map(event => (
              <EventRow key={event.id} event={event} />
            ))
          )
        )}
      </div>
    </div>
  );
}
