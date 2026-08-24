import { Fragment, useState, useEffect, useMemo } from "react";
import {
  getProjectTokenUsage,
  getProjectTokenDaily,
  getOrgTree,
  tokenRequestTypeLabel,
} from "../api";
import { roleLabels } from "../chat/constants";
import type { TokenUsageEntry, TokenDailyEntry } from "../api";
import {
  billedPromptTokens,
  cacheHitPercent,
  formatHitPercent,
} from "./tokenUsageStats";

// ── Formatters ──────────────────────────────────────────────

function fmtNum(n: number | undefined | null): string {
  if (n == null) return "-";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

function fmtCalls(n: number | undefined | null): string {
  if (n == null) return "-";
  return String(n);
}

type AgentMeta = { name: string; role: string };

type OrgNode = { id: string; name: string; role?: string; children?: OrgNode[] | null };
type OrgTreeResponse = { tree: OrgNode[] };

function flattenAgentMeta(tree: unknown): Map<string, AgentMeta> {
  const meta = new Map<string, AgentMeta>();
  const roots = Array.isArray(tree)
    ? (tree as OrgNode[])
    : ((tree as OrgTreeResponse)?.tree ?? []);
  const visit = (n: OrgNode) => {
    meta.set(n.id, { name: n.name, role: n.role || "" });
    if (Array.isArray(n.children)) n.children.forEach(visit);
  };
  roots.forEach(visit);
  return meta;
}

function agentLabel(id: string, meta: AgentMeta | undefined): string {
  return meta?.name || id.slice(0, 8);
}

function AgentCell({ id, meta }: { id: string; meta: AgentMeta | undefined }) {
  const role = meta ? (roleLabels[meta.role] || meta.role || "—") : "—";
  return (
    <div className="min-w-[110px]" title={id}>
      <div className="text-g-fg">{agentLabel(id, meta)}</div>
      <div className="text-[10px] text-g-fg-4">{role}</div>
    </div>
  );
}

/** 数值单元格，按总/子渠道两套间距样式渲染。 */
function NumCell({
  value,
  fmt,
  dense,
  strong,
}: {
  value: number | null | undefined;
  fmt: (n: number | null | undefined) => string;
  dense?: boolean;
  strong?: boolean;
}) {
  return (
    <td
      className={`font-mono text-right ${
        strong ? "font-semibold text-violet-600 " : ""
      }${dense ? "px-2 py-1.5" : "px-2 py-2"}`}
    >
      {fmt(value)}
    </td>
  );
}

// ── Sub-components ──────────────────────────────────────────

function StatCard({
  label,
  value,
  accent,
  hint,
}: {
  label: string;
  value: string;
  accent: string;
  hint?: string;
}) {
  return (
    <div
      className="flex-1 min-w-[120px] bg-white border border-g-border rounded-gmLg px-4 py-3 shadow-gm-sm hover-lift"
      title={hint}
    >
      <div className="text-[10px] font-medium uppercase tracking-wider text-g-fg-3">{label}</div>
      <div className={`text-xl font-semibold mt-1 font-mono num ${accent}`}>{value}</div>
      {hint ? (
        <div className="text-[9px] text-g-fg-4 mt-1 leading-snug">{hint}</div>
      ) : null}
    </div>
  );
}

function DailyBarChart({ entries }: { entries: TokenDailyEntry[] }) {
  if (!entries.length) return null;
  const max = Math.max(...entries.map((e) => e.total_tokens), 1);
  return (
    <div className="flex items-end gap-1.5 h-28 border-b border-g-border">
      {entries.map((e) => {
        const h = Math.max(4, (e.total_tokens / max) * 96);
        return (
          <div
            key={e.day}
            className="flex-1 flex flex-col items-center justify-end gap-1.5 group h-full"
            title={`${e.day}: ${fmtNum(e.total_tokens)} tokens / ${e.llm_calls} calls`}
          >
            <div
              className="w-full max-w-[36px] rounded-t-[4px] bg-gradient-to-b from-indigo-400 to-g-blue group-hover:from-violet-400 group-hover:to-violet-600 transition-all"
              style={{ height: `${h}px` }}
            />
            <span className="text-[9px] text-g-fg-4 font-mono num truncate w-full text-center">
              {e.day.slice(5)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function fetchTokenData(projectId: string) {
  return Promise.all([
    getProjectTokenUsage(projectId),
    getProjectTokenDaily(projectId, 30),
    getOrgTree(projectId).catch(() => null),
  ]).then(([u, d, tree]) => ({
    entries: u.entries ?? [],
    daily: d.entries ?? [],
    meta: flattenAgentMeta(tree),
  }));
}

export default function TokenUsagePanel({ projectId }: { projectId: string }) {
  const [entries, setEntries] = useState<TokenUsageEntry[]>([]);
  const [daily, setDaily] = useState<TokenDailyEntry[]>([]);
  const [agentMeta, setAgentMeta] = useState<Map<string, AgentMeta>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    fetchTokenData(projectId)
      .then((data) => {
        if (ac.signal.aborted) return;
        setEntries(data.entries);
        setDaily(data.daily);
        setAgentMeta(data.meta);
      })
      .catch((e) => {
        if (ac.signal.aborted || e?.name === "AbortError") return;
        setError(e?.message ?? "加载失败");
      })
      .finally(() => {
        if (!ac.signal.aborted) setLoading(false);
      });
    return () => ac.abort();
  }, [projectId]);

  const totals = useMemo(() => {
    const acc = entries.reduce(
      (sum, e) => {
        sum.llm_calls += e.llm_calls || 0;
        sum.input += e.input_tokens || 0;
        sum.output += e.output_tokens || 0;
        sum.cache_read += e.cache_read_tokens || 0;
        sum.cache_creation += e.cache_creation_tokens || 0;
        sum.total += e.total_tokens || 0;
        return sum;
      },
      { llm_calls: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0, total: 0 },
    );
    return {
      ...acc,
      billed: billedPromptTokens(acc.input, acc.cache_read, acc.cache_creation),
      hitPct: cacheHitPercent(acc.input, acc.cache_read, acc.cache_creation),
    };
  }, [entries]);

  // 按 agent 分组：每行汇总 + 来源拆分（request_type）明细
  const grouped = useMemo(() => {
    type Group = { agent_id: string; summary: TokenUsageEntry; rows: TokenUsageEntry[] };
    const map = new Map<string, Group>();
    for (const e of entries) {
      let g = map.get(e.agent_id);
      if (!g) {
        g = { agent_id: e.agent_id, summary: { ...e }, rows: [] };
        map.set(e.agent_id, g);
      } else {
        const s = g.summary;
        s.llm_calls += e.llm_calls || 0;
        s.input_tokens += e.input_tokens || 0;
        s.output_tokens += e.output_tokens || 0;
        s.cache_read_tokens += e.cache_read_tokens || 0;
        s.cache_creation_tokens += e.cache_creation_tokens || 0;
        s.total_tokens += e.total_tokens || 0;
      }
      g.rows.push(e);
    }
    return [...map.values()].sort(
      (a, b) => b.summary.total_tokens - a.summary.total_tokens,
    );
  }, [entries]);

  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (agentId: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(agentId)) next.delete(agentId);
      else next.add(agentId);
      return next;
    });

  const noData = !loading && !error && entries.length === 0;

  return (
    <div className="h-full overflow-y-auto p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-g-fg">Token 用量</h2>
        <button
          onClick={() => {
            setLoading(true);
            setError(null);
            fetchTokenData(projectId)
              .then((data) => {
                setEntries(data.entries);
                setDaily(data.daily);
                setAgentMeta(data.meta);
              })
              .catch((e) => setError(e?.message ?? "刷新失败"))
              .finally(() => setLoading(false));
          }}
          className="text-xs text-g-blue hover:bg-g-blue-bg px-2 py-1 rounded-gm transition-all active:scale-95"
        >
          刷新
        </button>
      </div>

      {loading && (
        <div className="text-sm text-g-fg-3 animate-pulse-soft py-8 text-center">
          加载中...
        </div>
      )}

      {error && (
        <div className="text-sm text-red-500 bg-red-50 border border-red-100 rounded-gm px-3 py-2">
          加载失败：{error}
        </div>
      )}

      {noData && (
        <div className="text-sm text-g-fg-3 py-8 text-center">
          暂无 token 用量数据。Agent 产生对话/压缩后会自动记录。
        </div>
      )}

      {!loading && !error && entries.length > 0 && (
        <>
          {/* 统计卡片 */}
          <div className="flex gap-3 flex-wrap">
            <StatCard
              label="总 Token"
              value={fmtNum(totals.total)}
              accent="text-violet-600"
              hint="未命中 + 输出 + 缓存写，不含缓存读"
            />
            <StatCard label="输出" value={fmtNum(totals.output)} accent="text-g-fg" hint="模型生成" />
            <StatCard
              label="输入（命中）"
              value={fmtNum(totals.cache_read)}
              accent="text-g-fg"
              hint="前缀命中，不进记账合计"
            />
            <StatCard
              label="输入（未命中）"
              value={fmtNum(totals.input)}
              accent="text-g-fg"
              hint="未走缓存的新输入"
            />
            <StatCard
              label="命中率"
              value={formatHitPercent(totals.hitPct)}
              accent="text-g-blue"
              hint="缓存读 ÷（未命中 + 缓存读 + 缓存写）"
            />
            <StatCard label="LLM 调用" value={fmtCalls(totals.llm_calls)} accent="text-g-blue" />
          </div>

          {/* 每日趋势 */}
          <div className="bg-white border border-g-border rounded-gmLg p-4 shadow-gm-sm">
            <div className="text-xs font-medium text-g-fg mb-3">每日 token 趋势（近 30 天）</div>
            <DailyBarChart entries={daily} />
          </div>

          {/* 按 Agent 汇总（可展开来源明细） */}
          <div className="bg-white border border-g-border rounded-gmLg shadow-gm-sm overflow-x-auto">
            <div className="px-4 py-2.5 text-xs font-medium text-g-fg border-b border-g-border">
              按 Agent 汇总
              <span className="ml-2 text-g-fg-4 font-normal">
                （点击来源可展开主对话 / 压缩 / 子代理拆分）
              </span>
            </div>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-g-fg-3 border-b border-g-border">
                  <th scope="col" className="px-4 py-2 font-medium">Agent</th>
                  <th scope="col" className="px-2 py-2 font-medium text-right">LLM 调用</th>
                  <th scope="col" className="px-2 py-2 font-medium text-right">输入（未命中）</th>
                  <th scope="col" className="px-2 py-2 font-medium text-right">输出</th>
                  <th scope="col" className="px-2 py-2 font-medium text-right">输入（命中）</th>
                  <th scope="col" className="px-2 py-2 font-medium text-right">命中率</th>
                  <th scope="col" className="px-4 py-2 font-medium text-right">总 Token</th>
                </tr>
              </thead>
              <tbody>
                {grouped.map((g) => {
                  const a = g.summary;
                  const isOpen = expanded.has(a.agent_id);
                  return (
                    <Fragment key={a.agent_id}>
                      <tr
                        className={`border-b border-g-border/60 hover:bg-g-bg-muted/50 ${
                          isOpen ? "bg-g-bg-muted/30" : ""
                        }`}
                      >
                        <td className="px-4 py-1.5">
                          <div className="flex items-center gap-1.5">
                            <button
                              type="button"
                              onClick={() => toggle(a.agent_id)}
                              aria-label={isOpen ? "收起来源明细" : "展开来源明细"}
                              aria-expanded={isOpen}
                              aria-controls={isOpen ? `token-detail-${a.agent_id}` : undefined}
                              className="shrink-0 text-g-fg-4 hover:text-g-blue transition-colors p-0.5 -ml-0.5"
                            >
                              <svg
                                className={`w-3 h-3 transition-transform ${isOpen ? "rotate-90" : ""}`}
                                viewBox="0 0 16 16"
                                fill="currentColor"
                                aria-hidden
                              >
                                <path d="M6 3l5 5-5 5V3z" />
                              </svg>
                            </button>
                            <AgentCell id={a.agent_id} meta={agentMeta.get(a.agent_id)} />
                          </div>
                        </td>
                        <NumCell value={a.llm_calls} fmt={fmtCalls} />
                        <NumCell value={a.input_tokens} fmt={fmtNum} />
                        <NumCell value={a.output_tokens} fmt={fmtNum} />
                        <NumCell value={a.cache_read_tokens} fmt={fmtNum} />
                        <NumCell
                          value={cacheHitPercent(a.input_tokens, a.cache_read_tokens, a.cache_creation_tokens)}
                          fmt={formatHitPercent}
                        />
                        <NumCell value={a.total_tokens} fmt={fmtNum} strong />
                      </tr>
                      {isOpen && (
                        <tr id={`token-detail-${a.agent_id}`} className="border-b border-g-border/60 bg-g-bg-muted/20">
                          <td colSpan={7} className="px-4 py-2">
                            <table className="w-full border-collapse text-[11px]">
                              <thead>
                                <tr className="text-left text-g-fg-3">
                                  <th scope="col" className="pl-6 pr-2 py-1 font-medium">来源</th>
                                  <th scope="col" className="px-2 py-1 font-medium text-right">LLM 调用</th>
                                  <th scope="col" className="px-2 py-1 font-medium text-right">输入（未命中）</th>
                                  <th scope="col" className="px-2 py-1 font-medium text-right">输出</th>
                                  <th scope="col" className="px-2 py-1 font-medium text-right">输入（命中）</th>
                                  <th scope="col" className="px-2 py-1 font-medium text-right">命中率</th>
                                  <th scope="col" className="px-4 py-1 font-medium text-right">总 Token</th>
                                </tr>
                              </thead>
                              <tbody>
                                {g.rows.map((r, i) => (
                                  <tr key={`${r.agent_id}-${r.request_type}-${i}`} className="hover:bg-g-bg-muted/30">
                                    <td className="pl-6 pr-2 py-1 text-left">
                                      <span className="inline-block text-[10px] text-g-fg-2 bg-g-bg-muted px-1.5 py-0.5 rounded">
                                        {tokenRequestTypeLabel(r.request_type)}
                                      </span>
                                    </td>
                                    <NumCell dense value={r.llm_calls} fmt={fmtCalls} />
                                    <NumCell dense value={r.input_tokens} fmt={fmtNum} />
                                    <NumCell dense value={r.output_tokens} fmt={fmtNum} />
                                    <NumCell dense value={r.cache_read_tokens} fmt={fmtNum} />
                                    <NumCell
                                      dense
                                      value={cacheHitPercent(r.input_tokens, r.cache_read_tokens, r.cache_creation_tokens)}
                                      fmt={formatHitPercent}
                                    />
                                    <NumCell dense value={r.total_tokens} fmt={fmtNum} />
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}