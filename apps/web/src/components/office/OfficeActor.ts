/**
 * OfficeActor — Agent character sprite.
 *
 * Each actor owns:
 *  - A PIXI.Container (positioned in scene space)
 *  - An AgentStateMachine (controls visual state)
 *  - Procedurally-drawn body / face / bubble graphics
 *
 * The scene calls `setTarget()` each frame with the desired state,
 * then `update(delta)` to interpolate position & animate.
 */

import * as PIXI from "pixi.js";
import type { OfficeAgent, AgentVisualState } from "./types";
import { AgentStateMachine, shouldWalk, stateAlpha } from "./state-machine";
import type { StateInput } from "./state-machine";
import { ROLE_COLORS, DEFAULT_ROLE_COLOR, WALK_SPEED, BOB_AMPLITUDE, BOB_FREQ } from "./constants";

// ── Label Factory ─────────────────────────────────────────────────

function makeLabel(text: string, size = 12): PIXI.Text {
  return new PIXI.Text({
    text,
    style: {
      fontFamily: "monospace",
      fontSize: size,
      fill: 0xf8fafc,
      fontWeight: "700",
      align: "center",
    },
  });
}

// ── Role Color ────────────────────────────────────────────────────

function roleColor(agent: OfficeAgent): number {
  return ROLE_COLORS[agent.role] ?? DEFAULT_ROLE_COLOR;
}

// ── Actor ─────────────────────────────────────────────────────────

export class OfficeActor {
  readonly container = new PIXI.Container();
  readonly agent: OfficeAgent;

  private fsm = new AgentStateMachine();
  private body = new PIXI.Graphics();
  private face = new PIXI.Graphics();
  private label: PIXI.Text;
  private bubble: PIXI.Container;

  // Position interpolation
  private target = { x: 0, y: 0 };
  private walkPhase = 0;
  private _selected = false;

  constructor(agent: OfficeAgent, onSelect: (id: string) => void) {
    this.agent = agent;
    this.label = makeLabel(agent.name, 11);
    this.bubble = this._buildBubble();

    // Interaction
    this.container.eventMode = "static";
    this.container.cursor = "pointer";
    this.container.on("pointertap", () => onSelect(agent.id));

    // Layer children
    this.container.addChild(this.body, this.face, this.bubble, this.label);
    this.label.anchor.set(0.5, 0);
    this.label.y = 38;
    this.bubble.visible = false;

    // Initial draw
    this._drawBody();

    // Listen for state transitions (e.g. bubble pop animation)
    this.fsm.onTransitionTo((_from, to) => {
      if (to === "alert") this._pulseBubble();
    });
  }

  // ── Public API ────────────────────────────────────────────────

  /** Set the desired state for this frame. Called every tick. */
  setTarget(x: number, y: number, input: StateInput, selected: boolean): void {
    this.target = { x, y };
    this._selected = selected;

    const output = this.fsm.evaluate(input);
    this.bubble.visible = output.showBubble;
    this.bubble.y = input.talking ? -52 : -44;
    this.label.visible = selected || input.talking;
    this.body.alpha = stateAlpha(output.visual);
  }

  /** Advance simulation by `delta` frames. Call every tick. */
  update(delta: number): void {
    // Walk phase (for bob animation)
    const dx = this.target.x - this.container.x;
    const dy = this.target.y - this.container.y;
    if (shouldWalk(dx, dy)) {
      this.walkPhase += delta * BOB_FREQ;
    } else {
      this.walkPhase *= 0.85; // decay when stationary
    }

    // Position interpolation
    this.container.x += dx * Math.min(1, delta * WALK_SPEED);
    this.container.y += dy * Math.min(1, delta * WALK_SPEED);

    // zIndex = y for isometric depth sort
    this.container.zIndex = Math.round(this.container.y);

    // Bob animation
    const bob = Math.sin(this.walkPhase) * BOB_AMPLITUDE;
    this.body.y = bob;
    this.face.y = bob;

    // Redraw body (selected highlight may have changed)
    this._drawBody();
  }

  /** Current visual state (read-only). */
  get visualState(): AgentVisualState {
    return this.fsm.current;
  }

  // ── Private ───────────────────────────────────────────────────

  private _buildBubble(): PIXI.Container {
    const c = new PIXI.Container();
    const bg = new PIXI.Graphics();
    bg.roundRect(-34, -6, 68, 26, 4);
    bg.fill(0xf8fafc);
    bg.stroke({ width: 2, color: 0x2563eb });
    // Tail
    bg.moveTo(-6, 20);
    bg.lineTo(3, 29);
    bg.lineTo(10, 20);
    bg.fill(0xf8fafc);
    bg.stroke({ width: 2, color: 0x2563eb });
    const text = new PIXI.Text({
      text: "...",
      style: { fontFamily: "monospace", fontSize: 12, fill: 0x1d4ed8, fontWeight: "700" },
    });
    text.anchor.set(0.5, 0.5);
    text.y = 7;
    c.addChild(bg, text);
    return c;
  }

  /** Brief scale pulse when entering alert state. */
  private _pulseBubble(): void {
    this.bubble.scale.set(1.2);
    const start = performance.now();
    const ticker = this.container.parent?.parent?.eventMode // quick check if ticker exists
      ? null : null; // we'll use a simple timeout
    const anim = () => {
      const elapsed = performance.now() - start;
      if (elapsed > 300) {
        this.bubble.scale.set(1);
        return;
      }
      const t = elapsed / 300;
      this.bubble.scale.set(1.2 - 0.2 * t);
      requestAnimationFrame(anim);
    };
    requestAnimationFrame(anim);
  }

  private _drawBody(): void {
    const accent = roleColor(this.agent);
    const sel = this._selected;

    this.body.clear();
    this.face.clear();

    // Body
    this.body.roundRect(-11, -12, 22, 30, 4);
    this.body.fill(accent);
    this.body.stroke({ width: sel ? 3 : 2, color: sel ? 0xbfdbfe : 0x111827 });

    // Arms
    this.body.rect(-15, -4, 6, 18);
    this.body.fill(accent);
    this.body.rect(9, -4, 6, 18);
    this.body.fill(accent);

    // Legs
    this.body.rect(-9, 16, 7, 17);
    this.body.fill(0x1f2937);
    this.body.rect(2, 16, 7, 17);
    this.body.fill(0x1f2937);

    // Face — skin
    this.face.circle(0, -22, 12);
    this.face.fill(0xf2b184);

    // Hair
    this.face.rect(-11, -31, 22, 8);
    this.face.fill(0x31251f);

    // Eyes
    this.face.circle(-4, -21, 1.5);
    this.face.fill(0x111827);
    this.face.circle(5, -21, 1.5);
    this.face.fill(0x111827);
  }
}
