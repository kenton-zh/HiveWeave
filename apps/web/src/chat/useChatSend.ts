import {
  useState,
  useRef,
  useEffect,
  useCallback,
  type MutableRefObject,
  type Dispatch,
  type SetStateAction,
} from "react";
import { streamChat, joinAgentChannel } from "../api";
import { mergeDeltaContent } from "../utils/mergeDelta";
import { useAppStore } from "../store";
import type { ChatMessage, StreamDraft, ToolCall } from "./types";
import { beginStreamRound } from "./messageUtils";

type UpdateStreamDraft = (
  updater: StreamDraft | null | ((prev: StreamDraft | null) => StreamDraft | null)
) => void;

/**
 * Queue entries are tagged with their intended recipient. ChatPanel is NOT
 * remounted on agent switch (stable key), so an untagged queue would drain
 * agent A's parked messages into agent B's chat the moment B is viewed idle.
 */
type QueuedMessage = { agentId: string; text: string };

/**
 * Send / queue / stop — preserves streamChat's abort handle on streamAbortRef.
 */
export function useChatSend(opts: {
  agentId: string | null;
  activeAgentIdRef: MutableRefObject<string | null>;
  streamDraftRef: MutableRefObject<StreamDraft | null>;
  updateStreamDraft: UpdateStreamDraft;
  isStreaming: boolean;
  setIsStreaming: (v: boolean) => void;
  isAgentProcessing: boolean;
  loadMessagesFromDb: (id: string) => Promise<boolean>;
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  refreshOrgTree: () => void;
  thinkingElapsed: number | null;
  setThinkingElapsed: (v: number | null) => void;
  stickToBottomRef: MutableRefObject<boolean>;
}) {
  const {
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
    setThinkingElapsed,
    stickToBottomRef,
  } = opts;

  const [input, setInput] = useState("");
  const [images, setImages] = useState<string[]>([]);
  const [queuedCount, setQueuedCount] = useState(0);
  const [retryInfo, setRetryInfo] = useState<{
    attempt: number;
    maxRetries: number;
    reason: string;
  } | null>(null);
  const [showApprovalDialog, setShowApprovalDialog] = useState(false);
  const [pendingApprovalTool, setPendingApprovalTool] = useState<string | null>(null);

  const pendingQueueRef = useRef<QueuedMessage[]>([]);
  const autoSendRef = useRef(false);
  const handleSendRef = useRef<() => void>(() => {});
  const sendingLockRef = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  /** streamChat's cancel handle — AbortController alone does not push WS cancel. */
  const streamAbortRef = useRef<(() => void) | null>(null);
  const responseTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const updateProcessingAgent = useAppStore((s) => s.updateProcessingAgent);
  const pendingInitialMessage = useAppStore((s) => s.pendingInitialMessage);

  /** Queued-count banner reflects the VIEWED agent only. */
  const syncQueuedCount = useCallback(() => {
    const current = activeAgentIdRef.current;
    setQueuedCount(
      current ? pendingQueueRef.current.filter((e) => e.agentId === current).length : 0
    );
  }, [activeAgentIdRef]);

  /** Remove and return the first queued entry for `id` (entries may be interleaved). */
  const shiftQueuedFor = useCallback((id: string): QueuedMessage | undefined => {
    const idx = pendingQueueRef.current.findIndex((e) => e.agentId === id);
    if (idx < 0) return undefined;
    return pendingQueueRef.current.splice(idx, 1)[0];
  }, []);

  const addImages = useCallback((files: FileList | File[]) => {
    const readers: Promise<string>[] = [];
    for (const file of Array.from(files)) {
      if (!file.type.startsWith("image/")) continue;
      readers.push(
        new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result as string);
          reader.readAsDataURL(file);
        })
      );
    }
    Promise.all(readers).then((urls) => {
      setImages((prev) => [...prev, ...urls].slice(0, 5));
    });
  }, []);

  const handlePaste = useCallback(
    (e: React.ClipboardEvent) => {
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
    },
    [addImages]
  );

  const handleFileInput = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (e.target.files) addImages(e.target.files);
      if (fileInputRef.current) fileInputRef.current.value = "";
    },
    [addImages]
  );

  const removeImage = useCallback((index: number) => {
    setImages((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleSend = useCallback(() => {
    if (!agentId) return;

    if (!autoSendRef.current && sendingLockRef.current) {
      if (input.trim()) {
        pendingQueueRef.current.push({ agentId, text: input.trim() });
        setInput("");
        syncQueuedCount();
      }
      return;
    }

    let messageText: string;
    if (autoSendRef.current) {
      autoSendRef.current = false;
      messageText = shiftQueuedFor(agentId)?.text || "";
      syncQueuedCount();
    } else {
      if (!input.trim()) return;
      messageText = input.trim();
      setInput("");
      if (isStreaming || isAgentProcessing) {
        pendingQueueRef.current.push({ agentId, text: messageText });
        syncQueuedCount();
        return;
      }
    }

    if (!messageText) return;

    sendingLockRef.current = true;

    const sendingImages = images;
    setImages([]);

    const sendingForAgentId = agentId;
    const isActiveSession = () => activeAgentIdRef.current === sendingForAgentId;
    const clearStreamAbort = () => {
      // Stream finished (or abandoned): drop cancel handle so agent switch /
      // remount cannot push a stale WS "cancel" into a later turn (TEST6).
      streamAbortRef.current = null;
    };
    const releaseLockAndFinish = () => {
      sendingLockRef.current = false;
      clearStreamAbort();
      if (pendingQueueRef.current.some((e) => e.agentId === sendingForAgentId)) {
        // If the user switched chats within the 300ms window, leave the entry
        // parked — the drain effect will send it when its own chat is viewed.
        setTimeout(() => {
          if (activeAgentIdRef.current !== sendingForAgentId) return;
          // A manual send may have started a stream within the window — never
          // run a second concurrent stream; its own completion re-arms this.
          if (sendingLockRef.current) return;
          autoSendRef.current = true;
          handleSend();
        }, 300);
      }
    };

    stickToBottomRef.current = true;
    setIsStreaming(true);
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
      releaseLockAndFinish(); // also clears streamAbortRef
    }, 300_000);
    const allToolsUsed = new Set<string>();
    let _dbgTextCount = 0;
    let _dbgFirstText = 0;
    const controller = new AbortController();
    abortControllerRef.current = controller;

    const optimisticUserId = `pending-user-${sendingForAgentId}-${Date.now()}`;
    setMessages((prev) => {
      // Delayed auto-send may fire after the user switched chats — never leak
      // another agent's bubble into the currently viewed message list.
      if (!isActiveSession()) return prev;
      if (prev.some((m) => m.id === optimisticUserId)) return prev;
      return [
        ...prev,
        {
          id: optimisticUserId,
          role: "user" as const,
          content: messageText,
          timestamp: Date.now(),
          isBackground: false,
          isRead: true,
        },
      ];
    });

    const { abort: abortStream } = streamChat(
      sendingForAgentId,
      messageText,
      sendingImages,
      (event) => {
        if (!isActiveSession()) return;
        if (event.type === "round_start") {
          updateStreamDraft((prev) => (prev ? beginStreamRound(prev) : prev));
          return;
        }
        if (event.type === "message_id") {
          try {
            const parsed = JSON.parse(event.data);
            if (parsed.role === "user" && parsed.id) {
              setMessages((prev) => {
                const without = prev.filter((m) => m.id !== optimisticUserId);
                if (without.some((m) => m.id === parsed.id)) return without;
                return [
                  ...without,
                  {
                    id: parsed.id,
                    role: "user" as const,
                    content: messageText,
                    timestamp: Date.now(),
                    isBackground: false,
                    isRead: true,
                  },
                ];
              });
            }
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
              updateStreamDraft({ assistantId: parsed.id, segments: [] });
              console.log(`[SSE] streamDraft initialized: assistantId=${parsed.id}`);
            }
          } catch {
            /* ignore */
          }
          loadMessagesFromDb(sendingForAgentId);
          return;
        }

        if ((event.type === "text" || event.type === "text_delta") && !streamDraftRef.current) {
          const placeholderId = `draft-${sendingForAgentId}-${Date.now()}`;
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
          updateStreamDraft({ assistantId: placeholderId, segments: [] });
          console.log(`[SSE] streamDraft lazy-initialized: assistantId=${placeholderId}`);
        } else if (event.type === "thinking") {
          setThinkingElapsed(event.elapsed_s ?? null);
        } else if (event.type === "text" || event.type === "text_delta") {
          setThinkingElapsed(null);
          _dbgTextCount++;
          if (_dbgTextCount === 1) _dbgFirstText = performance.now();
          if (_dbgTextCount <= 3 || _dbgTextCount % 20 === 0) {
            console.log(
              `[SSE] text #${_dbgTextCount}: ${event.data.length}chars, t=${(performance.now() - _dbgFirstText).toFixed(0)}ms`
            );
          }
          if (!streamDraftRef.current) {
            const placeholderId = `draft-${sendingForAgentId}-${Date.now()}`;
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
              segments: [{ type: "text", content: event.data }],
            });
            console.log(`[SSE] streamDraft lazy-initialized: assistantId=${placeholderId}`);
            return;
          }

          updateStreamDraft((prev) => {
            if (!prev) return prev;
            const last = prev.segments[prev.segments.length - 1];
            if (last && last.type === "text") {
              return {
                ...prev,
                segments: [
                  ...prev.segments.slice(0, -1),
                  { ...last, content: mergeDeltaContent(last.content || "", event.data) },
                ],
              };
            }
            return { ...prev, segments: [...prev.segments, { type: "text", content: event.data }] };
          });
        } else if (event.type === "thinking_delta") {
          setThinkingElapsed(null);
          if (!streamDraftRef.current) {
            const placeholderId = `draft-${sendingForAgentId}-${Date.now()}`;
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
              segments: [{ type: "thinking", content: event.data }],
            });
            return;
          }
          updateStreamDraft((prev) => {
            if (!prev) return prev;
            const last = prev.segments[prev.segments.length - 1];
            if (last && last.type === "thinking") {
              return {
                ...prev,
                segments: [
                  ...prev.segments.slice(0, -1),
                  { ...last, content: mergeDeltaContent(last.content || "", event.data) },
                ],
              };
            }
            return {
              ...prev,
              segments: [...prev.segments, { type: "thinking", content: event.data }],
            };
          });
        } else if (event.type === "tool_use") {
          setThinkingElapsed(null);
          try {
            const toolData = JSON.parse(event.data);
            const rawName: string = toolData.toolName || toolData.tool_name || toolData.tool || "";
            const toolName = rawName.replace(/^hiveweave__/, "");
            const argsRaw = toolData.arguments || toolData.input || {};
            const args = typeof argsRaw === "string" ? JSON.parse(argsRaw) : argsRaw;
            const toolCall: ToolCall = { tool: toolName, input: args };
            allToolsUsed.add(toolCall.tool);
            updateStreamDraft((prev) =>
              prev ? { ...prev, segments: [...prev.segments, { type: "tool_call", tool: toolCall }] } : prev
            );
          } catch {
            /* ignore */
          }
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
          } catch {
            /* ignore */
          }
        } else if (event.type === "queued_message") {
          loadMessagesFromDb(sendingForAgentId);
        } else if (event.type === "done") {
          setThinkingElapsed(null);
          console.log(
            `[SSE] done — total text events: ${_dbgTextCount}, elapsed: ${_dbgFirstText ? (performance.now() - _dbgFirstText).toFixed(0) : "N/A"}ms`
          );
          if (responseTimeoutRef.current) {
            clearTimeout(responseTimeoutRef.current);
            responseTimeoutRef.current = null;
          }
          setPendingApprovalTool(null);
          setRetryInfo(null);
          if (sendingForAgentId) updateProcessingAgent(sendingForAgentId, false);
          const ORG_TOOLS = new Set([
            "create_agent",
            "transfer_agent",
            "dismiss_agent",
            "create_from_template",
            "hire_agent",
          ]);
          if ([...allToolsUsed].some((x) => ORG_TOOLS.has(x))) refreshOrgTree();
          const draftContent =
            streamDraftRef.current?.segments
              ?.filter((s) => s.type === "text" || s.type === "thinking")
              ?.map((s) => s.content || "")
              ?.join("") || "";
          if (draftContent) {
            setMessages((prev) =>
              prev.map((m) =>
                m.isStreaming && m.role === "assistant" && !m.content
                  ? { ...m, content: draftContent }
                  : m
              )
            );
          }
          loadMessagesFromDb(sendingForAgentId).then((ok) => {
            if (ok) {
              updateStreamDraft(null);
            } else {
              updateStreamDraft((prev) => (prev ? { ...prev, persisted: true } : prev));
            }
            setIsStreaming(false);
          });
          releaseLockAndFinish();
        } else if (event.type === "busy") {
          setThinkingElapsed(null);
          if (responseTimeoutRef.current) {
            clearTimeout(responseTimeoutRef.current);
            responseTimeoutRef.current = null;
          }
          if (sendingForAgentId) updateProcessingAgent(sendingForAgentId, false);
          setInput(messageText);
          updateStreamDraft(null);
          setIsStreaming(false);
          setRetryInfo(null);
          pendingQueueRef.current = pendingQueueRef.current.filter(
            (e) => e.agentId !== sendingForAgentId
          );
          syncQueuedCount();
          autoSendRef.current = false;
          sendingLockRef.current = false;
          clearStreamAbort();
        } else if (event.type === "error") {
          setThinkingElapsed(null);
          if (responseTimeoutRef.current) {
            clearTimeout(responseTimeoutRef.current);
            responseTimeoutRef.current = null;
          }
          setRetryInfo(null);
          if (sendingForAgentId) updateProcessingAgent(sendingForAgentId, false);
          loadMessagesFromDb(sendingForAgentId).then((ok) => {
            if (ok) {
              updateStreamDraft(null);
            } else {
              updateStreamDraft((prev) => (prev ? { ...prev, persisted: true } : prev));
            }
            setIsStreaming(false);
          });
          releaseLockAndFinish();
        }
      }
    );
    streamAbortRef.current = abortStream;
  }, [
    agentId,
    input,
    images,
    isStreaming,
    isAgentProcessing,
    refreshOrgTree,
    loadMessagesFromDb,
    activeAgentIdRef,
    streamDraftRef,
    updateStreamDraft,
    setIsStreaming,
    setMessages,
    setThinkingElapsed,
    updateProcessingAgent,
    stickToBottomRef,
    syncQueuedCount,
    shiftQueuedFor,
  ]);

  handleSendRef.current = handleSend;

  // pendingInitialMessage — dedicated effect (must not cancel send on re-run)
  useEffect(() => {
    if (!pendingInitialMessage || !agentId) return;
    if (pendingInitialMessage.agentId !== agentId) return;

    const message = pendingInitialMessage.message;
    const sendingForAgentId = agentId;
    useAppStore.getState().setPendingInitialMessage(null);

    void joinAgentChannel(sendingForAgentId).finally(() => {
      if (activeAgentIdRef.current !== sendingForAgentId) return;
      autoSendRef.current = true;
      pendingQueueRef.current.push({ agentId: sendingForAgentId, text: message });
      handleSendRef.current();
    });
  }, [pendingInitialMessage, agentId, activeAgentIdRef]);

  // Queued-count banner is per-agent — recompute when switching chats.
  useEffect(() => {
    syncQueuedCount();
  }, [agentId, syncQueuedCount]);

  // Drain queued messages when the VIEWED agent becomes idle. Entries for
  // other agents stay parked until their own chat is viewed and idle —
  // a message queued for agent A must never auto-send to agent B on switch.
  useEffect(() => {
    if (!agentId || isStreaming || isAgentProcessing) return;
    if (!pendingQueueRef.current.some((e) => e.agentId === agentId)) return;
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
    streamAbortRef.current?.();
    streamAbortRef.current = null;
    abortControllerRef.current?.abort();
    setIsStreaming(false);
    updateStreamDraft(null);
    setRetryInfo(null);
    if (responseTimeoutRef.current) clearTimeout(responseTimeoutRef.current);
    if (agentId) updateProcessingAgent(agentId, false);
    if (pendingQueueRef.current.some((e) => e.agentId === agentId)) {
      pendingQueueRef.current = pendingQueueRef.current.filter((e) => e.agentId !== agentId);
      syncQueuedCount();
    }
  }, [agentId, setIsStreaming, updateStreamDraft, updateProcessingAgent, syncQueuedCount]);

  return {
    input,
    setInput,
    images,
    queuedCount,
    retryInfo,
    setRetryInfo,
    showApprovalDialog,
    setShowApprovalDialog,
    pendingApprovalTool,
    setPendingApprovalTool,
    fileInputRef,
    handlePaste,
    handleFileInput,
    removeImage,
    handleSend,
    handleStop,
    handleKeyDown,
    streamAbortRef,
    abortControllerRef,
    responseTimeoutRef,
  };
}
