import { useState, useRef, useCallback } from "react";
import type { StreamDraft } from "./types";

/**
 * RAF-throttled stream draft state.
 * Ref updates synchronously for event handlers; React state updates ≤60fps
 * so rapid delta bursts don't stutter the UI.
 */
export function useStreamDraft() {
  const [streamDraft, setStreamDraft] = useState<StreamDraft | null>(null);
  const streamDraftRef = useRef<StreamDraft | null>(null);
  const rafPendingRef = useRef(false);

  const updateStreamDraft = useCallback(
    (updater: StreamDraft | null | ((prev: StreamDraft | null) => StreamDraft | null)) => {
      const next = typeof updater === "function" ? updater(streamDraftRef.current) : updater;
      streamDraftRef.current = next;
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

  return { streamDraft, streamDraftRef, updateStreamDraft, setStreamDraft };
}
