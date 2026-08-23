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
  /** 整间办公室背景（apimart gpt-image-2-official 4K 图生图 + mask 局部重绘：
   *  原图 1:1 复刻 → 去人物 → 前台移至正门 → 远侧椅后移留座。3840×2160（16:9），
   *  场景内缩到 WORLD_W×WORLD_H = 1280×720。 */
  OFFICE_BG: "/office-assets/office-scene-bg.png",
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
  [ASSET_URLS.AGENT_DEV]: { cols: 8, rows: 4, frameW: 64, frameH: 96, scale: 0.8, scaleMode: "linear" },
  [ASSET_URLS.AGENT_MANAGER]: { cols: 4, rows: 3, frameW: 32, frameH: 48, scale: 1.6, scaleMode: "nearest" },
  [ASSET_URLS.AGENT_QA]: { cols: 4, rows: 3, frameW: 32, frameH: 48, scale: 1.6, scaleMode: "nearest" },
};

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
  working: [6, 7, 8, 9],
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

/** 到桌落座的视觉偏移（world px，2K 切片与背景椅对位实测）：
 * A = 桌后左侧椅（面朝右下 45°）；B = 桌前右侧椅（面朝左上 45°，= A + 椅位差(85,38)） */
export const SIT_OFFSET_A = { x: 35, y: 35 } as const;
export const SIT_OFFSET_B = { x: 120, y: 73 } as const;

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
