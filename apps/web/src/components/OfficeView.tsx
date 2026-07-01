/**
 * OfficeView — Thin React host for the PixiJS Office Scene.
 *
 * Responsibilities:
 *   1. Mount the PixiJS canvas into a DOM ref.
 *   2. Bridge Zustand store state → OfficeScene.setSnapshot().
 *   3. Forward scene interactions (agent clicks) → Zustand actions.
 *   4. Handle ResizeObserver for responsive canvas sizing.
 *
 * Knows NOTHING about PixiJS rendering internals.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getCommunications, getOrgTree } from "../api";
import { useAppStore } from "../store";
import { OfficeScene } from "./office/OfficeScene";
import type { OfficeAgent, OfficeInteraction, SceneSnapshot } from "./office/types";

// ── Helpers ───────────────────────────────────────────────────────

function flattenAgents(nodes: OfficeAgent[]): OfficeAgent[] {
  const result: OfficeAgent[] = [];
  const visit = (node: OfficeAgent) => {
    result.push(node);
    node.children?.forEach(visit);
  };
  nodes.forEach(visit);
  return result;
}

// ── Component ─────────────────────────────────────────────────────

export default function OfficeView() {
  const hostRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<OfficeScene | null>(null);

  // ── Zustand selectors ──────────────────────────────────────────
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);
  const selectedAgentId = useAppStore((s) => s.selectedAgentId);
  const setSelectedAgent = useAppStore((s) => s.setSelectedAgent);
  const setRightPanelTab = useAppStore((s) => s.setRightPanelTab);
  const orgTreeVersion = useAppStore((s) => s.orgTreeVersion);
  const processingAgents = useAppStore((s) => s.processingAgents);
  const activeCommunications = useAppStore((s) => s.activeCommunications);
  const setActiveCommunications = useAppStore((s) => s.setActiveCommunications);
  const userPingAgentIds = useAppStore((s) => s.userPingAgentIds);

  // ── Local state (from API) ─────────────────────────────────────
  const [roots, setRoots] = useState<OfficeAgent[]>([]);
  const [error, setError] = useState<string | null>(null);

  // ── Derived data ───────────────────────────────────────────────
  const agents = useMemo(() => flattenAgents(roots), [roots]);

  const communicatingIds = useMemo(() => {
    const ids = new Set<string>();
    activeCommunications.forEach((comm) => {
      ids.add(comm.fromAgentId);
      ids.add(comm.toAgentId);
    });
    return ids;
  }, [activeCommunications]);

  // ── Interaction handler (PixiJS → React) ───────────────────────
  const handleInteraction = useCallback((event: OfficeInteraction) => {
    if (event.type === "select-agent") {
      setSelectedAgent(event.agentId);
      setRightPanelTab("chat");
    }
  }, [setSelectedAgent, setRightPanelTab]);

  // ── Scene lifecycle (mount / resize / destroy) ─────────────────
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;

    const scene = new OfficeScene(handleInteraction);
    sceneRef.current = scene;

    scene.mount(host).then(() => {
      if (disposed) return;
      const observer = new ResizeObserver(([entry]) => {
        scene.resize(entry.contentRect.width, entry.contentRect.height);
      });
      observer.observe(host);
      (scene as any).__observer = observer;
    }).catch((err) => {
      if (!disposed) {
        console.error("[OfficeView] Scene mount failed:", err);
        setError(`Office scene init error: ${err?.message || err}`);
      }
    });

    return () => {
      disposed = true;
      (scene as any).__observer?.disconnect?.();
      scene.destroy();
      sceneRef.current = null;
    };
  }, [handleInteraction]);

  // ── Load org tree for selected project ─────────────────────────
  useEffect(() => {
    let mounted = true;
    async function loadOffice() {
      if (!selectedProjectId) {
        setRoots([]);
        return;
      }
      try {
        const data = await getOrgTree(selectedProjectId);
        let parsed: any[];
        if (Array.isArray(data)) {
          parsed = data;
        } else if (data?.tree && Array.isArray(data.tree)) {
          parsed = data.tree;
        } else if (data?.id) {
          parsed = [data];
        } else {
          parsed = [];
        }
        if (mounted) {
          setRoots(parsed);
          setError(null);
        }
      } catch (err) {
        if (mounted) setError(err instanceof Error ? err.message : "Failed to load office");
      }
    }
    loadOffice();
    return () => { mounted = false; };
  }, [selectedProjectId, orgTreeVersion]);

  // ── Poll communications for talking indicators ─────────────────
  useEffect(() => {
    if (!selectedProjectId) return;
    let mounted = true;
    const poll = async () => {
      try {
        const comms = await getCommunications();
        if (mounted && Array.isArray(comms)) setActiveCommunications(comms);
      } catch {
        // Office renders fine without transient comm events
      }
    };
    poll();
    const interval = setInterval(poll, 3000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [selectedProjectId, setActiveCommunications]);

  // ── Push snapshot into scene every render ──────────────────────
  useEffect(() => {
    const snapshot: SceneSnapshot = {
      agents,
      selectedAgentId,
      communicatingIds,
      processingIds: new Set(processingAgents),
      userPingIds: new Set(userPingAgentIds),
    };
    sceneRef.current?.setSnapshot(snapshot);
  }, [agents, selectedAgentId, communicatingIds, processingAgents, userPingAgentIds]);

  // ── Render ─────────────────────────────────────────────────────
  return (
    <div className="relative h-full w-full overflow-hidden bg-[#09111f]">
      <div ref={hostRef} className="h-full w-full" />
      {error && (
        <div className="absolute top-4 left-4 rounded-md border border-red-500/40 bg-red-950/80 px-3 py-2 text-xs text-red-100 z-10">
          {error}
        </div>
      )}
      {!error && (
        <div className="absolute top-2 right-2 text-[10px] text-gray-600 z-10 font-mono">
          PixiJS v8 · {agents.length} agents · {processingAgents.length} active
        </div>
      )}
    </div>
  );
}
