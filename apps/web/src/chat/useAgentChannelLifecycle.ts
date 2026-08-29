import { useEffect, useRef, type MutableRefObject } from "react";
import { joinAgentChannel, leaveAgentChannel } from "../api";
import { useAppStore } from "../store";
import type { StreamDraft } from "./types";

type UpdateStreamDraft = (
  updater: StreamDraft | null | ((prev: StreamDraft | null) => StreamDraft | null)
) => void;

/**
 * WebSocket channel + local stream-UI lifecycle.
 *
 * CRITICAL: leave / clear timeout / drop abort handles run ONLY on `[agentId]`
 * cleanup. Do NOT push WS "cancel" on agent switch (BUG-034 / TEST6) — that
 * killed background trigger resumes. Explicit Stop uses handleStop instead.
 * Mount / orgTreeVersion re-runs of other effects must NOT tear down the channel.
 */
export function useAgentChannelLifecycle(opts: {
  agentId: string | null;
  streamAbortRef: MutableRefObject<(() => void) | null>;
  abortControllerRef: MutableRefObject<AbortController | null>;
  responseTimeoutRef: MutableRefObject<ReturnType<typeof setTimeout> | null>;
  isStreaming: boolean;
  updateStreamDraft: UpdateStreamDraft;
  setIsStreaming: (v: boolean) => void;
  setRetryInfo: (v: { attempt: number; maxRetries: number; reason: string } | null) => void;
}) {
  const {
    agentId,
    streamAbortRef,
    abortControllerRef,
    responseTimeoutRef,
    isStreaming,
    updateStreamDraft,
    setIsStreaming,
    setRetryInfo,
  } = opts;

  const processingAgents = useAppStore((s) => s.processingAgents);
  const socketReconnectVersion = useAppStore((s) => s.socketReconnectVersion);
  const prevReconnectVersion = useRef(0);

  // Pre-join the agent channel when the chat panel mounts / agent changes.
  useEffect(() => {
    if (!agentId) return;
    joinAgentChannel(agentId).catch(() => {});
  }, [agentId]);

  // Manage WebSocket channel + local UI lifecycle when agentId changes.
  // BUG-034 / TEST6: do NOT call streamAbortRef (pushes WS "cancel") on switch —
  // leaveAgentChannel also skips cancel. Switching agents must not kill a
  // background / trigger resume turn. Explicit Stop still goes through handleStop.
  useEffect(() => {
    return () => {
      if (agentId) {
        // Drop stale abort handle so a later remount cannot cancel a new turn.
        streamAbortRef.current = null;
        abortControllerRef.current?.abort();
        abortControllerRef.current = null;
        if (responseTimeoutRef.current) {
          clearTimeout(responseTimeoutRef.current);
          responseTimeoutRef.current = null;
        }
        leaveAgentChannel(agentId);
      }
    };
  }, [agentId, streamAbortRef, abortControllerRef, responseTimeoutRef]);

  // Reset stale streaming state when WebSocket reconnects.
  // BUG-033: Don't clear streamDraft entirely — persist it so the streamed
  // content doesn't vanish. The DB load on next user action will reconcile it.
  // socketReconnectVersion 只由真实 socket 重连 bump（App onOpen 钩子），
  // 0 = 尚未发生过重连，任何 ≥1 的变化都是重连信号。
  useEffect(() => {
    if (socketReconnectVersion === prevReconnectVersion.current) return;
    prevReconnectVersion.current = socketReconnectVersion;
    if (socketReconnectVersion > 0) {
      const stillProcessing = agentId ? processingAgents.includes(agentId) : false;
      if (!stillProcessing && isStreaming) {
        updateStreamDraft((prev) => (prev ? { ...prev, persisted: true } : prev));
        setIsStreaming(false);
        setRetryInfo(null);
        if (responseTimeoutRef.current) {
          clearTimeout(responseTimeoutRef.current);
          responseTimeoutRef.current = null;
        }
      }
    }
  }, [
    socketReconnectVersion,
    agentId,
    processingAgents,
    isStreaming,
    updateStreamDraft,
    setIsStreaming,
    setRetryInfo,
    responseTimeoutRef,
  ]);
}
