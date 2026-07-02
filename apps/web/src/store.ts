import { create } from "zustand";
import { mergeDeltaContent } from "./utils/mergeDelta";

interface ActiveCommunication {
  id: string;
  fromAgentId: string;
  toAgentId: string;
  type: "dispatch" | "message" | "trigger" | "peer";
  createdAt: number;
}

interface Project {
  id: string;
  name: string;
  workspacePath: string | null;
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

interface AppState {
  selectedAgentId: string | null;
  setSelectedAgent: (id: string | null) => void;
  activeView: "tree" | "office";
  setActiveView: (view: "tree" | "office") => void;
  rightPanelTab: "chat" | "agent" | "logs" | "goals";
  setRightPanelTab: (tab: "chat" | "agent" | "logs" | "goals") => void;
  chatSessions: Record<string, ChatMessage[]>;
  addMessage: (agentId: string, msg: ChatMessage) => void;
  replaceMessage: (agentId: string, oldId: string, newMsg: ChatMessage) => void;
  removeMessage: (agentId: string, msgId: string) => void;
  setChatMessages: (agentId: string, messages: ChatMessage[]) => void;
  clearChatSessions: () => void;
  orgTreeVersion: number;
  refreshOrgTree: () => void;
  socketReconnectVersion: number;
  bumpSocketReconnect: () => void;
  activeCommunications: ActiveCommunication[];
  setActiveCommunications: (comms: ActiveCommunication[]) => void;
  userName: string;
  setUserName: (name: string) => void;
  projects: Project[];
  setProjects: (projects: Project[]) => void;
  selectedProjectId: string | null;
  setSelectedProjectId: (id: string | null) => void;
  // Pending approvals
  pendingApprovals: Record<string, PendingApproval[]>; // keyed by agentId
  setPendingApprovals: (agentId: string, approvals: PendingApproval[]) => void;
  setAllPendingApprovals: (approvals: PendingApproval[]) => void;
  removeApproval: (requestId: string) => void;
  // Add agent dialog
  showAddAgent: boolean;
  addAgentParentId: string | null;
  openAddAgent: (parentId?: string | null) => void;
  closeAddAgent: () => void;
  // Runtime processing status — which agents are currently processing (LLM/API activity)
  processingAgents: string[];
  setProcessingAgents: (ids: string[]) => void;
  updateProcessingAgent: (id: string, processing: boolean) => void;
  // User ping notification — agents that have sent user-directed messages
  userPingAgentIds: string[];
  setUserPingAgentIds: (ids: string[]) => void;
  // Pending scheduled alarms — soonest alarm per agent (keyed by toAgentId)
  agentAlarms: Record<string, AgentAlarmInfo>;
  setAgentAlarms: (alarms: Record<string, AgentAlarmInfo>) => void;
  // Real-time activity feed — live agent actions visible in Logs
  activityFeed: ActivityEntry[];
  addActivity: (entry: ActivityEntry) => void;
  clearActivity: () => void;
  _activityFeedInternal: ActivityEntry[];
  _activityRafPending: boolean;
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

export const useAppStore = create<AppState>((set, get) => ({
  selectedAgentId: null,
  setSelectedAgent: (id) => set({ selectedAgentId: id }),
  activeView: "tree",
  setActiveView: (view) => set({ activeView: view }),
  rightPanelTab: "chat",
  setRightPanelTab: (tab) => set({ rightPanelTab: tab }),
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
  socketReconnectVersion: 0,
  bumpSocketReconnect: () => set((s) => ({ socketReconnectVersion: s.socketReconnectVersion + 1 })),
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
  // Add agent dialog
  showAddAgent: false,
  addAgentParentId: null,
  openAddAgent: (parentId) => set({ showAddAgent: true, addAgentParentId: parentId || null }),
  closeAddAgent: () => set({ showAddAgent: false, addAgentParentId: null }),
  // Runtime processing status
  processingAgents: [],
  setProcessingAgents: (ids) => set({ processingAgents: ids }),
  updateProcessingAgent: (id, processing) =>
    set((state) => {
      const current = new Set(state.processingAgents);
      if (processing) current.add(id);
      else current.delete(id);
      return { processingAgents: [...current] };
    }),
  // User ping notifications
  userPingAgentIds: [],
  setUserPingAgentIds: (ids) => set({ userPingAgentIds: ids }),
  // Pending scheduled alarms
  agentAlarms: {},
  setAgentAlarms: (alarms) => set({ agentAlarms: alarms }),
  // Live Activity: external immutable array triggers React re-render
  activityFeed: [],
  // Internal mutable buffer — deltas accumulate here without triggering React
  _activityFeedInternal: [] as ActivityEntry[],
  _activityRafPending: false,
  addActivity: (entry) => {
    const st = get();
    const feed = st._activityFeedInternal;

    // Deduplicate non-delta events: SSE reconnects replay recent events from the
    // server's recentActivity buffer. Skip an incoming event if an entry with the
    // same (agentId, timestamp, type, toolName) already exists in the feed.
    // Delta events (text_delta/thinking_delta) are never replayed (server only
    // buffers non-delta events), so they don't need this check.
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
}));
