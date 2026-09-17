/** P2-2 统一骨架件：同构占位（与最终布局同形），替代一行灰字 Loading。 */

export function SkeletonLine({ className = "" }: { className?: string }) {
  return (
    <div className={`rounded-gm bg-g-bg-muted/80 animate-pulse-soft ${className}`} />
  );
}

/** 列表型骨架（WorkLog / Monitor / 通用右栏内容）。 */
export function SkeletonList({ rows = 5 }: { rows?: number }) {
  return (
    <div className="h-full flex flex-col gap-2.5 p-4" data-testid="skeleton-list">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3">
          <SkeletonLine className="w-7 h-7 shrink-0 !rounded-full" />
          <div className="flex-1 flex flex-col gap-1.5">
            <SkeletonLine className={`h-3 ${i % 2 ? "w-4/5" : "w-[92%]"}`} />
            <SkeletonLine className="h-2.5 w-1/2" />
          </div>
        </div>
      ))}
    </div>
  );
}

/** Office 视图冷启动骨架：与最终 canvas 布局同构（地板色块 + 半透明小人
 * 占位）—— pixi 资源加载期间观感是「加载中」而非「卡死」。 */
export function OfficeSkeleton() {
  return (
    <div className="h-full w-full relative overflow-hidden" data-testid="office-skeleton">
      {/* 地板 */}
      <div className="absolute inset-x-0 bottom-0 h-2/5 bg-g-bg-muted animate-pulse-soft" />
      {/* 后墙 */}
      <div className="absolute inset-x-0 top-0 h-3/5 bg-g-bg-soft" />
      {/* 半透明小人占位（与座位标定近似分布） */}
      {[18, 38, 58, 78].map((x, i) => (
        <div
          key={x}
          className="absolute bottom-[16%] w-8 h-12 rounded-t-full bg-g-fg-4/40 animate-pulse-soft"
          style={{ left: `${x}%`, animationDelay: `${i * 0.3}s` }}
        />
      ))}
      {/* 桌面色块 */}
      {[10, 45, 80].map((x) => (
        <div
          key={x}
          className="absolute bottom-[30%] w-16 h-2 rounded-gm bg-g-border animate-pulse-soft"
          style={{ left: `${x}%` }}
        />
      ))}
    </div>
  );
}
