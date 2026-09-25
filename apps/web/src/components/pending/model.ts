/**
 * FE-14「待我处理」聚合模型（设计方案 §11.1）
 *
 * 本文件是**纯函数层**：把四组既有数据源（问题 / 审批 / 验收中任务 /
 * 提醒）归一成统一的 PendingItem 行，供面板渲染与计数。不做任何 IO，
 * 不持有状态 —— 聚合口径全部在这里可单测。
 *
 * 数据源映射（全部为**既有** API，不新建后端端点）：
 *   待回答  → getQuestions({ projectId, status: "pending" })   （与 QuestionDialog 同源）
 *   待授权  → getProjectPendingApprovals(projectId)            （与 OrgTree 轮询同源）
 *   待验收  → listTasks(projectId, { status: "verifying" })    （口径见 AGG 口径注释）
 *   需关注  → getUserPings + getProjectAlarms + store.agentHealth（error 项）
 *
 * 「待验收」口径说明：平台没有「交付物需用户确认」的专用信号 —— 任务
 * 状态机（services/tasks/constants.py）里 verifying = 交付物已提交并进入
 * 平台 VERIFY 验收环节；CEO 对用户的终验走 message_user（普通消息通道，
 * 不进本面板）。因此本组取 status=verifying 的任务聚合，条目跳转任务窗。
 */
import type { PendingApproval, PendingQuestion, ProjectAlarm, UserPing } from "../../api";

export type PendingGroupId = "questions" | "approvals" | "acceptance" | "attention";

/** 面板统一条目。字段都是**展示**用途，业务语义在数据源处已经成立。 */
export interface PendingItem {
  /** 稳定 React key：组前缀 + 源 id */
  key: string;
  groupId: PendingGroupId;
  /** 发起 / 相关成员（点 ⇒ openAgentChat）；广播类条目可能缺省 */
  actorId?: string;
  /** 一行摘要 */
  summary: string;
  /** 辅助说明（影响范围 / 唤醒时间 / 错误详情），可缺省 */
  note?: string;
  /** 相关任务 id（点 ⇒ openTask） */
  taskId?: string;
  /** 等待起点（现实毫秒）；无时间语义的条目（定时唤醒）缺省且不显示等待时长 */
  since?: number;
}

/** listTasks 返回行的结构子集（避免为类型耦合 timeline/types）。 */
export interface VerifyingTaskRow {
  id: string;
  title?: string | null;
  status?: string;
  assignee_id?: string | null;
  submitted_at?: number | null;
  updated_at?: number | null;
}

/** store.agentHealth 里 error 条目的归一形（projectId 过滤已由调用方完成）。 */
export interface HealthErrorRow {
  agentId: string;
  message: string;
  at: number;
}

export interface PendingRawInput {
  questions: PendingQuestion[];
  approvals: PendingApproval[];
  tasks: VerifyingTaskRow[];
  pings: UserPing[];
  alarms: ProjectAlarm[];
  /** 与 alarms 同一响应采样的当前游戏秒数（算唤醒倒计时用） */
  alarmNowGameSeconds?: number;
  healthErrors: HealthErrorRow[];
}

/** 分组元数据（顺序 = 呈现顺序）。 */
export const GROUP_META: Array<{
  id: PendingGroupId;
  label: string;
  emptyTitle: string;
  emptyDescription: string;
}> = [
  {
    id: "questions",
    label: "待回答",
    emptyTitle: "没有等待你回答的问题",
    emptyDescription: "成员需要澄清时会在这里提问",
  },
  {
    id: "approvals",
    label: "待授权",
    emptyTitle: "没有待授权的请求",
    emptyDescription: "成员请求敏感操作权限时会出现在这里",
  },
  {
    id: "acceptance",
    label: "待验收",
    emptyTitle: "没有验收中的任务",
    emptyDescription: "任务提交进入验收环节后会出现在这里",
  },
  {
    id: "attention",
    label: "需关注",
    emptyTitle: "没有需要关注的提醒",
    emptyDescription: "成员提醒、定时唤醒与运行错误会出现在这里",
  },
];

/** 审批工具名 → 展示名（与 ApprovalDialog 同规则）。 */
export function formatToolName(name: string): string {
  return name.replace(/^hiveweave__/, "").replace(/_/g, " ");
}

/** 现实等待时长 → 短文案（「刚刚 / 5 分钟 / 3 小时 / 2 天」）。 */
export function formatWait(ms: number): string {
  if (!Number.isFinite(ms) || ms < 60_000) return "刚刚";
  const m = Math.floor(ms / 60_000);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时`;
  return `${Math.floor(h / 24)} 天`;
}

/** 游戏秒 → 短文案（唤醒倒计时等游戏时间语义）。 */
export function formatGameSeconds(s: number): string {
  if (!Number.isFinite(s) || s <= 0) return "已到点";
  if (s < 60) return `${Math.round(s)} 秒`;
  const m = s / 60;
  if (m < 60) return `${Math.round(m)} 分钟`;
  const h = m / 60;
  if (h < 24) return `${Math.round(h)} 小时`;
  return `${Math.round(h / 24)} 天`;
}

function asArray<T>(v: T[] | null | undefined): T[] {
  return Array.isArray(v) ? v : [];
}

/** 取第一个合法的正毫秒时间戳（后端 0/null 皆视为缺省）。 */
function firstPositiveMs(...candidates: Array<number | null | undefined>): number | undefined {
  for (const c of candidates) {
    if (typeof c === "number" && c > 0) return c;
  }
  return undefined;
}

/** 四组数据源 → 统一条目列表（畸形审批条目保留可见，FE-08 纪律）。 */
export function buildPendingItems(raw: PendingRawInput): PendingItem[] {
  const items: PendingItem[] = [];

  for (const q of asArray(raw.questions)) {
    if (!q?.id) continue;
    items.push({
      key: `q-${q.id}`,
      groupId: "questions",
      actorId: q.agentId || undefined,
      summary: q.question || "（问题内容为空）",
      since: typeof q.createdAt === "number" ? q.createdAt : undefined,
    });
  }

  for (const a of asArray(raw.approvals)) {
    if (!a?.id) continue;
    const tool = a.toolName ? formatToolName(a.toolName) : "未知工具";
    items.push({
      key: `a-${a.id}`,
      groupId: "approvals",
      actorId: a.agentId || undefined,
      summary: a.description || `请求执行 ${tool}`,
      note: a.malformed
        ? `数据异常：缺少关键字段${a.missingFields?.length ? `（${a.missingFields.join("、")}）` : ""}，不可直接批准`
        : undefined,
      since: typeof a.createdAt === "number" && a.createdAt > 0 ? a.createdAt : undefined,
    });
  }

  for (const t of asArray(raw.tasks)) {
    if (!t?.id) continue;
    items.push({
      key: `t-${t.id}`,
      groupId: "acceptance",
      actorId: t.assignee_id || undefined,
      summary: t.title || "（无标题任务）",
      note: "交付物进入验收环节",
      taskId: t.id,
      since: firstPositiveMs(t.submitted_at, t.updated_at),
    });
  }

  for (const p of asArray(raw.pings)) {
    if (!p) continue;
    const actorId = p.agentId || p.agentIds?.[0] || undefined;
    const broadcast =
      Array.isArray(p.agentIds) && p.agentIds.length > 1 ? p.agentIds.length : 0;
    items.push({
      key: `ping-${p.id ?? `${p.agentId ?? "unknown"}-${p.timestamp ?? ""}`}`,
      groupId: "attention",
      actorId,
      summary: p.content || "成员向你发来了提醒",
      note: broadcast > 1 ? `同时发给 ${broadcast} 名成员` : undefined,
      since: typeof p.timestamp === "number" && p.timestamp > 0 ? p.timestamp : undefined,
    });
  }

  const alarmNow = raw.alarmNowGameSeconds;
  for (const al of asArray(raw.alarms)) {
    if (!al?.id) continue;
    items.push({
      key: `alarm-${al.id}`,
      groupId: "attention",
      actorId: al.toAgentId || undefined,
      summary: al.purpose || "成员设定了定时唤醒",
      note:
        typeof alarmNow === "number" && typeof al.fireAtGameSeconds === "number"
          ? `游戏时间约 ${formatGameSeconds(al.fireAtGameSeconds - alarmNow)}后唤醒`
          : undefined,
      // 定时唤醒没有「已等待」语义：仅用于排序，不展示等待时长
      since: undefined,
    });
  }

  for (const h of asArray(raw.healthErrors)) {
    if (!h?.agentId) continue;
    items.push({
      key: `health-${h.agentId}`,
      groupId: "attention",
      actorId: h.agentId,
      summary: "成员运行出错",
      note: h.message || "未知错误（详见成员监控面板）",
      since: typeof h.at === "number" && h.at > 0 ? h.at : undefined,
    });
  }

  return items;
}

export interface PendingGroupView {
  id: PendingGroupId;
  label: string;
  emptyTitle: string;
  emptyDescription: string;
  items: PendingItem[];
}

/** 条目按组归类（组顺序 = GROUP_META），组内按等待起点升序（最久未处理在前），无时间戳条目排组尾。 */
export function groupPendingItems(items: PendingItem[]): PendingGroupView[] {
  return GROUP_META.map((meta) => ({
    ...meta,
    items: items
      .filter((it) => it.groupId === meta.id)
      .sort((a, b) => (a.since ?? Number.POSITIVE_INFINITY) - (b.since ?? Number.POSITIVE_INFINITY)),
  }));
}
