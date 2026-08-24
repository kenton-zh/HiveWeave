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
  /** 执行状态：流式期间由 tool_use/tool_result 事件维护。 */
  status?: "running" | "ok" | "error";
  /** 工具结果摘要（流式事件 500 字 / 落库 segments 2000 字截断）。 */
  result?: string;
}

/** 消息来源（metadata.source；legacy 消息由前端推断）。 */
export type MessageSource = "user" | "agent" | "system" | "watchdog";

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
  source?: MessageSource;
  fromAgentId?: string | null;
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
  /** Passive/trigger stream — hide from main Chat; do not put chips in 团队沟通. */
  isBackground?: boolean;
}
