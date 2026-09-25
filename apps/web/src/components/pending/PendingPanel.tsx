/**
 * FE-14「待我处理」聚合面板（设计方案 §11.1 / 前端设计规格 §15.3）
 *
 * 形态裁决：**内嵌浮层面板**（OfficeWorkspace 本地 state 控制开合），不注册
 * gamewindow kind —— 窗口注册表（gamewindow/registry+store）不在本改动名下，
 * 且该面板是 HUD 抽屉性质（随 HUD 常驻计数徽标），不需要游戏窗的拖拽/最小化。
 *
 * 联动程度（§11.1「主要操作」）：
 *   点成员     → openAgentChat(agentId)（navigation/commands 显式命令）
 *   点任务     → openTask(taskId)
 *   待授权条目 → 「去审批」直接复用 ApprovalDialog（同 OrgTree/ChatPanel 的
 *                用法），面板内即可批准/拒绝 —— 不是只给指路文案。
 *   待回答条目 → openAgentChat 进入成员聊天；回答弹窗由 App 层全局
 *                QuestionDialog 承载（§11.2 交互契约），本面板不重复实现。
 *
 * 状态矩阵（§15.3）：首载骨架；后台刷新保留旧数据；网络错误按组显示
 * 错误+重试（不伪装成空列表）；空态给原因与下一步。
 *
 * Esc 不关本面板（有意）：面板是 HUD 常驻抽屉而非模态，且 QuestionDialog
 * 等全局模态的可见性不在任何 store 里，无法可靠实现「只关最上层」
 * （§8.2）—— 关闭走 HUD 按钮/标题叉，避免 Esc 误伤底下的面板。
 */
import { useEffect, useState } from "react";
import ApprovalDialog from "../ApprovalDialog";
import EmptyState from "../EmptyState";
import { SkeletonList } from "../Skeleton";
import { agentDisplayName } from "../../navigation/agentNames";
import { openAgentChat, openTask } from "../../navigation/commands";
import { useAppStore } from "../../store";
import { formatWait, GROUP_META, type PendingGroupId, type PendingGroupView, type PendingItem } from "./model";
import { usePendingData } from "./usePendingData";

type Filter = "all" | PendingGroupId;

interface Props {
  open: boolean;
  onClose: () => void;
  /** 徽标数据回传：OfficeWorkspace 用它显示 HUD 未处理总数 */
  onTotalChange?: (total: number) => void;
}

function actorLabel(actorId?: string): string | null {
  if (!actorId) return null;
  return agentDisplayName(actorId) ?? actorId;
}

function PendingRow({
  item,
  now,
  onApprove,
}: {
  item: PendingItem;
  now: number;
  onApprove: (agentId: string) => void;
}) {
  const actorId = item.actorId;
  const taskId = item.taskId;
  const name = actorLabel(actorId);
  const wait = typeof item.since === "number" ? formatWait(now - item.since) : null;
  const canApprove = item.groupId === "approvals" && typeof actorId === "string";
  return (
    <div
      data-testid="pending-item"
      className="flex items-start gap-2 px-3 py-2.5 border-b border-g-border/50 last:border-b-0 hover:bg-g-bg-soft/60 transition-colors"
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 text-xs min-w-0">
          {actorId && name ? (
            <button
              type="button"
              data-testid="pending-item-actor"
              onClick={() => openAgentChat(actorId)}
              className="font-medium text-g-fg hover:text-g-blue truncate max-w-[9rem] transition-colors"
              title={`打开 ${name} 的聊天窗`}
            >
              {name}
            </button>
          ) : null}
          <span className="text-g-fg-4 shrink-0">{wait ? `等待 ${wait}` : "进行中"}</span>
        </div>
        <div className="text-sm text-g-fg mt-0.5 break-words" title={item.summary}>
          {item.summary}
        </div>
        {item.note ? <div className="text-xs text-g-fg-3 mt-0.5 break-words">{item.note}</div> : null}
      </div>
      <div className="flex items-center gap-1.5 shrink-0 pt-0.5">
        {taskId ? (
          <button
            type="button"
            data-testid="pending-item-task"
            onClick={() => openTask(taskId)}
            className="px-2 py-1 text-xs rounded-gm border border-g-border bg-g-bg text-g-fg-2 hover:text-g-fg hover:border-g-border-strong active:scale-[0.97] transition-all"
            title="打开任务窗查看详情"
          >
            查看任务
          </button>
        ) : null}
        {canApprove ? (
          <button
            type="button"
            data-testid="pending-item-approve"
            onClick={() => onApprove(actorId as string)}
            className="px-2 py-1 text-xs rounded-gm bg-g-blue text-white hover:brightness-110 active:scale-[0.97] transition-all"
            title={`审批 ${name ?? actorId} 的权限请求（批准 / 拒绝）`}
          >
            去审批
          </button>
        ) : null}
      </div>
    </div>
  );
}

function GroupSection({
  group,
  failed,
  now,
  onApprove,
  onRetry,
}: {
  group: PendingGroupView;
  failed: boolean;
  now: number;
  onApprove: (agentId: string) => void;
  onRetry: () => void;
}) {
  return (
    <section data-testid={`pending-group-${group.id}`} className="border-b border-g-border last:border-b-0">
      <header className="flex items-center gap-2 px-3 pt-2.5 pb-1.5">
        <h4 className="text-xs font-semibold text-g-fg-2">{group.label}</h4>
        <span
          data-testid={`pending-count-${group.id}`}
          className={`min-w-[1.25rem] text-center px-1 py-px text-[11px] rounded-full ${
            group.items.length > 0
              ? "bg-g-blue-bg text-g-blue font-medium"
              : "bg-g-bg-muted text-g-fg-4"
          }`}
        >
          {group.items.length}
        </span>
        {failed ? (
          <span className="ml-auto text-[11px] text-g-red-vivid">部分加载失败</span>
        ) : null}
      </header>
      {failed ? (
        <div className="mx-3 mb-2.5 px-2.5 py-2 rounded-gm bg-g-red-bg border border-g-red/30 flex items-center gap-2">
          <span className="text-xs text-g-red flex-1">该组数据加载失败，当前显示可能不完整</span>
          <button
            type="button"
            data-testid={`pending-retry-${group.id}`}
            onClick={onRetry}
            className="px-2 py-0.5 text-xs rounded-gm border border-g-red/40 text-g-red hover:bg-g-red/10 transition-colors"
          >
            重试
          </button>
        </div>
      ) : group.items.length === 0 ? (
        <p className="px-3 pb-2.5 text-xs text-g-fg-4">
          {group.emptyTitle} —— {group.emptyDescription}
        </p>
      ) : (
        group.items.map((item) => <PendingRow key={item.key} item={item} now={now} onApprove={onApprove} />)
      )}
    </section>
  );
}

export default function PendingPanel({ open, onClose, onTotalChange }: Props) {
  const { projectId, loading, errors, groups, countByGroup, total, now, refresh } = usePendingData();
  const [filter, setFilter] = useState<Filter>("all");
  const [approvalAgentId, setApprovalAgentId] = useState<string | null>(null);

  const projectName = useAppStore(
    (s) => s.projects.find((p) => p.id === s.selectedProjectId)?.name,
  );

  useEffect(() => {
    onTotalChange?.(total);
  }, [total, onTotalChange]);

  // 审批弹窗复用 ApprovalDialog；关闭后立即补一轮刷新（计数归位）
  const approvalDialog = approvalAgentId ? (
    <ApprovalDialog
      agentId={approvalAgentId}
      onClose={() => {
        setApprovalAgentId(null);
        refresh();
      }}
    />
  ) : null;

  if (!open) return approvalDialog;

  const anyFailed = errors.questions || errors.approvals || errors.acceptance || errors.attention;
  const visibleGroups = filter === "all" ? groups : groups.filter((g) => g.id === filter);

  return (
    <>
      {approvalDialog}
      <aside
        data-testid="pending-panel"
        className="absolute left-3 top-11 z-20 w-[380px] max-w-[calc(100%-1.5rem)] max-h-[calc(100%-3.5rem)] flex flex-col rounded-gmLg border border-g-win-border bg-g-win-body-solid shadow-gm-window animate-slide-up overflow-hidden"
        aria-label="待我处理"
      >
        <header className="flex items-center gap-2 px-3 py-2.5 border-b border-g-win-border bg-g-win-header text-g-win-header-fg shrink-0">
          <h3 className="text-sm font-semibold">待我处理</h3>
          <span className="text-[11px] text-g-win-header-fg/70 truncate" title={projectName ?? undefined}>
            {projectName ? `当前项目 · ${projectName}` : "当前项目"}
          </span>
          <button
            type="button"
            data-testid="pending-panel-close"
            onClick={onClose}
            aria-label="收起待我处理面板"
            className="ml-auto p-1 rounded-gm text-g-win-header-fg/70 hover:text-g-win-header-fg hover:bg-g-win-control-hover transition-colors"
            title="收起面板（不提交任何业务操作）"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </header>

        {/* 组筛选（FE-14：计数、筛选、跳转、恢复一致） */}
        <div className="flex items-center gap-1.5 px-3 py-2 border-b border-g-border shrink-0" role="group" aria-label="按分组筛选">
          {(["all", "questions", "approvals", "acceptance", "attention"] as const).map((id) => {
            const label =
              id === "all"
                ? "全部"
                : GROUP_META.find((m) => m.id === id)?.label ?? id;
            const count = id === "all" ? total : countByGroup[id];
            const active = filter === id;
            return (
              <button
                key={id}
                type="button"
                data-testid={`pending-filter-${id}`}
                onClick={() => setFilter(id)}
                className={`px-2 py-1 text-xs rounded-gm border transition-colors ${
                  active
                    ? "border-g-blue/50 bg-g-blue-bg text-g-blue font-medium"
                    : "border-g-border bg-g-bg text-g-fg-3 hover:text-g-fg hover:border-g-border-strong"
                }`}
              >
                {label}
                {count > 0 ? <span className="ml-1 font-medium">{count}</span> : null}
              </button>
            );
          })}
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto">
          {!projectId ? (
            <EmptyState
              title="未选择项目"
              description="选择一个项目后，这里会聚合该项目内等待你处理的事项"
            />
          ) : loading ? (
            <SkeletonList rows={4} />
          ) : total === 0 && !anyFailed ? (
            <EmptyState
              title="没有等待你处理的事项"
              description="成员提问、权限请求、验收任务与异常提醒会按分组出现在这里"
            />
          ) : (
            visibleGroups.map((g) => (
              <GroupSection
                key={g.id}
                group={g}
                failed={errors[g.id]}
                now={now}
                onApprove={setApprovalAgentId}
                onRetry={refresh}
              />
            ))
          )}
        </div>
      </aside>
    </>
  );
}
