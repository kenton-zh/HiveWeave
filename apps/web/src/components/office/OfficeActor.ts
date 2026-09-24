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
  DEV_ANIM_FPS,
  DEV_ANIM_ONESHOT,
  type AgentAnimKey,
  type SheetLayout,
} from "./constants";

/** 帧内脚底锚点（87.5% 帧高），切片时脚底对齐该比例 */
const FOOT_ANCHOR_Y = 0.875;

// ── Label Factory ─────────────────────────────────────────────────

/** 名牌排版常量（规格 §4.7：整块 ≤96px、头顶上方 4px；字号 14/12 贴 2026-09 定稿图） */
const NAME_FONT_SIZE = 14;
const ROLE_FONT_SIZE = 12;
const LABEL_PAD_X = 8;
const LABEL_PAD_Y = 3;
/** 行间负 gap：正文行高自带上下留白，直接叠放会多出空行，贴紧才像一块名签 */
const LABEL_LINE_TIGHTEN = 3;
const LABEL_BG = 0x0f172a;
/**
 * 名牌横向外偏量（world px）。为什么需要（2026-09-23 审计实测）：
 * 同排相邻两席的**角色中心距**只有 **72.5**（席距 dx=166，A 位在桌左 `-50`、B 位在桌右 `+43.5`），
 * 而名牌整块可达 96 ⇒ 居中摆放时相邻两块**重叠 23.5px**（`review-3`(A,x=817) 与 `review-1`(B,x=744.5) 即实例）。
 * 故按座位朝向把名牌**向外**偏：A 位（座在桌左）向左、B 位（座在桌右）向右。
 * 16 使中心距 72.5 + 2×16 = 104.5 > 96 ✓（留 8.5 缝隙）。
 */
const LABEL_SIDE_SHIFT = 16;
/**
 * 名牌底板不透明度。0.72 → 0.84 → 0.93（2026-09-23）：压在橙木地板上低 alpha 会被
 * 稀释成"半透明灰"，两行对比度一起掉；定稿图是近实心深底，抬到 0.93 才贴那个观感。
 * 底板够实 ⇒ 名字行去掉黑描边（描边糊 12px 中文笔画，定稿图也是无描边白字）。
 */
const LABEL_BG_ALPHA = 0.93;
const LABEL_HEAD_GAP = 4;
/**
 * 前台名牌的**额外下压量**（world px，正 = 往下压）。
 *
 * 为什么需要（2026-09-23 浏览器实测）：名牌默认挂在「头顶上方 4px」，而头顶是按
 * **整帧高**算的 —— `-sprite.scale.y × texture.height × FOOT_ANCHOR_Y` ≈ -67.2。
 * 工位角色在桌子后面，这个高度没问题；但**前台柜台只有世界高 121**，角色坐在柜台后
 * 时头顶只比柜台台面高约 41 ⇒ 名牌再往上飘 100 就落到**后排工位的桌面上方**，
 * 实测截到「HR」名牌压在 build-1 桌面上，观感像别人的牌子。
 *
 * 压到柜台顶沿上方一点即可：41 - 24 ≈ 17 ⇒ 名牌底边停在柜台上方，与 HR 头顶贴合。
 */
const FRONT_LABEL_EXTRA_DROP = 24;
/** 名字行文字上限 72px（sans 12px 实测宽度；整行另加 GEM+GAP=14 ⇒ 最宽 86） */
const NAME_MAX_TEXT_W = 72;
/** 职位行文字上限 84px（整块 96 封顶；长职务仍靠 fitText 截断） */
const ROLE_MAX_TEXT_W = 84;
/** 名字行左侧部门菱形宝石（◆）：宽 + 与名字的间距；整行最宽 = 9+5+72 = 86 < 96 不裁字 */
const GEM_SIZE = 9;
const GEM_GAP = 5;
/** 职位行固定浅灰（部门色让给宝石）：比 0x94a3b8 再抬一档，11px 中文在深底上要更亮才不发闷 */
const LABEL_ROLE_COLOR = 0xb0b9c6;

/**
 * 名牌文字工厂。底板 α=0.93 后名字行不再描边（描边糊中文）；职位行本就不描边。
 * 若未来要在浅底上加字，描边走 `TextStyle.stroke`（Pixi 按字形轮廓画硬边），
 * 不用 `BlurFilter`：滤镜在 100+ 名牌下每帧多一次离屏 pass。
 */
function makeLabel(text: string, size: number, color: number): PIXI.Text {
  return new PIXI.Text({
    text,
    style: {
      // 中文名牌不用 monospace（CJK 回退字重发虚）；sans 栈贴近定稿图的干净无衬线
      fontFamily: '"Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
      fontSize: size,
      fill: color,
      fontWeight: "700",
      align: "center",
    },
  });
}

/**
 * 按**实测渲染宽度**截断（超出补 "…"）。用宽度而非字数表，是因为 role 有时直接是
 * 后端给的中文职务（实测有 `Three.js技术负责人`），中英混排下字数与宽度不成比例。
 */
function fitText(t: PIXI.Text, maxW: number): void {
  if (t.width <= maxW) return;
  const raw = t.text;
  for (let n = raw.length - 1; n > 0; n--) {
    t.text = raw.slice(0, n) + "…";
    if (t.width <= maxW) return;
  }
  t.text = "…";
}

// ── Role Display Name ─────────────────────────────────────────────

/** 后端 role（英文 key）→ 中文显示名，对齐 agent_templates 的职务列 */
const ROLE_LABELS: Record<string, string> = {
  ceo: "CEO",
  hr: "HR",
  qa_lead: "QA 负责人",
  test_engineer: "测试工程师",
  developer: "开发工程师",
  architect: "架构师",
  manager: "经理",
  code_reviewer: "代码审查",
  security_auditor: "安全审计",
};

/**
 * 兜底「员工」而不是原样显示 role：裸 key（`web_perf_auditor`）直接上名牌比不显示更糟。
 * 但后端部分项目直接下发中文职务，这类必须原样保留 —— 否则按 key 查不到会被吞成「员工」。
 */
function roleDisplayName(role: string | undefined | null): string {
  const raw = (role ?? "").trim();
  if (!raw) return "员工";
  const mapped = ROLE_LABELS[raw];
  if (mapped) return mapped;
  return /[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]/.test(raw) ? raw : "员工";
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

/**
 * 名牌宝石提亮色（名字行 ◆ 部门色专用）：深色主色（尤其兜底的 slate `0x64748b`）落在深色底板上等于看不见。
 * 按比例放大三通道（保 HSL 的色相与饱和度，只抬亮度），阈值取「最亮通道 ≥200」：
 * amber/blue/red/purple/indigo/yellow/cyan 本来就在阈值上，取值一字不变，
 * 只有兜底的 slate 被抬成浅灰蓝。**只作用于名牌宝石**，职位行是固定灰（LABEL_ROLE_COLOR），
 * 角色身体的 accent 仍用原主色。
 */
function labelRoleColor(agent: OfficeAgent): number {
  const c = roleColor(agent);
  const r = (c >> 16) & 0xff;
  const g = (c >> 8) & 0xff;
  const b = c & 0xff;
  const max = Math.max(r, g, b);
  if (max >= 200) return c;
  const k = 200 / Math.max(1, max);
  const lift = (v: number) => Math.min(255, Math.round(v * k));
  return (lift(r) << 16) | (lift(g) << 8) | lift(b);
}

// ── Actor ─────────────────────────────────────────────────────────

export class OfficeActor {
  readonly container = new PIXI.Container();
  readonly agent: OfficeAgent;

  private fsm = new AgentStateMachine();
  private shadow = new PIXI.Graphics();
  private ring = new PIXI.Graphics();
  /** 像素 sprite（32×48 帧，整表切片自管切换）；无贴图时走程序化 body/face */
  private sprite: PIXI.Sprite | null = null;
  /** sheet 帧纹理（32×48/帧，整表切片）；无帧动画时为空 */
  private frameTextures: PIXI.Texture[] = [];
  /** 状态 → 帧序（null = 旧单帧 sheet，走程序摆动） */
  private animSeqs: Record<AgentAnimKey, number[]> | null = null;
  private animSeqKey: AgentAnimKey | null = null;
  private frameIdx = 0;
  private frameTimer = 0;
  /** 当前激活的帧序（getup 为坐下序列倒序；推进循环用它，避免读到静态 getup 表） */
  private activeFrames: number[] = [];
  /** 目标是否为本人工位（决定坐下/起身） */
  private atDesk = false;
  /** 坐姿朝向（对应场景两种椅子朝向） */
  private sitVariant: "A" | "B" = "A";
  /** 是否坐前台（前台名牌要额外压低 —— 理由见 FRONT_LABEL_EXTRA_DROP） */
  private atFrontDesk = false;
  private sitPhase: "standing" | "sitdown" | "sitting" | "getup" = "standing";
  // 程序化回退（缺贴图时）
  private body: PIXI.Graphics | null = null;
  private face: PIXI.Graphics | null = null;
  private workDots = new PIXI.Graphics();
  private label: PIXI.Container;
  /** 名牌整块高度（两行 + 内边距），构造期量一次用于「头顶上方 4px」定位 */
  private labelHeight = 0;
  /** 名牌基准 y（构造期定稿）。`setTarget` 在此之上按是否前台加偏移。 */
  private _labelBaseY = 0;
  private bubble: PIXI.Container;
  private bubbleDots = new PIXI.Graphics();

  // Position interpolation
  private target = { x: 0, y: 0 };
  private walkPhase = 0;
  private _selected = false;
  private _lastSel = false;
  /** alert 进入时的 start timestamp，-1 表示未在脉冲 */
  private _pulseStart = -1;
  private bubbleTex: PIXI.Texture | null;

  constructor(
    agent: OfficeAgent,
    onSelect: (id: string) => void,
    sheetTex: PIXI.Texture | null,
    layout: SheetLayout | null,
    animSeqs: Record<AgentAnimKey, number[]> | null,
    bubbleTex: PIXI.Texture | null,
  ) {
    this.agent = agent;
    this.bubbleTex = bubbleTex;
    this.animSeqs = animSeqs;
    this.label = this._buildLabel(agent.name, agent.role);
    this.bubble = this._buildBubble(bubbleTex);

    // Interaction
    this.container.eventMode = "static";
    this.container.cursor = "pointer";
    this.container.on("pointertap", () => onSelect(agent.id));

    // Layer children (bottom → top)
    this.container.addChild(this.shadow, this.ring);
    if (sheetTex) {
      // 采样模式按布局：旧 16-bit 表用 nearest 保像素锐利；新 2K 高清表与背景同质感用 linear
      if (layout) sheetTex.source.scaleMode = layout.scaleMode;
      // sheetTex 是整张 sheet；按 layout 切片出全部帧（frameW×frameH），供 Sprite 按状态切换帧。
      const cols = layout?.cols ?? 1;
      const rows = layout?.rows ?? 1;
      const fw = layout?.frameW ?? 32;
      const fh = layout?.frameH ?? 48;
      for (let r = 0; r < rows; r++) {
        for (let c = 0; c < cols; c++) {
          const t = new PIXI.Texture({
            source: sheetTex.source,
            frame: new PIXI.Rectangle(c * fw, r * fh, fw, fh),
            orig: new PIXI.Rectangle(0, 0, fw, fh),
          });
          // 手动更新 UV（PixiJS v8 中 new Texture(frame,orig) 后需要调用一次，否则默认 UV 是整图）
          t.updateUvs();
          this.frameTextures.push(t);
        }
      }
      const firstSeq = (animSeqs?.idle ?? [0])
        .map((i) => this.frameTextures[i])
        .filter(Boolean);
      const spr = new PIXI.Sprite(firstSeq.length ? firstSeq[0] : this.frameTextures[0]);
      spr.anchor.set(0.5, FOOT_ANCHOR_Y);
      // 显示尺寸 = 帧高 × scale：旧表 48×1.6 ≈ 77；新表 96×0.8 ≈ 77（与放大家具 1.6-1.8x 比例协调）
      spr.scale.set(layout?.scale ?? 1.6);
      spr.alpha = 1;
      this.sprite = spr;
      this.animSeqKey = "idle";
      this.container.addChild(spr);
    } else {
      this.body = new PIXI.Graphics();
      this.face = new PIXI.Graphics();
      this.body.alpha = 1;
      this.face.alpha = 1;
      this.container.addChild(this.body, this.face);
      this._drawBody();
    }
    // shadow 只画一次（脚底位置不变，bounce 只改变 scale）；须在 sprite/body 定案后调用
    this._drawShadow();
    this.container.addChild(this.workDots, this.bubble, this.label);
    // 名牌底边落在「头顶上方 4px」：sprite 模式头顶 = 帧高 × 脚底锚点比例（96×0.875×0.8 ≈ 67），
    // 程序化模式发梢硬编码在 -33（见 _drawBody 的 hair roundRect）。
    // 名牌容器原点 = 底板顶边中线，故 y = 头顶 - 4 - 整个名牌高度。
    const headTop = this.sprite
      ? -Math.abs(this.sprite.scale.y) * this.sprite.texture.height * FOOT_ANCHOR_Y
      : -33;
    this.label.y = headTop - LABEL_HEAD_GAP - this.labelHeight;
    this._labelBaseY = this.label.y;
    this.bubble.visible = false;

    // Listen for state transitions (e.g. bubble pop animation)
    this.fsm.onTransitionTo((_from, to) => {
      if (to === "alert") this._pulseBubble();
    });
  }

  // ── Public API ────────────────────────────────────────────────

  /** Set the desired state for this frame. Called every tick. */
  setTarget(
    x: number,
    y: number,
    input: StateInput,
    selected: boolean,
    atDesk = false,
    sitVariant: "A" | "B" = "A",
    atFrontDesk = false,
  ): void {
    this.target = { x, y };
    this._selected = selected;
    this.atDesk = atDesk;
    this.sitVariant = sitVariant;
    this.atFrontDesk = atFrontDesk;

    const output = this.fsm.evaluate(input);
    this.bubble.visible = output.showBubble;
    this.bubble.y = input.talking ? -56 : -48;
    // 名牌默认常显（§4.7「默认开」）。此前只在选中/说话时显示，画面上根本认不出谁是谁；
    // 选中改用轻微放大做强调，不再兼作可见性开关（关闭开关留给 HUD，未接）。
    this.label.scale.set(selected ? 1.08 : 1);
    // 名牌按座位朝向向外偏，避免与邻座名牌重叠（理由见 LABEL_SIDE_SHIFT）。
    // 只在取值变化时赋值：这是一帧一次的 setTarget，不是构造期，避免无谓的 transform 记号。
    // ⚠ 前台不参与横向外偏：柜台前无邻座，偏出去反而压到旁边工位的桌面上。
    const sideShift = this.atFrontDesk
      ? 0
      : sitVariant === "B"
        ? LABEL_SIDE_SHIFT
        : -LABEL_SIDE_SHIFT;
    if (this.label.x !== sideShift) this.label.x = sideShift;
    // 前台额外下压（理由见 FRONT_LABEL_EXTRA_DROP）
    const labelY = this._labelBaseY + (this.atFrontDesk ? FRONT_LABEL_EXTRA_DROP : 0);
    if (this.label.y !== labelY) this.label.y = labelY;
    const a = stateAlpha(output.visual);
    if (this.body) this.body.alpha = a;
    if (this.face) this.face.alpha = a;
    if (this.sprite) this.sprite.alpha = a;
  }

  /** 落座深度覆盖（A = base-1 挡腿；B = base+1 坐进近侧椅）；null = 走动按自身 y */
  private depthOverride: number | null = null;

  /** 设置落座深度基准（由 OfficeScene 每帧传入；离桌传 null 回落到自身 y） */
  setDepth(value: number | null): void {
    this.depthOverride = value;
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

    // Position interpolation（一次性坐/起身期间暂停插值，防止起身过程中滑走）
    const paused = !!this.animSeqs && (this.sitPhase === "sitdown" || this.sitPhase === "getup");
    if (!paused) {
      const dx = this.target.x - this.container.x;
      const dy = this.target.y - this.container.y;
      this.container.x += dx * Math.min(1, delta * WALK_SPEED);
      this.container.y += dy * Math.min(1, delta * WALK_SPEED);
    }

    // 深度排序：落座时用所属桌套件的基准（被自己的桌面遮住），否则按自身 y
    this.container.zIndex =
      this.depthOverride ?? Math.round(this.container.y);

    // B 位近侧椅：水平翻转朝向桌子（紫衣/dev 帧默认朝右）
    if (this.sprite) {
      const sx = Math.abs(this.sprite.scale.x);
      this.sprite.scale.x = this.atDesk && this.sitVariant === "B" ? -sx : sx;
    }

    // 动画选型：帧动画（dev 女孩 sheet）优先，旧单帧 sheet/程序化 body 走摆动兜底
    const state = this.fsm.current;
    // 到桌坐下 / 离桌起身（仅帧动画模式）：坐下一次性动作 → 坐姿循环；离桌起身一次性 → 行走
    if (this.animSeqs) {
      if (this.atDesk && !walking && this.sitPhase === "standing") this.sitPhase = "sitdown";
      if (!this.atDesk && (this.sitPhase === "sitting" || this.sitPhase === "sitdown")) {
        this.sitPhase = "getup";
      }
    }
    const animKey: AgentAnimKey = this.animSeqs
      ? state === "alert" || state === "talking"
        ? state   // ping/聊天优先：坐姿时被 ping 也弹出问号帧
        : this.sitPhase === "sitdown"
          ? this.sitVariant === "B"
            ? "sitdown_b"
            : "sitdown"
          : this.sitPhase === "getup"
            ? "getup"
            : this.sitPhase === "sitting" && !walking
              ? state === "working"
                ? "working"
                : this.sitVariant === "B"
                  ? "sitting_b"
                  : "sitting"
              : state === "walking" || walking
                ? "walking"
                : state === "working"
                  ? "working"
                  : "idle"
      : state === "walking" || walking
        ? "walking"
        : state === "working"
          ? "working"
          : "idle";
    // talk/alert/坐系列无独立程序参数，借 idle 的摆动
    const p =
      AGENT_PROC_ANIM[animKey === "walking" || animKey === "working" ? animKey : "idle"];

    // Bob 位移（程序化 body/face）；帧动画模式下站立循环减半、关 rotation（帧自带姿态）
    const bob = Math.sin(this.walkPhase * (p.bobHz / BOB_FREQ)) * p.bobAmp;
    if (this.body && this.face) {
      this.body.y = bob;
      this.face.y = bob;
    }
    if (this.sprite) {
      if (this.animSeqs) {
        this._setAnim(animKey);
        // 自管帧计时器：以场景 delta（帧≈1/60s）推进，fps 由 DEV_ANIM_FPS 控制；
        // 大 delta（切后台回来）一次消耗多帧，避免动画变慢
        const fps = DEV_ANIM_FPS[animKey] ?? 4;
        this.frameTimer += (delta * fps) / 60;
        const adv = Math.floor(this.frameTimer);
        if (adv > 0) {
          this.frameTimer -= adv;
          let moved = 0;
          while (moved < adv && moved < this.activeFrames.length * 2) {
            const next = (this.frameIdx + 1) % this.activeFrames.length;
            if (next === 0 && DEV_ANIM_ONESHOT.includes(animKey)) {
              // 一次性动作播完：转入后续 phase（sitting / standing），末帧保留显示
              if (animKey === "sitdown" || animKey === "sitdown_b") this.sitPhase = "sitting";
              if (animKey === "getup") this.sitPhase = "standing";
              break;
            }
            this.frameIdx = next;
            this.sprite.texture = this.frameTextures[this.activeFrames[this.frameIdx]];
            moved++;
          }
        }
        // 坐下/起身/坐姿帧不叠加 bob，站立循环微幅 bob
        this.sprite.y =
          animKey === "idle" || animKey === "walking" || animKey === "working"
            ? bob * 0.5
            : 0;
        this.sprite.rotation = 0;
      } else {
        this.sprite.y = bob;
        this.sprite.rotation = Math.sin(this.walkPhase * (p.bobHz / BOB_FREQ) * 2) * p.leanAmp;
      }
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
      const ry = this.sprite ? 4 : 34;
      this.ring.clear();
      this.ring.ellipse(0, ry, 21 + pulse * 3.5, 7.5 + pulse * 1.2);
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

  /**
   * 两行名牌：名字行（◆部门宝石 + 12px 白字无描边）+ 职位（11px 固定浅灰）。
   * **只在构造期建一次** —— Pixi Text 自带纹理缓存，只要不重设 `.text` 就不重上传纹理；
   * 名牌随小人走位时更新的是容器 transform，代价接近 0。
   * 深色圆角底板（α=0.93 近实心）撑对比 ⇒ 名字行不再描边（描边糊中文，定稿图亦无）。
   * 部门色只上宝石（labelRoleColor 抬亮，避免兜底 slate 在深底上隐形）；
   * 职位行固定浅灰 —— 与设计定稿一致（色在 ◆，字在灰）。
   */
  private _buildLabel(name: string, role: string): PIXI.Container {
    const c = new PIXI.Container();
    const nameText = makeLabel(name, NAME_FONT_SIZE, 0xf8fafc);
    const roleText = makeLabel(roleDisplayName(role), ROLE_FONT_SIZE, LABEL_ROLE_COLOR);
    fitText(nameText, NAME_MAX_TEXT_W);
    fitText(roleText, ROLE_MAX_TEXT_W);
    // 名字行 = 宝石 + 间距 + 文字，整组水平居中；职位行相对底板居中
    const nameRowW = GEM_SIZE + GEM_GAP + nameText.width;
    nameText.anchor.set(0, 0);
    roleText.anchor.set(0.5, 0);
    nameText.y = LABEL_PAD_Y;
    roleText.y = nameText.y + nameText.height - LABEL_LINE_TIGHTEN;

    const gem = new PIXI.Graphics();
    const hs = GEM_SIZE / 2;
    gem.moveTo(hs, 0);
    gem.lineTo(0, hs);
    gem.lineTo(-hs, 0);
    gem.lineTo(0, -hs);
    gem.closePath();
    gem.fill(labelRoleColor(this.agent));
    const rowX = -nameRowW / 2;
    gem.x = rowX + hs;
    gem.y = nameText.y + nameText.height / 2;
    nameText.x = rowX + GEM_SIZE + GEM_GAP;

    const innerW = Math.max(nameRowW, roleText.width);
    const w = Math.min(96, innerW + LABEL_PAD_X * 2);
    const h = roleText.y + roleText.height + LABEL_PAD_Y;
    const bg = new PIXI.Graphics();
    bg.roundRect(-w / 2, 0, w, h, 8);
    bg.fill({ color: LABEL_BG, alpha: LABEL_BG_ALPHA });
    // 定稿图有发丝描边：1px 冷灰把牌子从木地板/隔板上“描”出来，远看轮廓更定型
    bg.stroke({ width: 1, color: 0x64748b, alpha: 0.95 });

    this.labelHeight = h;
    c.addChild(bg, gem, nameText, roleText);
    return c;
  }

  /** 接触阴影：贴住脚底消除悬浮感（程序化 body 的脚在 y≈34，sprite 帧的脚在 y≈0） */
  private _drawShadow(): void {
    const sy = this.sprite ? 3 : 34;
    const rx = this.sprite ? 15 : 17;
    this.shadow.clear();
    this.shadow.ellipse(0, sy, rx, 5.5);
    this.shadow.fill({ color: 0x0f172a, alpha: 0.22 });
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

  /** 按状态切换帧序列（dev 动画表；旧单帧 sheet 不走到这里） */
  private _setAnim(key: AgentAnimKey): void {
    if (this.animSeqKey === key || !this.animSeqs) return;
    let seq: number[] = this.animSeqs[key];
    if (key === "getup") {
      // 起身 = 当前坐姿朝向的「坐下」序列倒序播放（动作方向一致、服装一致）
      const sitKey = this.sitVariant === "B" ? "sitdown_b" : "sitdown";
      seq = [...this.animSeqs[sitKey]].reverse();
    }
    const frames = seq.map((i) => this.frameTextures[i]).filter(Boolean);
    if (!frames.length) return;
    this.animSeqKey = key;
    this.activeFrames = seq;
    this.frameIdx = 0;
    this.frameTimer = 0;
    // 先切到新序列的帧 0，下一步 timer 推进到帧 1
    this.sprite!.texture = frames[0];
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
