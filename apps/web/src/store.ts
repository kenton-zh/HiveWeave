import { create } from "zustand";

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

interface AppState {
  selectedAgentId: string | null;
  setSelectedAgent: (id: string | null) => void;
  activeView: "tree" | "office";
  setActiveView: (view: "tree" | "office") => void;
  rightPanelTab: "chat" | "agent" | "logs";
  setRightPanelTab: (tab: "chat" | "agent" | "logs") => void;
  chatSessions: Record<string, ChatMessage[]>;
  addMessage: (agentId: string, msg: ChatMessage) => void;
  replaceMessage: (agentId: string, oldId: string, newMsg: ChatMessage) => void;
  removeMessage: (agentId: string, msgId: string) => void;
  clearChatSessions: () => void;
  orgTreeVersion: number;
  refreshOrgTree: () => void;
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
  // Real-time activity feed — live agent actions visible in Logs
  activityFeed: ActivityEntry[];
  addActivity: (entry: ActivityEntry) => void;
  clearActivity: () => void;
}

export interface ActivityEntry {
  agentId: string;
  agentName: string;
  type: "thinking" | "text" | "tool_use" | "tool_result" | "done" | "error";
  content?: string;
  toolName?: string;
  toolInput?: string;
  toolResult?: string;
  errorMessage?: string;
  timestamp: number;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  images?: string[];
  timestamp: number;
  isBackground?: boolean;
  isRead?: boolean;
}

export const useAppStore = create<AppState>((set) => ({
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
  clearChatSessions: () => set({ chatSessions: {} }),
  orgTreeVersion: 0,
  refreshOrgTree: () => set((s) => ({ orgTreeVersion: s.orgTreeVersion + 1 })),
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
  activityFeed: [],
  addActivity: (entry) =>
    set((state) => ({
      activityFeed: [...state.activityFeed.slice(-199), { ...entry }],
    })),
  clearActivity: () => set({ activityFeed: [] }),
}));
