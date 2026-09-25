/**
 * extractDelivery 纯逻辑（FE-15，方案 §9.5/§9.4）。
 * 锁死：说完成（task.submitted）与验证（approved/verifying/merged/closed）
 * 分列；产物只来自 task.merged payload；脏 payload 降级不抛。
 */
import { describe, expect, it } from "vitest";
import { extractDelivery } from "./delivery";
import type { TaskTimelineResponse, TimelineEvent } from "./types";

const AGENTS = { "agent-1": { name: "小明", role: "executor" } };

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
    task: {
      id: "t-1",
      title: "任务",
      status: "closed",
      blocked_reason: null,
    },
    agents: AGENTS,
    events: [],
    max_event_ts: 0,
    truncated: false,
    ...overrides,
  };
}

describe("extractDelivery —— 说完成 vs 已验证（§9.4）", () => {
  it("task.submitted → claim（含次数与当事人），与验证记录分列", () => {
    const d = extractDelivery(
      base({
        events: [
          ev({ id: "e1", type: "task.submitted", ts: 1000 }),
          ev({ id: "e2", type: "task.submitted", ts: 5000 }),
        ],
      }),
    );
    expect(d.claim).toEqual({
      ts: 5000,
      actor: "小明",
      count: 2,
      reworkAfter: false,
    });
    expect(d.verifications).toEqual([]);
  });

  it("rework 在最新提交之后 → reworkAfter=true（声明已失效）", () => {
    const d = extractDelivery(
      base({
        events: [
          ev({ id: "e1", type: "task.submitted", ts: 1000 }),
          ev({ id: "e2", type: "task.running", ts: 2000, reason_code: "review_rework" }),
        ],
      }),
    );
    expect(d.claim?.reworkAfter).toBe(true);
  });

  it("rework 在提交之前 → 不影响最新声明", () => {
    const d = extractDelivery(
      base({
        events: [
          ev({ id: "e1", type: "task.running", ts: 500, reason_code: "review_rework" }),
          ev({ id: "e2", type: "task.submitted", ts: 1000 }),
        ],
      }),
    );
    expect(d.claim?.reworkAfter).toBe(false);
  });

  it("approved/verifying/merged/closed 各取最新一条，按 ts 升序，当事人可读", () => {
    const d = extractDelivery(
      base({
        events: [
          ev({ id: "e1", type: "task.approved", ts: 3000 }),
          ev({ id: "e2", type: "task.approved", ts: 8000 }),
          ev({ id: "e3", type: "task.merged", ts: 9000 }),
          ev({ id: "e4", type: "task.verifying", ts: 8500 }),
          ev({ id: "e5", type: "task.closed", ts: 9999 }),
        ],
      }),
    );
    expect(d.verifications.map((v) => [v.kind, v.ts])).toEqual([
      ["approved", 8000],
      ["verifying", 8500],
      ["merged", 9000],
      ["closed", 9999],
    ]);
    expect(d.verifications[0].actor).toBe("小明");
  });

  it("无任何记录 → hasAny=false，claim=null", () => {
    const d = extractDelivery(base());
    expect(d.claim).toBeNull();
    expect(d.verifications).toEqual([]);
    expect(d.hasAny).toBe(false);
  });
});

describe("extractDelivery —— 产物（仅 task.merged payload）", () => {
  it("提取 files/files_total/merge_commit/target_branch", () => {
    const d = extractDelivery(
      base({
        events: [
          ev({
            id: "m1",
            type: "task.merged",
            ts: 9000,
            detail: {
              merge_commit: "abc123def456",
              files: ["src/a.py", "src/b.ts"],
              files_total: 5,
              target_branch: "main",
            },
          }),
        ],
      }),
    );
    expect(d.artifacts.files).toEqual(["src/a.py", "src/b.ts"]);
    expect(d.artifacts.filesTotal).toBe(5);
    expect(d.artifacts.mergeCommit).toBe("abc123def456");
    expect(d.artifacts.targetBranch).toBe("main");
    expect(d.hasAny).toBe(true);
  });

  it("脏 payload 降级：非字符串路径过滤、files_total 非数字为 null", () => {
    const d = extractDelivery(
      base({
        events: [
          ev({
            id: "m1",
            type: "task.merged",
            ts: 9000,
            detail: { files: ["ok.py", 42, null, "", "  "], files_total: "many" },
          }),
        ],
      }),
    );
    expect(d.artifacts.files).toEqual(["ok.py"]);
    expect(d.artifacts.filesTotal).toBeNull();
    expect(d.limitations).toEqual([]); // total 未知 → 不虚构限制
  });

  it("files_total > 记录数 → 已知限制如实标注", () => {
    const d = extractDelivery(
      base({
        events: [
          ev({
            id: "m1",
            type: "task.merged",
            ts: 9000,
            detail: { files: ["a.py"], files_total: 25 },
          }),
        ],
      }),
    );
    expect(d.limitations.join("\n")).toContain("25");
  });

  it("blocked_reason 与 truncated 进入已知限制", () => {
    const d = extractDelivery(
      base({
        task: { id: "t-1", title: "任务", status: "blocked", blocked_reason: "等依赖" },
        events: [ev({ id: "e1", type: "task.submitted", ts: 1000 })],
        truncated: true,
      }),
    );
    expect(d.limitations.join("\n")).toContain("等依赖");
    expect(d.limitations.join("\n")).toContain("截断");
  });

  it("merged 之外的事件不产出产物（不虚构路径）", () => {
    const d = extractDelivery(
      base({
        events: [
          ev({ id: "e1", type: "task.closed", ts: 9000, detail: { files: ["假路径.py"] } }),
        ],
      }),
    );
    expect(d.artifacts.files).toEqual([]);
    expect(d.artifacts.mergeCommit).toBeNull();
  });
});
