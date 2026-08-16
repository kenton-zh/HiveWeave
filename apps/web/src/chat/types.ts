export interface AgentInfo {
  id: string;
  name: string;
  role: string;
  status: string;
  parentId?: string | null;
  position?: string;
}

export interface ToolCall {
  tool: string;
  input: Record<string, any>;
  /** Stream tool_call_id — used to dedup replayed chips. */
  id?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system" | "team";
  content: string;
  images?: string[];
  timestamp: number;
  toolCalls?: ToolCall[];
  isBackground?: boolean;
  isRead?: boolean;
  isStreaming?: boolean;
  isContext?: boolean;
  teamFromAgentId?: string;
  teamToAgentId?: string;
  _thinking?: string;
  _segments?: MsgSegment[];
}

export interface MsgSegment {
  type: "text" | "tool_call" | "thinking";
  content?: string;
  tool?: ToolCall;
}

export interface StreamDraft {
  assistantId: string;
  segments: MsgSegment[];
  /** Set when DB load failed after done — keep draft visible as final content. */
  persisted?: boolean;
}
