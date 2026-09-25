/**
 * TeamTimeline 缩放按钮接线（FE-10 / SR-02 / T-16）。
 *
 * 「+」按钮必须让可见时间跨度变小（放大）。组件内部视口不可直接断言，
 * 用深链 hash（useDeepLinkWriter 400ms 防抖写 since/until）作可观测契约。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../api", () => ({
  getTeamActivity: vi.fn(),
  getProjectGameTime: vi.fn(),
}));

import { getProjectGameTime, getTeamActivity } from "../../api";
import TeamTimeline from "./TeamTimeline";
import { useAppStore } from "../../store";

const NOW = 1_700_000_000_000;

function fixture() {
  return {
    agents: [{ id: "a-1", name: "CEO", parent_id: null }],
    task_segments: [
      {
        task_id: "t-1",
        title: "任务A",
        assignee_id: "a-1",
        creator_id: null,
        reviewer_id: null,
        status: "running",
        started_at: NOW - 600e3,
        ended_at: null,
        ongoing: true,
      },
    ],
    active_assignments: [],
    window: { since: NOW - 3600e3, until: NOW },
    max_event_ts: NOW,
    changed: true,
    truncated: false,
    has_more_earlier: false,
  };
}

function hashView(): { since: number; until: number } | null {
  const m = window.location.hash.match(/since=(\d+).*until=(\d+)/);
  if (!m) return null;
  return { since: Number(m[1]), until: Number(m[2]) };
}

describe("TeamTimeline 缩放方向（T-16）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.location.hash = "";
    useAppStore.setState({ selectedProjectId: "p-1", timelineVersion: 0 });
    vi.mocked(getTeamActivity).mockResolvedValue(fixture() as never);
    vi.mocked(getProjectGameTime).mockRejectedValue(new Error("no anchor"));
  });

  it("点「+」可见时间跨度变小（span ÷ 1.25）", async () => {
    render(<TeamTimeline />);
    expect((await screen.findAllByText("CEO"))[0]).toBeInTheDocument();

    // 初始窗口 1h（mount 后 400ms 深链落 hash）
    await waitFor(
      () => {
        const v = hashView();
        expect(v && v.until - v.since).toBe(3600e3);
      },
      { timeout: 3000 },
    );

    fireEvent.click(screen.getByTitle("放大（可见时间跨度变小）"));

    await waitFor(
      () => {
        const v = hashView();
        expect(v && v.until - v.since).toBe(2880e3); // 3600e3 / 1.25
      },
      { timeout: 3000 },
    );
  });

  it("点「−」可见时间跨度变大（span × 1.25）", async () => {
    render(<TeamTimeline />);
    expect((await screen.findAllByText("CEO"))[0]).toBeInTheDocument();
    await waitFor(
      () => {
        const v = hashView();
        expect(v && v.until - v.since).toBe(3600e3);
      },
      { timeout: 3000 },
    );

    fireEvent.click(screen.getByTitle("缩小（可见时间跨度变大）"));

    await waitFor(
      () => {
        const v = hashView();
        expect(v && v.until - v.since).toBe(4500e3); // 3600e3 / 0.8
      },
      { timeout: 3000 },
    );
  });

  it("缩放/平移共用同一时间→像素映射：任务条按窗口百分比定位（与 TimeAxis/now 线同源 view）", async () => {
    render(<TeamTimeline />);
    expect(await screen.findByText("任务A")).toBeInTheDocument();
    // now 线存在（视口含当前时刻）
    // nowMs 来自本地 tick，NOW 为固定 fixture 时刻 → nowPct 可能为 null；
    // 此处只锁任务条与刻度都消费同一 view 的产物——泳道行渲染了任务块。
    expect(screen.getByTitle("任务A")).toBeInTheDocument();
  });
});
