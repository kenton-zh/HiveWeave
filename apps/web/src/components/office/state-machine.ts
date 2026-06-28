/**
 * Agent Visual State Machine
 * ==========================
 * Each OfficeActor owns an instance of this FSM.  The scene calls
 * `evaluate()` every frame with the latest snapshot flags; the FSM
 * determines the current visual state and any transition side-effects
 * (animation triggers, bubble toggles, position targets).
 *
 * State diagram:
 *
 *                    ┌──────────┐
 *          ┌────────│  idle    │◄────────┐
 *          │        └────┬─────┘         │
 *          │   working?  │  !working     │
 *          │        ┌────▼─────┐         │
 *          │        │ working  │─────────┤
 *          │        └────┬─────┘         │
 *          │    talking?  │  !talking    │
 *          │        ┌────▼─────┐         │
 *          │        │ talking  │─────────┘
 *          │        └────┬─────┘
 *          │      ping?  │
 *          │        ┌────▼─────┐
 *          └─────── │  alert   │─────────┘
 *                   └──────────┘
 *
 * Priority: alert > talking > working > idle
 * "walking" is a transient sub-state entered when the actor's
 * position differs significantly from its target.
 */

import type { AgentVisualState } from "./types";

// ── FSM Event Input ───────────────────────────────────────────────

export interface StateInput {
  processing: boolean;
  talking: boolean;
  ping: boolean;
}

// ── FSM Output ────────────────────────────────────────────────────

export interface StateOutput {
  visual: AgentVisualState;
  showBubble: boolean;
}

// ── Transition Hooks (for animation triggers) ─────────────────────

export type TransitionHook = (from: AgentVisualState, to: AgentVisualState) => void;

// ── Machine ───────────────────────────────────────────────────────

const PRIORITY: AgentVisualState[] = ["alert", "talking", "working", "idle"];

export class AgentStateMachine {
  private _current: AgentVisualState = "idle";
  private _previous: AgentVisualState = "idle";
  private onTransition: TransitionHook | null = null;

  /** Current visual state (read-only). */
  get current(): AgentVisualState {
    return this._current;
  }

  /** Previous visual state — useful for transition animations. */
  get previous(): AgentVisualState {
    return this._previous;
  }

  /** Register a callback invoked on every state transition. */
  onTransitionTo(fn: TransitionHook): void {
    this.onTransition = fn;
  }

  /**
   * Evaluate the FSM for this frame.
   * Returns the resolved visual state and bubble visibility.
   */
  evaluate(input: StateInput): StateOutput {
    const next = this.resolve(input);
    const changed = next !== this._current;
    if (changed) {
      this._previous = this._current;
      this._current = next;
      this.onTransition?.(this._previous, this._current);
    }
    return {
      visual: this._current,
      showBubble: input.talking || input.ping,
    };
  }

  /** Force-reset to idle (e.g. agent removed from scene). */
  reset(): void {
    if (this._current !== "idle") {
      this._previous = this._current;
      this._current = "idle";
      this.onTransition?.(this._previous, "idle");
    }
  }

  // ── private ─────────────────────────────────────────────────

  private resolve(input: StateInput): AgentVisualState {
    if (input.ping)   return "alert";
    if (input.talking) return "talking";
    if (input.processing) return "working";
    return "idle";
  }
}

/**
 * Determine whether the actor should be visually "walking" this frame.
 * Walking is a transient modifier, not a full state — it is derived
 * from the distance between the actor's current position and its target.
 */
export function shouldWalk(dx: number, dy: number, threshold = 3): boolean {
  return Math.abs(dx) > threshold || Math.abs(dy) > threshold;
}

/**
 * Given the visual state, return the base alpha for the actor's body.
 */
export function stateAlpha(state: AgentVisualState): number {
  return state === "working" ? 1.0 : 0.96;
}

/**
 * Roaming / talking behaviour helpers.
 *
 * These deterministic "moods" are driven by agent index + time so that
 * idle agents occasionally wander without needing backend events.
 */
export function isRoamingFrame(index: number, now: number): boolean {
  return Math.floor(now / 3000 + index) % 6 === 1;
}

export function isChatteringFrame(index: number, now: number): boolean {
  return Math.floor(now / 4000 + index) % 7 === 0;
}
