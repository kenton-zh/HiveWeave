/**
 * delivery — 任务交付与产物摘要的纯逻辑（FE-15，方案 §9.5/§9.4，验收 T-24）。
 *
 * 数据来源 = 任务既有字段 + 端点 1 事件流的 detail payload，不新增 API：
 *  - 「Agent 说完成」= task.submitted 事件（执行者声明，**不是**验证）；
 *  - 「产物已验证」= task.approved / task.verifying / task.merged / task.closed
 *    事件（平台评审/验证记录），两者必须分开呈现（§9.4）；
 *  - 产物文件清单 = task.merged payload 的 files / files_total /
 *    merge_commit / target_branch。**文件路径不是下载链接** —— 上层组件
 *    只允许「展示 + 复制」，本模块不产生任何可点击跳转语义。
 *
 * 事件流按 ts 升序（后端排序），但扫描统一取「最新一条」，不依赖顺序。
 */

import type { TaskTimelineResponse, TimelineEvent } from "./types";

/** 平台验证/评审阶段（与「执行者声明」相对，§9.4 阶段表）。 */
export interface DeliveryVerification {
  kind: "approved" | "verifying" | "merged" | "closed";
  /** 中文标签（§9.4 用户语言：验证通过 / 已合并…） */
  label: string;
  ts: number;
  /** 当事 agent 名（agents 映射缺失时退回短 id） */
  actor: string | null;
}

/** 执行者「说完成」声明（task.submitted），与验证记录严格分列。 */
export interface DeliveryClaim {
  /** 最新一次提交时刻 */
  ts: number;
  actor: string | null;
  /** 提交次数（rework 循环会多次提交） */
  count: number;
  /** 最新提交之后是否被打回（review_rework）——声明已失效需重交 */
  reworkAfter: boolean;
}

/** 产物清单（全部来自 task.merged payload；无合并记录即为空）。 */
export interface DeliveryArtifacts {
  /** 产物路径（仅展示+复制，绝不是链接） */
  files: string[];
  /** 后端实际合并文件总数（payload 只带前 20 条） */
  filesTotal: number | null;
  mergeCommit: string | null;
  targetBranch: string | null;
}

export interface DeliverySummary {
  claim: DeliveryClaim | null;
  /** 按 ts 升序的平台验证/评审记录（每类取最新） */
  verifications: DeliveryVerification[];
  artifacts: DeliveryArtifacts;
  /** 已知限制（blocked_reason / 截断等，诚实呈现不推导） */
  limitations: string[];
  /** 是否存在任何交付记录（决定空态文案） */
  hasAny: boolean;
}

function actorOf(
  ev: TimelineEvent,
  agents: TaskTimelineResponse["agents"],
): string | null {
  if (!ev.agent_id) return null;
  return agents[ev.agent_id]?.name || `${ev.agent_id.slice(0, 8)}…`;
}

function payloadOf(ev: TimelineEvent): Record<string, unknown> {
  return ev.detail && typeof ev.detail === "object" && !Array.isArray(ev.detail)
    ? ev.detail
    : {};
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

function latestEvent(events: TimelineEvent[], type: string): TimelineEvent | null {
  let out: TimelineEvent | null = null;
  for (const ev of events) {
    if (ev.type === type && (!out || ev.ts > out.ts)) out = ev;
  }
  return out;
}

/** 从端点 1 响应提取交付摘要（纯函数，脏 payload 一律降级不抛）。 */
export function extractDelivery(data: TaskTimelineResponse): DeliverySummary {
  const events = data.events ?? [];
  const agents = data.agents ?? {};

  // ── 执行者声明（Agent 说完成）────────────────────────────
  let claim: DeliveryClaim | null = null;
  let submitCount = 0;
  let lastSubmit: TimelineEvent | null = null;
  for (const ev of events) {
    if (ev.type !== "task.submitted") continue;
    submitCount++;
    if (!lastSubmit || ev.ts > lastSubmit.ts) lastSubmit = ev;
  }
  if (lastSubmit) {
    const reworkAfter = events.some(
      (ev) =>
        ev.ts > lastSubmit!.ts &&
        (ev.type === "task.rework" ||
          (ev.type === "task.running" && ev.reason_code === "review_rework")),
    );
    claim = {
      ts: lastSubmit.ts,
      actor: actorOf(lastSubmit, agents),
      count: submitCount,
      reworkAfter,
    };
  }

  // ── 平台验证/评审记录（产物已验证侧）─────────────────────
  const VERIF: Array<{ type: string; kind: DeliveryVerification["kind"]; label: string }> = [
    { type: "task.approved", kind: "approved", label: "评审通过" },
    { type: "task.verifying", kind: "verifying", label: "开始验证" },
    { type: "task.merged", kind: "merged", label: "已合并" },
    { type: "task.closed", kind: "closed", label: "已关闭" },
  ];
  const verifications: DeliveryVerification[] = [];
  for (const { type, kind, label } of VERIF) {
    const ev = latestEvent(events, type);
    if (ev) verifications.push({ kind, label, ts: ev.ts, actor: actorOf(ev, agents) });
  }
  verifications.sort((a, b) => a.ts - b.ts);

  // ── 产物清单（task.merged payload）───────────────────────
  const merged = latestEvent(events, "task.merged");
  const payload = merged ? payloadOf(merged) : {};
  const rawFiles = Array.isArray(payload.files) ? payload.files : [];
  const files = rawFiles.filter((f): f is string => typeof f === "string" && !!f.trim());
  const artifacts: DeliveryArtifacts = {
    files,
    filesTotal:
      typeof payload.files_total === "number" && Number.isFinite(payload.files_total)
        ? payload.files_total
        : null,
    mergeCommit: merged ? str(payload.merge_commit) : null,
    targetBranch: merged ? str(payload.target_branch) : null,
  };

  // ── 已知限制（只呈现既有事实，不凭空推导）────────────────
  const limitations: string[] = [];
  const blocked = str(data.task?.blocked_reason);
  if (blocked) limitations.push(`阻塞：${blocked}`);
  if (data.truncated) limitations.push("事件流超出预算被截断，最早的记录可能缺失");
  if (artifacts.filesTotal != null && artifacts.filesTotal > files.length) {
    limitations.push(`合并文件共 ${artifacts.filesTotal} 个，仅记录前 ${files.length} 个`);
  }

  return {
    claim,
    verifications,
    artifacts,
    limitations,
    hasAny: !!claim || verifications.length > 0 || files.length > 0,
  };
}

/** 复制文本到剪贴板（clipboard API + execCommand 兜底）。 */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }
}
