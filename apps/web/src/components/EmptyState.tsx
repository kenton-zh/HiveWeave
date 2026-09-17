import type { ReactNode } from "react";

interface EmptyStateProps {
  /** lucide 等线性图标（20px 档），不用 emoji —— 跨平台字形不一致且与
   * 克制调性冲突（P2-1）。 */
  icon?: ReactNode;
  title: string;
  description?: string;
  /** 关键：可点的引导动作 —— 「暂无」之后用户该做什么（10 处空状态
   * 此前 0 处有 CTA，P2-1）。 */
  action?: { label: string; onClick: () => void };
}

/** 一级面板统一空状态（P2-1）：图标 + 标题 + 副说明 + 引导动作。
 * 10 处各写各的（灰字一行 / 三行 emoji）由此收敛。 */
export default function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div
      data-testid="empty-state"
      className="h-full flex flex-col items-center justify-center gap-1.5 py-10 text-center animate-fade-in"
    >
      {icon ? (
        <div className="text-g-fg-4 mb-1 [&>svg]:w-7 [&>svg]:h-7">{icon}</div>
      ) : null}
      <div className="text-sm font-medium text-g-fg-2">{title}</div>
      {description ? (
        <div className="text-xs text-g-fg-3 max-w-[36ch]">{description}</div>
      ) : null}
      {action ? (
        <button
          type="button"
          onClick={action.onClick}
          className="mt-2.5 px-3 py-1.5 text-xs font-medium rounded-gm border border-g-border
                     bg-g-bg text-g-fg-2 shadow-gm-sm hover:text-g-fg hover:border-g-border-strong
                     hover:shadow-gm active:scale-[0.97] transition-all"
        >
          {action.label}
        </button>
      ) : null}
    </div>
  );
}
