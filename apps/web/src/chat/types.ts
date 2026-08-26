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

/**
 * 上下文边界标记（后端 store.py 压缩/裁剪落地后发出）。
 * conversation_turns 被重写而 chat_messages 只追加 —— 没有这个标记时
 * UI 会显示模型早已忘记的历史。渲染为一条分界线而非普通系统气泡。
 */
export type ContextMarkerKind = "compaction" | "prune";

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
  /** 非空 → 该消息是上下文边界标记，渲染为分界线。 */
  _contextMarker?: ContextMarkerKind;
  /** 本轮流端到端耗时的冻结统计（done 时结算；tokens 用渲染时的估算口径，
   *  分子分母一致）。仅会话内有效——DB 不存生成耗时，刷新后丢失；
   *  会话内 reload 由 loadMessagesFromDb 按 id 携带。 */
  _genStats?: { ms: number };
}

export interface MsgSegment {
  type: "text" | "tool_call" | "thinking";
  content?: string;
  tool?: ToolCall;
}

export interface StreamDraft {
  assistantId: string;
  segments: MsgSegment[];
  /** 流开始时间（首个事件到达）。用于气泡头部实时 tok/s。 */
  startedAt?: number;
  /** Set when DB load failed after done — keep draft visible as final content. */
  persisted?: boolean;
  /** Passive/trigger stream — hide from main Chat; do not put chips in 团队沟通. */
  isBackground?: boolean;
}
