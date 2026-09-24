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
  purpleDemoSeat,
  ASSET_URLS,
  ASSET_LOAD_LIST,
  SHEET_LAYOUTS,
  DEV_ANIM_SEQS,
  PURPLE_ANIM_SEQS,
  HR_FRONTDESK_ANIM_SEQS,
  frontDeskRoutineState,
  PURPLE_DEMO_ALL_AGENTS,
  FIRST_AGENT_FULL_ANIMATIONS,
  FIRST_AGENT_DEMO_CYCLE_MS,
  FIRST_AGENT_TYPE_MS,
  FRONDESK_SLOT,
  furnitureSet,
  isFrontDeskSlot,
  deskDepthBase,
  seatPosFor,
  roleSheetUrl,
  isHrRole,
  type FurniturePiece,
} from "./constants";
import { isRoamingFrame, isChatteringFrame } from "./state-machine";
import { OfficeActor } from "./OfficeActor";
import { renderFrontSign, FRONT_SIGN_CANVAS } from "./frontSign";

// ── Seat resolution (single source of truth) ──────────────────────

/**
 * 决定一个 agent 坐哪、以什么姿态落座。
 *
 * ⚠ **唯一判定源**：`_syncActors`（建角）与 `_tick`（逐帧驱动）都走这里。
 * 历史上这两处各写一份，改了一处漏另一处会造成「建角在 A、每帧拽去 B」的
 * 位置抖动（本仓有前科：声明同源但实际分裂）。新增座位规则只改本函数。
 *
 * 优先级（2026-09-23 用户钦定「HR 永远坐前台」）：
 *  1. **HR → 前台**（硬绑，任何项目、任何 index）。项目无 HR 时前台空着，
 *     由 `_frontDeskFallbackId` 指定的 0 号角色补位（保证柜台不空，见下方注释）。
 *  2. 演示路径（`PURPLE_DEMO_ALL_AGENTS`）→ 0 号占前台，其余进工位网格。
 *  3. 常规 → 按 role 三池分配工位。
 */
interface SeatDecision {
  desk: DeskSlot;
  variant: "A" | "B";
  /** 是否坐前台（前台走 A 姿态 + 柜台后固定位，且不参与演示周期切换） */
  atFrontDesk: boolean;
}

function resolveSeat(
  agent: { id: string; role: string },
  index: number,
  roster: readonly { id: string; role: string }[],
  fallbackFrontDeskId: string | null,
): SeatDecision {
  // ① HR 硬绑前台
  if (isHrRole(agent.role)) {
    return { desk: FRONDESK_SLOT, variant: "A", atFrontDesk: true };
  }
  // ② 无 HR ⇒ 由兜底者（0 号）补位，避免柜台空置
  const hasHr = roster.some((a) => isHrRole(a.role));
  if (!hasHr && fallbackFrontDeskId === agent.id) {
    return { desk: FRONDESK_SLOT, variant: "A", atFrontDesk: true };
  }
  // ③ 演示路径
  //    ⚠ 2026-09-23：**必须排除前台**。`purpleDemoSeat(0)` 会把 0 号派到前台，
  //    而前台已归 HR（或兜底者）——两套逻辑叠加会在柜台后画出**两个人**
  //    （实测首次截图：HR「天线」与 0 号「潮汐」重叠）。
  //    故演示路径只用工位网格，前台席位一律跳过。
  if (PURPLE_DEMO_ALL_AGENTS) {
    const demoSeat = purpleDemoSeat(index);
    if (demoSeat.desk.id !== FRONDESK_SLOT.id) {
      return { ...demoSeat, atFrontDesk: false };
    }
    // 0 号本会落前台 ⇒ 改派工位网格（按 index 取，避免与其他人重叠）
    return {
      desk: DESKS[index % Math.max(DESKS.length, 1)],
      variant: "B",
      atFrontDesk: false,
    };
  }
  // ④ 常规工位
  return { desk: getDesk(index, agent.role), variant: index % 2 === 0 ? "A" : "B", atFrontDesk: false };
}

// ── Sheet / animation selection (single source of truth) ──────────

/**
 * 帧序表按 sheet URL 路由。**唯一判定源** —— 建角与驱动都不要各写一份
 * if-else（本仓有前科：声明同源但实际分裂，改一处漏另一处 ⇒ 播错帧）。
 * URL 不在表内（404 回退到 dev）时为 undefined，OfficeActor 自动降级为程序化 body。
 */
const ANIM_SEQS_FOR: Record<string, Record<string, number[]> | undefined> = {
  [ASSET_URLS.AGENT_DEV]: DEV_ANIM_SEQS as Record<string, number[]>,
  [ASSET_URLS.AGENT_PURPLE]: PURPLE_ANIM_SEQS as Record<string, number[]>,
  [ASSET_URLS.AGENT_HR_FRONTDESK]: HR_FRONTDESK_ANIM_SEQS as Record<string, number[]>,
};

/**
 * 选 sheet。优先级：
 *  1. **前台席位 → HR 专用正面表**（用户钦定「这个女生的动画只做和前台匹配的」）。
 *     ⚠ 必须排在演示分支之前：前台可能是 0 号（项目无 HR 时的兜底），
 *     若先走演示分支会给她套上紫衣**侧视**表 —— 朝向与遮挡片不匹配。
 *  2. `FIRST_AGENT_FULL_ANIMATIONS` 的 0 号演示 → dev 满帧表。
 *  3. `PURPLE_DEMO_ALL_AGENTS` 演示 → 紫衣表。
 *  4. 常规 → 按 role 选表。
 *
 * ⚠ 入参 `atFrontDesk` 必须来自 `resolveSeat`，不得在下游另算
 *   （否则又出现「建角判前台、每帧不判」的漂移）。
 * ⚠ 「素材是否真的加载成功」由 `_syncActors` 用 `available` 复核，本函数只表达意愿。
 */
function pickSheetUrl(
  agent: { role: string },
  index: number,
  atFrontDesk: boolean,
): string {
  if (atFrontDesk) return ASSET_URLS.AGENT_HR_FRONTDESK;
  if (FIRST_AGENT_FULL_ANIMATIONS && index === 0) return ASSET_URLS.AGENT_DEV;
  if (PURPLE_DEMO_ALL_AGENTS) return ASSET_URLS.AGENT_PURPLE;
  return roleSheetUrl(agent.role);
}

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
  /**
   * 项目无 HR 时，坐前台补位的 agent id（= 可见列表的 0 号）。
   * 在 `_syncActors` 里按当前 roster 定稿，`_tick` 直接读 —— 避免两处各算一次。
   * 存在 HR 时恒为 null（HR 一定在前台，见 `resolveSeat`）。
   */
  private _frontDeskFallbackId: string | null = null;
  private _ready = false;
  private _destroyed = false;

  /** 预载纹理（URL → Texture）；mount 时填充 */
  private tex: Record<string, PIXI.Texture> = {};

  /** 每张 agent sheet 的整张纹理（URL → Texture）；帧切片在 OfficeActor 内按 SHEET_LAYOUTS 完成 */
  private sheetFrames: Record<string, PIXI.Texture> = {};

  // Ambient animation targets (visual only)
  private _motes: { g: PIXI.Graphics; vx: number; vy: number; phase: number }[] = [];

  private snapshot: SceneSnapshot = {
    agents: [],
    processingIds: new Set(),
    communicatingIds: new Set(),
    selectedAgentId: null,
    userPingIds: new Set(),
    projectName: null,
  };

  /** 前台招牌 sprite（弧面项目名）；null = 素材未就绪 */
  private _frontSignSprite: PIXI.Sprite | null = null;
  /** 已渲染的项目名，用于避免每帧重算 canvas */
  private _frontSignName: string | null = null;

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
    // 旧像素角色表 agent-manager / agent-qa 已于 2026-09-22 删除（美术路线裁决），预载只剩两张高清表
    for (const url of [ASSET_URLS.AGENT_DEV, ASSET_URLS.AGENT_PURPLE, ASSET_URLS.AGENT_HR_FRONTDESK]) {
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
        // 旧的程序化房间 / 像素家具管线已于 2026-09-22 随「像素风方向永久放弃」删除
        // （含 _drawRoom / _drawFurniture / _drawDesk / _drawPlant 等 12 个方法，542 行）。
        // 没有底图就没有场景 —— **显式报错，不静默降级**（设计规格 §3 T0）。
        console.error(
          "[office] 缺少 OFFICE_BG（office-scene-bg.png）：场景将只剩角色。请检查 public/office-assets/",
        );
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

  /** Receive a new snapshot from React. Synchronises actors.
   *  2026-09-23：追加前台招牌重绘 —— 项目名变化时重算弧面文字 canvas。 */
  setSnapshot(snapshot: SceneSnapshot): void {
    this.snapshot = snapshot;
    if (this._ready) {
      this._syncActors();
      this._refreshFrontSign();
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
    // 无 HR 时的前台补位者 = 可见列表 0 号（有 HR 时不用，见 resolveSeat）
    const roster = visible as readonly { id: string; role: string }[];
    this._frontDeskFallbackId = visible.length > 0 ? visible[0].id : null;
    visible.forEach((agent, index) => {
      if (!this.actorMap.has(agent.id)) {
        const seatDecision = resolveSeat(
          agent,
          index,
          roster,
          this._frontDeskFallbackId,
        );
        const desk = seatDecision.desk;
        const wanted = pickSheetUrl(agent, index, seatDecision.atFrontDesk);
        // 实际可用的 URL：素材 404 时按「前台→紫衣→dev」逐级回退。
        // 帧表/布局一律按**实际** URL 推导（`ANIM_SEQS_FOR` + `SHEET_LAYOUTS`），
        // 否则会出现「用紫衣的帧号索引 dev 的图」这类静默错配。
        const sheetUrl =
          wanted === ASSET_URLS.AGENT_HR_FRONTDESK && !this.sheetFrames[wanted]
            ? ASSET_URLS.AGENT_PURPLE
            : this.sheetFrames[wanted]
              ? wanted
              : ASSET_URLS.AGENT_DEV;
        const sheetTex = this.sheetFrames[sheetUrl] ?? null;
        // 落座：前台 A（柜台后）；紫衣演示工位走 B（近侧可见椅，z 在 front 之上）。
        const variant: "A" | "B" = seatDecision.variant;
        const seat = seatPosFor(sheetUrl, desk, variant);
        this.agentSheetUrl.set(agent.id, sheetUrl);
        const actor = new OfficeActor(
          agent,
          (id) => {
            this.onInteraction({ type: "select-agent", agentId: id });
          },
          sheetTex ?? null,
          SHEET_LAYOUTS[sheetUrl] ?? null,
          ANIM_SEQS_FOR[sheetUrl] ?? null,
          // 气泡贴图（SPEECH_BUBBLE）随像素素材一并删除 ⇒ 走 OfficeActor 的程序化气泡
          null,
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

      const seatDecision = resolveSeat(
        agent,
        index,
        agents as readonly { id: string; role: string }[],
        this._frontDeskFallbackId,
      );
      const desk = seatDecision.desk;
      const sheetUrl = this.agentSheetUrl.get(agent.id) ?? ASSET_URLS.AGENT_DEV;
      const isDemoAgent = FIRST_AGENT_FULL_ANIMATIONS && index === 0;

      let processing = this.snapshot.processingIds.has(agent.id);
      let talking =
        this.snapshot.communicatingIds.has(agent.id) ||
        (!processing && isChatteringFrame(index, now));

      // ── 单角色动画演示 ────────────────────────────────────────
      // 位置冻结在座位上，0 号 FSM 在「打字 ↔ 坐姿呼吸」间周期性切换。
      // 2026-09-08：FIRST_AGENT_FULL_ANIMATIONS=false 后 0 号也穿紫衣 v2 sheet
      // （帧 0-3 打字 / 4-7 坐姿呼吸，全坐姿序列），循环对遮挡安全，故
      // 演示周期按 index===0 生效，不再依赖 dev sheet。
      //
      // 2026-09-23：**前台角色豁免此周期** —— 前台有自己的三态演示
      // （打字 ↔ 呼吸 ↔ 招手，见 FRONT_DESK_ROUTINE），且 HR 不一定是 0 号，
      // 两套周期叠加会互相打断。
      if (index === 0 && !seatDecision.atFrontDesk) {
        processing = now % FIRST_AGENT_DEMO_CYCLE_MS < FIRST_AGENT_TYPE_MS;
        talking = false;
      }

      // ── 前台 HR 三态值守（用户钦定：打字 + 呼吸 + 抬头招呼客人）────
      // 判据走 `frontDeskRoutineState`（纯函数，常量里可单测），不在本处内联时间比较。
      // 语义映射：working → 走打字的 processing；
      //           idle    → 呼吸（两者都关）；
      //           talking → 抬手招呼（借 talking 态，同时弹名称气泡「有客人」）。
      if (seatDecision.atFrontDesk) {
        const routine = frontDeskRoutineState(now);
        processing = routine === "working";
        talking = routine === "talking";
      }

      // Determine target position
      let tx: number;
      let ty: number;
      let atDesk = false;
      const purpleDemo = PURPLE_DEMO_ALL_AGENTS;
      const sitVariant: "A" | "B" = seatDecision.variant;

      if (seatDecision.atFrontDesk) {
        const seat = seatPosFor(sheetUrl, desk, sitVariant);
        tx = seat.x;
        ty = seat.y;
        atDesk = true;
      } else if (isDemoAgent) {
        const seat = seatPosFor(sheetUrl, desk, sitVariant);
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
        const seat = seatPosFor(sheetUrl, desk, sitVariant);
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
        seatDecision.atFrontDesk,
      );

      // A 位：z = base-1，被自己的桌面挡腿；B 位：z = base+1，坐在近侧可见椅里
      const base = deskDepthBase(desk);
      actor.setDepth(atDesk ? (sitVariant === "B" ? base + 1 : base - 1) : null);

      actor.update(delta);
    });

    // ── Ambient motion (visual only) ──────────────────────────
    const t = now / 1000;

    // 植物叶片摇摆（_swayLeaves）随 _drawPlant 一并删除 —— 2026-09-22 像素家具管线清理

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
   * 桌套件 sprite（bg 模式专用）：每桌两片。
   * back = 后椅 + 远侧显示器（角色之下）；front = 近侧桌面/前椅（角色之上，只挡腿）。
   * 白桌面楔必须在 front，不能进 back。
   * 深度：back = base-2，A 角色 = base-1，front = base，B 角色 = base+1。
   *
   * 2026-09-23：**前台柜台走同一套两层结构**（`FRONDESK_SLOT`）。
   * 它不在 DESKS 网格里，故单独渲染；素材是**分层两片**（不是单片）——
   * 单片会把坐进内嵌椅的 HR 整个吞掉（只剩头顶），分层后 HR 夹在
   * back（椅+显示器+白台面）与 front（木柜面+蓝牌）之间，正确露头、正确挡腿。
   * zIndex 用 `deskDepthBase` 同口径（柜台底边）；前台在画面前方（world y 最大）
   * ⇒ 天然压在其他工位 sprites 之上。
   */
  private _drawFurnitureSprites(parent: PIXI.Container): void {
    for (const desk of [...DESKS, FRONDESK_SLOT]) {
      const set = furnitureSet(desk);
      const base = deskDepthBase(desk);
      const isFront = isFrontDeskSlot(desk);
      const pieces: [string, FurniturePiece, number][] = [];
      if (set.back) {
        pieces.push([
          isFront ? ASSET_URLS.OFFICE_FRONTDESK_BACK : ASSET_URLS.OFFICE_DESK_BACK,
          set.back,
          base - 2,
        ]);
      }
      pieces.push([
        isFront ? ASSET_URLS.OFFICE_FRONTDESK_FRONT : ASSET_URLS.OFFICE_DESK_FRONT,
        set.front,
        base,
      ]);
      // 前台**第三片**：柜台内嵌的黑椅。z = base+2 ⇒ 同时压住角色与木柜面前脸。
      // 为什么必须独立成层（几何见 constants.FRONTDESK_SET.rearChair 的长注释）：
      // 椅子与木柜面原本同层（都在 front 片），而柜面上沿 world 548.38 夹在
      // 角色鞋底 558.0 与椅背顶 511.62 之间 ⇒ 共层时「椅子挡腰腿」与
      // 「椅背不切头」不可兼得（无解），故拆出第三层。
      if (isFront) {
        pieces.push([ASSET_URLS.OFFICE_FRONTDESK_CHAIR, set.front, base + 2]);
      }
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
      if (isFront) this._attachFrontSign(parent, desk, base);
    }
  }

  /**
   * 前台招牌：把「弧面项目名」canvas 作为 sprite 精确覆盖在柜台 sprite 上。
   *
   * 几何：净牌 canvas 与柜台素材**同一坐标系、同一像素尺寸**（284×158），
   * 故只要按柜台 sprite 的缩放比整体覆盖即可 —— 招牌与柜台严丝合缝，
   * 且随柜台一起被场景缩放，无二次重采样。
   *
   * zIndex = base + 0.5：压在柜台 sprite 之上、但低于柜台前的角色（B 位 = base+1）。
   * 前台无 B 位角色，实际恒在最上层，这里留 0.5 只是防止与柜台同 z 时的排序不确定。
   */
  private _attachFrontSign(parent: PIXI.Container, desk: DeskSlot, base: number): void {
    const signTex = this.tex[ASSET_URLS.OFFICE_FRONTDESK_SIGN];
    if (!signTex) return;
    const set = furnitureSet(desk);
    // 净牌 canvas 与柜台素材同尺寸 ⇒ 缩放比一致
    const scaleX = set.front.w / FRONT_SIGN_CANVAS.w;
    const scaleY = set.front.h / FRONT_SIGN_CANVAS.h;

    const canvas = renderFrontSign(
      signTex.source.resource as HTMLImageElement | HTMLCanvasElement,
      this.snapshot.projectName,
    );
    this._frontSignName = this.snapshot.projectName ?? null;
    const tex = PIXI.Texture.from(canvas);
    tex.source.scaleMode = "nearest";
    const s = new PIXI.Sprite(tex);
    s.width = FRONT_SIGN_CANVAS.w * scaleX;
    s.height = FRONT_SIGN_CANVAS.h * scaleY;
    s.x = desk.x + set.front.leftTop.x;
    s.y = desk.y + set.front.leftTop.y;
    s.zIndex = base + 0.5;
    parent.addChild(s);
    this._frontSignSprite = s;
  }

  /**
   * 项目名变化时重绘招牌。仅在值真的变了才重算（canvas 重绘约 145 次 drawImage）。
   * 材质未就绪时静默跳过 —— 下次 `_drawFurnitureSprites` 会用新名字重建。
   */
  private _refreshFrontSign(): void {
    const sprite = this._frontSignSprite;
    if (!sprite) return;
    const name = this.snapshot.projectName ?? null;
    if (name === this._frontSignName) return;
    const signTex = this.tex[ASSET_URLS.OFFICE_FRONTDESK_SIGN];
    if (!signTex) return;
    const canvas = renderFrontSign(
      signTex.source.resource as HTMLImageElement | HTMLCanvasElement,
      name,
    );
    const old = sprite.texture;
    const tex = PIXI.Texture.from(canvas);
    tex.source.scaleMode = "nearest";
    sprite.texture = tex;
    this._frontSignName = name;
    // 旧纹理是运行时生成的，需显式销毁避免泄漏（PIXI 不会自动回收非 Assets 纹理）
    if (old && old !== tex) {
      try {
        old.destroy(true);
      } catch {
        /* ignore：纹理可能仍被其他 sprite 引用 */
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
