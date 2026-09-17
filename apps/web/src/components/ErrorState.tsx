import type { ReactNode } from "react";

interface ErrorStateProps {
  title?: string;
  /** 失败详情（err.message 等）；原文展示，不参与任何判定。 */
  detail?: string;
  onRetry?: () => void;
  retryLabel?: string;
  /** 额外动作（如「查看模型配置」） */
  extra?: ReactNode;
}

/** P2-3 统一错误态：图标 + 标题 + 详情 + 重试。替代散落的裸红块。 */
export default function ErrorState({
  title = "加载失败",
  detail,
  onRetry,
  retryLabel = "重试",
  extra,
}: ErrorStateProps) {
  return (
    <div
      role="alert"
      data-testid="error-state"
      className="h-full flex flex-col items-center justify-center gap-2 py-10 text-center"
    >
      <div className="w-9 h-9 rounded-full bg-g-red-bg text-g-red flex items-center justify-center text-lg">
        !
      </div>
      <div className="text-sm font-medium text-g-fg-2">{title}</div>
      {detail ? (
        <div className="text-xs text-g-fg-3 max-w-[48ch] break-all">{detail}</div>
      ) : null}
      <div className="flex items-center gap-2 mt-1.5">
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="px-3 py-1.5 text-xs font-medium rounded-gm bg-g-blue text-white
                       shadow-gm-sm hover:brightness-110 active:scale-[0.97] transition-all"
          >
            {retryLabel}
          </button>
        ) : null}
        {extra}
      </div>
    </div>
  );
}
