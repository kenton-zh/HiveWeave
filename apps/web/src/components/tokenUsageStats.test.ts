import { describe, expect, it } from "vitest";
import {
  billedPromptTokens,
  cacheHitPercent,
  formatHitPercent,
} from "./tokenUsageStats";

describe("tokenUsageStats", () => {
  it("matches DSH cacheHitPercent: cache / (uncached + cacheRead + cacheWrite)", () => {
    expect(billedPromptTokens(10, 90, 0)).toBe(100);
    expect(cacheHitPercent(10, 90, 0)).toBe(90);
    expect(cacheHitPercent(100, 2000, 0)).toBe(95);
    expect(cacheHitPercent(0, 90, 0)).toBe(100);
    expect(cacheHitPercent(0, 0, 0)).toBeNull();
    expect(formatHitPercent(90)).toBe("90%");
    expect(formatHitPercent(null)).toBe("—");
  });

  it("counts Anthropic cache writes in the denominator", () => {
    expect(billedPromptTokens(100, 2000, 50)).toBe(2150);
    expect(cacheHitPercent(100, 2000, 50)).toBe(93);
  });
});
