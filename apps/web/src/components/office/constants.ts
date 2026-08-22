/**
 * Office Scene — Constants
 * World dimensions, desk layout, role colours, asset descriptors.
 */

import * as PIXI from "pixi.js";
import type { DeskSlot } from "./types";

// ── World ─────────────────────────────────────────────────────────

export const WORLD_W = 1280;
export const WORLD_H = 720;  // 16:9 对齐 3840×2160 gpt-image-2 官方 4K 背景图（缩 33.3%）
export const TILE_W = 64;
export const TILE_H = 32;

// ── Desk Layout ───────────────────────────────────────────────────

/**
 * 9 desk slots grouped by role，对应 1:1 复刻背景图（9 张白桌 2+3+4 三排）。
 *   领导席（lead）= 顶排 2 张（管理层坐在后排，靠近厨房/前台）
 *   开发（build）= 中排 3 张 + 底排前 2 张 = 5
 *   评审/QA（review）= 底排后 2 张
 *
 * 坐标基准：desk (x, y) 是「桌子上表面」在世界坐标系的中间-ish 位置。
 * OfficeScene 中 agent 容器 y = desk.y + 78，即脚底正好落在「桌前地面」。
 * 椅子 sprite y = desk.y + 44（靠背底部大约在 agent 脚底上方 34 px）。
 *
 * 校准方式：读取 3840×2160 gpt-image-2-official 4K 原图 → 用画图圈出每
 * 张桌前空椅的靠背中心 → 缩到 world 1280×720（除以 3），
 * desk.y = chair_center_y − 44，desk.x 与椅子靠背中心 x 对齐（±3px）。
 */
export const DESKS: DeskSlot[] = [
  // 顶排 2 张（后排领导桌）
  { id: "lead-1",   x: 586, y: 180, role: "lead" },   // CEO (top-left)
  { id: "lead-2",   x: 693, y: 206, role: "lead" },   // Architect / Manager (top-right)
  // 中排 3 张
  { id: "build-1",  x: 439, y: 282, role: "build" },  // middle-left (close to meeting room)
  { id: "build-2",  x: 604, y: 310, role: "build" },  // middle-center
  { id: "build-3",  x: 843, y: 299, role: "build" },  // middle-right (near right plant)
  // 底排 4 张
  { id: "build-4",  x: 504, y: 421, role: "build" },  // bottom row 1st (front-left)
  { id: "build-5",  x: 605, y: 450, role: "build" },  // bottom row 2nd
  { id: "review-1", x: 768, y: 444, role: "review" }, // bottom row 3rd
  { id: "review-2", x: 871, y: 474, role: "review" }, // bottom row 4th (front-right close to entry)
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
  { x: 470, y: 262 },  // KAIROSOFT 弧形前台 前
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
export const WALK_SPEED = 0.12;
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
  /** 整间办公室背景（AI 图生图 1:1 复刻，3840×2400 4K → 缩到 WORLD_W×WORLD_H = 1280×800，比例 16:10 完全对齐） */
  OFFICE_BG: "/office-assets/office-scene-bg.png",
} as const;

/** 全部需预载的资产 URL（OfficeScene.mount 中统一 Assets.load） */
export const ASSET_LOAD_LIST: string[] = Object.values(ASSET_URLS);

// ── Agent Spritesheet Layout ──────────────────────────────────────

/**
 * Agent sheet 为 128×144，内部 32×48 网格 4 列 × 3 行 = 12 帧。
 * ⚠️ 图像验证：12 帧**完全相同**（站立静止图的重复平铺，无动画帧）。
 *  3 张 sheet 的唯一差异是角色上衣配色：
 *    agent-dev-sheet     → 红衫（developer / executor 池）
 *    agent-manager-sheet → 蓝衫（manager / coordinator / ceo 池）
 *    agent-qa-sheet      → 黄衫（qa / reviewer / auditor 池）
 *
 * 角色内容在帧内 y≈42 处触底（脚底 y=42，头顶 y≈5），脚底 anchor = 42/48 ≈ 0.875。
 * 由于 sheet 本身无动画帧，行走/呼吸/打字通过**程序缩放摆动**叠加在 sprite 上实现。
 *
 * 参考 walk.png (3840×1632 / 6 帧循环 = 640×1632 每帧) 为另一套横版像素小人，
 * 比例 / 视角 / 风格与当前 isometric 办公室不一致，暂不接入本场景。
 */
export const SHEET_FRAME_W = 32;
export const SHEET_FRAME_H = 48;
export const SHEET_FOOT_ANCHOR_Y = 42 / 48;

/** 只用 sheet 左上角第 0 帧（其他 11 帧完全相同） */
export const SHEET_STAND_FRAME = { x: 0, y: 0 } as const;

/** 模块级 Rectangle 实例（复用以避免 new Rectangle 分配 / GC 压力）。
 * 注意：切 frame 必须同步传 orig = 同尺寸 Rectangle，否则 PixiJS v8 下
 * texture.width / getBounds / pointertap 命中检测会按整图 128×144 计算。 */
export const SHEET_STAND_FRAME_RECT = new PIXI.Rectangle(
  SHEET_STAND_FRAME.x,
  SHEET_STAND_FRAME.y,
  SHEET_FRAME_W,
  SHEET_FRAME_H,
);
export const SHEET_STAND_ORIG_RECT = new PIXI.Rectangle(
  0,
  0,
  SHEET_FRAME_W,
  SHEET_FRAME_H,
);

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

export const MAX_VISIBLE_AGENTS = DESKS.length; // 9（与背景图 9 张白桌对齐）
