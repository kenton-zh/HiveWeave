/**
 * 单个游戏窗口 —— WinBox 薄封装 + React portal
 *
 * 三个设计决定（都有明确理由，改动前先读）：
 *
 * 1. **用 React portal，不用嵌套 createRoot**：窗口内容渲染进 WinBox 的
 *    `.wb-body`，仍然是同一棵 React 树 ⇒ 主 store / context / 既有面板组件
 *    全部可直接使用。换成 createRoot 会切断 context，ChatPanel 这类读 store
 *    的组件会直接失效。
 *
 * 2. **窗口只建一次**（deps=[]）：几何初值从 store 取，此后 WinBox 的移动 /
 *    缩放只**单向回写** store，store 不反过来驱动 WinBox —— 避免受控 / 非受控
 *    双向同步的抖动（拖拽过程中被 props 拽回去）。例外是**编程式几何变更**
 *    （FE-16 专注进出 / 一键归位）：store 已把目标几何写好，这里按信号把
 *    WinBox 拉到该几何 —— 只在信号变化时应用，用户拖拽路径不走这里（无回环）。
 *
 * 3. **销毁路径只走一次**：用户点 X 时 `onclose` 返回 `true`（⚠ winbox 语义是
 *    **truthy 才中止关闭**，与直觉相反）拦下自毁、只同步 store，真正销毁由 React
 *    卸载的 cleanup `wb.close(true)` 完成；cleanup 自身放行（force ⇒ 返回 false）。
 *    反向写会二次销毁：此时 `stack_win.indexOf(this)` 已是 -1 ⇒ `splice(-1,1)`
 *    **误删栈尾的另一个窗口**，再走到 `this.unmount()` 读已置 null 的 body 抛
 *    TypeError（winbox.js:1219/1221，审计 2026-09-22 实锤）。
 * 4. **基础样式单独引入**（winbox.min.css）—— ESM 源入口不注入 CSS，见下方 import 注释。
 *
 * FE-16（2026-09-25，设计规格 §8.5）：专注模式 ——
 *   - 标题栏「专注」按钮（winbox addControl，插在关闭钮前）⇔ store.toggleFocus；
 *   - 进出专注只改几何：WinBox 被 resize/move 到 store 现值，**内容不重挂载**；
 *   - 专注期间 setGeometry 被 store 忽略（几何由专注状态接管），拖拽不留痕；
 *   - Esc 退出专注：本窗处于专注态时才监听（§8.2 Esc 只关层，不提交不清草稿）。
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
// ⚠ 必须走 ESM 源入口（深路径）：winbox 的 `browser` 字段指向 UMD bundle，
// Vite 生产构建优先取它 ⇒ rollup 报 "default is not exported"。详见 types/winbox.d.ts。
import WinBox, { type WinBoxInstance } from "winbox/src/js/winbox.js";
import { useGameWindowStore, type GameWindowKind, type GameWindowState } from "./store";
import { isWindowMenuOpen } from "./WindowMenu";
// ⚠ winbox 的**基础样式必须自行引入**：ESM 源入口（src/js/winbox.js）不注入 CSS，
// 只有 UMD bundle 内联。缺它会直接崩：`.winbox` 没有 position:fixed、`.wb-control *`
// 没有 display:inline-block ⇒ 窗口退化成 body 下的静态块、拖不动、resize 手柄失效。
import "winbox/dist/css/winbox.min.css";
import "./gamewindow.css";

/** 窗口 z-index 起点：必须高于 App header(z-30) 与办公室 HUD(z-20)，否则窗口拖到
 *  顶部会被压住、连拖拽手柄都点不到（winbox 默认从 10 起自增，见 winbox.js:24/933）。 */
const BASE_Z_INDEX = 100;

/** 最小化时 winbox 会把窗口压成高度 = header(35px) 的底部细条 —— 用它区分最小化几何 */
const MINIMIZED_GEOM_MAX = 60;

/** FE-11：文本密集窗（chat / agent 详情 / logs / task 详情）挂 `hw-win-solid`，
 *  正文底色高不透明 —— 地板家具不再透出（设计规格 §4.5 窗口外观统一第 2 条；
 *  css 落点见 gamewindow.css `.hw-win-solid`）。图形/图表窗（org/timeline/token/
 *  goals/monitor/debug）维持暖白 97% 半透明，与场景保持联动。 */
const SOLID_BODY_KINDS: ReadonlySet<GameWindowKind> = new Set(["chat", "agent", "logs", "task"]);

/** 专注按钮的 DOM 类名（wb-control 内，removeControl 按它找） */
const FOCUS_CTRL_CLASS = "hw-win-focus-ctrl";
/** wb-control 模板子序：[wb-min, wb-max, wb-full, wb-close] —— 插在关闭钮前 */
const FOCUS_CTRL_INDEX = 3;
/** 专注按钮两态文案：未专注 = 进入（撑满主工作区）；已专注 = 退出（收回原几何）。
 *  用二字文本而非图形字符 —— 控制钮 26px 宽装得下，且不赌特殊字体的字形覆盖。 */
const FOCUS_CTRL_ENTER = "专注";
const FOCUS_CTRL_EXIT = "还原";

/** 专注按钮的可读名称（§8.7 图标钮必须有名称）——创建时与两态切换时共用 */
function describeFocusCtrl(ctrl: HTMLElement, isFocused: boolean) {
  ctrl.textContent = isFocused ? FOCUS_CTRL_EXIT : FOCUS_CTRL_ENTER;
  ctrl.title = isFocused ? "退出专注（恢复原几何，Esc）" : "专注视图（占满主工作区）";
  ctrl.setAttribute("aria-label", ctrl.title);
  ctrl.setAttribute("aria-pressed", String(isFocused));
}

interface Props {
  win: GameWindowState;
  children: ReactNode;
}

export default function GameWindow({ win, children }: Props) {
  const [body, setBody] = useState<HTMLElement | null>(null);
  const wbRef = useRef<WinBoxInstance | null>(null);
  // 专注按钮 DOM（addControl 不返回节点，建好后按类名取一次）
  const focusCtrlRef = useRef<HTMLElement | null>(null);
  // 只订阅自己这一条聚焦信号（避免整个窗口层因任一窗口聚焦而重渲染）
  const focusNonce = useGameWindowStore((s) => s.focusSignal[win.id] ?? 0);
  // FE-16：本窗是否处于专注态（布尔选择器 —— 只有本窗进出专注才重渲染）
  const isFocused = useGameWindowStore((s) => s.focusModeId === win.id);
  // FE-16：一键归位信号（计数器，只在归位时变化）
  const resetNonce = useGameWindowStore((s) => s.layoutResetNonce);

  useEffect(() => {
    // 最小化时 winbox 会调 resize(...,true)+move(...,true) 把窗口压成底部细条，
    // 虽带 _skip_update（不写内部值）但**仍触发 onresize/onmove 回调** ⇒ 那些几何
    // 不能进 store，否则持久化后重开同类窗口只剩底部一条（winbox.js:525-533）。
    // 判据用几何值而非 timing：窗口 minheight=220，任何 h<=60 的回调必是最小化态；
    // 且 resize 先于 move 触发，故这个 flag 能顺势挡住随后的 move。
    let minimizedLike = false;

    const wb = new WinBox({
      title: win.title,
      class: `hw-win no-full${SOLID_BODY_KINDS.has(win.kind) ? " hw-win-solid" : ""}`,
      index: BASE_Z_INDEX,
      x: win.geom.x,
      y: win.geom.y,
      width: win.geom.w,
      height: win.geom.h,
      minwidth: 300,
      minheight: 220,
      onclose: (force?: boolean) => {
        // ⚠ winbox 的语义与直觉相反：onclose 返回 **truthy 才中止关闭**
        // （源码 winbox.js:1207 `if(onclose && onclose(force)){ return true; }`）。
        // 所以：cleanup 发起的 force 调用放行（false），用户点 X 的非 force 调用
        // 中止（true）并交给 React 卸载销毁 —— 保证销毁路径只走一次、不会二次
        // splice 到栈尾其它窗口。
        if (force) return false;
        useGameWindowStore.getState().close(win.id);
        return true;
      },
      onmove: (x, y) => {
        if (minimizedLike) return;
        useGameWindowStore.getState().setGeometry(win.id, { x, y });
      },
      onresize: (w, h) => {
        minimizedLike = h <= MINIMIZED_GEOM_MAX || w <= MINIMIZED_GEOM_MAX;
        if (minimizedLike) return;
        useGameWindowStore.getState().setGeometry(win.id, { w, h });
      },
      // 最小化态回写 store（「已打开面板菜单」要显示是否最小化；restore 同理）
      onminimize: () => useGameWindowStore.getState().setMinimized(win.id, true),
      onrestore: () => useGameWindowStore.getState().setMinimized(win.id, false),
    });
    wbRef.current = wb;
    setBody(wb.body);

    // FE-16：标题栏「专注」按钮（winbox 官方 addControl；样式复用 .wb-control * 既有皮肤）
    wb.addControl({
      class: FOCUS_CTRL_CLASS,
      index: FOCUS_CTRL_INDEX,
      click: () => useGameWindowStore.getState().toggleFocus(win.id),
    });
    const ctrl = wb.window.getElementsByClassName(FOCUS_CTRL_CLASS)[0] as HTMLElement | undefined;
    if (ctrl) {
      ctrl.style.cursor = "pointer";
      describeFocusCtrl(ctrl, false); // 初始即有可读名称（§8.7），非等首次切换才补
    }
    focusCtrlRef.current = ctrl ?? null;

    // onfocus 不接：WinBox 自身点击置顶已足够，接上会与 focusSignal 形成回环

    return () => {
      // 卸载前摘掉自加的控制钮与监听（钮的 onclick 随节点销毁，这里显式移除
      // 是 FE-18 资源清理纪律：不留挂在外部容器上的游离 DOM）
      try {
        wb.removeControl(FOCUS_CTRL_CLASS);
      } catch {
        /* 窗口 DOM 已被 winbox 自行清理时忽略 */
      }
      wbRef.current = null;
      focusCtrlRef.current = null;
      wb.close(true);
    };
    // 仅建窗一次；几何/标题的后续变化走下面的独立 effect
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 标题同步（如选中 agent 改名后）
  useEffect(() => {
    wbRef.current?.setTitle(win.title);
  }, [win.title]);

  // 聚焦信号：置顶 + 从最小化恢复
  useEffect(() => {
    const wb = wbRef.current;
    if (!wb || focusNonce === 0) return;
    try {
      wb.restore();
    } catch {
      /* 未处于最小化时个别版本会抛，忽略 */
    }
    wb.focus();
  }, [focusNonce]);

  // FE-16：把 WinBox 几何拉到 store 现值（编程式几何变更专用）。
  // 只在专注进出 / 归位信号时触发 —— 用户拖拽只单向回写 store，不走这里（无回环）。
  // 先 restore（非最小化时是 no-op；最小化时先把 winbox 内部值恢复）再 resize+move。
  const applyStoreGeometry = () => {
    const wb = wbRef.current;
    if (!wb) return;
    const cur = useGameWindowStore.getState().windows.find((w) => w.id === win.id);
    if (!cur) return;
    try {
      wb.restore();
    } catch {
      /* 未处于最小化时个别版本会抛，忽略 */
    }
    wb.resize(cur.geom.w, cur.geom.h);
    wb.move(cur.geom.x, cur.geom.y);
  };

  // 专注进出：应用 store 现几何（enterFocus/exitFocus 已写好目标几何），
  // 并同步标题栏按钮的两态文案/可读名称。
  const prevFocusedRef = useRef(false);
  useEffect(() => {
    if (prevFocusedRef.current === isFocused) return;
    prevFocusedRef.current = isFocused;
    applyStoreGeometry();
    const ctrl = focusCtrlRef.current;
    if (ctrl) describeFocusCtrl(ctrl, isFocused);
    // win.id 稳定（单例恒为 kind）；applyStoreGeometry 每次渲染重建，无需进 deps
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isFocused]);

  // 一键归位：store 已把每个窗几何重算为预设，这里把 WinBox 同步过去
  const prevResetRef = useRef(resetNonce);
  useEffect(() => {
    if (prevResetRef.current === resetNonce) return;
    prevResetRef.current = resetNonce;
    applyStoreGeometry();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetNonce]);

  // FE-16：Esc 退出专注 —— 仅本窗处于专注态时监听（§8.5：窗层负责 Esc）。
  // 跳过两种情况（§8.2 Esc 一次关一层）：焦点在已打开面板菜单内（那一下 Esc
  // 归菜单），或菜单开着但焦点不在其内（菜单自己的 Esc 处理会关菜单——
  // 此时这里不连带退出专注；medium #3）。
  useEffect(() => {
    if (!isFocused) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const target = e.target as HTMLElement | null;
      if (target?.closest?.('[role="menu"]')) return;
      if (isWindowMenuOpen()) return;
      useGameWindowStore.getState().exitFocus(win.id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isFocused, win.id]);

  if (!body) return null;
  return createPortal(<div className="h-full w-full overflow-hidden">{children}</div>, body);
}
