/**
 * TimelineTooltip — 任务块 hover 浮层卡片（Timeline v4 §5.2/§5.3）。
 * 内容：标题/状态/起止/负责人/评审人。pointer-events-none，跟随光标。
 */

import type { AgentRef, TaskSegment } from "./types";
import { formatDateTime, formatDuration } from "../../utils/format";
import { statusStyle } from "./utils";

export interface TooltipState {
  seg: TaskSegment;
  x: number;
  y: number;
}

export default function TimelineTooltip({
  tip,
  agents,
  nowMs,
}: {
  tip: TooltipState | null;
  agents: Record<string, AgentRef>;
  nowMs: number;
}) {
  if (!tip) return null;
  const { seg, x, y } = tip;
  const st = statusStyle(seg.status);
  const end = seg.ended_at ?? nowMs;
  const name = (id?: string | null) =>
    id ? agents[id]?.name || `${id.slice(0, 8)}…` : null;

  return (
    <div
      className="fixed z-50 pointer-events-none max-w-[280px] rounded-gm border border-g-border bg-g-bg shadow-gm-pop px-3 py-2 animate-fade-in"
      style={{
        left: Math.max(8, Math.min(x + 12, window.innerWidth - 300)),
        top: Math.max(8, Math.min(y + 14, window.innerHeight - 150)),
      }}
    >
      <p className="text-xs font-medium text-g-fg leading-4">{seg.title}</p>
      <div className="mt-1 flex items-center gap-1.5">
        <span className={`px-1.5 py-px rounded text-[10px] ${st.chipBg} ${st.chipText}`}>
          {st.label}
        </span>
        <span className="text-[10px] text-g-fg-4 font-mono">
          {formatDuration(end - seg.started_at)}
        </span>
      </div>
      <div className="mt-1 space-y-0.5 text-[10px] text-g-fg-3">
        <p>
          {formatDateTime(seg.started_at)} → {seg.ended_at ? formatDateTime(seg.ended_at) : "进行中"}
        </p>
        {name(seg.assignee_id) && <p>负责人 {name(seg.assignee_id)}</p>}
        {name(seg.reviewer_id) && <p>评审人 {name(seg.reviewer_id)}</p>}
      </div>
    </div>
  );
}
