/**
 * 窗口管理浮动控件（FE-16 · §8.5 规则 3/4 + §6.2「窗口状态要可见」）
 *
 * 功能：
 *   ① 已打开面板菜单 —— 列出已开窗口（标题=种类·对象 / 最小化 / 固定状态），
 *      点击行 = 聚焦/恢复（requestFocus：最小化窗 restore+置顶）；
 *   ② 一键归位 —— resetLayout：清几何记忆并重落预设，业务状态不动；
 *   ③ 关闭全部窗口（closeAll —— store 既有能力，此前零调用方）。
 *
 * 挂载点：GameWindowLayer 内的角落浮动控件（左下）。⚠ 不挂 OfficeWorkspace HUD
 * （该文件归另一工作流）——若主会话决定挪去 HUD，把 <WindowMenuControl/> 从
 * GameWindowLayer 移走、在 HUD 里原样挂载即可（组件自包含，无外部依赖）。
 *
 * 订阅纪律（FE-18 §16.3）：外层按钮只订阅 `windows.length`（数字，拖拽不改它 ⇒
 * 拖拽不重渲染本控件）；窗口清单只在弹层打开时订阅（弹层挂载才有订阅）。
 */
import { useEffect, useRef, useState, type RefObject } from "react";
import { useGameWindowStore } from "./store";

function WindowMenuPopover({
  onClose,
  wrapperRef,
}: {
  onClose: () => void;
  /** 外层容器（含触发按钮）——点外判定用它，否则「点按钮关菜单」会被 click toggle 重新打开 */
  wrapperRef: RefObject<HTMLDivElement | null>;
}) {
  // 弹层打开期间才订阅窗口清单（小列表，拖拽期间重渲染可接受）
  const windows = useGameWindowStore((s) => s.windows);
  const pinnedAgent = useGameWindowStore((s) => s.pinnedAgent);

  // 点外面关 + Esc 关（监听与清理成对 —— FE-18 生命周期纪律）
  useEffect(() => {
    const onPointerDown = (e: PointerEvent) => {
      const el = wrapperRef.current;
      if (el && !el.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose, wrapperRef]);

  return (
    <div
      className="absolute bottom-11 left-3 z-10 w-64 rounded-gmLg border border-g-border bg-g-bg/95 p-1.5 shadow-gm-pop backdrop-blur-sm"
      role="menu"
      aria-label="已打开面板"
    >
      {windows.length === 0 ? (
        <div className="px-2.5 py-2 text-[11px] text-g-fg-3">没有打开的面板</div>
      ) : (
        windows.map((w) => {
          const pinned = pinnedAgent[w.kind] != null;
          return (
            <button
              key={w.id}
              role="menuitem"
              onClick={() => {
                useGameWindowStore.getState().requestFocus(w.id);
                onClose();
              }}
              className="flex w-full items-center gap-2 rounded-gm px-2.5 py-1.5 text-left text-[11px] text-g-fg hover:bg-g-bg-muted active:bg-g-bg-muted"
              title="点击聚焦/恢复该面板"
            >
              <span className="truncate">{w.title}</span>
              <span className="ml-auto flex shrink-0 items-center gap-1">
                {pinned && (
                  <span className="rounded-gmSm border border-g-blue/40 bg-g-blue-bg px-1.5 py-0.5 text-[10px] text-g-blue">
                    已固定
                  </span>
                )}
                {w.minimized && (
                  <span className="rounded-gmSm border border-g-border bg-g-bg-soft px-1.5 py-0.5 text-[10px] text-g-fg-3">
                    最小化
                  </span>
                )}
              </span>
            </button>
          );
        })
      )}
      <div className="my-1 border-t border-g-border" />
      <button
        role="menuitem"
        onClick={() => {
          useGameWindowStore.getState().resetLayout();
          onClose();
        }}
        className="w-full rounded-gm px-2.5 py-1.5 text-left text-[11px] text-g-fg hover:bg-g-bg-muted active:bg-g-bg-muted"
        title="所有窗口回到默认位置（不清聊天/队列/业务状态）"
      >
        一键归位
      </button>
      <button
        role="menuitem"
        onClick={() => {
          useGameWindowStore.getState().closeAll();
          onClose();
        }}
        className="w-full rounded-gm px-2.5 py-1.5 text-left text-[11px] text-g-fg-3 hover:bg-g-bg-muted hover:text-g-fg active:bg-g-bg-muted"
        title="关闭全部窗口（后台运行与队列不受影响）"
      >
        关闭全部窗口
      </button>
    </div>
  );
}

// 菜单开着时置 true —— GameWindow 的专注 Esc 用它做「一次关一层」判定：
// 菜单打开但焦点不在 menu 元素内（点在 body 等处）时，那一下 Esc 归菜单，
// 不连带退出专注（§8.2；批2-4 集成审计 medium #3）。
let menuOpen = false;
export function isWindowMenuOpen(): boolean {
  return menuOpen;
}

export default function WindowMenuControl() {
  const [open, setOpen] = useState(false);
  // 数字订阅：只随「窗口个数」变化重渲染，拖拽移动窗口不触发（FE-18）
  const count = useGameWindowStore((s) => s.windows.length);
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    menuOpen = open;
    return () => {
      menuOpen = false;
    };
  }, [open]);

  return (
    // z-[400]：winbox 窗口直接挂在 document.body，z 从 100 起每次聚焦 +1
    // （winbox.js:933 index_counter），必须压过窗口才不会被专注/停靠窗盖住。
    <div ref={wrapperRef} className="absolute bottom-3 left-3 z-[400]">
      {open && (
        <WindowMenuPopover wrapperRef={wrapperRef} onClose={() => setOpen(false)} />
      )}
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
        className="flex items-center gap-1.5 rounded-full border border-white/20 bg-black/55 px-3 py-1.5 text-[11px] text-white/85 shadow-gm-sm backdrop-blur-sm transition-colors hover:bg-black/75 hover:text-white"
        title="已打开面板 / 一键归位"
      >
        <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
          {/* 窗口叠层图形 */}
          <rect x="3" y="7" width="14" height="12" rx="2" />
          <path d="M8 4h11a2 2 0 0 1 2 2v10" strokeLinecap="round" />
        </svg>
        面板{count > 0 ? ` ${count}` : ""}
      </button>
    </div>
  );
}
