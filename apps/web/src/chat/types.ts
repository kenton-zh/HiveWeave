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
export type MessageSource =
  | "user"
  | "web"
  | "agent"
  | "agent_to_user"
  | "system"
  | "watchdog";

/**
 * 上下文边界标记（后端 store.py 压缩/裁剪落地后发出）。
 * conversation_turns 被重写而 chat_messages 只追加 —— 没有这个标记时
 * UI 会显示模型早已忘记的历史。渲染为一条分界线而非普通系统气泡。
 */
export type ContextMarkerKind = "compaction" | "prune";

/**
 * 聊天附件引用（学 DSH ImageAttachmentRef 的字段语义，见
 * docs/前端设计规格.md §10.1.3）：
 * - `urlOrId` 是存储句柄（不透明 id 或 URL），**优先于**内联 base64 ——
 *   持久化引用走句柄，避免把字节塞进消息体。
 * - `name` 仅供显示，产出方必须剥掉本地路径成分（DSH types.ts:22-23）。
 * - `mediaType` 应由存储字节验证，不信任声明方。
 * 注意：后端 chat_messages 表/接口当前**没有** attachments 字段（只有
 * images TEXT），该字段是前端先行建模 —— 后端回传待接（见 P1 完成报告）。
 */
/**
 * fixplan #8：平台计算的**交付状态徽章**。
 *
 * 后端三条用户可见出口（`message_user` / `send_message(to=用户)` / `question`）
 * 都会把它挂进 `chat_messages.metadata`，**与消息正文完全无关** ——
 * 所以换措辞/换语言/否定句都不会影响它（这正是它取代"8 词出口门禁"的理由：
 * 那是文本判据，改个说法就绕过了）。
 *
 * 缺省（无该字段）= 老消息 / 非 CEO 消息 ⇒ **不渲染**，不破坏现状。
 */
export interface DeliveryBadge {
  state: "unmarked" | "complete" | "blocked";
  /** `complete` 时的核验时间（ISO-8601）。 */
  deliveredAt?: string;
  /** 未通过核验时的待收口项（给人读的一句话）。 */
  blockers?: string[];
}

export interface AttachmentRef {
  kind: "image" | "file";
  /** 显示名（剥路径，只留文件名）。 */
  name: string;
  mediaType?: string;
  bytes?: number;
  width?: number;
  height?: number;
  /** 存储句柄：不透明附件 id 或可直接渲染/下载的 URL。 */
  urlOrId: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system" | "team";
  content: string;
  images?: string[];
  /** 结构化附件（图片走 gallery，file 走文件 chip）。后端回传待接。 */
  attachments?: AttachmentRef[];
  /** fixplan #8 交付状态徽章（后端 metadata.delivery_state）。缺省不渲染。 */
  deliveryBadge?: DeliveryBadge;
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
  type: "text" | "tool_call" | "thinking" | "round_boundary";
  content?: string;
  tool?: ToolCall;
  /** round_boundary 专用：实际轮号（0 起号，与后端 round_start 同口径）。
   *  live 由 beginStreamRound 插入，持久化由后端 build_display_segments
   *  产出 —— 同 kind 同渲染分支（live==persisted）。 */
  round?: number;
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
