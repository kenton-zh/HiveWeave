import { useEffect, useRef, type MutableRefObject } from "react";
import { joinAgentChannel, leaveAgentChannel } from "../api";
import { useAppStore } from "../store";
import type { StreamDraft } from "./types";

type UpdateStreamDraft = (
  updater: StreamDraft | null | ((prev: StreamDraft | null) => StreamDraft | null)
) => void;

/**
 * WebSocket channel + stream lifecycle.
 *
 * CRITICAL: abort / leave / clear timeout run ONLY on `[agentId]` cleanup.
 * Mount / orgTreeVersion re-runs of other effects must NOT kill the stream.
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

  // Manage WebSocket channel + stream lifecycle — abort stream, clear timeout,
  // and leave channel ONLY when agentId actually changes, not when
  // loadMessagesFromDb or orgTreeVersion triggers a re-run of the main mount
  // effect. This prevents the "stops after one sentence" bug where the stream
  // gets killed mid-response because lobby:status or org tree refresh causes
  // the mount effect to re-run.
  useEffect(() => {
    return () => {
      if (agentId) {
        streamAbortRef.current?.();
        streamAbortRef.current = null;
        abortControllerRef.current?.abort();
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
  useEffect(() => {
    if (socketReconnectVersion === prevReconnectVersion.current) return;
    prevReconnectVersion.current = socketReconnectVersion;
    if (socketReconnectVersion > 1) {
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
