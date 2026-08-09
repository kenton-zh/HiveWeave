import { useCallback, useEffect, useRef, useState } from "react";
import { useAppStore } from "../store";
import type { ToastType } from "../store";

const STYLES: Record<ToastType, { bg: string; border: string; icon: string; iconColor: string; iconBg: string; accent: string }> = {
  info: {
    bg: "bg-g-bg/95",
    border: "border-g-border",
    icon: "i",
    iconColor: "text-sky-700",
    iconBg: "bg-sky-100",
    accent: "bg-sky-500",
  },
  success: {
    bg: "bg-emerald-50/95",
    border: "border-emerald-200",
    icon: "✓",
    iconColor: "text-emerald-700",
    iconBg: "bg-emerald-100",
    accent: "bg-emerald-500",
  },
  error: {
    bg: "bg-red-50/95",
    border: "border-red-200",
    icon: "!",
    iconColor: "text-red-700",
    iconBg: "bg-red-100",
    accent: "bg-red-500",
  },
  warning: {
    bg: "bg-amber-50/95",
    border: "border-amber-200",
    icon: "⚠",
    iconColor: "text-amber-700",
    iconBg: "bg-amber-100",
    accent: "bg-amber-500",
  },
};

const AUTO_DISMISS_MS: Record<ToastType, number> = {
  info: 4000,
  success: 4000,
  warning: 4000,
  error: 6000,
};

const EXIT_MS = 180;

export default function ToastContainer() {
  const toasts = useAppStore((s) => s.toasts);
  const dismissToast = useAppStore((s) => s.dismissToast);
  const [leaving, setLeaving] = useState<ReadonlySet<string>>(new Set());
  const timersRef = useRef<Map<string, number>>(new Map());

  const exit = useCallback(
    (id: string) => {
      setLeaving((prev) => {
        if (prev.has(id)) return prev;
        const next = new Set(prev);
        next.add(id);
        return next;
      });
      window.setTimeout(() => {
        dismissToast(id);
        setLeaving((prev) => {
          if (!prev.has(id)) return prev;
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }, EXIT_MS);
    },
    [dismissToast],
  );

  useEffect(() => {
    for (const t of toasts) {
      if (!timersRef.current.has(t.id)) {
        timersRef.current.set(
          t.id,
          window.setTimeout(() => exit(t.id), AUTO_DISMISS_MS[t.type] ?? 4000),
        );
      }
    }
    for (const id of [...timersRef.current.keys()]) {
      if (!toasts.some((t) => t.id === id)) {
        window.clearTimeout(timersRef.current.get(id));
        timersRef.current.delete(id);
      }
    }
  }, [toasts, exit]);

  useEffect(
    () => () => {
      for (const timer of timersRef.current.values()) window.clearTimeout(timer);
      timersRef.current.clear();
    },
    [],
  );

  if (toasts.length === 0 && leaving.size === 0) return null;

  return (
    <div className="fixed top-4 right-4 z-[100] flex flex-col gap-2.5 max-w-sm w-full pointer-events-none">
      {toasts.map((t, i) => {
        const s = STYLES[t.type] ?? STYLES.info;
        const isLeaving = leaving.has(t.id);
        return (
          <div
            key={t.id}
            className={`pointer-events-auto relative overflow-hidden flex items-start gap-3 pl-4 pr-3 py-3 rounded-gmLg border ${s.bg} ${s.border} shadow-gm-pop backdrop-blur-md ${
              isLeaving ? "hw-toast-leave pointer-events-none" : "animate-slide-in-right"
            }`}
            style={{ animationDelay: isLeaving ? "0ms" : `${Math.min(i, 4) * 40}ms` }}
            role="alert"
            data-testid="toast"
            data-toast-type={t.type}
          >
            {/* Left accent bar */}
            <span className={`absolute left-0 top-0 bottom-0 w-1 ${s.accent} opacity-80`} aria-hidden="true" />
            <span
              className={`${s.iconColor} ${s.iconBg} w-6 h-6 rounded-full flex items-center justify-center font-bold text-xs leading-none mt-0.5 shrink-0 shadow-gm-sm`}
              aria-hidden="true"
            >
              {s.icon}
            </span>
            <p className="flex-1 text-sm text-g-fg leading-relaxed break-words whitespace-pre-wrap">
              {t.message}
            </p>
            <button
              onClick={() => exit(t.id)}
              className="text-g-fg-4 hover:text-g-fg hover:bg-g-bg-muted rounded-full w-6 h-6 flex items-center justify-center transition-all text-base leading-none shrink-0 active:scale-90"
              aria-label="关闭"
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}