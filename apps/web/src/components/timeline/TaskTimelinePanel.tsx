/**
 * TaskTimelinePanel — 单任务全链路回放（Timeline v4 §5.2，P1 核心组件）。
 *
 * 元信息卡 + 垂直事件流（按游戏日分组折叠，§5.5.8）。数据源是 REST
 * 端点 1（WS task_event 只是失效信号 → store.timelineVersion 触发重拉）。
 * 三态先例：空态照 WorkLogPanel，错误+重试照 MonitorPanel，骨架屏新写
 * （tailwind.config.js 已定义的 shimmer 动画）。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { getProjectGameTime, getTaskTimeline } from "../../api";
import { useAppStore } from "../../store";
import { formatClock, formatDateTime } from "../../utils/format";
import type { TaskTimelineResponse, TimelineEvent } from "./types";
import type { GameTimeAnchor } from "./utils";
import {
  buildTaskTimelineMarkdown,
  eventAccent,
  gameClockOf,
  gameDayOf,
  realDayKey,
  statusStyle,
} from "./utils";

// ── 游戏日分组 ─────────────────────────────────────────────────

interface DayGroup {
  key: string;
  label: string;
  events: TimelineEvent[];
}

function groupByDay(
  events: TimelineEvent[],
  anchor: GameTimeAnchor | null,
): DayGroup[] {
  const map = new Map<string, DayGroup>();
  for (const ev of events) {
    let key: string;
    let label: string;
    if (anchor) {
      const day = gameDayOf(ev.ts, anchor);
      key = `day-${day}`;
      label = `第 ${day} 天`;
    } else {
      key = realDayKey(ev.ts);
      label = key; // 无锚点降级：现实日期
    }
    let g = map.get(key);
    if (!g) {
      g = { key, label, events: [] };
      map.set(key, g);
    }
    g.events.push(ev);
  }
  return [...map.values()];
}

/** 特殊 reason_code 徽章（其余 reason 已含在后端 title 里）。 */
function reasonChip(ev: TimelineEvent): { label: string; cls: string } | null {
  if (ev.reason_code === "review_rework") {
    return { label: "打回", cls: "bg-g-red-bg text-g-red" };
  }
  if (ev.reason_code === "dependency_fulfilled") {
    return { label: "依赖解除", cls: "bg-g-green-bg text-g-green" };
  }
  return null;
}

// ── 骨架屏 ─────────────────────────────────────────────────────

function Skeleton() {
  return (
    <div className="p-4 space-y-4">
      <div
        className="h-16 rounded-gm animate-shimmer"
        style={{
          backgroundImage:
            "linear-gradient(90deg, #eff1f4 25%, #e3e6eb 50%, #eff1f4 75%)",
          backgroundSize: "200% 100%",
        }}
      />
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="flex gap-2">
          <div
            className="h-3 w-14 rounded animate-shimmer"
            style={{
              backgroundImage:
                "linear-gradient(90deg, #eff1f4 25%, #e3e6eb 50%, #eff1f4 75%)",
              backgroundSize: "200% 100%",
            }}
          />
          <div
            className="h-3 flex-1 rounded animate-shimmer"
            style={{
              backgroundImage:
                "linear-gradient(90deg, #eff1f4 25%, #e3e6eb 50%, #eff1f4 75%)",
              backgroundSize: "200% 100%",
            }}
          />
        </div>
      ))}
    </div>
  );
}

// ── 元信息卡 ───────────────────────────────────────────────────

/** 复制任务链路为 Markdown（v4 §5.5.2），clipboard API + textarea 兜底。 */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }
}

function MetaCard({
  data,
  anchor,
}: {
  data: TaskTimelineResponse;
  anchor: GameTimeAnchor | null;
}) {
  const t = data.task;
  const st = statusStyle(t.status);
  const [copyState, setCopyState] = useState<"idle" | "ok" | "fail">("idle");
  const copyTimer = useRef<number | null>(null);
  // 卸载清理悬挂定时器（连续点击也只保留最后一个）
  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    },
    [],
  );
  const agentName = (id?: string | null): string | null => {
    if (!id) return null;
    return data.agents[id]?.name || `${id.slice(0, 8)}…`;
  };
  const assignee = agentName(t.assignee_id);
  const reviewer = agentName(t.reviewer_id);
  const creator = agentName(t.creator_id);

  const onCopy = async () => {
    const ok = await copyText(buildTaskTimelineMarkdown(data, anchor));
    setCopyState(ok ? "ok" : "fail");
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopyState("idle"), 1500);
  };

  return (
    <div className="border-b border-g-border bg-g-bg-soft px-4 py-3">
      <div className="flex items-start gap-2">
        <h3 className="flex-1 min-w-0 text-sm font-medium text-g-fg leading-5">
          {t.title}
        </h3>
        <button
          onClick={onCopy}
          title="复制任务链路为 Markdown（贴给 AI 分析卡在哪）"
          className={`shrink-0 px-2 py-0.5 rounded-gm border text-[11px] transition-colors ${
            copyState === "ok"
              ? "border-g-green text-g-green"
              : copyState === "fail"
                ? "border-g-red text-g-red"
                : "border-g-border text-g-fg-3 hover:bg-g-bg-muted hover:text-g-fg-2"
          }`}
        >
          {copyState === "ok" ? "已复制 ✓" : copyState === "fail" ? "复制失败" : "复制 MD"}
        </button>
        <span
          className={`shrink-0 px-2 py-0.5 rounded-full text-[11px] font-medium ${st.chipBg} ${st.chipText}`}
        >
          {st.label}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-g-fg-3">
        <span className="font-mono text-g-fg-4">{t.id.slice(0, 8)}</span>
        {assignee && <span>负责人 {assignee}</span>}
        {reviewer && <span>评审人 {reviewer}</span>}
        {creator && <span>创建者 {creator}</span>}
        {typeof t.progress === "number" && <span>进度 {Math.round(t.progress)}%</span>}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-g-fg-4">
        {typeof t.created_at === "number" && (
          <span>创建 {formatDateTime(t.created_at)}</span>
        )}
        {typeof t.closed_at === "number" && (
          <span>结束 {formatDateTime(t.closed_at)}</span>
        )}
        {!!t.is_archived && (
          <span className="px-1.5 py-0.5 rounded bg-g-bg-muted text-g-fg-3">已归档</span>
        )}
      </div>
      {t.blocked_reason && (
        <p className="mt-1 text-[11px] text-orange-600">阻塞原因：{t.blocked_reason}</p>
      )}
    </div>
  );
}

// ── 事件行 ─────────────────────────────────────────────────────

function EventRow({
  ev,
  data,
  anchor,
}: {
  ev: TimelineEvent;
  data: TaskTimelineResponse;
  anchor: GameTimeAnchor | null;
}) {
  const accent = eventAccent(ev);
  const chip = reasonChip(ev);

  // 交接事件展示 from → to；其余事件展示当事 agent
  let who: string | null = null;
  if (ev.type === "handoff.created" && ev.from_agent_id && ev.to_agent_id) {
    const a = data.agents[ev.from_agent_id]?.name || ev.from_agent_id.slice(0, 8);
    const b = data.agents[ev.to_agent_id]?.name || ev.to_agent_id.slice(0, 8);
    who = `${a} → ${b}`;
  } else if (ev.agent_id) {
    who = data.agents[ev.agent_id]?.name || null;
  }

  return (
    <div className="relative pb-3 animate-fade-in">
      <span
        className={`absolute -left-[21px] top-1.5 w-2.5 h-2.5 rounded-full ring-2 ring-g-bg ${accent.dot}`}
      />
      <div className="flex items-start justify-between gap-2">
        <span className="flex-1 min-w-0 text-xs text-g-fg leading-5">
          {ev.title}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-g-fg-4 leading-5">
          {formatClock(ev.ts)}
          {anchor ? ` · 游戏 ${gameClockOf(ev.ts, anchor)}` : ""}
        </span>
      </div>
      {(chip || who) && (
        <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
          {chip && (
            <span className={`px-1.5 py-px rounded text-[10px] font-medium ${chip.cls}`}>
              {chip.label}
            </span>
          )}
          {who && <span className="text-[10px] text-g-fg-3">{who}</span>}
        </div>
      )}
    </div>
  );
}

// ── 主组件 ─────────────────────────────────────────────────────

export default function TaskTimelinePanel() {
  const projectId = useAppStore((s) => s.selectedProjectId);
  const taskId = useAppStore((s) => s.selectedTaskId);
  const timelineVersion = useAppStore((s) => s.timelineVersion);

  const [data, setData] = useState<TaskTimelineResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [anchor, setAnchor] = useState<GameTimeAnchor | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // 游戏时间锚点（分组用；失败降级为现实日期分组）
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
            typeof d.realSecondsPerGameDay === "number"
              ? d.realSecondsPerGameDay
              : 3600,
        });
      })
      .catch(() => {
        if (!cancelled) setAnchor(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // 端点 1：task_id / 项目 / 失效信号变化都重拉
  useEffect(() => {
    if (!projectId || !taskId) {
      setData(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    getTaskTimeline(projectId, taskId)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: any) => {
        if (cancelled || e?._aborted) return;
        setError(e?.message || "任务时间轴加载失败");
        setData(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, taskId, timelineVersion, reloadKey]);

  // 换任务时重置折叠状态
  useEffect(() => {
    setCollapsed(new Set());
  }, [taskId]);

  const groups = useMemo(
    () => (data ? groupByDay(data.events, anchor) : []),
    [data, anchor],
  );

  const toggleDay = (key: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  if (!taskId) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-1.5 text-center px-6">
        <span className="text-2xl">🗂️</span>
        <p className="text-sm text-g-fg-4">未选中任务</p>
        <p className="text-[11px] text-g-fg-4/70">
          在时间轴视图里用任务选择器打开一个任务
        </p>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {loading && !data ? (
        <Skeleton />
      ) : error ? (
        <div className="h-full flex flex-col items-center justify-center gap-2 px-6 text-center">
          <span className="text-2xl">⚠️</span>
          <p className="text-sm text-g-red">{error}</p>
          <p className="text-[11px] text-g-fg-4/70">
            task_id 直达时请确认任务存在于本项目（含已归档）
          </p>
          <button
            onClick={() => setReloadKey((k) => k + 1)}
            className="mt-1 px-3 py-1 text-xs rounded-gm border border-g-border text-g-fg-2 hover:bg-g-bg-soft transition-colors"
          >
            重试
          </button>
        </div>
      ) : data ? (
        <>
          <MetaCard data={data} anchor={anchor} />
          {data.truncated && (
            <div className="px-4 py-1.5 text-[11px] text-amber-700 bg-g-yellow-bg border-b border-g-border">
              事件超出预算，仅保留最新部分 —— 窗口最早的事件可能缺失
            </div>
          )}
          <div className="flex-1 overflow-y-auto px-4 py-3">
            {data.events.length === 0 ? (
              <div className="py-8 flex flex-col items-center justify-center gap-1.5 text-center">
                <span className="text-2xl">📭</span>
                <p className="text-sm text-g-fg-4">暂无事件</p>
                <p className="text-[11px] text-g-fg-4/70">
                  该任务还没有产生任何流转记录
                </p>
              </div>
            ) : (
              groups.map((g) => {
                const isCollapsed = collapsed.has(g.key);
                return (
                  <div key={g.key} className="mb-1">
                    <button
                      onClick={() => toggleDay(g.key)}
                      className="w-full flex items-center gap-1.5 py-1 text-[11px] font-medium text-g-fg-3 hover:text-g-fg transition-colors"
                    >
                      <svg
                        className={`w-3 h-3 transition-transform ${isCollapsed ? "-rotate-90" : ""}`}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={2}
                          d="M19 9l-7 7-7-7"
                        />
                      </svg>
                      <span>{g.label}</span>
                      <span className="text-g-fg-4 font-normal">
                        {g.events.length} 个事件
                      </span>
                    </button>
                    {!isCollapsed && (
                      <div className="relative ml-1 border-l border-g-border pl-4 mt-1">
                        {g.events.map((ev) => (
                          <EventRow
                            key={`${ev.type}:${ev.id}`}
                            ev={ev}
                            data={data}
                            anchor={anchor}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </>
      ) : null}
    </div>
  );
}
