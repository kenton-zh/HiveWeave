import { useState, useRef, useEffect, useCallback, useMemo, type MutableRefObject } from "react";
import { getAgent, getChatMessages, markMessagesRead, subscribeAgentStream } from "../api";
import type { ChatEvent } from "../api";
import { useAppStore } from "../store";
import type { AgentInfo, ChatMessage, MsgSegment, StreamDraft } from "./types";
import {
  appendToolCallSegment,
  beginStreamRound,
  draftFromStreamingMessage,
  isInjectedContext,
  isTeamChannelMessage,
  mapDbToChatMessages,
  parseToolUsePayload,
} from "./messageUtils";

type UpdateStreamDraft = (
  updater: StreamDraft | null | ((prev: StreamDraft | null) => StreamDraft | null)
) => void;

export function useChatMessages(opts: {
  agentId: string | null;
  streamDraft: StreamDraft | null;
  streamDraftRef: MutableRefObject<StreamDraft | null>;
  updateStreamDraft: UpdateStreamDraft;
  isStreaming: boolean;
  setIsStreaming: (v: boolean) => void;
  setThinkingElapsed: (v: number | null) => void;
  activeAgentIdRef: MutableRefObject<string | null>;
}) {
  const {
    agentId,
    streamDraft,
    streamDraftRef,
    updateStreamDraft,
    isStreaming,
    setIsStreaming,
    setThinkingElapsed,
    activeAgentIdRef,
  } = opts;

  const [agentInfo, setAgentInfo] = useState<AgentInfo | null>(null);
  const agentInfoRef = useRef<AgentInfo | null>(null);
  useEffect(() => {
    agentInfoRef.current = agentInfo;
  }, [agentInfo]);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [teamCommsExpanded, setTeamCommsExpanded] = useState(false);
  const [expandedMessageId, setExpandedMessageId] = useState<string | null>(null);

  const refreshOrgTree = useAppStore((s) => s.refreshOrgTree);
  const processingAgents = useAppStore((s) => s.processingAgents);
  const updateProcessingAgent = useAppStore((s) => s.updateProcessingAgent);
  const orgTreeVersion = useAppStore((s) => s.orgTreeVersion);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const savedDraftsRef = useRef<Record<string, StreamDraft | null>>({});
  const prevAgentIdRef = useRef<string | null>(null);
  const passiveSubRef = useRef<string | null>(null);
  const passiveUnsubRef = useRef<(() => void) | null>(null);

  const handleMessagesScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distFromBottom <= 72;
  }, []);

  const loadMessagesFromDb = useCallback(
    async (loadForAgentId: string): Promise<boolean> => {
      try {
        const dbMessages = await getChatMessages(loadForAgentId);
        if (activeAgentIdRef.current !== loadForAgentId) return false;
        const converted = mapDbToChatMessages(dbMessages);
        const ZOMBIE_STREAMING_MS = 12 * 60 * 1000;
        const now = Date.now();
        const hasStreamDraft = streamDraftRef.current !== null;
        const agentIsProcessing = useAppStore.getState().processingAgents.includes(loadForAgentId);
        const sanitized = converted.map((m) => {
          if (!m.isStreaming || m.role !== "assistant") return m;
          if (hasStreamDraft && streamDraftRef.current?.assistantId === m.id) return m;
          if (agentIsProcessing) return m;
          if (now - m.timestamp > ZOMBIE_STREAMING_MS) {
            return { ...m, isStreaming: false, content: m.content || "[对话被中断]" };
          }
          return m;
        });
        const seen = new Set<string>();
        const deduped = sanitized.filter((m) => {
          if (seen.has(m.id)) return false;
          seen.add(m.id);
          return true;
        });
        if (streamDraftRef.current?.assistantId) {
          const hasTarget = deduped.some((m) => m.id === streamDraftRef.current!.assistantId);
          if (!hasTarget) {
            deduped.push({
              id: streamDraftRef.current.assistantId,
              role: "assistant" as const,
              content: "",
              timestamp: Date.now(),
              isBackground: false,
              isRead: true,
              isStreaming: true,
            });
          }
        }
        setMessages(deduped);
        useAppStore.getState().setChatMessages(loadForAgentId, deduped);
        const unreadIds = deduped
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
    },
    [activeAgentIdRef, refreshOrgTree, streamDraftRef]
  );

  const releasePassiveStream = useCallback((id?: string) => {
    if (passiveSubRef.current == null) return;
    if (id && passiveSubRef.current !== id) return;
    passiveUnsubRef.current?.();
    passiveUnsubRef.current = null;
    passiveSubRef.current = null;
  }, []);

  const applyStreamEvent = useCallback(
    (event: ChatEvent, forAgentId: string) => {
      if (activeAgentIdRef.current !== forAgentId) return;
      if (event.type === "round_start") {
        updateStreamDraft((prev) => (prev ? beginStreamRound(prev) : prev));
        return;
      }
      if (event.type === "message_id") {
        try {
          const parsed = JSON.parse(event.data);
          if (parsed.role === "assistant" && parsed.id) {
            setMessages((prev) => {
              if (prev.some((m) => m.id === parsed.id)) return prev;
              return [
                ...prev,
                {
                  id: parsed.id,
                  role: "assistant" as const,
                  content: "",
                  timestamp: Date.now(),
                  isBackground: false,
                  isRead: true,
                  isStreaming: true,
                },
              ];
            });
            if (!streamDraftRef.current) {
              updateStreamDraft({ assistantId: parsed.id, segments: [] });
            }
          }
        } catch {
          /* ignore */
        }
        return;
      }
      if (event.type === "text_delta" || event.type === "thinking_delta" || event.type === "text") {
        setThinkingElapsed(null);
        const segType: MsgSegment["type"] = event.type === "thinking_delta" ? "thinking" : "text";
        if (!streamDraftRef.current) {
          const placeholderId = `draft-${forAgentId}-${Date.now()}`;
          setMessages((prev) => {
            if (prev.some((m) => m.id === placeholderId)) return prev;
            return [
              ...prev,
              {
                id: placeholderId,
                role: "assistant" as const,
                content: "",
                timestamp: Date.now(),
                isBackground: false,
                isRead: true,
                isStreaming: true,
              },
            ];
          });
          updateStreamDraft({
            assistantId: placeholderId,
            segments: [{ type: segType, content: event.data }],
          });
          return;
        }
        updateStreamDraft((prev) => {
          if (!prev) return prev;
          const last = prev.segments[prev.segments.length - 1];
          if (last && last.type === segType) {
            const merged = (last.content || "") + event.data;
            return {
              ...prev,
              segments: [...prev.segments.slice(0, -1), { ...last, content: merged }],
            };
          }
          return { ...prev, segments: [...prev.segments, { type: segType, content: event.data }] };
        });
        return;
      }
      if (event.type === "thinking") {
        setThinkingElapsed(event.elapsed_s ?? null);
        return;
      }
      if (event.type === "tool_use") {
        setThinkingElapsed(null);
        const parsed = parseToolUsePayload(event.data);
        if (!parsed) return;
        if (!streamDraftRef.current) {
          const placeholderId = `draft-${forAgentId}-${Date.now()}`;
          setMessages((prev) => {
            if (prev.some((m) => m.id === placeholderId)) return prev;
            return [
              ...prev,
              {
                id: placeholderId,
                role: "assistant" as const,
                content: "",
                timestamp: Date.now(),
                isBackground: false,
                isRead: true,
                isStreaming: true,
              },
            ];
          });
          updateStreamDraft(
            appendToolCallSegment(
              { assistantId: placeholderId, segments: [] },
              parsed.toolCall,
              parsed.toolCallId,
            ),
          );
          return;
        }
        updateStreamDraft((prev) =>
          prev ? appendToolCallSegment(prev, parsed.toolCall, parsed.toolCallId) : prev,
        );
        return;
      }
      if (event.type === "done") {
        setThinkingElapsed(null);
        loadMessagesFromDb(forAgentId).then((ok) => {
          if (ok) updateStreamDraft(null);
        });
        setIsStreaming(false);
        updateProcessingAgent(forAgentId, false);
        delete savedDraftsRef.current[forAgentId];
        releasePassiveStream(forAgentId);
        return;
      }
      if (event.type === "error") {
        setThinkingElapsed(null);
        setIsStreaming(false);
        updateProcessingAgent(forAgentId, false);
        delete savedDraftsRef.current[forAgentId];
        releasePassiveStream(forAgentId);
      }
    },
    [
      activeAgentIdRef,
      loadMessagesFromDb,
      releasePassiveStream,
      setIsStreaming,
      setThinkingElapsed,
      streamDraftRef,
      updateProcessingAgent,
      updateStreamDraft,
    ],
  );
  const applyStreamEventRef = useRef(applyStreamEvent);
  applyStreamEventRef.current = applyStreamEvent;

  const attachPassiveStream = useCallback(
    (id: string) => {
      if (passiveSubRef.current === id) return;
      releasePassiveStream();
      const unsub = subscribeAgentStream(id, (event) => applyStreamEventRef.current(event, id));
      passiveSubRef.current = id;
      passiveUnsubRef.current = unsub;
    },
    [releasePassiveStream],
  );

  // Mount / agent / orgTreeVersion effect — MUST NOT abort stream on cleanup.
  useEffect(() => {
    const switchingFrom = prevAgentIdRef.current;
    if (switchingFrom && switchingFrom !== agentId && streamDraftRef.current) {
      savedDraftsRef.current[switchingFrom] = streamDraftRef.current;
    }
    prevAgentIdRef.current = agentId;

    if (!agentId) {
      setAgentInfo(null);
      setMessages([]);
      updateStreamDraft(null);
      setConfirmingDelete(false);
      setTeamCommsExpanded(false);
      setExpandedMessageId(null);
      return;
    }

    let cancelled = false;
    const loadForAgentId = agentId;
    stickToBottomRef.current = true;

    const isAgentSwitch = switchingFrom !== agentId;
    const savedDraft = savedDraftsRef.current[agentId];
    const isStillProcessing = useAppStore.getState().processingAgents.includes(agentId);

    if (isAgentSwitch && savedDraft && isStillProcessing) {
      updateStreamDraft(savedDraft);
      setIsStreaming(true);
      attachPassiveStream(agentId);
    } else if (isAgentSwitch) {
      setIsStreaming(false);
      updateStreamDraft(null);
      if (savedDraft) delete savedDraftsRef.current[agentId];
    }

    const cached = useAppStore.getState().chatSessions[loadForAgentId];
    if (cached && cached.length > 0) {
      setMessages(cached as ChatMessage[]);
    } else {
      setMessages([]);
    }

    async function fetchAgent() {
      try {
        const raw = await getAgent(loadForAgentId);
        const data =
          raw && typeof raw === "object" && "agent" in raw && (raw as any).agent
            ? (raw as any).agent
            : raw;
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
      // Only prevent stale fetch/load results. Do NOT abort the stream here —
      // this effect re-runs when loadMessagesFromDb or orgTreeVersion changes.
      cancelled = true;
    };
  }, [
    agentId,
    attachPassiveStream,
    loadMessagesFromDb,
    orgTreeVersion,
    activeAgentIdRef,
    setIsStreaming,
    setThinkingElapsed,
    streamDraftRef,
    updateStreamDraft,
    updateProcessingAgent,
  ]);

  // Passive live stream while parked on this agent (trigger/wake — not user streamChat).
  // subscribeAgentStream replaces _agentHandlers: skip when streamChat owns it (isStreaming).
  // Do NOT return an unsubscribe tied to isStreaming — going false→true would steal the handler.
  useEffect(() => {
    if (!agentId) {
      releasePassiveStream();
      return;
    }
    if (!processingAgents.includes(agentId)) {
      const wasPassive = passiveSubRef.current === agentId;
      releasePassiveStream(agentId);
      if (wasPassive) setIsStreaming(false);
      return;
    }
    if (isStreaming) return;
    attachPassiveStream(agentId);
    if (!streamDraftRef.current) {
      const cached = useAppStore.getState().chatSessions[agentId] as ChatMessage[] | undefined;
      const streaming = cached?.find((m) => m.isStreaming && m.role === "assistant");
      if (streaming) {
        updateStreamDraft(
          draftFromStreamingMessage(streaming, { includeTools: false }),
        );
      }
    }
    setIsStreaming(true);
  }, [
    agentId,
    isStreaming,
    processingAgents,
    attachPassiveStream,
    releasePassiveStream,
    streamDraftRef,
    updateStreamDraft,
    setIsStreaming,
  ]);

  // Drop the passive handler on agent switch. Never push WS cancel (BUG-034).
  useEffect(() => {
    return () => {
      releasePassiveStream();
    };
  }, [agentId, releasePassiveStream]);

  // BUG-036: event-driven load only
  useEffect(() => {
    if (!agentId) return;
    loadMessagesFromDb(agentId);
  }, [agentId]); // eslint-disable-line react-hooks/exhaustive-deps

  const isAgentProcessing = agentId ? processingAgents.includes(agentId) : false;

  const displayMessages = useMemo(() => {
    let merged = messages;
    const hasPersistedDraft = streamDraft && streamDraft.persisted;
    if ((isStreaming && streamDraft) || hasPersistedDraft) {
      merged = messages.map((m) => {
        const isTarget = m.id === streamDraft!.assistantId;
        if (!isTarget && !hasPersistedDraft) {
          return m.isStreaming ? { ...m, isStreaming: false } : m;
        }
        if (!isTarget && hasPersistedDraft) {
          return m;
        }
        const textParts = streamDraft!.segments.filter((s) => s.type === "text").map((s) => s.content || "");
        const thinkingParts = streamDraft!.segments
          .filter((s) => s.type === "thinking")
          .map((s) => s.content || "");
        const newTools = streamDraft!.segments.filter((s) => s.type === "tool_call").map((s) => s.tool!);
        return {
          ...m,
          content: textParts.join(""),
          toolCalls: newTools.length > 0 ? newTools : m.toolCalls || [],
          _segments: streamDraft!.segments,
          _thinking: thinkingParts.join(""),
          isStreaming: hasPersistedDraft ? false : true,
        };
      });
    } else {
      merged = merged.map((m) => (m.isStreaming ? { ...m, isStreaming: false } : m));
    }
    merged = merged.filter((m) => !isInjectedContext(m));
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
    const ORPHAN_WARN_DELAY_MS = 5000;
    const now = Date.now();
    const lastUser = foreground[foreground.length - 1];
    const userMsgAge =
      lastUser?.role === "user" && lastUser?.timestamp ? now - lastUser.timestamp : Infinity;
    if (
      trailingUserCount >= 1 &&
      !isAgentProcessing &&
      !hasStreamingPlaceholder &&
      !isStreaming &&
      userMsgAge > ORPHAN_WARN_DELAY_MS
    ) {
      if (lastUser?.role === "user") {
        const warn =
          trailingUserCount >= 2
            ? "你已发送多条消息但 Agent 尚未回复。请等待当前任务完成，或检查网络/API 配置后重试。"
            : "⚠️ 上次对话未收到回复。Agent 可能遇到了异常，请重新发送消息。";
        return [
          ...foreground,
          {
            id: `${lastUser.id}-orphan`,
            role: "system" as const,
            content: warn,
            timestamp: lastUser.timestamp + 1,
          },
        ];
      }
    }
    return foreground;
  }, [messages, isStreaming, streamDraft, isAgentProcessing]);

  useEffect(() => {
    if (!stickToBottomRef.current) return;
    messagesEndRef.current?.scrollIntoView({ behavior: isStreaming ? "auto" : "smooth" });
  }, [displayMessages, isStreaming]);

  useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    const id = requestAnimationFrame(() => {
      if (cancelled) return;
      const cached = useAppStore.getState().chatSessions[agentId];
      const sanitized = messages
        .map((m) => (m.isStreaming ? { ...m, isStreaming: false } : m))
        .filter(
          (m) =>
            !(
              m.role === "assistant" &&
              !m.isStreaming &&
              !m.content &&
              (!m.toolCalls || m.toolCalls.length === 0)
            )
        );
      if (cached && cached.length === sanitized.length && cached.every((c, i) => c === sanitized[i]))
        return;
      useAppStore.getState().setChatMessages(agentId, sanitized);
    });
    return () => {
      cancelled = true;
      cancelAnimationFrame(id);
    };
  }, [agentId, messages]);

  const { directMessages, teamMessages } = useMemo(() => {
    const team = messages.filter((m) => isTeamChannelMessage(m));
    const teamRoleMsgs = team.filter((m) => m.role === "team");
    const dedupedTeam = team.filter((m) => {
      if (!(m.isBackground && m.role === "user")) return true;
      return !teamRoleMsgs.some(
        (t) =>
          t.teamFromAgentId === m.teamFromAgentId &&
          t.teamToAgentId === m.teamToAgentId &&
          Math.abs(t.timestamp - m.timestamp) < 60_000
      );
    });
    const direct = displayMessages.filter((m) => !isTeamChannelMessage(m));
    return { directMessages: direct, teamMessages: dedupedTeam };
  }, [messages, displayMessages]);

  const hasTeamComms = teamMessages.length > 0;

  return {
    agentInfo,
    setAgentInfo,
    agentInfoRef,
    messages,
    setMessages,
    confirmingDelete,
    setConfirmingDelete,
    teamCommsExpanded,
    setTeamCommsExpanded,
    expandedMessageId,
    setExpandedMessageId,
    loadMessagesFromDb,
    messagesEndRef,
    scrollContainerRef,
    stickToBottomRef,
    handleMessagesScroll,
    isAgentProcessing,
    displayMessages,
    directMessages,
    teamMessages,
    hasTeamComms,
    refreshOrgTree,
  };
}
