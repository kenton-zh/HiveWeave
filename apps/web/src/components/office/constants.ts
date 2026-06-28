/**
 * Office Scene — Constants
 * World dimensions, desk layout, role colours, asset descriptors.
 */

import type { DeskSlot } from "./types";

// ── World ─────────────────────────────────────────────────────────

export const WORLD_W = 1280;
export const WORLD_H = 760;
export const TILE_W = 64;
export const TILE_H = 32;

// ── Desk Layout ───────────────────────────────────────────────────

/**
 * 12 desk slots grouped by role.  The layout maps to an isometric
 * office floor plan visible in the rendered scene.
 *
 *   lead   — CEO / Architect / Manager (right side, near whiteboard)
 *   build  — Developers (centre-left)
 *   review — QA / Reviewer / Auditor (bottom-right)
 */
export const DESKS: DeskSlot[] = [
  { id: "lead-1",   x: 820,  y: 178, role: "lead" },
  { id: "lead-2",   x: 910,  y: 294, role: "lead" },
  { id: "build-1",  x: 292,  y: 278, role: "build" },
  { id: "build-2",  x: 430,  y: 278, role: "build" },
  { id: "build-3",  x: 568,  y: 278, role: "build" },
  { id: "build-4",  x: 444,  y: 456, role: "build" },
  { id: "build-5",  x: 582,  y: 456, role: "build" },
  { id: "build-6",  x: 720,  y: 456, role: "build" },
  { id: "review-1", x: 864,  y: 452, role: "review" },
  { id: "review-2", x: 1010, y: 452, role: "review" },
  { id: "build-7",  x: 266,  y: 478, role: "build" },
  { id: "review-3", x: 1040, y: 284, role: "review" },
];

// ── Common Area (talking / roaming targets) ───────────────────────

/** Gather point when agents are "talking" */
export const COMMON_TARGETS = [
  { x: 620, y: 338 },
  { x: 662, y: 338 },
  { x: 704, y: 364 },
  { x: 746, y: 364 },
];

/** Roaming waypoints (random walk between desk and common area) */
export const ROAM_WAYPOINTS = [
  { x: 404, y: 350 },
  { x: 492, y: 388 },
  { x: 580, y: 350 },
  { x: 668, y: 388 },
  { x: 756, y: 350 },
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

export function assignDeskIndex(agentIndex: number, role: string): number {
  const pool = DESKS.filter((d) => d.role === resolveDeskRole(role));
  return agentIndex % Math.max(pool.length, 1);
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

export const MAX_VISIBLE_AGENTS = DESKS.length; // 12
