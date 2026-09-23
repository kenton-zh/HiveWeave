/**
 * 办公室主界面 —— 办公室升为唯一主界面（ADR-011 §D2 / 设计规格 §2.3）
 *
 * 结构（自下而上）：
 *   ① 底层   PixiJS OfficeScene 全屏铺满（懒加载，pixi.js ~1MB 不进主 chunk）
 *   ② 覆盖   HUD 工具条（面板入口 + 回工作台）
 *   ③ 浮窗   GameWindowLayer —— Chat / 组织树 / 时间线 / 目标 / 详情…
 *
 * 与旧三栏的关系：**工作台（三栏）保留为可回退形态**（过渡期）。验证稳定后
 * 按 ADR-011 迁移第 2 步把三栏退役、`react-resizable-panels` 下线。
 *
 * 注意：本组件**不实现**项目级「上班/下班」—— 该按钮留在 App header，
 * 避免在两处重复维护同一份 activate/deactivate 逻辑。
 */
import { Suspense, useEffect } from "react";
import { lazyRetry } from "../mainPanel";
import { useAppStore } from "../store";
import ErrorBoundary from "./ErrorBoundary";
import { OfficeSkeleton } from "./Skeleton";
import GameWindowLayer from "./gamewindow/GameWindowLayer";
import { useGameWindowStore, type GameWindowKind } from "./gamewindow/store";

const OfficeView = lazyRetry(() => import("./OfficeView"));

interface Props {
  onExitToWorkbench: () => void;
}

interface Entry {
  kind: GameWindowKind;
  label: string;
  needsAgent?: boolean;
  needsProject?: boolean;
  needsTask?: boolean;
}

/** HUD 的面板入口。顺序 = 使用频率（组织树/时间线是最常看的两个） */
const ENTRIES: Entry[] = [
  { kind: "org", label: "组织树" },
  { kind: "timeline", label: "时间线" },
  { kind: "goals", label: "目标", needsProject: true },
  { kind: "agent", label: "详情", needsAgent: true },
  { kind: "logs", label: "日志", needsAgent: true },
  { kind: "monitor", label: "监控", needsAgent: true },
  { kind: "token", label: "Token", needsProject: true },
  { kind: "task", label: "任务", needsTask: true },
  { kind: "debug", label: "调试" },
];

export default function OfficeWorkspace({ onExitToWorkbench }: Props) {
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);
  const selectedAgentId = useAppStore((s) => s.selectedAgentId);
  const selectedTaskId = useAppStore((s) => s.selectedTaskId);
  const openWindow = useGameWindowStore((s) => s.open);
  const closeKind = useGameWindowStore((s) => s.closeKind);

  // 选中 agent ⇒ 自动开/聚焦它的聊天窗（蓝图 §11「点电脑 → 聊天窗」）
  // 注意：open() 对已存在的窗口只发聚焦信号，不会开出第二个。
  useEffect(() => {
    if (!selectedAgentId) return;
    openWindow("chat", { agentId: selectedAgentId }, "聊天");
  }, [selectedAgentId, openWindow]);

  // 切项目 ⇒ 关掉上一项目的 agent 级窗口，避免串项目（同 v4 时间线的清理纪律）
  useEffect(() => {
    return () => {
      closeKind("chat");
      closeKind("agent");
      closeKind("logs");
      closeKind("monitor");
    };
  }, [selectedProjectId, closeKind]);

  const handleEntry = (e: Entry) => {
    if (e.kind === "agent" || e.kind === "logs" || e.kind === "monitor") {
      if (!selectedAgentId) return;
      openWindow(e.kind, { agentId: selectedAgentId }, e.label);
      return;
    }
    if (e.kind === "task") {
      if (!selectedTaskId) return;
      openWindow(e.kind, { taskId: selectedTaskId }, e.label);
      return;
    }
    openWindow(e.kind, {}, e.label);
  };

  return (
    <div className="relative flex-1 overflow-hidden bg-[#1a1d24]">
      {/* ① 底层：办公室场景铺满。OfficeView 内部 _fit 按容器尺寸缩放。
          ⚠ 必须带 Suspense —— OfficeView 是懒加载（pixi.js ~1MB），App.tsx 原用法
          就包了 Suspense fallback=<OfficeSkeleton/>；漏掉它首帧没有骨架，观感是"卡死"。 */}
      <div className="absolute inset-0">
        <ErrorBoundary label="办公室场景">
          <Suspense fallback={<OfficeSkeleton />}>
            <OfficeView />
          </Suspense>
        </ErrorBoundary>
      </div>

      {/* ② HUD 工具条 */}
      <div className="absolute top-0 left-0 right-0 z-20 flex items-center gap-1.5 px-3 py-2 bg-[#10131a]/78 backdrop-blur-[2px] border-b border-black/40">
        <span className="text-[11px] font-semibold tracking-wider text-white/55 mr-1.5 select-none">
          OFFICE
        </span>
        {ENTRIES.map((e) => {
          const disabled =
            (e.needsAgent && !selectedAgentId) ||
            (e.needsProject && !selectedProjectId) ||
            (e.needsTask && !selectedTaskId);
          return (
            <button
              key={e.kind}
              onClick={() => handleEntry(e)}
              disabled={disabled}
              className="text-[11px] px-2.5 py-1 rounded-md text-white/80 bg-white/8 hover:bg-white/16 hover:text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              title={disabled ? "需先选中对应对象" : `打开${e.label}窗口`}
            >
              {e.label}
            </button>
          );
        })}

        <button
          onClick={onExitToWorkbench}
          className="ml-auto text-[11px] px-2.5 py-1 rounded-md text-white/70 bg-white/8 hover:bg-white/16 hover:text-white transition-colors"
          title="切回三栏工作台（过渡期回退手段）"
        >
          工作台
        </button>
      </div>

      {/* ③ 窗口层（浮在最上） */}
      <GameWindowLayer />
    </div>
  );
}
