/**
 * 详情窗固定角标（FE-07 / UX-02）
 *
 * 为什么在窗层而不是面板里：AgentDetailPanel 是业务面板（另一工作流维护，
 * 不持有窗口语义）；「固定对象」是**窗口级**状态（gamewindow store 的
 * pinnedAgent），角标是它唯一的显式操作入口 —— 没有它，固定就只能靠
 * store action 触发，用户无从发现、无从解除。
 *
 * 语义：未固定 = 提供固定；已固定 = 明确显示「已固定：乙」（对象名进角标，
 * 标题同步由 navigation/commands 的 setAgentDetailPinned 维护），
 * 点击解除后立即恢复跟随当前选中。
 */
import { agentDisplayName } from "../../navigation/agentNames";
import { setAgentDetailPinned } from "../../navigation/commands";
import { useGameWindowStore } from "./store";

export default function AgentPinChip({ agentId }: { agentId: string }) {
  const pinnedId = useGameWindowStore((s) => s.pinnedAgent.agent);
  const pinned = pinnedId != null;

  // 角标显示"被固定的人"（正常即窗口当前对象）；未固定时显示当前对象名，
  // 让"固定的是谁"在点击前就可预期。
  const shownId = pinned ? pinnedId ?? agentId : agentId;
  const shownName = agentDisplayName(shownId) ?? shownId.slice(0, 8);

  const label = pinned ? `已固定：${shownName}（点击解除）` : `固定：${shownName}`;

  return (
    <button
      onClick={() => setAgentDetailPinned(!pinned)}
      title={
        pinned
          ? "已固定：选择其他成员不会覆盖此窗口的内容，点击解除固定"
          : "固定到当前成员：之后选择其他人时，此窗口内容不再跟随变化"
      }
      className={`absolute bottom-2.5 right-2.5 z-10 flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] shadow-gm-sm backdrop-blur-sm transition-colors ${
        pinned
          ? "border-g-blue/40 bg-g-blue-bg/95 text-g-blue"
          : "border-g-border bg-white/85 text-g-fg-3 hover:bg-white hover:text-g-fg"
      }`}
    >
      <svg
        className={`h-3 w-3 shrink-0 ${pinned ? "" : "opacity-60"}`}
        viewBox="0 0 24 24"
        fill={pinned ? "currentColor" : "none"}
        stroke="currentColor"
        strokeWidth={2}
      >
        {/* 图钉 */}
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M16 3v2l-1 1v4l3 2v2h-5v6l-1 1-1-1v-6H6v-2l3-2V6L8 5V3h8z"
        />
      </svg>
      {label}
    </button>
  );
}
