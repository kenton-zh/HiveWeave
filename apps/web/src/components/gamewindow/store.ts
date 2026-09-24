/**
 * Game Window Layer — 窗口状态机
 *
 * 依据：`docs/前端设计规格.md` §2.3 信息架构（ADO-011 §D2）
 *   窗口层（React DOM 浮窗）→ 交互桥 → 底层（PixiJS OfficeScene 全屏）
 *
 * 边界（§2.1 分层原则）：本 store **只管窗口层自身的视觉状态** ——
 * 开 / 关 / 聚焦 / 几何 / 最小化 / 最大化。它**不持有任何业务状态**：
 * agent、任务、项目等业务事实仍然只存在于主 store 与 WS 事件流里。
 *
 * z 序**不由本 store 管理** —— 交给 WinBox 自身的点击置顶行为，本 store 只保留
 * 一个"请求聚焦"的信号（同一窗口重复打开时置顶，而不是开第二个）。
 */
import { create } from "zustand";

// ── 类型 ──────────────────────────────────────────────────────────

export type GameWindowKind =
  | "chat" // 单个 agent 的对话
  | "org" // 组织树
  | "timeline" // 团队泳道时间线
  | "token" // token 用量
  | "goals" // 目标工作簿
  | "agent" // agent 详情
  | "logs" // 工作日志
  | "monitor" // 监控
  | "debug" // 调试
  | "task"; // 任务全链路回放

export interface GameWindowPayload {
  agentId?: string;
  taskId?: string;
  projectId?: string;
}

export interface GameWindowGeometry {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface GameWindowState {
  id: string;
  kind: GameWindowKind;
  title: string;
  payload: GameWindowPayload;
  geom: GameWindowGeometry;
}

// ── 常量 ──────────────────────────────────────────────────────────

/** 各类窗口的默认尺寸（px）。刻意给不同初值，避免全部叠成同一块 */
const DEFAULT_SIZE: Record<GameWindowKind, { w: number; h: number }> = {
  chat: { w: 560, h: 640 },
  org: { w: 760, h: 620 },
  timeline: { w: 980, h: 640 },
  token: { w: 760, h: 580 },
  goals: { w: 660, h: 600 },
  agent: { w: 560, h: 620 },
  logs: { w: 660, h: 600 },
  monitor: { w: 620, h: 620 },
  debug: { w: 720, h: 560 },
  task: { w: 840, h: 640 },
};

/** 级联步长：新窗口逐个右下偏移，避免完全重叠 */
const CASCADE_STEP = 28;
/** 级联回绕阈值：偏移超过这么多格后回到起始位 */
const CASCADE_WRAP = 6;
/** 距屏幕边缘的安全边距 */
const MARGIN = 12;
/** 顶部占用估算 = App header（h-14 = 56px）+ 办公室 HUD 条（~38px）。
 *  窗口初始落点据此下移，避免一开出来就被压住（用户拖过一次后即走持久化几何）。 */
const TOP_INSET = 94;

const GEOM_KEY = "hw-gamewin-geom";

// ── 几何持久化（容错：localStorage 不可用时静默降级为纯内存）────────

function readGeomCache(): Record<string, GameWindowGeometry> {
  try {
    const raw = localStorage.getItem(GEOM_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, GameWindowGeometry>) : {};
  } catch {
    return {};
  }
}

let geomWriteTimer: ReturnType<typeof setTimeout> | null = null;

function writeGeomCache(id: string, geom: GameWindowGeometry) {
  if (geomWriteTimer) clearTimeout(geomWriteTimer);
  // 去抖 300ms：拖拽过程中 onmove 会高频触发，不能每次都序列化整表
  geomWriteTimer = setTimeout(() => {
    try {
      const cache = readGeomCache();
      cache[id] = geom;
      localStorage.setItem(GEOM_KEY, JSON.stringify(cache));
    } catch {
      /* 忽略：隐私模式 / 配额超限 */
    }
  }, 300);
}

// ── 工具 ──────────────────────────────────────────────────────────

/**
 * 窗口 id。
 *
 * **单例策略（2026-09-23 用户钦定「像微信一样」）**：每个 kind 全局只存在一个窗口，
 * id 恒为 `kind` —— 点不同的人**不再各开一窗**，而是在同一个窗口里**切换内容**
 * （`open()` 覆盖 payload ⇒ `registry` 用新的 `agentId` 重渲染面板；`ChatPanel` 的
 * 加载 effect 依赖 `agentId`，会自行重载该会话）。
 *
 * 此前按 payload 分窗（`chat:agent-42`）⇒ 连点 3 个人就叠出 3 个「聊天」窗（用户实拍为证）。
 * 副作用（正向）：几何/置顶信号都以 kind 为键 ⇒ 同一个面板的位置稳定，不再级联错位。
 */
export function gameWindowId(kind: GameWindowKind): string {
  return kind;
}

/** 计算新窗口的落点：优先用历史几何，否则按当前窗口数级联偏移 */
function nextGeometry(kind: GameWindowKind, id: string, cascadeIndex: number): GameWindowGeometry {
  const { w, h } = DEFAULT_SIZE[kind];
  const cached = readGeomCache()[id];
  if (cached && typeof cached.x === "number" && typeof cached.w === "number") {
    return { ...cached };
  }
  const step = (cascadeIndex % CASCADE_WRAP) * CASCADE_STEP;
  const vw = typeof window === "undefined" ? 1440 : window.innerWidth;
  const vh = typeof window === "undefined" ? 900 : window.innerHeight;
  return {
    x: Math.max(MARGIN, Math.round(vw * 0.5 - w * 0.5) + step),
    y: Math.max(TOP_INSET, Math.round(TOP_INSET + (vh - TOP_INSET - h) / 2) + step),
    w,
    h,
  };
}

// ── Store ─────────────────────────────────────────────────────────

interface GameWindowStore {
  windows: GameWindowState[];
  /** id → nonce：递增即请求该窗口置顶（重复 open 时用，不新开窗口） */
  focusSignal: Record<string, number>;

  open: (kind: GameWindowKind, payload?: GameWindowPayload, title?: string) => void;
  close: (id: string) => void;
  closeAll: () => void;
  /** 关闭某一类窗口的全部实例（如切项目时关掉所有 agent 窗） */
  closeKind: (kind: GameWindowKind) => void;
  /** 请求置顶（最小化时同时恢复）；窗口不存在则忽略 */
  requestFocus: (id: string) => void;
  setGeometry: (id: string, patch: Partial<GameWindowGeometry>) => void;
  setTitle: (id: string, title: string) => void;
  /** 已打开则返回 id（供调用方判断），供 UI 反馈用 */
  isOpen: (id: string) => boolean;
}

export const useGameWindowStore = create<GameWindowStore>((set, get) => ({
  windows: [],
  focusSignal: {},

  open: (kind, payload = {}, title) => {
    const id = gameWindowId(kind);
    const existing = get().windows.find((w) => w.id === id);

    if (existing) {
      // 已存在 ⇒ 不新开，而是**切换内容**（微信式：同一个面板换会话）：
      // ① 覆盖 payload（面板据此重渲染，如 chat 换 agent）
      // ② 标题只在调用方显式传入且变化时更新（避免调用方不传 title 时把标题清掉）
      // ③ 发聚焦信号：GameWindow 收到后置顶 + 从最小化恢复
      const payloadChanged =
        existing.payload.agentId !== payload.agentId || existing.payload.taskId !== payload.taskId;
      const titleChanged = title !== undefined && title !== existing.title;
      if (payloadChanged || titleChanged) {
        set((s) => ({
          windows: s.windows.map((w) =>
            w.id === id ? { ...w, payload, ...(title !== undefined ? { title } : {}) } : w,
          ),
        }));
      }
      set((s) => ({ focusSignal: { ...s.focusSignal, [id]: (s.focusSignal[id] ?? 0) + 1 } }));
      return;
    }

    set((s) => ({
      windows: [
        ...s.windows,
        {
          id,
          kind,
          title: title ?? defaultTitle(kind),
          payload,
          geom: nextGeometry(kind, id, s.windows.length),
        },
      ],
      focusSignal: { ...s.focusSignal, [id]: (s.focusSignal[id] ?? 0) + 1 },
    }));
  },

  close: (id) =>
    set((s) => {
      const { [id]: _drop, ...rest } = s.focusSignal;
      return { windows: s.windows.filter((w) => w.id !== id), focusSignal: rest };
    }),

  closeAll: () => set({ windows: [], focusSignal: {} }),

  closeKind: (kind) =>
    set((s) => {
      const keep = s.windows.filter((w) => w.kind !== kind);
      const removed = new Set(s.windows.filter((w) => w.kind === kind).map((w) => w.id));
      const focusSignal: Record<string, number> = {};
      for (const [k, v] of Object.entries(s.focusSignal)) {
        if (!removed.has(k)) focusSignal[k] = v;
      }
      return { windows: keep, focusSignal };
    }),

  requestFocus: (id) =>
    set((s) =>
      s.windows.some((w) => w.id === id)
        ? { focusSignal: { ...s.focusSignal, [id]: (s.focusSignal[id] ?? 0) + 1 } }
        : {},
    ),

  setGeometry: (id, patch) => {
    const cur = get().windows.find((w) => w.id === id);
    if (!cur) return;
    const geom = { ...cur.geom, ...patch };
    if (geom.x === cur.geom.x && geom.y === cur.geom.y && geom.w === cur.geom.w && geom.h === cur.geom.h) {
      return; // 值未变则不写（WinBox 的 onmove 会重复回调同值）
    }
    set((s) => ({ windows: s.windows.map((w) => (w.id === id ? { ...w, geom } : w)) }));
    writeGeomCache(id, geom);
  },

  setTitle: (id, title) =>
    set((s) => ({ windows: s.windows.map((w) => (w.id === id ? { ...w, title } : w)) })),

  isOpen: (id) => get().windows.some((w) => w.id === id),
}));

/** 默认标题（payload 相关标题由调用方传入，如「聊天 · 折纸」） */
function defaultTitle(kind: GameWindowKind): string {
  const table: Record<GameWindowKind, string> = {
    chat: "聊天",
    org: "组织树",
    timeline: "时间线",
    token: "Token 用量",
    goals: "目标",
    agent: "Agent 详情",
    logs: "工作日志",
    monitor: "监控",
    debug: "调试",
    task: "任务链路",
  };
  return table[kind];
}
