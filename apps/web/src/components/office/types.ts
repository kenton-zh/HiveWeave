/**
 * Office Scene — Architecture Boundary
 * =====================================
 * This module defines the interfaces that separate the Office scene into
 * three distinct layers:
 *
 *   Layer 1 — React Host (OfficeView.tsx)
 *     Reads Zustand store, feeds `SceneSnapshot` into the PixiJS host.
 *     Knows NOTHING about rendering.
 *
 *   Layer 2 — PixiJS Scene (OfficeScene.ts)
 *     Owns the PIXI.Application, scene graph, and render loop.
 *     Receives `SceneSnapshot` → synchronises actors & environment.
 *     Knows NOTHING about React or Zustand.
 *
 *   Layer 3 — Agent Actor + State Machine (OfficeActor.ts, state-machine.ts)
 *     Each agent character owns its FSM. The scene calls `setTargetState()`
 *     and `update(delta)` — the actor handles visual interpolation & animation.
 *
 * ── Asset Manifest ─────────────────────────────────────────────────
 *
 * All visual assets are enumerated here. Currently rendered procedurally
 * via PIXI.Graphics primitives, but the manifest defines the target
 * sprite/texture API so that a future spritesheet pipeline can drop in.
 */

import type * as PIXI from "pixi.js";

// ── Agent Identity (mirrors backend agent shape) ──────────────────

export interface OfficeAgent {
  id: string;
  name: string;
  role: string;
  status: string;
  children?: OfficeAgent[];
}

// ── Agent Visual State Machine ────────────────────────────────────

export type AgentVisualState =
  | "idle"         // Standing at desk, subtle idle animation
  | "working"      // Working at desk (processing=true), bobbing
  | "walking"      // Moving between desk ↔ common area
  | "talking"      // Gathered in common area, speech bubble visible
  | "alert";       // User ping notification, bubble + pulse

export interface AgentStateSnapshot {
  visual: AgentVisualState;
  targetX: number;
  targetY: number;
  selected: boolean;
  showBubble: boolean;
  roleColor: number;
}

// ── Desk Assignment ───────────────────────────────────────────────

export type DeskRole = "lead" | "build" | "review";

export interface DeskSlot {
  id: string;
  x: number;
  y: number;
  role: DeskRole;
}

// ── Scene Layer Order (bottom → top) ──────────────────────────────

export const SCENE_LAYERS = [
  "floor",       // Tiles, walls, background
  "furniture",   // Desks, plants, vending, sofa, whiteboard
  "actors",      // Agent characters (sorted by y for depth)
  "ui",          // HUD overlay
] as const;

export type SceneLayer = (typeof SCENE_LAYERS)[number];

// ── Asset Manifest ────────────────────────────────────────────────

/** Procedural asset descriptor — describes a shape to be drawn with Graphics. */
export interface ProcAsset {
  id: string;
  layer: SceneLayer;
  /** Draw this asset onto the given Graphics object at (0,0). Caller handles positioning & zIndex. */
  draw: (g: PIXI.Graphics) => void;
  /** Optional: anchor offset so the asset positions correctly relative to its (x,y). */
  anchorX?: number;
  anchorY?: number;
}

/** Top-level manifest enumerating every static asset in the scene. */
export interface AssetManifest {
  version: 1;
  desks: DeskSlot[];
  furniture: ProcAsset[];
  environment: ProcAsset[];
  hud: ProcAsset[];
}

// ── Scene Snapshot (React → PixiJS bridge) ────────────────────────

export interface SceneSnapshot {
  agents: OfficeAgent[];
  processingIds: Set<string>;
  communicatingIds: Set<string>;
  selectedAgentId: string | null;
  userPingIds: Set<string>;
}

// ── Interaction Events (PixiJS → React bridge) ────────────────────

export interface OfficeInteraction {
  type: "select-agent";
  agentId: string;
}

export type OfficeInteractionHandler = (event: OfficeInteraction) => void;

// ── Spritesheet Manifest (future) ─────────────────────────────────

/**
 * When the project moves to raster sprites, each entry maps a logical
 * asset id → spritesheet frame coordinates.
 */
export interface SpriteFrame {
  textureId: string;
  x: number;
  y: number;
  w: number;
  h: number;
  anchorX?: number;
  anchorY?: number;
}

export interface SpriteManifest {
  textureUrl: string;
  frames: Record<string, SpriteFrame>;
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
const _ASSET_MANIFEST_VERSION = 1 as const;
