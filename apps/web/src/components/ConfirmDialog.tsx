import { useId } from "react";
import Modal from "./Modal";

interface Props {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Test-friendly confirmation dialog — replaces native window.confirm()
 * so browser automation tools can see and interact with the buttons.
 * 焦点陷阱 / Escape / 遮罩关闭由 Modal 统一提供。
 */
export default function ConfirmDialog({
  title,
  message,
  confirmLabel = "确定",
  cancelLabel = "取消",
  danger = false,
  onConfirm,
  onCancel,
}: Props) {
  const titleId = useId();
  return (
    <Modal onClose={onCancel} labelledBy={titleId} overlayClassName="z-[60]" className="w-full max-w-md mx-4 p-6">
      {/* Header */}
      <div className="flex items-center gap-3 mb-4">
        {danger && (
          <div className="w-8 h-8 rounded-full bg-g-red-bg ring-1 ring-g-red flex items-center justify-center shrink-0">
            <svg className="w-5 h-5 text-g-red" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" />
            </svg>
          </div>
        )}
        <h3 id={titleId} className="text-base font-semibold text-g-fg">{title}</h3>
      </div>

      <p className="text-sm text-g-fg-3 mb-6 leading-relaxed">{message}</p>

      <div className="flex items-center justify-end gap-3">
        <button
          onClick={onCancel}
          className="px-4 py-2 text-sm text-g-fg-3 hover:text-g-fg border border-g-border rounded-gm hover:bg-g-bg-muted hover:border-g-border-strong active:scale-[0.97] transition-all"
        >
          {cancelLabel}
        </button>
        <button
          onClick={onConfirm}
          className={`px-4 py-2 text-sm text-white rounded-gm shadow-gm-sm active:scale-[0.97] transition-all ${
            danger
              ? "bg-g-red hover:bg-g-red-vivid"
              : "bg-g-blue text-white hover:bg-g-blue"
          }`}
        >
          {confirmLabel}
        </button>
      </div>
    </Modal>
  );
}
