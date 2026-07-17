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

/** Darken a 0xRRGGBB colour by a factor (for shading body parts). */
function shade(color: number, factor: number): number {
  const r = Math.min(255, Math.round(((color >> 16) & 0xff) * factor));
  const g = Math.min(255, Math.round(((color >> 8) & 0xff) * factor));
  const b = Math.min(255, Math.round((color & 0xff) * factor));
  return (r << 16) | (g << 8) | b;
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
  private shadow = new PIXI.Graphics();
  private ring = new PIXI.Graphics();
  private body = new PIXI.Graphics();
  private face = new PIXI.Graphics();
  private workDots = new PIXI.Graphics();
  private label: PIXI.Container;
  private bubble: PIXI.Container;
  private bubbleDots = new PIXI.Graphics();

  // Position interpolation
  private target = { x: 0, y: 0 };
  private walkPhase = 0;
  private _selected = false;

  constructor(agent: OfficeAgent, onSelect: (id: string) => void) {
    this.agent = agent;
    this.label = this._buildLabel(agent.name);
    this.bubble = this._buildBubble();

    // Interaction
    this.container.eventMode = "static";
    this.container.cursor = "pointer";
    this.container.on("pointertap", () => onSelect(agent.id));

    // Layer children (bottom → top)
    this.container.addChild(this.shadow, this.ring, this.body, this.face, this.workDots, this.bubble, this.label);
    this.label.y = 40;
    this.bubble.visible = false;

    // Initial draw
    this._drawBody();
    this._drawShadow();

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
    this.bubble.y = input.talking ? -56 : -48;
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

    // Shadow stays grounded — squash slightly while bobbing
    const squash = 1 - Math.min(0.18, Math.abs(bob) * 0.06);
    this.shadow.scale.set(squash, 1);
    this.shadow.alpha = 0.9 - Math.min(0.25, Math.abs(bob) * 0.1);

    const now = performance.now();

    // Selection ring — soft pulsing ellipse at the feet
    if (this._selected) {
      const pulse = (Math.sin(now / 260) + 1) / 2;
      this.ring.clear();
      this.ring.ellipse(0, 34, 21 + pulse * 3.5, 7.5 + pulse * 1.2);
      this.ring.stroke({ width: 2, color: 0x60a5fa, alpha: 0.5 + pulse * 0.4 });
      this.ring.visible = true;
    } else if (this.ring.visible) {
      this.ring.visible = false;
      this.ring.clear();
    }

    // Working indicator — three typing dots above the head
    if (this.fsm.current === "working") {
      const t = now / 300;
      this.workDots.clear();
      for (let i = 0; i < 3; i++) {
        const bounce = Math.max(0, Math.sin(t + i * 0.9)) * 3;
        this.workDots.circle(-7 + i * 7, -42 - bounce, 2.2);
        this.workDots.fill({ color: 0xf8fafc, alpha: 0.65 + 0.35 * Math.max(0, Math.sin(t + i * 0.9)) });
      }
      this.workDots.visible = true;
    } else if (this.workDots.visible) {
      this.workDots.visible = false;
      this.workDots.clear();
    }

    // Speech bubble — animated ellipsis dots
    if (this.bubble.visible) {
      const t = now / 280;
      this.bubbleDots.clear();
      for (let i = 0; i < 3; i++) {
        const lift = Math.max(0, Math.sin(t + i * 0.9)) * 2.6;
        this.bubbleDots.circle(-9 + i * 9, 7 - lift, 2.5);
        this.bubbleDots.fill(0x1d4ed8);
      }
    }

    // Redraw body (selected highlight may have changed)
    this._drawBody();
  }

  /** Current visual state (read-only). */
  get visualState(): AgentVisualState {
    return this.fsm.current;
  }

  // ── Private ───────────────────────────────────────────────────

  private _buildLabel(name: string): PIXI.Container {
    const c = new PIXI.Container();
    const text = makeLabel(name, 10);
    text.anchor.set(0.5, 0);
    text.y = 1;
    const w = Math.max(30, text.width + 14);
    const bg = new PIXI.Graphics();
    bg.roundRect(-w / 2, -2, w, 17, 8.5);
    bg.fill({ color: 0x0f172a, alpha: 0.72 });
    c.addChild(bg, text);
    return c;
  }

  private _drawShadow(): void {
    this.shadow.clear();
    this.shadow.ellipse(0, 34, 17, 5.5);
    this.shadow.fill({ color: 0x0f172a, alpha: 0.18 });
  }

  private _buildBubble(): PIXI.Container {
    const c = new PIXI.Container();
    const bg = new PIXI.Graphics();
    // Tail (drawn first so the body overlaps its seam)
    bg.moveTo(-6, 18);
    bg.lineTo(2, 28);
    bg.lineTo(9, 18);
    bg.fill(0xffffff);
    bg.stroke({ width: 2, color: 0x3b82f6 });
    // Body
    bg.roundRect(-32, -8, 64, 26, 8);
    bg.fill(0xffffff);
    bg.stroke({ width: 2, color: 0x3b82f6 });
    // Cover the tail seam for a clean union
    bg.rect(-7, 16, 17, 4);
    bg.fill(0xffffff);
    this.bubbleDots.y = 0;
    c.addChild(bg, this.bubbleDots);
    return c;
  }

  /** Brief scale pulse when entering alert state. */
  private _pulseBubble(): void {
    this.bubble.scale.set(1.2);
    const start = performance.now();
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
    const accentDark = shade(accent, 0.78);
    const sel = this._selected;

    this.body.clear();
    this.face.clear();

    // Arms (slightly darker than torso for depth)
    this.body.roundRect(-15, -4, 6, 18, 3);
    this.body.fill(accentDark);
    this.body.roundRect(9, -4, 6, 18, 3);
    this.body.fill(accentDark);

    // Legs + shoes
    this.body.rect(-9, 16, 7, 15);
    this.body.fill(0x1f2937);
    this.body.rect(2, 16, 7, 15);
    this.body.fill(0x1f2937);
    this.body.roundRect(-10, 29, 9, 5, 2);
    this.body.fill(0x0f172a);
    this.body.roundRect(1, 29, 9, 5, 2);
    this.body.fill(0x0f172a);

    // Torso
    this.body.roundRect(-11, -12, 22, 30, 4);
    this.body.fill(accent);
    this.body.stroke({ width: sel ? 3 : 2, color: sel ? 0xbfdbfe : 0x111827 });

    // Collar
    this.body.moveTo(-4, -12);
    this.body.lineTo(0, -6);
    this.body.lineTo(4, -12);
    this.body.closePath();
    this.body.fill(0xf8fafc);

    // Belt line
    this.body.rect(-11, 12, 22, 2);
    this.body.fill({ color: 0x111827, alpha: 0.35 });

    // Face — skin
    this.face.circle(0, -22, 12);
    this.face.fill(0xf2b184);
    this.face.stroke({ width: 1.5, color: 0xd99a63 });

    // Hair (with a small fringe notch)
    this.face.roundRect(-11, -33, 22, 9, 3);
    this.face.fill(0x31251f);
    this.face.rect(-11, -26, 4, 4);
    this.face.fill(0x31251f);

    // Eyes
    this.face.circle(-4, -21, 1.5);
    this.face.fill(0x111827);
    this.face.circle(5, -21, 1.5);
    this.face.fill(0x111827);

    // Cheeks
    this.face.circle(-7, -17, 1.8);
    this.face.fill({ color: 0xef9a76, alpha: 0.55 });
    this.face.circle(8, -17, 1.8);
    this.face.fill({ color: 0xef9a76, alpha: 0.55 });

    // Smile
    this.face.arc(0.5, -18.5, 4, 0.2 * Math.PI, 0.8 * Math.PI);
    this.face.stroke({ width: 1.4, color: 0x7c4a26, cap: "round" });
  }
}
