/** Real-world seconds that equal one project (game) day. */
export const REAL_SECONDS_PER_GAME_DAY = 3600; // 1 hour

/** Seconds in one project day. */
export const GAME_SECONDS_PER_DAY = 86_400;

/** Game seconds advanced per one real second while the server is running. */
export const GAME_TIME_SCALE = GAME_SECONDS_PER_DAY / REAL_SECONDS_PER_GAME_DAY;

export function realMsToGameSeconds(deltaRealMs: number): number {
  return Math.floor((deltaRealMs / 1000) * GAME_TIME_SCALE);
}

export function gameSecondsToRealMs(deltaGameSeconds: number): number {
  return Math.floor((deltaGameSeconds / GAME_TIME_SCALE) * 1000);
}

export function decomposeGameSeconds(gameSeconds: number): {
  day: number;
  hours: number;
  minutes: number;
  seconds: number;
} {
  const total = Math.max(0, Math.floor(gameSeconds));
  const day = Math.floor(total / GAME_SECONDS_PER_DAY);
  const remainder = total % GAME_SECONDS_PER_DAY;
  const hours = Math.floor(remainder / 3600);
  const minutes = Math.floor((remainder % 3600) / 60);
  const seconds = remainder % 60;
  return { day, hours, minutes, seconds };
}
