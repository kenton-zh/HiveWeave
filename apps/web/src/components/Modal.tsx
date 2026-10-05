import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cn } from "../lib/utils";

/**
 * 共享模态原语（2026-10-05 无障碍加固）。
 *
 * 一次补齐所有弹窗长期缺失的 dialog 语义与键盘行为（Audit P1）：
 * - role="dialog" + aria-modal + aria-labelledby/aria-label；
 * - 焦点陷阱（Tab/Shift+Tab 在面板内循环）+ 挂载聚焦 + 卸载还原；
 * - Escape 关闭、点遮罩关闭。
 *
 * 走 body portal：祖先若有 transform/filter，fixed 遮罩会被困在祖先盒内
 * （同 ImageLightbox.tsx 的成因），portal 规避。
 *
 * 调用方本就按需挂载（`{open && <X/>}`），故本组件默认「挂载即打开」。
 * 类名经 `cn` 合并，调用方可覆盖 z-index / 背景 / 面板宽度。
 */
const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

// 模块级模态栈：多个 Modal 叠加时（如 SettingsPanel 内嵌 ConfirmDialog、
// ModelConfigPage 内嵌子对话框），只有最上层响应 Escape/Tab。否则每个 Modal
// 各自在 window 上监听 —— stopPropagation 挡不住**同一 target** 上的兄弟监听，
// 一次 Esc 会把整叠全关掉。
const MODAL_STACK: symbol[] = [];

export default function Modal({
  onClose,
  labelledBy,
  ariaLabel,
  className,
  overlayClassName,
  overlayTestId,
  children,
}: {
  onClose?: () => void;
  /** 标题元素 id（推荐）；与 ariaLabel 二选一。 */
  labelledBy?: string;
  ariaLabel?: string;
  /** 面板类名（宽度/内边距等）。 */
  className?: string;
  /** 遮罩类名（z-index / 背景 / 布局对齐）。 */
  overlayClassName?: string;
  overlayTestId?: string;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const [modalId] = useState(() => Symbol("modal"));

  // 入栈/出栈（栈顶 = 最上层模态）
  useEffect(() => {
    MODAL_STACK.push(modalId);
    return () => {
      const i = MODAL_STACK.lastIndexOf(modalId);
      if (i >= 0) MODAL_STACK.splice(i, 1);
    };
  }, [modalId]);

  // 挂载聚焦 + 卸载还原焦点到打开者
  useEffect(() => {
    restoreRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = panelRef.current;
    const first = panel?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? panel)?.focus();
    return () => {
      if (restoreRef.current?.isConnected) restoreRef.current.focus();
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (MODAL_STACK[MODAL_STACK.length - 1] !== modalId) return; // 只处理最上层
      if (e.defaultPrevented) return; // 内层控件已处理（如地址栏 Esc 还原）
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose?.();
        return;
      }
      if (e.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      if (items.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === panel)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    // 冒泡相（非 capture）：让输入框等内层控件的 Escape 有机会先处理并
    // stopPropagation，从而不误关整个模态。
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, modalId]);

  return createPortal(
    <div
      data-testid={overlayTestId}
      className={cn(
        "fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-[2px] animate-fade-in",
        overlayClassName,
      )}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose?.();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        aria-label={labelledBy ? undefined : ariaLabel}
        tabIndex={-1}
        className={cn(
          "bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg outline-none animate-scale-in",
          className,
        )}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
