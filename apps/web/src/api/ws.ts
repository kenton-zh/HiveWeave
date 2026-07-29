import { Socket, Channel } from "phoenix";
import { dbg, getApiKey } from "./shared";

/**
 * Phoenix.js WebSocket-based API client.
 *
 * Channels used:
 *   - "lobby:status"  - global agent processing status
 *   - "agent:<id>"    - per-agent chat stream + status + inbox
 *   - "project:<id>"  - per-project game time + status
 */

const SOCKET_URL =
  (import.meta.env.VITE_WS_URL as string | undefined) ||
  (typeof window !== "undefined" && window.location.hostname === "localhost"
    ? "ws://localhost:4000/socket"
    : "/socket");

let _socket: Socket | null = null;

// Persistent per-agent channels: one joined channel per agent, reused across
// messages.  This prevents the backend's join/3 from calling
// Phoenix.PubSub.subscribe multiple times (which would duplicate event delivery).
// IMPORTANT: Store on globalThis to survive Vite HMR module reloads.
// Without this, HMR resets these Maps to empty, causing streamChat to create
// duplicate agent channels — each receiving the same stream_chunk events,
// resulting in "结巴" (stutter/duplication) in the streaming display.
const _agentChannels: Map<string, any> = (globalThis as any).__hw_agentChannels ?? new Map();
(globalThis as any).__hw_agentChannels = _agentChannels;
const _agentHandlers: Map<string, (event: ChatEvent) => void> = (globalThis as any).__hw_agentHandlers ?? new Map();
(globalThis as any).__hw_agentHandlers = _agentHandlers;
/** Cache in-flight channel.join() promises — phoenix.js throws on second join() while joining. */
const _agentJoinPromises: Map<string, Promise<void>> =
  (globalThis as any).__hw_agentJoinPromises ?? new Map();
(globalThis as any).__hw_agentJoinPromises = _agentJoinPromises;

/**
 * Join once and reuse the same Promise while state === "joining".
 * Never call channel.join() twice on the same Channel instance.
 * Clear the cached promise on ok/error so a later reconnect recreates cleanly.
 */
function joinChannelOnce(agentId: string, channel: any): Promise<void> {
  if (channel.state === "joined") {
    _agentJoinPromises.delete(agentId);
    return Promise.resolve();
  }
  const inflight = _agentJoinPromises.get(agentId);
  if (inflight && channel.state === "joining") {
    return inflight;
  }
  // Joining without our promise (e.g. HMR) — attach to existing JoinPush, never re-join().
  if (channel.state === "joining") {
    const p = new Promise<void>((resolve, reject) => {
      const push = channel.joinPush;
      if (push?.receive) {
        push
          .receive("ok", () => {
            _agentJoinPromises.delete(agentId);
            resolve();
          })
          .receive("error", (resp: any) => {
            _agentJoinPromises.delete(agentId);
            reject(resp);
          });
      } else {
        const t = window.setInterval(() => {
          if (channel.state === "joined") {
            window.clearInterval(t);
            _agentJoinPromises.delete(agentId);
            resolve();
          } else if (channel.state !== "joining") {
            window.clearInterval(t);
            _agentJoinPromises.delete(agentId);
            reject(new Error("channel join aborted"));
          }
        }, 50);
      }
    });
    _agentJoinPromises.set(agentId, p);
    return p;
  }
  // Stale promise from a prior successful join — drop and start fresh join.
  _agentJoinPromises.delete(agentId);
  const p = new Promise<void>((resolve, reject) => {
    try {
      channel
        .join()
        .receive("ok", () => {
          _agentJoinPromises.delete(agentId);
          resolve();
        })
        .receive("error", (resp: any) => {
          _agentJoinPromises.delete(agentId);
          reject(resp);
        });
    } catch (err) {
      _agentJoinPromises.delete(agentId);
      reject(err);
    }
  });
  _agentJoinPromises.set(agentId, p);
  return p;
}

export function getSocket(): Socket {
  // Use globalThis to survive Vite HMR — without this, HMR resets _socket
  // to null, creating a second WebSocket connection while the old one stays
  // alive. Two sockets = two agent channels = duplicate stream_chunk events.
  if (!(globalThis as any).__hw_socket) {
    const params: Record<string, string> = {};
    const apiKey = getApiKey();
    if (apiKey) params.api_key = apiKey;
    const socket = new Socket(SOCKET_URL, {
      params,
      reconnectAfterMs: (tries: number) => [1000, 2000, 5000, 10000][tries - 1] ?? 10000,
      heartbeatIntervalMs: 30_000,
    });
    socket.connect();
    (globalThis as any).__hw_socket = socket;
    _socket = socket;
  }
  return (globalThis as any).__hw_socket as Socket;
}

export interface ChatEvent {
  type: "text" | "text_delta" | "thinking_delta" | "tool_use" | "tool_result" | "message_id" | "error" | "done" | "busy" | "approval_request" | "retry" | "queued_message" | "thinking";
  data: string;
  deltaId?: string;
  elapsed_s?: number;
}

export function streamChat(
  agentId: string,
  message: string,
  images: string[] | undefined,
  onEvent: (event: ChatEvent) => void
): { abort: () => void } {
  const socket = getSocket();
  dbg("ws", `streamChat called for ${agentId}`, { agentId, messageLen: message.length, socketConnected: (socket as any).isConnected?.() ?? false });

  if (!(globalThis as any).__hw_lastSeq) (globalThis as any).__hw_lastSeq = {};
  (globalThis as any).__hw_lastSeq[agentId] = 0;
  _agentHandlers.set(agentId, onEvent);

  let channel = _agentChannels.get(agentId);

  // Helper: push chat message to channel
  const pushChat = (ch: any) => {
    ch.push("chat", { message, images: images?.length ? images : undefined });
  };

  if (channel && channel.state === "joined") {
    dbg("ws", `push chat (channel already joined) for ${agentId}`);
    pushChat(channel);
  } else if (channel && channel.state === "joining") {
    // Reuse in-flight JoinPush — never call channel.join() again (phoenix joinedOnce).
    dbg("ws", `channel still joining for ${agentId}, waiting for join`);
    joinChannelOnce(agentId, channel)
      .then(() => {
        dbg("ws", `channel joined (deferred) for ${agentId}, pushing chat`);
        pushChat(channel);
      })
      .catch((resp: any) => {
        dbg("error", `deferred channel join FAILED for ${agentId}`, resp);
        const handler = _agentHandlers.get(agentId);
        handler?.({ type: "error", data: JSON.stringify(resp) });
      });
  } else {
    if (channel) {
      dbg("ws", `channel state=${channel.state}, leaving old channel for ${agentId}`);
      try { channel.leave(); } catch {}
      _agentChannels.delete(agentId);
      _agentJoinPromises.delete(agentId);
    }

    channel = socket.channel(`agent:${agentId}`);
    _agentChannels.set(agentId, channel);
    dbg("ws", `creating new channel agent:${agentId}`);

    bindAgentChannelEvents(channel, agentId);

    joinChannelOnce(agentId, channel)
      .then(() => {
        dbg("ws", `channel joined for ${agentId}, pushing chat`);
        pushChat(channel);
      })
      .catch((resp: any) => {
        dbg("error", `channel join FAILED for ${agentId}`, resp);
        const handler = _agentHandlers.get(agentId);
        handler?.({ type: "error", data: JSON.stringify(resp) });
      });
  }

  return {
    abort: () => {
      dbg("ws", `abort called for ${agentId}`);
      channel?.push("cancel", {});
      _agentHandlers.delete(agentId);
    },
  };
}


function bindAgentChannelEvents(channel: any, agentId: string) {
  channel.on("init", () => { dbg("ws", `init event for ${agentId}`); });

  channel.on("message_id", (payload: any) => {
    dbg("ws", `message_id for ${agentId}`, payload);
    const handler = _agentHandlers.get(agentId);
    handler?.({ type: "message_id", data: JSON.stringify(payload) });
  });

  channel.on("stream_chunk", (payload: any) => {
    const handler = _agentHandlers.get(agentId);
    if (!handler) return;
    const text = typeof payload === "string" ? payload : payload.text || "";
    if (typeof payload === "object" && payload.delta) {
      const deltaId = payload.deltaId || "";
      const seq = payload.seq;
      if (typeof seq === "number") {
        const lastSeq = (globalThis as any).__hw_lastSeq ?? {};
        const last = lastSeq[agentId] ?? 0;
        if (seq <= last) return;
        lastSeq[agentId] = seq;
        (globalThis as any).__hw_lastSeq = lastSeq;
      }
      if (payload.reasoning) {
        handler({ type: "thinking_delta", data: text, deltaId });
      } else {
        handler({ type: "text_delta", data: text, deltaId });
      }
    } else {
      dbg("ws", `stream_chunk (non-delta) for ${agentId}: ${text.slice(0, 100)}`);
      handler({ type: "text", data: text });
    }
  });

  channel.on("stream_tool", (payload: any) => {
    const handler = _agentHandlers.get(agentId);
    if (!handler) return;
    if (payload.type === "tool_use") {
      dbg("ws", `tool_use for ${agentId}: ${payload.toolName}`, payload);
      handler({ type: "tool_use", data: JSON.stringify(payload) });
    } else if (payload.type === "tool_result") {
      dbg("ws", `tool_result for ${agentId}: ${payload.toolName}`);
      handler({ type: "tool_result", data: JSON.stringify(payload) });
    }
  });

  channel.on("status_change", () => {
    dbg("ws", `status_change for ${agentId}`);
  });

  channel.on("thinking", (payload: any) => {
    const handler = _agentHandlers.get(agentId);
    if (!handler) return;
    const elapsed = typeof payload === "object" ? payload.elapsed_s : undefined;
    handler({ type: "thinking", data: "", elapsed_s: elapsed });
  });

  channel.on("done", () => {
    dbg("ws", `done event for ${agentId}`);
    const handler = _agentHandlers.get(agentId);
    handler?.({ type: "done", data: "" });
  });

  channel.on("error", (payload: any) => {
    dbg("error", `error event for ${agentId}: ${payload?.message || "Unknown"}`, payload);
    const handler = _agentHandlers.get(agentId);
    handler?.({ type: "error", data: payload?.message || "Unknown error" });
  });

  channel.on("busy", (payload: any) => {
    dbg("ws", `busy event for ${agentId}: ${payload?.message || "busy"}`, payload);
    const handler = _agentHandlers.get(agentId);
    handler?.({ type: "busy", data: payload?.message || "Agent is busy" });
  });
}

/**
 * Join (or create) a persistent agent channel without sending a message.
 * Used to warm up the WebSocket before onboarding / first chat.
 */
export function joinAgentChannel(agentId: string): Promise<void> {
  const socket = getSocket();

  const existing = _agentChannels.get(agentId);
  if (existing?.state === "joined") {
    return Promise.resolve();
  }
  if (existing && existing.state === "joining") {
    return joinChannelOnce(agentId, existing);
  }

  if (existing) {
    try { existing.leave(); } catch {}
    _agentChannels.delete(agentId);
    _agentJoinPromises.delete(agentId);
  }

  const channel = socket.channel(`agent:${agentId}`);
  _agentChannels.set(agentId, channel);
  bindAgentChannelEvents(channel, agentId);
  dbg("ws", `joinAgentChannel creating channel agent:${agentId}`);

  return joinChannelOnce(agentId, channel).then(() => {
    dbg("ws", `joinAgentChannel joined for ${agentId}`);
  });
}

/**
 * Explicitly leave an agent's persistent channel. Call this when the agent
 * is deleted or when you need to force a fresh channel on the next message.
 */
export function leaveAgentChannel(agentId: string) {
  const channel = _agentChannels.get(agentId);
  if (channel) {
    // BUG-034: 不再发送 cancel — 切换 agent 时不应停止后台 agent 的
    // LLM 流。Agent 会继续运行，用户可以随时切回来查看结果。
    try { channel.leave(); } catch {}
    _agentChannels.delete(agentId);
  }
  _agentJoinPromises.delete(agentId);
  _agentHandlers.delete(agentId);
}

/**
 * Passively subscribe to an agent's stream events without sending a message.
 * Used when switching back to an agent that's still processing — we resume
 * receiving stream_chunk, stream_tool, done, and error events.
 *
 * Returns a cleanup function that unregisters the handler.
 */
export function subscribeAgentStream(
  agentId: string,
  onEvent: (event: ChatEvent) => void,
): () => void {
  _agentHandlers.set(agentId, onEvent);
  // Ensure the channel is joined (reuses existing joined channel, or joins fresh)
  joinAgentChannel(agentId).catch(() => {});
  return () => {
    _agentHandlers.delete(agentId);
  };
}

// ---------------------------------------------------------------------------
// Agent status subscription (was SSE-based before)
// ---------------------------------------------------------------------------

export interface ActivityEntry {
  agentId: string;
  agentName: string;
  type: string;
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

export function subscribeAgentStatus(
  onSnapshot: (agentIds: string[], paused?: boolean) => void,
  onStatus: (agentId: string, processing: boolean, disposition?: string) => void,
  onActivity?: (event: ActivityEntry) => void,
  onOrgChanged?: () => void,
  onGoalsUpdated?: (projectId: string) => void,
  onQuestionAsked?: () => void,
): { abort: () => void } {
  const socket = getSocket();
  const channel = socket.channel("lobby:status");

  channel.on("init", (payload: Record<string, unknown>) => {
    if (Array.isArray(payload.agentIds)) {
      onSnapshot(payload.agentIds, (payload.paused as boolean | undefined) ?? false);
    }
  });

  channel.on("status_change", (payload: Record<string, unknown>) => {
    if (typeof payload.agentId === "string") {
      const processing =
        typeof payload.processing === "boolean"
          ? payload.processing
          : payload.status === "processing";
      const disposition =
        typeof payload.disposition === "string" ? payload.disposition : undefined;
      onStatus(payload.agentId, !!processing, disposition);
    }
  });

  channel.on("org_changed", () => {
    onOrgChanged?.();
  });

  channel.on("goals_updated", (payload: Record<string, unknown>) => {
    if (typeof payload.projectId === "string") {
      onGoalsUpdated?.(payload.projectId);
    }
  });

  channel.on("question_asked", () => {
    onQuestionAsked?.();
  });

  channel.on("activity", (payload: Record<string, unknown>) => {
    if (onActivity && typeof payload.agentId === "string") {
      onActivity({
        agentId: payload.agentId as string,
        agentName: (payload.agentName as string | undefined) || "",
        type: (payload.type as string | undefined) || "",
        content: payload.content as string | undefined,
        deltaId: payload.deltaId as string | undefined,
        toolName: payload.toolName as string | undefined,
        toolInput: payload.toolInput as string | object | undefined,
        toolResult: payload.toolResult as string | object | undefined,
        errorMessage: payload.errorMessage as string | undefined,
        timestamp: (payload.timestamp as number | undefined) || Date.now(),
      });
    }
  });

  // Live model resolution events — routed to onActivity for store interception
  channel.on("model_resolved", (payload: Record<string, unknown>) => {
    if (onActivity && typeof payload.agentId === "string") {
      onActivity(payload as any);
    }
  });

  channel.join().receive("ok", () => {
    // Initial snapshot is pushed via "init" event
  }).receive("error", () => {
    onSnapshot([], false);
  });

  return {
    abort: () => {
      channel.leave();
    },
  };
}

// Re-export the Channel for advanced usage
export { Channel };
