import { describe, it, expect, vi } from "vitest";
import { importWithRetry, resolveLeftPanel } from "./mainPanel";

describe("resolveLeftPanel", () => {
  it("keeps Token on its own panel even without a project", () => {
    expect(resolveLeftPanel("token", "proj-1")).toBe("token");
    expect(resolveLeftPanel("token", null)).toBe("token-empty");
    expect(resolveLeftPanel("token", undefined)).toBe("token-empty");
  });

  it("does not send Token to Office", () => {
    expect(resolveLeftPanel("office", "proj-1")).toBe("office");
    expect(resolveLeftPanel("tree", "proj-1")).toBe("tree");
    expect(resolveLeftPanel("timeline", "proj-1")).toBe("timeline");
  });
});

describe("importWithRetry", () => {
  it("retries once after a rejected chunk load", async () => {
    const load = vi
      .fn()
      .mockRejectedValueOnce(new Error("Failed to fetch dynamically imported module"))
      .mockResolvedValueOnce({ default: () => null });
    vi.useFakeTimers();
    const pending = importWithRetry(load);
    await vi.advanceTimersByTimeAsync(200);
    await expect(pending).resolves.toEqual({ default: expect.any(Function) });
    expect(load).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });
});
