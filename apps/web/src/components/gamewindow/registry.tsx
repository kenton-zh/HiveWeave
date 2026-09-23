/**
 * 游戏窗口内容注册表 —— GameWindowKind → 面板组件
 *
 * 加载策略与 App.tsx 既有的 lazy 边界保持一致（**EXE 包体积是硬约束**，
 * 见 `docs/前端设计规格.md` §18.3）：重量级 / 低频面板保持懒加载，常驻面板静态导入。
 *
 * 面板 props 全部来自 payload + 主 store，窗口层**不额外持有业务状态**（§2.1）。
 */
import { Suspense, type ReactNode } from "react";
import { lazyRetry } from "../../mainPanel";
import ErrorBoundary from "../ErrorBoundary";
import { SkeletonList } from "../Skeleton";
import ChatPanel from "../ChatPanel";
import OrgTree from "../OrgTree";
import TokenUsagePanel from "../TokenUsagePanel";
import type { GameWindowState } from "./store";

// ⚠ 懒加载边界必须与 App.tsx 严格一致：EXE 包体积是硬约束（设计规格 §18.3），
// 不能因为"窗口可能同时打开"就改成静态导入 —— 那会把它们全塞进主 chunk。
const GoalsPanel = lazyRetry(() => import("../GoalsPanel"));
const TaskTimelinePanel = lazyRetry(() => import("../timeline/TaskTimelinePanel"));
const TimelineView = lazyRetry(() => import("../timeline/TimelineView"));
const AgentDetailPanel = lazyRetry(() => import("../AgentDetailPanel"));
const MonitorPanel = lazyRetry(() => import("../MonitorPanel"));
const WorkLogPanel = lazyRetry(() => import("../WorkLogPanel"));
const DebugPanel = lazyRetry(() => import("../DebugPanel"));

export interface GamePanelContext {
  selectedProjectId: string | null;
}

/** 缺前置条件时的占位（比裸文字更符合 §10.4 的三态体系） */
function Missing({ text }: { text: string }) {
  return (
    <div className="h-full flex items-center justify-center px-6 text-center text-sm text-g-fg-3">
      {text}
    </div>
  );
}

export function renderGamePanel(win: GameWindowState, ctx: GamePanelContext): ReactNode {
  const agentId = win.payload.agentId ?? null;
  const taskId = win.payload.taskId ?? null;

  const body = ((): ReactNode => {
    switch (win.kind) {
      case "chat":
        return agentId ? (
          <ChatPanel agentId={agentId} hidden={false} />
        ) : (
          <Missing text="请先选择一个 Agent" />
        );
      case "org":
        return <OrgTree />;
      case "timeline":
        return <TimelineView />;
      case "token":
        return ctx.selectedProjectId ? (
          <TokenUsagePanel key={ctx.selectedProjectId} projectId={ctx.selectedProjectId} />
        ) : (
          <Missing text="请先选择一个项目" />
        );
      case "goals":
        return ctx.selectedProjectId ? (
          <GoalsPanel projectId={ctx.selectedProjectId} />
        ) : (
          <Missing text="请先选择一个项目" />
        );
      case "agent":
        return agentId ? <AgentDetailPanel agentId={agentId} /> : <Missing text="请先选择一个 Agent" />;
      case "logs":
        return agentId ? <WorkLogPanel agentId={agentId} /> : <Missing text="请先选择一个 Agent" />;
      case "monitor":
        return agentId ? <MonitorPanel agentId={agentId} /> : <Missing text="请先选择一个 Agent" />;
      case "debug":
        return <DebugPanel />;
      case "task":
        return taskId ? <TaskTimelinePanel /> : <Missing text="请先选择一个任务" />;
      default:
        return <Missing text="未知面板" />;
    }
  })();

  return (
    // 每个窗口独立兜底：一个面板崩了不该带走整层窗口（对齐 App.tsx 的 ErrorBoundary 用法）
    <ErrorBoundary key={win.id} label={`窗口:${win.kind}`}>
      <Suspense
        fallback={
          <div className="p-3">
            <SkeletonList rows={6} />
          </div>
        }
      >
        {body}
      </Suspense>
    </ErrorBoundary>
  );
}
