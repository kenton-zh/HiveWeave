/** Prompt-side token stats (DSH billedInputTokens / cacheHitPercent). */

export function billedPromptTokens(
  input: number,
  cacheRead: number = 0,
  cacheWrite: number = 0,
): number {
  return (input || 0) + (cacheRead || 0) + (cacheWrite || 0);
}

/** Rounded cache-hit share of prompt-side input; null when nothing was billed. */
export function cacheHitPercent(
  input: number,
  cacheRead: number = 0,
  cacheWrite: number = 0,
): number | null {
  const denom = billedPromptTokens(input, cacheRead, cacheWrite);
  if (denom <= 0) return null;
  return Math.round(((cacheRead || 0) / denom) * 100);
}

export function formatHitPercent(pct: number | null | undefined): string {
  return pct == null ? "—" : `${pct}%`;
}
