import { create } from "zustand";
import { mergeDeltaContent } from "./utils/mergeDelta";
import type { Project, AgentLiveStatus } from "./api";
import { parseDeepLink } from "./components/timeline/useDeepLink";

// 深链恢复：#view=timeline&task=<id> 直接同视角打开（v4 §5.5.1）
const _deepLink = parseDeepLink();

interface ActiveCommunication {
  id: string;
  fromAgentId?: string;
  toAgentId?: string;
  type: string;
  createdAt: number;
}

interface PendingApproval {
  id: string;
  agentId: string;
  toolName: string;
  toolArguments: string;
  description: string;
  status: string;
  createdAt: number;
}

/**
 * Soonest pending alarm for an agent, used to render a countdown pill on the
 * org-tree card. `currentGameSeconds`/`sampledAt` are sampled together so the
 * frontend can extrapolate the live game time without an extra round-trip.
 */
export interface AgentAlarmInfo {
  purpose: string;
  fireAtGameSeconds: number;
  currentGameSeconds: number;
  sampledAt: number;
}

/**
 * Latest health state for an agent, driven by lobby "agent_health" events
 * ({ type, agentId, projectId, health: "error" | "ok", message, at }).
 * Nodes read this to turn their card red on LLM/model call errors.
 * `projectId` lets nodes ignore stale entries from a previously open project.
 */
export interface AgentHealthInfo {
  health: "error" | "ok";
  message: string;
  at: number;
  projectId?: string;
}

/**
 * Live model actually being used by an agent, driven by "model_resolved"
 * stream events. Updates on every turn start and on failover.
 */
export interface AgentActiveModelInfo {
  modelName: string;
  modelId: string;
  source: string; // "tier_resolved" | "failover"
  failedModel?: string;
  at: number;
}

/**
 * 团队开会状态（docs/spec/team-meeting.md §前端）：由 lobby
 * "meeting_updated" 事件驱动，按 seq 幂等合并（at-least-once 语义）。
 * ChatPanel 状态条 / OrgTree 开会徽标只读此字段；会务流不进 chatSessions。
 */
export interface MeetingStatusInfo {
  meetingId: string;
  projectId: string;
  status: "assembling" | "collecting" | "facilitating" | "concluded" | "aborted";
  topicIndex: number;
  roundIndex: number;
  title?: string;
  seq: number;
  updatedAt: number;
}

/** 会议仍占锁的状态（ChatPanel 状态条 / OrgTree 徽标的显示条件）。 */
export const MEETING_ACTIVE_STATUSES = new Set([
  "assembling",
  "collecting",
  "facilitating",
]);

interface AppState {
  selectedAgentId: string | null;
  setSelectedAgent: (id: string | null) => void;
  activeView: "tree" | "office" | "timeline" | "token";
  setActiveView: (view: "tree" | "office" | "timeline" | "token") => void;
  rightPanelTab: "chat" | "agent" | "logs" | "goals" | "monitor" | "task" | "debug";
  setRightPanelTab: (tab: "chat" | "agent" | "logs" | "goals" | "monitor" | "task" | "debug") => void;
  // Timeline — 团队活动可视化（v4）
  selectedTaskId: string | null;
  setSelectedTask: (id: string | null) => void;
  /** WS task_event 失效信号合并后的版本号；面板监听它触发 REST 重新拉取 */
  timelineVersion: number;
  notifyTaskEvent: (projectId: string, taskId: string) => void;
  chatSessions: Record<string, ChatMessage[]>;
  addMessage: (agentId: string, msg: ChatMessage) => void;
  replaceMessage: (agentId: string, oldId: string, newMsg: ChatMessage) => void;
  removeMessage: (agentId: string, msgId: string) => void;
  setChatMessages: (agentId: string, messages: ChatMessage[]) => void;
  clearChatSessions: () => void;
  orgTreeVersion: number;
  refreshOrgTree: () => void;
  goalsVersion: number;
  goalsUpdatedProjectId: string | null;
  bumpGoalsVersion: (projectId: string) => void;
  socketReconnectVersion: number;
  bumpSocketReconnect: () => void;
  questionVersion: number;
  bumpQuestionVersion: () => void;
  activeCommunications: ActiveCommunication[];
  setActiveCommunications: (comms: ActiveCommunication[]) => void;
  userName: string;
  setUserName: (name: string) => void;
  projects: Project[];
  setProjects: (projects: Project[]) => void;
  selectedProjectId: string | null;
  setSelectedProjectId: (id: string | null) => void;
  apiKey: string | null;
  setApiKey: (key: string | null) => void;
  // Pending approvals
  pendingApprovals: Record<string, PendingApproval[]>; // keyed by agentId
  setPendingApprovals: (agentId: string, approvals: PendingApproval[]) => void;
  setAllPendingApprovals: (approvals: PendingApproval[]) => void;
  removeApproval: (requestId: string) => void;
  // Runtime processing status — which agents are currently processing (LLM/API activity)
  processingAgents: string[];
  setProcessingAgents: (ids: string[]) => void;
  updateProcessingAgent: (id: string, processing: boolean) => void;
  // Orthogonal disposition (waiting_human / blocked / complete / …)
  agentDispositions: Record<string, string>;
  setAgentDisposition: (id: string, disposition: string) => void;
  // User ping notification — agents that have sent user-directed messages
  userPingAgentIds: string[];
  setUserPingAgentIds: (ids: string[]) => void;
  // Pending scheduled alarms — soonest alarm per agent (keyed by toAgentId)
  agentAlarms: Record<string, AgentAlarmInfo>;
  setAgentAlarms: (alarms: Record<string, AgentAlarmInfo>) => void;
  // Agent health — lobby "agent_health" events flag agents with LLM/model errors
  agentHealth: Record<string, AgentHealthInfo>;
  setAgentHealth: (agentId: string, info: AgentHealthInfo | null) => void;
  clearAgentHealth: () => void;
  // Agent active model — live "model_resolved" events track actual model in use
  agentActiveModel: Record<string, AgentActiveModelInfo>;
  setAgentActiveModel: (agentId: string, info: AgentActiveModelInfo | null) => void;
  // Per-agent 实时活动相位（LLM/工具/子代理…，4s 轮询一份）——OrgTree 徽章
  // 与 ChatPanel 头部共用同一数据源（09-08 #9：两处状态同帧矛盾根因）
  liveMap: Record<string, AgentLiveStatus>;
  setLiveMap: (map: Record<string, AgentLiveStatus>) => void;
  // 团队开会 — lobby "meeting_updated"（seq 幂等合并；见 MeetingStatusInfo）
  activeMeeting: MeetingStatusInfo | null;
  setActiveMeeting: (info: MeetingStatusInfo | null) => void;
  // Pending initial message — set by NewProjectDialog, consumed by ChatPanel on mount
  pendingInitialMessage: { agentId: string; message: string } | null;
  setPendingInitialMessage: (msg: { agentId: string; message: string } | null) => void;
  // Real-time activity feed — live agent actions visible in Logs
  activityFeed: ActivityEntry[];
  addActivity: (entry: ActivityEntry) => void;
  clearActivity: () => void;
  _activityFeedInternal: ActivityEntry[];
  _activityRafPending: boolean;
  // Toast notifications — replaces native alert() so messages are captured
  // by browser automation tools and styled consistently with the app
  toasts: ToastItem[];
  showToast: (message: string, type?: ToastType) => void;
  dismissToast: (id: string) => void;
  // Debug log — captures all API calls, WebSocket events, and errors
  debugLogs: DebugLogEntry[];
  addDebugLog: (entry: Omit<DebugLogEntry, "id" | "timestamp">) => void;
  clearDebugLogs: () => void;
}

export interface DebugLogEntry {
  id: string;
  timestamp: number;
  category: "api" | "ws" | "error" | "info" | "state";
  message: string;
  data?: any;
}

export type ToastType = "info" | "success" | "error" | "warning";
export interface ToastItem {
  id: string;
  message: string;
  type: ToastType;
  createdAt: number;
}

export interface ActivityEntry {
  agentId: string;
  agentName: string;
  type: "thinking" | "text" | "tool_use" | "tool_result" | "done" | "error" | "text_delta" | "thinking_delta";
  content?: string;
  deltaId?: string;
  toolName?: string;
  // The Elixir backend sometimes forwards these as raw objects (from the
  // stream_event) and sometimes as JSON strings (from the activity broadcast).
  // Renderers must handle both shapes.
  toolInput?: string | object;
  toolResult?: string | object;
  errorMessage?: string;
  timestamp: number;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system" | "team";
  content: string;
  images?: string[];
  timestamp: number;
  isBackground?: boolean;
  isRead?: boolean;
  toolCalls?: Array<{ tool: string; input: Record<string, any> }>;
  teamFromAgentId?: string;
  teamToAgentId?: string;
  isContext?: boolean;
  isStreaming?: boolean;
}

// WS task_event 合并计时器（模块级，避免进 state 引起渲染抖动）
let _taskEventCoalesceTimer: ReturnType<typeof setTimeout> | null = null;

export const useAppStore = create<AppState>((set, get) => ({
  selectedAgentId: null,
  setSelectedAgent: (id) => set({ selectedAgentId: id }),
  activeView: _deepLink.view ?? "tree",
  setActiveView: (view) => set({ activeView: view }),
  rightPanelTab: _deepLink.taskId ? "task" : "chat",
  setRightPanelTab: (tab) => set({ rightPanelTab: tab }),
  // Timeline — 选中任务时右栏自动切到"任务"页签；清除时若停留在该页签则回落聊天
  selectedTaskId: _deepLink.taskId,
  setSelectedTask: (id) =>
    set((s) =>
      id
        ? { selectedTaskId: id, rightPanelTab: "task" }
        : {
            selectedTaskId: null,
            ...(s.rightPanelTab === "task" ? { rightPanelTab: "chat" as const } : {}),
          },
    ),
  timelineVersion: 0,
  notifyTaskEvent: (projectId, _taskId) => {
    // 只关心当前项目的信号；WS 只是失效信号，真正数据走 REST。
    if (get().selectedProjectId !== projectId) return;
    if (_taskEventCoalesceTimer) return; // 1s 内的突发信号合并成一次刷新
    _taskEventCoalesceTimer = setTimeout(() => {
      _taskEventCoalesceTimer = null;
      set((s) => ({ timelineVersion: s.timelineVersion + 1 }));
    }, 1000);
  },
  chatSessions: {},
  addMessage: (agentId, msg) =>
    set((state) => ({
      chatSessions: {
        ...state.chatSessions,
        [agentId]: [...(state.chatSessions[agentId] || []), msg],
      },
    })),
  replaceMessage: (agentId, oldId, newMsg) =>
    set((state) => ({
      chatSessions: {
        ...state.chatSessions,
        [agentId]: (state.chatSessions[agentId] || []).map((m) =>
          m.id === oldId ? newMsg : m
        ),
      },
    })),
  removeMessage: (agentId, msgId) =>
    set((state) => ({
      chatSessions: {
        ...state.chatSessions,
        [agentId]: (state.chatSessions[agentId] || []).filter(
          (m) => m.id !== msgId
        ),
      },
    })),
  setChatMessages: (agentId, messages) =>
    set((state) => ({
      chatSessions: { ...state.chatSessions, [agentId]: messages },
    })),
  clearChatSessions: () => set({ chatSessions: {} }),
  orgTreeVersion: 0,
  refreshOrgTree: () => set((s) => ({ orgTreeVersion: s.orgTreeVersion + 1 })),
  goalsVersion: 0,
  goalsUpdatedProjectId: null,
  bumpGoalsVersion: (projectId: string) => set((s) => ({ goalsVersion: s.goalsVersion + 1, goalsUpdatedProjectId: projectId })),
  socketReconnectVersion: 0,
  bumpSocketReconnect: () => set((s) => ({ socketReconnectVersion: s.socketReconnectVersion + 1 })),
  questionVersion: 0,
  bumpQuestionVersion: () => set((s) => ({ questionVersion: s.questionVersion + 1 })),
  activeCommunications: [],
  setActiveCommunications: (comms) => set({ activeCommunications: comms }),
  userName: (typeof localStorage !== "undefined" ? localStorage.getItem("hiveweave-user-name") : null) || "用户",
  setUserName: (name) => {
    if (typeof localStorage !== "undefined") localStorage.setItem("hiveweave-user-name", name);
    set({ userName: name });
  },
  projects: [],
  setProjects: (projects) => set({ projects }),
  selectedProjectId: null,
  setSelectedProjectId: (id) => set({ selectedProjectId: id }),
  apiKey: null,
  setApiKey: (key) => set({ apiKey: key }),
  // Pending approvals
  pendingApprovals: {},
  setPendingApprovals: (agentId, approvals) =>
    set((state) => ({
      pendingApprovals: {
        ...state.pendingApprovals,
        [agentId]: approvals,
      },
    })),
  setAllPendingApprovals: (approvals) =>
    set((state) => {
      const grouped: Record<string, PendingApproval[]> = {};
      for (const a of approvals) {
        if (!grouped[a.agentId]) grouped[a.agentId] = [];
        grouped[a.agentId].push(a);
      }
      return { pendingApprovals: grouped };
    }),
  removeApproval: (requestId) =>
    set((state) => {
      const newApprovals: Record<string, PendingApproval[]> = {};
      for (const [agentId, approvals] of Object.entries(state.pendingApprovals)) {
        newApprovals[agentId] = approvals.filter((a) => a.id !== requestId);
      }
      return { pendingApprovals: newApprovals };
    }),
  // Runtime processing status
  processingAgents: [],
  setProcessingAgents: (ids) => set((state) => {
    // Avoid unnecessary re-renders when the list hasn't changed
    if (state.processingAgents.length === ids.length &&
        state.processingAgents.every((a, i) => a === ids[i])) {
      return state;
    }
    return { processingAgents: ids };
  }),
  updateProcessingAgent: (id, processing) =>
    set((state) => {
      const current = new Set(state.processingAgents);
      const wasProcessing = current.has(id);
      // No change — return same reference to skip re-render
      if (processing === wasProcessing) return state;
      if (processing) current.add(id);
      else current.delete(id);
      return { processingAgents: [...current] };
    }),
  agentDispositions: {},
  setAgentDisposition: (id, disposition) =>
    set((state) => {
      if (state.agentDispositions[id] === disposition) return state;
      return {
        agentDispositions: { ...state.agentDispositions, [id]: disposition },
      };
    }),
  // User ping notifications
  userPingAgentIds: [],
  setUserPingAgentIds: (ids) => set({ userPingAgentIds: ids }),
  // Pending scheduled alarms
  agentAlarms: {},
  setAgentAlarms: (alarms) => set({ agentAlarms: alarms }),
  // Agent health (LLM/model call errors) — "ok" (or null) clears the entry
  agentHealth: {},
  setAgentHealth: (agentId, info) =>
    set((state) => {
      const next = { ...state.agentHealth };
      if (!info || info.health === "ok") {
        if (!(agentId in next)) return state; // nothing to clear
        delete next[agentId];
      } else {
        const prev = next[agentId];
        if (prev && prev.message === info.message && prev.at === info.at) return state;
        next[agentId] = info;
      }
      return { agentHealth: next };
    }),
  clearAgentHealth: () =>
    set((state) =>
      Object.keys(state.agentHealth).length ? { agentHealth: {} } : state
    ),
  // Agent active model (live model_resolved events)
  agentActiveModel: {},
  setAgentActiveModel: (agentId, info) =>
    set((state) => {
      const next = { ...state.agentActiveModel };
      if (!info) {
        if (!(agentId in next)) return state;
        delete next[agentId];
      } else {
        next[agentId] = info;
      }
      return { agentActiveModel: next };
    }),
  // Per-agent 实时活动相位（useLiveStatusPoll 每 4s 整表刷新）
  liveMap: {},
  setLiveMap: (map) => set({ liveMap: map }),
  // Pending initial message
  pendingInitialMessage: null,
  setPendingInitialMessage: (msg) => set({ pendingInitialMessage: msg }),
  // 团队开会状态（meeting_updated → addActivity 拦截写入）
  activeMeeting: null,
  setActiveMeeting: (info) =>
    set((state) => {
      if (info === null) {
        return state.activeMeeting === null ? state : { activeMeeting: null };
      }
      const prev = state.activeMeeting;
      // seq 幂等：同会议旧 seq / 同 seq 重复事件不合并（at-least-once）
      if (
        prev &&
        prev.meetingId === info.meetingId &&
        (info.seq < prev.seq ||
          (info.seq === prev.seq && info.status === prev.status))
      ) {
        return state;
      }
      return { activeMeeting: info };
    }),
  // Live Activity: external immutable array triggers React re-render
  activityFeed: [],
  // Internal mutable buffer — deltas accumulate here without triggering React
  _activityFeedInternal: [] as ActivityEntry[],
  _activityRafPending: false,
  addActivity: (entry) => {
    // Intercept lobby "agent_health" events (not an ActivityEntry — arrives via
    // `as any`): they only drive the agentHealth map that turns org-tree node
    // cards red, and never enter the activity feed.
    const rawEvent = entry as unknown as {
      type?: string;
      agentId?: string;
      projectId?: string;
      health?: string;
      message?: unknown;
      at?: unknown;
    };
    if (rawEvent?.type === "agent_health") {
      const agentId = rawEvent.agentId;
      if (typeof agentId === "string" && agentId) {
        if (rawEvent.health === "ok") {
          get().setAgentHealth(agentId, null);
        } else {
          get().setAgentHealth(agentId, {
            health: "error",
            message:
              typeof rawEvent.message === "string"
                ? rawEvent.message
                : String(rawEvent.message ?? ""),
            at: typeof rawEvent.at === "number" ? rawEvent.at : Date.now(),
            projectId:
              typeof rawEvent.projectId === "string" ? rawEvent.projectId : undefined,
          });
        }
      }
      return;
    }

    // Intercept "model_resolved" events — track actual model in use per agent
    if (rawEvent?.type === "model_resolved") {
      const ev = rawEvent as any;
      const agentId = ev.agentId;
      if (typeof agentId === "string" && agentId && ev.modelName) {
        get().setAgentActiveModel(agentId, {
          modelName: String(ev.modelName),
          modelId: String(ev.modelId ?? ""),
          source: String(ev.source ?? "tier_resolved"),
          failedModel: ev.failedModel ? String(ev.failedModel) : undefined,
          at: Date.now(),
        });
      }
      return;
    }

    // Intercept "meeting_updated" — 团队开会状态（seq 幂等合并，不进活动流）
    if (rawEvent?.type === "meeting_updated") {
      const ev = rawEvent as any;
      const meetingId = ev.meetingId;
      if (typeof meetingId === "string" && meetingId) {
        get().setActiveMeeting({
          meetingId,
          projectId: String(ev.projectId ?? ev.project_id ?? ""),
          status: ev.status,
          topicIndex: Number(ev.topicIndex ?? ev.topic_index ?? 0),
          roundIndex: Number(ev.roundIndex ?? ev.round_index ?? 0),
          title: typeof ev.title === "string" ? ev.title : undefined,
          seq: Number(ev.seq ?? 0),
          updatedAt: Date.now(),
        });
      }
      return;
    }

    const st = get();
    const feed = st._activityFeedInternal;

    // Deduplicate non-delta events: SSE reconnects replay recent events from the
    // server's recentActivity buffer. Skip an incoming event if an entry with the
    // same (agentId, timestamp, type, toolName) already exists in the feed.
    // Delta events (text_delta/thinking_delta) are never replayed (server skips
    // them in agent replay and recent_activity), so they don't need this check.
    if (entry.type !== "text_delta" && entry.type !== "thinking_delta") {
      const dedupKey = `${entry.agentId}|${entry.timestamp}|${entry.type}|${entry.toolName || ""}`;
      for (let i = feed.length - 1; i >= 0; i--) {
        const e = feed[i];
        if (`${e.agentId}|${e.timestamp}|${e.type}|${e.toolName || ""}` === dedupKey) {
          return; // Already in feed — skip replayed duplicate
        }
      }
    }

    if (entry.type === "text_delta" || entry.type === "thinking_delta") {
      // Delta: append/replace to matching entry (immutable update to avoid shared-object mutation).
      // Some LLM/SDKs send the FULL accumulated text per chunk instead of incremental deltas.
      // In that case the new chunk contains the existing content as a prefix — we REPLACE to
      // avoid "好的 好的, 我先我先分析…" style duplication. Real deltas fall through to plain append.
      let found = false;
      for (let i = feed.length - 1; i >= 0; i--) {
        const e = feed[i];
        if (e.agentId === entry.agentId && e.deltaId === entry.deltaId && e.type === entry.type) {
          feed[i] = { ...e, content: mergeDeltaContent(e.content || "", entry.content || ""), timestamp: entry.timestamp };
          found = true;
          break;
        }
      }
      if (!found) {
        feed.push({ ...entry });
        // Cap at 200 entries to prevent unbounded growth in long streaming sessions
        if (feed.length > 200) {
          st._activityFeedInternal = feed.slice(-200);
        }
      }

      // Throttle React re-render to ~60fps via RAF
      if (!st._activityRafPending) {
        st._activityRafPending = true;
        requestAnimationFrame(() => {
          const s = get();
          s._activityRafPending = false;
          set({ activityFeed: [...s._activityFeedInternal] });
        });
      }
      return;
    }

    // Non-delta: add directly, sync to React immediately
    feed.push({ ...entry });
    if (feed.length > 200) {
      st._activityFeedInternal = feed.slice(-200);
    }
    set({ activityFeed: [...st._activityFeedInternal] });
  },
  clearActivity: () => {
    const st = get();
    st._activityFeedInternal = [];
    st._activityRafPending = false; // Reset RAF flag so pending callbacks don't re-populate
    set({ activityFeed: [] });
  },
  // Toast notifications
  toasts: [],
  showToast: (message, type = "info") => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const toast: ToastItem = { id, message, type, createdAt: Date.now() };
    set({ toasts: [...get().toasts, toast] });
    // Auto-dismiss TTL + exit animation are owned by ToastContainer (store stays pure).
  },
  dismissToast: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
  // Debug logs
  debugLogs: [],
  addDebugLog: (entry) => {
    const id = `dbg-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    const fullEntry: DebugLogEntry = { ...entry, id, timestamp: Date.now() };
    const cur = get().debugLogs;
    // Keep last 500 entries
    const next = cur.length >= 500 ? cur.slice(-499) : cur;
    set({ debugLogs: [...next, fullEntry] });
  },
  clearDebugLogs: () => set({ debugLogs: [] }),
}));

// Expose store globally for api.ts debug logging (avoids circular import)
if (typeof window !== "undefined") {
  (window as any).__hwStore = useAppStore;
}
