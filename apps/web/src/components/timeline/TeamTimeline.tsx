/**
 * TeamTimeline — 团队泳道总览容器（Timeline v4 §5.2，P2 核心）。
 *
 * 数据纪律（v4 §三写死）：REST 聚合端点是唯一数据源，WS task_event
 * 只是失效信号（store.timelineVersion 触发重拉），30s 兜底轮询仅在本
 * 组件挂载期间运行。视口 = {since, until} 时间窗口（usePanZoom），
 * 段按窗口百分比定位，无 CSS transform。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getProjectGameTime, getTeamActivity } from "../../api";
import type { TeamActivityQuery } from "../../api";
import { useAppStore } from "../../store";
import type { ActiveAssignment, TaskSegment, TeamActivityResponse, TeamAgent } from "./types";
import type { GameTimeAnchor } from "./utils";
import { statusStyle } from "./utils";
import { clampSpan, isLiveWindow, usePanZoom } from "./usePanZoom";
import type { TimeViewport } from "./usePanZoom";
import { parseDeepLink, useDeepLinkWriter } from "./useDeepLink";
import TimeAxis, { dayBoundaries } from "./TimeAxis";
import TeamTimelineLane, { LANE_LABEL_W } from "./TeamTimelineLane";
import type { LaneData } from "./TeamTimelineLane";
import MiniMap from "./MiniMap";
import TimelineTooltip from "./TimelineTooltip";
import type { TooltipState } from "./TimelineTooltip";

const PRESETS: Array<{ label: string; ms: number | "all" }> = [
  { label: "最近 1h", ms: 3600e3 },
  { label: "最近 6h", ms: 6 * 3600e3 },
  { label: "最近 24h", ms: 24 * 3600e3 },
  { label: "全部", ms: "all" },
];

export default function TeamTimeline() {
  const projectId = useAppStore((s) => s.selectedProjectId);
  const projects = useAppStore((s) => s.projects);
  const selectedTaskId = useAppStore((s) => s.selectedTaskId);
  const setSelectedTask = useAppStore((s) => s.setSelectedTask);
  const timelineVersion = useAppStore((s) => s.timelineVersion);
  const agentHealth = useAppStore((s) => s.agentHealth);

  // ── 本地状态 ─────────────────────────────────────────────
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [data, setData] = useState<TeamActivityResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [anchor, setAnchor] = useState<GameTimeAnchor | null>(null);
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);
  const [keyword, setKeyword] = useState("");
  const [statusFilter, setStatusFilter] = useState<Set<string>>(new Set());
  const [agentFilter, setAgentFilter] = useState<Set<string>>(new Set());
  // 后端截断/更早数据提示（v4：不静默截断，前端必须提示缩窗）
  const [hint, setHint] = useState<{ truncated: boolean; hasEarlier: boolean }>({
    truncated: false,
    hasEarlier: false,
  });

  // ── 视口（深链恢复初始窗口；跨度强制 clamp 到 [5min, 45d]） ──
  const initialView = useMemo<TimeViewport>(() => {
    const dl = parseDeepLink();
    if (dl.since && dl.until && dl.until > dl.since) {
      return { since: dl.since, until: dl.since + clampSpan(dl.until - dl.since) };
    }
    const now = Date.now();
    return { since: now - 3600e3, until: now };
  }, []);
  const containerRef = useRef<HTMLDivElement>(null);
  // 骨架屏阶段容器 div 未渲染 → wheel 监听挂不上；ready 标志驱动 effect 补挂
  const containerReady = !initialLoading && !error && !!data && (data.agents?.length ?? 0) > 0;
  const pan = usePanZoom({
    containerRef,
    initial: initialView,
    labelWidth: LANE_LABEL_W,
    containerReady,
  });
  const { view } = pan;

  const viewRef = useRef(view);
  viewRef.current = view;
  const fetchedRef = useRef<{ since: number; until: number } | null>(null);
  const lastMaxTsRef = useRef<number | null>(null);
  const reqSeqRef = useRef(0);

  // ── 数据拉取（REST 是唯一数据源） ────────────────────────
  const refresh = useCallback(
    async (force: boolean = false) => {
      if (!projectId) return;
      const v = viewRef.current;
      const span = v.until - v.since;
      // 拉取窗口 = 视口外扩 25%，平移小幅不触发重拉
      const since_ms = Math.floor(v.since - span * 0.25);
      const until_ms = Math.max(Math.ceil(v.until + span * 0.25), Date.now());
      const seq = ++reqSeqRef.current;
      try {
        const q: TeamActivityQuery = { since_ms, until_ms, limit: 2000 };
        // 后端契约（timeline.py）：短路不校验窗口参数——仅当请求窗口
        // 完全落在已拉取窗口内才允许带 token，否则（平移/缩放换窗）必须
        // 全量拉取，否则安静项目会永久拿到 changed:false 假阴性。
        const f = fetchedRef.current;
        const covered = !!f && since_ms >= f.since && until_ms <= f.until;
        if (!force && covered && lastMaxTsRef.current) q.if_changed_since = lastMaxTsRef.current;
        const res = await getTeamActivity(projectId, q);
        if (seq !== reqSeqRef.current) return; // 过期响应丢弃
        if (res.changed === false) return; // 短路：无变化，保留旧数据
        const full = res as TeamActivityResponse;
        setData(full);
        lastMaxTsRef.current = full.max_event_ts || null;
        // 以服务端回显窗口为准记账（服务端可能规范化窗口）
        fetchedRef.current = full.window ?? { since: since_ms, until: until_ms };
        setHint({
          truncated: full.truncated === true,
          hasEarlier: full.has_more_earlier === true,
        });
        setError(null);
      } catch (e: any) {
        if (seq !== reqSeqRef.current || e?._aborted) return;
        setError(e?.message || "团队活动加载失败");
      } finally {
        if (seq === reqSeqRef.current) setInitialLoading(false);
      }
    },
    [projectId],
  );

  // 项目变化：清空重拉
  useEffect(() => {
    setData(null);
    setError(null);
    setInitialLoading(true);
    setHint({ truncated: false, hasEarlier: false });
    lastMaxTsRef.current = null;
    fetchedRef.current = null;
    if (projectId) void refresh(true);
  }, [projectId, refresh]);

  // 视口移出已拉取范围 → 防抖重拉（平移/缩放）
  useEffect(() => {
    if (!projectId) return;
    const f = fetchedRef.current;
    if (f && view.since >= f.since && view.until <= f.until) return;
    const t = setTimeout(() => {
      // 首拉仍在途（或已失败走重试按钮）→ 不重复发请求；
      // refresh 内部会按窗口覆盖情况决定是否带 if_changed_since。
      if (!fetchedRef.current && reqSeqRef.current > 0) return;
      void refresh();
    }, 300);
    return () => clearTimeout(t);
  }, [view.since, view.until, projectId, refresh]);

  // 30s 兜底轮询（仅挂载期间；WS 断连/丢事件时退化为它）
  useEffect(() => {
    if (!projectId) return;
    const t = setInterval(() => void refresh(), 30_000);
    return () => clearInterval(t);
  }, [projectId, refresh]);

  // WS 失效信号（store 已做 1s 合并 + 项目过滤）
  useEffect(() => {
    if (!projectId || timelineVersion === 0) return;
    void refresh();
  }, [timelineVersion, projectId, refresh]);

  // 每秒本地 tick（now 线 / 进行中段 / 等待时长）
  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  // 游戏时间锚点（Day 分界用；失败则隐藏 Day 标注）
  useEffect(() => {
    if (!projectId) {
      setAnchor(null);
      return;
    }
    let cancelled = false;
    getProjectGameTime(projectId)
      .then((d: any) => {
        if (cancelled) return;
        setAnchor({
          atRealMs: Date.now(),
          gameSeconds: typeof d.gameSeconds === "number" ? d.gameSeconds : 0,
          realSecondsPerGameDay:
            typeof d.realSecondsPerGameDay === "number" ? d.realSecondsPerGameDay : 3600,
        });
      })
      .catch(() => {
        if (!cancelled) setAnchor(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // 深链写侧：view/taskId/since/until 同步进 hash（400ms 防抖）
  useDeepLinkWriter({
    active: !!projectId,
    taskId: selectedTaskId,
    since: view.since,
    until: view.until,
  });

  // ── 泳道构建（agents 树序 + 客户端过滤） ─────────────────
  const lanes = useMemo<LaneData[]>(() => {
    if (!data) return [];
    const kw = keyword.trim().toLowerCase();
    const segs = (data.task_segments ?? []).filter((s) => {
      if (kw && !s.title.toLowerCase().includes(kw)) return false;
      if (statusFilter.size > 0 && !statusFilter.has(s.status)) return false;
      return true;
    });

    // parent_id 自建树 → DFS 排序（store 无 org tree 数据，端点 2 自带层级）
    const byParent = new Map<string, TeamAgent[]>();
    for (const a of data.agents ?? []) {
      const key = a.parent_id || "";
      const arr = byParent.get(key) ?? [];
      arr.push(a);
      byParent.set(key, arr);
    }
    const order: TeamAgent[] = [];
    const seen = new Set<string>();
    const visit = (parentId: string) => {
      for (const a of byParent.get(parentId) ?? []) {
        if (seen.has(a.id)) continue; // 环保护
        seen.add(a.id);
        order.push(a);
        visit(a.id);
      }
    };
    visit("");
    for (const a of data.agents ?? []) {
      if (!seen.has(a.id)) order.push(a); // 孤儿兜底
    }

    const laneAgents = agentFilter.size > 0 ? order.filter((a) => agentFilter.has(a.id)) : order;

    const byAssignee = new Map<string, TaskSegment[]>();
    const unclaimed: TaskSegment[] = [];
    for (const s of segs) {
      if (!s.assignee_id) {
        unclaimed.push(s);
        continue;
      }
      const arr = byAssignee.get(s.assignee_id) ?? [];
      arr.push(s);
      byAssignee.set(s.assignee_id, arr);
    }
    const assignmentByAgent = new Map<string, ActiveAssignment>();
    for (const aa of data.active_assignments ?? []) assignmentByAgent.set(aa.agent_id, aa);

    const sortSegs = (arr: TaskSegment[]) => arr.sort((x, y) => x.started_at - y.started_at);
    const out: LaneData[] = laneAgents.map((a) => ({
      key: a.id,
      name: a.name || `${a.id.slice(0, 8)}…`,
      role: a.role,
      healthError: Boolean(agentHealth[a.id]),
      assignment: assignmentByAgent.get(a.id) ?? null,
      segments: sortSegs(byAssignee.get(a.id) ?? []),
    }));
    if (unclaimed.length > 0 && agentFilter.size === 0) {
      out.push({
        key: "__unclaimed__",
        name: "待认领",
        role: null,
        segments: sortSegs(unclaimed),
        virtual: true,
      });
    }
    return out;
  }, [data, keyword, statusFilter, agentFilter, agentHealth]);

  const agentMap = useMemo(() => {
    const m: Record<string, TeamAgent> = {};
    for (const a of data?.agents ?? []) m[a.id] = a;
    return m;
  }, [data]);

  const presentStatuses = useMemo(() => {
    const s = new Set<string>();
    for (const seg of data?.task_segments ?? []) s.add(seg.status);
    return [...s];
  }, [data]);

  // ── 交互 ─────────────────────────────────────────────────
  const applyPreset = (ms: number | "all") => {
    const n = Date.now();
    if (ms === "all") {
      const proj = projects.find((p) => p.id === projectId);
      const start = proj?.createdAt ? proj.createdAt - 60e3 : n - 7 * 24 * 3600e3;
      pan.setView({ since: start, until: n + 5 * 60e3 });
    } else {
      pan.setView({ since: n - ms, until: n });
    }
  };

  const toggleSet = (set: Set<string>, value: string, apply: (s: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    apply(next);
  };

  const handleHover = useCallback((seg: TaskSegment | null, e?: React.MouseEvent) => {
    if (seg && e) setTooltip({ seg, x: e.clientX, y: e.clientY });
    else setTooltip(null);
  }, []);

  // Day 分界带（交替底色）
  const bands = useMemo(() => {
    const bds = dayBoundaries(view.since, view.until, anchor);
    const span = Math.max(1, view.until - view.since);
    const out: Array<{ key: string; left: number; width: number; day: number }> = [];
    for (let i = 0; i < bds.length; i++) {
      const start = bds[i].ts;
      const end = i + 1 < bds.length ? bds[i + 1].ts : view.until;
      if (bds[i].day % 2 !== 0) continue; // 只给偶数日上底色，形成交替
      out.push({
        key: `band-${bds[i].day}`,
        left: ((start - view.since) / span) * 100,
        width: ((end - start) / span) * 100,
        day: bds[i].day,
      });
    }
    return out;
  }, [view.since, view.until, anchor]);

  const nowPct =
    nowMs >= view.since && nowMs <= view.until
      ? ((nowMs - view.since) / Math.max(1, view.until - view.since)) * 100
      : null;

  // ── 渲染 ─────────────────────────────────────────────────
  return (
    <div className="h-full flex flex-col overflow-hidden relative bg-g-bg">
      {/* 工具条：预设档位 + 缩放 + 筛选 */}
      <div className="px-3 py-2 border-b border-g-border bg-white/80 backdrop-blur-sm shrink-0 space-y-1.5">
        <div className="flex items-center gap-2">
          <div className="flex gap-0.5 bg-g-bg-muted rounded-full p-0.5">
            {PRESETS.map((p) => (
              <button
                key={p.label}
                onClick={() => applyPreset(p.ms)}
                className="px-2 py-0.5 text-[10px] rounded-full text-g-fg-3 hover:text-g-fg hover:bg-white transition-colors"
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-0.5">
            <button
              onClick={() => pan.zoomBy(1.25)}
              className="w-6 h-6 flex items-center justify-center rounded text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted transition-colors text-sm"
              title="放大"
            >
              +
            </button>
            <button
              onClick={() => pan.zoomBy(0.8)}
              className="w-6 h-6 flex items-center justify-center rounded text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted transition-colors text-sm"
              title="缩小"
            >
              −
            </button>
          </div>
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="筛选任务标题…"
            className="ml-auto w-40 px-2 py-1 text-[11px] rounded-gm border border-g-border bg-g-bg text-g-fg placeholder:text-g-fg-4 focus:outline-none focus:border-g-border-focus transition-colors"
          />
        </div>
        {(presentStatuses.length > 0 || (data?.agents?.length ?? 0) > 0) && (
          <div className="flex flex-wrap items-center gap-1">
            {presentStatuses.map((s) => {
              const st = statusStyle(s);
              const active = statusFilter.has(s);
              return (
                <button
                  key={s}
                  onClick={() => toggleSet(statusFilter, s, setStatusFilter)}
                  className={`px-1.5 py-px rounded-full text-[10px] border transition-colors ${
                    active
                      ? `${st.chipBg} ${st.chipText} border-current`
                      : "border-g-border text-g-fg-4 hover:text-g-fg"
                  }`}
                >
                  {st.label}
                </button>
              );
            })}
            {(data?.agents?.length ?? 0) > 0 && (data?.agents?.length ?? 0) <= 12 && (
              <>
                <span className="w-px h-3 bg-g-border mx-0.5" />
                {(data?.agents ?? []).map((a) => {
                  const active = agentFilter.has(a.id);
                  return (
                    <button
                      key={a.id}
                      onClick={() => toggleSet(agentFilter, a.id, setAgentFilter)}
                      className={`px-1.5 py-px rounded-full text-[10px] border transition-colors truncate max-w-[90px] ${
                        active
                          ? "bg-g-blue-bg text-g-blue border-g-blue/40"
                          : "border-g-border text-g-fg-4 hover:text-g-fg"
                      }`}
                    >
                      {a.name}
                    </button>
                  );
                })}
              </>
            )}
          </div>
        )}
      </div>

      {/* 主体 */}
      <div className="flex-1 min-h-0 relative">
        {initialLoading ? (
          <div className="absolute inset-0 p-4 space-y-2">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <div
                key={i}
                className="h-9 rounded-gm animate-shimmer"
                style={{
                  backgroundImage:
                    "linear-gradient(90deg, #eff1f4 25%, #e3e6eb 50%, #eff1f4 75%)",
                  backgroundSize: "200% 100%",
                }}
              />
            ))}
          </div>
        ) : error ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center px-6">
            <span className="text-2xl">⚠️</span>
            <p className="text-sm text-g-red">{error}</p>
            <button
              onClick={() => void refresh(true)}
              className="px-3 py-1 text-xs rounded-gm border border-g-border text-g-fg-2 hover:bg-g-bg-soft transition-colors"
            >
              重试
            </button>
          </div>
        ) : !data || (data.agents?.length ?? 0) === 0 ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 text-center px-6">
            <span className="text-2xl">🏜️</span>
            <p className="text-sm text-g-fg-4">暂无团队活动</p>
            <p className="text-[11px] text-g-fg-4/70">
              项目还没有成员或任务事件；试试切换时间范围或「全部」
            </p>
          </div>
        ) : (
          <div
            ref={containerRef}
            {...pan.bind}
            className={`absolute inset-0 overflow-y-auto overflow-x-hidden select-none ${
              pan.isDragging ? "cursor-grabbing" : "cursor-grab"
            }`}
          >
            {/* 顶部时间轴（纵向滚动 sticky） */}
            <div className="sticky top-0 z-20 flex bg-g-bg border-b border-g-border shadow-gm-sm">
              <div
                className="shrink-0 border-r border-g-border px-2.5 flex items-end pb-1 text-[9px] text-g-fg-4"
                style={{ width: LANE_LABEL_W }}
              >
                成员 / 时间
              </div>
              <div className="relative flex-1">
                <TimeAxis since={view.since} until={view.until} anchor={anchor} />
              </div>
            </div>

            {/* 泳道区（Day 分界带 + now 线叠层） */}
            <div className="relative">
              {bands.map((b) => (
                <div
                  key={b.key}
                  className="absolute inset-y-0 bg-slate-100/60 pointer-events-none"
                  style={{ left: `${b.left}%`, width: `${b.width}%` }}
                />
              ))}
              {lanes.map((lane, i) => (
                <TeamTimelineLane
                  key={lane.key}
                  lane={lane}
                  view={view}
                  nowMs={nowMs}
                  zebra={i % 2 === 1}
                  onSelectTask={setSelectedTask}
                  onHover={handleHover}
                />
              ))}
              {nowPct !== null && (
                <div
                  className="absolute inset-y-0 z-10 pointer-events-none"
                  style={{ left: `${nowPct}%` }}
                >
                  <div className="w-px h-full bg-g-red" />
                  <span className="absolute top-0 -left-[3px] w-[7px] h-[7px] rounded-full bg-g-red animate-ping-ring" />
                </div>
              )}
            </div>
          </div>
        )}

        {/* 截断/更早数据提示（v4：不静默截断） */}
        {!initialLoading && !error && data && (hint.truncated || hint.hasEarlier) && (
          <div className="absolute top-1.5 left-1/2 -translate-x-1/2 z-30 px-2.5 py-1 rounded-full bg-amber-50 border border-amber-200 text-amber-700 text-[10px] shadow-gm-sm pointer-events-none whitespace-nowrap">
            {hint.truncated
              ? "窗口内事件超限，部分内容未展示——请缩小时间范围"
              : "还有更早的活动数据——放大或左移窗口可查看"}
          </div>
        )}

        {/* 「跳到现在」浮动按钮（视口不含当前时刻时出现） */}
        {!initialLoading && !isLiveWindow(view, nowMs) && (
          <button
            onClick={pan.jumpToNow}
            className="absolute bottom-3 right-3 z-30 flex items-center gap-1 px-3 py-1.5 text-xs font-medium text-white bg-g-blue rounded-full shadow-gm-md hover:shadow-gm-lg active:scale-95 transition-all animate-slide-up"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            跳到现在
          </button>
        )}
      </div>

      {/* MiniMap（有数据才渲染） */}
      {data && (
        <MiniMap
          segments={data.task_segments ?? []}
          view={view}
          onJump={pan.setView}
          nowMs={nowMs}
        />
      )}

      <TimelineTooltip tip={tooltip} agents={agentMap} nowMs={nowMs} />
    </div>
  );
}
