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
 * 桌位 = **规则网格**（3 列 × 2 排），按背景图 office-scene-bg.png（KAIROSOFT 平面）标定。
 * 该底图在场景里被拉伸到 WORLD_W×WORLD_H = 1280×720 ⇒ **world 与底图像素 1:1**。
 * 坐标 = 该桌「落座基准点」，桌套件/椅位由 DESK_SET 的相对偏移从此点推出；
 * 顺序即 getDesk() / purpleDemoSeat() 的分配顺序。
 *
 * ── 2026-09-23 排成整齐网格（用户要求「桌椅摆整齐」）──
 * 由 **`apps/web/scripts/calibrate-desk-grid.mjs`** 程序化求解（该脚本同时是改底图后的标定工具）：
 * 枚举 (列距, 行距, 起点)，要求**每个格位**的桌套件包围盒（x∈[-78,+81] / y∈[-62,+67]）
 * 整块落在木地板空地上（基色 RGB(242,170,96) ±26），取「间距尽可能大 + 整体居中」的最优解：
 * **dx=166 / dy=132 / 起点 (535,360)**。`DIAG=1` 可打印逐行可用空地范围。
 *
 * ⚠ **整齐 ⇒ 上限 6 席，这是底图形状的硬约束，不是没搜够**：
 * 空地是「上宽下窄」的楔形（诊断见 `_desk_grid.mjs` 的 `DIAG=1`）——
 * y≈430 那一行最宽（x 460..1110，宽 651，恰好只够 3 列）；
 * y≥510 起右半侧就只剩 x 815..1050（宽 ~240 ⇒ 只放得下 1 席）。
 * 4 列 × 2 排 / 2 列 × 4 排 / 3 列 × 3 排 全部**零可行解**（都枚举过）。
 * ⇒ 要 8 席就必须放弃规则网格（第 5 席起只能塞进右侧那块窄空地），要 10 席（§5.3）
 *   只能换/扩底图。
 * ⚠ 间距下限：同排 x 差 ≥ 162（桌宽 159）、上下 y 差 ≥ 130（套件高 129），再小就穿插。
 * 当前 dx=166 / dy=132 已贴近下限 ⇒ 两排之间只有 3 world px 缝，观感偏紧凑。
 *
 * ⚠ 命名禁忌：任何工位**绝不能用 `front-desk`**（`FRONDESK_SLOT.id`），
 * 否则 `furnitureSet()` 会把那席位渲染成前台柜台套件。
 * （历史：旧判据是 `id === "review-2"` 死代码，已于 2026-09-23 修正为认 `FRONDESK_SLOT.id`。）
 */
export const DESKS: DeskSlot[] = [
  // 上排（y=360）
  { id: "lead-1",   x: 535, y: 360, role: "lead" },   // 左
  { id: "build-1",  x: 701, y: 360, role: "build" },  // 中
  { id: "build-2",  x: 867, y: 360, role: "build" },  // 右
  // 下排（y=492）
  { id: "build-3",  x: 535, y: 492, role: "build" },  // 左
  { id: "review-1", x: 701, y: 492, role: "review" }, // 中
  { id: "review-3", x: 867, y: 492, role: "review" }, // 右
];

/**
 * 前台接待位（2026-09-23 新增，用户钦定「前台桌子提取出来放到前台」）。
 *
 * **独立于 DESKS 网格**：它不参与 `getDesk()` / `purpleDemoSeat()` 的轮转分配，
 * 只在 `OfficeScene._drawFurnitureSprites` 里单独渲染柜台 sprite，
 * 并由 `frontDeskSeat()` 作为 **HR 专用固定席位**。
 *
 * ⚠ 它**绝不能**进 DESKS：DESKS 是「规则工位网格」，进网格会被座位分配器
 * 当成普通工位发出去；且 MAX_VISIBLE_AGENTS 绑 DESKS.length，混进去会虚增上限。
 * （历史坑：旧代码用 `DESKS.find(id==="review-2")` 找前台，而 DESKS 里从未有过
 * 该 id ⇒ 静默 fallback 到 review-3，前台柜台从未渲染过。本常量正是修掉这一条。）
 *
 * 坐标：柜台正面中心在地面的投影点（world；底图 1280×720 与底图像素 1:1）。
 * 标定自 `323ef556-…png`（KAIROSOFT 已布置版，前台无人）：
 * 原图裁框 (608,648)-(892,806) × 0.76555 = 显示 217.4×121.0，
 * sprite 左上角 (465.5,496.1)，柜台正面中心底沿 (573,605)。
 */
export const FRONDESK_SLOT: DeskSlot = { id: "front-desk", x: 573, y: 605, role: "review" };

// ── Common Area (talking / roaming targets) ───────────────────────

/** Gather point when agents are "talking" — 工位下方空地（KAIROSOFT 平面无前台，
 *  改用空地中央；roaming/talking 当前未接线，仅作状态机目标点保留） */
export const COMMON_TARGETS = [
  { x: 700, y: 560 },
  { x: 740, y: 570 },
  { x: 780, y: 560 },
  { x: 820, y: 570 },
];

/** Roaming waypoints（办公室 5 个地标之间散步），按 KAIROSOFT 平面重标：
 *  左上会议室外 / 左下蓝沙发+豆袋 / 中上双沙发会客区 / 右上厨房吧台 / 右侧灰沙发电视区 */
export const ROAM_WAYPOINTS = [
  { x: 174, y: 300 },   // 左上 玻璃会议室外
  { x: 196, y: 545 },   // 左下 蓝沙发 + 黄豆袋休息区
  { x: 406, y: 140 },   // 中上 双蓝沙发 + 茶几
  { x: 696, y: 120 },   // 右上 厨房吧台 高脚凳前
  { x: 1020, y: 280 },  // 右侧 灰沙发 + 电视 + 会客区
];

// ── Role Colours ──────────────────────────────────────────────────

export const ROLE_COLORS: Record<string, number> = {
  ceo:              0xf59e0b, // amber
  architect:        0xa855f7, // purple
  manager:          0x3b82f6, // blue
  hr:               0xf43f5e, // rose
  qa:               0xeab308, // yellow
  qa_lead:          0xeab308, // yellow（2026-09-23 补：实测 TEST_DSH_47 有 qa_lead，
  //                             原本落兜底 slate ⇒ 名牌职位行与 qa/test_engineer 不同色）
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
 *
 * ⚠ 2026-09-23：`reception` 的取法已修 —— 只认 `FRONDESK_SLOT`，
 * **不再** `DESKS.find(id==="review-2")`（该 id 从不在 DESKS 里 ⇒ 旧代码恒 fallback）。
 * 但注意本章「前台 = 0 号演示位」的旧语义已被 `frontDeskSeat()` 取代：
 * 前台现在**专属于 HR**，不再由 0 号占用。本函数只服务演示路径，保留兼容。
 */
export function purpleDemoSeat(index: number): { desk: DeskSlot; variant: "A" | "B" } {
  if (index === 0) return { desk: FRONDESK_SLOT, variant: "A" };
  return {
    desk: DESKS[(index - 1) % Math.max(DESKS.length, 1)],
    variant: "B",
  };
}

// ── Front Desk (HR 专属固定席位) ──────────────────────────────────

/** HR 的角色判据（大小写不敏感的全词匹配，避免 "hr" 命中 "shrimp" 之类子串） */
const HR_ROLE_RE = /^(hr|human_?resources?|人事|人力资源)$/i;

export function isHrRole(role: string | null | undefined): boolean {
  return !!role && HR_ROLE_RE.test(role.trim());
}

/**
 * HR 是否坐前台。
 *
 * 用户 2026-09-23 钦定：**硬绑 —— HR 永远坐前台，每个项目都是**。
 *
 * 但纯硬绑有一个**静默失败场景**：项目没招 HR / HR 被 dismiss 时，
 * 前台会空着且无任何报错。故本函数带 `allowFallback`：
 * 无 HR 时由 `fallbackAgent`（调用方传 0 号角色）补位，保证柜台不空。
 * ⚠ fallback 只在**确实没有 HR** 时启用；一旦存在 HR，HR 一定在前台。
 */
export function isFrontDeskAgent(
  agent: { role?: string | null },
  roster: readonly { role?: string | null }[],
): boolean {
  const hasHr = roster.some((a) => isHrRole(a.role));
  if (hasHr) return isHrRole(agent.role);
  return false;   // 无 HR ⇒ 无人坐前台（由调用方决定是否启用 0 号兜底）
}

/** 前台落座位（含 sheet 锚点语义校正，与 seatPosFor 同口径） */
export function frontDeskSeat(sheetUrl: string) {
  return seatPosFor(sheetUrl, FRONDESK_SLOT, "A");
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
 * 生成规格见 docs/前端设计规格.md §11；等距风格与场景一致。
 */
export const ASSET_URLS = {
  AGENT_DEV: "/office-assets/agent-dev-sheet.png",
  // ⚠ 全部旧**像素风**素材已于 2026-09-22 清除（用户钦定「永远放弃这个方向」）：
  //   AGENT_MANAGER / AGENT_QA / FLOOR_TILE / DESK / CHAIR / PLANT /
  //   SPEECH_BUBBLE / WALL_WINDOW / WHITEBOARD —— 九件，外加 assets/mvp 整条像素管线
  //   （124 文件 / 5.5 MB）。
  //   它们服务的绘制路径（程序化房间 + 像素家具，含 _drawRoom / _drawFurniture /
  //   _drawDesk / _drawPlant … 共 12 个方法、542 行）在 bg 模式下**永不可达**，已一并删除。
  //   **方向不再恢复**；若需新的场景配件，按 §11.1 的等距高清路线重做（§17-T15）。
  /** 整间办公室背景。2026-09-21 换为 KAIROSOFT 平面（1104×608 源 → 写入 1280×720 =
   *  WORLD_W×WORLD_H，world 与底图像素 1:1）。旧底图（1672×941）备份为
   *  office-scene-bg.pre-kairo.bak.png。陈设不与角色交互，桌椅全部由引擎 sprite 渲染。 */
  OFFICE_BG: "/office-assets/office-scene-bg.png?v=kairo",
  /** 分层家具 sprite。BACK = 后椅+远侧显示器+隔板；FRONT = 近侧桌面/前椅（挡腿）。
   *  2026-09-21 随底图重切（取自真值工位整件、无旧底图烤入阴影），query 递增强刷。 */
  OFFICE_DESK_BACK: "/office-assets/office-desk-back.png?v=kairo",
  OFFICE_DESK_FRONT: "/office-assets/office-desk-front.png?v=kairo",
  OFFICE_FRONTDESK_SET: "/office-assets/office-frontdesk-set.png",
  /** 前台柜台的**分层**两片（2026-09-23）：back = 内嵌黑椅 + 双显示器 + 白台面（角色之下）；
   *  front = 木柜面前脸 + 蓝牌 + 接地阴影（角色之上，挡腿）。切割脚本见
   *  `tasks/frontdesk-extract/split_frontdesk_layers.py`（两片 alpha 之和 == 原图）。
   *  ⚠ `OFFICE_FRONTDESK_SET` 单片版本**仍保留**作回退/对照，但运行时不再使用。 */
  OFFICE_FRONTDESK_BACK: "/office-assets/office-frontdesk-back.png",
  OFFICE_FRONTDESK_FRONT: "/office-assets/office-frontdesk-front.png",
  /**
   * **第三片**：柜台内嵌的黑椅（2026-09-23 从 `front` 再切出来，`tasks/frontdesk-extract/split_chair_piece.py`）。
   *
   * ── 为什么必须独立成片（几何证明，见 FRONTDESK_SET.rearChair 长注释）──
   * 椅子原本在 `front` 片里，与木柜面前脸**同一层**（base）。而木柜面上沿在 world 548.38，
   * 角色鞋底在 558.0 —— 椅子与柜面共层 ⇒ 谁在前只能二选一：
   *   椅子在前 → 柜面被椅子压出缺口（错）；柜面在前 → 椅子被柜面吃掉（错）。
   * 实测椅背顶 world 511.62 比 HR 头顶（490.8）**高 20.8**，
   * 即「椅子必须挡住腰腿」与「椅背不能切头」在共层下**无解**。
   * 故拆第三层：`OFFICE_FRONTDESK_CHAIR`（z = base+2，压角色也压柜面），
   * 而 `FRONT` 片降为纯木柜面+蓝牌（不含椅）。
   * `OFFICE_FRONTDESK_FRONT_NOFURNITURE` 是拆分产物（= 原 FRONT 去掉椅），
   * 与 `CHAIR` 的 alpha 之和 == 原 `FRONT`（脚本自检）。
   */
  OFFICE_FRONTDESK_CHAIR: "/office-assets/office-frontdesk-front-chair.png",
  /** 前台招牌「净牌」——柜台蓝牌已抹去原 KAIROSOFT 文字，供运行时按项目名
   *  逐列弯曲绘制（弧面参数见 FRONT_SIGN）。2026-09-23 从同一素材派生。 */
  OFFICE_FRONTDESK_SIGN: "/office-assets/office-frontdesk-sign-clean.png",
  /** 拆分中间产物：原 `front` 片去掉内嵌黑椅（木柜面前脸 + 蓝牌 + 接地阴影）。
   *  ⚠ 运行时**不渲染**——保留是为了让 `split_chair_piece.py` 的「两片 alpha 之和
   *  == 原 front」自检可复跑，以及将来要回退到双片结构时有对照件。
   *
   *  ⚠⚠ 但它**仍在 `ASSET_LOAD_LIST` 里**（该表 = `Object.values(ASSET_URLS)`，
   *  在 `OfficeScene.mount` 统一预载）—— 而 `apps/web/vite.config.ts` 已把
   *  `*-nofurniture.png` 列入 `ARTIFACT_PATTERNS` ⇒ **生产构建下这个 URL 会被
   *  预载并 404**（dev 下 200）。功能上确认无害：预载走
   *  `Assets.load(...).catch(() => null)`，且 `_drawFurnitureSprites` 的 pieces
   *  数组从不含它。但这是 dev/prod 的**行为分叉**，别在它上面叠新逻辑。
   *  收口二选一（**未做**，2026-09-24 审计记）：① 从 `ASSET_URLS` 摘出，路径写进
   *  本注释、文件搬去 `assets/art/office/working/`；② 反过来从 `ARTIFACT_PATTERNS`
   *  撤掉这条、承认它随包发（54 KB）。**别只做一半。** */
  OFFICE_FRONTDESK_FRONT_NOFURNITURE:
    "/office-assets/office-frontdesk-front-nofurniture.png",
  /** 紫衣女孩动画表 v2（2026-09-08：MiniMax H3 本地视频生成抽帧，4×2 = 8 帧全
   *  身坐姿 96×96：帧 0-3 打字循环、帧 4-7 坐姿呼吸。侧视朝右（与 v1/dev 同向，
   *  B 位引擎翻转后朝左对桌；生成帧原始朝左，已整体镜像）。内容底 y=84 =
   *  anchor 0.875 鞋底；打字/坐姿两组共用同一联合 bbox 摆放，切换动作不缩放
   *  不跳动。腿部仍由桌套件 front 片运行时遮挡。）
   *  v1（4 帧打字）备份于 agent-purple-typing-sheet.png。 */
  AGENT_PURPLE: "/office-assets/agent-purple-anim-sheet.png?v=h3v2",
  /** HR 前台专用动画表（2026-09-23，MiniMax H3 本地生成，正面朝镜头）。
   *  4×2 = 8 帧 96×96：**帧 0-3 = 打字（上带）**、**帧 4-7 = 坐姿呼吸（下带）**。
   *
   *  ── 为什么不复用紫衣 sheet（用户钦定「这个女生的动画只做和前台匹配的」）──
   *  紫衣 v2 是**侧视朝右**（B 位工位对桌用），前台 HR 需要**正面朝镜头**。
   *  两者不同源、朝向不相容，且遮挡片是按正面轮廓切的 ⇒ 必须单独生成。
   *
   *  ── 素材身份（硬绑，用户钦定「横数第二个白衣女孩，每个项目都是 HR」）──
   *  参考图 = 立绘表 `bf746083-…png` 第一行第二列「白衣女孩」，
   *  裁件 `tasks/hr-frontdesk/ref/hr-girl-front.png`（148×531）。
   *  ⚠ 与紫衣女孩**不是同一角色** —— 不要把两张 sheet 合并或互相回退。 */
  AGENT_HR_FRONTDESK: "/office-assets/agent-hr-frontdesk-sheet.png?v=h3v1",
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
  // 旧像素角色表 AGENT_MANAGER / AGENT_QA（32×48 帧）条目 2026-09-22 删除：
  // lead / review 两类现统一走 AGENT_DEV（高清 64×96），见下方 sheetUrlForKind。
  [ASSET_URLS.AGENT_PURPLE]: { cols: 4, rows: 2, frameW: 96, frameH: 96, scale: 0.8, scaleMode: "nearest" },
  // HR 前台表与紫衣表**同规格**（4×2 / 96×96 / scale 0.8）：同 scale 才能共用
  // seatPosFor 的锚点语义，换角色不换几何。
  [ASSET_URLS.AGENT_HR_FRONTDESK]: { cols: 4, rows: 2, frameW: 96, frameH: 96, scale: 0.8, scaleMode: "nearest" },
};

/**
 * 紫衣女孩动画帧表 v2（4×2 = 8 帧：0-3 打字循环、4-7 坐姿呼吸，均侧视朝右）。
 * working/idle 在两套坐姿动作间切换；坐下/起身无独立帧，指向静态帧。
 */
export const PURPLE_ANIM_SEQS: Record<AgentAnimKey, number[]> = {
  idle: [4, 5, 6, 7],
  walking: [0],
  working: [0, 1, 2, 3],
  talking: [4, 5, 6, 7],
  alert: [0],
  sitdown: [0],
  sitting: [4, 5, 6, 7],
  sitdown_b: [0],
  sitting_b: [4, 5, 6, 7],
  getup: [0],
};

/**
 * HR 前台动画帧表（4×2 = 8 帧：0-3 打字、4-7 坐姿呼吸，均**正面朝镜头**）。
 *
 * ── 带契约（与紫衣表的「行 = 动作组」一致，但语义更硬）────────────────
 * 行 0（帧 0-3）= **上带**：手抬到键盘高度打字。
 * 行 1（帧 4-7）= **下带**：手放下、放松呼吸。
 * 带内四帧共用一个脚底线与一个头顶线（`build_front_sheet.band_align`），
 * 故**行内任意换帧都不动几何**。
 *
 * ⚠ 但**跨行切换会动**（手的位置不同 ⇒ bbox 高不同）。HR 被柜台挡到只剩头肩，
 * 跨行切换在观感上只有手部差异（手在柜台后不可见），所以安全；
 * 若将来把 HR 的可见范围加大（露出柜台以上更多），必须重新检查这一条。
 *
 * ⚠ 与 `PURPLE_ANIM_SEQS` **不可互换**：紫衣各键的帧号语义是侧视动作，
 * 直接拿去索引正面表会播成跳帧。两者由 `OfficeScene` 按 sheetUrl 二选一。
 */
export const HR_FRONTDESK_ANIM_SEQS: Record<AgentAnimKey, number[]> = {
  idle: [4, 5, 6, 7],
  walking: [4],
  working: [0, 1, 2, 3],
  talking: [4, 5, 6, 7],
  alert: [0],
  sitdown: [0],
  sitting: [4, 5, 6, 7],
  sitdown_b: [0],
  sitting_b: [4, 5, 6, 7],
  getup: [0],
};

/**
 * 前台 HR 的**三态值守周期**（用户钦定：打字 + 呼吸 + 抬头招呼客人）。
 *
 * 为什么单开一个周期而不是复用 `FIRST_AGENT_DEMO_CYCLE_MS`：
 *  1. 那个周期是按 **index === 0** 触发的「演示」路径，而 HR 不一定是 0 号
 *     （roster 按 id 排序，HR 可能是任意位）；两套周期叠加会互相打断。
 *  2. 前台需要**第三态**（招手/招呼），演示周期只有两态。
 *
 * 时序（一个完整周期 15s）：
 *   [0, 8000)                      打字      → working
 *   [8000, 12000)                  呼吸歇息  → idle
 *   [12000, 15000)                 抬头招呼  → talking
 * 招手态借 `talking` 键（`HR_FRONTDESK_ANIM_SEQS.talking` = 呼吸帧）。
 * ⚠ 招手目前落到「呼吸帧 + 名称气泡」而非独立招手帧 —— 想让它真的抬手，
 * 需要 H3 再出一条 `front_greet` 动画并把它接进 `talking` 键（见 SKILL 备忘）。
 */
export const FRONT_DESK_ROUTINE = {
  cycleMs: 15000,
  typingMs: 8000,
  idleMs: 4000,
  /** 招手态时长 = cycleMs - typingMs - idleMs（此处显式写死以便校验） */
  greetMs: 3000,
} as const;

/**
 * 前台 HR 值守周期 → 视觉态。返回 null 表示「不走周期」（调用方保留原态）。
 * **纯函数**：给定 elapsed 毫秒返回 'working' | 'idle' | 'talking'，
 * 不读时钟、不碰 snapshot ⇒ 可单测、无副作用。
 */
export function frontDeskRoutineState(
  elapsedMs: number,
): "working" | "idle" | "talking" {
  const { cycleMs, typingMs, idleMs } = FRONT_DESK_ROUTINE;
  const t = ((elapsedMs % cycleMs) + cycleMs) % cycleMs;   // 负数也安全
  if (t < typingMs) return "working";
  if (t < typingMs + idleMs) return "idle";
  return "talking";
}

/** 演示开关：所有 agent 默认都用紫衣女孩 sheet（视觉统一） */
export const PURPLE_DEMO_ALL_AGENTS = true;

/**
 * 单角色动画演示开关（历史：true 时索引 0 切回 dev 满帧 sheet 跑 FSM 全状态）。
 *
 * 2026-09-08 起恒为 **false**：紫衣 sheet v2（H3 本地视频生成，4×2=8 帧）已有
 * 打字 / 坐姿呼吸两套坐姿动作，0 号角色直接穿紫衣 v2，由 OfficeScene._tick 的
 * index===0 演示周期驱动「打字 ↔ 呼吸」切换，dev 满帧演示下线。
 * （若要恢复 dev 演示，改回 true 即可——_syncActors/_tick 两处按此常量分支。）
 *
 * ── 视角 / 遮挡硬约束（改动前务必读）─────────────────────────
 * 1. 接待员走 A（柜台后，无椅）。工位紫衣走 B（近侧可见椅）：zIndex = base+1
 *    画在 front 片前面，人坐进那把空着的黑椅；scale.x 翻转朝向桌子。
 *    远侧后椅留在 BACK，空着（角色比椅背大，后座上看不见「坐进椅子」）。
 * 2. 演示循环只在**坐姿系**状态间切换（working 打字 ↔ idle 坐姿呼吸），
 *    绝不触发 walking / talking / alert：
 *      - walking 序列会驱动角色离座平移，坐姿与桌面的遮挡关系当场失效；
 *      - walking 表项当前指向静态帧 0（紫衣 v2 无行走帧；行走 sheet 已入库
 *        agent-purple-walk-*.png，未接线——接线前必须先做漫游状态机）。
 * 3. 角色位置冻结在座位上（atDesk 恒 true），不做 roaming / 聚集位移。
 */
export const FIRST_AGENT_FULL_ANIMATIONS = false;
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
  // 2026-09-21 换底图为 KAIROSOFT 平面后**重切**：整件取自 image_1789991327976.jpg
  // （848×689 透明底真值工位，无旧底图烤入阴影），切边用旧 front 片已验证的 ∧ 曲线
  // 作 oracle 映射而来。世界尺度不变（159.2 world ↔ 848 px，S=0.1877）。
  back: { w: 159.2, h: 106.8, leftTop: { x: -78.2, y: -62.1 } },  // 848×569：后椅+远侧显示器+隔板
  front: { w: 143.2, h: 88.0, leftTop: { x: -63.2, y: -20.8 } },  // 763×469：近侧桌面+前椅（挡腿）
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

/**
 * 前台套件几何（2026-09-23 重切 + **分层三片** + **整体缩放 0.70**，用户钦定）。
 *
 * 素材来源：从**同一 KAIROSOFT 平面的已布置版**（`323ef556-…png`）重抠，
 * 画风/光照/像素密度与底图完全同源。原素材 284×158，原显示 217.4×121.0 world。
 *
 * ── 为什么不照原尺寸放（2026-09-23 实测，本值是「解出来」的不是「试出来」的）──
 * 角色 sheet 全身高只有 **76.8 world**（96 帧 × 0.8 缩放）。柜台原尺寸 121.0 高，
 * 其**白台面顶沿**落在 world 521.6、**精灵顶沿** 510.9。两个约束一夹：
 *     ① 头顶必须高于柜台顶沿  ⇒ 鞋底 < 599.8
 *     ② 鞋底必须低于台面顶沿  ⇒ 鞋底 > 521.6
 * 原尺寸下这窗口只有 **−47 … 0**（且旧值 dy=−47 恰好压在边界外 ⇒ 人被整个吞掉）。
 * 观感上「只露一个头」，实测截图确认过。
 *
 * 故按用户裁决**整体缩小柜台**到 **k = 0.70**（152.2 × 84.7 world），
 * 并以**底沿为锚**（保持接地位置不变 ⇒ 不会浮空），缩放后：
 *     柜台顶沿 532.6 / 台面顶沿 550.3  ⇒ 可行鞋底窗口 **(550.3, 599.8)**，宽 49.5
 * 取窗口中部 **鞋底 = 575.0（dy = −30.0）**：
 *     头顶 507.8 ⇒ 高出柜台顶沿 **24.8**（头肩清楚露出）
 *     鞋底 575.0 ⇒ 低于台面顶沿 **24.7**（腿被台面挡住）
 *
 * ⚠ 缩放由 `FRONTDESK_SCALE` 单一常量控制，**不要在 FRONTDESK_SET 里手改 w/h** ——
 *   两片（back/front/chair）必须同缩放，否则切边会错位出缺口。
 * ⚠ 改这个缩放比必须同时重算 `rearChair.dy`（求解过程见上面注释的公式）。
 *
 * ── 为什么分三片（2026-09-23 浏览器实测暴露）──────────────────────
 * 素材原本是**一整片不透明图**（含双显示器 + 白台面 + 木柜面前脸 + 蓝牌）。
 * HR 坐进去后 zIndex 恒低于整片柜台 ⇒ **整个人被柜台轮廓吞掉**，
 * 只剩头顶渗出。修法与桌套件同构（DESK_SET 本就是 back/front 两片）：
 *   back 片 = 台面上的双显示器 + 白台面（角色**之下**，base-2）
 *   front 片 = 木柜面前脸 + 蓝牌 + 接地阴影（角色**之上**，挡腰腿，base）
 *   chair 片 = 柜台正中那件深色扶手家具（**再之上**，base+2）
 * 切边 = **蓝牌上沿**的二次拟合曲线（逐列直接切白台面下沿会产生梳齿状缺口 —— 已证伪）。
 * 切割脚本 `tasks/frontdesk-extract/split_frontdesk_layers.py`，
 * 第三片（chair）由 `split_chair_piece.py` 从 front 再拆，两片 alpha 之和 == 原 front（脚本自检）。
 */
export const FRONTDESK_SCALE = 0.70;

export const FRONTDESK_SET: typeof DESK_SET = {
  /** 后片：双显示器 + 白台面（角色**之下**） */
  back: {
    w: 217.4 * FRONTDESK_SCALE,
    h: 121.0 * FRONTDESK_SCALE,
    leftTop: { x: -107.9 * FRONTDESK_SCALE, y: -108.7 * FRONTDESK_SCALE },
  },
  /** 前片：木柜面前脸 + 蓝牌 + 接地阴影（角色**之上**，挡腰腿） */
  front: {
    w: 217.4 * FRONTDESK_SCALE,
    h: 121.0 * FRONTDESK_SCALE,
    leftTop: { x: -107.9 * FRONTDESK_SCALE, y: -108.7 * FRONTDESK_SCALE },
  },
  /**
   * 前台落座位（2026-09-23 **解出来**的值，不是扫参扫出来的）。
   *
   * ── x 的由来 ──────────────────────────────────────────────────
   * 柜台素材横向中心：back 片 bbox sprite x24..262 的中心 = 143 ≡ **场景 desk.x = 573**
   * （标定时特意让 sprite 中心对齐槽位原点）。故接待位取 **dx = 0**（正中）。
   * ⚠ 历史错误：旧值 `dx = +28.4` 是按「素材内嵌黑椅的椅面中心 sprite(178,80)」标的，
   * 但那件深色家具实测是**背面朝观众的扶手沙发**、摆在台面上（不是给人坐的椅子），
   * 按它对齐会把人推到柜台**偏右**、并且贴着那件沙发。改为正中。
   *
   * ── y 的由来（约束求解）──────────────────────────────────────
   * 两条硬约束（world y 越大越靠下）：
   *   ① 头顶必须高于柜台精灵顶沿 532.6  ⇒ 鞋底 < 532.6 + 67.2 = 599.8
   *   ② 鞋底必须低于白台面顶沿 550.3    ⇒ 鞋底 > 550.3
   * 可行窗口 **(550.3, 599.8)**，取中部 **575.0** ⇒ `dy = 575.0 − 605 = −30.0`：
   *   头顶 507.8（高出顶沿 24.8，头肩露出）/ 鞋底 575.0（低于台面 24.7，腿被挡）。
   *
   * 遮挡链（world z，由低到高）：
   *   back(显示器+白台面, base-2) → 角色(base-1) → front(木柜面+蓝牌, base)
   *   → chair(台面那件深色沙发, base+2)
   */
  rearChair: { dx: 0, dy: -30.0 },
  /** 前台无近侧椅（柜台是一片，不走 B 位）；保留字段满足类型，与 rearChair 同点。 */
  frontChair: { dx: 0, dy: -30.0 },
};

export type FurnitureSet = typeof DESK_SET;

// ── Front Desk Sign (弧面项目名铭牌) ───────────────────────────────

/**
 * 前台招牌的**弧面几何**（唯一权威，Python 标定脚本 `tasks/frontdesk-extract/sign_name.py`
 * 的参数以此为准，改这里要同步改那边）。
 *
 * 背景（2026-09-23 用户钦定）：柜台蓝牌是**弧形**（随柜台圆柱面弯曲），
 * 平面文字盖上去必假 ⇒ 必须按弧面逐列弯曲。实测（284×158 sprite 像素）：
 *   - x 50..85  ：牌高 22→31（弧面**远端**，被透视压缩）
 *   - x 85..194 ：牌高稳定约 31（弧面**近端/正面区**）
 * 两条边缘均为二次曲线（`polyfit` 实测，曲率非零 ⇒ 确为弧非梯形）。
 *
 * 上下边缘：top(x) / bot(x) 为 sprite 像素坐标。前端按列切片时，
 * 第 i 列（x = SIGN_X0 + i）的文字要纵向拉伸到 bot(x)-top(x) 并放在 top(x) 处。
 */
export const FRONT_SIGN = {
  /** 牌左右缘（sprite 像素） */
  x0: 50,
  x1: 194,
  /** 上边缘二次曲线 y = a·x² + b·x + c */
  top: { a: -0.0007, b: 0.3216, c: 54.07 },
  /** 下边缘二次曲线 */
  bot: { a: -0.0015, b: 0.5433, c: 70.98 },
} as const;

/** sprite 像素 → 该 x 处的上边缘 / 牌高 */
export function frontSignTop(x: number): number {
  const { a, b, c } = FRONT_SIGN.top;
  return a * x * x + b * x + c;
}
export function frontSignHeight(x: number): number {
  const { a, b, c } = FRONT_SIGN.bot;
  const botY = a * x * x + b * x + c;
  return botY - frontSignTop(x);
}

/** 槽位 → 家具套件（前台槽位用前台几何，其余用标准桌套件）
 *  ⚠ 2026-09-23：判据由 `id === "review-2"` 改为 `id === FRONDESK_SLOT.id`。
 *  旧判据是死代码——DESKS 里从来没有 review-2（前台槽位独立于网格后），
 *  导致前台柜台**从未渲染**且 `purpleDemoSeat` 静默 fallback。 */
export function furnitureSet(desk: DeskSlot): FurnitureSet {
  return desk.id === FRONDESK_SLOT.id ? FRONTDESK_SET : DESK_SET;
}

/** 是否前台槽位。渲染层要按它选前台专用素材（分层两片），别再散落 `id === "front-desk"`。 */
export function isFrontDeskSlot(desk: DeskSlot): boolean {
  return desk.id === FRONDESK_SLOT.id;
}

/**
 * 深度基准线（world y）：取 front 片底边。
 * front 片 zIndex = base；A 位角色 = base-1（被桌面挡腿），B 位 = base+1（坐进近侧椅）；
 * back 片 = base-2。跨桌用同一基准线不产生穿插。
 */
export function deskDepthBase(desk: DeskSlot): number {
  const set = furnitureSet(desk);
  // + desk.x * 1e-3：**同排并列的 tiebreak**。
  // 2026-09-23 改成规则网格后，同一排的多张桌 `y` 完全相同 ⇒ base 并列；跨排仍差 132，
  // 同排靠「JS sort 稳定 + 恰好不重叠」兜底（实测同排 back 间隙仅 6.8px）。
  // 一旦精灵变宽或席位挪位就会失序，故加一个远小于 1 个 zIndex 单位、只用于同排定序的偏移。
  // ⚠ 调用方**不得再 Math.round** 这个返回值（会把 1e-3 抹平，tiebreak 失效）。
  return desk.y + set.front.leftTop.y + set.front.h + desk.x * 1e-3;
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
  // lead / review 曾各有独立像素 sheet（agent-manager / agent-qa，32×48 帧），
  // 2026-09-22 随美术路线裁决统一到 AGENT_DEV（高清 64×96）—— 像素方向永久放弃。
  if (kind === "lead") return ASSET_URLS.AGENT_DEV;
  if (kind === "review") return ASSET_URLS.AGENT_DEV;
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

/**
 * 同屏角色上限 = 标定出的席位数（**不是** 7，也不是 §5.3 的 10）。
 * `OfficeScene._orderedAgents()` 按 id 排序后 `slice(0, 这个数)` —— 超出的 agent
 * **不上场且无任何提示**（2026-09-22 实测：6 人项目只坐 4 席）。改席位数就是改这个值。
 */
export const MAX_VISIBLE_AGENTS = DESKS.length;
