/** Real-world seconds that equal one project (game) day. */
export const REAL_SECONDS_PER_GAME_DAY = 3600; // 1 hour

/** Seconds in one project day. */
export const GAME_SECONDS_PER_DAY = 86_400;

/** Game seconds advanced per one real second while the server is running. */
export const GAME_TIME_SCALE = GAME_SECONDS_PER_DAY / REAL_SECONDS_PER_GAME_DAY;

export interface GameTimeSnapshot {
  /** Total project seconds elapsed since project day 0. */
  gameSeconds: number;
  day: number;
  hours: number;
  minutes: number;
  seconds: number;
  /** Human-readable project time, e.g. "第 3 天 04:12:30" */
  formatted: string;
  /** ISO real-world timestamp */
  realTimestamp: number;
  realFormatted: string;
}

export function realMsToGameSeconds(deltaRealMs: number): number {
  return Math.floor((deltaRealMs / 1000) * GAME_TIME_SCALE);
}

export function gameSecondsToRealMs(deltaGameSeconds: number): number {
  return Math.floor((deltaGameSeconds / GAME_TIME_SCALE) * 1000);
}

export function decomposeGameSeconds(gameSeconds: number): Pick<GameTimeSnapshot, "day" | "hours" | "minutes" | "seconds"> {
  const total = Math.max(0, Math.floor(gameSeconds));
  const day = Math.floor(total / GAME_SECONDS_PER_DAY);
  const remainder = total % GAME_SECONDS_PER_DAY;
  const hours = Math.floor(remainder / 3600);
  const minutes = Math.floor((remainder % 3600) / 60);
  const seconds = remainder % 60;
  return { day, hours, minutes, seconds };
}

export function formatGameTime(gameSeconds: number): string {
  const { day, hours, minutes, seconds } = decomposeGameSeconds(gameSeconds);
  const hh = String(hours).padStart(2, "0");
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return `第 ${day} 天 ${hh}:${mm}:${ss}`;
}

export function formatRealTime(timestampMs: number = Date.now()): string {
  const d = new Date(timestampMs);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function buildGameTimeSnapshot(gameSeconds: number, realTimestamp: number = Date.now()): GameTimeSnapshot {
  const parts = decomposeGameSeconds(gameSeconds);
  return {
    gameSeconds: Math.max(0, Math.floor(gameSeconds)),
    ...parts,
    formatted: formatGameTime(gameSeconds),
    realTimestamp,
    realFormatted: formatRealTime(realTimestamp),
  };
}

export function parseGameTimeOffset(input: {
  dueInGameDays?: number | string;
  dueInGameHours?: number | string;
  dueInGameMinutes?: number | string;
  dueInGameSeconds?: number | string;
}): number {
  const days = Number(input.dueInGameDays || 0);
  const hours = Number(input.dueInGameHours || 0);
  const minutes = Number(input.dueInGameMinutes || 0);
  const seconds = Number(input.dueInGameSeconds || 0);
  return days * GAME_SECONDS_PER_DAY + hours * 3600 + minutes * 60 + seconds;
}
