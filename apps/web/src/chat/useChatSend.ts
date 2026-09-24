import {
  useState,
  useRef,
  useEffect,
  useCallback,
  useMemo,
  type MutableRefObject,
  type Dispatch,
  type SetStateAction,
} from "react";
import { streamChat, joinAgentChannel, pushInsert, getChatMessages } from "../api";
import { mergeDeltaContent } from "../utils/mergeDelta";
import { useAppStore } from "../store";
import type { ChatMessage, StreamDraft } from "./types";
import {
  enqueuePending,
  entriesForAgent,
  hasQueuedForAgent,
  hasSendableContent,
  normalizeProjectId,
  takeNextQueued,
  useQueueStore,
  type PendingMessage,
} from "./queueStore";
import { notifyRunEnded, registerRun, requestStop, unregisterRun } from "./stopRegistry";
import { appendToolCallSegment, applyToolResult, beginStreamRound, mapDbToChatMessages, parseToolUsePayload, settledMessageHasSegments } from "./messageUtils";

type UpdateStreamDraft = (
  updater: StreamDraft | null | ((prev: StreamDraft | null) => StreamDraft | null)
) => void;

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
  const [retryInfo, setRetryInfo] = useState<{
    attempt: number;
    maxRetries: number;
    reason: string;
  } | null>(null);
  const [showApprovalDialog, setShowApprovalDialog] = useState(false);
  const [pendingApprovalTool, setPendingApprovalTool] = useState<string | null>(null);

  // 队列唯一事实源在 queueStore（模块级，projectId+agentId 键控）——组件
  // 重挂（registry 以 key=agentId 重建）不丢队列。zustand v5 裸选择器必须
  // 返回稳定引用：entries 原样订阅，过滤放 useMemo；projectId 归一化成
  // 字符串（原始值选择器，引用稳定）。
  const projectId = useAppStore((s) => normalizeProjectId(s.selectedProjectId));
  const allQueueEntries = useQueueStore((s) => s.entries);
  const queueEntries = useMemo(
    () => entriesForAgent(allQueueEntries, projectId, agentId),
    [allQueueEntries, projectId, agentId]
  );
  /** 当前在飞的 turn 对应的队列条目（排队投递/插话才有；直接发送无条目）。 */
  const activeEntryIdRef = useRef<string | null>(null);
  const autoSendRef = useRef(false);
  const handleSendRef = useRef<() => void>(() => {});
  const sendingLockRef = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  /** streamChat's cancel handle — AbortController alone does not push WS cancel. */
  const streamAbortRef = useRef<(() => void) | null>(null);
  /** 输入框 auto-grow：随内容撑高，封顶后内部滚动。 */
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  /** IME 组合期保护：Enter 用于选候选词，绝不误发。Safari 在 compositionend 之后才收 keydown，故延迟复位。 */
  const composingRef = useRef(false);
  const responseTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const updateProcessingAgent = useAppStore((s) => s.updateProcessingAgent);
  const pendingInitialMessage = useAppStore((s) => s.pendingInitialMessage);

  /** 当前 turn 的队列条目落定：接受/失败只作用于本条，不碰其他排队。 */
  const settleActiveEntry = useCallback(
    (entryId: string | null, outcome: "accepted" | "failed", errorMessage?: string) => {
      if (!entryId) return;
      const store = useQueueStore.getState();
      if (outcome === "accepted") store.markAccepted(entryId);
      else store.markFailed(entryId, errorMessage || "发送失败");
    },
    []
  );

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
    const text = input.trim();
    const attachments = images.slice(); // 附件快照绑定本条消息（SR-07/T-05）

    if (!autoSendRef.current) {
      // 统一校验（SR-06，仅用户主动发送）：有正文**或**有附件即合法
      //（支持纯图片，后端接受空正文 + images）。发送按钮 disabled 用同一
      // 判据，两端一致。drain（autoSend）路径由队列包自带内容，不校验输入框。
      if (!hasSendableContent(text, attachments)) return;
      if (sendingLockRef.current) {
        // 忙线 → 完整消息包入列（模块级 store，不随组件销毁丢失），
        // 成功入列后才清输入框与附件（SR-08 同款时序）。
        enqueuePending({
          projectId,
          agentId,
          text,
          attachments,
          mode: "normal",
        });
        setInput("");
        setImages([]);
        return;
      }
    }

    let messageText: string;
    let messageImages: string[];
    let activeEntryId: string | null = null;
    if (autoSendRef.current) {
      // drain：取出本 agent 最早一条 queued-local（原子置 sending），附件
      // 用包内快照——绝不回读当前输入框，也绝不改投当前选中成员。
      autoSendRef.current = false;
      const entry = takeNextQueued(projectId, agentId);
      if (!entry) return;
      messageText = entry.text;
      messageImages = entry.attachments;
      activeEntryId = entry.clientMessageId;
      activeEntryIdRef.current = entry.clientMessageId;
    } else {
      messageText = text;
      messageImages = attachments;
      setInput("");
      setImages([]);
      if (isStreaming || isAgentProcessing) {
        enqueuePending({
          projectId,
          agentId,
          text,
          attachments,
          mode: "normal",
        });
        return;
      }
      activeEntryIdRef.current = null;
    }

    sendingLockRef.current = true;

    const sendingImages = messageImages;

    const sendingForAgentId = agentId;
    const isActiveSession = () => activeAgentIdRef.current === sendingForAgentId;
    let activeRunId: string | null = null;
    const clearStreamAbort = () => {
      // Stream finished (or abandoned): drop cancel handle so agent switch /
      // remount cannot push a stale WS "cancel" into a later turn (TEST6).
      // 停止注册表条目同步注销（切走再切回后点「停止」靠注册表存活，
      // run 收口必须清掉，否则残留条目会指向已结束的轮次）。
      streamAbortRef.current = null;
      if (activeRunId) unregisterRun(sendingForAgentId, activeRunId);
    };
    const releaseLockAndFinish = () => {
      sendingLockRef.current = false;
      activeEntryIdRef.current = null;
      clearStreamAbort();
      if (
        useQueueStore
          .getState()
          .entries.some(
            (e) => e.agentId === sendingForAgentId && e.state === "queued-local"
          )
      ) {
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
      settleActiveEntry(activeEntryId, "failed", "响应超时，未获服务器确认");
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
          images: sendingImages.length ? sendingImages : undefined,
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
          // round 号由 ws 层放在 data（0 起轮号，字符串）——交给
          // beginStreamRound 插入轮次分隔（首轮/缺号 no-op）。与被动路径
          // （useChatMessages）同款解析；此前主动流漏传轮号，用户发起的
          // turn live 阶段永远没有轮次分隔（B6 live==persisted 缺口）。
          const roundNum = parseInt(event.data, 10);
          updateStreamDraft((prev) =>
            prev ? beginStreamRound(prev, Number.isFinite(roundNum) ? roundNum : undefined) : prev
          );
          return;
        }
        if (event.type === "message_id") {
          try {
            const parsed = JSON.parse(event.data);
            if (parsed.role === "user" && parsed.id) {
              // 服务器已持久化本条用户消息 = 该队列条目 accepted 的权威 ack
              //（§14.4：accepted 只代表接收成功，不代表任务执行完成）。
              settleActiveEntry(activeEntryId, "accepted");
              setMessages((prev) => {
                const without = prev.filter((m) => m.id !== optimisticUserId);
                if (without.some((m) => m.id === parsed.id)) return without;
                return [
                  ...without,
                  {
                    id: parsed.id,
                    role: "user" as const,
                    content: messageText,
                    images: sendingImages.length ? sendingImages : undefined,
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
              updateStreamDraft({
                assistantId: parsed.id,
                segments: [],
                isBackground: false,
                startedAt: Date.now(),
              });
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
          updateStreamDraft({
            assistantId: placeholderId,
            // 修复：首个 delta 曾被丢弃（segments 空 + 分支无后续合并），
            // 现在直接入 segment，否则气泡首字缺失。
            segments: [{ type: "text", content: event.data }],
            isBackground: false,
            startedAt: Date.now(),
          });
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
              isBackground: false,
              startedAt: Date.now(),
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
              isBackground: false,
              startedAt: Date.now(),
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
          const parsed = parseToolUsePayload(event.data);
          if (!parsed) return;
          allToolsUsed.add(parsed.toolCall.tool);
          updateStreamDraft((prev) =>
            prev ? appendToolCallSegment(prev, parsed.toolCall, parsed.toolCallId) : prev
          );
        } else if (event.type === "tool_result") {
          // 工具完成 → spinner 即时转 ✓/✗（与被动订阅路径一致）
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
          // turn 权威收口：若停止在途 → 确认「已停止」（§14.5 后端确认）。
          if (sendingForAgentId) notifyRunEnded(sendingForAgentId, activeRunId);
          // message_id ack 可能早于 done 已落定；此处兜底补一次（幂等）。
          settleActiveEntry(activeEntryId, "accepted");
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
          // done 先于 draft 清空：结算本轮端到端耗时冻结到消息（与被动订阅
          // 路径 useChatMessages 的 done 结算同款）。DB 重载后由
          // loadMessagesFromDb 按 id 携带，会话内持续显示 tok/s。
          const finishedDraft = streamDraftRef.current;
          const genStats =
            finishedDraft && finishedDraft.startedAt
              ? { ms: Math.max(1, Date.now() - finishedDraft.startedAt) }
              : null;
          const finishedId = finishedDraft?.assistantId;
          // done 一到立即把 draft 切到 persisted 形态：isStreaming 翻 false 后
          // 未 persisted 的 draft 会被 mergeStreamDraftIntoMessages 忽略（退回
          // 平铺的中途快照行）——收口重试窗口内必须继续按结构化 draft 渲染，
          // 直到带 metadata.segments 的权威快照落地换掉它。id 守卫（P2-1）：
          // 翻转只打在本 done 对应的 turn 上，不碰新 turn 的 live draft。
          updateStreamDraft((prev) =>
            prev && prev.assistantId === finishedId ? { ...prev, persisted: true } : prev
          );
          // fetch-then-swap gate（TEST_DSH_44 Bug#1，与被动路径
          // useChatMessages.swapDraftWhenReady 同款）：后端一个 turn 有两次
          // done——streamer 收尾 done（core.py stream finally，早于
          // handle_completion 落库 metadata.segments）+ completion 落库后的
          // 权威 done。第一次到达时 DB 里还是中途快照（content=旁白拼接、
          // 无 segments），直接清 draft 会把整轮结构化渲染坍缩成平铺文本。
          // 只有重载回来的消息带 segments 才清 draft；gate 失败保留
          // persisted draft（结构化 settle 渲染），权威 done / 重试完成收口。
          const swapDraftWhenReady = async (attempt = 0): Promise<void> => {
            if (activeAgentIdRef.current !== sendingForAgentId) return; // 已切走，放弃
            const ok = await loadMessagesFromDb(sendingForAgentId);
            const sessionMsgs =
              (useAppStore.getState().chatSessions[sendingForAgentId] as ChatMessage[] | undefined) ??
              [];
            const ready = ok && settledMessageHasSegments(sessionMsgs, finishedId);
            if (!ready && attempt < 4) {
              window.setTimeout(() => void swapDraftWhenReady(attempt + 1), 400);
              return; // draft 留屏（结构化整轮视图），等带分段的权威快照
            }
            if (ready) {
              // id 守卫：重试窗口内可能已有新 turn 的 draft 在飞（旧 swap
              // 不得误清新 turn 的 live 视图）。
              updateStreamDraft((prev) => (prev && prev.assistantId !== finishedId ? prev : null));
            }
            if (genStats && finishedId) {
              setMessages((prev) =>
                prev.map((m) => (m.id === finishedId ? { ...m, _genStats: genStats } : m)),
              );
            }
          };
          void swapDraftWhenReady();
          setIsStreaming(false);
          releaseLockAndFinish();
        } else if (event.type === "busy") {
          setThinkingElapsed(null);
          if (responseTimeoutRef.current) {
            clearTimeout(responseTimeoutRef.current);
            responseTimeoutRef.current = null;
          }
          if (sendingForAgentId) updateProcessingAgent(sendingForAgentId, false);
          // 忙线拒绝：本条消息落 failed 保留原文可重试（SR-08），其余排队
          // 条目不再整体清空（旧行为会把该成员整条队列抹掉）。
          if (activeEntryId) {
            settleActiveEntry(activeEntryId, "failed", "成员忙，消息未被接受，可重试");
            updateStreamDraft(null);
            setIsStreaming(false);
            setRetryInfo(null);
            autoSendRef.current = false;
            sendingLockRef.current = false;
            clearStreamAbort();
            activeEntryIdRef.current = null;
          } else {
            // 直接发送（无队列条目）被拒 → 退回输入框，不静默丢失。
            setInput(messageText);
            setImages(sendingImages);
            updateStreamDraft(null);
            setIsStreaming(false);
            setRetryInfo(null);
            sendingLockRef.current = false;
            clearStreamAbort();
          }
        } else if (event.type === "error") {
          setThinkingElapsed(null);
          // turn 错误收口：与 done 同款确认「已停止」（停止在途时）。
          if (sendingForAgentId) notifyRunEnded(sendingForAgentId, activeRunId);
          if (responseTimeoutRef.current) {
            clearTimeout(responseTimeoutRef.current);
            responseTimeoutRef.current = null;
          }
          setRetryInfo(null);
          if (sendingForAgentId) updateProcessingAgent(sendingForAgentId, false);
          // 与 done 同款 gate（TEST_DSH_44 Bug#1）：错误收口的 DB 行
          // （[对话被中断]/部分旁白）没有 metadata.segments——清 draft 换平铺
          // 行会把流式期间可见的工具行/思考段坍缩。gate 失败保留 persisted
          // draft（结构化），下一 turn 的 message_id 重建 draft 自然交接。
          // erroredId 在入口捕获：fetch 往返间 draft 可能已换成新 turn 的。
          const erroredId = streamDraftRef.current?.assistantId;
          loadMessagesFromDb(sendingForAgentId).then((ok) => {
            const sessionMsgs =
              (useAppStore.getState().chatSessions[sendingForAgentId] as ChatMessage[] | undefined) ??
              [];
            // 条目仍在 sending（message_id ack 未到就收到 error）：后端在
            // agent.chat() 前必落库用户消息，用 DB 内容核对裁决——正文在 =
            // 已送达（补 accepted）；不在 = 投递未确认，落 failed 保原文可重试。
            if (activeEntryId) {
              const store = useQueueStore.getState();
              const entry = store.entries.find((e) => e.clientMessageId === activeEntryId);
              if (entry && entry.state === "sending") {
                const persisted =
                  ok &&
                  sessionMsgs.some(
                    (m) => m.role === "user" && m.content === messageText
                  );
                if (persisted) store.markAccepted(activeEntryId);
                else store.markFailed(activeEntryId, "未能确认送达，可重试");
              }
            }
            const hasSegments = ok && settledMessageHasSegments(sessionMsgs, erroredId);
            if (hasSegments) {
              updateStreamDraft((prev) => (prev && prev.assistantId !== erroredId ? prev : null));
            } else {
              updateStreamDraft((prev) =>
                prev && prev.assistantId === erroredId ? { ...prev, persisted: true } : prev
              );
            }
            setIsStreaming(false);
          });
          releaseLockAndFinish();
        }
      }
    );
    streamAbortRef.current = abortStream;
    // 停止注册表（模块级）：切走再切回后仍能对准本轮 cancel（§14.5）。
    activeRunId = registerRun(sendingForAgentId, abortStream);
  }, [
    agentId,
    input,
    images,
    isStreaming,
    isAgentProcessing,
    projectId,
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
    settleActiveEntry,
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
      enqueuePending({
        projectId,
        agentId: sendingForAgentId,
        text: message,
        attachments: [],
        mode: "normal",
      });
      handleSendRef.current();
    });
  }, [pendingInitialMessage, agentId, activeAgentIdRef, projectId]);

  // Drain queued messages when the VIEWED agent becomes idle. Entries for
  // other agents stay parked until their own chat is viewed and idle —
  // a message queued for agent A must never auto-send to agent B on switch.
  // Queue lives in the module store: this effect also re-fires when entries
  // change（重试把 failed 翻回 queued-local 时能接上）。sendingLock 在飞时
  // 不重复 drain —— 同空闲窗口的下一条由 done 的 300ms 重发接力（旧契约），
  // 避免把多条排队背靠背全部打出。
  useEffect(() => {
    if (!agentId || isStreaming || isAgentProcessing) return;
    if (sendingLockRef.current) return;
    if (!hasQueuedForAgent(queueEntries, projectId, agentId)) return;
    autoSendRef.current = true;
    handleSend();
  }, [agentId, isStreaming, isAgentProcessing, handleSend, queueEntries, projectId]);

  // 插话：AI 工作期间把消息直接注入运行中 turn 的 next-step 窗口，不排队。
  // SR-08：消息先以 sending 态入列（文本/附件已快照绑定），确认后才算成功；
  // 失败保留原文与附件在队列面板可重试。后端把插话落库为 user 消息，以
  // DB 内容核对作为「服务器已接收」的确认（不提前宣布成功）。
  const attemptInterrupt = useCallback(
    (entry: PendingMessage) => {
      const agent = entry.agentId;
      try {
        pushInsert(agent, entry.text, entry.attachments.length ? entry.attachments : undefined);
      } catch (err) {
        useQueueStore
          .getState()
          .markFailed(entry.clientMessageId, err instanceof Error ? err.message : "插话发送失败");
        return;
      }
      let settled = false;
      // persisted 直接作为裁决入参：DB 里查到同文 user 消息 = 服务器已接收。
      const check = (persisted: boolean) => {
        if (settled) return;
        const cur = useQueueStore
          .getState()
          .entries.find((e) => e.clientMessageId === entry.clientMessageId);
        if (!cur || cur.state !== "sending") {
          settled = true;
          return;
        }
        if (persisted) {
          settled = true;
          useQueueStore.getState().markAccepted(entry.clientMessageId);
        }
      };
      const persistedInDb = async (): Promise<boolean> => {
        try {
          const dbMessages = await getChatMessages(agent);
          if (!Array.isArray(dbMessages)) return false;
          return mapDbToChatMessages(dbMessages).some(
            (m) => m.role === "user" && m.content === entry.text,
          );
        } catch {
          return false;
        }
      };
      const reload = () => {
        // DB 核对不依赖当前激活会话：用户切走不代表插话没送达（切走后
        // loadMessagesFromDb 内部会守卫 no-op），改用裸拉取裁决，激活时
        // 顺带刷新会话视图。
        if (activeAgentIdRef.current === agent) {
          loadMessagesFromDb(agent).then(async (ok) => {
            check(ok && (await persistedInDb()));
          });
        } else {
          persistedInDb().then(check);
        }
      };
      window.setTimeout(reload, 300);
      window.setTimeout(reload, 1500);
      window.setTimeout(() => {
        if (settled) return;
        const cur = useQueueStore
          .getState()
          .entries.find((e) => e.clientMessageId === entry.clientMessageId);
        if (cur && cur.state === "sending") {
          useQueueStore
            .getState()
            .markFailed(entry.clientMessageId, "插话未获服务器确认，可重试");
        }
      }, 10_000);
    },
    [activeAgentIdRef, loadMessagesFromDb]
  );

  const handleInsert = useCallback(() => {
    if (!agentId) return;
    const text = input.trim();
    const attachments = images.slice();
    if (!hasSendableContent(text, attachments)) return;
    // 先入列（文本/附件快照绑定），入列成功后才清输入框——失败可从队列面板重试。
    const entry = enqueuePending({
      projectId,
      agentId,
      text,
      attachments,
      mode: "interrupt",
      initialState: "sending",
    });
    setInput("");
    setImages([]);
    attemptInterrupt(entry);
  }, [agentId, input, images, projectId, attemptInterrupt]);

  /** 队列面板「重试」：插话原地重发；普通消息翻回 queued-local 交给 drain。 */
  const retryQueued = useCallback(
    (clientMessageId: string) => {
      const entry = useQueueStore
        .getState()
        .entries.find((e) => e.clientMessageId === clientMessageId);
      if (!entry || entry.state !== "failed") return;
      if (entry.mode === "interrupt") {
        useQueueStore.getState().markSending(clientMessageId);
        attemptInterrupt({ ...entry, state: "sending", errorMessage: undefined });
      } else {
        useQueueStore.getState().requeue(clientMessageId);
      }
    },
    [attemptInterrupt]
  );

  /** 队列面板「取消/移除」：queued-local=取消未发消息；accepted=仅移除记录（不撤回）。 */
  const cancelQueued = useCallback((clientMessageId: string) => {
    useQueueStore.getState().remove(clientMessageId);
  }, []);

  /** 输入框 auto-grow：内容撑高到 MAX 封顶后内部滚动，参考 deepseek-harness 的 composer。 */
  const autoResizeTextarea = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    const MAX_GROW_PX = 200;
    el.style.removeProperty("height");
    el.style.height = `${Math.min(el.scrollHeight, MAX_GROW_PX)}px`;
  }, []);

  useEffect(() => {
    autoResizeTextarea();
  }, [input, autoResizeTextarea]);

  // Safari 在 compositionend 之后才交付闭合 keydown，延迟一个 tick 复位，避免闭合 Enter 被误发。
  const resetComposing = useCallback(() => {
    setTimeout(() => {
      composingRef.current = false;
    }, 10);
  }, []);

  const onCompositionStart = useCallback(() => {
    composingRef.current = true;
  }, []);

  const onCompositionEnd = useCallback(() => {
    resetComposing();
  }, [resetComposing]);

  // 部分 IME 在点击别处/Esc/中断时只触发 compositioncancel 而不触发 compositionend，
  // 若不复位 composingRef 会让后续 Enter 永远无法发送。React 未映射该事件，用原生监听挂到 textarea。
  useEffect(() => {
    const el = textareaRef.current;
    if (el === null) return;
    const onCancel = () => {
      composingRef.current = false;
    };
    el.addEventListener("compositioncancel", onCancel);
    return () => {
      el.removeEventListener("compositioncancel", onCancel);
    };
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey) return; // Shift+Enter 原生换行
    const composing =
      composingRef.current || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229;
    if (composing || e.repeat) return; // IME 选候选 / 长按连发 → 走原生
    e.preventDefault();
    handleSend();
  };

  const handleStop = useCallback(() => {
    if (!agentId) return;
    // 停止请求带 agentId 走模块级注册表（§14.5）：不依赖当前页面是不是
    // 最初发起流的页面——切走再切回，cancel 仍投到该 agent 当前存活的
    // channel。「正在停止」立即置位；「已停止」只等后端确认（done/error
    // 事件或 processing 权威回读），超时转 uncertain + 可重试，这里绝不
    // 提前宣布成功，也不做本地乐观清场（draft/流式态由 done/error 收口）。
    requestStop(agentId);
    // 本地句柄立即作废：后续 cancel 一律走 ws 层当前存活 channel 的
    // push("cancel")，不再碰陈旧 abort 闭包（它会误删被动订阅 handler）。
    streamAbortRef.current = null;
    // 停的对象是「本轮执行轮次」（§8.7）：该成员仍在本地暂存的排队条目随
    // 停止取消，避免停止后 idle 又被 drain 自动拉起新 turn；已 accepted 的
    // 不动（cancelled 不能假装撤销已送达的消息，§14.4）。
    useQueueStore.getState().removeQueuedLocalsForAgent(projectId, agentId);
  }, [agentId, projectId]);

  return {
    input,
    setInput,
    images,
    setImages,
    /** 本成员的队列条目（queueStore 订阅）——唯一事实源，面板直接渲染。 */
    queueEntries,
    retryInfo,
    setRetryInfo,
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
    /** 队列面板动作：重试 / 取消（accepted 仅移除记录）。 */
    retryQueued,
    cancelQueued,
    streamAbortRef,
    abortControllerRef,
    responseTimeoutRef,
  };
}
