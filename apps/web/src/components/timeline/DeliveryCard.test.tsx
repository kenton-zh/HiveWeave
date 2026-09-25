/**
 * DeliveryCard 渲染契约（FE-15，验收 T-24）。
 *
 * 安全红线锁死：
 *  1. 产物路径只展示 + 复制 —— 卡片内 0 个 <a>、无「下载」字样按钮
 *     （路径渲染成下载链接/假按钮 = 回归失败）；
 *  2. 「Agent 说完成」与「已验证」分离呈现；
 *  3. 无产物时只有说明，无任何复制/下载按钮（不做无效主按钮）；
 *  4. 明示平台无下载/预览能力（T-24：不给无效下载承诺）。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeliveryCard from "./DeliveryCard";
import type { TaskTimelineResponse, TimelineEvent } from "./types";

function ev(partial: Partial<TimelineEvent> & { id: string; type: string; ts: number }): TimelineEvent {
  return {
    task_id: "t-1",
    agent_id: "agent-1",
    from_status: null,
    to_status: null,
    reason_code: null,
    from_agent_id: null,
    to_agent_id: null,
    title: partial.type,
    detail: null,
    ...partial,
  };
}

function base(overrides: Partial<TaskTimelineResponse> = {}): TaskTimelineResponse {
  return {
    task: { id: "t-1", title: "任务", status: "closed", blocked_reason: null },
    agents: { "agent-1": { name: "小明", role: "executor" } },
    events: [],
    max_event_ts: 0,
    truncated: false,
    ...overrides,
  };
}

beforeEach(() => {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    configurable: true,
  });
});

describe("DeliveryCard —— 路径不是下载链接（T-24 红线）", () => {
  it("合并产物路径渲染为文本 + 复制按钮；卡片内零 <a>、零「下载」按钮", () => {
    const { container } = render(
      <DeliveryCard
        data={base({
          events: [
            ev({ id: "s1", type: "task.submitted", ts: 1000 }),
            ev({
              id: "m1",
              type: "task.merged",
              ts: 9000,
              detail: {
                merge_commit: "abc123def4567890",
                files: ["apps/web/src/a.tsx", "apps/web/src/b.ts"],
                files_total: 2,
                target_branch: "main",
              },
            }),
          ],
        })}
      />,
    );
    // 路径可见
    expect(screen.getByText("apps/web/src/a.tsx")).toBeInTheDocument();
    // 复制动作可用且复制的是路径本体
    fireEvent.click(screen.getByTestId("delivery-copy-path"));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("apps/web/src/a.tsx");
    // 红线：无任何锚点；无任何「下载」承诺
    expect(container.querySelectorAll("a")).toHaveLength(0);
    expect(screen.queryByText("下载")).not.toBeInTheDocument();
    // 复制按钮不携带 href/跳转语义（button 元素本身）
    const copyBtn = screen.getByTestId("delivery-copy-path");
    expect(copyBtn.tagName).toBe("BUTTON");
    expect(copyBtn.getAttribute("href")).toBeNull();
    // 能力边界明示
    expect(screen.getByTestId("delivery-capability-note")).toHaveTextContent(
      "暂未提供产物下载与预览",
    );
  });

  it("提交但无验证记录：声明与验证分列 —— 已提交 ≠ 已验证", () => {
    render(
      <DeliveryCard
        data={base({
          events: [ev({ id: "s1", type: "task.submitted", ts: 1000 })],
        })}
      />,
    );
    expect(screen.getByTestId("delivery-claim")).toHaveTextContent("执行者声明完成");
    expect(screen.getByTestId("delivery-unverified")).toHaveTextContent("已提交 ≠ 已验证");
  });

  it("有验证记录时两列并存，与声明可区分", () => {
    render(
      <DeliveryCard
        data={base({
          events: [
            ev({ id: "s1", type: "task.submitted", ts: 1000 }),
            ev({ id: "a1", type: "task.approved", ts: 2000 }),
          ],
        })}
      />,
    );
    expect(screen.getByTestId("delivery-claim")).toHaveTextContent("执行者声明完成");
    expect(screen.getByText("评审通过")).toBeInTheDocument();
    expect(screen.queryByTestId("delivery-unverified")).not.toBeInTheDocument();
  });

  it("打回后声明标注「其后已被打回」", () => {
    render(
      <DeliveryCard
        data={base({
          events: [
            ev({ id: "s1", type: "task.submitted", ts: 1000 }),
            ev({ id: "r1", type: "task.running", ts: 2000, reason_code: "review_rework" }),
          ],
        })}
      />,
    );
    expect(screen.getByTestId("delivery-claim")).toHaveTextContent("其后已被打回");
  });

  it("无产物记录：只有说明，无复制按钮（不做无效操作）", () => {
    render(
      <DeliveryCard
        data={base({ events: [ev({ id: "s1", type: "task.submitted", ts: 1000 })] })}
      />,
    );
    expect(screen.getByTestId("delivery-no-artifacts")).toHaveTextContent("暂无产物记录");
    expect(screen.queryByTestId("delivery-copy-path")).not.toBeInTheDocument();
    expect(screen.queryByTestId("delivery-copy-commit")).not.toBeInTheDocument();
  });

  it("已知限制与用户验收说明呈现；复制失败可感知不静默", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockRejectedValue(new Error("deny")) },
      configurable: true,
    });
    render(
      <DeliveryCard
        data={base({
          task: { id: "t-1", title: "任务", status: "blocked", blocked_reason: "等依赖" },
          events: [
            ev({
              id: "m1",
              type: "task.merged",
              ts: 9000,
              detail: { files: ["a.py"], files_total: 1, merge_commit: "abc" },
            }),
          ],
        })}
      />,
    );
    expect(screen.getByTestId("delivery-limitations")).toHaveTextContent("等依赖");
    expect(screen.getByTestId("delivery-acceptance-note")).toHaveTextContent("用户验收");
    // execCommand 兜底在 jsdom 同样失败 → 按钮显示复制失败
    fireEvent.click(screen.getByTestId("delivery-copy-path"));
    const btn = await screen.findByText("复制失败");
    expect(btn).toBeInTheDocument();
  });
});
