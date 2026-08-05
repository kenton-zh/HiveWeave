import { useState, useEffect, useMemo } from "react";
import {
  getProjectTokenUsage,
  getProjectTokenDaily,
  tokenRequestTypeLabel,
} from "../api";
import type { TokenUsageEntry, TokenDailyEntry } from "../api";

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

// ── Sub-components ──────────────────────────────────────────

function StatCard({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent: string;
}) {
  return (
    <div className="flex-1 min-w-[120px] bg-white border border-g-border rounded-gm px-4 py-3 shadow-gm-sm">
      <div className="text-[11px] text-g-fg-3">{label}</div>
      <div className={`text-xl font-semibold mt-0.5 font-mono ${accent}`}>{value}</div>
    </div>
  );
}

function DailyBarChart({ entries }: { entries: TokenDailyEntry[] }) {
  if (!entries.length) return null;
  const max = Math.max(...entries.map((e) => e.total_tokens), 1);
  return (
    <div className="flex items-end gap-1 h-24">
      {entries.map((e) => {
        const h = Math.max(4, (e.total_tokens / max) * 100);
        return (
          <div
            key={e.day}
            className="flex-1 flex flex-col items-center gap-1 group"
            title={`${e.day}: ${fmtNum(e.total_tokens)} tokens / ${e.llm_calls} calls`}
          >
            <div
              className="w-full rounded-t-sm bg-gradient-to-b from-g-blue to-blue-600/70 group-hover:from-violet-500 transition-all"
              style={{ height: `${h}px` }}
            />
            <span className="text-[9px] text-g-fg-4 font-mono truncate w-full text-center">
              {e.day.slice(5)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ── Main panel ──────────────────────────────────────────────

export default function TokenUsagePanel({ projectId }: { projectId: string }) {
  const [entries, setEntries] = useState<TokenUsageEntry[]>([]);
  const [daily, setDaily] = useState<TokenDailyEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    Promise.all([
      getProjectTokenUsage(projectId),
      getProjectTokenDaily(projectId, 30),
    ])
      .then(([u, d]) => {
        if (!alive) return;
        setEntries(u.entries ?? []);
        setDaily(d.entries ?? []);
      })
      .catch((e) => {
        if (!alive) return;
        setError(e?.message ?? "加载失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  const totals = useMemo(() => {
    return entries.reduce(
      (acc, e) => {
        acc.llm_calls += e.llm_calls || 0;
        acc.input += e.input_tokens || 0;
        acc.output += e.output_tokens || 0;
        acc.cache_read += e.cache_read_tokens || 0;
        acc.cache_creation += e.cache_creation_tokens || 0;
        acc.total += e.total_tokens || 0;
        return acc;
      },
      { llm_calls: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0, total: 0 },
    );
  }, [entries]);

  // 按 agent 透视（折叠 request_type）
  const byAgent = useMemo(() => {
    const map = new Map<string, TokenUsageEntry>();
    for (const e of entries) {
      const cur = map.get(e.agent_id);
      if (!cur) {
        map.set(e.agent_id, { ...e });
        continue;
      }
      cur.llm_calls += e.llm_calls || 0;
      cur.input_tokens += e.input_tokens || 0;
      cur.output_tokens += e.output_tokens || 0;
      cur.cache_read_tokens += e.cache_read_tokens || 0;
      cur.cache_creation_tokens += e.cache_creation_tokens || 0;
      cur.total_tokens += e.total_tokens || 0;
      cur.duration_ms += e.duration_ms || 0;
    }
    return [...map.values()].sort((a, b) => b.total_tokens - a.total_tokens);
  }, [entries]);

  const noData = !loading && !error && entries.length === 0;

  return (
    <div className="h-full overflow-y-auto p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-g-fg">Token 用量</h2>
        <button
          onClick={() => {
            setLoading(true);
            Promise.all([
              getProjectTokenUsage(projectId),
              getProjectTokenDaily(projectId, 30),
            ])
              .then(([u, d]) => {
                setEntries(u.entries ?? []);
                setDaily(d.entries ?? []);
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
            <StatCard label="LLM 调用" value={fmtCalls(totals.llm_calls)} accent="text-g-blue" />
            <StatCard label="输入 tokens" value={fmtNum(totals.input)} accent="text-g-fg" />
            <StatCard label="输出 tokens" value={fmtNum(totals.output)} accent="text-g-fg" />
            <StatCard label="缓存读取" value={fmtNum(totals.cache_read)} accent="text-g-fg-3" />
            <StatCard label="总计 tokens" value={fmtNum(totals.total)} accent="text-violet-600" />
          </div>

          {/* 每日趋势 */}
          <div className="bg-white border border-g-border rounded-gm p-4 shadow-gm-sm">
            <div className="text-xs font-medium text-g-fg mb-3">每日 token 趋势（近 30 天）</div>
            <DailyBarChart entries={daily} />
          </div>

          {/* 按 Agent 汇总 */}
          <div className="bg-white border border-g-border rounded-gm shadow-gm-sm overflow-hidden">
            <div className="px-4 py-2.5 text-xs font-medium text-g-fg border-b border-g-border">
              按 Agent 汇总
            </div>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-g-fg-3 border-b border-g-border">
                  <th className="px-4 py-2 font-medium">Agent</th>
                  <th className="px-2 py-2 font-medium text-right">调用</th>
                  <th className="px-2 py-2 font-medium text-right">输入</th>
                  <th className="px-2 py-2 font-medium text-right">输出</th>
                  <th className="px-2 py-2 font-medium text-right">缓存读</th>
                  <th className="px-4 py-2 font-medium text-right">总计</th>
                </tr>
              </thead>
              <tbody>
                {byAgent.map((a) => (
                  <tr key={a.agent_id} className="border-b border-g-border/60 hover:bg-g-bg-muted/50">
                    <td className="px-4 py-2 font-mono text-g-fg">{a.agent_id}</td>
                    <td className="px-2 py-2 text-right font-mono">{fmtCalls(a.llm_calls)}</td>
                    <td className="px-2 py-2 text-right font-mono">{fmtNum(a.input_tokens)}</td>
                    <td className="px-2 py-2 text-right font-mono">{fmtNum(a.output_tokens)}</td>
                    <td className="px-2 py-2 text-right font-mono">{fmtNum(a.cache_read_tokens)}</td>
                    <td className="px-4 py-2 text-right font-mono font-semibold text-violet-600">
                      {fmtNum(a.total_tokens)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* 按 agent × request_type 明细 */}
          <div className="bg-white border border-g-border rounded-gm shadow-gm-sm overflow-hidden">
            <div className="px-4 py-2.5 text-xs font-medium text-g-fg border-b border-g-border">
              调用来源明细（主对话 / 压缩 / 子代理）
            </div>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-g-fg-3 border-b border-g-border">
                  <th className="px-4 py-2 font-medium">Agent</th>
                  <th className="px-2 py-2 font-medium">来源</th>
                  <th className="px-2 py-2 font-medium text-right">调用</th>
                  <th className="px-2 py-2 font-medium text-right">输入</th>
                  <th className="px-2 py-2 font-medium text-right">输出</th>
                  <th className="px-4 py-2 font-medium text-right">总计</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => (
                  <tr key={`${e.agent_id}-${e.request_type}-${i}`} className="border-b border-g-border/60 hover:bg-g-bg-muted/50">
                    <td className="px-4 py-1.5 font-mono text-g-fg">{e.agent_id}</td>
                    <td className="px-2 py-1.5">
                      <span className="inline-block text-[10px] text-g-fg-2 bg-g-bg-muted px-1.5 py-0.5 rounded">
                        {tokenRequestTypeLabel(e.request_type)}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono">{fmtCalls(e.llm_calls)}</td>
                    <td className="px-2 py-1.5 text-right font-mono">{fmtNum(e.input_tokens)}</td>
                    <td className="px-2 py-1.5 text-right font-mono">{fmtNum(e.output_tokens)}</td>
                    <td className="px-4 py-1.5 text-right font-mono font-medium">{fmtNum(e.total_tokens)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}