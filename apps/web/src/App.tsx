import { SkeletonList, OfficeSkeleton } from "./components/Skeleton";
import { useState, useRef, useEffect, Suspense } from "react";
import { Panel, PanelGroup, PanelResizeHandle, type ImperativePanelHandle } from "react-resizable-panels";
import OrgTree from "./components/OrgTree";
import OrgRail from "./components/OrgRail";
import ChatPanel from "./components/ChatPanel";
import ProjectTimeBadge from "./components/ProjectTimeBadge";
import ToastContainer from "./components/Toast";
import TokenUsagePanel from "./components/TokenUsagePanel";
import ErrorBoundary from "./components/ErrorBoundary";
import { useLiveStatusPoll } from "./hooks/useLiveStatusPoll";
import AssistantBall from "./components/AssistantBall";
import PendingPanel from "./components/pending/PendingPanel";
import { lazyRetry, resolveLeftPanel, OFFICE_UI_ENABLED } from "./mainPanel";

// 办公室关闭时 OfficeWorkspace 永不渲染 ⇒ 其 chunk（gamewindow/OfficeView/pixi.js
// ~1MB）不进主流程；开启时才按需拉取。PendingPanel 静态引入（顶栏徽标常驻所需）。
const OfficeWorkspace = lazyRetry(() => import("./components/OfficeWorkspace"));

// Lazy-loaded: only fetched when the user navigates to them.
// OfficeView alone pulls in pixi.js (~1MB); dialogs are interaction-only.
// TokenUsagePanel is small — static import. A rejected React.lazy() promise
// stays rejected for the session, which is the stuck English "Loading..." on Token.
const WorkLogPanel = lazyRetry(() => import("./components/WorkLogPanel"));
const AgentDetailPanel = lazyRetry(() => import("./components/AgentDetailPanel"));
const MonitorPanel = lazyRetry(() => import("./components/MonitorPanel"));
const DebugPanel = lazyRetry(() => import("./components/DebugPanel"));
const FolderPicker = lazyRetry(() => import("./components/FolderPicker"));
const OfficeView = lazyRetry(() => import("./components/OfficeView"));
const ModelConfigPage = lazyRetry(() => import("./components/ModelConfigPage"));
const ApiKeyDialog = lazyRetry(() => import("./components/ApiKeyDialog"));
const SettingsPanel = lazyRetry(() => import("./components/SettingsPanel"));
const GoalsPanel = lazyRetry(() => import("./components/GoalsPanel"));
const QuestionDialog = lazyRetry(() => import("./components/QuestionDialog"));
const NewProjectDialog = lazyRetry(() => import("./components/NewProjectDialog"));
const ConfirmDialog = lazyRetry(() => import("./components/ConfirmDialog"));
const TimelineView = lazyRetry(() => import("./components/timeline/TimelineView"));
const TaskTimelinePanel = lazyRetry(() => import("./components/timeline/TaskTimelinePanel"));
import { useAppStore } from "./store";
import { getProjects, createProject, deleteProject, leaveAgentChannel, subscribeAgentStatus, getProjectGameTime, getSettings, updateSettings, initApiKeyFromStorage, restartBackend, restartFrontend, updateProject, getSocket } from "./api";
import ProjectRunControls from "./components/ProjectRunControls";
import EmptyState from "./components/EmptyState";
import { SegmentItem } from "./components/SegmentTabs";
import { PanelLeftClose } from "lucide-react";
import type { DeleteProjectResponse, Project } from "./api";

function App() {
  const selectedAgentId = useAppStore((s) => s.selectedAgentId);
  const setSelectedAgent = useAppStore((s) => s.setSelectedAgent);
  const clearChatSessions = useAppStore((s) => s.clearChatSessions);
  const refreshOrgTree = useAppStore((s) => s.refreshOrgTree);
  const userName = useAppStore((s) => s.userName);
  const setUserName = useAppStore((s) => s.setUserName);
  const projects = useAppStore((s) => s.projects);
  const setProjects = useAppStore((s) => s.setProjects);
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);
  const setSelectedProjectId = useAppStore((s) => s.setSelectedProjectId);
  // per-agent 活动相位轮询（App 根挂载一次；OrgTree 徽章 + ChatPanel 头部共用）
  useLiveStatusPoll(selectedProjectId);
  const socketReconnectVersion = useAppStore((s) => s.socketReconnectVersion);
  const activeView = useAppStore((s) => s.activeView);
  const setActiveView = useAppStore((s) => s.setActiveView);
  const rightPanelTab = useAppStore((s) => s.rightPanelTab);
  // 主界面形态：office = 办公室主界面（ADR-011 §D2）｜ workbench = 三栏工作台（组织图 + Chat）。
  // OFFICE_UI_ENABLED=false（办公室暂时关闭）⇒ 强制 workbench，右下角无切换入口。
  const [workspaceMode, setWorkspaceMode] = useState<"office" | "workbench">(
    OFFICE_UI_ENABLED ? "office" : "workbench"
  );
  // 顶栏「待我处理」抽屉（办公室关闭后由工作台承接，计数徽标 + 面板开合）
  const [pendingOpen, setPendingOpen] = useState(false);
  const [pendingTotal, setPendingTotal] = useState(0);
  const setRightPanelTab = useAppStore((s) => s.setRightPanelTab);
  const selectedTaskId = useAppStore((s) => s.selectedTaskId);

  const setProcessingAgents = useAppStore((s) => s.setProcessingAgents);
  const updateProcessingAgent = useAppStore((s) => s.updateProcessingAgent);
  const showToast = useAppStore((s) => s.showToast);

  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState(userName);
  const nameInputRef = useRef<HTMLInputElement>(null);

  // Project selector state
  const [showProjectMenu, setShowProjectMenu] = useState(false);
  const [showFolderPicker, setShowFolderPicker] = useState(false);
  const [folderPickerInitialPath] = useState<string | undefined>(() => {
    if (typeof window === "undefined") return undefined;
    const params = new URLSearchParams(window.location.search);
    return params.get("folderPath") ?? undefined;
  });
  const [showModelConfig, setShowModelConfig] = useState(false);
  const [showApiKeyDialog, setShowApiKeyDialog] = useState(false);
  const [showMcpPanel, setShowMcpPanel] = useState(false);
  const [showNewProjectDialog, setShowNewProjectDialog] = useState(false);
  const [newProjectCEO, setNewProjectCEO] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null);
  // P1 (§5.5b①)：项目设置 —— 外部只读参考目录
  const [showProjectSettings, setShowProjectSettings] = useState(false);
  const [readDirsDraft, setReadDirsDraft] = useState("");
  const [savingReadDirs, setSavingReadDirs] = useState(false);
  const [writableDirsDraft, setWritableDirsDraft] = useState("");
  const [savingWritableDirs, setSavingWritableDirs] = useState(false);
  const [sandboxModeDraft, setSandboxModeDraft] = useState("");
  const projectMenuRef = useRef<HTMLDivElement>(null);
  const projectListRef = useRef<HTMLDivElement>(null);
  const [deletingProjectId, setDeletingProjectId] = useState<string | null>(null);
  const [queuedDeleteIds, setQueuedDeleteIds] = useState<string[]>([]);
  const deleteQueueRef = useRef<Array<{ id: string; name: string }>>([]);
  const deleteRunningRef = useRef(false);


  // Restore API key from localStorage on mount
  useEffect(() => {
    initApiKeyFromStorage();
  }, []);

  // Load projects on mount
  useEffect(() => {
    async function load() {
      try {
        deleteQueueRef.current = [];
        deleteRunningRef.current = false;
        setDeletingProjectId(null);
        setQueuedDeleteIds([]);

        const list = await getProjects();
        setProjects(list);
        const current = useAppStore.getState().selectedProjectId;
        if (list.length === 0) {
          setSelectedProjectId(null);
          setSelectedAgent(null);
        } else {
          const exists = current && list.some((p) => p.id === current);
          if (!exists) {
            setSelectedProjectId(list[0].id);
          }
        }
      } catch (err) {
        console.error("Failed to load projects:", err);
      }
    }
    load();
  }, []);

  // WebSocket 重连后重新获取项目列表（同步 isStarted 等状态）
  useEffect(() => {
    if (socketReconnectVersion === 0) return; // 跳过初始值
    getProjects().then(setProjects).catch(() => {});
  }, [socketReconnectVersion]);

  // Close project menu on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (projectMenuRef.current && !projectMenuRef.current.contains(e.target as Node)) {
        setShowProjectMenu(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  // Open the project menu with the selected project scrolled into view
  // (list area is height-capped and scrolls internally once projects grow).
  useEffect(() => {
    if (!showProjectMenu) return;
    projectListRef.current?.querySelector('[data-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [showProjectMenu]);


  useEffect(() => {
    if (!deletingProjectId && queuedDeleteIds.length === 0) return;
    const timer = window.setTimeout(() => {
      if (!deleteRunningRef.current) return;
      console.warn("[App] Delete UI state timed out — resetting");
      deleteQueueRef.current = [];
      deleteRunningRef.current = false;
      setDeletingProjectId(null);
      setQueuedDeleteIds([]);
    }, 125_000);
    return () => window.clearTimeout(timer);
  }, [deletingProjectId, queuedDeleteIds.length]);

  // WebSocket 重连信号 — socket 层无条件触发，不依赖 lobby 快照内容变化。
  // 此前 bump 只在 processing 集合快照 key 变化时发出：断连前后集合恰好
  // 相同（如重连横跨一个完整轮次）时聊天层永远不知道发生过重连，错过
  // 的 done/整轮不会补偿，面板停在断连时刻的快照上。
  useEffect(() => {
    const g = globalThis as any;
    if (g.__hw_socketReconnectHooked) return;
    let socket: ReturnType<typeof getSocket>;
    try {
      socket = getSocket();
    } catch {
      return; // 构造失败不置位，下次 effect 重试注册
    }
    g.__hw_socketReconnectHooked = true;
    // establishedConnections 在 open 回调前自增：首连=1，重连≥2。
    socket.onOpen(() => {
      if ((socket as any).establishedConnections > 1) {
        useAppStore.getState().bumpSocketReconnect();
      }
    });
  }, []);

  // Subscribe to real-time agent processing status
  useEffect(() => {
    let lastSnapshotKey = "";
    const controller = subscribeAgentStatus(
      (agentIds) => {
        const key = agentIds.slice().sort().join(",");
        if (key === lastSnapshotKey) return;
        lastSnapshotKey = key;
        setProcessingAgents(agentIds);
        // 重连信号不再由快照发出：断连前后集合恰好相同时（重连横跨一个
        // 完整轮次）这里永远不触发。唯一信号源是上方 socket.onOpen 的
        // bump —— socketReconnectVersion 因此只统计真实重连。
      },
      (agentId, processing, disposition) => {
        updateProcessingAgent(agentId, processing);
        if (disposition) {
          useAppStore.getState().setAgentDisposition(agentId, disposition);
        }
      },
      (event) => {
        useAppStore.getState().addActivity(event as any);
      },
      () => refreshOrgTree(),
      (projectId: string) => useAppStore.getState().bumpGoalsVersion(projectId),
      () => useAppStore.getState().bumpQuestionVersion(),
      (signal) => useAppStore.getState().notifyTaskEvent(signal.project_id, signal.task_id),
    );
    return () => controller.abort();
  }, []);

  useEffect(() => {
    getSettings().then((settings) => {
      if (settings.operatorName) {
        setUserName(settings.operatorName);
      }
    }).catch(() => {});
  }, []);

  // FE-05（UX-06）：「上班/下班」开关已抽成 ProjectRunControls 共享组件
  // （单一业务实现），挂载在共用顶栏 + 旧工作台左栏，见下方两处 <ProjectRunControls />。

  const currentProject = projects.find((p) => p.id === selectedProjectId);

  // P1/P2 (§5.5b)：打开项目设置 —— 编辑外部只读参考 / 附加可写目录
  const openProjectSettings = () => {
    if (!currentProject) return;
    setReadDirsDraft((currentProject.additionalReadDirs ?? []).join(", "));
    setWritableDirsDraft((currentProject.additionalWritableDirs ?? []).join(", "));
    setSandboxModeDraft((currentProject.sandboxMode ?? "").trim());
    setShowProjectSettings(true);
    setShowProjectMenu(false);
  };

  const saveProjectDirs = async () => {
    if (!selectedProjectId) return;
    setSavingReadDirs(true);
    try {
      const readDirs = readDirsDraft
        .split(/[,，\n]/)
        .map((d) => d.trim())
        .filter(Boolean);
      const writableDirs = writableDirsDraft
        .split(/[,，\n]/)
        .map((d) => d.trim())
        .filter(Boolean);
      await updateProject(selectedProjectId, {
        additionalReadDirs: readDirs,
        additionalWritableDirs: writableDirs,
        sandboxMode: sandboxModeDraft.trim(),
      });
      const list = await getProjects();
      setProjects(list);
      setShowProjectSettings(false);
      showToast("项目目录设置已保存", "success");
    } catch (err) {
      console.error("Save project dirs failed:", err);
      showToast("保存失败", "error");
    } finally {
      setSavingReadDirs(false);
    }
  };

  const handleSwitchProject = (id: string) => {
    const st = useAppStore.getState();
    if (st.selectedAgentId) {
      try { leaveAgentChannel(st.selectedAgentId); } catch { /* noop */ }
    }
    useAppStore.setState({
      selectedProjectId: id,
      selectedAgentId: null,
      selectedTaskId: null,
      chatSessions: {},
      processingAgents: [],
      orgTreeVersion: st.orgTreeVersion + 1,
    });
    setRightPanelTab("chat");
    setShowProjectMenu(false);
  };

  const handleCreateProjectFromFolder = async (folderPath: string) => {
    const normalizedPath = folderPath.replace(/\\/g, "/");
    const existing = projects.find(
      (p) => p.workspacePath?.replace(/\\/g, "/") === normalizedPath
    );
    if (existing) {
      setSelectedProjectId(existing.id);
      setSelectedAgent(null);
      clearChatSessions();
      refreshOrgTree();
      setShowProjectMenu(false);
      showToast(`该目录已有项目「${existing.name}」，已切换到该项目`, "info");
      return;
    }

    const name = folderPath.split(/[\\/]/).filter(Boolean).pop() || "New Project";
    try {
      const { project, mainAgentId, adopted } = await createProject(name, folderPath, undefined, undefined, "zh");
      const updated = await getProjects();
      setProjects(updated);
      setSelectedProjectId(project.id);
      clearChatSessions();
      refreshOrgTree();
      setShowProjectMenu(false);
      if (adopted) {
        showToast("该目录已含平台数据，已导入项目（团队与历史保留）", "success");
        setSelectedAgent(null);
        setNewProjectCEO(null);
      } else if (mainAgentId) {
        setNewProjectCEO(mainAgentId);
        setSelectedAgent(mainAgentId);
        setTimeout(() => {
          setShowNewProjectDialog(true);
        }, 500);
      }
    } catch (err) {
      console.error("Failed to create project:", err);
      const msg = err instanceof Error ? err.message : String(err);
      showToast(`创建项目失败：${msg}`, "error");
    }
  };

  const detachDeletedProject = (id: string, remaining: Project[]) => {
    const st = useAppStore.getState();
    if (st.selectedProjectId !== id) return;
    const next = remaining[0]?.id ?? null;
    if (st.selectedAgentId) {
      try { leaveAgentChannel(st.selectedAgentId); } catch { /* noop */ }
    }
    useAppStore.setState({
      selectedProjectId: next,
      selectedAgentId: null,
      selectedTaskId: null,
      chatSessions: {},
      processingAgents: [],
      orgTreeVersion: st.orgTreeVersion + 1,
      ...(st.rightPanelTab === "task" ? { rightPanelTab: "chat" as const } : {}),
    });
    setShowNewProjectDialog(false);
    setNewProjectCEO(null);
  };


  const showDeleteCleanupToasts = (result: DeleteProjectResponse | undefined) => {
    if (!result) return;
    const wc = result.workspaceCleanup;
    if (wc?.status === "skipped" && wc.reason === "shared") {
      const peers = wc.sharedWith?.length ? wc.sharedWith.join(", ") : "其他项目";
      showToast(`工作区 .hiveweave 已保留（与 ${peers} 共享路径）`, "info");
    }
    if (wc?.status === "scheduled") {
      showToast("平台数据目录正在后台清理，请稍候", "info");
    }
    if (wc?.status === "failed") {
      const suffix = wc.pendingDir ? `：${wc.pendingDir}` : "";
      showToast(`工作区清理未完成${suffix}`, "error");
    }
    if (result.dbLeftover) {
      showToast("部分数据库文件可能仍残留在磁盘上", "error");
    }
    if (result.warning) {
      showToast(result.warning, "warning");
    }
  };


  const runDeleteQueue = async () => {
    if (deleteRunningRef.current) return;
    deleteRunningRef.current = true;
    try {
      while (deleteQueueRef.current.length > 0) {
        const item = deleteQueueRef.current.shift()!;
        setDeletingProjectId(item.id);
        try {
          const deleteResult = await deleteProject(item.id);
          showToast(`项目「${item.name}」已删除`, "success");
          showDeleteCleanupToasts(deleteResult);
        } catch (err) {
          const msg = err instanceof Error ? err.message : "未知错误";
          console.error("Failed to delete project:", err);
          if (msg.includes("404") || msg.toLowerCase().includes("abort")) {
            showToast(`项目「${item.name}」已删除`, "success");
          } else {
            showToast(`删除项目「${item.name}」失败: ${msg}`, "error");
          }
        }
        try {
          const updated = await getProjects();
          setProjects(updated);
          detachDeletedProject(item.id, updated);
        } catch (err) {
          console.error("Failed to refresh projects after delete:", err);
          detachDeletedProject(item.id, useAppStore.getState().projects);
        } finally {
          setQueuedDeleteIds((prev) => prev.filter((id) => id !== item.id));
        }
      }
    } finally {
      setDeletingProjectId(null);
      setQueuedDeleteIds([]);
      deleteRunningRef.current = false;
    }
  };

  const handleDeleteProject = (id: string) => {
    const currentProjects = useAppStore.getState().projects;
    const proj = currentProjects.find((p) => p.id === id);
    if (!proj) return;
    if (deleteQueueRef.current.some((item) => item.id === id)) return;
    setConfirmDelete({ id, name: proj.name });
    setShowProjectMenu(false);
  };

  const handleConfirmDelete = () => {
    if (!confirmDelete) return;
    const { id, name } = confirmDelete;
    setConfirmDelete(null);
    const currentProjects = useAppStore.getState().projects;
    const remaining = currentProjects.filter((p) => p.id !== id);
    setProjects(remaining);
    detachDeletedProject(id, remaining);

    deleteQueueRef.current.push({ id, name });
    setQueuedDeleteIds((prev) => [...prev, id]);
    void runDeleteQueue();
  };

  const startEditName = () => {
    setNameDraft(userName);
    setEditingName(true);
    setTimeout(() => nameInputRef.current?.focus(), 0);
  };

  const saveName = () => {
    const trimmed = nameDraft.trim();
    if (trimmed) {
      setUserName(trimmed);
      updateSettings({ operatorName: trimmed }).catch((err) => {
        console.warn("Failed to sync operator name to backend:", err);
      });
    }
    setEditingName(false);
  };

  const leftPanel = resolveLeftPanel(activeView, selectedProjectId);

  // ── 左栏折叠（2026-10-01「组织图太占地方」修复）────────────────
  // 收起后左 Panel 缩到 collapsedSize≈3.5%（窄条 OrgRail：头像+状态点），
  // 页头与视图用 display:none 保持挂载 —— OrgTree 的 approvals/闹钟轮询、
  // 缩放与折叠状态都不中断，展开即恢复原样。
  // 布局经 autoSaveId 持久化（恢复是 layout-effect 内同步读，且库在首帧
  // 必回调 onResize）；挂载 effect 再兜一次 isCollapsed()，之后变化全走
  // onResize —— 三条路径互为保险。
  const leftPanelRef = useRef<ImperativePanelHandle>(null);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  useEffect(() => {
    const p = leftPanelRef.current;
    if (p) setLeftCollapsed(p.isCollapsed());
  }, []);

  // 合并栏的 "chat" 页签只在选中成员时存在：无成员时残留的 chat tab 归一为
  // "目标"（避免未高亮的空页签）。
  const effectiveRightTab =
    rightPanelTab === "chat" && !selectedAgentId ? "goals" : rightPanelTab;

  // 合并栏详情面板块（agent/logs/monitor/debug；goals 与 task 在外层链处理）。
  // key=tab：错误不跨 tab sticky（boundary 随 tab 重挂载清 state）。
  const detailPanels = (
    <ErrorBoundary key={effectiveRightTab} label="右栏面板">
      <Suspense fallback={<SkeletonList rows={5} />}>
        {effectiveRightTab === "agent" && selectedAgentId && (
          <div key="panel-agent" className="hw-tab-in h-full">
            <AgentDetailPanel agentId={selectedAgentId} />
          </div>
        )}
        {effectiveRightTab === "monitor" && selectedAgentId && (
          <div key="panel-monitor" className="hw-tab-in h-full">
            <MonitorPanel agentId={selectedAgentId} />
          </div>
        )}
        {effectiveRightTab === "debug" && (
          <div key="panel-debug" className="hw-tab-in h-full">
            <DebugPanel />
          </div>
        )}
        {effectiveRightTab === "logs" && selectedAgentId && (
          <div key="panel-logs" className="hw-tab-in h-full">
            <WorkLogPanel agentId={selectedAgentId} />
          </div>
        )}
      </Suspense>
    </ErrorBoundary>
  );

  return (
    <div className="h-screen flex flex-col bg-g-bg-soft">
      {/* Top Bar */}
      <header className="h-14 border-b border-g-border flex items-center px-5 bg-white shrink-0 relative z-30">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-8 h-8 rounded-gm bg-gradient-to-br from-g-blue to-g-purple flex items-center justify-center shrink-0 shadow-gm-sm transition-transform duration-200 hover:scale-105">
            <span className="text-white font-bold text-sm">H</span>
          </div>
          <h1 className="text-[14px] font-semibold text-g-fg tracking-tight shrink-0 select-none">
            HiveWeave
          </h1>
          <ProjectTimeBadge projectId={selectedProjectId} />
        </div>

        {/* Project Selector */}
        <div className="ml-6 relative" ref={projectMenuRef}>
          <button
            data-testid="project-trigger"
            onClick={() => setShowProjectMenu(!showProjectMenu)}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-gm bg-white border transition-all duration-200 text-[12px] text-g-fg shadow-gm-sm hover:shadow-gm active:scale-[0.98] ${
              showProjectMenu ? "border-g-blue/50 ring-2 ring-g-blue/15" : "border-g-border hover:border-g-border-strong"
            }`}
          >
            <svg className="w-4 h-4 text-g-fg-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
            </svg>
            <span className="max-w-[120px] truncate">{currentProject?.name || "选择项目"}</span>
            <svg className={`w-3 h-3 text-g-fg-3 transition-transform duration-200 ${showProjectMenu ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          {(deletingProjectId || queuedDeleteIds.length > 0) && (
            <div className="absolute top-full left-0 mt-1.5 w-56 px-3 py-2 text-xs text-g-fg-2 bg-white border border-g-border rounded-gm shadow-gm-md z-50 animate-slide-down">
              正在删除项目，请稍候...
              {queuedDeleteIds.length > 1 ? `（队列 ${queuedDeleteIds.length} 个）` : ""}
            </div>
          )}
          {showProjectMenu && (
            <div
              data-testid="project-menu"
              className="absolute top-full left-0 mt-1.5 w-56 max-h-[calc(100vh-5rem)] flex flex-col bg-white border border-g-border rounded-gmLg shadow-gm-pop z-50 py-1.5 px-1 animate-scale-in origin-top-left"
            >
              <div ref={projectListRef} className="overflow-y-auto min-h-0">
                {projects.map((p) => (
                  <div
                    key={p.id}
                    data-selected={p.id === selectedProjectId}
                    className={`flex items-center justify-between px-3 py-2 text-sm cursor-pointer rounded-gm hover:bg-g-bg-muted transition-colors ${
                      p.id === selectedProjectId ? "text-g-blue bg-g-blue-bg" : "text-g-fg"
                    }`}
                  >
                    <span
                      className="flex-1 truncate"
                      onClick={() => handleSwitchProject(p.id)}
                    >
                      {p.name}
                    </span>
                    <button
                      type="button"
                      disabled={deletingProjectId === p.id || queuedDeleteIds.includes(p.id)}
                      onClick={(e) => { e.stopPropagation(); handleDeleteProject(p.id); }}
                      className="ml-2 text-g-fg-4 hover:text-g-red hover:bg-g-red-bg rounded-full p-1 transition-all disabled:opacity-30 disabled:cursor-not-allowed"
                      title={deletingProjectId === p.id ? "删除中..." : "删除项目"}
                    >
                      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                    </button>
                  </div>
                ))}
              </div>

              <div className="border-t border-g-border mt-1 pt-1 shrink-0">
                {selectedProjectId && (
                  <button
                    onClick={openProjectSettings}
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-g-fg-3 hover:text-g-blue hover:bg-g-blue-bg/60 rounded-gm transition-colors"
                  >
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                    </svg>
                    项目设置
                  </button>
                )}
                <button
                  onClick={() => setShowFolderPicker(true)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-g-fg-3 hover:text-g-blue hover:bg-g-blue-bg/60 rounded-gm transition-colors"
                >
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                  </svg>
                  新建项目
                </button>
              </div>
            </div>
          )}
        </div>

        <div className="ml-auto flex items-center gap-1.5">
          {/* 待我处理：工作台形态由顶栏承接（办公室形态走 OfficeWorkspace 自带
              HUD 入口，两处同时挂 = 双份轮询）。徽标 = 未处理总数 */}
          {workspaceMode === "workbench" && (
          <div className="relative">
            <button
              data-testid="pending-trigger"
              aria-expanded={pendingOpen}
              aria-haspopup="dialog"
              onClick={() => setPendingOpen((v) => !v)}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium rounded-gm border transition-all duration-200 active:scale-[0.97] ${
                pendingOpen
                  ? "text-g-blue border-g-blue/40 bg-g-blue-bg shadow-gm-sm"
                  : "text-g-fg-2 bg-white border-g-border shadow-gm-sm hover:text-g-blue hover:border-g-blue/40 hover:shadow-gm"
              }`}
              title="查看所有等待你处理的事项（提问 / 授权 / 验收 / 提醒）"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
              </svg>
              <span className="hidden xl:inline">待我处理</span>
              {pendingTotal > 0 && (
                <span
                  data-testid="pending-hud-badge"
                  className="min-w-[1.1rem] px-1 py-px text-center text-[10px] leading-4 font-semibold rounded-full bg-g-red text-white"
                >
                  {pendingTotal > 99 ? "99+" : pendingTotal}
                </span>
              )}
            </button>
            <PendingPanel
              open={pendingOpen}
              onClose={() => setPendingOpen(false)}
              onTotalChange={setPendingTotal}
              overlayClassName="fixed right-3 top-[3.75rem] z-40 w-[380px] max-w-[calc(100vw-1.5rem)] max-h-[calc(100vh-4.75rem)]"
            />
          </div>
          )}
          {/* 项目运行开关（FE-05/UX-06）：office / workbench 两种工作区共用的顶栏入口，
              与旧工作台左栏渲染同一个 ProjectRunControls 实例逻辑 */}
          <ProjectRunControls className="mr-1" />
          {/* Restart buttons */}
          <div className="flex items-center gap-0.5 bg-g-bg-soft border border-g-border rounded-gm p-0.5">
            <button
              onClick={async () => {
                if (!confirm("Restart backend? This will kill and relaunch the uvicorn server.")) return;
                try {
                  await restartBackend();
                  showToast("Backend restarting...", "info");
                } catch {
                  showToast("Failed to trigger backend restart", "error");
                }
              }}
              className="text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted rounded-gm p-1.5 transition-all active:scale-90"
              title="Restart Backend"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
            </button>
            <button
              onClick={async () => {
                if (!confirm("Restart frontend? This will kill and relaunch the Vite dev server.")) return;
                try {
                  await restartFrontend();
                  showToast("Frontend restarting...", "info");
                } catch {
                  showToast("Failed to trigger frontend restart", "error");
                }
              }}
              className="text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted rounded-gm p-1.5 transition-all active:scale-90"
              title="Restart Frontend"
            >
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2" />
              </svg>
            </button>
          </div>
          {/* Model Config page (模型清单 + 层级配置) — 带文字的醒目入口 */}
          <button
            onClick={() => setShowModelConfig(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-g-fg-2 bg-white border border-g-border rounded-gm shadow-gm-sm hover:text-g-blue hover:border-g-blue/40 hover:shadow-gm transition-all duration-200 active:scale-[0.97]"
            title="模型配置"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 21v-7m0-4V3m8 18v-9m0-4V3m8 18v-5m0-4V3M1 14h6m2-6h6m2 8h6" />
            </svg>
            模型配置
          </button>
          {/* API Key 设置 */}
          <button
            onClick={() => setShowApiKeyDialog(true)}
            className="text-g-fg-3 hover:text-g-blue hover:bg-g-blue-bg/60 rounded-full p-1.5 transition-all active:scale-90"
            title="API Key 设置"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 7a4 4 0 11-8 0 4 4 0 018 0zM12 15v6m-3-3h6" />
            </svg>
          </button>
          {/* 设置面板（无人值守 / MCP 服务器） */}
          <button
            onClick={() => setShowMcpPanel(true)}
            className="text-g-fg-3 hover:text-g-blue hover:bg-g-blue-bg/60 rounded-full p-1.5 transition-all active:scale-90"
            title="设置"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          </button>
          {/* Editable user name */}
          {editingName ? (
            <input
              ref={nameInputRef}
              value={nameDraft}
              onChange={(e) => setNameDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") saveName(); if (e.key === "Escape") setEditingName(false); }}
              onBlur={saveName}
              className="w-24 px-2 py-1 text-xs bg-white border border-g-blue/40 rounded-gm text-g-fg focus:outline-none focus:border-g-blue focus:ring-2 focus:ring-g-blue/25 transition-all"
            />
          ) : (
            <button
              onClick={startEditName}
              className="flex items-center gap-1.5 text-xs text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted rounded-full px-2.5 py-1.5 transition-all active:scale-95"
              title="点击修改你的名称"
            >
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
              </svg>
              <span>{userName}</span>
            </button>
          )}
        </div>
      </header>

      {/* 主界面形态：办公室（OFFICE_UI_ENABLED=true 时默认）/ 工作台（组织图 + Chat 三栏）。
          办公室暂时关闭（OFFICE_UI_ENABLED=false）⇒ 恒为工作台，OfficeWorkspace chunk
          不渲染、不加载（pixi.js ~1MB 不进主流程）。
          ⚠ 三栏分支的内部缩进刻意保持原样未动 —— 1000+ 行文件重排缩进的出错
          风险大于收益，等三栏按 ADR-011 正式退役时一并清理。 */}
      {OFFICE_UI_ENABLED && workspaceMode === "office" ? (
        <Suspense fallback={<OfficeSkeleton />}>
          <OfficeWorkspace onExitToWorkbench={() => setWorkspaceMode("workbench")} />
        </Suspense>
      ) : (
      <>
      {/* Main Content — floating panels on a gray workbench.
          10-01 改版：两栏（组织图 ｜ 对话+详情合并栏）。原三栏的中栏 Chat 与
          右栏详情合并成一栏、对话成为页签之一 —— 三栏时 Chat 只有 ~31% 宽，
          消息流太挤。可拖拽分栏（autoSaveId 持久化栏宽到 localStorage）。 */}
      <PanelGroup
        direction="horizontal"
        autoSaveId="hw-main-columns-v2"
        className="flex-1 overflow-hidden"
      >
        {/* Left Panel - Org Tree / Office；collapsible：一键收成 OrgRail 窄条 */}
        <Panel
          id="left"
          ref={leftPanelRef}
          defaultSize={32}
          minSize={18}
          collapsible
          collapsedSize={3.5}
          onResize={(size) => setLeftCollapsed(size < 4)}
          // 折叠态去掉 pl-3：窄条只有 ~40–56px，12px 内边距会让深层头像溢出（审计 L3）
          className={`min-w-0 pt-3 pb-3 ${leftCollapsed ? "" : "pl-3"}`}
        >
        <div className="h-full flex flex-col bg-white rounded-gmLg border border-g-border shadow-gm-sm overflow-hidden min-w-0">
          <div className={`px-3 py-2.5 border-b border-g-border bg-white flex items-center gap-3 ${leftCollapsed ? "hidden" : ""}`}>
            {/* View tabs（办公室关闭时不渲染 Office 页签）。
                pillId 统一 "hw-seg-left"：切换时白底指示条滑动（SegmentItem） */}
            <div className="hw-segment">
              <SegmentItem
                active={leftPanel === "tree"}
                pillId="hw-seg-left"
                onClick={() => setActiveView("tree")}
              >
                组织图
              </SegmentItem>
              {OFFICE_UI_ENABLED && (
                <SegmentItem
                  active={activeView === "office"}
                  pillId="hw-seg-left"
                  onClick={() => setActiveView("office")}
                >
                  办公室
                </SegmentItem>
              )}
              <SegmentItem
                active={activeView === "timeline"}
                pillId="hw-seg-left"
                onClick={() => setActiveView("timeline")}
              >
                时间线
              </SegmentItem>
              {selectedProjectId && (
                <SegmentItem
                  active={activeView === "token"}
                  pillId="hw-seg-left"
                  onClick={() => setActiveView("token")}
                >
                  Token
                </SegmentItem>
              )}
            </div>

            {/* Project-level start/stop button —— FE-05：与顶栏共用 ProjectRunControls，
                业务实现只有一份（不再在 App 内维护第二套开关逻辑） */}
            <ProjectRunControls className="ml-auto shrink-0" />
            {/* 收起侧栏（2026-10-01）：组织图常驻占宽 → 一键变窄条 */}
            <button
              onClick={() => leftPanelRef.current?.collapse()}
              title="收起侧栏（组织图变为窄条，可随时展开）"
              aria-label="收起侧栏"
              className="shrink-0 p-1 rounded-gm text-g-fg-4 hover:text-g-fg hover:bg-g-bg-muted transition-colors"
            >
              <PanelLeftClose className="w-4 h-4" />
            </button>
          </div>
          <div className={`flex-1 overflow-hidden bg-app-tint ${leftCollapsed ? "hidden" : ""}`}>
            <div key={activeView} className="hw-tab-in h-full">
              {leftPanel === "tree" ? (
                <OrgTree />
              ) : leftPanel === "timeline" ? (
                <ErrorBoundary label="Timeline">
                  <Suspense fallback={<SkeletonList rows={6} />}>
                    <TimelineView />
                  </Suspense>
                </ErrorBoundary>
              ) : leftPanel === "token" && selectedProjectId ? (
                <TokenUsagePanel key={selectedProjectId} projectId={selectedProjectId} />
              ) : leftPanel === "token-empty" ? (
                <div className="h-full flex items-center justify-center text-g-fg-3 text-sm">
                  请先选择一个项目
                </div>
              ) : (
                <ErrorBoundary label="Office">
                  <Suspense fallback={<OfficeSkeleton />}>
                    <OfficeView />
                  </Suspense>
                </ErrorBoundary>
              )}
            </div>
          </div>
          {/* 折叠态：窄条头像列表（页头/视图此时 display:none 保持挂载） */}
          {leftCollapsed && <OrgRail onExpand={() => leftPanelRef.current?.expand()} />}
        </div>
        </Panel>

        <PanelResizeHandle className="w-2 self-stretch my-3 rounded-gm transition-colors hover:bg-g-blue-bg/70 data-[resize-handle-state=drag]:bg-g-blue/30" />

        {/* 合并栏：对话 + 详情同栏切页签（原中栏 Chat 与右栏详情合并）。
            ChatPanel 跨页签常驻挂载 —— hidden 只切可见性，不打断通道生命周期，
            切到详情再切回来不会丢流式进度。 */}
        {/* defaultSize 与左栏合计须 =100（32+68）：两栏都设 defaultSize 时
            库不会补差额，合计≠100 会触发归一化 warning 并持久化偏离值（审计 M2） */}
        <Panel id="work" defaultSize={68} minSize={40} className="min-w-0 pt-3 pr-3 pb-3">
        <div className="h-full flex flex-col bg-white rounded-gmLg border border-g-border shadow-gm-sm overflow-hidden min-w-0">
          {/* Tab bar */}
          <div className="px-3 py-2.5 border-b border-g-border bg-white flex items-center gap-1">
            {/* pillId 统一 "hw-seg-right"：与左栏隔离，避免跨栏串联动画 */}
            <div className="hw-segment mr-2 overflow-x-auto">
            {selectedAgentId && (
              <SegmentItem
                active={rightPanelTab === "chat"}
                pillId="hw-seg-right"
                onClick={() => setRightPanelTab("chat")}
              >
                对话
              </SegmentItem>
            )}
            <SegmentItem
              active={effectiveRightTab === "goals"}
              pillId="hw-seg-right"
              onClick={() => setRightPanelTab("goals")}
            >
              目标
            </SegmentItem>
            {selectedAgentId && (
              <>
                <SegmentItem
                  active={effectiveRightTab === "agent"}
                  pillId="hw-seg-right"
                  onClick={() => setRightPanelTab("agent")}
                >
                  详情
                </SegmentItem>
                <SegmentItem
                  active={effectiveRightTab === "logs"}
                  pillId="hw-seg-right"
                  onClick={() => setRightPanelTab("logs")}
                >
                  日志
                </SegmentItem>
                <SegmentItem
                  active={effectiveRightTab === "monitor"}
                  pillId="hw-seg-right"
                  onClick={() => setRightPanelTab("monitor")}
                >
                  监控
                </SegmentItem>
              </>
            )}
            {selectedTaskId && (
              <SegmentItem
                active={effectiveRightTab === "task"}
                pillId="hw-seg-right"
                onClick={() => setRightPanelTab("task")}
              >
                任务
              </SegmentItem>
            )}
            <SegmentItem
              active={effectiveRightTab === "debug"}
              pillId="hw-seg-right"
              onClick={() => setRightPanelTab("debug")}
            >
              调试
            </SegmentItem>
            </div>
          </div>

          {/* Tab content —— ChatPanel 跨页签常驻（hidden 只切可见性，不打断
              通道生命周期）。详情区用 effectiveRightTab 判定：无成员时残留的
              chat tab 归一为 goals，避免整栏空白。 */}
          <div className="flex-1 overflow-hidden">
            {selectedAgentId && (
              <ChatPanel key="panel-chat" agentId={selectedAgentId} hidden={rightPanelTab !== "chat"} />
            )}
            {effectiveRightTab !== "chat" && (
              effectiveRightTab === "task" && selectedTaskId ? (
                <ErrorBoundary label="任务">
                  <Suspense fallback={<SkeletonList rows={6} />}>
                    <div key="panel-task" className="hw-tab-in h-full">
                      <TaskTimelinePanel />
                    </div>
                  </Suspense>
                </ErrorBoundary>
              ) : effectiveRightTab === "goals" ? (
                <ErrorBoundary key="panel-goals" label="右栏面板">
                  <div className="hw-tab-in h-full">
                    {selectedProjectId ? (
                      <Suspense fallback={<SkeletonList rows={5} />}>
                        <GoalsPanel projectId={selectedProjectId} />
                      </Suspense>
                    ) : (
                      <div className="h-full flex items-center justify-center text-g-fg-3 text-sm">
                        请先选择一个项目
                      </div>
                    )}
                  </div>
                </ErrorBoundary>
              ) : effectiveRightTab === "debug" ? (
                // 调试面板不依赖成员，无选中成员时也应可看（否则高亮「调试」却显示空态）。
                detailPanels
              ) : !selectedAgentId ? (
                <EmptyState
                  icon={
                    <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                    </svg>
                  }
                  title="未选中成员"
                  description="在左侧组织图中点击成员节点，即可查看其对话、详情、日志与监控"
                />
              ) : (
                detailPanels
              )
            )}
          </div>
        </div>
        </Panel>
      </PanelGroup>
      </>
      )}

      {/* 工作台模式下的回办公室入口（办公室模式内已有 HUD 入口；办公室关闭时隐藏） */}
      {OFFICE_UI_ENABLED && workspaceMode === "workbench" && (
        <button
          onClick={() => setWorkspaceMode("office")}
          className="fixed bottom-5 right-5 z-40 px-3.5 py-2 rounded-full bg-[#394d6a] text-white text-xs font-medium shadow-gm-md hover:bg-[#2f405a] transition-colors"
          title="切回办公室主界面"
        >
          办公室
        </button>
      )}

      {/* Lazy-loaded dialogs — wrapped in Suspense, fallback=null since they're overlays */}
      <ErrorBoundary label="对话框" fallback={null}>
      <Suspense fallback={null}>
        {showFolderPicker && (
          <FolderPicker
            initialPath={folderPickerInitialPath}
            onSelect={(path) => {
              handleCreateProjectFromFolder(path);
              setShowFolderPicker(false);
            }}
            onCancel={() => setShowFolderPicker(false)}
          />
        )}

        {showModelConfig && (
          <ModelConfigPage onClose={() => setShowModelConfig(false)} />
        )}

        {showApiKeyDialog && (
          <ApiKeyDialog onClose={() => setShowApiKeyDialog(false)} />
        )}

        {showMcpPanel && (
          <SettingsPanel onClose={() => setShowMcpPanel(false)} />
        )}

        {confirmDelete && (
          <ConfirmDialog
            title="删除项目"
            message={`确定删除项目「${confirmDelete.name}」吗？所有相关数据将被永久删除。`}
            confirmLabel="删除"
            danger
            onConfirm={handleConfirmDelete}
            onCancel={() => setConfirmDelete(null)}
          />
        )}

        {showProjectSettings && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4 animate-fade-in" onClick={() => setShowProjectSettings(false)}>
            <div className="w-full max-w-md bg-white rounded-gmLg border border-g-border shadow-gm-pop p-5 animate-scale-in" onClick={(e) => e.stopPropagation()}>
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-[14px] font-semibold text-g-fg">项目设置 — {currentProject?.name}</h3>
                <button type="button" onClick={() => setShowProjectSettings(false)} className="text-g-fg-4 hover:text-g-fg rounded-full p-1 transition-colors">
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
              <label className="block text-xs text-g-fg-2 mb-1">
                外部参考目录（只读，逗号分隔；agent 的 read_file/grep/list 可读，不授予写）
              </label>
              <textarea
                value={readDirsDraft}
                onChange={(e) => setReadDirsDraft(e.target.value)}
                rows={4}
                placeholder={"D:\\参考资料\nD:\\共享组件库"}
                className="w-full px-3 py-2 text-[12px] text-g-fg bg-g-bg-muted border border-g-border rounded-gm focus:border-g-blue focus:ring-1 focus:ring-g-blue/30 outline-none resize-y"
              />
              <label className="block text-xs text-g-fg-2 mb-1 mt-3">
                附加可写目录（仅 ACL 沙箱 on 时生效；逗号分隔；agent 受限令牌可写，拒绝系统目录/项目 .hiveweave 内）
              </label>
              <textarea
                value={writableDirsDraft}
                onChange={(e) => setWritableDirsDraft(e.target.value)}
                rows={4}
                placeholder={"D:\\素材库\nD:\\共享可写目录"}
                className="w-full px-3 py-2 text-[12px] text-g-fg bg-g-bg-muted border border-g-border rounded-gm focus:border-g-blue focus:ring-1 focus:ring-g-blue/30 outline-none resize-y"
              />
              <label className="block text-xs text-g-fg-2 mb-1 mt-3">
                沙箱模式（ACL 沙箱 on 时生效；默认继承环境，危险全开放=信任项目跳过沙箱）
              </label>
              <select
                value={sandboxModeDraft}
                onChange={(e) => setSandboxModeDraft(e.target.value)}
                className="w-full px-3 py-2 text-[12px] text-g-fg bg-g-bg-muted border border-g-border rounded-gm focus:border-g-blue focus:ring-1 focus:ring-g-blue/30 outline-none"
              >
                <option value="">继承环境（默认启用沙箱）</option>
                <option value="danger-full-access">danger-full-access（逃生门，跳过沙箱）</option>
              </select>
              <div className="flex justify-end gap-2 mt-4">
                <button type="button" onClick={() => setShowProjectSettings(false)} className="px-3 py-1.5 text-[12px] text-g-fg-2 hover:text-g-fg rounded-gm transition-colors">
                  取消
                </button>
                <button type="button" disabled={savingReadDirs} onClick={saveProjectDirs} className="px-3 py-1.5 text-[12px] text-white bg-g-blue hover:bg-g-blue/90 rounded-gm transition-all disabled:opacity-50">
                  {savingReadDirs ? "保存中..." : "保存"}
                </button>
              </div>
            </div>
          </div>
        )}

        <QuestionDialog />
        {showNewProjectDialog && newProjectCEO && (
          <NewProjectDialog
            ceoAgentId={newProjectCEO}
            onClose={() => {
              setShowNewProjectDialog(false);
              setNewProjectCEO(null);
            }}
          />
        )}
      </Suspense>
      </ErrorBoundary>
      <AssistantBall />
      <ToastContainer />
    </div>
  );
}

export default App;
