/**
 * 统一导航命令（设计方案 §14.3；FE-01 / FE-02 / FE-07）
 *
 * 为什么存在：开窗此前依赖 `selectedAgentId` **变化**驱动的 effect ——
 * 同一个人重复点击不产生状态变化，动作就像失效了（UX-01 根因）。导航命令把
 * 「打开/聚焦」变成**显式动作**，语义收敛为：
 *
 *   已开且同对象 ⇒ 聚焦；最小化 ⇒ 恢复并置顶；已关 ⇒ 重开；
 *   另一对象   ⇒ 同一单例窗换 payload（43d6256 单例契约：gameWindowId 恒为 kind）。
 *
 * 边界：本模块只编排 app / gamewindow 两个 store，不持有业务状态；
 * 无效参数（空 id / null 清除）直接拒绝或不开窗；找不到的任务由
 * TaskTimelinePanel 自身的错误态给出说明（不空白，见其 error 分支）。
 * 办公室窗口层未挂载（工作台形态）时不开游戏窗 —— 窗开了也没有层渲染它。
 */
import { useAppStore } from "../store";
import {
  gameWindowId,
  useGameWindowStore,
  type GameWindowKind,
} from "../components/gamewindow/store";
import { agentDisplayName } from "./agentNames";

// ── 办公室窗口层在位信号 ─────────────────────────────────────────
// GameWindowLayer 只在 workspaceMode==="office" 时随 OfficeWorkspace 挂载
// （App.tsx 顶层分支）。该模式不在任何 store 里（App 本地 state），所以由
// OfficeWorkspace 挂载/卸载时打点。唯一真正需要它的是 openTask —— 时间线
// 入口在两种形态下都存在，其它命令的调用点本来就都在办公室层内。

let officeSurfaceActive = false;

/** OfficeWorkspace 挂载时置 true、卸载时置 false。 */
export function setOfficeSurfaceActive(active: boolean): void {
  officeSurfaceActive = active;
}

// ── 标题 ─────────────────────────────────────────────────────────

/** 对象名进标题（§6.3「标题应带对象名」）；名字未知则退回裸基础名。 */
function agentWindowTitle(
  kind: Extract<GameWindowKind, "chat" | "agent">,
  agentId: string,
  pinned = false,
): string {
  const base = kind === "chat" ? "聊天" : "详情";
  const name = agentDisplayName(agentId);
  return name ? `${base} · ${name}${pinned ? "（已固定）" : ""}` : base;
}

// ── FE-01（UX-01）：成员聊天 ─────────────────────────────────────

/**
 * 显式「打开/聚焦成员聊天窗」命令。
 * 调用点：OfficeView 场景点击（每次点击都发，含同 ID 重复点击）+
 * OfficeWorkspace 的选中 effect（承接 OrgTree / 项目初始化等其它选中来源）。
 */
export function openAgentChat(agentId: string): void {
  if (!agentId || !officeSurfaceActive) return;
  useGameWindowStore.getState().open("chat", { agentId }, agentWindowTitle("chat", agentId));
}

// ── FE-07（UX-02）：详情窗跟随与固定 ─────────────────────────────

/**
 * 选中变化时详情窗**跟随**：窗口未开不开（打开走 HUD 显式入口）、
 * 固定中不覆盖、内容已是该对象则不动。
 * 用 retarget 而非 open —— 跟随只换内容，不发聚焦信号（不抢 z 序、
 * 不把最小化窗口拽回来）。
 */
export function followAgentDetail(agentId: string): void {
  if (!agentId || !officeSurfaceActive) return;
  const gw = useGameWindowStore.getState();
  if (!gw.isOpen("agent")) return;
  if (gw.pinnedAgent.agent != null) return;
  const win = gw.windows.find((w) => w.kind === "agent");
  if (!win || win.payload.agentId === agentId) return;
  gw.retarget("agent", { agentId }, agentWindowTitle("agent", agentId));
}

/**
 * HUD「详情」入口：打开/聚焦详情窗。**固定期只聚焦不覆盖** ——
 * 否则「固定乙 → 选中甲 → 点详情」会静默把窗换成甲，正是 UX-02 要消灭的混淆。
 */
export function openAgentDetailWindow(agentId: string): void {
  if (!agentId || !officeSurfaceActive) return;
  const gw = useGameWindowStore.getState();
  if (gw.pinnedAgent.agent != null && gw.isOpen("agent")) {
    gw.requestFocus(gameWindowId("agent"));
    return;
  }
  gw.open("agent", { agentId }, agentWindowTitle("agent", agentId));
}

/**
 * 详情窗 pin 开关（窗内角标调用）。
 * 固定 = 钉住当前 payload 对象，标题立即可见「已固定：乙」；
 * 解除 = 清 pin 并立即恢复跟随当前选中（无选中则停在原地）。
 * pin 存在窗口 store、随窗关闭自动清除（见 gamewindow/store），不会比窗口活得久。
 */
export function setAgentDetailPinned(pinned: boolean): void {
  const gw = useGameWindowStore.getState();
  const win = gw.windows.find((w) => w.kind === "agent");
  if (!win) return; // 窗不在，pin 无处安放
  // 幂等护栏：同向重复 toggle（双击角标等）直接忽略，避免无意义的 state 翻动
  if ((gw.pinnedAgent.agent != null) === pinned) return;
  const currentId = win.payload.agentId;
  if (pinned) {
    if (!currentId) return; // Missing 占位（无对象）不可固定
    gw.setAgentPin("agent", currentId);
    gw.setTitle(win.id, agentWindowTitle("agent", currentId, true));
    return;
  }
  gw.setAgentPin("agent", null);
  gw.setTitle(win.id, agentWindowTitle("agent", currentId ?? "", false));
  const selected = useAppStore.getState().selectedAgentId;
  if (selected && selected !== currentId) {
    gw.retarget("agent", { agentId: selected }, agentWindowTitle("agent", selected, false));
  }
}

// ── FE-02（UX-04）：统一任务打开 ─────────────────────────────────

/**
 * 统一任务打开命令 —— 所有入口（时间线任务条 / 任务搜索 / 直达 task_id /
 * 清除选中）都走这里，不再各自调用 setSelectedTask。
 * ① 既有选中语义原样保留（工作台右栏页签联动 = 旧形态行为不变）；
 * ② 办公室形态 ⇒ 打开/聚焦任务窗（单例换 payload，语义同 FE-01）。
 * 无效任务：窗口照常打开，TaskTimelinePanel 对 404 有明确错误态 + 重试，
 * 不是空白页。
 */
export function openTask(taskId: string | null): void {
  useAppStore.getState().setSelectedTask(taskId);
  if (!taskId) return; // 清除选中不是「打开」，不开窗
  if (!officeSurfaceActive) return;
  useGameWindowStore.getState().open("task", { taskId });
}
