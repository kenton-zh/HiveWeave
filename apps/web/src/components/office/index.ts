/**
 * office/ — PixiJS Isometric Office Scene
 * ========================================
 *
 * Architecture:
 *   OfficeView (React host)  ─── SceneSnapshot ───►  OfficeScene (PixiJS)
 *   OfficeScene (PixiJS)     ─── OfficeInteraction ►  OfficeView (React)
 *
 * Each OfficeActor owns an AgentStateMachine that governs its
 * visual state transitions.
 */

export { OfficeScene } from "./OfficeScene";
export { OfficeActor } from "./OfficeActor";
export { AgentStateMachine, isRoamingFrame, isChatteringFrame } from "./state-machine";
export type {
  OfficeAgent,
  DeskSlot,
  AgentVisualState,
  AgentStateSnapshot,
  SceneSnapshot,
  AssetManifest,
  OfficeInteraction,
  OfficeInteractionHandler,
  ProcAsset,
  SpriteFrame,
  SpriteManifest,
  SceneLayer,
  DeskRole,
} from "./types";
export {
  SCENE_LAYERS,
} from "./types";
export {
  WORLD_W,
  WORLD_H,
  TILE_W,
  TILE_H,
  DESKS,
  ROLE_COLORS,
  DEFAULT_ROLE_COLOR,
  MAX_VISIBLE_AGENTS,
  getDesk,
  resolveDeskRole,
  assignDeskIndex,
  isoToScreen,
  ASSET_IDS,
} from "./constants";
