import { useState, useRef, useEffect, useCallback, useMemo } from "react";
import { useAppStore } from "../store";
import { streamChat, getAgent, deleteAgent, getChatMessages, markMessagesRead } from "../api";
import ApprovalDialog from "./ApprovalDialog";

interface AgentInfo {
  id: string;
  name: string;
  role: string;
  status: string;
  parentId?: string | null;
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
  teamFromAgentId?: string;
  teamToAgentId?: string;
}

interface StreamDraft {
  assistantId: string;
  content: string;
  toolCalls: ToolCall[];
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

const TEAM_TOOLS = new Set(["dispatch_task", "message_superior", "message_peer"]);

function isTeamChannelMessage(msg: ChatMessage): boolean {
  if (msg.role === "team") return true;
  if (msg.isBackground) return true;
  if (msg.toolCalls?.some((tc) => TEAM_TOOLS.has(tc.tool))) return true;
  return false;
}

function tryParseToolCalls(raw: string): ToolCall[] {
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed;
    return [];
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
    images: typeof m.images === "string" ? tryParseImages(m.images) : m.images,
    timestamp: m.createdAt,
    toolCalls: m.toolCalls ? tryParseToolCalls(m.toolCalls) : undefined,
    isBackground: !!m.isBackground,
    isRead: !!m.isRead,
    isStreaming: !!m.isStreaming,
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


function formatToolInputHint(tool: string, input: Record<string, any>): string | null {
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
        className="w-full text-left text-[11px] text-gray-400 hover:text-gray-300 transition-colors font-mono truncate"
      >
        {summary} \u5de5\u5177\u8c03\u7528 ({toolCalls.length}) \u2014 {preview}
      </button>
      {expanded && (
        <ul className="mt-1.5 space-y-0.5 font-mono text-[10px] text-gray-500 pl-3 border-l border-surface-border/60">
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

  return (
    <div className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
      <div
        className={`
          max-w-[80%] rounded-2xl px-4 py-3
          ${msg.role === "user"
            ? "bg-accent text-white"
            : "bg-surface-card border border-surface-border text-gray-200"
          }
        `}
      >
        {msg.content && (
          <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
        )}

        {msg.images && msg.images.length > 0 && (
          <div className={"flex gap-1.5 flex-wrap " + (msg.content ? "mt-2" : "")}>
            {msg.images.map((url, i) => (
              <img key={i} src={url} className="max-h-48 max-w-[200px] rounded-lg object-cover" alt="" />
            ))}
          </div>
        )}

        {msg.toolCalls && msg.toolCalls.length > 0 && (
          <div className={msg.content ? "mt-3 pt-3 border-t border-surface-border/50" : ""}>
            <ToolCallsBlock toolCalls={msg.toolCalls} />
          </div>
        )}

        {msg.role === "assistant" && isStreaming && !msg.content && (!msg.toolCalls || msg.toolCalls.length === 0) && (
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
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [showApprovalDialog, setShowApprovalDialog] = useState(false);
  const [pendingApprovalTool, setPendingApprovalTool] = useState<string | null>(null);
  const [teamCommsExpanded, setTeamCommsExpanded] = useState(false);
  const [expandedMessageId, setExpandedMessageId] = useState<string | null>(null);
  const [agentNameCache, setAgentNameCache] = useState<Record<string, string>>({});
  const [queuedCount, setQueuedCount] = useState(0);
  const pendingQueueRef = useRef<string[]>([]);
  const autoSendRef = useRef(false);
  const [retryInfo, setRetryInfo] = useState<{ attempt: number; maxRetries: number; reason: string } | null>(null);
  const [images, setImages] = useState<string[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);


  const handleMessagesScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distFromBottom <= 72;
  }, []);

  const loadMessagesFromDb = useCallback(async (loadForAgentId: string) => {
    try {
      const dbMessages = await getChatMessages(loadForAgentId);
      if (activeAgentIdRef.current !== loadForAgentId) return;
      const converted = mapDbToChatMessages(dbMessages);
      setMessages(converted);
      const unreadIds = converted
        .filter((m) => !m.isRead && (m.isBackground || m.role === "team"))
        .map((m) => m.id);
      if (unreadIds.length > 0) {
        markMessagesRead(unreadIds).catch(() => {});
        refreshOrgTree();
      }
    } catch (err) {
      if (activeAgentIdRef.current !== loadForAgentId) return;
      console.warn("Failed to load chat messages from DB:", err);
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
      setStreamDraft(null);
      refreshOrgTree();
      useAppStore.getState().setSelectedAgent(null);
    } catch (err: any) {
      alert(err.message || "Failed to delete agent");
      setConfirmingDelete(false);
    }
  }, [agentId, confirmingDelete, refreshOrgTree]);

  useEffect(() => {
    if (!agentId) {
      setAgentInfo(null);
      setMessages([]);
      setStreamDraft(null);
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
    setStreamDraft(null);
    setMessages([]);

    async function fetchAgent() {
      try {
        const data = await getAgent(loadForAgentId);
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

    return () => {
      cancelled = true;
      abortControllerRef.current?.abort();
      if (responseTimeoutRef.current) {
        clearTimeout(responseTimeoutRef.current);
        responseTimeoutRef.current = null;
      }
    };
  }, [agentId, loadMessagesFromDb]);

  const isAgentProcessing = agentId ? processingAgents.includes(agentId) : false;

  const hasUnansweredUser = useMemo(() => {
    const fg = messages.filter((m) => !m.isBackground && (m.role === "user" || m.role === "assistant"));
    const last = fg[fg.length - 1];
    const streaming = messages.some((m) => m.isStreaming && m.role === "assistant");
    return last?.role === "user" && !streaming;
  }, [messages]);


  const displayMessages = useMemo(() => {
    let merged = messages;
    if (isStreaming && streamDraft) {
      merged = messages.map((m) =>
        m.id === streamDraft.assistantId
          ? {
              ...m,
              content: streamDraft.content || m.content,
              toolCalls: streamDraft.toolCalls.length > 0 ? streamDraft.toolCalls : m.toolCalls,
              isStreaming: true,
            }
          : m
      );
    }
    const foreground = merged.filter((m) => !m.isBackground && (m.role === "user" || m.role === "assistant"));
    let trailingUserCount = 0;
    for (let i = foreground.length - 1; i >= 0; i--) {
      if (foreground[i].role === "user") trailingUserCount++;
      else break;
    }
    const hasStreamingPlaceholder = merged.some((m) => m.isStreaming && m.role === "assistant");
    if (trailingUserCount >= 1 && !isAgentProcessing && !hasStreamingPlaceholder && !isStreaming) {
      const lastUser = foreground[foreground.length - 1];
      if (lastUser?.role === "user") {
        const warn = trailingUserCount >= 2
          ? "你已发送多条消息但 Agent 尚未回复。请等待当前任务完成，或检查网络/API 配置后重试。"
          : "⚠️ 上次对话未收到回复。Agent 可能遇到了异常，请重新发送消息。";
        return [...merged, {
          id: `${lastUser.id}-orphan`,
          role: "system" as const,
          content: warn,
          timestamp: lastUser.timestamp + 1,
        }];
      }
    }
    return merged;
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

  const { directMessages, teamMessages } = useMemo(() => {
    const direct = displayMessages.filter((m) => !isTeamChannelMessage(m));
    const team = displayMessages.filter((m) => isTeamChannelMessage(m));
    return { directMessages: direct, teamMessages: team };
  }, [displayMessages]);

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
    const idsToFetch: string[] = [];
    for (const id of counterpartIds) {
      if (!agentNameCache[id]) idsToFetch.push(id);
    }
    if (idsToFetch.length === 0) return;
    for (const id of idsToFetch) {
      getAgent(id).then((data) => {
        if (data?.name) setAgentNameCache((prev) => ({ ...prev, [id]: data.name }));
      }).catch(() => {});
    }
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

    let messageText: string;
    if (autoSendRef.current) {
      autoSendRef.current = false;
      messageText = pendingQueueRef.current.shift() || "";
      setQueuedCount(pendingQueueRef.current.length);
    } else {
      if (!input.trim()) return;
      messageText = input.trim();
      setInput("");
      if (isStreaming || isAgentProcessing || hasUnansweredUser) {
        pendingQueueRef.current.push(messageText);
        setQueuedCount(pendingQueueRef.current.length);
        return;
      }
    }

    if (!messageText) return;

    const sendingImages = images;
    setImages([]);

    const sendingForAgentId = agentId;
    const isActiveSession = () => activeAgentIdRef.current === sendingForAgentId;
    const finishTurn = () => {
      if (pendingQueueRef.current.length > 0) {
        autoSendRef.current = true;
        setTimeout(() => handleSend(), 300);
      }
    };

    stickToBottomRef.current = true;
    setIsStreaming(true);
    setStreamDraft(null);
    setRetryInfo(null);
    if (responseTimeoutRef.current) clearTimeout(responseTimeoutRef.current);
    responseTimeoutRef.current = setTimeout(() => {
      if (!isActiveSession()) return;
      setIsStreaming(false);
      setStreamDraft(null);
      loadMessagesFromDb(sendingForAgentId);
      finishTurn();
    }, 30_000);
    const allToolsUsed = new Set<string>();
    abortControllerRef.current?.abort();
    abortControllerRef.current = streamChat(sendingForAgentId, messageText, sendingImages, (event) => {
      if (!isActiveSession()) return;
      if (event.type === "message_id") {
        loadMessagesFromDb(sendingForAgentId);
        try {
          const parsed = JSON.parse(event.data);
          if (parsed.role === "assistant" && parsed.id) {
            setStreamDraft({ assistantId: parsed.id, content: "", toolCalls: [] });
          }
        } catch {}
      } else if (event.type === "text") {
        setStreamDraft((prev) => prev ? { ...prev, content: prev.content + event.data } : prev);
      } else if (event.type === "tool_use") {
        try {
          const toolData = JSON.parse(event.data);
          const toolCall: ToolCall = {
            tool: (toolData.tool || "").replace(/^hiveweave__/, ""),
            input: toolData.input || {},
          };
          allToolsUsed.add(toolCall.tool);
          setStreamDraft((prev) => prev ? { ...prev, toolCalls: [...prev.toolCalls, toolCall] } : prev);
        } catch {}
      } else if (event.type === "tool_result") {
        setPendingApprovalTool(null);
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
          // Extend response timeout to accommodate retry backoff (delay + 10s buffer)
          if (responseTimeoutRef.current) clearTimeout(responseTimeoutRef.current);
          const extraMs = (data.delayMs || 5000) + 10000;
          responseTimeoutRef.current = setTimeout(() => {
            if (!isActiveSession()) return;
            setIsStreaming(false);
            setStreamDraft(null);
            setRetryInfo(null);
            loadMessagesFromDb(sendingForAgentId);
            finishTurn();
          }, extraMs);
        } catch {}
      } else if (event.type === "queued_message") {
        loadMessagesFromDb(sendingForAgentId);
      } else if (event.type === "done") {
        if (responseTimeoutRef.current) { clearTimeout(responseTimeoutRef.current); responseTimeoutRef.current = null; }
        setPendingApprovalTool(null);
        setRetryInfo(null);
        const ORG_TOOLS = new Set(["create_agent", "transfer_agent", "dismiss_agent", "create_from_template"]);
        if ([...allToolsUsed].some((x) => ORG_TOOLS.has(x))) refreshOrgTree();
        loadMessagesFromDb(sendingForAgentId);
        setStreamDraft(null);
        setIsStreaming(false);
        finishTurn();
      } else if (event.type === "error") {
        if (responseTimeoutRef.current) { clearTimeout(responseTimeoutRef.current); responseTimeoutRef.current = null; }
        setRetryInfo(null);
        setStreamDraft((prev) => prev ? { ...prev, content: prev.content + "\n\nError: " + event.data } : prev);
        loadMessagesFromDb(sendingForAgentId);
        setStreamDraft(null);
        setIsStreaming(false);
        finishTurn();
      }
    });
  }, [agentId, input, isStreaming, isAgentProcessing, hasUnansweredUser, refreshOrgTree, loadMessagesFromDb]);

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
    setStreamDraft(null);
    setRetryInfo(null);
    if (responseTimeoutRef.current) clearTimeout(responseTimeoutRef.current);
    if (pendingQueueRef.current.length > 0) {
      pendingQueueRef.current = [];
      setQueuedCount(0);
    }
  }, []);

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
  const resolveName = (id: string) => agentNameCache[id] || id.substring(0, 8);

  return (
    <div className="h-full flex flex-col bg-surface">
      {agentInfo && (
        <div className="px-6 py-3 border-b border-surface-border bg-surface-card shrink-0">
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${agentInfo.status === "active" && isAgentProcessing ? "bg-emerald-400 animate-pulse" : "bg-gray-400"}`} />
            <span className="text-sm font-medium text-gray-200">{agentInfo.name}</span>
            <span className="text-xs text-gray-500">·</span>
            <span className="text-xs text-gray-400">{roleLabels[agentInfo.role] || agentInfo.role}</span>
            <span className="text-xs text-gray-500">·</span>
            <span className={`text-xs ${runtimeStatusInfo.color}`}>{runtimeStatusInfo.text}</span>
          </div>
        </div>
      )}

      <div ref={scrollContainerRef} onScroll={handleMessagesScroll} className="flex-1 min-h-0 overflow-y-auto px-6 py-4 space-y-4">
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
        <div className="shrink-0 border-t border-surface-border bg-surface-card">
          <button onClick={() => { setTeamCommsExpanded(!teamCommsExpanded); if (teamCommsExpanded) setExpandedMessageId(null); }} className="w-full px-6 py-3 flex items-center justify-between hover:bg-surface-border/30 transition-colors">
            <span className="text-sm font-medium text-gray-300">团队沟通 ({teamMessages.length})</span>
            <span className="text-xs text-gray-500">{teamCommsExpanded ? "收起" : "展开"}</span>
          </button>
          {teamCommsExpanded && (
            <div className="max-h-[40vh] overflow-y-auto py-1">
              {[...teamMessages].sort((a, b) => b.timestamp - a.timestamp).map((msg) => {
                const isIncoming = msg.role === "user" || !!msg.teamFromAgentId;
                const counterpartId = isIncoming
                  ? (msg.teamFromAgentId ?? null)
                  : (msg.teamToAgentId || getDirectedAgentId(msg, agentInfo?.parentId));
                const label = isIncoming
                  ? (msg.role === "user" ? userName : (counterpartId ? resolveName(counterpartId) : "Unknown"))
                  : (counterpartId ? resolveName(counterpartId) : (agentInfo?.name || "Agent"));
                const directionTag = isIncoming ? "收到" : "发送";
                const preview = msg.content || (msg.toolCalls?.length ? msg.toolCalls.map((tc) => tc.tool).join(", ") : "(empty)");
                const isExpanded = expandedMessageId === msg.id;
                return (
                  <button
                    key={msg.id}
                    onClick={() => setExpandedMessageId(isExpanded ? null : msg.id)}
                    className={"w-full px-6 py-3 text-left hover:bg-surface-border/30 transition-colors " + (!msg.isRead ? "bg-accent/5 " : "")}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <span className={"text-[10px] font-medium px-1.5 py-0.5 rounded " + (isIncoming ? "bg-emerald-500/15 text-emerald-300" : "bg-blue-500/15 text-blue-300")}>
                        {directionTag}
                      </span>
                      <span className="text-sm font-medium text-gray-200">{label}</span>
                      {!msg.isRead && (
                        <span className="text-[10px] text-accent font-medium">未读</span>
                      )}
                    </div>
                    <p className={"text-xs text-gray-500 " + (isExpanded ? "whitespace-pre-wrap" : "truncate")}>{preview}</p>
                    {isExpanded && msg.toolCalls && msg.toolCalls.length > 0 && (
                      <div className="mt-2 space-y-1">
                        {msg.toolCalls.map((tc, i) => {
                          const cat = toolCategories[tc.tool] || { color: "text-gray-300", bg: "bg-gray-500/15", label: tc.tool };
                          return (
                            <div key={i} className={"text-[11px] px-2 py-1 rounded " + cat.bg + " " + cat.color}>
                              {cat.label}: {tc.tool}
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
                <button onClick={() => removeImage(i)} className="absolute -top-1 -right-1 w-5 h-5 bg-red-500 text-white rounded-full text-[10px] flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">x</button>
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
          {isStreaming ? (
            <button onClick={handleStop} className="px-6 py-3 bg-red-500 hover:bg-red-600 text-white rounded-xl text-sm font-medium transition-colors">停止</button>
          ) : (
            <button onClick={handleSend} disabled={!input.trim() && images.length === 0} className="px-6 py-3 bg-accent text-white rounded-xl text-sm disabled:opacity-50">发送</button>
          )}
        </div>
      </div>

      {showApprovalDialog && agentId && (
        <ApprovalDialog agentId={agentId} onClose={() => { setShowApprovalDialog(false); setPendingApprovalTool(null); }} />
      )}
    </div>
  );
}

export default ChatPanel;
