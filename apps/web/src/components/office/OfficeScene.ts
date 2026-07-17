/**
 * OfficeScene — PixiJS scene orchestrator.
 *
 * Owns:
 *  - PIXI.Application lifecycle (init / resize / destroy)
 *  - Scene graph (floor → furniture → actors → ui)
 *  - Static environment rendering (room, furniture, HUD)
 *  - Actor synchronisation (create / update / destroy from SceneSnapshot)
 *  - Per-frame tick loop
 *
 * Communicates with React via:
 *  - Input:  `setSnapshot(snapshot)` — called by React when Zustand changes
 *  - Output: `onInteraction` callback — agent clicks bubble up to React
 */

import * as PIXI from "pixi.js";
import type {
  SceneSnapshot,
  OfficeAgent,
  DeskSlot,
  OfficeInteractionHandler,
} from "./types";
import { SCENE_LAYERS } from "./types";
import {
  WORLD_W,
  WORLD_H,
  TILE_W,
  TILE_H,
  DESKS,
  COMMON_TARGETS,
  ROAM_WAYPOINTS,
  MAX_VISIBLE_AGENTS,
  isoToScreen,
  getDesk,
} from "./constants";
import { isRoamingFrame, isChatteringFrame } from "./state-machine";
import { OfficeActor } from "./OfficeActor";

// ── Scene ─────────────────────────────────────────────────────────

export class OfficeScene {
  readonly app = new PIXI.Application();

  private root = new PIXI.Container();          // unscaled root
  private world = new PIXI.Container();         // scaled & centered world
  private layers: Record<string, PIXI.Container> = {};
  private actorMap = new Map<string, OfficeActor>();
  private _ready = false;
  private _destroyed = false;

  // Ambient animation targets (visual only)
  private _swayLeaves: PIXI.Container[] = [];
  private _motes: { g: PIXI.Graphics; vx: number; vy: number; phase: number }[] = [];

  private snapshot: SceneSnapshot = {
    agents: [],
    processingIds: new Set(),
    communicatingIds: new Set(),
    selectedAgentId: null,
    userPingIds: new Set(),
  };

  private onInteraction: OfficeInteractionHandler;

  constructor(onInteraction: OfficeInteractionHandler) {
    this.onInteraction = onInteraction;
  }

  // ── Lifecycle ─────────────────────────────────────────────────

  async mount(host: HTMLElement): Promise<void> {
    await this.app.init({
      width: host.clientWidth,
      height: host.clientHeight,
      background: 0x09111f,
      antialias: false,
      resolution: window.devicePixelRatio || 1,
      autoDensity: true,
    });

    if (this._destroyed) {
      this.app.destroy(true);
      return;
    }

    host.appendChild(this.app.canvas);

    // Scene graph
    this.app.stage.addChild(this.root);
    this.world.sortableChildren = true;
    this.root.addChild(this.world);

    for (const layer of SCENE_LAYERS) {
      const c = new PIXI.Container();
      c.label = layer;
      this.layers[layer] = c;
      this.world.addChild(c);
    }

    // Build static environment
    this._drawRoom(this.layers.floor);
    this._drawFurniture(this.layers.furniture);
    this._drawHud(this.layers.ui);
    this._drawAmbient(this.layers.ui);

    // Fit to host
    this._fit(host.clientWidth, host.clientHeight);

    // Render loop
    this.app.ticker.add((ticker) => this._tick(ticker.deltaTime));

    this._ready = true;
  }

  destroy(): void {
    this._destroyed = true;
    if (this._ready) {
      this.app.destroy(true);
    }
    this.actorMap.clear();
  }

  resize(width: number, height: number): void {
    if (!this._ready) return;
    this.app.renderer.resize(width, height);
    this._fit(width, height);
  }

  // ── State Bridge ──────────────────────────────────────────────

  /** Receive a new snapshot from React. Synchronises actors. */
  setSnapshot(snapshot: SceneSnapshot): void {
    this.snapshot = snapshot;
    if (this._ready) {
      this._syncActors();
    }
  }

  // ── Private: Fit & Transform ──────────────────────────────────

  private _fit(width: number, height: number): void {
    const scale = Math.min(width / WORLD_W, height / WORLD_H);
    this.world.scale.set(scale);
    this.world.x = Math.round((width - WORLD_W * scale) / 2);
    this.world.y = Math.round((height - WORLD_H * scale) / 2);

    // UI layer shares world transform offset but NOT scale
    // (HUD text stays crisp at native resolution)
    const ui = this.layers.ui;
    if (ui) {
      ui.scale.set(scale);
      ui.x = this.world.x;
      ui.y = this.world.y;
    }
  }

  // ── Private: Actor Sync ───────────────────────────────────────

  private _syncActors(): void {
    const visible = this.snapshot.agents.slice(0, MAX_VISIBLE_AGENTS);
    const keep = new Set(visible.map((a) => a.id));

    // Remove actors no longer present
    for (const [id, actor] of this.actorMap) {
      if (!keep.has(id)) {
        this.layers.actors.removeChild(actor.container);
        actor.container.destroy({ children: true });
        this.actorMap.delete(id);
      }
    }

    // Create new actors
    const actorsLayer = this.layers.actors;
    visible.forEach((agent, index) => {
      if (!this.actorMap.has(agent.id)) {
        const desk = getDesk(index, agent.role);
        const actor = new OfficeActor(agent, (id) => {
          this.onInteraction({ type: "select-agent", agentId: id });
        });
        actor.container.x = desk.x;
        actor.container.y = desk.y + 54;
        actorsLayer.addChild(actor.container);
        this.actorMap.set(agent.id, actor);
      }
    });
  }

  // ── Private: Tick ─────────────────────────────────────────────

  private _tick(delta: number): void {
    const now = performance.now();
    const agents = this.snapshot.agents.slice(0, MAX_VISIBLE_AGENTS);

    agents.forEach((agent, index) => {
      const actor = this.actorMap.get(agent.id);
      if (!actor) return;

      const desk = getDesk(index, agent.role);
      const processing = this.snapshot.processingIds.has(agent.id);
      const talking =
        this.snapshot.communicatingIds.has(agent.id) ||
        (!processing && isChatteringFrame(index, now));

      // Determine target position
      let tx: number;
      let ty: number;

      if (talking) {
        const spot = COMMON_TARGETS[index % COMMON_TARGETS.length];
        tx = spot.x;
        ty = spot.y;
      } else if (!processing && !talking && isRoamingFrame(index, now)) {
        const wp = ROAM_WAYPOINTS[index % ROAM_WAYPOINTS.length];
        tx = wp.x;
        ty = wp.y;
      } else {
        tx = desk.x;
        ty = desk.y + 54;
      }

      actor.setTarget(
        tx,
        ty,
        {
          processing,
          talking,
          ping: this.snapshot.userPingIds.has(agent.id),
        },
        this.snapshot.selectedAgentId === agent.id,
      );

      actor.update(delta);
    });

    // ── Ambient motion (visual only) ──────────────────────────
    const t = now / 1000;

    // Plant leaves sway gently
    for (let i = 0; i < this._swayLeaves.length; i++) {
      this._swayLeaves[i].rotation = Math.sin(t * 1.1 + i * 1.7) * 0.045;
    }

    // Dust motes drift slowly upward, wrapping around the room
    for (const m of this._motes) {
      m.g.x += m.vx * delta;
      m.g.y += m.vy * delta;
      m.g.alpha = 0.1 + 0.09 * Math.sin(t * 0.8 + m.phase);
      if (m.g.y < 110) {
        m.g.y = WORLD_H - 60;
        m.g.x = 120 + Math.random() * (WORLD_W - 240);
      }
    }
  }

  // ── Private: Ambient Particles ────────────────────────────────

  private _drawAmbient(ui: PIXI.Container): void {
    for (let i = 0; i < 14; i++) {
      const g = new PIXI.Graphics();
      g.circle(0, 0, 1.4 + Math.random() * 1.6);
      g.fill({ color: 0xffffff, alpha: 0.9 });
      g.x = 120 + Math.random() * (WORLD_W - 240);
      g.y = 130 + Math.random() * (WORLD_H - 220);
      g.alpha = 0.12;
      ui.addChild(g);
      this._motes.push({
        g,
        vx: (Math.random() - 0.5) * 0.12,
        vy: -(0.08 + Math.random() * 0.12),
        phase: Math.random() * Math.PI * 2,
      });
    }
  }

  // ── Private: Environment Drawing ──────────────────────────────

  private _drawRoom(floor: PIXI.Container): void {
    // Ceiling / sky — soft vertical gradient (4 stacked strips)
    const skyStops = [0x9be5f1, 0xabe9f3, 0xbdeef6, 0xd2f3f8];
    const stripH = 78 / skyStops.length;
    const bg = new PIXI.Graphics();
    bg.rect(0, 0, WORLD_W, WORLD_H);
    bg.fill(0x9be5f1);
    skyStops.forEach((c, i) => {
      bg.rect(0, i * stripH, WORLD_W, stripH + 1);
      bg.fill(c);
    });
    bg.rect(0, 78, WORLD_W, 62);
    bg.fill(0xe2f2f2);
    // Sunlight glow near the ceiling centre
    bg.ellipse(WORLD_W / 2, 96, 340, 46);
    bg.fill({ color: 0xffffff, alpha: 0.22 });
    floor.addChild(bg);

    // Back wall (angled — isometric)
    const backWall = new PIXI.Graphics();
    backWall.moveTo(182, 96);
    backWall.lineTo(1130, 96);
    backWall.lineTo(1050, 220);
    backWall.lineTo(96, 220);
    backWall.closePath();
    backWall.fill(0xd9d0bd);
    backWall.stroke({ width: 4, color: 0x8b7355 });
    floor.addChild(backWall);

    // Skirting board along the back wall base
    const skirting = new PIXI.Graphics();
    skirting.moveTo(96, 220);
    skirting.lineTo(1050, 220);
    skirting.lineTo(1050, 209);
    skirting.lineTo(96, 209);
    skirting.closePath();
    skirting.fill(0xc4b7a0);
    floor.addChild(skirting);

    // Side wall
    const sideWall = new PIXI.Graphics();
    sideWall.moveTo(96, 220);
    sideWall.lineTo(0, 164);
    sideWall.lineTo(0, 650);
    sideWall.lineTo(96, 706);
    sideWall.closePath();
    sideWall.fill(0xcfc6b8);
    sideWall.stroke({ width: 4, color: 0x8b7355 });
    floor.addChild(sideWall);

    // Windows on back wall
    for (let i = 0; i < 11; i++) this._drawWindow(floor, 230 + i * 78, 122, false);
    // Windows on side wall
    for (let i = 0; i < 6; i++) this._drawWindow(floor, 24, 238 + i * 72, true);

    // Wall clock between windows and whiteboard
    this._drawWallClock(floor, 646, 152);
    // Motivational bar-chart poster on the left back wall
    this._drawPoster(floor, 150, 128);

    // Isometric floor tiles
    const tiles = new PIXI.Graphics();
    for (let y = 0; y < 12; y++) {
      for (let x = 0; x < 14; x++) {
        const p = isoToScreen(x, y);
        const color = (x + y) % 2 === 0 ? 0x7a5035 : 0x8b5e3c;
        this._drawIsoTile(tiles, p.x, p.y + 112, color);
      }
    }
    floor.addChild(tiles);

    // Rug under the common area (gather point)
    this._drawRug(floor, 683, 354);
  }

  private _drawRug(parent: PIXI.Container, cx: number, cy: number): void {
    const rug = new PIXI.Graphics();
    // Outer diamond
    rug.moveTo(cx, cy - 46);
    rug.lineTo(cx + 118, cy);
    rug.lineTo(cx, cy + 46);
    rug.lineTo(cx - 118, cy);
    rug.closePath();
    rug.fill({ color: 0x4a6b96, alpha: 0.9 });
    rug.stroke({ width: 2, color: 0x33507a, alpha: 0.9 });
    // Inner diamond accent
    rug.moveTo(cx, cy - 32);
    rug.lineTo(cx + 86, cy);
    rug.lineTo(cx, cy + 32);
    rug.lineTo(cx - 86, cy);
    rug.closePath();
    rug.fill({ color: 0x6288b4, alpha: 0.9 });
    // Centre medallion
    rug.moveTo(cx, cy - 14);
    rug.lineTo(cx + 36, cy);
    rug.lineTo(cx, cy + 14);
    rug.lineTo(cx - 36, cy);
    rug.closePath();
    rug.fill({ color: 0x7fa3cc, alpha: 0.9 });
    parent.addChild(rug);
  }

  private _drawWallClock(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    g.circle(x, y, 13);
    g.fill(0xf8fafc);
    g.stroke({ width: 2.5, color: 0x8b7355 });
    // Hour / minute hands (static 10:10 — classic display time)
    g.moveTo(x, y);
    g.lineTo(x - 4.5, y - 4);
    g.stroke({ width: 2, color: 0x374151, cap: "round" });
    g.moveTo(x, y);
    g.lineTo(x + 5, y - 6);
    g.stroke({ width: 1.6, color: 0x374151, cap: "round" });
    g.circle(x, y, 1.4);
    g.fill(0x374151);
    parent.addChild(g);
  }

  private _drawPoster(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    g.roundRect(x, y, 64, 44, 3);
    g.fill(0xf8fafc);
    g.stroke({ width: 2.5, color: 0x8b7355 });
    // Tiny bar chart
    const bars = [10, 18, 14, 24];
    bars.forEach((h, i) => {
      g.rect(x + 10 + i * 12, y + 34 - h, 8, h);
      g.fill([0x4285f4, 0x34a853, 0xfbbc05, 0xea4335][i]);
    });
    parent.addChild(g);
  }

  private _drawWindow(parent: PIXI.Container, x: number, y: number, side: boolean): void {
    const g = new PIXI.Graphics();
    const w = side ? 48 : 52;
    // Outer frame
    g.roundRect(x - 2, y - 2, w + 4, 38, 2);
    g.fill(0xf8fafc);
    g.stroke({ width: 2, color: 0x9ca3af });
    // Panes — sky gradient (light top → deeper blue bottom)
    const paneW = side ? 16 : 18;
    [x + 5, x + (side ? 25 : 29)].forEach((px) => {
      g.rect(px, y + 5, paneW, 12);
      g.fill(0xdff2fd);
      g.rect(px, y + 17, paneW, 12);
      g.fill(0xbae6fd);
      // Diagonal glass shine
      g.moveTo(px + 3, y + 26);
      g.lineTo(px + paneW - 4, y + 8);
      g.stroke({ width: 2, color: 0xffffff, alpha: 0.55, cap: "round" });
    });
    // Sill
    g.rect(x - 4, y + 36, w + 8, 4);
    g.fill(0xe5e7eb);
    g.stroke({ width: 1, color: 0x9ca3af });
    parent.addChild(g);
  }

  private _drawIsoTile(g: PIXI.Graphics, x: number, y: number, fill: number, edge = 0x6b4a31): void {
    g.moveTo(x, y - TILE_H / 2);
    g.lineTo(x + TILE_W / 2, y);
    g.lineTo(x, y + TILE_H / 2);
    g.lineTo(x - TILE_W / 2, y);
    g.closePath();
    g.fill(fill);
    g.stroke({ width: 1, color: edge, alpha: 0.42 });
  }

  // ── Private: Furniture ────────────────────────────────────────

  private _drawFurniture(furniture: PIXI.Container): void {
    this._drawWhiteboard(furniture, 732, 164);
    this._drawPlant(furniture, 202, 276);
    this._drawPlant(furniture, 1112, 520);
    this._drawVending(furniture, 132, 344);
    this._drawSofa(furniture, 875, 260);
    this._drawMeetingTable(furniture, 780, 356);

    for (const desk of DESKS) {
      this._drawDesk(furniture, desk);
    }
  }

  private _drawDesk(parent: PIXI.Container, desk: DeskSlot): void {
    const g = new PIXI.Graphics();
    g.x = desk.x;
    g.y = desk.y;
    // Desk surface
    g.roundRect(-44, -16, 88, 46, 3);
    g.fill(0x8a6148);
    g.stroke({ width: 3, color: 0x50372b });
    // Front edge highlight (warm wood sheen)
    g.rect(-41, 24, 82, 3);
    g.fill({ color: 0xa97c5f, alpha: 0.8 });
    // Monitor stand + screen
    g.rect(-4, -14, 8, 4);
    g.fill(0x111827);
    g.rect(-22, -39, 44, 26);
    g.fill(0x172033);
    g.stroke({ width: 2, color: 0x111827 });
    g.rect(-17, -34, 34, 16);
    g.fill(0x38bdf8);
    // Screen glow + code lines
    g.rect(-17, -34, 34, 5);
    g.fill({ color: 0x7dd3fc, alpha: 0.7 });
    for (let i = 0; i < 3; i++) {
      g.rect(-14, -26 + i * 4, 10 + (i % 2) * 8, 1.6);
      g.fill({ color: 0x0c4a6e, alpha: 0.65 });
    }
    // Keyboard + mouse
    g.rect(-28, 4, 56, 10);
    g.fill(0x263142);
    g.rect(-24, 6.5, 48, 1.4);
    g.fill({ color: 0x3b4a63, alpha: 0.9 });
    g.ellipse(24, 20, 4, 2.6);
    g.fill(0x1f2937);
    // Coffee mug
    g.rect(-36, -6, 7, 8);
    g.fill(0xf8fafc);
    g.stroke({ width: 1.2, color: 0x9ca3af });
    g.circle(-28.2, -2, 2.2);
    g.stroke({ width: 1.2, color: 0x9ca3af });
    // Chair
    g.roundRect(-18, 32, 36, 28, 5);
    g.fill(0x303746);
    g.stroke({ width: 2, color: 0x111827 });
    g.rect(-14, 36, 28, 3);
    g.fill({ color: 0x47506b, alpha: 0.8 });
    g.zIndex = Math.round(desk.y);
    parent.addChild(g);
  }

  private _drawWhiteboard(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    g.rect(x, y, 128, 70);
    g.fill(0xf8fafc);
    g.stroke({ width: 3, color: 0x9ca3af });
    // Sketch lines
    g.rect(x + 18, y + 22, 30, 3);
    g.fill(0xef4444);
    g.rect(x + 70, y + 24, 32, 3);
    g.fill(0x22c55e);
    g.rect(x + 18, y + 34, 44, 2.4);
    g.fill({ color: 0x3b82f6, alpha: 0.8 });
    // Circle diagram with an arrow
    g.circle(x + 96, y + 46, 9);
    g.stroke({ width: 2, color: 0x8b5cf6 });
    g.moveTo(x + 52, y + 48);
    g.lineTo(x + 82, y + 48);
    g.stroke({ width: 2, color: 0x64748b });
    // Marker tray + markers + eraser
    g.rect(x + 14, y + 70, 100, 5);
    g.fill(0xd1d5db);
    g.rect(x + 22, y + 66.5, 12, 3.5);
    g.fill(0xef4444);
    g.rect(x + 40, y + 66.5, 12, 3.5);
    g.fill(0x3b82f6);
    g.roundRect(x + 88, y + 64, 16, 6, 2);
    g.fill(0x9ca3af);
    parent.addChild(g);
  }

  private _drawPlant(parent: PIXI.Container, x: number, y: number): void {
    const c = new PIXI.Container();
    c.x = x;
    c.y = y;
    const pot = new PIXI.Graphics();
    // Pot with rim
    pot.roundRect(-12, 20, 24, 18, 3);
    pot.fill(0x955f32);
    pot.stroke({ width: 1.5, color: 0x6b4226 });
    pot.roundRect(-14, 18, 28, 6, 3);
    pot.fill(0xa86b3a);
    c.addChild(pot);

    // Leaves live in their own container so they can sway
    const leaves = new PIXI.Container();
    leaves.y = 20; // pivot at the pot rim
    const lg = new PIXI.Graphics();
    for (let i = 0; i < 5; i++) {
      lg.ellipse(Math.cos(i) * 13, -20 + Math.sin(i * 1.6) * 7, 18, 8);
      lg.fill(i % 2 ? 0x37b24d : 0x2f9e44);
    }
    // Leaf veins highlight
    lg.ellipse(-6, -24, 10, 4);
    lg.fill({ color: 0x51cf66, alpha: 0.6 });
    leaves.addChild(lg);
    c.addChild(leaves);
    this._swayLeaves.push(leaves);

    c.zIndex = y;
    parent.addChild(c);
  }

  private _drawVending(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    g.roundRect(x, y, 68, 112, 4);
    g.fill(0xef4444);
    g.stroke({ width: 4, color: 0x7f1d1d });
    // Header sign
    g.roundRect(x + 6, y + 6, 56, 14, 3);
    g.fill(0xf8fafc);
    const sign = new PIXI.Text({
      text: "DRINKS",
      style: { fontFamily: "monospace", fontSize: 9, fill: 0xb91c1c, fontWeight: "700" },
    });
    sign.x = x + 13;
    sign.y = y + 8.5;
    g.addChild(sign);
    // Glass front with cans
    for (let row = 0; row < 4; row++) {
      for (let col = 0; col < 3; col++) {
        g.rect(x + 10 + col * 16, y + 26 + row * 15, 10, 10);
        g.fill([0xfef08a, 0x22d3ee, 0x22c55e][col]);
      }
    }
    // Glass shine
    g.moveTo(x + 12, y + 84);
    g.lineTo(x + 34, y + 26);
    g.stroke({ width: 5, color: 0xffffff, alpha: 0.28, cap: "round" });
    // Pickup slot
    g.roundRect(x + 10, y + 92, 48, 12, 2);
    g.fill(0x7f1d1d);
    g.zIndex = y;
    parent.addChild(g);
  }

  private _drawSofa(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    // Base
    g.roundRect(x, y, 152, 62, 8);
    g.fill(0x475569);
    g.stroke({ width: 4, color: 0x1f2937 });
    // Backrest
    g.roundRect(x + 4, y + 4, 144, 20, 6);
    g.fill(0x52637a);
    // Seat cushion seams
    g.moveTo(x + 76, y + 28);
    g.lineTo(x + 76, y + 58);
    g.stroke({ width: 2, color: 0x1f2937, alpha: 0.5 });
    // Armrests
    g.roundRect(x - 4, y + 8, 14, 46, 5);
    g.fill(0x3b4a63);
    g.stroke({ width: 3, color: 0x1f2937 });
    g.roundRect(x + 142, y + 8, 14, 46, 5);
    g.fill(0x3b4a63);
    g.stroke({ width: 3, color: 0x1f2937 });
    // Throw pillows
    g.roundRect(x + 16, y + 12, 22, 16, 4);
    g.fill(0xfbbf24);
    g.roundRect(x + 112, y + 12, 22, 16, 4);
    g.fill(0x38bdf8);
    g.zIndex = y;
    parent.addChild(g);
  }

  private _drawMeetingTable(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    g.roundRect(x, y, 158, 68, 4);
    g.fill(0x9a6a43);
    g.stroke({ width: 4, color: 0x5f3d27 });
    // Centre seam
    g.moveTo(x + 79, y + 4);
    g.lineTo(x + 79, y + 64);
    g.stroke({ width: 1.5, color: 0x5f3d27, alpha: 0.5 });
    // Notepads
    g.rect(x + 22, y + 16, 18, 12);
    g.fill(0xf8fafc);
    g.stroke({ width: 1, color: 0x9ca3af });
    g.rect(x + 112, y + 40, 18, 12);
    g.fill(0xf8fafc);
    g.stroke({ width: 1, color: 0x9ca3af });
    // Water pitcher
    g.ellipse(x + 80, y + 30, 7, 9);
    g.fill({ color: 0xbae6fd, alpha: 0.85 });
    g.stroke({ width: 1.5, color: 0x7dd3fc });
    g.zIndex = y;
    parent.addChild(g);
  }

  // ── Private: HUD ──────────────────────────────────────────────

  private _drawHud(ui: PIXI.Container): void {
    const bar = new PIXI.Graphics();
    bar.rect(0, 0, WORLD_W, 76);
    bar.fill(0xdbeafe);
    // Top sheen strip for a subtle gradient feel
    bar.rect(0, 0, WORLD_W, 26);
    bar.fill({ color: 0xeff6ff, alpha: 0.9 });
    // Bottom accent line
    bar.rect(0, 72, WORLD_W, 4);
    bar.fill(0x2563eb);
    ui.addChild(bar);

    const title = new PIXI.Text({
      text: "HiveWeave Office",
      style: {
        fontFamily: "monospace",
        fontSize: 26,
        fill: 0x24124f,
        fontWeight: "700",
      },
    });
    title.x = 38;
    title.y = 22;
    ui.addChild(title);

    // Accent underline beneath the title
    const underline = new PIXI.Graphics();
    underline.roundRect(38, 54, 96, 4, 2);
    underline.fill(0x2563eb);
    ui.addChild(underline);

    // Right-side live chip
    const chip = new PIXI.Graphics();
    chip.roundRect(WORLD_W - 132, 22, 96, 30, 15);
    chip.fill({ color: 0xffffff, alpha: 0.75 });
    chip.stroke({ width: 2, color: 0x2563eb });
    chip.circle(WORLD_W - 112, 37, 5);
    chip.fill(0x22c55e);
    ui.addChild(chip);

    const chipText = new PIXI.Text({
      text: "LIVE",
      style: {
        fontFamily: "monospace",
        fontSize: 15,
        fill: 0x1d4ed8,
        fontWeight: "700",
      },
    });
    chipText.x = WORLD_W - 100;
    chipText.y = 29;
    ui.addChild(chipText);
  }
}
