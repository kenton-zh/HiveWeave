import type { ChatMessage, StreamDraft, ToolCall } from "./types";

/** Drop prior-round narration; keep tool chips. Matches backend round_start text-acc reset. */
export function beginStreamRound(draft: StreamDraft): StreamDraft {
  return {
    ...draft,
    segments: draft.segments.filter((s) => s.type === "tool_call"),
  };
}

export function isTeamChannelMessage(msg: ChatMessage): boolean {
  return (
    msg.role === "team" ||
    (msg.isBackground === true && msg.role === "user")
  ) as boolean;
}

export function tryParseToolCalls(raw: string): ToolCall[] {
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Normalize OpenAI tool_call format to our ToolCall interface.
    // Backend stores: [{"function": {"name": "list_files", "arguments": "{\"path\": \".\"}"}, "id": "...", "type": "function"}]
    // Frontend expects: [{tool: "list_files", input: {path: "."}}]
    return parsed.map((tc: any): ToolCall => {
      // Already in our format
      if (tc.tool && tc.input) {
        return { tool: tc.tool, input: tc.input };
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
        return { tool: tc.function.name || "unknown", input };
      }
      // Unknown format — best effort
      return { tool: tc.name || tc.tool || "unknown", input: tc.input || tc.arguments || {} };
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
    isBackground: !!m.isBackground,
    isRead: !!m.isRead,
    isStreaming: !!m.isStreaming,
    isContext: !!m.isContext,
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
