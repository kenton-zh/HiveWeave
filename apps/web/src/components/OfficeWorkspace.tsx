/**
 * 办公室主界面 —— 办公室升为唯一主界面（ADR-011 §D2 / 设计规格 §2.3）
 *
 * 结构（自下而上）：
 *   ① 底层   PixiJS OfficeScene 全屏铺满（懒加载，pixi.js ~1MB 不进主 chunk）
 *   ② 覆盖   HUD 工具条（面板入口 + 待我处理 + 回工作台）
 *   ③ 浮层   待我处理聚合面板（FE-14，内嵌抽屉，开合由本组件本地 state 管）
 *   ④ 浮窗   GameWindowLayer —— Chat / 组织树 / 时间线 / 目标 / 详情…
 *
 * 与旧三栏的关系：**工作台（三栏）保留为可回退形态**（过渡期）。验证稳定后
 * 按 ADR-011 迁移第 2 步把三栏退役、`react-resizable-panels` 下线。
 *
 * 注意：本组件**不实现**项目级「上班/下班」—— 该按钮留在 App header，
 * 避免在两处重复维护同一份 activate/deactivate 逻辑。
 */
import { Suspense, useEffect, useState } from "react";
import { lazyRetry } from "../mainPanel";
import { useAppStore } from "../store";
import {
  followAgentDetail,
  openAgentChat,
  openAgentDetailWindow,
  setOfficeSurfaceActive,
} from "../navigation/commands";
import ErrorBoundary from "./ErrorBoundary";
import { OfficeSkeleton } from "./Skeleton";
import GameWindowLayer from "./gamewindow/GameWindowLayer";
import { useGameWindowStore, type GameWindowKind } from "./gamewindow/store";
import PendingPanel from "./pending/PendingPanel";

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
  // FE-14：待我处理面板开合 + HUD 徽标计数（数据轮询在 PendingPanel 内部）
  const [pendingOpen, setPendingOpen] = useState(false);
  const [pendingTotal, setPendingTotal] = useState(0);

  // 窗口层在位信号：workspaceMode==="office" 不在任何 store 里（App 本地
  // state），由挂载/卸载打点给导航命令（openTask 等据此判断「开了窗有没有层渲染」）
  useEffect(() => {
    setOfficeSurfaceActive(true);
    return () => setOfficeSurfaceActive(false);
  }, []);

  // FE-01（UX-01）：选中成员 ⇒ 显式「打开/聚焦聊天窗」命令。
  // 旧实现把开窗挂在 selectedAgentId **变化**上 —— 同一个人重复点击不产生
  // 状态变化，「点甲→关聊天→再点甲」就像失效了。命令化后语义固定为：
  // 已开⇒聚焦 / 最小化⇒恢复置顶 / 已关⇒重开 / 换人⇒同窗换内容。
  // （同 ID 重复点击的显式命令由 OfficeView 场景点击直接发出 —— 那里没有
  // 状态变化可等，只有显式调用才能触发。）
  useEffect(() => {
    if (!selectedAgentId) return;
    openAgentChat(selectedAgentId);
  }, [selectedAgentId, openAgentChat]);

  // FE-07（UX-02）：详情窗默认**跟随**当前选中成员（未固定时；窗未开则不开，
  // 打开走 HUD 显式入口）。固定中由 followAgentDetail 内部拒绝覆盖。
  useEffect(() => {
    if (!selectedAgentId) return;
    followAgentDetail(selectedAgentId);
  }, [selectedAgentId, followAgentDetail]);

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
    if (e.kind === "agent") {
      if (!selectedAgentId) return;
      // FE-07：详情入口走命令 —— 固定期只聚焦、不覆盖内容（否则
      // 「固定乙→选中甲→点详情」会静默把窗换成甲，正是 UX-02 要消灭的混淆）
      openAgentDetailWindow(selectedAgentId);
      return;
    }
    if (e.kind === "logs" || e.kind === "monitor") {
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

      {/* ② HUD 工具条。FE-11：底盘用 g-hud 令牌（替代裸值 bg-[#10131a]/78） */}
      <div className="absolute top-0 left-0 right-0 z-20 flex items-center gap-1.5 px-3 py-2 bg-g-hud backdrop-blur-[2px] border-b border-black/40">
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

        {/* FE-14：待我处理聚合入口（徽标 = 未处理总数，0 时不显示） */}
        <button
          onClick={() => setPendingOpen((v) => !v)}
          className={`ml-auto flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-md transition-colors ${
            pendingOpen
              ? "text-white bg-white/16"
              : "text-white/80 bg-white/8 hover:bg-white/16 hover:text-white"
          }`}
          title="查看所有等待你处理的事项（提问 / 授权 / 验收 / 提醒）"
        >
          待我处理
          {pendingTotal > 0 && (
            <span
              data-testid="pending-hud-badge"
              className="min-w-[1.1rem] px-1 py-px text-center text-[10px] leading-4 font-semibold rounded-full bg-g-red text-white"
            >
              {pendingTotal > 99 ? "99+" : pendingTotal}
            </span>
          )}
        </button>

        <button
          onClick={onExitToWorkbench}
          className="text-[11px] px-2.5 py-1 rounded-md text-white/70 bg-white/8 hover:bg-white/16 hover:text-white transition-colors"
          title="切回三栏工作台（过渡期回退手段）"
        >
          工作台
        </button>
      </div>

      {/* ③ 待我处理面板（FE-14：内嵌浮层，非游戏窗 —— registry 不在本次改动名下） */}
      <PendingPanel
        open={pendingOpen}
        onClose={() => setPendingOpen(false)}
        onTotalChange={setPendingTotal}
      />

      {/* ④ 窗口层（浮在最上） */}
      <GameWindowLayer />
    </div>
  );
}
