/**
 * gamewindow store —— FE-16 停靠 / 专注 / 归位 / 边界裁剪（设计规格 §8.5 C1 + §6.3）
 *
 * 契约（§8.5 硬规则）：
 *   1. 历史几何优先于停靠预设 —— 停靠只是首次默认值，不是强制吸附；
 *   2. 一键归位只重置几何，不清聊天/队列/草稿/业务状态；
 *   3. 专注不改变业务数据：进出只改几何，payload/标题/pin 原样；
 *   4. 最小化几何不写入正常尺寸（MINIMIZED_GEOM_MAX 判据在 GameWindow.tsx 守住，
 *      这里锁 setGeometry 对专注期的写入豁免）；
 *   5. 恢复历史几何做视口边界裁剪（大屏记忆小屏打开 ⇒ 拉回可达区，T-18）。
 */
import { beforeEach, describe, expect, it } from "vitest";
import {
  clampToViewport,
  gameWindowId,
  useGameWindowStore,
  type GameWindowState,
} from "./store";

const GEOM_KEY = "hw-gamewin-geom";
/** jsdom 默认视口（innerWidth×innerHeight = 1024×768） */
const VW = 1024;
const VH = 768;
const MARGIN = 12;
const TOP_INSET = 94;

function gw() {
  return useGameWindowStore.getState();
}

/** 与 OfficeWorkspace.test 同款：显式重置全部窗口层状态（含 FE-16 新字段） */
function resetStore() {
  useGameWindowStore.setState({
    windows: [],
    focusSignal: {},
    pinnedAgent: {},
    focusModeId: null,
    preFocusGeom: {},
    layoutResetNonce: 0,
  });
}

function winByKind(kind: string): GameWindowState {
  const win = gw().windows.find((w) => w.kind === kind);
  if (!win) throw new Error(`window kind=${kind} not open`);
  return win;
}

/** 预置历史几何（模拟上一会话留下的记忆） */
function seedCache(geom: Record<string, unknown>) {
  localStorage.setItem(GEOM_KEY, JSON.stringify(geom));
}

beforeEach(() => {
  localStorage.clear();
  resetStore();
});

// ── 停靠预设（§8.5 表：成员级面板首开 ⇒ 右缘 400 宽贴边） ────────────

describe("FE-16 停靠预设", () => {
  it("无历史几何首开 chat ⇒ 停靠几何：宽 400、x=视口宽−400−MARGIN、y=TOP_INSET、h 吃满工作区", () => {
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" }, "聊天 · 甲");

    const geom = winByKind("chat").geom;
    expect(geom.w).toBe(400);
    expect(geom.x).toBe(VW - 400 - MARGIN); // 612：右缘贴边（距右 MARGIN）
    expect(geom.y).toBe(TOP_INSET);
    expect(geom.h).toBe(VH - TOP_INSET - MARGIN);
  });

  it("成员级四类（chat/agent/logs/monitor）都落停靠；org/timeline 等主视图窗维持居中默认", () => {
    for (const kind of ["chat", "agent", "logs", "monitor"] as const) {
      useGameWindowStore.getState().open(kind, { agentId: "agent-A" });
      expect(winByKind(kind).geom.w).toBe(400);
      expect(winByKind(kind).geom.x).toBe(VW - 400 - MARGIN);
      useGameWindowStore.getState().closeKind(kind);
    }
    // 主视图载体不强行停靠（§8.5 表）：org 用 DEFAULT_SIZE 居中，非 400 宽贴边
    useGameWindowStore.getState().open("org");
    const org = winByKind("org").geom;
    expect(org.w).toBe(760);
    expect(org.x).not.toBe(VW - 400 - MARGIN);
  });

  it("有历史几何 ⇒ 用历史（停靠预设只兜首开）", () => {
    seedCache({ chat: { x: 100, y: 150, w: 500, h: 400 } });
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    const geom = winByKind("chat").geom;
    expect(geom).toEqual({ x: 100, y: 150, w: 500, h: 400 });
  });

  it("拖走后不再落预设：拖动写入几何记忆，重开沿用记忆位置", () => {
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    // 用户拖走（onmove → setGeometry → 记忆接管）
    useGameWindowStore.getState().setGeometry(gameWindowId("chat"), { x: 40, y: 60 });
    useGameWindowStore.getState().close(gameWindowId("chat"));
    expect(gw().windows).toHaveLength(0);

    useGameWindowStore.getState().open("chat", { agentId: "agent-B" });
    const geom = winByKind("chat").geom;
    expect(geom.x).toBe(40);
    expect(geom.y).toBe(60);
    expect(geom.w).toBe(400); // 尺寸记忆来自上次（未改尺寸 ⇒ 保持）
    // 记忆确实落了盘（不只是内存残留）
    expect(JSON.parse(localStorage.getItem(GEOM_KEY)!).chat.x).toBe(40);
  });
});

// ── 专注模式（§8.5：占满主工作区，退出恢复原几何，业务不动） ──────────

describe("FE-16 专注模式", () => {
  it("进入专注 ⇒ 几何占满主工作区（视口 − App header − HUD）；退出 ⇒ 恢复进入前几何", () => {
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    const before = winByKind("chat").geom;

    useGameWindowStore.getState().enterFocus(gameWindowId("chat"));
    expect(gw().focusModeId).toBe("chat");
    const focused = winByKind("chat").geom;
    expect(focused).toEqual({ x: MARGIN, y: TOP_INSET, w: VW - 2 * MARGIN, h: VH - TOP_INSET - MARGIN });

    useGameWindowStore.getState().exitFocus(gameWindowId("chat"));
    expect(gw().focusModeId).toBe(null);
    expect(winByKind("chat").geom).toEqual(before);
  });

  it("专注不改业务数据：payload/标题/pin 原样，仅发一次聚焦信号（不重挂载的前提）", () => {
    useGameWindowStore.getState().open("agent", { agentId: "agent-B" }, "详情 · 乙");
    useGameWindowStore.getState().setAgentPin("agent", "agent-B");
    const nonceBefore = gw().focusSignal["agent"] ?? 0;

    useGameWindowStore.getState().enterFocus(gameWindowId("agent"));
    useGameWindowStore.getState().exitFocus(gameWindowId("agent"));

    const win = winByKind("agent");
    expect(win.payload).toEqual({ agentId: "agent-B" });
    expect(win.title).toBe("详情 · 乙");
    expect(gw().pinnedAgent.agent).toBe("agent-B");
    // enter / exit 各发一次聚焦信号（GameWindow 据此 restore+focus，与几何无关）
    expect(gw().focusSignal["agent"]).toBe(nonceBefore + 2);
  });

  it("专注互斥：A 专注中进入 B ⇒ A 自动退出并恢复原几何", () => {
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    useGameWindowStore.getState().open("org");
    const chatBefore = winByKind("chat").geom;

    useGameWindowStore.getState().enterFocus(gameWindowId("chat"));
    useGameWindowStore.getState().enterFocus(gameWindowId("org"));

    expect(gw().focusModeId).toBe("org");
    expect(winByKind("chat").geom).toEqual(chatBefore); // chat 已恢复
    expect(winByKind("org").geom.w).toBe(VW - 2 * MARGIN); // org 正专注
  });

  it("专注期间 setGeometry 被忽略（几何由专注态接管，退出才能干净恢复）", () => {
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    useGameWindowStore.getState().enterFocus(gameWindowId("chat"));
    const focused = winByKind("chat").geom;

    // 模拟专注中拖拽（WinBox onmove → setGeometry）
    useGameWindowStore.getState().setGeometry(gameWindowId("chat"), { x: 1, y: 1, w: 100, h: 100 });
    expect(winByKind("chat").geom).toEqual(focused);
  });

  it("对不存在 / 未开窗口的专注操作是安全的 no-op", () => {
    expect(() => gw().enterFocus("chat")).not.toThrow();
    expect(() => gw().exitFocus("chat")).not.toThrow();
    expect(gw().focusModeId).toBe(null);
  });
});

// ── 一键归位（§8.5 规则 2：只重置布局，不清业务状态） ─────────────────

describe("FE-16 一键归位", () => {
  it("几何清空并按类型重落预设；几何记忆（localStorage）一并清空", () => {
    seedCache({ org: { x: 50, y: 50, w: 500, h: 400 } });
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    useGameWindowStore.getState().open("org");
    // 拖走 chat，让它的当前几何偏离预设
    useGameWindowStore.getState().setGeometry(gameWindowId("chat"), { x: 30, y: 30 });

    useGameWindowStore.getState().resetLayout();

    // 成员级 ⇒ 重新停靠；主视图 ⇒ 历史已清，回居中默认
    expect(winByKind("chat").geom).toEqual({
      x: VW - 400 - MARGIN,
      y: TOP_INSET,
      w: 400,
      h: VH - TOP_INSET - MARGIN,
    });
    expect(winByKind("org").geom.w).toBe(760);
    // 几何记忆清空（归位后重开不复活旧位置）
    expect(localStorage.getItem(GEOM_KEY)).toBe(null);
    // 归位 nonce 递增（GameWindow 据此把 WinBox 同步到新几何）
    expect(gw().layoutResetNonce).toBe(1);
  });

  it("归位不动业务状态：窗口数、payload、标题、pin、专注外的信号全部保留", () => {
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" }, "聊天 · 甲");
    useGameWindowStore.getState().open("agent", { agentId: "agent-B" }, "详情 · 乙");
    useGameWindowStore.getState().setAgentPin("agent", "agent-B");
    const before = gw().windows.map((w) => ({
      id: w.id,
      kind: w.kind,
      title: w.title,
      payload: w.payload,
    }));

    useGameWindowStore.getState().resetLayout();

    expect(gw().windows.map((w) => ({ id: w.id, kind: w.kind, title: w.title, payload: w.payload })))
      .toEqual(before);
    expect(gw().pinnedAgent.agent).toBe("agent-B");
    expect(gw().focusModeId).toBe(null); // 专注态一并复位（属布局）
    expect(gw().preFocusGeom).toEqual({});
  });

  it("归位清掉防抖写：清缓存后 300ms 内旧几何不复活", async () => {
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    useGameWindowStore.getState().setGeometry(gameWindowId("chat"), { x: 66, y: 66 });
    useGameWindowStore.getState().resetLayout();
    // 立即断言 + 等过防抖窗口，确认没有挂着的定时器把旧值写回
    expect(localStorage.getItem(GEOM_KEY)).toBe(null);
    await new Promise((r) => setTimeout(r, 350));
    expect(localStorage.getItem(GEOM_KEY)).toBe(null);
  });
});

// ── 边界裁剪（§6.3：历史几何超视口 ⇒ 拉回可达区；T-18） ───────────────

describe("FE-16 边界裁剪", () => {
  it("clampToViewport：大屏记忆在小视口收进来，标题栏/关闭钮可达", () => {
    // 3000×2000 屏上留下的几何，落到 1024×768
    const clamped = clampToViewport({ x: 2500, y: 1800, w: 2600, h: 1600 }, VW, VH);
    expect(clamped.w).toBeLessThanOrEqual(VW - 2 * MARGIN);
    expect(clamped.h).toBeLessThanOrEqual(VH - MARGIN);
    expect(clamped.x).toBeLessThanOrEqual(VW - clamped.w); // 右缘不出视口（关闭钮可达）
    expect(clamped.y).toBeGreaterThanOrEqual(0); // 标题栏可达
    expect(clamped.y).toBeLessThanOrEqual(VH - 36); // 标题栏 36px 高不出下缘
  });

  it("clampToViewport：负坐标拉回可视区，但不小于『关闭钮仍可见』的左界", () => {
    const clamped = clampToViewport({ x: -5000, y: -100, w: 560, h: 640 }, VW, VH);
    expect(clamped.x).toBe(120 - 560); // 右缘至少留 120px 可见（控制钮所在端）
    expect(clamped.y).toBe(0);
  });

  it("clampToViewport：合法几何原样返回（幂等，预设不被裁坏）", () => {
    const dock = { x: VW - 400 - MARGIN, y: TOP_INSET, w: 400, h: VH - TOP_INSET - MARGIN };
    expect(clampToViewport(dock, VW, VH)).toEqual(dock);
    const focus = { x: MARGIN, y: TOP_INSET, w: VW - 2 * MARGIN, h: VH - TOP_INSET - MARGIN };
    expect(clampToViewport(focus, VW, VH)).toEqual(focus);
  });

  it("恢复历史几何时裁剪生效：超视口缓存 ⇒ 开窗落在可视区内", () => {
    seedCache({ chat: { x: 5000, y: -999, w: 2000, h: 3000 } });
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    const geom = winByKind("chat").geom;
    expect(geom.x).toBeGreaterThanOrEqual(0);
    expect(geom.x + geom.w).toBeLessThanOrEqual(VW);
    expect(geom.y).toBeGreaterThanOrEqual(0);
    expect(geom.h).toBeLessThanOrEqual(VH - MARGIN);
  });

  it("脏缓存（缺字段 / 非有限数）不采纳 ⇒ 回落停靠预设，不把 NaN 摆进 winbox", () => {
    seedCache({ chat: { x: 100, y: 100, w: 400 } }); // 缺 h
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    expect(winByKind("chat").geom).toEqual({
      x: VW - 400 - MARGIN,
      y: TOP_INSET,
      w: 400,
      h: VH - TOP_INSET - MARGIN,
    });
    useGameWindowStore.getState().close(gameWindowId("chat"));

    seedCache({ chat: { x: Number.NaN, y: 0, w: 400, h: 400 } }); // NaN
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    expect(Number.isFinite(winByKind("chat").geom.x)).toBe(true);
    expect(winByKind("chat").geom.w).toBe(400); // 停靠预设
  });

  it("持久化路径同样裁剪：拖出屏外的几何写入时拉回，关窗即落盘（越界自动拉回）", () => {
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    useGameWindowStore.getState().setGeometry(gameWindowId("chat"), { x: 5000, y: 5000 });
    const geom = winByKind("chat").geom;
    expect(geom.x + geom.w).toBeLessThanOrEqual(VW);
    expect(geom.y).toBeLessThanOrEqual(VH - 36);
    // 关窗 flush 挂起的几何写 ⇒ 记忆里存的也是裁剪后的值（不是越界原值）
    useGameWindowStore.getState().close(gameWindowId("chat"));
    expect(JSON.parse(localStorage.getItem(GEOM_KEY)!).chat).toEqual(geom);
  });
});

// ── 专注态生命周期收尾（与窗口关闭的组合） ────────────────────────────

describe("FE-16 专注态与窗口关闭", () => {
  it("关闭专注中的窗口 ⇒ focusModeId 与 preFocusGeom 一并清理", () => {
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    useGameWindowStore.getState().enterFocus(gameWindowId("chat"));
    useGameWindowStore.getState().close(gameWindowId("chat"));
    expect(gw().focusModeId).toBe(null);
    expect(gw().preFocusGeom).toEqual({});
  });

  it("closeKind / closeAll 同样清理专注态", () => {
    useGameWindowStore.getState().open("chat", { agentId: "agent-A" });
    useGameWindowStore.getState().enterFocus(gameWindowId("chat"));
    useGameWindowStore.getState().closeKind("chat");
    expect(gw().focusModeId).toBe(null);

    useGameWindowStore.getState().open("org");
    useGameWindowStore.getState().enterFocus(gameWindowId("org"));
    useGameWindowStore.getState().closeAll();
    expect(gw().focusModeId).toBe(null);
    expect(gw().preFocusGeom).toEqual({});
    expect(gw().windows).toHaveLength(0);
  });
});
