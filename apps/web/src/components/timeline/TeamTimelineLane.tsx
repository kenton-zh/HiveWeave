/**
 * TeamTimelineLane — 单泳道行 + 任务段渲染（Timeline v4 §5.2）。
 *
 * 段定位 = (时刻 - 视口起点) / 视口跨度 的百分比，父级保证窗口变化时
 * 整体重算 —— 不用 CSS transform，文字不形变。
 */

import type { ActiveAssignment, TaskSegment } from "./types";
import type { TimeViewport } from "./usePanZoom";
import { formatDuration } from "../../utils/format";
import { statusStyle, STRIPED_OVERLAY, UNCLAIMED_SEGMENT_CLASSES } from "./utils";

export interface LaneData {
  /** agent id；虚拟泳道用 "__unclaimed__" */
  key: string;
  name: string;
  role?: string | null;
  /** store.agentHealth 有错误记录时为 "error" */
  healthError?: boolean;
  /** 当前活跃任务（busy/waiting），标签列显示相对时长 */
  assignment?: ActiveAssignment | null;
  segments: TaskSegment[];
  /** 是否虚拟「待认领」泳道（段样式用虚线框） */
  virtual?: boolean;
}

export const LANE_LABEL_W = 176; // px，与 TeamTimeline 的列宽保持一致
export const LANE_H = 40; // px

export default function TeamTimelineLane({
  lane,
  view,
  nowMs,
  zebra,
  onSelectTask,
  onHover,
}: {
  lane: LaneData;
  view: TimeViewport;
  nowMs: number;
  zebra: boolean;
  onSelectTask: (taskId: string) => void;
  onHover: (seg: TaskSegment | null, e?: React.MouseEvent) => void;
}) {
  const span = Math.max(1, view.until - view.since);
  const pctOf = (ts: number) => ((ts - view.since) / span) * 100;

  return (
    <div
      className={`flex border-b border-g-border/70 ${zebra ? "bg-g-bg-soft/60" : "bg-transparent"}`}
      style={{ height: LANE_H }}
    >
      {/* 标签列（宽固定，横向永不滚动 → 无需 sticky） */}
      <div
        className="shrink-0 border-r border-g-border px-2.5 flex items-center gap-1.5 overflow-hidden"
        style={{ width: LANE_LABEL_W }}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full shrink-0 ${
            lane.healthError ? "bg-g-red" : "bg-g-green"
          }`}
          title={lane.healthError ? "该 Agent 有错误" : "正常"}
        />
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-medium text-g-fg truncate leading-3.5">
            {lane.name}
          </p>
          {lane.role && (
            <p className="text-[9px] text-g-fg-4 truncate leading-3">{lane.role}</p>
          )}
        </div>
        {lane.assignment && (
          <span
            className={`shrink-0 px-1 py-px rounded text-[9px] font-medium ${
              lane.assignment.kind === "busy"
                ? "bg-g-green-bg text-g-green"
                : "bg-g-yellow-bg text-amber-700"
            }`}
            title={lane.assignment.task_title}
          >
            {lane.assignment.kind === "busy" ? "干活" : "等待"}{" "}
            {formatDuration(Math.max(0, nowMs - lane.assignment.since))}
          </span>
        )}
      </div>

      {/* 时间区：段绝对定位 */}
      <div className="relative flex-1 overflow-hidden">
        {lane.segments.map((seg) => {
          const end = seg.ended_at ?? nowMs;
          if (end < view.since || seg.started_at > view.until) return null;
          const left = Math.max(0, pctOf(seg.started_at));
          const right = Math.min(100, pctOf(end));
          const width = right - left;
          if (width <= 0) return null;

          const st = statusStyle(seg.status);
          const unclaimed = lane.virtual || seg.status === "created";

          return (
            <button
              key={`${seg.task_id}-${seg.started_at}`}
              data-interactive
              onClick={() => onSelectTask(seg.task_id)}
              onMouseEnter={(e) => onHover(seg, e)}
              onMouseMove={(e) => onHover(seg, e)}
              onMouseLeave={() => onHover(null)}
              className={`absolute top-1/2 -translate-y-1/2 h-[22px] rounded overflow-hidden text-left animate-fade-in transition-shadow hover:shadow-gm-md hover:z-10 ${
                unclaimed ? UNCLAIMED_SEGMENT_CLASSES : `${st.bar}`
              }`}
              style={{ left: `${left}%`, width: `${width}%`, minWidth: 3 }}
              title={seg.title}
            >
              {st.striped && (
                <span className="absolute inset-0" style={STRIPED_OVERLAY} />
              )}
              {width > 6 && (
                <span
                  className={`relative block px-1 text-[9px] leading-[22px] truncate ${
                    unclaimed ? "text-g-fg-3" : st.barText
                  }`}
                >
                  {seg.title}
                </span>
              )}
              {seg.ongoing && (
                <span className="absolute right-0.5 top-1/2 -translate-y-1/2 w-1.5 h-1.5 rounded-full bg-white/90 animate-pulse-soft" />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
