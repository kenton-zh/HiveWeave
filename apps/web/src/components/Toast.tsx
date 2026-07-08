import { useAppStore } from "../store";
import type { ToastType } from "../store";

const STYLES: Record<ToastType, { bg: string; border: string; icon: string; iconColor: string }> = {
  info: {
    bg: "bg-g-bg",
    border: "border-g-border shadow-gm-sm-lg",
    icon: "i",
    iconColor: "text-sky-700",
  },
  success: {
    bg: "bg-emerald-50",
    border: "border-emerald-200",
    icon: "✓",
    iconColor: "text-emerald-700",
  },
  error: {
    bg: "bg-red-50",
    border: "border-red-200",
    icon: "!",
    iconColor: "text-red-700",
  },
  warning: {
    bg: "bg-amber-50",
    border: "border-amber-200",
    icon: "⚠",
    iconColor: "text-amber-700",
  },
};

export default function ToastContainer() {
  const toasts = useAppStore((s) => s.toasts);
  const dismissToast = useAppStore((s) => s.dismissToast);

  if (toasts.length === 0) return null;

  return (
    <div className="fixed top-4 right-4 z-[100] flex flex-col gap-2 max-w-sm w-full pointer-events-none">
      {toasts.map((t) => {
        const s = STYLES[t.type] ?? STYLES.info;
        return (
          <div
            key={t.id}
            className={`pointer-events-auto flex items-start gap-3 px-4 py-3 rounded-lg border ${s.bg} ${s.border} shadow-xl backdrop-blur-sm animate-[slideIn_0.2s_ease-out]`}
            role="alert"
            data-testid="toast"
            data-toast-type={t.type}
          >
            <span className={`${s.iconColor} font-bold text-base leading-tight mt-0.5`}>
              {s.icon}
            </span>
            <p className="flex-1 text-sm text-g-fg leading-relaxed break-words whitespace-pre-wrap">
              {t.message}
            </p>
            <button
              onClick={() => dismissToast(t.id)}
              className="text-g-fg-4 hover:text-g-fg transition-colors text-lg leading-none -mt-0.5"
              aria-label="关闭"
            >
              ×
            </button>
          </div>
        );
      })}
      <style>{`
        @keyframes slideIn {
          from { opacity: 0; transform: translateX(20px); }
          to { opacity: 1; transform: translateX(0); }
        }
      `}</style>
    </div>
  );
}
