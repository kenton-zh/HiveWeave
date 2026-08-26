import { useState, useRef, useEffect, useCallback, useMemo } from "react";
import { getAgent, deleteAgent } from "../api";
import { useAppStore } from "../store";
import ApprovalDialog from "./ApprovalDialog";
import TodoBar from "./TodoBar";
import { getRoleStyle, getPositionLabel } from "../utils/role-styles";
import { roleLabels, statusLabels } from "../chat/constants";
import { getDirectedAgentId, nextBadgePopToken } from "../chat/messageUtils";
import { MessageBubble, ChatMotionStyles } from "../chat/MessageBubble";
import { useStreamDraft } from "../chat/useStreamDraft";
import { useChatMessages } from "../chat/useChatMessages";
import { useChatSend } from "../chat/useChatSend";
import { useAgentChannelLifecycle } from "../chat/useAgentChannelLifecycle";

function ChatPanel({ agentId, hidden }: { agentId: string | null; hidden?: boolean }) {
  const [isStreaming, setIsStreaming] = useState(false);
  const [thinkingElapsed, setThinkingElapsed] = useState<number | null>(null);
  const activeAgentIdRef = useRef<string | null>(agentId);
  activeAgentIdRef.current = agentId;

  const userName = useAppStore((s) => s.userName);
  const agentDispositions = useAppStore((s) => s.agentDispositions);
  const disposition = agentId ? agentDispositions[agentId] : undefined;

  const { streamDraft, streamDraftRef, updateStreamDraft } = useStreamDraft();

  const {
    agentInfo,
    setAgentInfo,
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
    isAgentProcessing,
    directMessages,
    teamMessages,
    hasTeamComms,
    setMessages,
    refreshOrgTree,
  } = useChatMessages({
    agentId,
    streamDraft,
    streamDraftRef,
    updateStreamDraft,
    isStreaming,
    setIsStreaming,
    setThinkingElapsed,
    activeAgentIdRef,
  });

  const lastTeamCountRef = useRef<number | null>(null);
  const lastBadgeAgentRef = useRef<string | null>(null);
  const [badgePopToken, setBadgePopToken] = useState(0);
  useEffect(() => {
    let token = badgePopToken;
    if (lastBadgeAgentRef.current !== agentId) {
      lastBadgeAgentRef.current = agentId;
      lastTeamCountRef.current = null;
      token = 0;
    }
    const next = nextBadgePopToken(lastTeamCountRef.current, teamMessages.length, token);
    lastTeamCountRef.current = next.lastSeen;
    if (next.token !== badgePopToken) setBadgePopToken(next.token);
  }, [agentId, teamMessages.length, badgePopToken]);

  const sendApi = useChatSend({
    agentId,
    activeAgentIdRef,
    streamDraftRef,
    updateStreamDraft,
    isStreaming,
    setIsStreaming,
    isAgentProcessing,
    loadMessagesFromDb,
    setMessages,
    refreshOrgTree,
    thinkingElapsed,
    setThinkingElapsed,
    stickToBottomRef,
  });

  // Send queue is per-agent (entries tagged in useChatSend): switching chats
  // parks the previous agent's queued messages instead of clearing or draining
  // them into the newly viewed agent. queuedCount syncs inside useChatSend.

  useAgentChannelLifecycle({
    agentId,
    streamAbortRef: sendApi.streamAbortRef,
    abortControllerRef: sendApi.abortControllerRef,
    responseTimeoutRef: sendApi.responseTimeoutRef,
    isStreaming,
    updateStreamDraft,
    setIsStreaming,
    setRetryInfo: sendApi.setRetryInfo,
  });

  const [agentInfoCache, setAgentInfoCache] = useState<
    Record<string, { name: string; position?: string; role?: string }>
  >({});

  const counterpartIds = useMemo(() => {
    const ids = new Set<string>();
    // 主栏入站信件（digest 来源 agent）名字解析。"用户"是 message_user
    // 收据信件的虚拟来源（非真实 agent id），请求会 400——与 system 一同排除
    for (const msg of directMessages) {
      if (
        msg.fromAgentId &&
        msg.fromAgentId !== "system" &&
        msg.fromAgentId !== "用户"
      )
        ids.add(msg.fromAgentId);
    }
    for (const msg of teamMessages) {
      // "system"/"用户" 是虚拟通知源（见 resolveAgentInfo），非真实 agent——
      // 跳过，避免对 /api/org/agents/{虚拟名} 发起无效请求（400 循环轰炸）。
      if (msg.teamFromAgentId && msg.teamFromAgentId !== "system" && msg.teamFromAgentId !== "用户")
        ids.add(msg.teamFromAgentId);
      if (msg.teamToAgentId && msg.teamToAgentId !== "system" && msg.teamToAgentId !== "用户")
        ids.add(msg.teamToAgentId);
      const targetId = getDirectedAgentId(msg, agentInfo?.parentId);
      if (targetId && targetId !== "system" && targetId !== "用户") ids.add(targetId);
    }
    return ids;
  }, [directMessages, teamMessages, agentInfo]);

  useEffect(() => {
    if (agentInfo && agentId && agentInfo.id === agentId) {
      const next = {
        name: agentInfo.name,
        position: agentInfo.position,
        role: agentInfo.role,
      };
      setAgentInfoCache((prev) => {
        const cur = prev[agentId];
        if (
          cur &&
          cur.name === next.name &&
          cur.role === next.role &&
          cur.position === next.position
        ) {
          return prev;
        }
        return { ...prev, [agentId]: next };
      });
    }
    setAgentInfoCache((currentCache) => {
      const idsToFetch: string[] = [];
      for (const id of counterpartIds) {
        if (!currentCache[id]) idsToFetch.push(id);
      }
      if (idsToFetch.length === 0) return currentCache;
      for (const id of idsToFetch) {
        getAgent(id)
          .then((raw) => {
            const data =
              raw && typeof raw === "object" && "agent" in raw && (raw as any).agent
                ? (raw as any).agent
                : raw;
            if (data?.name && (!data.id || data.id === id)) {
              setAgentInfoCache((prev) => ({
                ...prev,
                [id]: { name: data.name, position: data.position, role: data.role },
              }));
            }
          })
          .catch(() => {});
      }
      return currentCache;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [counterpartIds, agentInfo, agentId]);

  const handleDelete = useCallback(async () => {
    if (!agentId) return;
    if (!confirmingDelete) {
      setConfirmingDelete(true);
      return;
    }
    try {
      let actorId: string | undefined;
      let cur: any = agentInfo;
      if (!cur || cur.id !== agentId) {
        cur = await getAgent(agentId);
      }
      let guard = 0;
      while (cur?.parentId && guard++ < 24) {
        cur = await getAgent(cur.parentId);
      }
      actorId = cur?.id || agentInfo?.parentId || undefined;
      await deleteAgent(agentId, actorId);
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
  }, [
    agentId,
    agentInfo,
    confirmingDelete,
    refreshOrgTree,
    setConfirmingDelete,
    setAgentInfo,
    setMessages,
    updateStreamDraft,
  ]);

  if (!agentId) {
    return (
      <div className="h-full flex items-center justify-center bg-g-bg">
        <ChatMotionStyles />
        <div className="text-center hw-msg-in">
          <div
            className="w-16 h-16 mx-auto mb-4 rounded-2xl flex items-center justify-center shadow-gm-sm border border-g-blue/15"
            style={{ background: "linear-gradient(135deg, #eceefb 0%, #e0e3f8 100%)" }}
          >
            <svg className="w-8 h-8 text-g-blue" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.8}
                d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
              />
            </svg>
          </div>
          <p className="text-g-fg-3 text-sm font-medium">选择一个 Agent 开始对话</p>
          <p className="text-g-fg-4 text-xs mt-1">从左侧组织架构或办公室视图中挑选成员</p>
        </div>
      </div>
    );
  }

  const statusInfo = statusLabels[agentInfo?.status || "idle"] || {
    text: agentInfo?.status || "Unknown",
    color: "text-g-fg-3",
  };
  const runtimeStatusInfo =
    disposition && statusLabels[disposition]
      ? statusLabels[disposition]
      : agentInfo?.status === "active"
        ? isAgentProcessing
          ? { text: "实现中", color: "text-emerald-600" }
          : { text: "空闲", color: "text-g-fg-3" }
        : statusInfo;

  const resolveAgentInfo = (id: string) => {
    if (!id) return { name: "系统", role: "" };
    if (agentInfoCache[id]) return agentInfoCache[id];
    if (agentInfo && id === agentId)
      return { name: agentInfo.name, position: agentInfo.position, role: agentInfo.role };
    if (id === "system") return { name: "系统通知" };
    return { name: id.slice(0, 8) + "…", role: "" };
  };

  const roleDots: Record<string, string> = {
    ceo: "bg-amber-400",
    hr: "bg-rose-400",
    architect: "bg-purple-400",
    manager: "bg-blue-400",
    pm: "bg-blue-400",
    developer: "bg-green-400",
    module_dev: "bg-green-400",
    test_engineer: "bg-yellow-400",
    code_reviewer: "bg-indigo-400",
    security_auditor: "bg-red-400",
    web_perf_auditor: "bg-cyan-400",
    qa: "bg-yellow-400",
    devops: "bg-cyan-400",
  };

  const {
    input,
    setInput,
    images,
    queuedCount,
    retryInfo,
    showApprovalDialog,
    setShowApprovalDialog,
    pendingApprovalTool,
    setPendingApprovalTool,
    fileInputRef,
    textareaRef,
    onCompositionStart,
    onCompositionEnd,
    handlePaste,
    handleFileInput,
    removeImage,
    handleSend,
    handleInsert,
    handleStop,
    handleKeyDown,
  } = sendApi;

  return (
    <div className="h-full flex flex-col bg-white" style={hidden ? { display: "none" } : undefined}>
      <ChatMotionStyles />
      {agentInfo && (
        <div className="px-4 py-3 border-b border-g-border shrink-0 bg-white">
          <div className="flex items-center gap-3">
            <div
              className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 text-sm font-bold text-white shadow-gm-sm ${
                agentInfo.role === "ceo"
                  ? "bg-amber-500"
                  : agentInfo.role === "hr"
                    ? "bg-rose-500"
                    : agentInfo.role === "architect"
                      ? "bg-purple-500"
                      : agentInfo.role === "manager" || agentInfo.role === "pm"
                        ? "bg-g-blue"
                        : agentInfo.role === "developer" || agentInfo.role === "module_dev"
                          ? "bg-g-green"
                          : "bg-g-fg-3"
              }`}
            >
              {agentInfo.name.charAt(0)}
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-g-fg truncate">{agentInfo.name}</span>
                <span
                  className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                    isAgentProcessing
                      ? "bg-emerald-500 hw-status-live"
                      : agentInfo.status === "idle" || agentInfo.status === "inactive"
                        ? "bg-gray-400"
                        : agentInfo.status === "promoted"
                          ? "bg-blue-400"
                          : agentInfo.status === "receiving"
                            ? "bg-amber-400 animate-pulse"
                            : agentInfo.status === "merging"
                              ? "bg-purple-400 animate-pulse"
                              : agentInfo.status === "dissolving" || agentInfo.status === "archived"
                                ? "bg-red-500"
                                : "bg-gray-400"
                  }`}
                />
                <span className={`text-[11px] shrink-0 ${runtimeStatusInfo.color}`}>
                  {runtimeStatusInfo.text}
                </span>
              </div>
              <span className="text-xs text-g-fg-3">
                {roleLabels[agentInfo.role] || agentInfo.role}
              </span>
            </div>
          </div>
        </div>
      )}

      <TodoBar agentId={agentId} />

      <div
        ref={scrollContainerRef}
        onScroll={handleMessagesScroll}
        className="flex-1 min-h-0 overflow-y-auto px-5 py-4 space-y-1"
      >
        {directMessages.length === 0 && !hasTeamComms && (
          <div className="text-center text-g-fg-4 text-sm mt-12">发送消息开始对话</div>
        )}
        {loadingOlder && (
          <div className="text-center text-g-fg-4 text-xs py-2">加载更早消息…</div>
        )}
        {directMessages.map((msg) => (
          <MessageBubble
            key={msg.id}
            msg={msg}
            isStreaming={
              !!msg.isStreaming || (isStreaming && streamDraft?.assistantId === msg.id)
            }
            thinkingElapsed={
              isStreaming && streamDraft?.assistantId === msg.id ? thinkingElapsed : null
            }
            streamStartedAt={
              isStreaming && streamDraft?.assistantId === msg.id
                ? streamDraft.startedAt
                : undefined
            }
            sourceName={
              msg.fromAgentId && msg.fromAgentId !== "system" && msg.fromAgentId !== "用户"
                ? resolveAgentInfo(msg.fromAgentId).name
                : undefined
            }
            agentName={agentInfo && agentInfo.id === agentId ? agentInfo.name : undefined}
          />
        ))}
        {pendingApprovalTool && isStreaming && (
          <div className="flex justify-start hw-msg-in">
            <div className="max-w-[80%] rounded-2xl px-4 py-3 bg-g-yellow-bg border border-g-yellow/70 shadow-gm-sm">
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-amber-500 animate-pulse shrink-0" />
                <span className="text-sm text-amber-700">
                  等待审批: {pendingApprovalTool.replace(/^hiveweave__/, "").replace(/_/g, " ")}
                </span>
              </div>
            </div>
          </div>
        )}
        {retryInfo && isStreaming && (
          <div className="flex justify-start hw-msg-in">
            <div className="max-w-[80%] rounded-2xl px-4 py-3 bg-g-bg-muted border border-g-border shadow-gm-sm">
              <div className="flex items-center gap-2">
                <svg className="w-4 h-4 text-orange-500 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                  />
                </svg>
                <span className="text-sm text-orange-600">
                  重试中... {retryInfo.attempt}/{retryInfo.maxRetries}
                </span>
                <span className="text-xs text-orange-500/70">{retryInfo.reason}</span>
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {hasTeamComms && (
        <div className="shrink-0 border-t border-g-border bg-g-bg-soft overflow-hidden">
          <button
            onClick={() => {
              setTeamCommsExpanded(!teamCommsExpanded);
              if (teamCommsExpanded) setExpandedMessageId(null);
            }}
            className="w-full px-4 py-2.5 flex items-center justify-between hover:bg-g-bg-muted transition-colors"
          >
            <div className="flex items-center gap-2">
              <svg className="w-3.5 h-3.5 text-g-fg-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M17 8h2a2 2 0 012 2v6a2 2 0 01-2 2h-2v4l-4-4H9a1.994 1.994 0 01-1.414-.586m0 0L11 14h4a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2v4l.586-.586z"
                />
              </svg>
              <span className="text-xs font-semibold text-g-fg-2 uppercase tracking-wide">团队沟通</span>
              <span
                className={`bg-g-blue text-white text-[10px] font-bold px-1.5 py-0.5 rounded-full leading-none shadow-gm-sm${badgePopToken > 0 ? " hw-badge-pop" : ""}`}
                key={badgePopToken}
              >
                {teamMessages.length}
              </span>
            </div>
            <svg
              className={`w-3.5 h-3.5 text-g-fg-4 transition-transform duration-200 ${teamCommsExpanded ? "rotate-180" : ""}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2.5}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          {teamCommsExpanded && (
            <div className="hw-sec-in max-h-[35vh] overflow-y-auto overflow-x-hidden py-1">
              {[...teamMessages]
                .sort((a, b) => b.timestamp - a.timestamp)
                .map((msg) => {
                  const isTeamMsg = msg.role === "team";
                  const isUserMsg = msg.role === "user" && !msg.isBackground;
                  const isBgIncoming = msg.isBackground && msg.role === "user";

                  let isIncoming: boolean;
                  let counterpartId: string | null;

                  if (isTeamMsg) {
                    if (msg.teamToAgentId === agentId && msg.teamFromAgentId !== agentId) {
                      isIncoming = true;
                      counterpartId = msg.teamFromAgentId ?? null;
                    } else if (msg.teamFromAgentId === agentId && msg.teamToAgentId !== agentId) {
                      isIncoming = false;
                      counterpartId = msg.teamToAgentId ?? null;
                    } else {
                      isIncoming = msg.teamToAgentId === agentId;
                      counterpartId = isIncoming
                        ? (msg.teamFromAgentId ?? null)
                        : (msg.teamToAgentId ?? null);
                    }
                  } else if (isBgIncoming) {
                    isIncoming = true;
                    counterpartId = msg.teamFromAgentId ?? null;
                  } else {
                    isIncoming = true;
                    counterpartId = null;
                  }

                  const fromName = isIncoming
                    ? isUserMsg
                      ? { name: userName || "操作员", position: "操作员", role: "" }
                      : counterpartId
                        ? resolveAgentInfo(counterpartId)
                        : { name: "系统", role: "" }
                    : counterpartId
                      ? resolveAgentInfo(counterpartId)
                      : agentInfo
                        ? {
                            name: agentInfo.name,
                            role: agentInfo.role,
                            position: agentInfo.position,
                          }
                        : { name: "未知", role: "" };

                  const info = fromName;
                  const roleStyle = getRoleStyle(info.role || "");
                  const positionLabel = getPositionLabel(info.position, info.role);
                  const dotColor = roleDots[info.role || ""] || "bg-gray-400";
                  const directionTag = isIncoming ? "收到" : "发送";
                  const preview = (msg.content || "").trim() || "（无正文）";
                  const isExpanded = expandedMessageId === msg.id;
                  return (
                    <button
                      key={msg.id}
                      onClick={() => setExpandedMessageId(isExpanded ? null : msg.id)}
                      className={
                        "w-full px-4 py-2 text-left hover:bg-g-bg-muted transition-colors " +
                        (!msg.isRead ? "bg-g-blue/5 shadow-[inset_2px_0_0_0_#4f46e5] " : "")
                      }
                    >
                      <div className="flex items-center gap-2 mb-0.5 min-w-0">
                        <span
                          className={
                            "text-xs font-medium px-1.5 py-0.5 rounded shrink-0 " +
                            (isIncoming ? "bg-g-green-bg text-g-green" : "bg-g-blue-bg text-g-blue")
                          }
                        >
                          {directionTag}
                        </span>
                        <span className={`w-2 h-2 rounded-full shrink-0 ${dotColor}`} />
                        <span className="text-sm font-medium text-g-fg truncate min-w-0">{info.name}</span>
                        {positionLabel && (
                          <span
                            className={`text-[10px] font-medium px-2 py-0.5 rounded-full shrink-0 ${roleStyle.bg} ${roleStyle.text}`}
                          >
                            {positionLabel}
                          </span>
                        )}
                        {!msg.isRead && (
                          <span className="text-xs text-g-blue font-medium shrink-0">未读</span>
                        )}
                      </div>
                      <p
                        className={
                          "text-xs text-g-fg-4 " +
                          (isExpanded ? "whitespace-pre-wrap break-words" : "truncate")
                        }
                      >
                        {preview}
                      </p>
                    </button>
                  );
                })}
            </div>
          )}
        </div>
      )}

      <div className="px-4 py-3 border-t border-g-border bg-white shrink-0">
        {images.length > 0 && (
          <div className="flex gap-2 mb-2 flex-wrap">
            {images.map((url, i) => (
              <div key={i} className="relative group hw-msg-in">
                <img
                  src={url}
                  className="h-16 w-16 object-cover rounded-lg border border-g-border shadow-gm-sm"
                  alt=""
                />
                <button
                  onClick={() => removeImage(i)}
                  className="absolute -top-1.5 -right-1.5 w-5 h-5 bg-red-500 text-white rounded-full text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-all shadow-gm-sm hover:scale-110"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        {queuedCount > 0 && (
          <p className="flex items-center gap-1.5 w-fit text-xs text-amber-700 bg-g-yellow-bg border border-g-yellow/50 rounded-full px-3 py-1 mb-2">
            <svg className="w-3 h-3 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l2 2m6-2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            已排队 {queuedCount} 条消息，将在当前回复完成后自动发送
          </p>
        )}
        <div className="flex items-end gap-2">
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleFileInput}
            accept="image/*"
            multiple
            className="hidden"
          />
          <div className="flex flex-1 items-end rounded-gmLg border border-g-border bg-g-bg-soft transition-all focus-within:border-g-blue focus-within:ring-2 focus-within:ring-g-blue/20">
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={images.length >= 5 || isStreaming}
              className="shrink-0 px-2.5 py-3 rounded-l-gmLg text-g-fg-3 hover:text-g-blue hover:bg-g-bg-muted disabled:opacity-30 transition-colors"
              title="添加图片 (支持粘贴/拖拽)"
            >
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <circle cx="8.5" cy="8.5" r="1.5" />
              <path d="M21 15l-5-5L5 21" />
            </svg>
          </button>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            onCompositionStart={onCompositionStart}
            onCompositionEnd={onCompositionEnd}
            placeholder="输入消息... (Enter 发送, Shift+Enter 换行, 支持粘贴图片)"
            className="flex-1 bg-transparent border-0 px-1 py-3 text-sm text-g-fg leading-relaxed resize-none outline-none overflow-y-auto min-h-[46px] max-h-[200px] placeholder:text-g-fg-4/60"
          />
          </div>
          {(isStreaming || isAgentProcessing) && (
            <button
              onClick={handleInsert}
              disabled={!input.trim()}
              className="px-4 py-2.5 bg-g-bg-soft border border-g-blue/40 text-g-blue hover:bg-g-blue/10 rounded-gm text-sm font-medium shadow-gm-sm transition-all hover:shadow-gm active:scale-95 disabled:opacity-40 disabled:cursor-not-allowed"
              title="立即插入对话，不等待当前工作完成"
            >
              插入
            </button>
          )}
          <button
            onClick={handleSend}
            disabled={!input.trim() && images.length === 0}
            className="px-5 py-2.5 bg-g-blue hover:bg-indigo-700 text-white rounded-gm text-sm font-medium shadow-gm-sm transition-all hover:shadow-gm active:scale-95 disabled:opacity-40 disabled:shadow-none"
          >
            发送
          </button>
          <button
            onClick={handleStop}
            disabled={!isStreaming}
            className="px-5 py-2.5 bg-white border border-g-border text-g-fg-2 hover:text-g-red hover:border-g-red/40 hover:bg-g-red-bg/50 rounded-gm text-sm font-medium shadow-gm-sm transition-all active:scale-95 disabled:opacity-30 disabled:cursor-not-allowed disabled:shadow-none"
          >
            停止
          </button>
        </div>
      </div>

      {showApprovalDialog && agentId && (
        <ApprovalDialog
          agentId={agentId}
          onClose={() => {
            setShowApprovalDialog(false);
            setPendingApprovalTool(null);
          }}
        />
      )}
    </div>
  );
}

export default ChatPanel;
