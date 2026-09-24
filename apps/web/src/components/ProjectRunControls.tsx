import { useState } from "react";
import { activateProject, deactivateProject, getProjects } from "../api";
import { useAppStore } from "../store";

/**
 * ProjectRunControls —— 项目「上班/下班」运行开关（UX-06 / 方案 §12.3）。
 *
 * 单一业务实现：从 App.tsx 旧三栏分支抽出。顶栏（office / workbench 两 种
 * 工作区共用的 <header>）与旧工作台左栏各挂一个本组件实例，共享同一份数据源
 * （store.projects[].isStarted）与同一组 API（activate/deactivateProject），
 * 不复制两套业务实现。
 *
 * 文案以后台契约为准（apps/hiveweave-py/.../api/projects.py deactivate 端点）：
 * 下班 = is_started=0 先落库阻断新 trigger（暂停后不再派新任务）+ 停光该项目
 * 全部 agent/watcher（off_duty 协作式取消）+ 停 game time。取消是协作式的，
 * 不承诺「立即停止」；按钮文案据此措辞，不说「暂停只是不派新任务」。
 *
 * 失败与重试：请求失败时行内提示「可点击重试」，按钮保持可用，成功后清除。
 */
export default function ProjectRunControls({ className = "" }: { className?: string }) {
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);
  const projects = useAppStore((s) => s.projects);
  const setProjects = useAppStore((s) => s.setProjects);
  const showToast = useAppStore((s) => s.showToast);
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);

  const currentProject = projects.find((p) => p.id === selectedProjectId);
  const isStarted = currentProject?.isStarted ?? false;

  // 无选中项目时本组件不渲染（顶栏与左栏挂载点都无需再判空）
  if (!selectedProjectId) return null;

  const handleToggle = async () => {
    if (pending) return; // 请求 pending 期间防重复提交
    setPending(true);
    try {
      if (isStarted) {
        await deactivateProject(selectedProjectId);
        showToast("已下班：项目全部 Agent 已停止，暂停期间不再派发新任务", "info");
      } else {
        await activateProject(selectedProjectId);
        showToast("已上班，Agent 已启动", "info");
      }
      // 刷新项目列表以获取最新 isStarted 状态
      const list = await getProjects();
      setProjects(list);
      setFailed(false);
    } catch (err) {
      console.error("Toggle project start failed:", err);
      setFailed(true);
      showToast("上下班操作失败，请重试", "error");
    } finally {
      setPending(false);
    }
  };

  return (
    <div className={`flex items-center gap-1.5 min-w-0 ${className}`} data-testid="project-run-controls">
      <button
        data-testid="project-run-toggle"
        onClick={handleToggle}
        disabled={pending}
        className={`flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full transition-all duration-200 active:scale-[0.97] ${
          isStarted
            ? "bg-g-green-bg text-g-green border border-g-green/20 hover:border-g-green/40"
            : "bg-g-bg-soft text-g-fg-3 border border-g-border hover:text-g-fg hover:border-g-border-strong"
        } disabled:opacity-50 disabled:cursor-not-allowed`}
        title={
          isStarted
            ? "下班：停止该项目全部 Agent（进行中的工作将被取消），暂停期间不再派发新任务"
            : "上班：启动该项目全部 Agent"
        }
      >
        <span className="relative flex w-2 h-2">
          {isStarted && (
            <span className="absolute inline-flex h-full w-full rounded-full bg-g-green-vivid animate-ping-ring" />
          )}
          <span className={`relative inline-flex w-2 h-2 rounded-full ${isStarted ? "bg-g-green-vivid" : "bg-g-fg-4"}`} />
        </span>
        <span>{pending ? "处理中..." : isStarted ? "上班中" : "已下班"}</span>
      </button>
      {failed && !pending && (
        <span
          data-testid="project-run-error"
          className="text-[11px] text-g-red whitespace-nowrap"
          title="上次上下班请求失败，项目状态未变更，可再次点击开关重试"
        >
          上次操作失败，可重试
        </span>
      )}
    </div>
  );
}
