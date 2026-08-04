/**
 * Timeline 前端类型（Timeline v4 §4.2 统一事件 Schema 的 TS 镜像）。
 * 后端字段名保持 snake_case 直映——聚合端点输出即契约，前端不做改名。
 */

/** 统一事件 schema（task_events / handoffs / inbox / work_logs 归并）。 */
export interface TimelineEvent {
  id: string;
  /** 现实毫秒时间戳 */
  ts: number;
  /** task.created / task.claimed / ... / handoff.created / inbox.message / work_log */
  type: string;
  task_id: string;
  agent_id?: string | null;
  from_agent_id?: string | null;
  to_agent_id?: string | null;
  from_status?: string | null;
  to_status?: string | null;
  reason_code?: string | null;
  /** 后端给的中文标题（单一来源） */
  title: string;
  /** 原始 payload（结构随 type 变化） */
  detail?: Record<string, unknown> | null;
}

export interface TaskSummary {
  id: string;
  title: string;
  description?: string | null;
  status: string;
  assignee_id?: string | null;
  creator_id?: string | null;
  reviewer_id?: string | null;
  priority?: string | null;
  progress?: number | null;
  tags?: string | string[] | null;
  parent_task_id?: string | null;
  blocked_reason?: string | null;
  wait_kind?: string | null;
  is_archived?: number | boolean | null;
  created_at?: number | null;
  claimed_at?: number | null;
  submitted_at?: number | null;
  closed_at?: number | null;
  updated_at?: number | null;
  archived_at?: number | null;
}

export interface AgentRef {
  id?: string;
  name?: string;
  role?: string | null;
}

/** 端点 1：GET /projects/{pid}/timeline/tasks/{tid} */
export interface TaskTimelineResponse {
  task: TaskSummary;
  /** agent_id → {name, role} */
  agents: Record<string, AgentRef>;
  events: TimelineEvent[];
  max_event_ts: number;
  truncated: boolean;
}

/** 泳道段（端点 2 task_segments 元素，v4 §4.5 输出）。 */
export interface TaskSegment {
  task_id: string;
  title: string;
  assignee_id?: string | null;
  creator_id?: string | null;
  reviewer_id?: string | null;
  status: string;
  started_at: number;
  /** null = 进行中（画到当前时刻红线） */
  ended_at: number | null;
  ongoing: boolean;
}

export interface ActiveAssignment {
  task_id: string;
  task_title: string;
  agent_id: string;
  /** busy = 干活中；waiting = 等别人（含 reviewer） */
  kind: "busy" | "waiting";
  since: number;
}

export interface TeamAgent {
  id: string;
  name: string;
  role?: string | null;
  parent_id?: string | null;
  status?: string | null;
  last_active_at?: number | null;
}

/** 端点 2：GET /projects/{pid}/timeline/activity 的完整响应。 */
export interface TeamActivityResponse {
  agents: TeamAgent[];
  task_segments: TaskSegment[];
  active_assignments: ActiveAssignment[];
  window: { since: number; until: number };
  max_event_ts: number;
  changed: boolean;
  truncated: boolean;
  has_more_earlier: boolean;
}

/** if_changed_since 短路时的最小响应（无数据体）。 */
export interface TeamActivityUnchanged {
  changed: false;
  max_event_ts: number;
  window?: { since: number; until: number };
}

/** lobby:status 上的 task_event 失效信号（v4 §4.1 载荷）。 */
export interface TaskEventSignal {
  type: "task_event";
  kind?: string;
  project_id: string;
  task_id: string;
  event_type?: string;
  to_status?: string | null;
  ts?: number;
}
