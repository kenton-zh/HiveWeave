import { motion } from "motion/react";
import type { ReactNode } from "react";

/**
 * 分段页签按钮 + 滑动指示条（2026-10-01 动效 · motion layoutId 共享布局动画）。
 *
 * 用法契约：
 * - 同一页签组内的所有按钮必须传**同一个** `pillId` —— layoutId 相同，
 *   切换时白色 pill 才会从旧按钮「滑」到新按钮（跨组件共享投影）。
 * - 两个独立页签栏（左栏视图 / 右栏面板）必须用**不同** pillId，
 *   否则两栏会跨栏串联动画。
 * - 按钮底色仍由 `.hw-segment-item-active` 提供（motion 失效时的兜底），
 *   pill 叠在其上负责滑动；文字层 `relative z-10` 保证 pill 从下方滑过、
 *   不遮字。
 */
export function SegmentItem({
  active,
  pillId,
  onClick,
  disabled,
  children,
}: {
  active: boolean;
  pillId: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`hw-segment-item ${active ? "hw-segment-item-active" : ""}`}
    >
      {active && (
        <motion.span
          layoutId={pillId}
          // 只负责白底滑动；描边/阴影归 .hw-segment-item-active（按钮自带
          // 1px border 在 inset-0 内含块之外）——pill 若也带 border/shadow
          // 会叠成双边框双阴影（审计 M-3）
          className="absolute inset-0 rounded-gm bg-white"
          transition={{ type: "spring", stiffness: 480, damping: 38 }}
          aria-hidden="true"
        />
      )}
      <span className="relative z-10">{children}</span>
    </button>
  );
}
