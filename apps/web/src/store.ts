import { create } from "zustand";

interface AppState {
  selectedAgentId: string | null;
  setSelectedAgent: (id: string | null) => void;
  chatSessions: Record<string, ChatMessage[]>;
  addMessage: (agentId: string, msg: ChatMessage) => void;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: number;
}

export const useAppStore = create<AppState>((set) => ({
  selectedAgentId: null,
  setSelectedAgent: (id) => set({ selectedAgentId: id }),
  chatSessions: {},
  addMessage: (agentId, msg) =>
    set((state) => ({
      chatSessions: {
        ...state.chatSessions,
        [agentId]: [...(state.chatSessions[agentId] || []), msg],
      },
    })),
}));
