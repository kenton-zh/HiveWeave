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
  }

  // ── Private: Environment Drawing ──────────────────────────────

  private _drawRoom(floor: PIXI.Container): void {
    // Ceiling / sky
    const bg = new PIXI.Graphics();
    bg.rect(0, 0, WORLD_W, WORLD_H);
    bg.fill(0x9be5f1);
    bg.rect(0, 78, WORLD_W, 62);
    bg.fill(0xe2f2f2);
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
  }

  private _drawWindow(parent: PIXI.Container, x: number, y: number, side: boolean): void {
    const g = new PIXI.Graphics();
    g.rect(x, y, side ? 48 : 52, 34);
    g.fill(0xeff6ff);
    g.stroke({ width: 2, color: 0x9ca3af });
    g.rect(x + 5, y + 5, side ? 16 : 18, 24);
    g.fill(0xbae6fd);
    g.rect(x + (side ? 25 : 29), y + 5, side ? 16 : 18, 24);
    g.fill(0xbae6fd);
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
    // Monitor
    g.rect(-22, -39, 44, 26);
    g.fill(0x172033);
    g.stroke({ width: 2, color: 0x111827 });
    g.rect(-17, -34, 34, 16);
    g.fill(0x38bdf8);
    // Keyboard
    g.rect(-28, 4, 56, 10);
    g.fill(0x263142);
    // Chair
    g.roundRect(-18, 32, 36, 28, 5);
    g.fill(0x303746);
    g.stroke({ width: 2, color: 0x111827 });
    g.zIndex = Math.round(desk.y);
    parent.addChild(g);
  }

  private _drawWhiteboard(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    g.rect(x, y, 128, 70);
    g.fill(0xf8fafc);
    g.stroke({ width: 3, color: 0x9ca3af });
    g.rect(x + 18, y + 22, 30, 3);
    g.fill(0xef4444);
    g.rect(x + 70, y + 24, 32, 3);
    g.fill(0x22c55e);
    parent.addChild(g);
  }

  private _drawPlant(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    g.x = x;
    g.y = y;
    g.rect(-12, 20, 24, 18);
    g.fill(0x955f32);
    for (let i = 0; i < 5; i++) {
      g.ellipse(Math.cos(i) * 13, Math.sin(i * 1.6) * 7, 18, 8);
      g.fill(i % 2 ? 0x37b24d : 0x2f9e44);
    }
    g.zIndex = y;
    parent.addChild(g);
  }

  private _drawVending(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    g.rect(x, y, 68, 112);
    g.fill(0xef4444);
    g.stroke({ width: 4, color: 0x7f1d1d });
    for (let row = 0; row < 4; row++) {
      for (let col = 0; col < 3; col++) {
        g.rect(x + 10 + col * 16, y + 12 + row * 16, 10, 10);
        g.fill([0xfef08a, 0x22d3ee, 0x22c55e][col]);
      }
    }
    g.zIndex = y;
    parent.addChild(g);
  }

  private _drawSofa(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    g.roundRect(x, y, 152, 62, 5);
    g.fill(0x475569);
    g.stroke({ width: 4, color: 0x1f2937 });
    g.zIndex = y;
    parent.addChild(g);
  }

  private _drawMeetingTable(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    g.roundRect(x, y, 158, 68, 4);
    g.fill(0x9a6a43);
    g.stroke({ width: 4, color: 0x5f3d27 });
    g.zIndex = y;
    parent.addChild(g);
  }

  // ── Private: HUD ──────────────────────────────────────────────

  private _drawHud(ui: PIXI.Container): void {
    const bar = new PIXI.Graphics();
    bar.rect(0, 0, WORLD_W, 76);
    bar.fill(0xdbeafe);
    bar.stroke({ width: 4, color: 0x2563eb });
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
  }
}
