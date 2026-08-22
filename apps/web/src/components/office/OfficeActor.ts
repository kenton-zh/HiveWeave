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
import {
  ROLE_COLORS,
  DEFAULT_ROLE_COLOR,
  WALK_SPEED,
  BOB_AMPLITUDE,
  BOB_FREQ,
  AGENT_PROC_ANIM,
  SHEET_FOOT_ANCHOR_Y,
  SHEET_FRAME_W,
  SHEET_FRAME_H,
  SHEET_STAND_FRAME,
} from "./constants";
import type { AgentAnimKind } from "./constants";

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
  /** 像素 sprite（站立帧，32×48）；无贴图时走程序化 body/face */
  private sprite: PIXI.Sprite | PIXI.AnimatedSprite | null = null;
  // 程序化回退（缺贴图时）
  private body: PIXI.Graphics | null = null;
  private face: PIXI.Graphics | null = null;
  private workDots = new PIXI.Graphics();
  private label: PIXI.Container;
  private bubble: PIXI.Container;
  private bubbleDots = new PIXI.Graphics();

  // Position interpolation
  private target = { x: 0, y: 0 };
  private walkPhase = 0;
  private _selected = false;
  private _lastSel = false;
  /** alert 进入时的 start timestamp，-1 表示未在脉冲 */
  private _pulseStart = -1;
  private standTex: PIXI.Texture | null;
  private bubbleTex: PIXI.Texture | null;

  constructor(
    agent: OfficeAgent,
    onSelect: (id: string) => void,
    standTex: PIXI.Texture | null,
    bubbleTex: PIXI.Texture | null,
  ) {
    this.agent = agent;
    this.standTex = standTex;
    this.bubbleTex = bubbleTex;
    this.label = this._buildLabel(agent.name);
    this.bubble = this._buildBubble(bubbleTex);

    // Interaction
    this.container.eventMode = "static";
    this.container.cursor = "pointer";
    this.container.on("pointertap", () => onSelect(agent.id));

    // Layer children (bottom → top)
    this.container.addChild(this.shadow, this.ring);
    // shadow 只画一次（脚底位置不变，bounce 只改变 scale）
    this._drawShadow();
    if (standTex) {
      // standTex 是整张 sheet（128×144 = 4列×3行 × 32×48）。
      // 用 AnimatedSprite 做单帧裁剪，frame = 左上角 (0,0,32,48)。
      // AnimatedSprite 即使只有一帧也工作正常，帧切换直接改 textures[0]。
      const frameTex = new PIXI.Texture({
        source: standTex.source,
        frame: new PIXI.Rectangle(
          SHEET_STAND_FRAME.x,
          SHEET_STAND_FRAME.y,
          SHEET_FRAME_W,
          SHEET_FRAME_H,
        ),
        orig: new PIXI.Rectangle(0, 0, SHEET_FRAME_W, SHEET_FRAME_H),
      });
      // 手动更新 UV（PixiJS v8 中 new Texture(frame,orig) 后需要调用一次，否则默认 UV 是整图）
      frameTex.updateUvs();

      const anim = new PIXI.AnimatedSprite([frameTex]);
      anim.anchor.set(0.5, SHEET_FOOT_ANCHOR_Y);
      // 32×48 像素小人 → 放大到 1.6x 约 51×77，和放大家具 (1.6-1.8x) 比例协调
      anim.scale.set(1.6);
      anim.alpha = 1;
      anim.autoUpdate = false; // 只有一帧，不跑 ticker
      anim.gotoAndStop(0);
      this.sprite = anim;
      this.container.addChild(anim);
    } else {
      this.body = new PIXI.Graphics();
      this.face = new PIXI.Graphics();
      this.body.alpha = 1;
      this.face.alpha = 1;
      this.container.addChild(this.body, this.face);
      this._drawBody();
    }
    this.container.addChild(this.workDots, this.bubble, this.label);
    this.label.y = 40;
    this.bubble.visible = false;

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
    const a = stateAlpha(output.visual);
    if (this.body) this.body.alpha = a;
    if (this.face) this.face.alpha = a;
    if (this.sprite) this.sprite.alpha = a;
  }

  /** Advance simulation by `delta` frames. Call every tick. */
  update(delta: number): void {
    const walking = shouldWalk(
      this.target.x - this.container.x,
      this.target.y - this.container.y,
    );
    if (walking) {
      this.walkPhase += delta * BOB_FREQ;
    } else {
      this.walkPhase *= 0.85; // decay when stationary
    }

    // Position interpolation
    const dx = this.target.x - this.container.x;
    const dy = this.target.y - this.container.y;
    this.container.x += dx * Math.min(1, delta * WALK_SPEED);
    this.container.y += dy * Math.min(1, delta * WALK_SPEED);

    // zIndex = y for isometric depth sort
    this.container.zIndex = Math.round(this.container.y);

    // 选定程序动画参数（因为 sheet 本身无动画帧）
    const state = this.fsm.current;
    const animKind: AgentAnimKind =
      state === "walking" || walking ? "walking" : state === "working" ? "working" : "idle";
    const p = AGENT_PROC_ANIM[animKind];

    // Bob animation（程序化角色 body/face；sprite 整体位移）
    const bob = Math.sin(this.walkPhase * (p.bobHz / BOB_FREQ)) * p.bobAmp;
    if (this.body && this.face) {
      this.body.y = bob;
      this.face.y = bob;
    }
    if (this.sprite) {
      this.sprite.y = bob;
      this.sprite.rotation = Math.sin(this.walkPhase * (p.bobHz / BOB_FREQ) * 2) * p.leanAmp;
    }

    // Shadow stays grounded — squash slightly while bobbing
    const squash = 1 - Math.min(0.18, Math.abs(bob) * 0.06);
    this.shadow.scale.set(squash, 1);
    this.shadow.alpha = 0.9 - Math.min(0.25, Math.abs(bob) * 0.1);

    // Sprite 模式：选中时 sprite 微亮；程序化模式仅在选中态切换时重绘 body（其余帧只改位置不动 GPU 指令）
    if (this.sprite) {
      this.sprite.tint = this._selected ? 0xddeeff : 0xffffff;
    } else if (this._selected !== this._lastSel) {
      this._drawBody();
      this._lastSel = this._selected;
    }

    // Alert pulse: 300ms bubble scale interpolate，完全在 update(delta) 内跑（不依赖 rAF，
    // 不会出现 container 销毁后写 / 多次 transition 叠加 / 后台页节流的问题）。
    if (this._pulseStart >= 0) {
      const elapsed = performance.now() - this._pulseStart;
      const t = Math.min(1, elapsed / 300);
      this.bubble.scale.set(1.2 - 0.2 * t);
      if (t >= 1) this._pulseStart = -1;
    }

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
        this.workDots.fill({
          color: 0xf8fafc,
          alpha: 0.65 + 0.35 * Math.max(0, Math.sin(t + i * 0.9)),
        });
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

  private _buildBubble(tex: PIXI.Texture | null): PIXI.Container {
    const c = new PIXI.Container();
    if (tex) {
      // speech-bubble.png: 96×48 → scale 1.1 放大到与放大后的家具匹配
      const s = new PIXI.Sprite(tex);
      s.anchor.set(0.5, 1);
      s.x = 0;
      s.y = 18;
      s.scale.set(1.1);
      c.addChild(s);
      this.bubbleDots.y = -13;
      this.bubbleDots.x = -3;
      c.addChild(this.bubbleDots);
    } else {
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
    }
    return c;
  }

  /** 启动 alert 气泡脉冲：下一帧起 update() 按 elapsed 线性插值 300ms。
   * 连续进入 alert 会重置起始时间（后一个覆盖前一个），不会多段叠加。 */
  private _pulseBubble(): void {
    this._pulseStart = performance.now();
    this.bubble.scale.set(1.2);
  }

  private _drawBody(): void {
    const body = this.body!;
    const face = this.face!;
    const accent = roleColor(this.agent);
    const accentDark = shade(accent, 0.78);
    const sel = this._selected;

    body.clear();
    face.clear();

    // Arms (slightly darker than torso for depth)
    body.roundRect(-15, -4, 6, 18, 3);
    body.fill(accentDark);
    body.roundRect(9, -4, 6, 18, 3);
    body.fill(accentDark);

    // Legs + shoes
    body.rect(-9, 16, 7, 15);
    body.fill(0x1f2937);
    body.rect(2, 16, 7, 15);
    body.fill(0x1f2937);
    body.roundRect(-10, 29, 9, 5, 2);
    body.fill(0x0f172a);
    body.roundRect(1, 29, 9, 5, 2);
    body.fill(0x0f172a);

    // Torso
    body.roundRect(-11, -12, 22, 30, 4);
    body.fill(accent);
    body.stroke({ width: sel ? 3 : 2, color: sel ? 0xbfdbfe : 0x111827 });

    // Collar
    body.moveTo(-4, -12);
    body.lineTo(0, -6);
    body.lineTo(4, -12);
    body.closePath();
    body.fill(0xf8fafc);

    // Belt line
    body.rect(-11, 12, 22, 2);
    body.fill({ color: 0x111827, alpha: 0.35 });

    // Face — skin
    face.circle(0, -22, 12);
    face.fill(0xf2b184);
    face.stroke({ width: 1.5, color: 0xd99a63 });

    // Hair (with a small fringe notch)
    face.roundRect(-11, -33, 22, 9, 3);
    face.fill(0x31251f);
    face.rect(-11, -26, 4, 4);
    face.fill(0x31251f);

    // Eyes
    face.circle(-4, -21, 1.5);
    face.fill(0x111827);
    face.circle(5, -21, 1.5);
    face.fill(0x111827);

    // Cheeks
    face.circle(-7, -17, 1.8);
    face.fill({ color: 0xef9a76, alpha: 0.55 });
    face.circle(8, -17, 1.8);
    face.fill({ color: 0xef9a76, alpha: 0.55 });

    // Smile
    face.arc(0.5, -18.5, 4, 0.2 * Math.PI, 0.8 * Math.PI);
    face.stroke({ width: 1.4, color: 0x7c4a26, cap: "round" });
  }
}
