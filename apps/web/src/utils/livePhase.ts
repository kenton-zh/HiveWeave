/**
 * 活动相位的统一文案/配色（OrgTree 徽章与 ChatPanel 头部共用）。
 * 从 OrgTree 抽出（09-08 #9 状态同源改造）。
 */

import type { AgentLivePhase } from "../api";

export const LIVE_PHASE_LABEL: Record<AgentLivePhase, string> = {
  tool: "工具",
  llm: "LLM",
  subagent: "子代理",
  working: "运行中",
  waiting: "等待",
  idle: "空闲",
};

export const LIVE_PHASE_STYLE: Record<AgentLivePhase, string> = {
  tool: "bg-g-yellow-bg text-g-yellow",
  llm: "bg-g-green-vivid/15 text-g-green",
  subagent: "bg-g-purple-bg text-g-purple",
  working: "bg-g-bg-muted text-g-fg-2",
  waiting: "bg-g-blue-bg text-g-blue",
  idle: "bg-g-fg-4/10 text-g-fg-4",
};

/** 非空闲相位的展示文案（phase 缺省视为空闲 → null）。 */
export function livePhaseLabel(
  phase: AgentLivePhase | undefined | null,
): string | null {
  if (!phase || phase === "idle") return null;
  return LIVE_PHASE_LABEL[phase] ?? "运行中";
}
