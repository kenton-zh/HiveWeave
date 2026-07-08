import { useState, useEffect } from "react";
import { getProjectGameTime } from "../api";

interface Props {
  projectId: string | null;
}

type GameTimeResponse = {
  formatted: string;
  realStartedAt?: number;             // unix seconds — BUG-005 fix
  realSecondsPerGameDay?: number;     // 1 real hour = 1 game day (3600s) — BUG-005 fix
};

const GAME_SECONDS_PER_DAY = 86400;
const DEFAULT_REAL_SECONDS_PER_GAME_DAY = 3600;

function formatGameSeconds(gs: number): string {
  const day = Math.floor(gs / GAME_SECONDS_PER_DAY);
  const rem = Math.floor(gs - day * GAME_SECONDS_PER_DAY);
  const h = Math.floor(rem / 3600);
  const m = Math.floor((rem % 3600) / 60);
  return `Day ${day} ${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

export default function ProjectTimeBadge({ projectId }: Props) {
  const [formatted, setFormatted] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) {
      setFormatted(null);
      return;
    }

    let cancelled = false;
    // 本地计算的"起点"：服务器返回的 (realStartedAt, gameSeconds) 对。
    // 之后每秒在本地做 (now - realStartedAt) * 24 + gameSeconds，
    // 完全消除每秒 HTTP poll（BUG-005 修复）。
    let realStartedAt: number | null = null;
    let baseGameSeconds = 0;
    let rate = DEFAULT_REAL_SECONDS_PER_GAME_DAY;

    const apply = (realNow: number) => {
      if (realStartedAt === null) return;
      const elapsedReal = Math.max(0, realNow - realStartedAt);
      // game_seconds = gameSeconds_at_snapshot + (elapsedReal * 24x)
      const gs = baseGameSeconds + (elapsedReal * GAME_SECONDS_PER_DAY) / rate;
      if (!cancelled) setFormatted(formatGameSeconds(gs));
    };

    const poll = async () => {
      try {
        const data = (await getProjectGameTime(projectId)) as GameTimeResponse;
        if (cancelled) return;
        if (typeof data.realStartedAt === "number") {
          realStartedAt = data.realStartedAt;
          rate = data.realSecondsPerGameDay ?? DEFAULT_REAL_SECONDS_PER_GAME_DAY;
          // baseGameSeconds = server's current gameSeconds at the moment it took the snapshot.
          // Apply local offset so initial display matches the server snapshot exactly.
          const serverNow = Math.floor(Date.now() / 1000);
          baseGameSeconds = (data as any).gameSeconds ?? 0;
          // Re-anchor: shift realStartedAt so the formula yields baseGameSeconds at serverNow.
          realStartedAt = serverNow;
        }
        if (data.formatted) {
          setFormatted(data.formatted);
        }
        // Tick once now (so the first second shows the right value)
        apply(Math.floor(Date.now() / 1000));
      } catch (e) {
        console.warn("[ProjectTime] Poll failed:", e);
      }
    };

    void poll();
    // 30s resync — keeps drift bounded if the server time changes (e.g. pause/resume)
    const resyncId = window.setInterval(poll, 30_000);
    // 1s local tick — pure JS, no HTTP
    const tickId = window.setInterval(() => apply(Math.floor(Date.now() / 1000)), 1000);

    return () => {
      cancelled = true;
      window.clearInterval(resyncId);
      window.clearInterval(tickId);
    };
  }, [projectId]);

  if (!projectId) {
    return <div className="px-2.5 py-1 rounded-md bg-g-bg border border-g-border text-xs text-g-fg-3 whitespace-nowrap shrink-0">No project</div>;
  }

  return (
    <div className="px-2.5 py-1 rounded-md bg-g-bg border border-g-border text-xs text-g-fg-3 whitespace-nowrap shrink-0">
      项目时间 {formatted ?? "—"}
    </div>
  );
}
