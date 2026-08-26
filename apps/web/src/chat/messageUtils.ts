import type {
  ChatMessage,
  ContextMarkerKind,
  MsgSegment,
  StreamDraft,
  ToolCall,
  MessageSource,
} from "./types";

/**
 * Keep the FULL block timeline across tool-loop rounds (DSH-style whole-turn
 * view): prior narration/thinking segments stay visible; tools dedup by id in
 * appendToolCallSegment. Backend round_start still resets its own DB text
 * accumulator — that only affects the mid-stream DB snapshot, not this draft.
 */
export function beginStreamRound(draft: StreamDraft): StreamDraft {
  return draft;
}

/**
 * Append a tool chip. If `toolCallId` is present and a segment already has
 * that id (replay / re-subscribe), no-op. Missing id always appends so
 * legitimate repeat calls of the same tool name are kept.
 */
export function appendToolCallSegment(
  draft: StreamDraft,
  toolCall: ToolCall,
  toolCallId?: string,
): StreamDraft {
  const id = toolCallId || undefined;
  if (id) {
    const exists = draft.segments.some(
      (s) => s.type === "tool_call" && s.tool?.id === id,
    );
    if (exists) return draft;
    return {
      ...draft,
      segments: [...draft.segments, { type: "tool_call", tool: { ...toolCall, id } }],
    };
  }
  return {
    ...draft,
    segments: [...draft.segments, { type: "tool_call", tool: toolCall }],
  };
}

/** Parse a stream_tool / tool_use event payload. Empty ids are treated as missing. */
export function parseToolUsePayload(
  raw: string,
): { toolCall: ToolCall; toolCallId?: string } | null {
  try {
    const toolData = JSON.parse(raw);
    const rawName: string = toolData.toolName || toolData.tool_name || toolData.tool || "";
    const toolName = String(rawName).replace(/^hiveweave__/, "");
    const argsRaw = toolData.arguments || toolData.input || {};
    let args: Record<string, any> = {};
    if (typeof argsRaw === "string") {
      try {
        args = JSON.parse(argsRaw);
      } catch {
        args = {};
      }
    } else if (argsRaw && typeof argsRaw === "object") {
      args = argsRaw;
    }
    const idRaw = toolData.toolCallId || toolData.tool_call_id || "";
    const toolCallId =
      typeof idRaw === "string" && idRaw.trim() ? idRaw.trim() : undefined;
    return { toolCall: { tool: toolName || "unknown", input: args, status: "running" }, toolCallId };
  } catch {
    return null;
  }
}

/**
 * tool_result 事件 → 更新 draft 中对应工具段的状态与结果摘要。
 * 匹配规则：tool_call_id 优先；缺失才按工具名兜底，且只更新最后一个
 * 匹配的 running 段（并行同名工具不写串）。事件里的工具名可能带
 * hiveweave__ 前缀（与段内已剥离名对齐）。已完结（ok/error）的段
 * 不覆盖 —— 重复事件（双广播路径）天然幂等。
 */
export function applyToolResult(
  draft: StreamDraft,
  toolCallId: string | undefined,
  toolName: string | undefined,
  success: boolean,
  result: string,
): StreamDraft {
  const name = toolName ? toolName.replace(/^hiveweave__/, "") : undefined;
  let idx = -1;
  for (let i = draft.segments.length - 1; i >= 0; i--) {
    const s = draft.segments[i];
    if (s.type !== "tool_call" || !s.tool || s.tool.status !== "running") continue;
    if (toolCallId ? s.tool.id === toolCallId : name !== undefined && s.tool.tool === name) {
      idx = i;
      break;
    }
  }
  if (idx === -1) return draft;
  const seg = draft.segments[idx];
  const next: StreamDraft = {
    ...draft,
    segments: [
      ...draft.segments.slice(0, idx),
      { ...seg, tool: { ...seg.tool!, status: success ? ("ok" as const) : ("error" as const), result } },
      ...draft.segments.slice(idx + 1),
    ],
  };
  return next;
}

/**
 * Increment-only badge pop token. First observation (lastSeen null), remount,
 * and non-increases keep the token so CSS pop does not replay.
 */
export function nextBadgePopToken(
  lastSeenCount: number | null,
  currentCount: number,
  prevToken: number,
): { token: number; lastSeen: number } {
  if (lastSeenCount === null || currentCount <= lastSeenCount) {
    return { token: prevToken, lastSeen: currentCount };
  }
  return { token: prevToken + 1, lastSeen: currentCount };
}

export function draftFromStreamingMessage(
  msg: ChatMessage,
  opts?: { includeTools?: boolean },
): StreamDraft {
  const segments: MsgSegment[] = [];
  if (msg._thinking) segments.push({ type: "thinking", content: msg._thinking });
  // Live subscribe replays tool_use with ids. Hydrating DB chips (often
  // without id) then replaying the same calls duplicates the row.
  if (opts?.includeTools !== false) {
    for (const t of msg.toolCalls ?? []) {
      segments.push({ type: "tool_call", tool: t });
    }
  }
  if (msg.content) segments.push({ type: "text", content: msg.content });
  return {
    assistantId: msg.id,
    segments,
    isBackground: msg.isBackground === true,
  };
}

/** Passive/trigger stream defaults to background; honor payload when present. */
function coerceBoolFlag(value: unknown): boolean | undefined {
  if (typeof value === "boolean") return value;
  if (value === 0 || value === 1) return value === 1;
  return undefined;
}

export function streamEventBackgroundFlag(payload: unknown): boolean | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  const rec = payload as Record<string, unknown>;
  const fromSnake = coerceBoolFlag(rec.is_background);
  if (fromSnake !== undefined) return fromSnake;
  return coerceBoolFlag(rec.isBackground);
}

export function streamEventIsBackground(payload: unknown, fallback = true): boolean {
  const flag = streamEventBackgroundFlag(payload);
  return flag === undefined ? fallback : flag;
}

export function isTeamChannelMessage(msg: ChatMessage): boolean {
  return (
    msg.role === "team" ||
    (msg.isBackground === true && msg.role === "user")
  );
}

export function mergeStreamDraftIntoMessages(
  messages: ChatMessage[],
  streamDraft: StreamDraft | null,
  opts: { isStreaming: boolean },
): ChatMessage[] {
  if (!streamDraft) {
    return messages.map((m) => (m.isStreaming ? { ...m, isStreaming: false } : m));
  }
  const hasPersistedDraft = !!streamDraft.persisted;
  if (!opts.isStreaming && !hasPersistedDraft) {
    return messages.map((m) => (m.isStreaming ? { ...m, isStreaming: false } : m));
  }
  return messages.map((m) => {
    const isTarget = m.id === streamDraft.assistantId;
    if (!isTarget && hasPersistedDraft) return m;
    if (!isTarget) {
      return m.isStreaming ? { ...m, isStreaming: false } : m;
    }
    const textParts = streamDraft.segments.filter((s) => s.type === "text").map((s) => s.content || "");
    const thinkingParts = streamDraft.segments
      .filter((s) => s.type === "thinking")
      .map((s) => s.content || "");
    const newTools = streamDraft.segments.filter((s) => s.type === "tool_call").map((s) => s.tool!);
    return {
      ...m,
      content: textParts.join(""),
      toolCalls: newTools.length > 0 ? newTools : m.toolCalls || [],
      _segments: streamDraft.segments,
      _thinking: thinkingParts.join(""),
      isStreaming: hasPersistedDraft ? false : true,
      isBackground: typeof streamDraft.isBackground === "boolean"
        ? streamDraft.isBackground
        : m.isBackground,
    };
  });
}

export function tryParseToolCalls(raw: string): ToolCall[] {
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Normalize OpenAI tool_call format to our ToolCall interface.
    // Backend stores: [{"function": {"name": "list_files", "arguments": "{\"path\": \".\"}"}, "id": "...", "type": "function"}]
    // Frontend expects: [{tool: "list_files", input: {path: "."}}]
    // status 默认 "ok"：走此解析的是已落库历史消息（无 metadata.segments），
    // 工具必然已执行完——缺省会让 ToolCallRow 永久渲染 spinner。
    return parsed.map((tc: any): ToolCall => {
      const id = typeof tc.id === "string" && tc.id.trim() ? tc.id.trim() : undefined;
      // 显式 status 优先；缺省时 ok:false（tool_history 失败标记）→ error，
      // 否则 ok（历史消息工具已执行完）。
      const status: ToolCall["status"] =
        tc.status === "error" || tc.status === "running"
          ? tc.status
          : tc.ok === false
            ? "error"
            : "ok";
      // Already in our format
      if (tc.tool && tc.input) {
        return id ? { tool: tc.tool, input: tc.input, id, status } : { tool: tc.tool, input: tc.input, status };
      }
      // OpenAI format: {function: {name, arguments}}
      if (tc.function) {
        let input: Record<string, any> = {};
        if (typeof tc.function.arguments === "string") {
          try {
            input = JSON.parse(tc.function.arguments);
          } catch {
            input = {};
          }
        } else if (typeof tc.function.arguments === "object" && tc.function.arguments) {
          input = tc.function.arguments;
        }
        return id
          ? { tool: tc.function.name || "unknown", input, id, status }
          : { tool: tc.function.name || "unknown", input, status };
      }
      // Unknown format — best effort
      return id
        ? { tool: tc.name || tc.tool || "unknown", input: tc.input || tc.arguments || {}, id, status }
        : { tool: tc.name || tc.tool || "unknown", input: tc.input || tc.arguments || {}, status };
    });
  } catch {
    return [];
  }
}

export function tryParseImages(raw: string): string[] {
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed;
    return [];
  } catch {
    return [];
  }
}

function tryParseMeta(raw: unknown): Record<string, any> | null {
  if (!raw) return null;
  if (typeof raw === "object") return raw as Record<string, any>;
  if (typeof raw !== "string") return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

/** 落库 metadata.segments（后端 build_display_segments）→ 前端 MsgSegment。 */
function normalizePersistedSegments(raw: unknown): MsgSegment[] | undefined {
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const segs: MsgSegment[] = [];
  for (const s of raw) {
    if (!s || typeof s !== "object") continue;
    if (s.type === "text" && typeof s.content === "string" && s.content) {
      segs.push({ type: "text", content: s.content });
    } else if (s.type === "thinking" && typeof s.content === "string" && s.content) {
      segs.push({ type: "thinking", content: s.content });
    } else if (s.type === "tool_call" && s.tool) {
      segs.push({
        type: "tool_call",
        tool: {
          tool: String(s.tool),
          input: (s.input && typeof s.input === "object" ? s.input : {}) as Record<string, any>,
          id: typeof s.id === "string" ? s.id : undefined,
          status: s.status === "error" ? "error" : s.status === "running" ? "running" : "ok",
          result: typeof s.result === "string" ? s.result : undefined,
        },
      });
    }
  }
  return segs.length > 0 ? segs : undefined;
}

const WATCHDOG_MARKERS = ["看门狗", "[SILENCE]", "[WAIT_TIMEOUT]"];

/** metadata.context_marker → 上下文边界类型（未知值忽略）。 */
function normalizeContextMarker(raw: unknown): ContextMarkerKind | undefined {
  return raw === "compaction" || raw === "prune" ? raw : undefined;
}

/** metadata.source 缺失（legacy 消息）时的回退推断。 */
export function inferMessageSource(
  m: any,
  meta: Record<string, any> | null,
): MessageSource | undefined {
  if (meta?.source === "user" || meta?.source === "agent" || meta?.source === "system" || meta?.source === "watchdog") {
    return meta.source;
  }
  const from = meta?.from_agent_id ?? m.teamFromAgentId ?? m.team_from_agent_id;
  const role = m.role;
  if (role === "user") {
    const bg = !!(m.isBackground ?? m.is_background);
    if (!bg) return "user";
    if (from && from !== "system") return "agent";
    const content = typeof m.content === "string" ? m.content : "";
    if (WATCHDOG_MARKERS.some((k) => content.includes(k))) return "watchdog";
    return "system";
  }
  return undefined;
}

export function mapDbToChatMessages(dbMessages: any[]): ChatMessage[] {
  if (!Array.isArray(dbMessages)) return [];
  return dbMessages.map((m: any) => {
    const meta = tryParseMeta(m.metadata);
    return {
      id: m.id,
      role: m.role,
      content: m.content,
      _thinking: m.thinking || undefined,
      images: typeof m.images === "string" ? tryParseImages(m.images) : m.images,
      timestamp: m.createdAt ?? m.created_at ?? Date.now(),
      toolCalls: m.toolCalls ?? m.tool_calls
        ? tryParseToolCalls(m.toolCalls ?? m.tool_calls)
        : undefined,
      isBackground: !!(m.isBackground ?? m.is_background),
      isRead: !!(m.isRead ?? m.is_read),
      isStreaming: !!(m.isStreaming ?? m.is_streaming),
      isContext: !!(m.isContext ?? m.is_context),
      teamFromAgentId: m.teamFromAgentId ?? m.team_from_agent_id ?? undefined,
      teamToAgentId: m.teamToAgentId ?? m.team_to_agent_id ?? undefined,
      source: inferMessageSource(m, meta),
      fromAgentId: meta?.from_agent_id ?? m.teamFromAgentId ?? m.team_from_agent_id ?? null,
      _segments: normalizePersistedSegments(meta?.segments),
      _contextMarker: normalizeContextMarker(meta?.context_marker),
    };
  });
}

export function getDirectedAgentId(msg: ChatMessage, agentParentId?: string | null): string | null {
  if (!msg.toolCalls || msg.toolCalls.length === 0) return null;
  for (const tc of msg.toolCalls) {
    if ((tc.tool === "dispatch_task" || tc.tool === "message_peer") && tc.input.toAgentId) return tc.input.toAgentId;
    if (tc.tool === "reject_work" && tc.input.subordinateId) return tc.input.subordinateId;
    if (tc.tool === "message_superior" && agentParentId) return agentParentId;
  }
  return null;
}

export function isInjectedContext(msg: ChatMessage): boolean {
  return msg.isContext === true;
}

/** Strip live-stream flags before writing the per-agent chat cache. */
export function sanitizeMessagesForCache(messages: ChatMessage[]): ChatMessage[] {
  return messages
    .map((m) => (m.isStreaming ? { ...m, isStreaming: false } : m))
    .filter(
      (m) =>
        !(
          m.role === "assistant" &&
          !m.isStreaming &&
          !m.content &&
          (!m.toolCalls || m.toolCalls.length === 0)
        ),
    );
}

/**
 * ChatPanel is not remounted on agent switch, so `messages` can still belong
 * to the previous person while `agentId` has already changed. Never write that
 * snapshot into the new agent's cache, and never persist a loading-empty list
 * over a populated session (refresh would be the only way to see 团队沟通 again).
 */
export function shouldWriteChatCache(opts: {
  agentId: string;
  messagesOwnerId: string | null;
  persistReady: boolean;
  next: ChatMessage[];
  existing: ChatMessage[] | undefined;
}): boolean {
  if (!opts.persistReady) return false;
  if (opts.messagesOwnerId !== opts.agentId) return false;
  if (opts.next.length === 0 && opts.existing && opts.existing.length > 0) return false;
  if (
    opts.existing &&
    opts.existing.length === opts.next.length &&
    opts.existing.every((c, i) => c === opts.next[i])
  ) {
    return false;
  }
  return true;
}


export function formatToolInputHint(tool: string, input: Record<string, any> | undefined | null): string | null {
  if (!input || typeof input !== "object") return null;
  const pick = (...keys: string[]) => {
    for (const k of keys) {
      const v = input[k];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
    return null;
  };
  switch (tool) {
    case "read_file":
    case "write_file":
    case "edit_file":
    case "delete_file":
      return pick("filePath", "path");
    case "list_files":
      return pick("dirPath", "path", "directory");
    case "glob":
    case "search_files":
      return pick("pattern", "query", "search");
    case "fetch_url":
      return pick("url");
    case "dispatch_task":
    case "message_peer":
      return pick("toAgentId", "agentId");
  }
  const generic = pick("filePath", "path", "pattern", "name", "id");
  if (generic) {
    const max = 48;
    return generic.length > max ? generic.slice(0, max) + "\u2026" : generic;
  }
  return null;
}
