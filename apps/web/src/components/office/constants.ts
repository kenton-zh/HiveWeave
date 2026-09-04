/**
 * Office Scene — Constants
 * World dimensions, desk layout, role colours, asset descriptors.
 */

import * as PIXI from "pixi.js";
import type { AgentVisualState, DeskSlot } from "./types";

// ── World ─────────────────────────────────────────────────────────

export const WORLD_W = 1280;
export const WORLD_H = 720;  // 16:9 对齐 3840×2160 gpt-image-2 官方 4K 背景图（缩 33.3%）
export const TILE_W = 64;
export const TILE_H = 32;

// ── Desk Layout ───────────────────────────────────────────────────

/**
 * 7 个桌位，按背景图 office-scene-bg.png（1672×941，world = px / 1.30625）标定：
 *   中排 3 桌 + 下排 3 桌 + 前台柜台。坐标 = 该桌「落座基准点」，
 *   桌套件/椅位全部由 DESK_SET 的相对偏移从此点推出（角色腰线对齐桌远缘）。
 * 顺序即 getDesk() 的分配顺序；紫衣演示走 purpleDemoSeat()（0 号占前台柜台，
 * 其余按工作位顺序坐 B 位近侧椅）。
 */
export const DESKS: DeskSlot[] = [
  { id: "lead-1",   x: 747, y: 326, role: "lead" },   // 中排中桌
  { id: "lead-2",   x: 966, y: 379, role: "lead" },   // 中排右桌
  { id: "build-1",  x: 510, y: 278, role: "build" },  // 中排左桌
  { id: "build-2",  x: 510, y: 433, role: "build" },  // 下排左桌
  { id: "build-3",  x: 794, y: 510, role: "build" },  // 下排中桌
  { id: "review-1", x: 1019, y: 581, role: "review" },// 下排右桌
  { id: "review-2", x: 563, y: 547, role: "review" }, // 前台柜台（F 视角接待员预留）
];

// ── Common Area (talking / roaming targets) ───────────────────────

/** Gather point when agents are "talking" — 入口前绿地毯旁的大厅等候区 */
export const COMMON_TARGETS = [
  { x: 510, y: 660 },
  { x: 540, y: 668 },
  { x: 570, y: 660 },
  { x: 600, y: 668 },
];

/** Roaming waypoints（办公室 5 个地标之间散步）：
 *  蓝沙发+黄豆袋休息区 / 弧形前台 / 玻璃会议室外 / 厨房吧台 / 电视会客区  */
export const ROAM_WAYPOINTS = [
  { x: 238, y: 462 },  // 左下 蓝双人沙发 + 黄豆袋
  { x: 532, y: 575 },  // HiveWeave 弧形前台 前（前台已移至正门内侧，面向大门）
  { x: 305, y: 210 },  // 玻璃会议室门外 白板旁
  { x: 735, y: 150 },  // 右上 厨房吧台 高脚凳前
  { x: 935, y: 178 },  // 右上 灰色沙发+电视+会客区 地毯边
];

// ── Role Colours ──────────────────────────────────────────────────

export const ROLE_COLORS: Record<string, number> = {
  ceo:              0xf59e0b, // amber
  architect:        0xa855f7, // purple
  manager:          0x3b82f6, // blue
  hr:               0xf43f5e, // rose
  qa:               0xeab308, // yellow
  test_engineer:    0xeab308,
  code_reviewer:    0x818cf8, // indigo
  security_auditor: 0xef4444, // red
  web_perf_auditor: 0x06b6d4, // cyan
  developer:        0x22c55e, // green
  module_dev:       0x22c55e,
};

export const DEFAULT_ROLE_COLOR = 0x64748b; // slate

// ── Agent Visual Parameters ───────────────────────────────────────

/** Walk speed factor (units per tick * delta) */
export const WALK_SPEED = 0.16;
/** Bob amplitude in pixels */
export const BOB_AMPLITUDE = 1.4;
/** Bob frequency (radians per tick) */
export const BOB_FREQ = 0.14;

// ── Desk Assignment ───────────────────────────────────────────────

export function resolveDeskRole(role: string): "lead" | "build" | "review" {
  if (role === "ceo" || role === "architect" || role === "manager") return "lead";
  if (/qa|test|review|audit/.test(role)) return "review";
  return "build";
}

export function getDesk(agentIndex: number, role: string): DeskSlot {
  const pool = DESKS.filter((d) => d.role === resolveDeskRole(role));
  return pool[agentIndex % Math.max(pool.length, 1)] ?? DESKS[agentIndex % DESKS.length];
}

/**
 * 紫衣演示落座：0 号接待员 A（柜台后），其余工位 B（近侧可见椅）。
 * 近侧椅完整出现在画面里，人 zIndex 高于 front 才能「坐进椅子」；
 * 远侧后椅只作空位（角色比椅背大，后座上看不见坐姿）。
 */
export function purpleDemoSeat(index: number): { desk: DeskSlot; variant: "A" | "B" } {
  const reception = DESKS.find((d) => d.id === "review-2") ?? DESKS[DESKS.length - 1];
  const work = DESKS.filter((d) => d.id !== "review-2");
  if (index === 0) return { desk: reception, variant: "A" };
  return {
    desk: work[(index - 1) % Math.max(work.length, 1)],
    variant: "B",
  };
}

// ── Isometric Projection ──────────────────────────────────────────

export function isoToScreen(tx: number, ty: number) {
  return {
    x: WORLD_W / 2 + (tx - ty) * (TILE_W / 2),
    y: 128 + (tx + ty) * (TILE_H / 2),
  };
}

// ── Raster Assets (public/office-assets) ──────────────────────────

/**
 * 像素资产 URL（Vite public 目录静态文件）。
 * 生成规格见 docs/前端像素办公室规格.md §10；等距风格与场景一致。
 */
export const ASSET_URLS = {
  AGENT_DEV: "/office-assets/agent-dev-sheet.png",
  AGENT_MANAGER: "/office-assets/agent-manager-sheet.png",
  AGENT_QA: "/office-assets/agent-qa-sheet.png",
  DESK: "/office-assets/desk-computer.png",
  FLOOR_TILE: "/office-assets/floor-tile.png",
  CHAIR: "/office-assets/office-chair.png",
  PLANT: "/office-assets/plant.png",
  SPEECH_BUBBLE: "/office-assets/speech-bubble.png",
  WALL_WINDOW: "/office-assets/wall-window.png",
  WHITEBOARD: "/office-assets/whiteboard.png",
  /** 整间办公室背景（PIL 逐行插值去桌椅版，1672×941：地板/墙/沙发/吧台/绿植等
   *  不与角色交互的陈设；桌椅全部改由引擎 sprite 渲染。原带桌版备份
   *  office-scene-bg.with-desks.bak.png）。场景内缩到 WORLD_W×WORLD_H = 1280×720。 */
  OFFICE_BG: "/office-assets/office-scene-bg.png",
  /** 分层家具 sprite。BACK = 后椅+远侧显示器；FRONT = 近侧桌面/前椅（挡腿）。
   *  白桌面楔必须留在 FRONT。query 只为强刷 public/ 无 hash 的 PNG。 */
  OFFICE_DESK_BACK: "/office-assets/office-desk-back.png?v=chair2",
  OFFICE_DESK_FRONT: "/office-assets/office-desk-front.png?v=chair2",
  OFFICE_FRONTDESK_SET: "/office-assets/office-frontdesk-set.png",
  /** 紫衣女孩打字动画表（2026-08-30：参考图锚定生成，2×2 = 4 帧打字循环。
   *  实测为全身坐姿帧（非腰截断）：内容底 y=83 ≈ anchor 0.875×96=84，即 0.875 处是鞋底；
   *  腿部由桌套件 front 片在运行时遮挡） */
  AGENT_PURPLE: "/office-assets/agent-purple-typing-sheet.png",
} as const;

/** 全部需预载的资产 URL（OfficeScene.mount 中统一 Assets.load） */
export const ASSET_LOAD_LIST: string[] = Object.values(ASSET_URLS);

// ── Agent Spritesheet Layout ──────────────────────────────────────

/**
 * 角色 sheet 布局（按 URL 区分）：cols×rows 帧网格，frame 尺寸 frameW×frameH，
 * 显示缩放 scale（world 显示尺寸 ≈ frameH×scale），采样模式 scaleMode。
 * - agent-dev-sheet（2026-08-22 v2）→ 512×384 = 8 列 × 4 行 × 64×96 帧（2K 图生图切片，
 *   高清柔和 Q 版 32 格：呼吸/行走/打字/喝咖啡/点头/跳跃/问号/坐下/坐姿/起身/冒烟/递卡），
 *   linear 平滑采样与办公室背景同质感，scale 0.8。
 * - agent-manager/qa-sheet（旧）→ 128×144 = 4×3 × 32×48 单帧表，nearest 保持像素锐利。
 * 角色内容在帧内 87.5% 处触底（脚底 anchor = 0.875）。
 */
export interface SheetLayout {
  cols: number;
  rows: number;
  frameW: number;
  frameH: number;
  scale: number;
  scaleMode: "nearest" | "linear";
}

export const SHEET_LAYOUTS: Record<string, SheetLayout> = {
  // scaleMode 一律 nearest：linear 会在帧边界采样到相邻帧像素（frame bleeding），
  // 浏览器实拍表现为角色周围半透明矩形"面纱"（2026-09-01 实测，nearest 后消失）
  [ASSET_URLS.AGENT_DEV]: { cols: 8, rows: 4, frameW: 64, frameH: 96, scale: 0.8, scaleMode: "nearest" },
  [ASSET_URLS.AGENT_MANAGER]: { cols: 4, rows: 3, frameW: 32, frameH: 48, scale: 1.6, scaleMode: "nearest" },
  [ASSET_URLS.AGENT_QA]: { cols: 4, rows: 3, frameW: 32, frameH: 48, scale: 1.6, scaleMode: "nearest" },
  [ASSET_URLS.AGENT_PURPLE]: { cols: 2, rows: 2, frameW: 96, frameH: 96, scale: 0.8, scaleMode: "nearest" },
};

/**
 * 紫衣女孩动画帧表（2×2 = 4 帧打字循环；只有 A 朝向坐姿）。
 * 演示模式：idle 也循环打字帧（每张桌的女孩持续打字）；
 * 坐下/起身系列指向静态帧 0（该 sheet 无此动作，sitPhase 不产生视觉跳变）。
 */
export const PURPLE_ANIM_SEQS: Record<AgentAnimKey, number[]> = {
  idle: [0, 1, 2, 3],
  walking: [0],
  working: [0, 1, 2, 3],
  talking: [0],
  alert: [0],
  sitdown: [0],
  sitting: [0, 1, 2, 3],
  sitdown_b: [0],
  sitting_b: [0],
  getup: [0],
};

/** 演示开关：所有 agent 默认都用紫衣女孩 sheet（视觉统一） */
export const PURPLE_DEMO_ALL_AGENTS = true;

/**
 * 单角色动画演示开关：指定 `startIndex` 起的 1 个角色（按 org tree id 排序后）
 * 切回 dev 满帧 sheet（呼吸/行走/打字/喝咖啡/点头/跳跃/问号/坐下/坐姿/起身），
 * 让其能基于 FSM 状态驱动出可见的多姿态切换。
 *
 * 设计动机：紫衣女孩 sheet 只有 4 帧打字循环，没有 idle/walking/talking/alert 分态；
 * 单一 sprite 永远「卡在打字」无法体现整个动画系统的能力。先让一名角色跑起来 →
 * 验证 dev sheet + FSM + DEV_ANIM_SEQS 全链路通畅 → 后续按角色正式上线时
 * 把 PURPLE_DEMO_ALL_AGENTS = false 即可全员就位。
 */
/**
 * 单角色动画演示开关：指定索引 0 的角色（按 org tree id 排序后）切回 dev 满帧 sheet
 * （呼吸/行走/打字/喝咖啡/点头/跳跃/问号/坐下/坐姿/起身），让其基于 FSM 状态驱动出
 * 可见的多姿态切换；其余角色仍用紫衣 sheet 保持视觉统一。
 *
 * 设计动机：紫衣 sheet 只有 4 帧打字循环，且 idle/working 指向同一序列，
 * 角色永远「卡在打字」，无法体现整个动画系统的能力。先让一名角色跑通
 * dev sheet + FSM + DEV_ANIM_SEQS 全链路，后续全员上线时把
 * PURPLE_DEMO_ALL_AGENTS = false 即可按角色就位。
 *
 * ── 视角 / 遮挡硬约束（改动前务必读）─────────────────────────
 * 1. 接待员走 A（柜台后，无椅）。工位紫衣走 B（近侧可见椅）：zIndex = base+1
 *    画在 front 片前面，人坐进那把空着的黑椅；scale.x 翻转朝向桌子。
 *    远侧后椅留在 BACK，空着（角色比椅背大，后座上看不见「坐进椅子」）。
 * 2. 演示循环只在**坐姿系**状态间切换（working 打字 ↔ idle 坐姿呼吸），
 *    绝不触发 walking / talking / alert：
 *      - walking 序列会驱动角色离座平移，坐姿与桌面的遮挡关系当场失效；
 *      - talking/alert 序列在 dev sheet 中是站姿/跳跃帧，一旦播放人物会
 *        整个浮到桌面之上。
 * 3. 角色位置冻结在座位上（atDesk 恒 true），不做 roaming / 聚集位移。
 */
export const FIRST_AGENT_FULL_ANIMATIONS = true;
/** 演示模式：一个完整「打字 → 停歇呼吸」周期的时长（毫秒） */
export const FIRST_AGENT_DEMO_CYCLE_MS = 9000;
/** 演示模式：周期内处于「打字」状态的时长（毫秒）；剩余时间走坐姿呼吸 */
export const FIRST_AGENT_TYPE_MS = 5500;

/**
 * dev sheet 落座 y 校正（world 单位，正值 = 向下移）。
 *
 * 2026-08-31 重标定：当前两套 sheet 的锚点语义一致 ——
 *   紫衣 sheet（2026-08-30 版）：全身坐姿帧，内容底 y=83 ≈ anchor(0.875×96=84)，0.875 处是**鞋底**；
 *   dev  sheet（v2）：全身坐姿帧（实测帧 8/9 bottom=84），0.875 处同样是**鞋底**。
 * 两套 sheet 共用 seatPos() 时无需再互相校正，本常量归零。
 * （旧值 +12 是按「紫衣=腰截断帧」的旧 sheet 标定的，已失效。）
 * 若后续替换 sheet 且锚点语义变化，在这里加回校正，不要改 seatPos
 * （seatPos 是三池共用的几何标定，改它会影响全部角色）。
 */
export const DEV_SHEET_SEAT_Y_OFFSET = 0;

// ── Furniture Layer（2026-08-30 分层化：桌套件由引擎 sprite 渲染，
//    桌套件 → 角色（躯干露桌面、腿被桌面遮）按锚点 y 画家算法排序） ─────────

/** 家具 sprite 单片：显示尺寸 + 左上角相对槽位偏移 */
export interface FurniturePiece {
  w: number;
  h: number;
  leftTop: { x: number; y: number };
}

/**
 * 桌套件几何（world = 原图像素 / 1.30625）。抠图取自 build-1 槽位（DESKS[2]）。
 * 深度三层：back（后椅 + 远侧显示器，角色之下）→ 角色 → front（近侧桌面/前椅，挡腿）。
 * FRONT 顶边按列走等距远缘 `y = -20.8 + 0.5*|rel_x|`；白桌面楔留在 FRONT，
 * 禁止放进 BACK（人会坐到桌面上）。后椅整把在 BACK，椅背从肩后露出。
 */
export const DESK_SET: {
  back: FurniturePiece | null;
  front: FurniturePiece;
  rearChair: { dx: number; dy: number };
  frontChair: { dx: number; dy: number };
} = {
  back: { w: 159.2, h: 127.8, leftTop: { x: -78.2, y: -62.1 } },  // 208×167：后椅+远侧显示器
  front: { w: 159.2, h: 88.8, leftTop: { x: -78.2, y: -20.8 } },
  /**
   * 后椅（A 位）：锚点 x / 鞋底锚点 y（相对槽位，world 单位）。
   * 2026-09-04：后椅从 BACK 抠除后人看起来坐在桌面上、近侧空椅才像「椅子」。
   * 椅背加回 BACK 后扫参：dx=-50 对准椅心，dy=18 腰线贴近该列远缘（腿被 FRONT 挡住）。
   * dx=-40 偏右离开椅背；dy=8 整个人压在桌面上。
   */
  rearChair: { dx: -50.0, dy: 18.0 },
  /**
   * 前椅（B 位）椅面中心 x / 鞋底锚点 y（相对槽位）。
   * 紫衣演示工位走这个座位：z 在 front 之上，人水平翻转朝向桌子。
   * 2026-08-31 实测：前椅完整可见，bbox rel_x 22.8..64.2（中心 43.5）、
   * 椅背顶 -7.8、椅脚底 +55.8；椅面顶 ≈ +11.7，鞋底 = 椅面顶 + 16.8 ≈ +28.5。
   */
  frontChair: { dx: 43.5, dy: 28.5 },
};

/** 前台套件（顶部残影带已切除；柜台整体为一片，无椅 —— 接待员躯干从柜台上缘露出）
 *  rearChair dy：锚点(鞋底)放 -14 时腰线（锚点上方 19.2px）恰好压柜台上缘 -33.3+3，
 *  头肩完整露出柜台；旧值 +26 会让整个人沉到柜台后只露头顶。 */
export const FRONTDESK_SET: typeof DESK_SET = {
  back: null,
  front: { w: 208.2, h: 71.2, leftTop: { x: -101.4, y: -33.3 } },
  rearChair: { dx: -20.0, dy: -14.0 },
  frontChair: { dx: 50.9, dy: 37.1 },
};

export type FurnitureSet = typeof DESK_SET;

/** 槽位 → 家具套件（前台槽位用前台几何，其余用标准桌套件） */
export function furnitureSet(desk: DeskSlot): FurnitureSet {
  return desk.id === "review-2" ? FRONTDESK_SET : DESK_SET;
}

/**
 * 深度基准线（world y）：取 front 片底边。
 * front 片 zIndex = base；A 位角色 = base-1（被桌面挡腿），B 位 = base+1（坐进近侧椅）；
 * back 片 = base-2。跨桌用同一基准线不产生穿插。
 */
export function deskDepthBase(desk: DeskSlot): number {
  const set = furnitureSet(desk);
  return desk.y + set.front.leftTop.y + set.front.h;
}

/**
 * 落座位（锚点 0.875 = 角色 sheet 的鞋底）。角色 sheet 是全身坐姿帧，
 * 腰线（锚点上方 19.2 world px）对齐桌远缘，大腿及以下由 front 片遮挡。
 */
export function seatPos(desk: DeskSlot, variant: "A" | "B" = "A") {
  const set = furnitureSet(desk);
  const c = variant === "B" ? set.frontChair : set.rearChair;
  return { x: desk.x + c.dx, y: desk.y + c.dy };
}

/**
 * 落座位（含 sheet 锚点语义校正）—— 建角与每帧驱动都应走这个入口。
 *
 * 2026-08-31 实测：两种 sheet 的 FOOT_ANCHOR_Y(0.875) 处内容一致 ——
 * 都是全身坐姿帧的**鞋底**（紫衣内容底 y=83、dev 坐姿帧 bottom=84），
 * 故 DEV_SHEET_SEAT_Y_OFFSET = 0，共用 seatPos() 即可。
 *
 * 若未来替换 sheet 且锚点语义不同（如回到腰截断帧），在此处按 sheet 加回
 * y 校正（正 = 下压），不要改 seatPos（三池共用的几何标定）。
 * 只校正 y，不动 x（各 sheet 横向中心一致）。
 */
export function seatPosFor(
  sheetUrl: string,
  desk: DeskSlot,
  variant: "A" | "B" = "A",
) {
  const base = seatPos(desk, variant);
  const isDevSheet = sheetUrl === ASSET_URLS.AGENT_DEV;
  return {
    x: base.x,
    y: base.y + (isDevSheet ? DEV_SHEET_SEAT_Y_OFFSET : 0),
  };
}

/** 动画键 = FSM 视觉态 + actor 派生动作（坐 A/B 两朝向 + 起身；冒烟/递卡暂无事件源未接线） */
export type AgentAnimKey =
  | AgentVisualState
  | "sitdown"
  | "sitdown_b"
  | "sitting"
  | "sitting_b"
  | "getup";

/**
 * dev 女孩动画帧序（0 基、行优先、8 列：1-8 呼吸/行走/打字、9-16 打字/咖啡/点头、
 * 17-24 跳/问号/坐下A、25-32 坐姿A/坐下B/坐姿B）。
 * FSM 映射：idle→呼吸，walking→行走，working→打字，talking→点头，alert→问号；
 * 到桌坐下：sitdown(A朝向)/sitdown_b(B朝向)（一次性）→ sitting/sitting_b（循环）；
 * 离桌：getup（一次性，= 当前朝向坐下序列的倒序播放）。
 */
export const DEV_ANIM_SEQS: Record<AgentAnimKey, number[]> = {
  idle: [0, 1],
  walking: [2, 3, 4, 5],
  /**
   * 打字：2026-08-31 修正 —— 原 [6,7,8,9] 混入了 2 帧站姿，已改为纯坐姿帧 [8,9]。
   *
   * 实测（scripts/analyze-frames.mjs，逐帧内容包围盒；脚底基准线 = 0.875×96 = 84）：
   *   帧 6 top=10 bottom=69 ／ 帧 7 top=11 bottom=69  ← bottom ≠ 84，是站姿，
   *        按 FOOT_ANCHOR_Y 对齐到椅脚后会**浮空 15px**，且整个上半身浮到桌面之上
   *        （A 位遮挡只有约 7.8px，压不住站立姿态）；
   *   帧 8 top=26 bottom=84 ／ 帧 9 top=27 bottom=84  ← bottom = 84，确认坐姿，
   *        且 top 比 sitting(17/19) 低约 9px = 身体前倾敲键盘，正是打字姿态。
   * 混入序列会让角色「站-坐-站-坐」上下跳 15px，视角与遮挡同时失效。
   */
  working: [8, 9],
  talking: [14, 15],
  alert: [18, 19],
  sitdown: [20, 21, 22, 23],
  sitting: [24, 25],
  sitdown_b: [26, 27, 28, 29],
  sitting_b: [30, 31],
  getup: [23, 22, 21, 20],
};

/** 各动作帧率（fps）：行走快、呼吸/坐姿慢循环 */
export const DEV_ANIM_FPS: Record<AgentAnimKey, number> = {
  idle: 3,
  walking: 10,
  working: 7,
  talking: 4,
  alert: 3,
  sitdown: 7,
  sitting: 2,
  sitdown_b: 7,
  sitting_b: 2,
  getup: 7,
};

/** 一次性播放（播完停在末帧，不回卷）；其余为循环帧 */
export const DEV_ANIM_ONESHOT: AgentAnimKey[] = ["sitdown", "sitdown_b", "getup"];

/** 程序动画参数（无内置帧 → 靠 Sprite scale/rotation/skew 摆动模拟） */
export const AGENT_PROC_ANIM = {
  /** 呼吸：整体小幅上下波动 */
  idle: { bobHz: 2, bobAmp: 1.0, leanAmp: 0 },
  /** 打字：上半身快速颤动 */
  working: { bobHz: 8, bobAmp: 0.6, leanAmp: 0.02 },
  /** 行走：整体更大幅度的摇摆 */
  walking: { bobHz: 8, bobAmp: 2.0, leanAmp: 0.06 },
} as const;

export type AgentAnimKind = keyof typeof AGENT_PROC_ANIM;

/** role → spritesheet URL（与 resolveDeskRole 三池对齐） */
export function roleSheetUrl(role: string): string {
  const kind = resolveDeskRole(role);
  if (kind === "lead") return ASSET_URLS.AGENT_MANAGER;
  if (kind === "review") return ASSET_URLS.AGENT_QA;
  return ASSET_URLS.AGENT_DEV;
}

// ── Asset Inventory ───────────────────────────────────────────────

/**
 * Procedural asset IDs — every visible element in the scene.
 * In the future each ID maps to a SpriteFrame in a spritesheet manifest.
 */
export const ASSET_IDS = {
  // Environment
  FLOOR_BG:       "floor_bg",
  BACK_WALL:      "back_wall",
  SIDE_WALL:      "side_wall",
  WINDOW:         "window",
  WINDOW_SIDE:    "window_side",
  ISO_TILE_LIGHT: "iso_tile_light",
  ISO_TILE_DARK:  "iso_tile_dark",

  // Furniture
  DESK:         "desk",
  WHITEBOARD:   "whiteboard",
  PLANT:        "plant",
  VENDING:      "vending",
  SOFA:         "sofa",
  MEETING_TABLE:"meeting_table",

  // HUD
  HUD_BAR:      "hud_bar",
  HUD_TITLE:    "hud_title",

  // Agent (procedural body parts)
  AGENT_BODY:   "agent_body",
  AGENT_FACE:   "agent_face",
  AGENT_BUBBLE: "agent_bubble",
} as const;

export type AssetId = (typeof ASSET_IDS)[keyof typeof ASSET_IDS];

// ── Max Visible Agents ────────────────────────────────────────────

export const MAX_VISIBLE_AGENTS = DESKS.length; // 7（6 张白桌 + 前台柜台）
