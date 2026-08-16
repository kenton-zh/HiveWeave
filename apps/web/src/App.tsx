import { useState, useRef, useEffect, Suspense } from "react";
import OrgTree from "./components/OrgTree";
import ChatPanel from "./components/ChatPanel";
import ProjectTimeBadge from "./components/ProjectTimeBadge";
import ToastContainer from "./components/Toast";
import TokenUsagePanel from "./components/TokenUsagePanel";
import { lazyRetry, resolveLeftPanel } from "./mainPanel";

// Lazy-loaded: only fetched when the user navigates to them.
// OfficeView alone pulls in pixi.js (~1MB); dialogs are interaction-only.
// TokenUsagePanel is small — static import. A rejected React.lazy() promise
// stays rejected for the session, which is the stuck English "Loading..." on Token.
const WorkLogPanel = lazyRetry(() => import("./components/WorkLogPanel"));
const AgentDetailPanel = lazyRetry(() => import("./components/AgentDetailPanel"));
const MonitorPanel = lazyRetry(() => import("./components/MonitorPanel"));
const DebugPanel = lazyRetry(() => import("./components/DebugPanel"));
const AddAgentDialog = lazyRetry(() => import("./components/AddAgentDialog"));
const FolderPicker = lazyRetry(() => import("./components/FolderPicker"));
const OfficeView = lazyRetry(() => import("./components/OfficeView"));
const ModelSettings = lazyRetry(() => import("./components/ModelSettings"));
const ModelConfigPage = lazyRetry(() => import("./components/ModelConfigPage"));
const ApiKeyDialog = lazyRetry(() => import("./components/ApiKeyDialog"));
const GoalsPanel = lazyRetry(() => import("./components/GoalsPanel"));
const QuestionDialog = lazyRetry(() => import("./components/QuestionDialog"));
const NewProjectDialog = lazyRetry(() => import("./components/NewProjectDialog"));
const ConfirmDialog = lazyRetry(() => import("./components/ConfirmDialog"));
const TimelineView = lazyRetry(() => import("./components/timeline/TimelineView"));
const TaskTimelinePanel = lazyRetry(() => import("./components/timeline/TaskTimelinePanel"));
import { useAppStore } from "./store";
import { getProjects, createProject, deleteProject, leaveAgentChannel, subscribeAgentStatus, activateProject, deactivateProject, getProjectGameTime, getSettings, updateSettings, initApiKeyFromStorage, restartBackend, restartFrontend } from "./api";
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
  const socketReconnectVersion = useAppStore((s) => s.socketReconnectVersion);
  const showAddAgent = useAppStore((s) => s.showAddAgent);
  const addAgentParentId = useAppStore((s) => s.addAgentParentId);
  const openAddAgent = useAppStore((s) => s.openAddAgent);
  const closeAddAgent = useAppStore((s) => s.closeAddAgent);
  const activeView = useAppStore((s) => s.activeView);
  const setActiveView = useAppStore((s) => s.setActiveView);
  const rightPanelTab = useAppStore((s) => s.rightPanelTab);
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
  const [showModelSettings, setShowModelSettings] = useState(false);
  const [showModelConfig, setShowModelConfig] = useState(false);
  const [showApiKeyDialog, setShowApiKeyDialog] = useState(false);
  const [showNewProjectDialog, setShowNewProjectDialog] = useState(false);
  const [newProjectCEO, setNewProjectCEO] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null);
  const [projectStarting, setProjectStarting] = useState(false);
  const projectMenuRef = useRef<HTMLDivElement>(null);
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

  // Subscribe to real-time agent processing status
  useEffect(() => {
    let lastSnapshotKey = "";
    const controller = subscribeAgentStatus(
      (agentIds) => {
        const key = agentIds.slice().sort().join(",");
        if (key === lastSnapshotKey) return;
        lastSnapshotKey = key;
        setProcessingAgents(agentIds);
        useAppStore.getState().bumpSocketReconnect();
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

  const handleToggleProjectStart = async () => {
    if (!selectedProjectId || projectStarting) return;
    setProjectStarting(true);
    try {
      const isStarted = currentProject?.isStarted;
      if (isStarted) {
        await deactivateProject(selectedProjectId);
        showToast("已下班，Agent 已暂停", "info");
      } else {
        await activateProject(selectedProjectId);
        showToast("已上班，Agent 已启动", "info");
      }
      // 刷新项目列表以获取最新 isStarted 状态
      const list = await getProjects();
      setProjects(list);
    } catch (err) {
      console.error("Toggle project start failed:", err);
      showToast("操作失败", "error");
    } finally {
      setProjectStarting(false);
    }
  };

  const currentProject = projects.find((p) => p.id === selectedProjectId);

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

  return (
    <div className="h-screen flex flex-col bg-g-bg-soft">
      {/* Top Bar */}
      <header className="h-14 border-b border-g-border flex items-center px-5 bg-white shrink-0 relative z-30">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-g-blue to-violet-600 flex items-center justify-center shrink-0 shadow-gm-sm transition-transform duration-200 hover:scale-105">
            <span className="text-white font-bold text-sm">H</span>
          </div>
          <h1 className="text-[15px] font-semibold text-g-fg tracking-tight shrink-0 select-none">
            HiveWeave
          </h1>
          <ProjectTimeBadge projectId={selectedProjectId} />
        </div>

        {/* Project Selector */}
        <div className="ml-6 relative" ref={projectMenuRef}>
          <button
            onClick={() => setShowProjectMenu(!showProjectMenu)}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-gm bg-white border transition-all duration-200 text-[13px] text-g-fg shadow-gm-sm hover:shadow-gm active:scale-[0.98] ${
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
            <div className="absolute top-full left-0 mt-1.5 w-56 bg-white border border-g-border rounded-gmLg shadow-gm-pop z-50 py-1.5 px-1 animate-scale-in origin-top-left">
              {projects.map((p) => (
                <div
                  key={p.id}
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
                    className="ml-2 text-g-fg-4 hover:text-red-600 hover:bg-red-50 rounded-full p-1 transition-all disabled:opacity-30 disabled:cursor-not-allowed"
                    title={deletingProjectId === p.id ? "删除中..." : "删除项目"}
                  >
                    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  </button>
                </div>
              ))}

              <div className="border-t border-g-border mt-1 pt-1">
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
              className="text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted rounded-md p-1.5 transition-all active:scale-90"
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
              className="text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted rounded-md p-1.5 transition-all active:scale-90"
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
            className="flex items-center gap-1.5 px-3 py-1.5 text-[13px] font-medium text-g-fg-2 bg-white border border-g-border rounded-gm shadow-gm-sm hover:text-g-blue hover:border-g-blue/40 hover:shadow-gm transition-all duration-200 active:scale-[0.97]"
            title="模型配置"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 21v-7m0-4V3m8 18v-9m0-4V3m8 18v-5m0-4V3M1 14h6m2-6h6m2 8h6" />
            </svg>
            模型配置
          </button>
          {/* Model Settings gear icon */}
          <button
            onClick={() => setShowModelSettings(true)}
            className="text-g-fg-3 hover:text-g-blue hover:bg-g-blue-bg/60 rounded-full p-1.5 transition-all duration-200 active:scale-90 active:rotate-45"
            title="Model Settings"
          >
            <svg className="w-4.5 h-4.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </button>
          {/* API Key 设置 */}
          <button
            onClick={() => setShowApiKeyDialog(true)}
            className="text-g-fg-3 hover:text-g-blue hover:bg-g-blue-bg/60 rounded-full p-1.5 transition-all active:scale-90"
            title="API Key 设置"
          >
            <svg className="w-4.5 h-4.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 7a4 4 0 11-8 0 4 4 0 018 0zM12 15v6m-3-3h6" />
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

      {/* Main Content — floating panels on a gray workbench */}
      <div className="flex flex-1 overflow-hidden gap-3 p-3">
        {/* Left Panel - Org Tree / Office */}
        <div className="flex-1 flex flex-col bg-white rounded-gmLg border border-g-border shadow-gm-sm overflow-hidden">
          <div className="px-3 py-2.5 border-b border-g-border bg-white flex items-center gap-3">
            {/* View tabs */}
            <div className="hw-segment">
              <button
                onClick={() => setActiveView("tree")}
                className={`hw-segment-item ${activeView === "tree" ? "hw-segment-item-active" : ""}`}
              >
                Org Tree
              </button>
              <button
                onClick={() => setActiveView("office")}
                className={`hw-segment-item ${activeView === "office" ? "hw-segment-item-active" : ""}`}
              >
                Office
              </button>
              <button
                onClick={() => setActiveView("timeline")}
                className={`hw-segment-item ${activeView === "timeline" ? "hw-segment-item-active" : ""}`}
              >
                Timeline
              </button>
              {selectedProjectId && (
                <button
                  onClick={() => setActiveView("token")}
                  className={`hw-segment-item ${activeView === "token" ? "hw-segment-item-active" : ""}`}
                >
                  Token
                </button>
              )}
            </div>

            {/* Project-level start/stop button */}
            {selectedProjectId && (
              <button
                onClick={handleToggleProjectStart}
                disabled={projectStarting}
                className={`flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full transition-all duration-200 ml-auto active:scale-[0.97] ${
                  currentProject?.isStarted
                    ? "bg-g-green-bg text-g-green border border-g-green/20 hover:border-g-green/40"
                    : "bg-g-bg-soft text-g-fg-3 border border-g-border hover:text-g-fg hover:border-g-border-strong"
                } disabled:opacity-50 disabled:cursor-not-allowed`}
                title={currentProject?.isStarted ? "点击下班，暂停该项目所有 Agent" : "点击上班，启动该项目所有 Agent"}
              >
                <span className="relative flex w-2 h-2">
                  {currentProject?.isStarted && (
                    <span className="absolute inline-flex h-full w-full rounded-full bg-emerald-400 animate-ping-ring" />
                  )}
                  <span className={`relative inline-flex w-2 h-2 rounded-full ${currentProject?.isStarted ? "bg-emerald-500" : "bg-g-fg-4"}`} />
                </span>
                <span>{projectStarting ? "处理中..." : currentProject?.isStarted ? "上班中" : "已下班"}</span>
              </button>
            )}

            {selectedProjectId && activeView === "tree" && (
              <button
                onClick={() => openAddAgent(null)}
                className="flex items-center gap-1 px-2.5 py-1 text-xs text-g-fg-3 hover:text-g-blue hover:bg-g-blue-bg rounded-gm transition-all active:scale-95"
                title="Create Agent"
              >
                <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                </svg>
                Agent
              </button>
            )}
          </div>
          <div className="flex-1 overflow-hidden bg-app-tint">
            <div key={activeView} className="hw-tab-in h-full">
              {leftPanel === "tree" ? (
                <OrgTree />
              ) : leftPanel === "timeline" ? (
                <Suspense fallback={<div className="h-full flex items-center justify-center text-g-fg-3 text-sm animate-pulse-soft">Loading...</div>}>
                  <TimelineView />
                </Suspense>
              ) : leftPanel === "token" && selectedProjectId ? (
                <TokenUsagePanel key={selectedProjectId} projectId={selectedProjectId} />
              ) : leftPanel === "token-empty" ? (
                <div className="h-full flex items-center justify-center text-g-fg-3 text-sm">
                  请先选择一个项目
                </div>
              ) : (
                <Suspense fallback={<div className="h-full flex items-center justify-center text-g-fg-3 text-sm animate-pulse-soft">Loading...</div>}>
                  <OfficeView />
                </Suspense>
              )}
            </div>
          </div>
        </div>

        {/* Right Panel - Chat / Agent / Logs */}
        <div className="w-2/5 flex flex-col bg-white rounded-gmLg border border-g-border shadow-gm-sm overflow-hidden">
          {/* Tab bar */}
          <div className="px-3 py-2.5 border-b border-g-border bg-white flex items-center gap-1">
            <div className="hw-segment mr-2">
            <button
              onClick={() => setRightPanelTab("goals")}
              className={`hw-segment-item ${rightPanelTab === "goals" ? "hw-segment-item-active" : ""}`}
            >
              Goals
            </button>
            {selectedAgentId && (
              <>
                <button
                  onClick={() => setRightPanelTab("chat")}
                  className={`hw-segment-item ${rightPanelTab === "chat" ? "hw-segment-item-active" : ""}`}
                >
                  Chat
                </button>
                <button
                  onClick={() => setRightPanelTab("agent")}
                  className={`hw-segment-item ${rightPanelTab === "agent" ? "hw-segment-item-active" : ""}`}
                >
                  Agent
                </button>
                <button
                  onClick={() => setRightPanelTab("logs")}
                  className={`hw-segment-item ${rightPanelTab === "logs" ? "hw-segment-item-active" : ""}`}
                >
                  Logs
                </button>
                <button
                  onClick={() => setRightPanelTab("monitor")}
                  className={`hw-segment-item ${rightPanelTab === "monitor" ? "hw-segment-item-active" : ""}`}
                >
                  监控
                </button>
                {selectedAgentId && (
                  <button
                    onClick={() => setRightPanelTab("debug")}
                    className={`hw-segment-item ${rightPanelTab === "debug" ? "hw-segment-item-active" : ""}`}
                  >
                    调试
                  </button>
                )}
              </>
            )}
            {selectedTaskId && (
              <button
                onClick={() => setRightPanelTab("task")}
                className={`hw-segment-item ${rightPanelTab === "task" ? "hw-segment-item-active" : ""}`}
              >
                任务
              </button>
            )}
            </div>
          </div>

          {/* Tab content */}
          <div className="flex-1 overflow-hidden">
            {rightPanelTab === "task" && selectedTaskId ? (
              <Suspense fallback={<div className="h-full flex items-center justify-center text-g-fg-3 text-sm animate-pulse-soft">Loading...</div>}>
                <div key="panel-task" className="hw-tab-in h-full">
                  <TaskTimelinePanel />
                </div>
              </Suspense>
            ) : !selectedAgentId ? (
              <div className="h-full flex items-center justify-center text-g-fg-3 text-sm animate-fade-in">
                <div className="text-center">
                  <div className="w-16 h-16 rounded-full bg-gradient-to-br from-g-bg-soft to-g-bg-muted border border-g-border flex items-center justify-center mx-auto mb-4 shadow-gm-sm ring-4 ring-g-bg-muted/50">
                    <svg className="w-8 h-8 text-g-fg-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
                    </svg>
                  </div>
                  <p className="text-g-fg-3 font-medium">从左侧 Org Tree 选择一个 Agent</p>
                  <p className="text-g-fg-4 text-xs mt-1.5">选择后即可查看对话、目标与日志</p>
                </div>
              </div>
            ) : (
              <>
                <ChatPanel key="panel-chat" agentId={selectedAgentId} hidden={rightPanelTab !== "chat"} />
                <Suspense fallback={<div className="h-full flex items-center justify-center text-g-fg-3 text-sm animate-pulse-soft">Loading...</div>}>
                  {rightPanelTab === "goals" && selectedProjectId && (
                    <div key="panel-goals" className="hw-tab-in h-full">
                      <GoalsPanel projectId={selectedProjectId} />
                    </div>
                  )}
                  {rightPanelTab === "goals" && !selectedProjectId && (
                    <div key="panel-goals-empty" className="hw-tab-in h-full flex items-center justify-center text-g-fg-3 text-sm">
                      请先选择一个项目
                    </div>
                  )}
                  {rightPanelTab === "agent" && (
                    <div key="panel-agent" className="hw-tab-in h-full">
                      <AgentDetailPanel agentId={selectedAgentId} />
                    </div>
                  )}
                  {rightPanelTab === "monitor" && (
                    <div key="panel-monitor" className="hw-tab-in h-full">
                      <MonitorPanel agentId={selectedAgentId} />
                    </div>
                  )}
                  {rightPanelTab === "debug" && (
                    <div key="panel-debug" className="hw-tab-in h-full">
                      <DebugPanel />
                    </div>
                  )}
                  {rightPanelTab === "logs" && (
                    <div key="panel-logs" className="hw-tab-in h-full">
                      <WorkLogPanel agentId={selectedAgentId} />
                    </div>
                  )}
                </Suspense>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Lazy-loaded dialogs — wrapped in Suspense, fallback=null since they're overlays */}
      <Suspense fallback={null}>
        {showAddAgent && selectedProjectId && (
          <AddAgentDialog
            projectId={selectedProjectId}
            parentId={addAgentParentId}
            onClose={closeAddAgent}
            onCreated={() => {
              closeAddAgent();
              refreshOrgTree();
            }}
          />
        )}

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

        {showModelSettings && (
          <ModelSettings onClose={() => setShowModelSettings(false)} />
        )}

        {showModelConfig && (
          <ModelConfigPage onClose={() => setShowModelConfig(false)} />
        )}

        {showApiKeyDialog && (
          <ApiKeyDialog onClose={() => setShowApiKeyDialog(false)} />
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
      <ToastContainer />
    </div>
  );
}

export default App;
