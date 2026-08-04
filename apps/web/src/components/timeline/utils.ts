/**
 * Timeline 视觉与换算工具（Timeline v4 §5.2 / §5.3）。
 *
 * 颜色纪律：状态色集中本文件一张表，显式枚举全部落库状态（含
 * cancelled，v4 §九）；全部是静态完整类名（Tailwind JIT 扫描要求，
 * 禁止模板字符串拼接类名）。先例：utils/role-styles.ts。
 */

import type { CSSProperties } from "react";
import type { TaskTimelineResponse, TimelineEvent } from "./types";
import { formatClock, formatDateTime } from "../../utils/format";

/** 落库的全部任务状态（tasks.status 值域，terminal: closed/cancelled）。 */
export type TaskStatus =
  | "created"
  | "claimed"
  | "running"
  | "rework"
  | "blocked"
  | "submitted"
  | "reviewing"
  | "approved"
  | "verifying"
  | "closed"
  | "cancelled";

export interface StatusStyle {
  /** 中文标签 */
  label: string;
  /** 泳道任务块底色（22px 圆角块） */
  bar: string;
  /** 块内文字色 */
  barText: string;
  /** 详情/事件徽章用浅色底 + 深色字 */
  chipBg: string;
  chipText: string;
  /** cancelled 斜纹 / 待认领虚线等纹理标记 */
  striped?: boolean;
}

/**
 * 状态色表（v4 §5.3）：running=emerald-500、waiting=amber-400、
 * blocked=orange-500、reviewing=violet-500、approved/done=sky-500、
 * cancelled=slate-400+斜纹、待认领空段=灰底虚线边框。
 */
export const STATUS_STYLES: Record<TaskStatus, StatusStyle> = {
  created: {
    label: "待认领",
    bar: "bg-slate-200",
    barText: "text-slate-600",
    chipBg: "bg-slate-100",
    chipText: "text-slate-600",
  },
  claimed: {
    label: "已认领",
    bar: "bg-emerald-200",
    barText: "text-emerald-900",
    chipBg: "bg-emerald-50",
    chipText: "text-emerald-700",
  },
  running: {
    label: "执行中",
    bar: "bg-emerald-500",
    barText: "text-white",
    chipBg: "bg-emerald-50",
    chipText: "text-emerald-700",
  },
  rework: {
    label: "返工",
    bar: "bg-emerald-500",
    barText: "text-white",
    chipBg: "bg-red-50",
    chipText: "text-red-600",
  },
  blocked: {
    label: "阻塞",
    bar: "bg-orange-500",
    barText: "text-white",
    chipBg: "bg-orange-50",
    chipText: "text-orange-700",
  },
  submitted: {
    label: "待评审",
    bar: "bg-amber-400",
    barText: "text-amber-950",
    chipBg: "bg-amber-50",
    chipText: "text-amber-700",
  },
  reviewing: {
    label: "评审中",
    bar: "bg-violet-500",
    barText: "text-white",
    chipBg: "bg-violet-50",
    chipText: "text-violet-700",
  },
  approved: {
    label: "已通过",
    bar: "bg-sky-500",
    barText: "text-white",
    chipBg: "bg-sky-50",
    chipText: "text-sky-700",
  },
  verifying: {
    label: "验证中",
    bar: "bg-sky-300",
    barText: "text-sky-950",
    chipBg: "bg-sky-50",
    chipText: "text-sky-700",
  },
  closed: {
    label: "已完成",
    bar: "bg-sky-500",
    barText: "text-white",
    chipBg: "bg-sky-50",
    chipText: "text-sky-700",
  },
  cancelled: {
    label: "已取消",
    bar: "bg-slate-400",
    barText: "text-white",
    chipBg: "bg-slate-100",
    chipText: "text-slate-500",
    striped: true,
  },
};

const FALLBACK_STYLE: StatusStyle = STATUS_STYLES.created;

export function statusStyle(status: string | null | undefined): StatusStyle {
  if (!status) return FALLBACK_STYLE;
  return (
    STATUS_STYLES[(status.toLowerCase() as TaskStatus)] ?? FALLBACK_STYLE
  );
}

/** cancelled 斜纹覆层（repeating-linear-gradient，v4 §5.3 空段纹理同款）。 */
export const STRIPED_OVERLAY: CSSProperties = {
  backgroundImage:
    "repeating-linear-gradient(45deg, rgba(255,255,255,0.35) 0px, rgba(255,255,255,0.35) 4px, transparent 4px, transparent 8px)",
};

/** 待认领空段（agent 无任务区间）：灰底虚线边框，与空白区分。 */
export const UNCLAIMED_SEGMENT_CLASSES =
  "bg-slate-100 border border-dashed border-slate-300";

// ── 事件流视觉 ─────────────────────────────────────────────────

/** 事件圆点/徽章配色（按事件类别，状态事件再按 to_status 细分）。 */
export function eventAccent(ev: TimelineEvent): {
  dot: string;
  chipBg: string;
  chipText: string;
} {
  if (ev.type === "handoff.created") {
    return { dot: "bg-g-blue", chipBg: "bg-g-blue-bg", chipText: "text-g-blue" };
  }
  if (ev.type === "inbox.message") {
    return { dot: "bg-teal-500", chipBg: "bg-teal-50", chipText: "text-teal-700" };
  }
  if (ev.type === "work_log") {
    return {
      dot: "bg-g-fg-4",
      chipBg: "bg-g-bg-muted",
      chipText: "text-g-fg-3",
    };
  }
  // 打回（reviewing→running + review_rework）红色锚点（v4 §5.5.7 先落最小版）
  if (ev.type === "task.running" && ev.reason_code === "review_rework") {
    return { dot: "bg-g-red", chipBg: "bg-g-red-bg", chipText: "text-g-red" };
  }
  const st = statusStyle(ev.to_status);
  return { dot: st.bar, chipBg: st.chipBg, chipText: st.chipText };
}

// ── 游戏日换算（事件分组 / Day 标签）──────────────────────────

/**
 * 游戏时间锚点：某一现实时刻对应的 gameSeconds。
 * 来自 GET /projects/{pid}/game-time（取响应瞬间本地打锚）。
 * 项目暂停期间游戏时间不走，线性外推会有漂移——只用于展示分组，
 * 与 ProjectTimeBadge 的本地外推同精度。
 */
export interface GameTimeAnchor {
  atRealMs: number;
  gameSeconds: number;
  /** 现实秒 / 游戏日（默认 3600） */
  realSecondsPerGameDay: number;
}

const GAME_SECONDS_PER_DAY = 86_400;

/** 给定锚点，求某现实时刻的游戏秒数（可为负 → clamp 到 0）。 */
export function gameSecondsAt(realMs: number, anchor: GameTimeAnchor): number {
  const rate =
    GAME_SECONDS_PER_DAY / Math.max(1, anchor.realSecondsPerGameDay);
  const gs =
    anchor.gameSeconds + ((realMs - anchor.atRealMs) / 1000) * rate;
  return Math.max(0, Math.floor(gs));
}

/** 现实毫秒 → 游戏日序号（Day N 的 N）。 */
export function gameDayOf(realMs: number, anchor: GameTimeAnchor): number {
  return Math.floor(gameSecondsAt(realMs, anchor) / GAME_SECONDS_PER_DAY);
}

/** Day 内时钟标签：把游戏秒折成 HH:MM（游戏时）。 */
export function gameClockOf(realMs: number, anchor: GameTimeAnchor): string {
  const gs = gameSecondsAt(realMs, anchor) % GAME_SECONDS_PER_DAY;
  const h = Math.floor(gs / 3600);
  const m = Math.floor((gs % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/** 无锚点降级：按现实日期分组（YYYY-MM-DD）。 */
export function realDayKey(realMs: number): string {
  const d = new Date(realMs);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// ── Markdown 导出（v4 §5.5.2：一键复制任务链路）───────────────

/** 表格单元格转义（| 与换行会破坏 Markdown 表格）。 */
function mdCell(s: string): string {
  return s.replace(/\|/g, "\\|").replace(/\r?\n+/g, " ");
}

/** 事件当事人描述（与 EventRow 的 who 逻辑同口径）。 */
function mdWho(ev: TimelineEvent, data: TaskTimelineResponse): string {
  const name = (id: string) => data.agents[id]?.name || id.slice(0, 8);
  if (ev.type === "handoff.created" && ev.from_agent_id && ev.to_agent_id) {
    return `${name(ev.from_agent_id)} → ${name(ev.to_agent_id)}`;
  }
  return ev.agent_id ? name(ev.agent_id) : "";
}

/**
 * 把单任务事件流渲染成紧凑 Markdown 表格——贴给同事/AI 分析
 * 「任务卡在哪」是最高频下游动作（v4 §5.5.2）。
 */
export function buildTaskTimelineMarkdown(
  data: TaskTimelineResponse,
  anchor: GameTimeAnchor | null,
): string {
  const t = data.task;
  const st = statusStyle(t.status);
  const agentName = (id?: string | null) =>
    id ? data.agents[id]?.name || `${id.slice(0, 8)}…` : null;

  const lines: string[] = [];
  lines.push(`## 任务链路：${t.title}`);
  lines.push("");
  const meta: string[] = [
    `task_id: \`${t.id}\``,
    `状态: ${st.label}（${t.status}）`,
  ];
  if (typeof t.progress === "number")
    meta.push(`进度: ${Math.round(t.progress)}%`);
  const assignee = agentName(t.assignee_id);
  const reviewer = agentName(t.reviewer_id);
  const creator = agentName(t.creator_id);
  if (assignee) meta.push(`负责人: ${assignee}`);
  if (reviewer) meta.push(`评审人: ${reviewer}`);
  if (creator) meta.push(`创建者: ${creator}`);
  if (typeof t.created_at === "number")
    meta.push(`创建: ${formatDateTime(t.created_at)}`);
  if (typeof t.closed_at === "number")
    meta.push(`结束: ${formatDateTime(t.closed_at)}`);
  if (t.is_archived) meta.push("已归档");
  lines.push(meta.join(" ｜ "));
  if (t.blocked_reason) lines.push(`\n阻塞原因：${t.blocked_reason}`);
  lines.push("");

  const head = anchor
    ? "| 时间 | 游戏时 | 事件 | 当事人 |"
    : "| 时间 | 事件 | 当事人 |";
  const sep = anchor ? "|---|---|---|---|" : "|---|---|---|";
  lines.push(head, sep);
  for (const ev of data.events) {
    const chip =
      ev.reason_code === "review_rework"
        ? "【打回】"
        : ev.reason_code === "dependency_fulfilled"
          ? "【依赖解除】"
          : "";
    const cells = anchor
      ? [
          formatClock(ev.ts),
          `Day${gameDayOf(ev.ts, anchor)} ${gameClockOf(ev.ts, anchor)}`,
          `${chip}${mdCell(ev.title)}`,
          mdCell(mdWho(ev, data)),
        ]
      : [formatClock(ev.ts), `${chip}${mdCell(ev.title)}`, mdCell(mdWho(ev, data))];
    lines.push(`| ${cells.join(" | ")} |`);
  }
  if (data.truncated) {
    lines.push("", "> 注：事件超出预算被截断，最早部分缺失。");
  }
  return lines.join("\n");
}
