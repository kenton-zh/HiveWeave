/**
 * Agent 状态的统一推导（2026-10-01 状态准确性修复）。
 *
 * `resolveAgentStatus` 是**优先级单一真值源**：组织图圆点（deriveAgentDot）
 * 与 ChatPanel 头部状态文案共用同一判定层序，消除两处矛盾。
 *
 * 根因记录：圆点此前只读生命周期列（后端 agents.status，仅 HR 操作会改、
 * 几乎恒为 active），看似"状态不准"；真正的忙闲权威是进程内存的
 * disposition + WS status_change（实时广播进 store.agentDispositions）。
 * 「在干嘛」由节点第二行的 live 相位徽章（livePhase：LLM/工具/等待…）负责，
 * 不与圆点混层。
 */

export type AgentDotVisual = {
  /** 圆点 Tailwind 类（颜色 + 可选脉冲） */
  dotClass: string;
  /** 悬浮解释文案（title 属性） */
  title: string;
};

/** turn-exit disposition → 等待/交付态圆点。
 * runnable（可运行=空闲）不入表，回落生命周期层；complete=已交付（本轮收口）。 */
const DISPOSITION_DOTS: Record<string, AgentDotVisual> = {
  waiting_human: { dotClass: "bg-g-yellow-vivid animate-pulse", title: "等待你处理" },
  waiting_agent: { dotClass: "bg-g-blue-vivid animate-pulse", title: "等待其他 Agent" },
  waiting_timer: { dotClass: "bg-g-blue-vivid", title: "等待定时唤醒" },
  blocked: { dotClass: "bg-g-red animate-pulse", title: "阻塞" },
  complete: { dotClass: "bg-g-blue", title: "已交付" },
};

/** agents.status 生命周期 → 圆点（active 未在忙即视为空闲） */
const LIFECYCLE_DOTS: Record<string, AgentDotVisual> = {
  active: { dotClass: "bg-g-fg-4/70", title: "空闲" },
  idle: { dotClass: "bg-g-fg-4/70", title: "空闲" },
  inactive: { dotClass: "bg-g-fg-4/70", title: "空闲" },
  promoted: { dotClass: "bg-g-blue", title: "已晋升" },
  receiving: { dotClass: "bg-g-yellow-vivid animate-pulse", title: "接收中" },
  merging: { dotClass: "bg-g-purple-vivid animate-pulse", title: "合并中" },
  dissolving: { dotClass: "bg-g-red", title: "解散中" },
  archived: { dotClass: "bg-g-red", title: "已归档" },
};

export type ResolvedAgentStatus =
  | { layer: "health"; message: string }
  | { layer: "processing" }
  | { layer: "disposition"; key: string }
  | { layer: "lifecycle"; key: string };

/**
 * 统一优先级判定（圆点与 ChatPanel 头部共用）：
 * 健康错误 > 运行中 > 等待/交付(disposition) > 生命周期。
 *
 * ⚠ processing 必须先于 disposition：disposition 只在 turn-exit 更新，
 * 处理期间 store 常驻旧值（runnable / 残留 waiting_*），若 disposition
 * 优先就会出现「实际运行中、头部却显示等待/Idle」的矛盾文案（审计 M1）。
 * runnable 不算等待（回落生命周期＝空闲）；waiting_timer / complete 须
 * 与 chat/constants.ts statusLabels 键域对齐（那边也补了 waiting_timer）。
 */
export function resolveAgentStatus(input: {
  healthError?: string | null;
  isProcessing?: boolean;
  disposition?: string | null;
  lifecycle?: string | null;
}): ResolvedAgentStatus {
  if (input.healthError) return { layer: "health", message: input.healthError };
  if (input.isProcessing) return { layer: "processing" };
  const d = input.disposition;
  if (d && DISPOSITION_DOTS[d]) return { layer: "disposition", key: d };
  return { layer: "lifecycle", key: input.lifecycle || "idle" };
}

export function deriveAgentDot(input: {
  /** agent_health 错误信息（null/undefined = 健康） */
  healthError?: string | null;
  /** WS status_change 的 processing 集合命中 */
  isProcessing?: boolean;
  /** store.agentDispositions[id]（turn exit 广播的实时等待态） */
  disposition?: string | null;
  /** 生命周期列（node.status） */
  lifecycle?: string | null;
}): AgentDotVisual {
  const r = resolveAgentStatus(input);
  if (r.layer === "health") {
    return { dotClass: "bg-g-red animate-pulse", title: `健康异常：${r.message}` };
  }
  if (r.layer === "processing") {
    return { dotClass: "bg-g-green-vivid animate-pulse", title: "运行中" };
  }
  if (r.layer === "disposition") return DISPOSITION_DOTS[r.key];
  return LIFECYCLE_DOTS[r.key] ?? { dotClass: "bg-g-fg-3", title: r.key };
}

/** agent_health 记录 → 错误文案（节点与窄条共用同一判定，防两处漂移）。
 * 与 OrgTree 原逻辑等价：仅 error 级生效；带 projectId 时须匹配当前项目。 */
export function deriveHealthError(
  health: { health?: string; message?: string; projectId?: string } | undefined,
  selectedProjectId: string | null,
): string | null {
  if (!health || health.health !== "error") return null;
  if (health.projectId && health.projectId !== selectedProjectId) return null;
  return health.message || "未知错误";
}
