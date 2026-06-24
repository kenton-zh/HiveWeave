import type { GameTimeSnapshot } from "@hiveweave/shared";

/** System context block injected on every agent trigger. */
export function buildTimeContextBlock(snapshot: GameTimeSnapshot): string {
  return `## 当前时间（每次触发自动附带）
- **项目时间**：${snapshot.formatted}（同事沟通、任务期限、闹钟均使用项目时间）
- **真实时间**：${snapshot.realFormatted}（仅在与外界交互、查新闻/资料时使用；操作前请先调用 \`hiveweave__get_real_time\` 确认）

> 人类操作者描述的时间默认指项目时间，除非明确给出真实日历日期（如「6月25日下午3点」）。`;
}

/** Prefix prepended to the user/trigger message on every agent turn. */
export function prefixTriggerMessage(snapshot: GameTimeSnapshot, message: string): string {
  return `[系统时间] 项目时间：${snapshot.formatted} | 真实时间：${snapshot.realFormatted}\n\n${message}`;
}

/** Prefix for inter-agent inbox messages (project time only). */
export function prefixInterAgentMessage(snapshot: GameTimeSnapshot, message: string): string {
  return `[项目时间 ${snapshot.formatted}] ${message}`;
}
