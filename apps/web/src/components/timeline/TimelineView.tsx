/**
 * TimelineView — 左栏时间轴视图容器（Timeline v4 §5.1 挂载点）。
 *
 * 顶部：任务选择器（搜索下拉 + task_id 直达）——选中任务后右栏
 * 「任务」页签展示全链路回放（TaskTimelinePanel）。
 * 主体：团队泳道总览（TeamTimeline）。
 */

import { useAppStore } from "../../store";
import TaskPicker from "./TaskPicker";
import TeamTimeline from "./TeamTimeline";

export default function TimelineView() {
  const projectId = useAppStore((s) => s.selectedProjectId);

  return (
    <div className="h-full flex flex-col overflow-hidden bg-g-bg-soft">
      {/* 顶栏：标题 + 任务选择器 */}
      <div className="px-4 py-3 border-b border-g-border bg-white/80 backdrop-blur-sm shrink-0">
        <div className="flex items-center gap-2 mb-2">
          <svg className="w-4 h-4 text-g-blue" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M13 10V3L4 14h7v7l9-11h-7z"
            />
          </svg>
          <h2 className="text-sm font-medium text-g-fg">团队活动时间轴</h2>
        </div>
        {projectId ? (
          <TaskPicker />
        ) : (
          <p className="text-xs text-g-fg-4">请先在顶部选择一个项目</p>
        )}
      </div>

      {/* 主体：团队泳道总览 */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {projectId ? (
          <TeamTimeline key={projectId} />
        ) : (
          <div className="h-full flex items-center justify-center text-g-fg-3 text-sm">
            未选择项目
          </div>
        )}
      </div>
    </div>
  );
}
