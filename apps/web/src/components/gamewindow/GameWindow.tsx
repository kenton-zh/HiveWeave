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
 *    双向同步的抖动（拖拽过程中被 props 拽回去）。
 *
 * 3. **销毁路径只走一次**：用户点 X 时 `onclose` 返回 `true`（⚠ winbox 语义是
 *    **truthy 才中止关闭**，与直觉相反）拦下自毁、只同步 store，真正销毁由 React
 *    卸载的 cleanup `wb.close(true)` 完成；cleanup 自身放行（force ⇒ 返回 false）。
 *    反向写会二次销毁：此时 `stack_win.indexOf(this)` 已是 -1 ⇒ `splice(-1,1)`
 *    **误删栈尾的另一个窗口**，再走到 `this.unmount()` 读已置 null 的 body 抛
 *    TypeError（winbox.js:1219/1221，审计 2026-09-22 实锤）。
 * 4. **基础样式单独引入**（winbox.min.css）—— ESM 源入口不注入 CSS，见下方 import 注释。
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
// ⚠ 必须走 ESM 源入口（深路径）：winbox 的 `browser` 字段指向 UMD bundle，
// Vite 生产构建优先取它 ⇒ rollup 报 "default is not exported"。详见 types/winbox.d.ts。
import WinBox, { type WinBoxInstance } from "winbox/src/js/winbox.js";
import { useGameWindowStore, type GameWindowState } from "./store";
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

interface Props {
  win: GameWindowState;
  children: ReactNode;
}

export default function GameWindow({ win, children }: Props) {
  const [body, setBody] = useState<HTMLElement | null>(null);
  const wbRef = useRef<WinBoxInstance | null>(null);
  // 只订阅自己这一条聚焦信号（避免整个窗口层因任一窗口聚焦而重渲染）
  const focusNonce = useGameWindowStore((s) => s.focusSignal[win.id] ?? 0);

  useEffect(() => {
    // 最小化时 winbox 会调 resize(...,true)+move(...,true) 把窗口压成底部细条，
    // 虽带 _skip_update（不写内部值）但**仍触发 onresize/onmove 回调** ⇒ 那些几何
    // 不能进 store，否则持久化后重开同类窗口只剩底部一条（winbox.js:525-533）。
    // 判据用几何值而非 timing：窗口 minheight=220，任何 h<=60 的回调必是最小化态；
    // 且 resize 先于 move 触发，故这个 flag 能顺势挡住随后的 move。
    let minimizedLike = false;

    const wb = new WinBox({
      title: win.title,
      class: "hw-win no-full",
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
    });
    wbRef.current = wb;
    setBody(wb.body);
    // onfocus 不接：WinBox 自身点击置顶已足够，接上会与 focusSignal 形成回环

    return () => {
      wbRef.current = null;
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

  if (!body) return null;
  return createPortal(<div className="h-full w-full overflow-hidden">{children}</div>, body);
}
