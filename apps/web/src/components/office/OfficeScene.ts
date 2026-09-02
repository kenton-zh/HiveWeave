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
  ASSET_URLS,
  ASSET_LOAD_LIST,
  SHEET_LAYOUTS,
  DEV_ANIM_SEQS,
  PURPLE_ANIM_SEQS,
  PURPLE_DEMO_ALL_AGENTS,
  FIRST_AGENT_FULL_ANIMATIONS,
  FIRST_AGENT_DEMO_CYCLE_MS,
  FIRST_AGENT_TYPE_MS,
  FRONTDESK_SET,
  furnitureSet,
  deskDepthBase,
  seatPosFor,
  roleSheetUrl,
  type FurniturePiece,
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
  /**
   * agent id → 实际生效的 sheet URL。
   * 选片涉及 404 回退（wanted → AGENT_DEV），每帧重算不可靠也不必要，
   * 因此在 _syncActors 建角时定稿，_tick 直接查表用于落座锚点校正。
   */
  private agentSheetUrl = new Map<string, string>();
  private _ready = false;
  private _destroyed = false;

  /** 预载纹理（URL → Texture）；mount 时填充 */
  private tex: Record<string, PIXI.Texture> = {};

  /** 每张 agent sheet 的整张纹理（URL → Texture）；帧切片在 OfficeActor 内按 SHEET_LAYOUTS 完成 */
  private sheetFrames: Record<string, PIXI.Texture> = {};

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
    // 逐 URL 独立加载 → 任一 png 404/失败时，其他图仍可用，缺图处 fallback 到程序化绘制。
    // PixiJS v8 的 Assets.load(string[]) 是"全有或全无"，所以不能用数组版本。
    const tex: Record<string, PIXI.Texture | null> = {};
    await Promise.all(
      ASSET_LOAD_LIST.map(async (url) => {
        tex[url] = await PIXI.Assets.load<PIXI.Texture>(url).catch(() => null);
      }),
    );
    this.tex = tex as Record<string, PIXI.Texture>;

    // agent sheet 的帧切片由 OfficeActor 内部完成（按 SHEET_LAYOUTS 的 cols×rows 切帧），
    // 此处只保留整张 Texture。
    for (const url of [ASSET_URLS.AGENT_DEV, ASSET_URLS.AGENT_MANAGER, ASSET_URLS.AGENT_QA, ASSET_URLS.AGENT_PURPLE]) {
      const sheet = tex[url];
      if (!sheet) continue;
      this.sheetFrames[url] = sheet;
    }

    let mounted = false;
    try {
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
      // 模式切换：如果有 office-scene-bg（1:1 复刻图生图版），就直接整张铺作背景，
      // 不再绘制程序化的墙/地板/家具 Graphics；保留 HUD 与 agent 容器（后续在背景上
      // 对齐 agent 位置）。否则回退到旧的程序化绘制管线。
      const bgTex = this.tex[ASSET_URLS.OFFICE_BG];
      if (bgTex) {
        this._drawBgScene(this.layers.floor);
        // 桌套件 sprite（桌+双椅+显示器连体，差分抠图自原图）放进 actors 层，
        // 与角色共用 deskDepthBase 深度基准做画家算法排序（几何见 constants.DESK_SET）
        this._drawFurnitureSprites(this.layers.actors);
        this.layers.actors.sortableChildren = true;
      } else {
        this._drawRoom(this.layers.floor);
        this._drawFurniture(this.layers.furniture);
        // 深度混层：见下方 fallback 分支
        this.layers.actors.sortableChildren = true;
        const furnitureChildren = [...this.layers.furniture.children];
        for (const child of furnitureChildren) {
          this.layers.actors.addChild(child);
        }
      }
      // 1:1 复刻模式：背景图已把标题/装饰/氛围全部画进去，
      // 不再叠加程序化画的 "HiveWeave Office" 横幅、LIVE 徽标、76px 顶部色带。
      // this._drawHud(this.layers.ui);
      this._drawAmbient(this.layers.ui);

      // Fit to host
      this._fit(host.clientWidth, host.clientHeight);

      // Render loop
      this.app.ticker.add((ticker) => this._tick(ticker.deltaTime));

      mounted = true;
      this._ready = true;
      // 竞态补偿：mount 完成前（Assets.load 比 org tree API 慢）到达的快照
      // 只被 setSnapshot 暂存、未建角色；ready 后立即补一次同步。
      this._syncActors();
      // 调试钩子（dev 专用）：浏览器 console 可遍历场景图定位渲染问题
      if (import.meta.env.DEV) (window as any).__officeScene = this;
    } finally {
      // 任何一步抛错（Assets.load 单条 catch 不会到这里；只在 app.init / appendChild / _drawRoom 抛错时触发）
      // 都要避免泄漏未 ready 的 Application（内部 WebGLRenderer/资源）。
      if (!mounted && !this._destroyed) {
        try {
          this.app.destroy(true);
        } catch {
          /* ignore double-destroy */
        }
      }
    }
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

  /** 稳定排序后的可见 agent：按 id 排序，避免组织树刷新顺序变化导致换桌漂移 */
  private _orderedAgents() {
    return [...this.snapshot.agents]
      .sort((a, b) => a.id.localeCompare(b.id))
      .slice(0, MAX_VISIBLE_AGENTS);
  }

  private _syncActors(): void {
    const visible = this._orderedAgents();
    const keep = new Set(visible.map((a) => a.id));

    // Remove actors no longer present
    for (const [id, actor] of this.actorMap) {
      if (!keep.has(id)) {
        this.layers.actors.removeChild(actor.container);
        actor.container.destroy({ children: true });
        this.actorMap.delete(id);
        this.agentSheetUrl.delete(id);
      }
    }

    // Create new actors
    const actorsLayer = this.layers.actors;
    visible.forEach((agent, index) => {
      if (!this.actorMap.has(agent.id)) {
        const desk = PURPLE_DEMO_ALL_AGENTS
          ? DESKS[index % DESKS.length]
          : getDesk(index, agent.role);
        // 单角色动画演示：索引 0 切回 dev 满帧 sheet（FSM 全状态可播：
        // 呼吸/坐下/坐姿/打字/起身…），其余角色沿用紫衣 sheet 保持视觉统一。
        const isDemoAgent = FIRST_AGENT_FULL_ANIMATIONS && index === 0;
        const wantedUrl = isDemoAgent
          ? ASSET_URLS.AGENT_DEV
          : PURPLE_DEMO_ALL_AGENTS
            ? ASSET_URLS.AGENT_PURPLE
            : roleSheetUrl(agent.role);
        // 实际加载成功的 URL（404 时回退 dev，布局/帧表按实际 URL 推导，避免错配）
        const sheetUrl = this.sheetFrames[wantedUrl] ? wantedUrl : ASSET_URLS.AGENT_DEV;
        const sheetTex = this.sheetFrames[sheetUrl] ?? null;
        const isPurple = sheetUrl === ASSET_URLS.AGENT_PURPLE;
        const hasFrameSeqs = sheetUrl === ASSET_URLS.AGENT_DEV || isPurple;
        // 落座锚定：脚底坐到椅面（紫衣/dev 表都是全身坐姿帧），下半身靠桌套件 zIndex 遮挡。
        // 朝向：演示角色与紫衣角色统一走 A 朝向（后椅）。B 朝向角色落在 front 片高度
        // 区间内、zIndex 更低，会被「桌面+显示器+前椅」整片盖死，不可用于坐姿演示。
        const variant: "A" | "B" = isPurple || isDemoAgent ? "A" : index % 2 === 0 ? "A" : "B";
        const seat = seatPosFor(sheetUrl, desk, variant);
        this.agentSheetUrl.set(agent.id, sheetUrl);
        const actor = new OfficeActor(
          agent,
          (id) => {
            this.onInteraction({ type: "select-agent", agentId: id });
          },
          sheetTex ?? null,
          hasFrameSeqs ? SHEET_LAYOUTS[sheetUrl] : null,
          hasFrameSeqs ? (isPurple ? PURPLE_ANIM_SEQS : DEV_ANIM_SEQS) : null,
          this.tex[ASSET_URLS.SPEECH_BUBBLE] ?? null,
        );
        actor.container.x = seat.x;
        actor.container.y = seat.y;
        actorsLayer.addChild(actor.container);
        this.actorMap.set(agent.id, actor);
      }
    });
  }

  // ── Private: Tick ─────────────────────────────────────────────

  private _tick(delta: number): void {
    const now = performance.now();
    const agents = this._orderedAgents();

    agents.forEach((agent, index) => {
      const actor = this.actorMap.get(agent.id);
      if (!actor) return;

      const desk = PURPLE_DEMO_ALL_AGENTS
        ? DESKS[index % DESKS.length]
        : getDesk(index, agent.role);
      const sheetUrl = this.agentSheetUrl.get(agent.id) ?? ASSET_URLS.AGENT_DEV;
      const isDemoAgent = FIRST_AGENT_FULL_ANIMATIONS && index === 0;

      let processing = this.snapshot.processingIds.has(agent.id);
      let talking =
        this.snapshot.communicatingIds.has(agent.id) ||
        (!processing && isChatteringFrame(index, now));

      // ── 单角色动画演示 ────────────────────────────────────────
      // 位置冻结在座位上，FSM 在「打字 ↔ 坐姿呼吸」间周期性切换。
      // 刻意不参与 talking / 聚集 / 散步：dev sheet 里点头与行走是站姿/位移序列，
      // 一旦播放人物会浮到桌面之上或走出座位 —— A 位遮挡只有约 7.8px，
      // 压不住站立姿态，视角与遮挡关系当场失效。
      // （ping 触发的 alert 是用户主动点击的瞬时反馈，保留。）
      if (isDemoAgent) {
        processing = now % FIRST_AGENT_DEMO_CYCLE_MS < FIRST_AGENT_TYPE_MS;
        talking = false;
      }

      // Determine target position
      let tx: number;
      let ty: number;
      let atDesk = false;
      const purpleDemo = PURPLE_DEMO_ALL_AGENTS;
      // 演示角色强制 A 朝向（后椅）：B 朝向角色落在 front 片高度区间内且 zIndex
      // 更低，会被「桌面+显示器+前椅」整片盖死，不可用于坐姿演示。
      const sitVariant: "A" | "B" =
        purpleDemo || isDemoAgent || index % 2 === 0 ? "A" : "B";

      if (isDemoAgent) {
        // 演示角色：常驻 A 椅位，不挪位
        const seat = seatPosFor(sheetUrl, desk, "A");
        tx = seat.x;
        ty = seat.y;
        atDesk = true;
      } else if (!purpleDemo && talking) {
        const spot = COMMON_TARGETS[index % COMMON_TARGETS.length];
        tx = spot.x;
        ty = spot.y;
      } else if (!purpleDemo && !processing && !talking && isRoamingFrame(index, now)) {
        const wp = ROAM_WAYPOINTS[index % ROAM_WAYPOINTS.length];
        tx = wp.x;
        ty = wp.y;
      } else if (purpleDemo) {
        // 紫衣女孩演示：常驻 A 椅位打字，聊天只弹气泡不挪位
        const seat = seatPosFor(sheetUrl, desk, "A");
        tx = seat.x;
        ty = seat.y;
        atDesk = true;
      } else {
        const seat = seatPosFor(sheetUrl, desk, sitVariant);
        tx = seat.x;
        ty = seat.y;
        atDesk = true;
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
        atDesk,
        sitVariant,
      );

      // 落座时把深度钉在桌套件基准之下（被自己的桌面遮住）；走动/离桌回落自身 y
      actor.setDepth(atDesk ? Math.round(deskDepthBase(desk)) - 1 : null);

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

  /**
   * 桌套件 sprite（bg 模式专用）：每桌两片（back = 后椅+桌远侧，front = 桌面+显示器+前椅），
   * 前台槽位只有 front 片。深度：back = base-2，角色 = base-1，front = base
   * → 「back → 角色 → front」三层固定顺序，桌面/显示器遮住角色躯干下半与前臂。
   */
  private _drawFurnitureSprites(parent: PIXI.Container): void {
    for (const desk of DESKS) {
      const set = furnitureSet(desk);
      const base = Math.round(deskDepthBase(desk));
      const isFront = set === FRONTDESK_SET;
      const pieces: [string, FurniturePiece, number][] = [];
      if (set.back) {
        pieces.push([ASSET_URLS.OFFICE_DESK_BACK, set.back, base - 2]);
      }
      pieces.push([
        isFront ? ASSET_URLS.OFFICE_FRONTDESK_SET : ASSET_URLS.OFFICE_DESK_FRONT,
        set.front,
        base,
      ]);
      for (const [url, piece, z] of pieces) {
        const tex = this.tex[url];
        if (!tex) continue;
        // 与背景同为 nearest：sprite 是原图像素，避免线性重采样产生边缘光晕
        tex.source.scaleMode = "nearest";
        const s = new PIXI.Sprite(tex);
        s.width = piece.w;
        s.height = piece.h;
        s.x = desk.x + piece.leftTop.x;
        s.y = desk.y + piece.leftTop.y;
        s.zIndex = z;
        parent.addChild(s);
      }
    }
  }

  /**
   * 背景层：office-scene-bg.png（PIL 去桌椅版，1672×941 ≈ 16:9）缩放到
   * WORLD_W×WORLD_H（1280×720）整层铺底，nearest 近邻保持像素边缘。
   * 背景只含不与角色交互的陈设（地板/墙/沙发/吧台/绿植/前台区地板）；
   * 桌/椅/显示器/前台由 _drawFurnitureSprites 以 sprite 分层渲染。
   */
  private _drawBgScene(floor: PIXI.Container): void {
    const tex = this.tex[ASSET_URLS.OFFICE_BG];
    if (!tex) return;
    const bg = new PIXI.Sprite(tex);
    bg.width = WORLD_W;
    bg.height = WORLD_H;
    bg.x = 0;
    bg.y = 0;
    bg.zIndex = -1;
    // 近邻缩放保持像素边缘（AI 生成图本身已经带像素化）
    const baseTex = tex.source;
    if (baseTex && "style" in baseTex) {
      try {
        (baseTex as any).style.scaleMode = "nearest";
      } catch {
        /* ignore */
      }
    }
    floor.addChild(bg);
  }

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

    // Windows on back wall — pixel-art wall-window sprites (64×64)
    if (this.tex[ASSET_URLS.WALL_WINDOW]) {
      // 原图 64×64，实际窗框 43×39，scale 1.5 → 65×59，视觉接近原程序化 78 间距排布
      const scale = 1.5;
      const count = 8;
      const spacing = (1040 - 280) / (count - 1);
      for (let i = 0; i < count; i++) {
        const w = new PIXI.Sprite(this.tex[ASSET_URLS.WALL_WINDOW]);
        w.anchor.set(0.5, 0.5);
        w.scale.set(scale);
        w.x = 280 + i * spacing;
        w.y = 152;
        w.zIndex = -1;
        floor.addChild(w);
      }
    } else {
      for (let i = 0; i < 11; i++) this._drawWindow(floor, 230 + i * 78, 122, false);
    }

    // Wall clock between windows and whiteboard
    this._drawWallClock(floor, 646, 152);
    // Motivational bar-chart poster on the left back wall
    this._drawPoster(floor, 150, 128);

    // Isometric floor tiles —
    // floor-tile.png 语义是"2×2 格合成 1 大钻石"(单张图内含 4 个三角形+中心菱形)，
    // 与原场景 12×14 的单格等距 tile 不是同一层级尺寸，直接铺会形成"大三角点阵"视觉。
    // 保持原程序化单格菱形绘制（大小正确、棋盘明暗已校准）。floor-tile.png 留作整图拼版素材。
    {
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

    // Rug under the common area (gather point)
    this._drawRug(floor, 683, 354);
  }

  private _drawRug(parent: PIXI.Container, cx: number, cy: number): void {
    const rug = new PIXI.Graphics();
    // 像素风地毯：3 层实心菱形 + 角上 8 个像素点星标，无透明度
    rug.moveTo(cx, cy - 48);
    rug.lineTo(cx + 120, cy);
    rug.lineTo(cx, cy + 48);
    rug.lineTo(cx - 120, cy);
    rug.closePath();
    rug.fill(0x3e5a7f);
    rug.moveTo(cx, cy - 34);
    rug.lineTo(cx + 88, cy);
    rug.lineTo(cx, cy + 34);
    rug.lineTo(cx - 88, cy);
    rug.closePath();
    rug.fill(0x5a7faa);
    rug.moveTo(cx, cy - 16);
    rug.lineTo(cx + 40, cy);
    rug.lineTo(cx, cy + 16);
    rug.lineTo(cx - 40, cy);
    rug.closePath();
    rug.fill(0x7fa3cc);
    // 8 个装饰角点（纯像素方块代替渐变）
    for (const [dx, dy] of [
      [-102, 0],
      [102, 0],
      [0, -42],
      [0, 42],
      [-70, -18],
      [70, -18],
      [-70, 18],
      [70, 18],
    ]) {
      rug.rect(cx + dx - 2, cy + dy - 2, 4, 4);
      rug.fill(0xf4d79a);
    }
    rug.zIndex = cy;
    parent.addChild(rug);
  }

  private _drawWallClock(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    // 像素挂钟：方形外壳 + 方形表盘 + 直角指针。不用圆。
    g.rect(x - 16, y - 16, 32, 32);
    g.fill(0xd4c7a8);
    g.rect(x - 13, y - 13, 26, 26);
    g.fill(0xf8fafc);
    // 四个钟点（像素小方块）
    for (const [dx, dy] of [[0, -9], [9, 0], [0, 9], [-9, 0]]) {
      g.rect(x + dx - 1, y + dy - 1, 2, 2);
      g.fill(0x1f2937);
    }
    // 时针（短粗）+ 分针（细长），10:10
    g.rect(x - 1, y - 5, 2, 5);
    g.fill(0x1f2937);
    g.rect(x, y - 7, 6, 2);
    g.fill(0x1f2937);
    // 中心轴
    g.rect(x - 1, y - 1, 2, 2);
    g.fill(0x7c2d12);
    parent.addChild(g);
  }

  private _drawPoster(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    // 像素海报：方形边框 + 4 条像素柱状图（直角），无圆角
    g.rect(x, y, 64, 44);
    g.fill(0xf8fafc);
    g.rect(x, y, 64, 2);
    g.fill(0x8b7355);
    g.rect(x, y + 42, 64, 2);
    g.fill(0x8b7355);
    g.rect(x, y, 2, 44);
    g.fill(0x8b7355);
    g.rect(x + 62, y, 2, 44);
    g.fill(0x8b7355);
    const bars = [10, 18, 14, 24];
    const colors = [0x4285f4, 0x34a853, 0xfbbc05, 0xea4335];
    bars.forEach((h, i) => {
      g.rect(x + 10 + i * 12, y + 34 - h, 8, h);
      g.fill(colors[i]);
    });
    parent.addChild(g);
  }

  private _drawWindow(parent: PIXI.Container, x: number, y: number, side: boolean): void {
    const g = new PIXI.Graphics();
    const w = side ? 48 : 52;
    // 外框 — 方形，无圆角无渐变
    g.rect(x - 2, y - 2, w + 4, 40);
    g.fill(0xe5e7eb);
    // 4 窗格：上浅蓝 / 下深蓝（分两半，无斜线条反光）
    const paneW = side ? 16 : 18;
    const gap = side ? 2 : 2;
    [x + 5, x + (side ? 25 : 29)].forEach((px) => {
      g.rect(px, y + 5, paneW, 12);
      g.fill(0xdff2fd);
      g.rect(px, y + 17 + gap, paneW, 12);
      g.fill(0x9dd4f4);
    });
    // 十字窗棂
    g.rect(x + (side ? 22 : 26), y + 3, 2, 34);
    g.fill(0x9ca3af);
    g.rect(x + 3, y + 19, w - 6, 2);
    g.fill(0x9ca3af);
    // 窗台
    g.rect(x - 4, y + 37, w + 8, 3);
    g.fill(0xd1d5db);
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
    // Whiteboard — pixel sprite (112×72 → scale 1.5 → 168×108)
    if (this.tex[ASSET_URLS.WHITEBOARD]) {
      const wb = new PIXI.Sprite(this.tex[ASSET_URLS.WHITEBOARD]);
      wb.anchor.set(0.5, 0.5);
      wb.scale.set(1.5);
      wb.x = 796;
      wb.y = 200;
      wb.zIndex = 164;
      furniture.addChild(wb);
    } else {
      this._drawWhiteboard(furniture, 732, 164);
    }

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
    const deskTex = this.tex[ASSET_URLS.DESK];
    const chairTex = this.tex[ASSET_URLS.CHAIR];

    if (deskTex) {
      // desk-computer.png: 96×72 (实际图形 4..91 × 5..50 = 87×45)
      // scale 1.8 → 173×130，和原程序化 desk 视觉尺寸对齐
      const deskScale = 1.8;
      const d = new PIXI.Sprite(deskTex);
      d.anchor.set(0.5, 0.5);
      d.scale.set(deskScale);
      // 原程序化桌的"桌面中心"大约在 desk.x, desk.y，这里往下移 2px 与腿底齐平
      d.x = desk.x;
      d.y = desk.y + 2;
      d.zIndex = Math.round(desk.y);
      parent.addChild(d);
      if (chairTex) {
        // office-chair.png: 64×64 (实际 21..44 × 9..50 = 23×41)
        // scale 1.6 → 约 37×66，视觉上能坐到放大后的桌面下方合适座位
        const c = new PIXI.Sprite(chairTex);
        c.anchor.set(0.5, 0.5);
        c.scale.set(1.6);
        c.x = desk.x;
        c.y = desk.y + 44;
        c.zIndex = Math.round(desk.y) + 1;
        parent.addChild(c);
      }
      return;
    }

    // 程序化回退（无贴图时）
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
    const plantTex = this.tex[ASSET_URLS.PLANT];
    const c = new PIXI.Container();
    c.x = x;
    c.y = y;

    if (plantTex) {
      // plant.png 48×64 (实际内容 27×53)，scale 1.3 → 与程序化 plant 尺寸匹配
      const leaves = new PIXI.Container();
      const s = new PIXI.Sprite(plantTex);
      s.anchor.set(0.5, 0.95); // 锚点放在盆的底部
      s.scale.set(1.3);
      leaves.addChild(s);
      c.addChild(leaves);
      this._swayLeaves.push(leaves);
    } else {
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
    }

    c.zIndex = y;
    parent.addChild(c);
  }

  private _drawVending(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    // 像素饮料机：方形外壳 + 方形招牌 + 3×4 点阵格子饮料罐 + 取物口。全直角。
    g.rect(x, y, 68, 112);
    g.fill(0xef4444);
    // 深色外框（4 边）
    g.rect(x, y, 68, 3);
    g.fill(0x7f1d1d);
    g.rect(x, y + 109, 68, 3);
    g.fill(0x7f1d1d);
    g.rect(x, y, 3, 112);
    g.fill(0x7f1d1d);
    g.rect(x + 65, y, 3, 112);
    g.fill(0x7f1d1d);
    // 招牌（白底红字 DRINKS，用像素点阵近似）
    g.rect(x + 6, y + 6, 56, 14);
    g.fill(0xf8fafc);
    // 用 6 个彩色像素块代替文字（字体不像素化）
    for (let i = 0; i < 5; i++) {
      g.rect(x + 11 + i * 10, y + 10, 6, 6);
      g.fill([0xea4335, 0xf97316, 0x22c55e, 0x3b82f6, 0x8b5cf6][i]);
    }
    // 玻璃陈列区 + 饮料罐（9 个彩色方形格）
    g.rect(x + 8, y + 24, 52, 62);
    g.fill(0x1f2937);
    g.rect(x + 10, y + 26, 48, 58);
    g.fill(0xdfe4e8);
    for (let row = 0; row < 4; row++) {
      for (let col = 0; col < 3; col++) {
        g.rect(x + 13 + col * 15, y + 29 + row * 13, 10, 9);
        g.fill([0xfef08a, 0x22d3ee, 0x22c55e][col]);
        // 罐盖
        g.rect(x + 13 + col * 15, y + 29 + row * 13, 10, 2);
        g.fill(0x64748b);
      }
    }
    // 取物口（长方形）
    g.rect(x + 10, y + 91, 48, 12);
    g.fill(0x7f1d1d);
    g.rect(x + 14, y + 94, 40, 6);
    g.fill(0x450a0a);
    // 右下脚硬币槽
    g.rect(x + 52, y + 106, 8, 3);
    g.fill(0x1f2937);
    g.zIndex = y;
    parent.addChild(g);
  }

  private _drawSofa(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    // 像素沙发：方形底盘 + 方形靠背 + 方形扶手 + 方形抱枕。全直角无圆角。
    g.rect(x, y + 24, 152, 38);
    g.fill(0x475569);
    // 深色底边
    g.rect(x, y + 56, 152, 6);
    g.fill(0x1e293b);
    // 靠背
    g.rect(x + 4, y + 4, 144, 24);
    g.fill(0x52637a);
    g.rect(x + 4, y + 4, 144, 3);
    g.fill(0x334155);
    // 扶手
    g.rect(x - 2, y + 8, 12, 46);
    g.fill(0x3b4a63);
    g.rect(x + 142, y + 8, 12, 46);
    g.fill(0x3b4a63);
    g.rect(x - 2, y + 48, 12, 6);
    g.fill(0x1e293b);
    g.rect(x + 142, y + 48, 12, 6);
    g.fill(0x1e293b);
    // 中分线（像素 2px 代替模糊描边）
    g.rect(x + 75, y + 24, 2, 32);
    g.fill(0x1f2937);
    // 抱枕（方形，无圆角）
    g.rect(x + 18, y + 10, 20, 14);
    g.fill(0xfbbf24);
    g.rect(x + 114, y + 10, 20, 14);
    g.fill(0x38bdf8);
    // 抱枕装饰点
    g.rect(x + 26, y + 16, 4, 2);
    g.fill(0x92400e);
    g.rect(x + 122, y + 16, 4, 2);
    g.fill(0x075985);
    g.zIndex = y;
    parent.addChild(g);
  }

  private _drawMeetingTable(parent: PIXI.Container, x: number, y: number): void {
    const g = new PIXI.Graphics();
    // 像素会议桌：方形桌面 + 直角桌框 + 4 条桌腿 + 方形记事本 + 方形水壶
    g.rect(x, y, 158, 68);
    g.fill(0x9a6a43);
    // 深色桌框（上/下/左/右 4 边）
    g.rect(x, y, 158, 3);
    g.fill(0x5f3d27);
    g.rect(x, y + 65, 158, 3);
    g.fill(0x5f3d27);
    g.rect(x, y, 3, 68);
    g.fill(0x5f3d27);
    g.rect(x + 155, y, 3, 68);
    g.fill(0x5f3d27);
    // 中分线
    g.rect(x + 78, y + 4, 2, 60);
    g.fill(0x7c4a22);
    // 桌腿（前后各 2 个小方块）
    g.rect(x + 6, y + 70, 8, 12);
    g.fill(0x4b2e1b);
    g.rect(x + 144, y + 70, 8, 12);
    g.fill(0x4b2e1b);
    g.rect(x + 32, y + 68, 8, 14);
    g.fill(0x4b2e1b);
    g.rect(x + 118, y + 68, 8, 14);
    g.fill(0x4b2e1b);
    // 记事本（方形，代替透明度描边）
    g.rect(x + 22, y + 16, 18, 12);
    g.fill(0xf8fafc);
    g.rect(x + 22, y + 16, 18, 1);
    g.fill(0x9ca3af);
    g.rect(x + 22, y + 27, 18, 1);
    g.fill(0x9ca3af);
    g.rect(x + 112, y + 40, 18, 12);
    g.fill(0xf8fafc);
    g.rect(x + 112, y + 40, 18, 1);
    g.fill(0x9ca3af);
    g.rect(x + 112, y + 51, 18, 1);
    g.fill(0x9ca3af);
    // 水壶（方形代替椭圆，实色不透明）
    g.rect(x + 76, y + 26, 8, 14);
    g.fill(0xbae6fd);
    g.rect(x + 76, y + 26, 8, 2);
    g.fill(0x7dd3fc);
    g.rect(x + 78, y + 24, 4, 2);
    g.fill(0x7dd3fc);
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
