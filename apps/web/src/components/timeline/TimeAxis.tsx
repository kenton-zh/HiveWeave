/**
 * TimeAxis — 顶部时间刻度 + 游戏日标签（Timeline v4 §5.2）。
 *
 * 双行：上行游戏日「Day N」（锚点换算，无锚点隐藏），下行现实时刻
 * HH:MM。刻度步长随视口跨度自适应，目标 6-12 个主刻度。
 */

import type { GameTimeAnchor } from "./utils";
import { gameSecondsAt } from "./utils";

function pickStepMs(spanMs: number): number {
  const steps = [
    5 * 60e3, 15 * 60e3, 30 * 60e3,
    3600e3, 2 * 3600e3, 6 * 3600e3, 12 * 3600e3,
    24 * 3600e3, 2 * 24 * 3600e3, 7 * 24 * 3600e3,
  ];
  for (const s of steps) {
    if (spanMs / s <= 12) return s;
  }
  return steps[steps.length - 1];
}

function tickLabel(ts: number, stepMs: number): string {
  const d = new Date(ts);
  if (stepMs >= 24 * 3600e3) {
    return `${d.getMonth() + 1}/${d.getDate()}`;
  }
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** 窗口内的游戏日起止边界（现实毫秒），供泳道背景画 Day 分界带。 */
export function dayBoundaries(
  since: number,
  until: number,
  anchor: GameTimeAnchor | null,
): Array<{ ts: number; day: number }> {
  if (!anchor) return [];
  const gsSince = gameSecondsAt(since, anchor);
  const gsUntil = gameSecondsAt(until, anchor);
  const rate = 86_400 / Math.max(1, anchor.realSecondsPerGameDay); // 游戏秒/现实秒
  const out: Array<{ ts: number; day: number }> = [];
  const firstDay = Math.floor(gsSince / 86_400);
  const lastDay = Math.floor(gsUntil / 86_400);
  for (let day = firstDay; day <= lastDay && out.length < 200; day++) {
    const boundaryGs = day * 86_400;
    const realMs = anchor.atRealMs + ((boundaryGs - anchor.gameSeconds) / rate) * 1000;
    if (realMs >= since && realMs <= until) out.push({ ts: realMs, day });
  }
  return out;
}

export default function TimeAxis({
  since,
  until,
  anchor,
}: {
  since: number;
  until: number;
  anchor: GameTimeAnchor | null;
}) {
  const span = Math.max(1, until - since);
  const step = pickStepMs(span);
  const first = Math.ceil(since / step) * step;
  const ticks: number[] = [];
  for (let t = first; t <= until && ticks.length < 60; t += step) ticks.push(t);

  const days = dayBoundaries(since, until, anchor);
  const pct = (ts: number) => `${((ts - since) / span) * 100}%`;

  return (
    <div className="relative h-10 w-full overflow-hidden">
      {/* 游戏日标签（上行） */}
      {anchor &&
        days.map(({ ts, day }) => (
          <span
            key={`day-${day}`}
            className="absolute top-0.5 -translate-x-1/2 text-[9px] font-medium text-g-fg-3 whitespace-nowrap"
            style={{ left: pct(ts) }}
          >
            Day {day}
          </span>
        ))}
      {/* 刻度（下行） */}
      {ticks.map((t) => (
        <span
          key={t}
          className="absolute bottom-0.5 -translate-x-1/2 text-[9px] text-g-fg-4 font-mono whitespace-nowrap"
          style={{ left: pct(t) }}
        >
          <span className="block mx-auto mb-px w-px h-1.5 bg-g-border-strong" />
          {tickLabel(t, step)}
        </span>
      ))}
    </div>
  );
}
