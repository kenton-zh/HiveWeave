/**
 * StatusEventBus — lightweight in-memory pub/sub for agent runtime status.
 *
 * Tracks which agents are currently processing (LLM token exchange or API calls).
 * Broadcasts status changes to all SSE subscribers (connected frontend clients).
 *
 * This is separate from the lifecycle `status` field (active/archived/created)
 * stored in the database. An agent with lifecycle status "active" is only
 * "working" when isProcessing is true; otherwise it is "idle".
 */

export type StatusListener = (agentId: string, processing: boolean) => void;

export interface ActivityEvent {
  agentId: string;
  agentName: string;
  type: "thinking" | "text" | "tool_use" | "tool_result" | "done" | "error" | "text_delta" | "thinking_delta";
  content?: string;
  /** For delta events: identifies the streaming segment so the frontend can append */
  deltaId?: string;
  toolName?: string;
  toolInput?: string;
  toolResult?: string;
  errorMessage?: string;
  timestamp: number;
}

export type ActivityListener = (event: ActivityEvent) => void;

class StatusEventBus {
  private processing = new Set<string>();
  private listeners = new Set<StatusListener>();
  private activityListeners = new Set<ActivityListener>();
  private _paused = false;
  /** Keep a rolling buffer of recent activity for late subscribers */
  private recentActivity: ActivityEvent[] = [];
  private readonly maxRecent = 50;

  /** Mark an agent as processing or idle, and broadcast the change. */
  setProcessing(agentId: string, value: boolean): void {
    if (value && this._paused) return; // Don't start new work when paused
    const was = this.processing.has(agentId);
    if (value && !was) {
      this.processing.add(agentId);
      this.emit(agentId, true);
    } else if (!value && was) {
      this.processing.delete(agentId);
      this.emit(agentId, false);
    }
    // If state is unchanged, skip — avoids duplicate events
  }

  /** Query whether a specific agent is currently processing. */
  isProcessing(agentId: string): boolean {
    return this.processing.has(agentId);
  }

  /** Whether the entire system is paused (下班 mode). */
  get isPaused(): boolean {
    return this._paused;
  }

  /** Pause all agent activity — reject new requests, stop auto-triggers. */
  pause(): void {
    if (this._paused) return;
    this._paused = true;
    // Clear current processing state
    this.clearAll();
  }

  /** Resume agent activity. */
  resume(): void {
    this._paused = false;
  }

  /** Return all currently-processing agent IDs as an array. */
  getAllProcessing(): string[] {
    return [...this.processing];
  }

  /**
   * Subscribe to status changes.
   * Returns an unsubscribe function.
   */
  subscribe(listener: StatusListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Broadcast a real-time activity event from an agent. */
  emitActivity(event: ActivityEvent): void {
    // Delta events are transient — don't store in recentActivity buffer
    const isDelta = event.type === "text_delta" || event.type === "thinking_delta";
    if (!isDelta) {
      this.recentActivity.push(event);
      if (this.recentActivity.length > this.maxRecent) {
        this.recentActivity = this.recentActivity.slice(-this.maxRecent);
      }
    }
    for (const fn of this.activityListeners) {
      try { fn(event); } catch { /* ignore */ }
    }
  }

  /** Get recent activity (for late SSE subscribers). */
  getRecentActivity(): ActivityEvent[] {
    return [...this.recentActivity];
  }

  /** Subscribe to activity events. Returns unsubscribe function, replays recent events. */
  subscribeActivity(listener: ActivityListener): () => void {
    this.activityListeners.add(listener);
    for (const event of this.recentActivity) {
      try { listener(event); } catch { /* ignore */ }
    }
    return () => {
      this.activityListeners.delete(listener);
    };
  }

  /** Subscribe to live activity events only (no replay). Use for SSE reconnections. */
  subscribeActivityLive(listener: ActivityListener): () => void {
    this.activityListeners.add(listener);
    return () => {
      this.activityListeners.delete(listener);
    };
  }

  /** Reset all processing state (e.g., on server restart). */
  clearAll(): void {
    const ids = [...this.processing];
    this.processing.clear();
    for (const id of ids) {
      this.emit(id, false);
    }
  }

  private emit(agentId: string, processing: boolean): void {
    for (const fn of this.listeners) {
      try {
        fn(agentId, processing);
      } catch {
        // Ignore listener errors — don't let one bad subscriber break others
      }
    }
  }
}

/** Singleton instance shared across server routes. */
export const statusEventBus = new StatusEventBus();
