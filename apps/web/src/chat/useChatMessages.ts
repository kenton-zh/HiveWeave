import { useState, useRef, useEffect, useCallback, useMemo, type MutableRefObject } from "react";
import { getAgent, getChatMessages, markMessagesRead, subscribeAgentStream } from "../api";
import type { ChatEvent } from "../api";
import { useAppStore } from "../store";
import type { AgentInfo, ChatMessage, MsgSegment, StreamDraft } from "./types";
import {
  appendToolCallSegment,
  applyToolResult,
  beginStreamRound,
  draftFromStreamingMessage,
  isTeamChannelMessage,
  mapDbToChatMessages,
  mergeStreamDraftIntoMessages,
  parseToolUsePayload,
  sanitizeMessagesForCache,
  shouldWriteChatCache,
  streamEventBackgroundFlag,
} from "./messageUtils";

type UpdateStreamDraft = (
  updater: StreamDraft | null | ((prev: StreamDraft | null) => StreamDraft | null)
) => void;

/**
 * status=idle 早于 done 抵达时，保留被动订阅的宽限窗口。done 是「按
 * metadata.segments 权威重载」的唯一入口，必须等到；但它也可能永远不来
 * （后端崩溃 / WS 断开），到期强制收口以免 draft 永久留屏。
 */
const DONE_AFTER_IDLE_GRACE_MS = 8000;

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
  const [messagesAgentId, setMessagesAgentId] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [teamCommsExpanded, setTeamCommsExpanded] = useState(false);
  const [expandedMessageId, setExpandedMessageId] = useState<string | null>(null);

  const refreshOrgTree = useAppStore((s) => s.refreshOrgTree);
  const processingAgents = useAppStore((s) => s.processingAgents);
  const updateProcessingAgent = useAppStore((s) => s.updateProcessingAgent);
  const socketReconnectVersion = useAppStore((s) => s.socketReconnectVersion);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const savedDraftsRef = useRef<Record<string, StreamDraft | null>>({});
  const prevAgentIdRef = useRef<string | null>(null);
  const persistReadyRef = useRef(false);
  const updateStreamDraftRef = useRef(updateStreamDraft);
  updateStreamDraftRef.current = updateStreamDraft;
  const passiveSubRef = useRef<string | null>(null);
  const passiveUnsubRef = useRef<(() => void) | null>(null);
  // idle-先于-done 宽限窗口的绝对截止点（按 agent + 轮次锚定，见下方
  // 被动订阅 effect）。存 ref 而非每次重算，防止邻居 agent 的状态抖动
  // 把窗口无限续期。
  const graceDeadlineRef = useRef<{ agentId: string; roundId: string; at: number } | null>(null);
  // 已被 done/error 正常收口的轮次 id。draft 的清空要等 DB 重载往返，
  // 这个标记让宽限逻辑能立刻识别「本轮已收口，无需兜底」。
  const settledRoundRef = useRef<string | null>(null);
  // 重连对账 effect 用：记录已处理过的 socketReconnectVersion，挂载晚于
  // 重连（切 agent / 开面板）时不回放历史重连。
  const reconnectSeenRef = useRef(socketReconnectVersion);

  // ChatPanel is not remounted on agent switch. Adjust session during render so
  // a committed frame never shows the previous person's 团队沟通 under a new id.
  if (agentId !== messagesAgentId) {
    if (!agentId) {
      persistReadyRef.current = false;
      setMessagesAgentId(null);
      setMessages([]);
    } else {
      const cached = useAppStore.getState().chatSessions[agentId] as ChatMessage[] | undefined;
      setMessagesAgentId(agentId);
      if (cached && cached.length > 0) {
        persistReadyRef.current = true;
        setMessages(cached);
      } else {
        persistReadyRef.current = false;
        setMessages([]);
      }
    }
  }

  // handleMessagesScroll 定义在后（依赖 loadOlderMessages 分页加载）。

  const loadMessagesFromDb = useCallback(
    async (loadForAgentId: string): Promise<boolean> => {
      try {
        const dbMessages = await getChatMessages(loadForAgentId);
        if (activeAgentIdRef.current !== loadForAgentId) return false;
        if (!Array.isArray(dbMessages)) {
          console.warn("getChatMessages returned non-array", loadForAgentId);
          return false;
        }
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
              isBackground: streamDraftRef.current.isBackground === true,
              isRead: true,
              isStreaming: true,
            });
          }
        }
        persistReadyRef.current = true;
        setMessagesAgentId(loadForAgentId);
        const unreadIds = deduped
          .filter((m) => !m.isRead && (m.isBackground || m.role === "team"))
          .map((m) => m.id);
        const forStore =
          unreadIds.length > 0
            ? deduped.map((m) => (unreadIds.includes(m.id) ? { ...m, isRead: true } : m))
            : deduped;
        const existing = useAppStore.getState().chatSessions[loadForAgentId] as
          | ChatMessage[]
          | undefined;
        const cacheNext = sanitizeMessagesForCache(forStore);
        const keepCachedEmptyFetch =
          forStore.length === 0 && !!existing && existing.length > 0;
        if (keepCachedEmptyFetch) {
          setMessages(existing);
          return true;
        }
        // _genStats 是会话内易失数据（DB 不存生成耗时）：DB 重载整体替换
        // messages 前按 id 从现有行携带，否则连续对话中上一条的 tok/s
        // 会被下一轮 done 的重载抹掉。
        setMessages((prev) => {
          const prevStats = new Map(
            prev.filter((m) => m._genStats).map((m) => [m.id, m._genStats!]),
          );
          if (prevStats.size === 0) return forStore;
          return forStore.map((m) => {
            const stats = prevStats.get(m.id);
            return stats ? { ...m, _genStats: stats } : m;
          });
        });
        if (
          shouldWriteChatCache({
            agentId: loadForAgentId,
            messagesOwnerId: loadForAgentId,
            persistReady: true,
            next: cacheNext,
            existing,
          })
        ) {
          useAppStore.getState().setChatMessages(loadForAgentId, cacheNext);
        }
        if (unreadIds.length > 0) {
          void markMessagesRead(unreadIds, loadForAgentId)
            .then(() => refreshOrgTree())
            .catch(() => {});
        }
        // done/reload 全量替换回默认窗口 → 历史分页状态复位（分页
        // state 声明在本 callback 之后，回调执行时已初始化，闭包安全）
        historyOffsetRef.current = 0;
        setHasMoreHistory(true);
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
            const flag = streamEventBackgroundFlag(parsed);
            const isBackground = flag ?? true;
            setMessages((prev) => {
              if (prev.some((m) => m.id === parsed.id)) return prev;
              return [
                ...prev,
                {
                  id: parsed.id,
                  role: "assistant" as const,
                  content: "",
                  timestamp: Date.now(),
                  isBackground,
                  isRead: true,
                  isStreaming: true,
                },
              ];
            });
            if (!streamDraftRef.current) {
              updateStreamDraft({ assistantId: parsed.id, segments: [], isBackground, startedAt: Date.now() });
            } else if (streamDraftRef.current.assistantId.startsWith("draft-")) {
              // placeholder 流（passive 接入先于 message_id）：迁移到真实 id，
              // 否则 done 结算的 _genStats 会打在幻影 placeholder 行上丢失。
              const cur = streamDraftRef.current;
              const oldId = cur.assistantId;
              updateStreamDraft({
                ...cur,
                assistantId: parsed.id,
                ...(flag !== undefined ? { isBackground: flag } : {}),
              });
              // 本 handler 上方已为 parsed.id 建行，删掉旧 placeholder 行避免重 id。
              setMessages((prev) => prev.filter((m) => m.id !== oldId));
            } else if (flag !== undefined) {
              updateStreamDraft((prev) => (prev ? { ...prev, isBackground: flag } : prev));
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
                isBackground: true,
                isRead: true,
                isStreaming: true,
              },
            ];
          });
          updateStreamDraft({
            assistantId: placeholderId,
            segments: [{ type: segType, content: event.data }],
            isBackground: true,
            startedAt: Date.now(),
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
                isBackground: true,
                isRead: true,
                isStreaming: true,
              },
            ];
          });
          updateStreamDraft(
            appendToolCallSegment(
              { assistantId: placeholderId, segments: [], isBackground: true, startedAt: Date.now() },
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
      if (event.type === "tool_result") {
        // 工具执行完成 → 更新对应工具段状态（running→ok/error）+ 结果摘要
        try {
          const p = JSON.parse(event.data);
          const id = p.toolCallId || p.tool_call_id || undefined;
          const name = p.toolName || p.tool_name || undefined;
          if (id || name) {
            updateStreamDraft((prev) =>
              prev
                ? applyToolResult(prev, id, name, p.success !== false, String(p.result || ""))
                : prev,
            );
          }
        } catch {
          /* ignore */
        }
        return;
      }
      if (event.type === "done") {
        setThinkingElapsed(null);
        // 本轮已由 done 正常收口 —— 记下轮次 id 并作废宽限窗口。draft 要
        // 等 DB 重载回来才清空，这期间 idle 引发的 effect 重跑仍会看到非空
        // draft；没有这个标记就会为一个已收口的轮次白起 8s 定时器。
        graceDeadlineRef.current = null;
        if (streamDraftRef.current) {
          settledRoundRef.current = streamDraftRef.current.assistantId;
        }
        // done 先于 draft 清空：结算本轮端到端耗时冻结到消息上，气泡头部
        // 据此显示 tok/s（tokens 分子用渲染时的估算，口径一致）。
        const finishedDraft = streamDraftRef.current;
        const genStats =
          finishedDraft && finishedDraft.startedAt
            ? { ms: Math.max(1, Date.now() - finishedDraft.startedAt) }
            : null;
        const finishedId = finishedDraft?.assistantId;
        loadMessagesFromDb(forAgentId).then((ok) => {
          if (ok) updateStreamDraft(null);
          if (genStats && finishedId) {
            setMessages((prev) =>
              prev.map((m) => (m.id === finishedId ? { ...m, _genStats: genStats } : m)),
            );
          }
        });
        setIsStreaming(false);
        updateProcessingAgent(forAgentId, false);
        delete savedDraftsRef.current[forAgentId];
        releasePassiveStream(forAgentId);
        return;
      }
      if (event.type === "error") {
        setThinkingElapsed(null);
        graceDeadlineRef.current = null;
        if (streamDraftRef.current) {
          settledRoundRef.current = streamDraftRef.current.assistantId;
        }
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

  // Mount / agent switch — MUST NOT abort stream on cleanup.
  useEffect(() => {
    const switchingFrom = prevAgentIdRef.current;
    if (switchingFrom && switchingFrom !== agentId && streamDraftRef.current) {
      savedDraftsRef.current[switchingFrom] = streamDraftRef.current;
    }
    prevAgentIdRef.current = agentId;
    // 历史分页状态重置（分页 state 声明在本 effect 之后——回调执行时已初始化）
    historyOffsetRef.current = 0;
    setHasMoreHistory(true);

    if (!agentId) {
      persistReadyRef.current = false;
      setAgentInfo(null);
      updateStreamDraftRef.current(null);
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
      updateStreamDraftRef.current(savedDraft);
      setIsStreaming(true);
      attachPassiveStream(agentId);
    } else if (isAgentSwitch) {
      setIsStreaming(false);
      updateStreamDraftRef.current(null);
      if (savedDraft) delete savedDraftsRef.current[agentId];
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
      cancelled = true;
    };
  }, [
    agentId,
    attachPassiveStream,
    loadMessagesFromDb,
    setIsStreaming,
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
      // idle 可能早于 done 抵达（两者走不同频道、不同 forward task，投递
      // 先后由事件循环决定 —— 后端把 done 提前只是提高胜率，不是硬序）。
      // 此时若立刻摘掉 handler，随后的 done 就没人接 —— 而 done 是「按
      // metadata.segments 权威重载」的唯一入口，丢了它气泡会永久停在流式
      // 中途的 DB 快照上（只剩最后一轮 content，think/旁白/工具块全消失）。
      // 有 draft 在飞 = 本轮尚未收口，宽限一段等 done 自行收尾；done 真的
      // 不来（后端崩/WS 断/park 路径只发 idle）则到期强制收口。
      const draft = streamDraftRef.current;
      if (draft && settledRoundRef.current !== draft.assistantId) {
        // deadline 存 ref：processingAgents 每次翻转都是新数组引用，邻居
        // agent 的状态抖动会让本 effect 反复重跑。若每次重置满窗口，多
        // agent 并发下宽限可被无限续期，「到期强制收口」的承诺就失效了。
        const graceForAgentId = agentId;
        const roundId = draft.assistantId;
        const pending = graceDeadlineRef.current;
        if (!pending || pending.agentId !== graceForAgentId || pending.roundId !== roundId) {
          graceDeadlineRef.current = {
            agentId: graceForAgentId,
            roundId,
            at: Date.now() + DONE_AFTER_IDLE_GRACE_MS,
          };
        }
        const remaining = Math.max(0, graceDeadlineRef.current!.at - Date.now());
        const timer = window.setTimeout(() => {
          if (activeAgentIdRef.current !== graceForAgentId) return;
          if (useAppStore.getState().processingAgents.includes(graceForAgentId)) return;
          // 轮次校验：done 已正常收口（draft 清空）或新一轮已开始（换了
          // assistantId）时必须放弃 —— 否则会把下一轮的 draft 误清、把
          // 别人拥有的流式态抹平。
          const cur = streamDraftRef.current;
          if (!cur || cur.assistantId !== roundId) return;
          graceDeadlineRef.current = null;
          const wasPassive = passiveSubRef.current === graceForAgentId;
          releasePassiveStream(graceForAgentId);
          if (wasPassive) setIsStreaming(false);
          void loadMessagesFromDb(graceForAgentId).then((ok) => {
            if (!ok) return;
            const latest = streamDraftRef.current;
            if (latest && latest.assistantId === roundId) updateStreamDraft(null);
          });
        }, remaining);
        return () => window.clearTimeout(timer);
      }
      graceDeadlineRef.current = null;
      const wasPassive = passiveSubRef.current === agentId;
      releasePassiveStream(agentId);
      if (wasPassive) setIsStreaming(false);
      return;
    }
    graceDeadlineRef.current = null;
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
    socketReconnectVersion,
    activeAgentIdRef,
    attachPassiveStream,
    loadMessagesFromDb,
    releasePassiveStream,
    streamDraftRef,
    updateStreamDraft,
    setIsStreaming,
  ]);

  // WS 重连对账：断连窗口内错过的 done 是「按 DB 权威重载」的唯一触发器，
  // 若重连时该 done 已成为过去（replay 环被后续轮次挤出 / 后端重启清空），
  // 面板会永远停在断连时刻的快照上。重连即拉一次 DB。socketReconnectVersion
  // 只由真实 socket 重连发出（App 的 onOpen 钩子）；reconnectSeenRef 保证
  // 挂载晚于重连时不回放。draft 清空判据双路：DB 行已收口（is_streaming=0，
  // 快照可能滞后时的权威信号）或 agent 已不在处理中——在飞的用户流两者
  // 皆不满足，draft 保留，done 仍做最终对账。
  useEffect(() => {
    if (socketReconnectVersion === reconnectSeenRef.current) return;
    reconnectSeenRef.current = socketReconnectVersion;
    const id = agentId;
    if (!id) return;
    let cancelled = false;
    void loadMessagesFromDb(id).then((ok) => {
      if (cancelled || !ok) return;
      const cur = streamDraftRef.current;
      if (!cur) return;
      const row = useAppStore
        .getState()
        .chatSessions[id]?.find((m) => m.id === cur.assistantId);
      const turnDoneInDb = !!row && !row.isStreaming;
      if (
        turnDoneInDb ||
        !useAppStore.getState().processingAgents.includes(id)
      ) {
        updateStreamDraft(null);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [
    socketReconnectVersion,
    agentId,
    loadMessagesFromDb,
    streamDraftRef,
    updateStreamDraft,
  ]);

  // Drop the passive handler on agent switch. Never push WS cancel (BUG-034).
  useEffect(() => {
    return () => {
      releasePassiveStream();
    };
  }, [agentId, releasePassiveStream]);

  const isAgentProcessing = agentId ? processingAgents.includes(agentId) : false;

  const displayMessages = useMemo(() => {
    // 全量时间线：真人用户、后台 trigger digest（agent/system/watchdog）、
    // 全部 assistant 回复（含后台）。role=team 仍归「团队沟通」栏。
    // digest（isContext）不再隐藏——由 MessageBubble 折叠展示。
    // 上下文边界标记（role=system + _contextMarker）也留在主栏：它标出
    // 「模型记忆从此处开始」，滤掉就回到了"显示与实际不一致"。
    const merged = mergeStreamDraftIntoMessages(messages, streamDraft, { isStreaming });
    const foreground = merged.filter((m) => {
      if (m.role === "system") return !!m._contextMarker;
      if (m.role !== "user" && m.role !== "assistant") return false;
      if (m.role === "assistant" && !m.isStreaming) {
        const hasContent = m.content && m.content.trim().length > 0;
        const hasToolCalls = m.toolCalls && m.toolCalls.length > 0;
        const hasSegments = m._segments && m._segments.length > 0;
        if (!hasContent && !hasToolCalls && !hasSegments) return false;
      }
      return true;
    });
    const isHumanUser = (m: ChatMessage) =>
      m.role === "user" && (m.source === "user" || (!m.isBackground && !m.source));
    let trailingUserCount = 0;
    for (let i = foreground.length - 1; i >= 0; i--) {
      if (isHumanUser(foreground[i])) trailingUserCount++;
      else break;
    }
    const hasStreamingPlaceholder = foreground.some((m) => m.isStreaming && m.role === "assistant");
    const ORPHAN_WARN_DELAY_MS = 5000;
    const now = Date.now();
    const lastUser = foreground[foreground.length - 1];
    const userMsgAge =
      lastUser && isHumanUser(lastUser) && lastUser.timestamp ? now - lastUser.timestamp : Infinity;
    if (
      trailingUserCount >= 1 &&
      !isAgentProcessing &&
      !hasStreamingPlaceholder &&
      !isStreaming &&
      userMsgAge > ORPHAN_WARN_DELAY_MS
    ) {
      if (lastUser && isHumanUser(lastUser)) {
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
    // ChatPanel is not remounted: persist only after messagesAgentId caught up
    // to agentId. A ref flipped in the switch effect would still see the previous
    // person's messages under the new id.
    const existing = useAppStore.getState().chatSessions[agentId] as ChatMessage[] | undefined;
    const next = sanitizeMessagesForCache(messages);
    if (
      !shouldWriteChatCache({
        agentId,
        messagesOwnerId: messagesAgentId,
        persistReady: persistReadyRef.current,
        next,
        existing,
      })
    ) {
      return;
    }
    useAppStore.getState().setChatMessages(agentId, next);
  }, [agentId, messages, messagesAgentId]);

  const { directMessages, teamMessages } = useMemo(() => {
    const team = messages.filter((m) => isTeamChannelMessage(m));
    const teamRoleMsgs = team.filter((m) => m.role === "team");
    const dedupedTeam = team.filter((m) => {
      if (!(m.isBackground && m.role === "user")) return true;
      return !teamRoleMsgs.some(
        (t) =>
          t.teamFromAgentId === m.teamFromAgentId &&
          t.teamToAgentId === m.teamToAgentId &&
          Math.abs(t.timestamp - m.timestamp) < 60_000,
      );
    });
    // 主栏 = 全量时间线（user/assistant 含 background）；team 角色只在团队栏。
    return { directMessages: displayMessages, teamMessages: dedupedTeam };
  }, [messages, displayMessages]);

  // ── 历史分页（滚动到顶自动加载更早消息）──────────────────
  // 分页状态复位在 loadMessagesFromDb 成功路径内联执行（done/agent 切换
  // 都会走到）；agent 切换 effect 内亦有一份 inline 复位兜底。
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(true);
  const historyOffsetRef = useRef(0);
  const loadingOlderRef = useRef(false);

  const loadOlderMessages = useCallback(async () => {
    const owner = messagesAgentId;
    if (!owner || loadingOlderRef.current || !hasMoreHistory) return;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    const el = scrollContainerRef.current;
    const prevHeight = el?.scrollHeight ?? 0;
    try {
      const PAGE = 100;
      const nextOffset = historyOffsetRef.current + PAGE;
      const older = await getChatMessages(owner, { limit: PAGE, offset: nextOffset });
      if (!Array.isArray(older)) return;
      historyOffsetRef.current = nextOffset;
      if (older.length < PAGE) setHasMoreHistory(false);
      const converted = mapDbToChatMessages(older).map((m) =>
        m.isStreaming ? { ...m, isStreaming: false } : m,
      );
      setMessages((prev) => {
        if (prev.length === 0) return converted;
        const seen = new Set(prev.map((m) => m.id));
        const fresh = converted.filter((m) => !seen.has(m.id));
        if (fresh.length === 0) return prev;
        return [...fresh, ...prev].sort((a, b) => a.timestamp - b.timestamp);
      });
      // 保持视口钉在原内容位置（prepend 不跳顶）
      requestAnimationFrame(() => {
        const el2 = scrollContainerRef.current;
        if (el2) el2.scrollTop = el2.scrollHeight - prevHeight;
      });
    } catch {
      /* 尽力而为 */
    } finally {
      loadingOlderRef.current = false;
      setLoadingOlder(false);
    }
  }, [messagesAgentId, hasMoreHistory, scrollContainerRef]);

  const handleMessagesScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    if (el.scrollTop <= 48 && el.scrollHeight > el.clientHeight) {
      void loadOlderMessages();
    }
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distFromBottom <= 72;
  }, [loadOlderMessages]);

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
    loadingOlder,
    hasMoreHistory,
    loadOlderMessages,
    isAgentProcessing,
    displayMessages,
    directMessages,
    teamMessages,
    hasTeamComms,
    refreshOrgTree,
  };
}
