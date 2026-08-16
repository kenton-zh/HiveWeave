import type { ChatMessage, MsgSegment, StreamDraft, ToolCall } from "./types";

/** Drop prior-round narration; keep tool chips. Matches backend round_start text-acc reset. */
export function beginStreamRound(draft: StreamDraft): StreamDraft {
  return {
    ...draft,
    segments: draft.segments.filter((s) => s.type === "tool_call"),
  };
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
    return { toolCall: { tool: toolName || "unknown", input: args }, toolCallId };
  } catch {
    return null;
  }
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
    return parsed.map((tc: any): ToolCall => {
      const id = typeof tc.id === "string" && tc.id.trim() ? tc.id.trim() : undefined;
      // Already in our format
      if (tc.tool && tc.input) {
        return id ? { tool: tc.tool, input: tc.input, id } : { tool: tc.tool, input: tc.input };
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
          ? { tool: tc.function.name || "unknown", input, id }
          : { tool: tc.function.name || "unknown", input };
      }
      // Unknown format — best effort
      return id
        ? { tool: tc.name || tc.tool || "unknown", input: tc.input || tc.arguments || {}, id }
        : { tool: tc.name || tc.tool || "unknown", input: tc.input || tc.arguments || {} };
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

export function mapDbToChatMessages(dbMessages: any[]): ChatMessage[] {
  if (!Array.isArray(dbMessages)) return [];
  return dbMessages.map((m: any) => ({
    id: m.id,
    role: m.role,
    content: m.content,
    _thinking: m.thinking || undefined,
    images: typeof m.images === "string" ? tryParseImages(m.images) : m.images,
    timestamp: m.createdAt ?? m.created_at ?? Date.now(),
    toolCalls: m.toolCalls ? tryParseToolCalls(m.toolCalls) : undefined,
    isBackground: !!(m.isBackground ?? m.is_background),
    isRead: !!(m.isRead ?? m.is_read),
    isStreaming: !!(m.isStreaming ?? m.is_streaming),
    isContext: !!(m.isContext ?? m.is_context),
    teamFromAgentId: m.teamFromAgentId ?? m.team_from_agent_id ?? undefined,
    teamToAgentId: m.teamToAgentId ?? m.team_to_agent_id ?? undefined,
  }));
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
