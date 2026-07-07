import { useState, useRef, useEffect, useCallback, useMemo } from "react";
import { useAppStore } from "../store";
import { streamChat, getAgent, deleteAgent, getChatMessages, markMessagesRead, leaveAgentChannel, joinAgentChannel } from "../api";
import { mergeDeltaContent } from "../utils/mergeDelta";
import ApprovalDialog from "./ApprovalDialog";
import TodoBar from "./TodoBar";
import { getRoleStyle, getPositionLabel } from "../utils/role-styles";

interface AgentInfo {
  id: string;
  name: string;
  role: string;
  status: string;
  parentId?: string | null;
  position?: string;
}

interface ToolCall {
  tool: string;
  input: Record<string, any>;
}

interface ChatMessage {
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
}

interface MsgSegment {
  type: "text" | "tool_call" | "thinking";
  content?: string;
  tool?: ToolCall;
}

interface StreamDraft {
  assistantId: string;
  segments: MsgSegment[];
}

const roleLabels: Record<string, string> = {
  hr: "HR",
  architect: "Architect",
  manager: "Manager",
  developer: "Developer",
  module_dev: "Developer",
  qa: "QA",
  devops: "DevOps",
};

const toolCategories: Record<string, { color: string; bg: string; label: string }> = {
  dispatch_task: { color: "text-blue-300", bg: "bg-blue-500/15", label: "Dispatch" },
  write_work_log: { color: "text-green-300", bg: "bg-green-500/15", label: "Log" },
  read_work_logs: { color: "text-green-300", bg: "bg-green-500/15", label: "Read Logs" },
  report_completion: { color: "text-green-300", bg: "bg-green-500/15", label: "Complete" },
  approve_work: { color: "text-purple-300", bg: "bg-purple-500/15", label: "Approve" },
  reject_work: { color: "text-red-300", bg: "bg-red-500/15", label: "Reject" },
  review_code: { color: "text-purple-300", bg: "bg-purple-500/15", label: "Review" },
  read_project_memory: { color: "text-amber-300", bg: "bg-amber-500/15", label: "Memory" },
  trigger_integration: { color: "text-amber-300", bg: "bg-amber-500/15", label: "Integration" },
  message_superior: { color: "text-emerald-300", bg: "bg-emerald-500/15", label: "Report Up" },
  message_peer: { color: "text-cyan-300", bg: "bg-cyan-500/15", label: "Peer Msg" },
  send_message: { color: "text-cyan-300", bg: "bg-cyan-500/15", label: "Send" },
  read_agent_status: { color: "text-green-300", bg: "bg-green-500/15", label: "Status" },
  list_subordinates: { color: "text-blue-300", bg: "bg-blue-500/15", label: "Team" },
  create_agent: { color: "text-pink-300", bg: "bg-pink-500/15", label: "Hire" },
  transfer_agent: { color: "text-orange-300", bg: "bg-orange-500/15", label: "Transfer" },
  dismiss_agent: { color: "text-red-300", bg: "bg-red-500/15", label: "Dismiss" },
  update_roster: { color: "text-rose-300", bg: "bg-rose-500/15", label: "Roster" },
  read_roster: { color: "text-rose-300", bg: "bg-rose-500/15", label: "View Roster" },
  list_all_agents: { color: "text-blue-300", bg: "bg-blue-500/15", label: "List All" },
  read_file: { color: "text-slate-300", bg: "bg-slate-500/15", label: "Read" },
  write_file: { color: "text-slate-300", bg: "bg-slate-500/15", label: "Write" },
  edit_file: { color: "text-slate-300", bg: "bg-slate-500/15", label: "Edit" },
  list_files: { color: "text-slate-300", bg: "bg-slate-500/15", label: "List" },
  search_files: { color: "text-slate-300", bg: "bg-slate-500/15", label: "Search" },
  delete_file: { color: "text-red-300", bg: "bg-red-500/15", label: "Delete" },
  glob: { color: "text-slate-300", bg: "bg-slate-500/15", label: "Glob" },
  fetch_url: { color: "text-indigo-300", bg: "bg-indigo-500/15", label: "Fetch" },
  read_charter: { color: "text-violet-300", bg: "bg-violet-500/15", label: "Charter" },
  save_charter: { color: "text-violet-300", bg: "bg-violet-500/15", label: "Save Charter" },
};

const statusLabels: Record<string, { text: string; color: string }> = {
  created: { text: "Created", color: "text-gray-400" },
  active: { text: "Active", color: "text-emerald-400" },
  promoted: { text: "Promoted", color: "text-blue-400" },
  receiving: { text: "Receiving", color: "text-amber-400" },
  merging: { text: "Merging", color: "text-purple-400" },
  dissolving: { text: "Dissolving", color: "text-red-400" },
  archived: { text: "Archived", color: "text-gray-500" },
  idle: { text: "Idle", color: "text-gray-400" },
  working: { text: "Working", color: "text-emerald-400" },
  error: { text: "Error", color: "text-red-400" },
  waiting: { text: "Waiting", color: "text-amber-400" },
};


function isTeamChannelMessage(msg: ChatMessage): boolean {
  // BUG-034 root-cause fix: 触发器保存的 team 消息同时标记了 is_context=True
  // (用于 inbox 注入 LLM 上下文),但前端的 isTeamChannelMessage 不能因此排除
  // 它们 — 否则 teamMessages 永远为空,hasTeamComms=false,折叠区永不显示。
  // 判定完全交给 isBackground+role 双条件,已足够精确。
  return (
    msg.role === "team" ||
    (msg.isBackground === true && (msg.role === "user" || msg.role === "assistant"))
  ) as boolean;
}

function tryParseToolCalls(raw: string): ToolCall[] {
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

function tryParseImages(raw: string): string[] {
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed;
    return [];
  } catch {
    return [];
  }
}

function mapDbToChatMessages(dbMessages: any[]): ChatMessage[] {
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
    teamFromAgentId: m.teamFromAgentId ?? undefined,
    teamToAgentId: m.teamToAgentId ?? undefined,
  }));
}

function getDirectedAgentId(msg: ChatMessage, agentParentId?: string | null): string | null {
  if (!msg.toolCalls || msg.toolCalls.length === 0) return null;
  for (const tc of msg.toolCalls) {
    if ((tc.tool === "dispatch_task" || tc.tool === "message_peer") && tc.input.toAgentId) return tc.input.toAgentId;
    if (tc.tool === "reject_work" && tc.input.subordinateId) return tc.input.subordinateId;
    if (tc.tool === "message_superior" && agentParentId) return agentParentId;
  }
  return null;
}

function isInjectedContext(msg: ChatMessage): boolean {
  return msg.isContext === true;
}


function formatToolInputHint(tool: string, input: Record<string, any> | undefined | null): string | null {
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

function ToolCallsBlock({ toolCalls }: { toolCalls: ToolCall[] }) {
  const [expanded, setExpanded] = useState(false);
  const names = toolCalls.map((tc) => tc.tool);
  const preview = names.slice(0, 6).join(", ") + (names.length > 6 ? ", \u2026" : "");
  const summary = expanded ? "\u25be" : "\u25b8";

  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full text-left text-xs text-gray-400 hover:text-gray-300 transition-colors font-mono truncate"
      >
        {summary} \u5de5\u5177\u8c03\u7528 ({toolCalls.length}) \u2014 {preview}
      </button>
      {expanded && (
        <ul className="mt-1.5 space-y-0.5 font-mono text-xs text-gray-500 pl-3 border-l border-surface-border/60">
          {toolCalls.map((tc, i) => {
            const hint = formatToolInputHint(tc.tool, tc.input);
            return (
              <li key={i} className="truncate">
                <span className="text-gray-400">{tc.tool}</span>
                {hint && <span className="text-gray-600"> \u2014 {hint}</span>}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function MessageBubble({ msg, isStreaming }: { msg: ChatMessage; isStreaming?: boolean }) {
  if (msg.role === "system") {
    return (
      <div className="flex justify-center">
        <div className="max-w-[90%] rounded-lg px-4 py-2 bg-amber-500/10 border border-amber-500/30 text-amber-200 text-xs text-center">
          <p className="whitespace-pre-wrap">{msg.content}</p>
        </div>
      </div>
    );
  }

  // Use interleaved segments if available, otherwise fall back to flat content+toolCalls
  const segments: MsgSegment[] = (msg as any)._segments || [];
  const hasSegments = segments.length > 0;

  const renderContent = () => (
    <>
      {hasSegments ? (
        // Interleaved: render text and tool calls in arrival order
        <div className="space-y-2">
          {segments.map((seg, i) => {
            if (seg.type === "thinking" && seg.content) {
              return (
                <details key={i} className="group mb-2">
                  <summary className="text-xs text-purple-300 cursor-pointer list-none flex items-center gap-1.5 select-none">
                    <span className="text-[9px] text-gray-600 group-open:rotate-90 transition-transform">▶</span>
                    <span>思考过程</span>
                  </summary>
                  <div className="mt-1 text-xs text-gray-400 bg-surface-alt/70 rounded px-2.5 py-1.5 whitespace-pre-wrap break-words max-h-48 overflow-y-auto leading-relaxed border-l-2 border-purple-500/20">
                    {seg.content}
                  </div>
                </details>
              );
            }
            if (seg.type === "text") {
              return seg.content ? <p key={i} className="text-base whitespace-pre-wrap">{seg.content}</p> : null;
            }
            if (seg.type === "tool_call" && seg.tool) {
              const hint = formatToolInputHint(seg.tool.tool, seg.tool.input);
              return (
                <div key={i} className="text-xs text-gray-400 flex items-center gap-1.5 font-mono">
                  <span className="w-1.5 h-1.5 rounded-full bg-accent/50 shrink-0" />
                  <span className="text-gray-300">{seg.tool.tool}</span>
                  {hint && <span className="text-gray-600 truncate">— {hint}</span>}
                </div>
              );
            }
            return null;
          })}
        </div>
      ) : (
        // Fallback: flat rendering
        <>
          {(msg as any)._thinking && (
            <details className="group mb-2">
              <summary className="text-xs text-purple-300 cursor-pointer list-none flex items-center gap-1.5 select-none">
                <span className="text-[9px] text-gray-600 group-open:rotate-90 transition-transform">▶</span>
                <span>思考过程</span>
              </summary>
              <div className="mt-1 text-xs text-gray-400 bg-surface-alt/70 rounded px-2.5 py-1.5 whitespace-pre-wrap break-words max-h-48 overflow-y-auto leading-relaxed border-l-2 border-purple-500/20">
                {(msg as any)._thinking}
              </div>
            </details>
          )}
          {msg.content && (
            <p className="text-base whitespace-pre-wrap">{msg.content}</p>
          )}
          {msg.toolCalls && msg.toolCalls.length > 0 && (
            <div className={msg.content ? "mt-2" : ""}>
              <ToolCallsBlock toolCalls={msg.toolCalls} />
            </div>
          )}
        </>
      )}
    </>
  );

  return (
    <div className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
      <div
        className={`
          max-w-[95%] rounded-2xl px-4 py-3
          ${msg.role === "user"
            ? "bg-accent text-white max-w-[80%]"
            : "bg-surface-card border border-surface-border text-gray-200"
          }
        `}
      >
        {msg.images && msg.images.length > 0 && (
          <div className="flex gap-1.5 flex-wrap mb-2">
            {msg.images.map((url, i) => (
              <img key={i} src={url} className="max-h-48 max-w-[200px] rounded-lg object-cover" alt="" />
            ))}
          </div>
        )}

        {renderContent()}

        {msg.role === "assistant" && isStreaming && !hasSegments && !msg.content && (!msg.toolCalls || msg.toolCalls.length === 0) && (
          <div className="flex gap-1">
            <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
            <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
            <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
          </div>
        )}
      </div>
    </div>
  );
}

function ChatPanel({ agentId }: { agentId: string | null }) {
  const [agentInfo, setAgentInfo] = useState<AgentInfo | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streamDraft, setStreamDraft] = useState<StreamDraft | null>(null);
  // Mirror streamDraft synchronously so event handlers (which close over an
  // older `streamDraft`) can read the latest value.
  const streamDraftRef = useRef<StreamDraft | null>(null);
  // RAF throttle for streamDraft updates — without this, 72+ delta events
  // arriving in ~280ms each trigger a separate setStreamDraft → React re-render,
  // causing bursty "结巴" (stutter) display. RAF coalesces them to ≤60fps.
  const rafPendingRef = useRef(false);
  const updateStreamDraft = useCallback(
    (updater: StreamDraft | null | ((prev: StreamDraft | null) => StreamDraft | null)) => {
      const next = typeof updater === "function" ? updater(streamDraftRef.current) : updater;
      streamDraftRef.current = next;
      // Throttle React state update via RAF — the ref is updated synchronously
      // so event handlers always see the latest value, but React only re-renders
      // once per animation frame (≤16ms), coalescing rapid delta bursts.
      if (!rafPendingRef.current) {
        rafPendingRef.current = true;
        requestAnimationFrame(() => {
          rafPendingRef.current = false;
          setStreamDraft(streamDraftRef.current);
        });
      }
    },
    []
  );
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const abortControllerRef = useRef<AbortController | null>(null);
  const responseTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeAgentIdRef = useRef<string | null>(agentId);
  activeAgentIdRef.current = agentId;
  const refreshOrgTree = useAppStore((s) => s.refreshOrgTree);
  const userName = useAppStore((s) => s.userName);
  const processingAgents = useAppStore((s) => s.processingAgents);
  const updateProcessingAgent = useAppStore((s) => s.updateProcessingAgent);
  const orgTreeVersion = useAppStore((s) => s.orgTreeVersion);
  const socketReconnectVersion = useAppStore((s) => s.socketReconnectVersion);
  const prevReconnectVersion = useRef(0);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [showApprovalDialog, setShowApprovalDialog] = useState(false);
  const [pendingApprovalTool, setPendingApprovalTool] = useState<string | null>(null);
  const [teamCommsExpanded, setTeamCommsExpanded] = useState(false);
  const [expandedMessageId, setExpandedMessageId] = useState<string | null>(null);
  const [agentInfoCache, setAgentInfoCache] = useState<Record<string, { name: string; position?: string; role?: string }>>({});
  const [queuedCount, setQueuedCount] = useState(0);
  const pendingQueueRef = useRef<string[]>([]);
  const autoSendRef = useRef(false);
  const handleSendRef = useRef<() => void>(() => {});
  const sendingLockRef = useRef(false);  // BUG-022 修复：防止快速双击导致重复发送
  const [retryInfo, setRetryInfo] = useState<{ attempt: number; maxRetries: number; reason: string } | null>(null);
  const [images, setImages] = useState<string[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);


  const handleMessagesScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distFromBottom <= 72;
  }, []);

  const loadMessagesFromDb = useCallback(async (loadForAgentId: string): Promise<boolean> => {
    try {
      const dbMessages = await getChatMessages(loadForAgentId);
      if (activeAgentIdRef.current !== loadForAgentId) return false;
      const converted = mapDbToChatMessages(dbMessages);
      setMessages(converted);
      useAppStore.getState().setChatMessages(loadForAgentId, converted);
      const unreadIds = converted
        .filter((m) => !m.isRead && (m.isBackground || m.role === "team"))
        .map((m) => m.id);
      if (unreadIds.length > 0) {
        markMessagesRead(unreadIds, loadForAgentId).catch(() => {});
        refreshOrgTree();
      }
      return true;
    } catch (err) {
      if (activeAgentIdRef.current !== loadForAgentId) return false;
      console.warn("Failed to load chat messages from DB:", err);
      return false;
    }
  }, [refreshOrgTree]);

  const handleDelete = useCallback(async () => {
    if (!agentId) return;
    if (!confirmingDelete) {
      setConfirmingDelete(true);
      return;
    }
    try {
      await deleteAgent(agentId);
      setConfirmingDelete(false);
      setAgentInfo(null);
      setMessages([]);
      updateStreamDraft(null);
      refreshOrgTree();
      useAppStore.getState().setSelectedAgent(null);
    } catch (err: any) {
      useAppStore.getState().showToast(err.message || "Failed to delete agent", "error");
      setConfirmingDelete(false);
    }
  }, [agentId, confirmingDelete, refreshOrgTree]);

  useEffect(() => {
    if (!agentId) {
      setAgentInfo(null);
      setMessages([]);
      updateStreamDraft(null);
      setConfirmingDelete(false);
      setTeamCommsExpanded(false);
      setExpandedMessageId(null);
      pendingQueueRef.current = [];
      setQueuedCount(0);
      return;
    }

    let cancelled = false;
    const loadForAgentId = agentId;
    stickToBottomRef.current = true;
    setIsStreaming(false);
    updateStreamDraft(null);

    // Use cached messages if available — switching back to a previously-viewed
    // agent renders instantly without waiting for the DB round-trip. The
    // background `loadMessagesFromDb` call below refreshes the cache.
    const cached = useAppStore.getState().chatSessions[loadForAgentId];
    if (cached && cached.length > 0) {
      setMessages(cached);
    } else {
      setMessages([]);
    }

    async function fetchAgent() {
      try {
        const raw = await getAgent(loadForAgentId);
        // Backend wraps response as %{agent: serialize_agent(a)}
        const data = (raw && typeof raw === "object" && "agent" in raw && raw.agent) ? raw.agent : raw;
        if (cancelled || activeAgentIdRef.current !== loadForAgentId) return;
        if (data && typeof data === "object" && data.id) {
          setAgentInfo(data);
        } else {
          setAgentInfo({ id: loadForAgentId, name: "Agent", role: "module_dev", status: "idle" });
        }
      } catch (err) {
        if (cancelled || activeAgentIdRef.current !== loadForAgentId) return;
        console.error("Failed to fetch agent:", err);
        setAgentInfo({ id: loadForAgentId, name: "Agent", role: "module_dev", status: "idle" });
      }
    }
    fetchAgent();
    loadMessagesFromDb(loadForAgentId);

    // pendingInitialMessage is handled by the dedicated effect below
    return () => {
      // Only prevent stale fetchAgent/loadMessagesFromDb results from
      // applying. Do NOT abort the stream or clear the response timeout here —
      // this effect re-runs when loadMessagesFromDb or orgTreeVersion changes
      // (e.g. after a lobby:status push), and aborting would kill the WebSocket
      // stream mid-response, causing "stops after one sentence" bug.
      // Stream/timeout/channel cleanup is handled by the [agentId] effect below.
      cancelled = true;
    };
  }, [agentId, loadMessagesFromDb, orgTreeVersion]);

  // Dedicated effect: watch for pendingInitialMessage changes.
  // This handles the case where ChatPanel is already mounted when
  // NewProjectDialog sets the pending message (e.g. CEO was already selected).
  const pendingInitialMessage = useAppStore((s) => s.pendingInitialMessage);
  useEffect(() => {
    if (!pendingInitialMessage || !agentId) return;
    if (pendingInitialMessage.agentId !== agentId) return;

    const message = pendingInitialMessage.message;
    const sendingForAgentId = agentId;
    // Consume the pending message
    useAppStore.getState().setPendingInitialMessage(null);

    // Send directly - onboarding messages must not show the queued-behind-busy UI.
    // Do NOT return cleanup that cancels the send timer: clearing pendingInitialMessage
    // re-runs this effect and the previous cleanup would cancel the send.
    //
    // BUG-032: 移除脆弱的 100ms setTimeout。streamChat() 内部已正确处理
    // "joining" 状态（等待 join 完成后 push）。后端 event_bus 的 replay
    // 机制也保证即使事件先于 join 产生，也会在 join 后重放。
    void joinAgentChannel(sendingForAgentId).finally(() => {
      if (activeAgentIdRef.current !== sendingForAgentId) return;
      autoSendRef.current = true;
      pendingQueueRef.current = [message];
      setQueuedCount(0);
      handleSendRef.current();
    });
  }, [pendingInitialMessage, agentId]);

  // Pre-join the agent channel when the chat panel mounts.
  useEffect(() => {
    if (!agentId) return;
    joinAgentChannel(agentId).catch(() => {});
  }, [agentId]);

  // Manage WebSocket channel + stream lifecycle — abort stream, clear timeout,
  // and leave channel ONLY when agentId actually changes, not when
  // loadMessagesFromDb or orgTreeVersion triggers a re-run of the main mount
  // effect. This prevents the "stops after one sentence" bug where the stream
  // gets killed mid-response because lobby:status or org tree refresh causes
  // the mount effect to re-run.
  useEffect(() => {
    return () => {
      if (agentId) {
        abortControllerRef.current?.abort();
        if (responseTimeoutRef.current) {
          clearTimeout(responseTimeoutRef.current);
          responseTimeoutRef.current = null;
        }
        leaveAgentChannel(agentId);
      }
    };
  }, [agentId]);

  // Reset stale streaming state when WebSocket reconnects.
  // If the socket drops mid-stream, no done/error event will arrive, leaving
  // isStreaming stuck. On reconnect, lobby:status fires an init snapshot —
  // if the agent is no longer processing, we force-reset the UI.
  // BUG-033: Don't clear streamDraft entirely — persist it so the streamed
  // content doesn't vanish. The DB load on next user action will reconcile it.
  useEffect(() => {
    if (socketReconnectVersion === prevReconnectVersion.current) return;
    prevReconnectVersion.current = socketReconnectVersion;
    // Only reset if this is not the initial mount
    if (socketReconnectVersion > 1) {
      const stillProcessing = agentId ? processingAgents.includes(agentId) : false;
      if (!stillProcessing && isStreaming) {
        // Persist the streamed content rather than clearing it
        updateStreamDraft((prev) => prev ? { ...prev, persisted: true } as any : prev);
        setIsStreaming(false);
        setRetryInfo(null);
        if (responseTimeoutRef.current) {
          clearTimeout(responseTimeoutRef.current);
          responseTimeoutRef.current = null;
        }
      }
    }
  }, [socketReconnectVersion, agentId, processingAgents, isStreaming]);

  const isAgentProcessing = agentId ? processingAgents.includes(agentId) : false;

  const hasUnansweredUser = useMemo(() => {
    const fg = messages.filter((m) => !m.isBackground && (m.role === "user" || m.role === "assistant"));
    const last = fg[fg.length - 1];
    const streaming = messages.some((m) => m.isStreaming && m.role === "assistant");
    return last?.role === "user" && !streaming;
  }, [messages]);


  const displayMessages = useMemo(() => {
    let merged = messages;
    // BUG-033: Also merge streamDraft when it has persisted content (DB load
    // failed after done event). This prevents streamed text from vanishing
    // when the HTTP fetch for DB messages fails.
    const hasPersistedDraft = streamDraft && (streamDraft as any).persisted;
    if ((isStreaming && streamDraft) || hasPersistedDraft) {
      merged = messages.map((m) => {
        const isTarget = m.id === streamDraft!.assistantId;
        if (!isTarget && !hasPersistedDraft) {
          // Any other message claiming to be streaming is stale (cached from a
          // previous session) — strip the flag so the "..." bubble goes away.
          return m.isStreaming ? { ...m, isStreaming: false } : m;
        }
        if (!isTarget && hasPersistedDraft) {
          // During persisted draft, show all messages normally except the target
          return m;
        }
        const textParts = streamDraft!.segments.filter(s => s.type === "text").map(s => s.content || "");
        const thinkingParts = streamDraft!.segments.filter(s => s.type === "thinking").map(s => s.content || "");
        const newTools = streamDraft!.segments.filter(s => s.type === "tool_call").map(s => s.tool!);
        return {
          ...m,
          content: textParts.join(""),
          // Use streamDraft tool calls as authoritative during streaming.
          // The DB message may already contain tool calls saved by the streamer's
          // intermediate update, so merging would duplicate them.
          toolCalls: newTools.length > 0 ? newTools : (m.toolCalls || []),
          _segments: streamDraft!.segments,
          _thinking: thinkingParts.join(""),
          isStreaming: hasPersistedDraft ? false : true,
        };
      });
    } else {
      // Not currently streaming — no message should carry the streaming flag.
      merged = merged.map((m) => (m.isStreaming ? { ...m, isStreaming: false } : m));
    }
    // Drop auto-injected coordinator context blocks (e.g. "## Messages\n- From: <uuid>...\n
    // [ESCALATION]...\n\n---\nProcess the above..."). The Elixir backend saves these as
    // background user messages; they are LLM-facing context, not part of the user-facing
    // conversation. We also drop background user messages with markdown-style content
    // even when isBackground is not set, as a safety net for legacy rows.
    merged = merged.filter((m) => !isInjectedContext(m));
    // Only show foreground messages (user + assistant, non-background, non-team).
    // Agent-to-agent messages (is_background=true or role="team") belong in the
    // "团队沟通" panel, not the main ChatPanel.
    // Also filter out empty assistant messages (no content, no tool calls) that
    // may be leftover placeholders from interrupted streams.
    const foreground = merged.filter((m) => {
      if (m.isBackground || (m.role !== "user" && m.role !== "assistant")) return false;
      if (m.role === "assistant" && !m.isStreaming) {
        const hasContent = m.content && m.content.trim().length > 0;
        const hasToolCalls = m.toolCalls && m.toolCalls.length > 0;
        if (!hasContent && !hasToolCalls) return false;
      }
      return true;
    });
    let trailingUserCount = 0;
    for (let i = foreground.length - 1; i >= 0; i--) {
      if (foreground[i].role === "user") trailingUserCount++;
      else break;
    }
    const hasStreamingPlaceholder = foreground.some((m) => m.isStreaming && m.role === "assistant");
    // BUG-003 修复：加 5s 时间阈值，避免 user 发消息瞬间 isAgentProcessing
    // 还没来得及更新就误报"上次对话未收到回复"。
    const ORPHAN_WARN_DELAY_MS = 5000;
    const now = Date.now();
    const lastUser = foreground[foreground.length - 1];
    const userMsgAge = lastUser?.role === "user" && lastUser?.timestamp
      ? now - lastUser.timestamp
      : Infinity;
    if (trailingUserCount >= 1 && !isAgentProcessing && !hasStreamingPlaceholder && !isStreaming
        && userMsgAge > ORPHAN_WARN_DELAY_MS) {
      if (lastUser?.role === "user") {
        const warn = trailingUserCount >= 2
          ? "你已发送多条消息但 Agent 尚未回复。请等待当前任务完成，或检查网络/API 配置后重试。"
          : "⚠️ 上次对话未收到回复。Agent 可能遇到了异常，请重新发送消息。";
        return [...foreground, {
          id: `${lastUser.id}-orphan`,
          role: "system" as const,
          content: warn,
          timestamp: lastUser.timestamp + 1,
        }];
      }
    }
    return foreground;
  }, [messages, isStreaming, streamDraft, isAgentProcessing]);

  useEffect(() => {
    if (!agentId) return;
    const pollForAgentId = agentId;
    const timer = setInterval(() => loadMessagesFromDb(pollForAgentId), 5000);
    return () => clearInterval(timer);
  }, [agentId, loadMessagesFromDb]);

  useEffect(() => {
    if (!stickToBottomRef.current) return;
    messagesEndRef.current?.scrollIntoView({ behavior: isStreaming ? "auto" : "smooth" });
  }, [displayMessages, isStreaming]);

  // Mirror messages into the store cache so a re-visit to this agent renders
  // instantly. Throttled via RAF so streaming updates don't thrash the store.
  // - Strip the per-message `isStreaming` flag (it is a local placeholder).
  // - Drop transient empty assistant placeholders (`content === ""` with no
  //   tool calls and no segments) — they survive remounts and would otherwise
  //   render as a dark empty bubble, making the chat look black on revisit.
  useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    const id = requestAnimationFrame(() => {
      if (cancelled) return;
      const cached = useAppStore.getState().chatSessions[agentId];
      const sanitized = messages
        .map((m) => (m.isStreaming ? { ...m, isStreaming: false } : m))
        .filter((m) => !(
          m.role === "assistant" &&
          !m.isStreaming &&
          !m.content &&
          (!m.toolCalls || m.toolCalls.length === 0)
        ));
      if (cached && cached.length === sanitized.length && cached.every((c, i) => c === sanitized[i])) return;
      useAppStore.getState().setChatMessages(agentId, sanitized);
    });
    return () => {
      cancelled = true;
      cancelAnimationFrame(id);
    };
  }, [agentId, messages]);

  const { directMessages, teamMessages } = useMemo(() => {
    // Team messages must be derived from the raw `messages` state, NOT from
    // `displayMessages` — the latter only keeps foreground (user/assistant)
    // messages and strips out role="team" entries, so filtering it for team
    // messages would always yield an empty list.
    const team = messages.filter((m) => isTeamChannelMessage(m));
    const direct = displayMessages.filter((m) => !isTeamChannelMessage(m));
    return { directMessages: direct, teamMessages: team };
  }, [messages, displayMessages]);

  const counterpartIds = useMemo(() => {
    const ids = new Set<string>();
    for (const msg of teamMessages) {
      if (msg.teamFromAgentId) ids.add(msg.teamFromAgentId);
      if (msg.teamToAgentId) ids.add(msg.teamToAgentId);
      const targetId = getDirectedAgentId(msg, agentInfo?.parentId);
      if (targetId) ids.add(targetId);
    }
    return ids;
  }, [teamMessages, agentInfo]);

  const hasTeamComms = teamMessages.length > 0;

  useEffect(() => {
    // Pre-populate cache with current agent's info so self-referencing
    // team messages (teamFromAgentId === agentId) resolve instantly.
    if (agentInfo && agentId) {
      setAgentInfoCache((prev) => {
        if (prev[agentId]) return prev;
        return { ...prev, [agentId]: { name: agentInfo.name, position: agentInfo.position, role: agentInfo.role } };
      });
    }
    // Use functional update to read latest cache state, avoiding stale closures
    setAgentInfoCache((currentCache) => {
      const idsToFetch: string[] = [];
      for (const id of counterpartIds) {
        if (!currentCache[id]) idsToFetch.push(id);
      }
      if (idsToFetch.length === 0) return currentCache;
      for (const id of idsToFetch) {
        getAgent(id).then((raw) => {
          const data = (raw && typeof raw === "object" && "agent" in raw && raw.agent) ? raw.agent : raw;
          if (data?.name) {
            setAgentInfoCache((prev) => ({ ...prev, [id]: { name: data.name, position: data.position, role: data.role } }));
          }
        }).catch(() => {});
      }
      return currentCache;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [counterpartIds, agentInfo]);

  const addImages = useCallback((files: FileList | File[]) => {
    const readers: Promise<string>[] = [];
    for (const file of Array.from(files)) {
      if (!file.type.startsWith("image/")) continue;
      readers.push(new Promise<string>((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as string);
        reader.readAsDataURL(file);
      }));
    }
    Promise.all(readers).then((urls) => {
      setImages((prev) => [...prev, ...urls].slice(0, 5));
    });
  }, []);

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const imageFiles: File[] = [];
    for (const item of Array.from(items)) {
      if (item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) imageFiles.push(file);
      }
    }
    if (imageFiles.length > 0) {
      e.preventDefault();
      addImages(imageFiles);
    }
  }, [addImages]);

  const handleFileInput = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) addImages(e.target.files);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, [addImages]);

  const removeImage = useCallback((index: number) => {
    setImages((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleSend = useCallback(() => {
    if (!agentId) return;

    // BUG-022 修复：防止快速双击/重连导致重复发送。
    // autoSend 由 finishTurn 触发，此时当前 stream 已结束，锁已释放。
    if (!autoSendRef.current && sendingLockRef.current) {
      if (input.trim()) {
        pendingQueueRef.current.push(input.trim());
        setInput("");
        setQueuedCount(pendingQueueRef.current.length);
      }
      return;
    }

    let messageText: string;
    if (autoSendRef.current) {
      autoSendRef.current = false;
      messageText = pendingQueueRef.current.shift() || "";
      setQueuedCount(pendingQueueRef.current.length);
    } else {
      if (!input.trim()) return;
      messageText = input.trim();
      setInput("");
      if (isStreaming || isAgentProcessing) {
        pendingQueueRef.current.push(messageText);
        setQueuedCount(pendingQueueRef.current.length);
        return;
      }
    }

    if (!messageText) return;

    // 上锁，直到本次 stream 最终结束
    sendingLockRef.current = true;

    const sendingImages = images;
    setImages([]);

    const sendingForAgentId = agentId;
    const isActiveSession = () => activeAgentIdRef.current === sendingForAgentId;
    const releaseLockAndFinish = () => {
      sendingLockRef.current = false;
      if (pendingQueueRef.current.length > 0) {
        autoSendRef.current = true;
        setTimeout(() => handleSend(), 300);
      }
    };

    stickToBottomRef.current = true;
    setIsStreaming(true);
    // Optimistically set processing state so the status indicator updates
    // immediately, without waiting for the lobby:status WebSocket event.
    updateProcessingAgent(sendingForAgentId, true);
    updateStreamDraft(null);
    setRetryInfo(null);
    if (responseTimeoutRef.current) clearTimeout(responseTimeoutRef.current);
    responseTimeoutRef.current = setTimeout(() => {
      if (!isActiveSession()) return;
      setIsStreaming(false);
      updateStreamDraft(null);
      updateProcessingAgent(sendingForAgentId, false);
      loadMessagesFromDb(sendingForAgentId);
      releaseLockAndFinish();
    }, 300_000);
    const allToolsUsed = new Set<string>();
    let _dbgTextCount = 0;
    let _dbgFirstText = 0;
    const controller = new AbortController();
    abortControllerRef.current = controller;
    // BUG-FIX 优化：立即把 user 消息以 placeholder 形式显示，
    // 避免 message_id 未到达前消息"消失"（最长可等 joinAgentChannel + 后端 save + WS 推送）。
    // 当 message_id 事件到达时，会去重并替换为真实 ID。
    const optimisticUserId = `pending-user-${sendingForAgentId}-${Date.now()}`;
    setMessages((prev) => {
      if (prev.some((m) => m.id === optimisticUserId)) return prev;
      return [...prev, {
        id: optimisticUserId, role: "user" as const, content: messageText,
        timestamp: Date.now(), isBackground: false, isRead: true,
      }];
    });

    streamChat(sendingForAgentId, messageText, sendingImages, (event) => {
      if (!isActiveSession()) return;
      if (event.type === "message_id") {
        try {
          const parsed = JSON.parse(event.data);
          if (parsed.role === "user" && parsed.id) {
            // 用真实 ID 替换占位符
            setMessages((prev) => {
              const without = prev.filter((m) => m.id !== optimisticUserId);
              if (without.some((m) => m.id === parsed.id)) return without;
              return [...without, {
                id: parsed.id, role: "user" as const, content: messageText,
                timestamp: Date.now(), isBackground: false, isRead: true,
              }];
            });
          }
          if (parsed.role === "assistant" && parsed.id) {
            // Optimistic assistant placeholder — ensures displayMessages merge has a target
            // before loadMessagesFromDb resolves, eliminating the async race condition
            setMessages((prev) => {
              if (prev.some((m) => m.id === parsed.id)) return prev;
              return [...prev, {
                id: parsed.id, role: "assistant" as const, content: "",
                timestamp: Date.now(), isBackground: false, isRead: true, isStreaming: true,
              }];
            });
            updateStreamDraft({ assistantId: parsed.id, segments: [] });
            console.log(`[SSE] streamDraft initialized: assistantId=${parsed.id}`);
          }
        } catch {}
        // Load full messages from DB — will replace optimistic placeholders when resolved
        loadMessagesFromDb(sendingForAgentId);
        return;
      }

      // Ensure streamDraft is initialized before we get the first text chunk.
      // The backend may not push an assistant message_id (it never does in our
      // current pipeline), so we initialize lazily on the first text event.
      if ((event.type === "text" || event.type === "text_delta") && !streamDraftRef.current) {
        const placeholderId = `draft-${sendingForAgentId}-${Date.now()}`;
        setMessages((prev) => {
          if (prev.some((m) => m.id === placeholderId)) return prev;
          return [...prev, {
            id: placeholderId, role: "assistant" as const, content: "",
            timestamp: Date.now(), isBackground: false, isRead: true, isStreaming: true,
          }];
        });
        updateStreamDraft({ assistantId: placeholderId, segments: [] });
        console.log(`[SSE] streamDraft lazy-initialized: assistantId=${placeholderId}`);
      } else if (event.type === "text" || event.type === "text_delta") {
        _dbgTextCount++;
        if (_dbgTextCount === 1) _dbgFirstText = performance.now();
        if (_dbgTextCount <= 3 || _dbgTextCount % 20 === 0) {
          console.log(`[SSE] text #${_dbgTextCount}: ${event.data.length}chars, t=${(performance.now() - _dbgFirstText).toFixed(0)}ms`);
        }
        // Lazy init: if no message_id has arrived yet, initialize streamDraft
        // with a placeholder id (backend may not push one for the assistant).
        if (!streamDraftRef.current) {
          const placeholderId = `draft-${sendingForAgentId}-${Date.now()}`;
          setMessages((prev) => {
            if (prev.some((m) => m.id === placeholderId)) return prev;
            return [...prev, {
              id: placeholderId, role: "assistant" as const, content: "",
              timestamp: Date.now(), isBackground: false, isRead: true, isStreaming: true,
            }];
          });
          updateStreamDraft({ assistantId: placeholderId, segments: [{ type: "text", content: event.data }] });
          console.log(`[SSE] streamDraft lazy-initialized: assistantId=${placeholderId}`);
          return;
        }

        updateStreamDraft((prev) => {
          if (!prev) return prev;
          const last = prev.segments[prev.segments.length - 1];
          if (last && last.type === "text") {
            // Use mergeDeltaContent to handle APIs that send full accumulated
            // text instead of incremental deltas (causes "结巴" duplication
            // if we naively append).
            return { ...prev, segments: [...prev.segments.slice(0, -1), { ...last, content: mergeDeltaContent(last.content || "", event.data) }] };
          }
          return { ...prev, segments: [...prev.segments, { type: "text", content: event.data }] };
        });
      } else if (event.type === "thinking_delta") {
        // Reasoning model thinking content — display in collapsible block
        if (!streamDraftRef.current) {
          const placeholderId = `draft-${sendingForAgentId}-${Date.now()}`;
          setMessages((prev) => {
            if (prev.some((m) => m.id === placeholderId)) return prev;
            return [...prev, {
              id: placeholderId, role: "assistant" as const, content: "",
              timestamp: Date.now(), isBackground: false, isRead: true, isStreaming: true,
            }];
          });
          updateStreamDraft({ assistantId: placeholderId, segments: [{ type: "thinking", content: event.data }] });
          return;
        }
        updateStreamDraft((prev) => {
          if (!prev) return prev;
          const last = prev.segments[prev.segments.length - 1];
          if (last && last.type === "thinking") {
            // Use mergeDeltaContent for thinking deltas too — same reasoning
            // as text_delta: some APIs send full accumulated text per chunk.
            return { ...prev, segments: [...prev.segments.slice(0, -1), { ...last, content: mergeDeltaContent(last.content || "", event.data) }] };
          }
          return { ...prev, segments: [...prev.segments, { type: "thinking", content: event.data }] };
        });
      } else if (event.type === "tool_use") {
        try {
          const toolData = JSON.parse(event.data);
          const toolCall: ToolCall = {
            tool: (toolData.tool || "").replace(/^hiveweave__/, ""),
            input: toolData.input || {},
          };
          allToolsUsed.add(toolCall.tool);
          updateStreamDraft((prev) => prev ? { ...prev, segments: [...prev.segments, { type: "tool_call", tool: toolCall }] } : prev);
        } catch {}
      } else if (event.type === "approval_request") {
        try {
          const data = JSON.parse(event.data);
          setPendingApprovalTool(data.tool || "unknown tool");
          setShowApprovalDialog(true);
        } catch {
          setShowApprovalDialog(true);
        }
      } else if (event.type === "retry") {
        try {
          const data = JSON.parse(event.data);
          setRetryInfo({
            attempt: data.attempt || 1,
            maxRetries: data.maxRetries || 3,
            reason: data.reason || "API error",
          });
          if (responseTimeoutRef.current) clearTimeout(responseTimeoutRef.current);
          const extraMs = (data.delayMs || 5000) + 10000;
          responseTimeoutRef.current = setTimeout(() => {
            if (!isActiveSession()) return;
            setIsStreaming(false);
            updateStreamDraft(null);
            updateProcessingAgent(sendingForAgentId, false);
            setRetryInfo(null);
            loadMessagesFromDb(sendingForAgentId);
            releaseLockAndFinish();
          }, extraMs);
        } catch {}
      } else if (event.type === "queued_message") {
        loadMessagesFromDb(sendingForAgentId);
      } else if (event.type === "done") {
        console.log(`[SSE] done — total text events: ${_dbgTextCount}, elapsed: ${_dbgFirstText ? (performance.now() - _dbgFirstText).toFixed(0) : 'N/A'}ms`);
        if (responseTimeoutRef.current) { clearTimeout(responseTimeoutRef.current); responseTimeoutRef.current = null; }
        setPendingApprovalTool(null);
        setRetryInfo(null);
        // Directly update processing state — don't rely solely on lobby:status
        // WebSocket channel, which may have missed the status_change event.
        if (sendingForAgentId) updateProcessingAgent(sendingForAgentId, false);
        const ORG_TOOLS = new Set(["create_agent", "transfer_agent", "dismiss_agent", "create_from_template", "hire_agent"]);
        if ([...allToolsUsed].some((x) => ORG_TOOLS.has(x))) refreshOrgTree();
        // BUG-033: Load final messages from DB, but only clear streamDraft
        // if the DB load succeeds. If it fails (e.g. server restart), the
        // streamed content stays visible instead of vanishing.
        loadMessagesFromDb(sendingForAgentId).then((ok) => {
          if (ok) {
            // DB loaded successfully — clear the draft and show persisted messages
            updateStreamDraft(null);
          } else {
            // DB load failed — keep streamDraft but mark content as final
            // (not streaming), so displayMessages shows it as a regular message
            updateStreamDraft((prev) => prev ? { ...prev, persisted: true } as any : prev);
            // Don't clear streamDraft — the user should still see the streamed text
          }
          setIsStreaming(false);
        });
        releaseLockAndFinish();
      } else if (event.type === "busy") {
        // Agent is processing a previous message — restore input so user doesn't lose their text
        if (responseTimeoutRef.current) { clearTimeout(responseTimeoutRef.current); responseTimeoutRef.current = null; }
        // BUG-032: 清除 optimistic processing 状态，agent 已拒绝此消息
        if (sendingForAgentId) updateProcessingAgent(sendingForAgentId, false);
        setInput(messageText);
        updateStreamDraft(null);
        setIsStreaming(false);
        setRetryInfo(null);
        // Drain queue since we can't send
        pendingQueueRef.current = [];
        setQueuedCount(0);
        autoSendRef.current = false;
        sendingLockRef.current = false;
      } else if (event.type === "error") {
        // BUG-033: 只在 DB 加载成功时清除 streamDraft。如果 HTTP 请求失败
        // (服务重启等)，保留已流式传输的内容，避免"出现一瞬间就没了"。
        if (responseTimeoutRef.current) { clearTimeout(responseTimeoutRef.current); responseTimeoutRef.current = null; }
        setRetryInfo(null);
        if (sendingForAgentId) updateProcessingAgent(sendingForAgentId, false);
        loadMessagesFromDb(sendingForAgentId).then((ok) => {
          if (ok) {
            updateStreamDraft(null);
          } else {
            updateStreamDraft((prev) => prev ? { ...prev, persisted: true } as any : prev);
          }
          setIsStreaming(false);
        });
        releaseLockAndFinish();
      }
    });
  }, [agentId, input, isStreaming, isAgentProcessing, hasUnansweredUser, refreshOrgTree, loadMessagesFromDb]);

  // Keep handleSendRef in sync so setTimeout/effect can always call the latest version
  handleSendRef.current = handleSend;

  // Drain queued messages when agent becomes idle (e.g. after background processing)
  useEffect(() => {
    if (!agentId || isStreaming || isAgentProcessing) return;
    if (pendingQueueRef.current.length === 0) return;
    autoSendRef.current = true;
    handleSend();
  }, [agentId, isStreaming, isAgentProcessing, handleSend]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleStop = useCallback(() => {
    abortControllerRef.current?.abort();
    setIsStreaming(false);
    updateStreamDraft(null);
    setRetryInfo(null);
    if (responseTimeoutRef.current) clearTimeout(responseTimeoutRef.current);
    if (agentId) updateProcessingAgent(agentId, false);
    if (pendingQueueRef.current.length > 0) {
      pendingQueueRef.current = [];
      setQueuedCount(0);
    }
  }, [agentId]);

  if (!agentId) {
    return (
      <div className="h-full flex items-center justify-center bg-surface">
        <div className="text-center">
          <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-surface-card border border-surface-border flex items-center justify-center">
            <svg className="w-8 h-8 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
            </svg>
          </div>
          <p className="text-gray-500 text-sm">选择一个 Agent 开始对话</p>
        </div>
      </div>
    );
  }

  const statusInfo = statusLabels[agentInfo?.status || "idle"] || { text: agentInfo?.status || "Unknown", color: "text-gray-400" };
  const runtimeStatusInfo = agentInfo?.status === "active"
    ? isAgentProcessing ? { text: "工作中", color: "text-emerald-400" } : { text: "空闲", color: "text-gray-400" }
    : statusInfo;
  const resolveAgentInfo = (id: string) => {
    // Check cache first
    if (agentInfoCache[id]) return agentInfoCache[id];
    // Fallback to current agent's info if the ID matches (we're viewing their panel)
    if (agentInfo && id === agentId) return { name: agentInfo.name, position: agentInfo.position, role: agentInfo.role };
    // System messages
    if (id === "system") return { name: "系统通知" };
    return { name: "加载中…" };
  };

  // Role colors matching OrgTree
  const roleDots: Record<string, string> = {
    ceo: "bg-amber-400", hr: "bg-rose-400", architect: "bg-purple-400",
    manager: "bg-blue-400", pm: "bg-blue-400",
    developer: "bg-green-400", module_dev: "bg-green-400",
    test_engineer: "bg-yellow-400", code_reviewer: "bg-indigo-400",
    security_auditor: "bg-red-400", web_perf_auditor: "bg-cyan-400",
    qa: "bg-yellow-400", devops: "bg-cyan-400",
  };

  return (
    <div className="h-full flex flex-col bg-surface">
      {agentInfo && (
        <div className="px-6 py-3 border-b border-surface-border bg-surface-card shrink-0">
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${
              isAgentProcessing ? "bg-emerald-400 animate-pulse"
              : agentInfo.status === "idle" || agentInfo.status === "inactive" ? "bg-gray-500"
              : agentInfo.status === "promoted" ? "bg-blue-400"
              : agentInfo.status === "receiving" ? "bg-amber-400 animate-pulse"
              : agentInfo.status === "merging" ? "bg-purple-400 animate-pulse"
              : agentInfo.status === "dissolving" || agentInfo.status === "archived" ? "bg-red-600"
              : "bg-gray-400"
            }`} />
            <span className="text-sm font-medium text-gray-200">{agentInfo.name}</span>
            <span className="text-xs text-gray-500">·</span>
            <span className="text-xs text-gray-400">{roleLabels[agentInfo.role] || agentInfo.role}</span>
            <span className="text-xs text-gray-500">·</span>
            <span className={`text-xs ${runtimeStatusInfo.color}`}>{runtimeStatusInfo.text}</span>
          </div>
        </div>
      )}

      <TodoBar agentId={agentId} />

      <div ref={scrollContainerRef} onScroll={handleMessagesScroll} className="flex-1 min-h-0 overflow-y-auto px-4 py-4 space-y-4">
        {directMessages.length === 0 && !hasTeamComms && (
          <div className="text-center text-gray-500 text-sm mt-12">发送消息开始对话</div>
        )}
        {directMessages.map((msg) => (
          <MessageBubble key={msg.id} msg={msg} isStreaming={!!msg.isStreaming || (isStreaming && streamDraft?.assistantId === msg.id)} />
        ))}
        {pendingApprovalTool && isStreaming && (
          <div className="flex justify-start">
            <div className="max-w-[80%] rounded-2xl px-4 py-3 bg-amber-500/10 border border-amber-500/30">
              <div className="flex items-center gap-2">
                <span className="text-sm text-amber-300">等待审批: {pendingApprovalTool.replace(/^hiveweave__/, "").replace(/_/g, " ")}</span>
              </div>
            </div>
          </div>
        )}
        {retryInfo && isStreaming && (
          <div className="flex justify-start">
            <div className="max-w-[80%] rounded-2xl px-4 py-3 bg-orange-500/10 border border-orange-500/30">
              <div className="flex items-center gap-2">
                <svg className="w-4 h-4 text-orange-400 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                <span className="text-sm text-orange-300">
                  重试中... {retryInfo.attempt}/{retryInfo.maxRetries}
                </span>
                <span className="text-xs text-orange-400/70">{retryInfo.reason}</span>
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {hasTeamComms && (
        <div className="shrink-0 border-t border-surface-border bg-surface-card overflow-hidden">
          <button onClick={() => { setTeamCommsExpanded(!teamCommsExpanded); if (teamCommsExpanded) setExpandedMessageId(null); }} className="w-full px-4 py-2 flex items-center justify-between hover:bg-surface-border/30 transition-colors">
            <span className="text-sm font-medium text-gray-300">团队沟通 ({teamMessages.length})</span>
            <span className="text-xs text-gray-500">{teamCommsExpanded ? "收起" : "展开"}</span>
          </button>
          {teamCommsExpanded && (
            <div className="max-h-[35vh] overflow-y-auto overflow-x-hidden py-1">
              {[...teamMessages].sort((a, b) => b.timestamp - a.timestamp).map((msg) => {
                // Determine direction:
                // 1. Real user message (role=user, !isBackground) → incoming (operator → agent)
                // 2. Background user message (role=user, isBackground) → incoming (other agent triggered current)
                // 3. Background assistant message (role=assistant, isBackground) → outgoing (current agent replied)
                // 4. Explicit team message (role=team) → check teamFromAgentId / teamToAgentId
                const isUserMsg = msg.role === "user" && !msg.isBackground;
                const isBgIncoming = msg.isBackground && msg.role === "user";
                const isBgOutgoing = msg.isBackground && msg.role === "assistant";
                const isIncoming = isUserMsg || isBgIncoming || (!isBgOutgoing && msg.teamToAgentId === agentId);
                const counterpartId = isIncoming
                  ? (msg.teamFromAgentId ?? (isBgIncoming ? getDirectedAgentId(msg, agentInfo?.parentId) : null))
                  : (msg.teamToAgentId || getDirectedAgentId(msg, agentInfo?.parentId));
                const info = isIncoming
                  ? (isUserMsg
                      ? { name: userName || "操作员", position: "操作员", role: "" }
                      : (counterpartId ? resolveAgentInfo(counterpartId) : { name: "系统", role: "" }))
                  : (counterpartId
                      ? resolveAgentInfo(counterpartId)
                      : (agentInfo ? { name: agentInfo.name, role: agentInfo.role, position: agentInfo.position } : { name: "加载中…", role: "" }));
                const roleStyle = getRoleStyle(info.role || "");
                const positionLabel = getPositionLabel(info.position, info.role);
                const dotColor = roleDots[info.role || ""] || "bg-gray-400";
                const directionTag = isIncoming ? "收到" : "发送";
                const preview = msg.content || (msg.toolCalls?.length ? msg.toolCalls.map((tc) => tc.tool).join(", ") : "(empty)");
                const isExpanded = expandedMessageId === msg.id;
                return (
                  <button
                    key={msg.id}
                    onClick={() => setExpandedMessageId(isExpanded ? null : msg.id)}
                    className={"w-full px-4 py-2 text-left hover:bg-surface-border/30 transition-colors " + (!msg.isRead ? "bg-accent/5 " : "")}
                  >
                    <div className="flex items-center gap-2 mb-0.5 min-w-0">
                      <span className={"text-xs font-medium px-1.5 py-0.5 rounded shrink-0 " + (isIncoming ? "bg-emerald-500/15 text-emerald-300" : "bg-blue-500/15 text-blue-300")}>
                        {directionTag}
                      </span>
                      <span className={`w-2 h-2 rounded-full shrink-0 ${dotColor}`} />
                      <span className="text-sm font-medium text-gray-200 truncate min-w-0">{info.name}</span>
                      {positionLabel && (
                        <span className={`text-[10px] font-medium px-2 py-0.5 rounded-full shrink-0 ${roleStyle.bg} ${roleStyle.text}`}>
                          {positionLabel}
                        </span>
                      )}
                      {!msg.isRead && (
                        <span className="text-xs text-accent font-medium shrink-0">未读</span>
                      )}
                    </div>
                    <p className={"text-xs text-gray-500 " + (isExpanded ? "whitespace-pre-wrap break-words" : "truncate")}>{preview}</p>
                    {isExpanded && msg.toolCalls && msg.toolCalls.length > 0 && (
                      <div className="mt-2 space-y-1">
                        {msg.toolCalls.filter((tc) => tc.tool).map((tc, i) => {
                          const cat = toolCategories[tc.tool] || { color: "text-gray-300", bg: "bg-gray-500/15", label: tc.tool };
                          const hint = formatToolInputHint(tc.tool, tc.input);
                          return (
                            <div key={i} className={"text-xs px-2 py-1 rounded flex items-center gap-1.5 " + cat.bg + " " + cat.color}>
                              <span>{cat.label}</span>
                              {hint && <span className="text-gray-500 truncate">— {hint}</span>}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}

      <div className="px-6 py-4 border-t border-surface-border bg-surface-card shrink-0">
        {images.length > 0 && (
          <div className="flex gap-2 mb-2 flex-wrap">
            {images.map((url, i) => (
              <div key={i} className="relative group">
                <img src={url} className="h-16 w-16 object-cover rounded-lg border border-surface-border" alt="" />
                <button onClick={() => removeImage(i)} className="absolute -top-1 -right-1 w-5 h-5 bg-red-500 text-white rounded-full text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">x</button>
              </div>
            ))}
          </div>
        )}
        {queuedCount > 0 && (
          <p className="text-xs text-amber-400 mb-2">已排队 {queuedCount} 条消息，将在当前回复完成后自动发送</p>
        )}
        <div className="flex gap-2 items-end">
          <input type="file" ref={fileInputRef} onChange={handleFileInput} accept="image/*" multiple className="hidden" />
          <button onClick={() => fileInputRef.current?.click()} disabled={images.length >= 5 || isStreaming} className="px-3 py-3 text-gray-400 hover:text-accent disabled:opacity-30 transition-colors" title="添加图片 (支持粘贴/拖拽)">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>
          </button>
          <textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={handleKeyDown} onPaste={handlePaste} placeholder="输入消息... (Enter 发送, Shift+Enter 换行, 支持粘贴图片)" className="flex-1 bg-surface border border-surface-border rounded-xl px-4 py-3 text-sm text-gray-100 resize-none focus:outline-none focus:border-accent" rows={1} disabled={isStreaming} />
          <button onClick={handleSend} disabled={(!input.trim() && images.length === 0) || isStreaming} className="px-6 py-3 bg-accent text-white rounded-xl text-sm disabled:opacity-50 transition-colors">发送</button>
          <button onClick={handleStop} disabled={!isStreaming} className="px-6 py-3 bg-red-500 hover:bg-red-600 text-white rounded-xl text-sm font-medium transition-colors disabled:opacity-30 disabled:cursor-not-allowed">停止</button>
        </div>
      </div>

      {showApprovalDialog && agentId && (
        <ApprovalDialog agentId={agentId} onClose={() => { setShowApprovalDialog(false); setPendingApprovalTool(null); }} />
      )}
    </div>
  );
}

export default ChatPanel;
