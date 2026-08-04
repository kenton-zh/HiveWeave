/**
 * MiniMap — 底部 40px 密度总览条 + 可拖视口框（Timeline v4 §5.2/§5.3）。
 * 粗粒度 div/CSS 实现：80 个密度桶 + 一个可拖拽的视口框，
 * 不追求丝滑拖拽（预算优先）。
 */

import { useCallback, useRef } from "react";
import type { TaskSegment } from "./types";
import type { TimeViewport } from "./usePanZoom";

const BUCKETS = 80;

export default function MiniMap({
  segments,
  view,
  onJump,
  nowMs,
}: {
  segments: TaskSegment[];
  view: TimeViewport;
  onJump: (v: TimeViewport) => void;
  nowMs: number;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ grabOffset: number } | null>(null);

  // 总览范围 = 视口 ∪ 全部段 ∪ 当前时刻
  let lo = Math.min(view.since, nowMs);
  let hi = Math.max(view.until, nowMs);
  for (const s of segments) {
    if (s.started_at < lo) lo = s.started_at;
    const end = s.ended_at ?? nowMs;
    if (end > hi) hi = end;
  }
  const range = Math.max(1, hi - lo);
  const pctOf = (ts: number) => ((ts - lo) / range) * 100;

  // 密度桶
  const counts = new Array(BUCKETS).fill(0);
  for (const s of segments) {
    const end = s.ended_at ?? nowMs;
    const b0 = Math.max(0, Math.floor(((s.started_at - lo) / range) * BUCKETS));
    const b1 = Math.min(BUCKETS - 1, Math.floor(((end - lo) / range) * BUCKETS));
    for (let b = b0; b <= b1; b++) counts[b]++;
  }
  const maxCount = Math.max(1, ...counts);

  const span = view.until - view.since;

  const jumpToClientX = useCallback(
    (clientX: number, asDrag: boolean) => {
      const el = trackRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const ts = lo + ((clientX - rect.left) / Math.max(1, rect.width)) * range;
      const center = asDrag ? ts + dragRef.current!.grabOffset : ts;
      onJump({ since: center - span / 2, until: center + span / 2 });
    },
    [lo, range, span, onJump],
  );

  return (
    <div className="h-10 shrink-0 border-t border-g-border bg-g-bg px-3 py-1.5">
      <div
        ref={trackRef}
        className="relative h-full rounded bg-g-bg-muted overflow-hidden cursor-pointer"
        onPointerDown={(e) => {
          if (!(e.target as HTMLElement).closest("[data-vpframe]")) {
            jumpToClientX(e.clientX, false);
          }
        }}
      >
        {/* 密度条 */}
        <div className="absolute inset-0 flex">
          {counts.map((c, i) => (
            <span
              key={i}
              className="flex-1 bg-g-blue"
              style={{ opacity: c === 0 ? 0 : 0.12 + 0.55 * (c / maxCount) }}
            />
          ))}
        </div>
        {/* 当前时刻 */}
        <span
          className="absolute top-0 bottom-0 w-px bg-g-red/70"
          style={{ left: `${pctOf(nowMs)}%` }}
        />
        {/* 视口框（可拖） */}
        <div
          data-vpframe
          data-interactive
          className="absolute top-0 bottom-0 border border-g-blue/70 bg-g-blue/10 rounded-sm cursor-grab active:cursor-grabbing"
          style={{
            left: `${Math.max(0, pctOf(view.since))}%`,
            width: `${Math.min(100, (span / range) * 100)}%`,
            minWidth: 8,
          }}
          onPointerDown={(e) => {
            e.stopPropagation();
            const el = trackRef.current;
            if (!el) return;
            const rect = el.getBoundingClientRect();
            const tsAtCursor =
              lo + ((e.clientX - rect.left) / Math.max(1, rect.width)) * range;
            dragRef.current = { grabOffset: view.since + span / 2 - tsAtCursor };
            (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (dragRef.current) jumpToClientX(e.clientX, true);
          }}
          onPointerUp={() => {
            dragRef.current = null;
          }}
          onPointerCancel={() => {
            dragRef.current = null;
          }}
        />
      </div>
    </div>
  );
}
