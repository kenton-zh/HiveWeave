import { useState, useRef, useEffect } from "react";
import OrgTree from "./components/OrgTree";
import ChatPanel from "./components/ChatPanel";
import WorkLogPanel from "./components/WorkLogPanel";
import AgentDetailPanel from "./components/AgentDetailPanel";
import MonitorPanel from "./components/MonitorPanel";
import AddAgentDialog from "./components/AddAgentDialog";
import FolderPicker from "./components/FolderPicker";
import OfficeView from "./components/OfficeView";
import ModelSettings from "./components/ModelSettings";
import GoalsPanel from "./components/GoalsPanel";
import ProjectTimeBadge from "./components/ProjectTimeBadge";
import QuestionDialog from "./components/QuestionDialog";
import NewProjectDialog from "./components/NewProjectDialog";
import { useAppStore } from "./store";
import { getProjects, createProject, deleteProject, subscribeAgentStatus, pauseSystem, resumeSystem, getPausedState, getProjectGameTime, getSettings, updateSettings, type Project } from "./api";

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
  const showAddAgent = useAppStore((s) => s.showAddAgent);
  const addAgentParentId = useAppStore((s) => s.addAgentParentId);
  const openAddAgent = useAppStore((s) => s.openAddAgent);
  const closeAddAgent = useAppStore((s) => s.closeAddAgent);
  const activeView = useAppStore((s) => s.activeView);
  const setActiveView = useAppStore((s) => s.setActiveView);
  const rightPanelTab = useAppStore((s) => s.rightPanelTab);
  const setRightPanelTab = useAppStore((s) => s.setRightPanelTab);

  const setProcessingAgents = useAppStore((s) => s.setProcessingAgents);
  const updateProcessingAgent = useAppStore((s) => s.updateProcessingAgent);

  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState(userName);
  const nameInputRef = useRef<HTMLInputElement>(null);

  // Project selector state
  const [showProjectMenu, setShowProjectMenu] = useState(false);
  const [showFolderPicker, setShowFolderPicker] = useState(false);
  const [showModelSettings, setShowModelSettings] = useState(false);
  const [showNewProjectDialog, setShowNewProjectDialog] = useState(false);
  const [newProjectCEO, setNewProjectCEO] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const projectMenuRef = useRef<HTMLDivElement>(null);

  // Load projects on mount
  useEffect(() => {
    async function load() {
      try {
        const list = await getProjects();
        setProjects(list);
        if (list.length > 0) {
          // Validate selectedProjectId — if it doesn't exist in the list, switch to the first one
          const current = useAppStore.getState().selectedProjectId;
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

  // Subscribe to real-time agent processing status
  useEffect(() => {
    const controller = subscribeAgentStatus(
      (agentIds, paused) => {
        setProcessingAgents(agentIds);
        if (paused !== undefined) setPaused(paused);
        // Signal socket reconnect so ChatPanel can reset stale isStreaming state
        useAppStore.getState().bumpSocketReconnect();
      },
      (agentId, processing) => updateProcessingAgent(agentId, processing),
      (event) => {
        useAppStore.getState().addActivity(event as any);
      },
      () => refreshOrgTree(),
    );
    return () => controller.abort();
  }, []);

  // Check system pause state on mount
  useEffect(() => {
    getPausedState().then((s) => setPaused(s.paused)).catch(() => {});
  }, []);

  // Load operator name from global settings (overrides localStorage)
  useEffect(() => {
    getSettings().then((settings) => {
      if (settings.operatorName) {
        setUserName(settings.operatorName);
      }
    }).catch(() => {});
  }, []);

  const handlePause = async () => {
    if (paused) {
      await resumeSystem();
      setPaused(false);
    } else {
      await pauseSystem();
      setPaused(true);
    }
  };

  const currentProject = projects.find((p) => p.id === selectedProjectId);

  const handleSwitchProject = (id: string) => {
    setSelectedProjectId(id);
    setSelectedAgent(null);
    setRightPanelTab("chat");
    clearChatSessions();
    refreshOrgTree();
    setShowProjectMenu(false);
  };

  const handleCreateProjectFromFolder = async (folderPath: string) => {
    // Check if a project with this workspace path already exists
    const normalizedPath = folderPath.replace(/\\/g, "/");
    const existing = projects.find(
      (p) => p.workspacePath?.replace(/\\/g, "/") === normalizedPath
    );
    if (existing) {
      // Switch to the existing project instead of creating a duplicate
      setSelectedProjectId(existing.id);
      setSelectedAgent(null);
      clearChatSessions();
      refreshOrgTree();
      setShowProjectMenu(false);
      return;
    }

    const name = folderPath.split(/[\\/]/).filter(Boolean).pop() || "New Project";
    try {
      const { project, mainAgentId } = await createProject(name, folderPath, undefined, undefined, "zh");
      const updated = await getProjects();
      setProjects(updated);
      setSelectedProjectId(project.id);
      setSelectedAgent(null);
      clearChatSessions();
      refreshOrgTree();
      setShowProjectMenu(false);
      // Show the new-project onboarding dialog
      if (mainAgentId) {
        setNewProjectCEO(mainAgentId);
        setShowNewProjectDialog(true);
      }
    } catch (err) {
      console.error("Failed to create project:", err);
    }
  };

  const handleDeleteProject = async (id: string) => {
    const proj = projects.find((p) => p.id === id);
    if (!proj) return;
    if (!confirm(`确定删除项目「${proj.name}」吗？所有相关数据将被永久删除。`)) return;
    try {
      await deleteProject(id);
      const updated = await getProjects();
      setProjects(updated);
      if (selectedProjectId === id) {
        const next = updated[0]?.id || null;
        setSelectedProjectId(next);
        setSelectedAgent(null);
        clearChatSessions();
        refreshOrgTree();
      }
      setShowProjectMenu(false);
    } catch (err) {
      console.error("Failed to delete project:", err);
      alert(`删除项目失败: ${err instanceof Error ? err.message : "未知错误"}`);
    }
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

  return (
    <div className="h-screen flex flex-col bg-surface">
      {/* Top Bar */}
      <header className="h-14 border-b border-surface-border flex items-center px-6 bg-surface-card shrink-0">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-accent flex items-center justify-center shrink-0">
            <span className="text-white font-bold text-sm">H</span>
          </div>
          <h1 className="text-lg font-semibold text-gray-100 tracking-tight shrink-0">
            HiveWeave
          </h1>
          <ProjectTimeBadge projectId={selectedProjectId} />
        </div>

        {/* Project Selector */}
        <div className="ml-6 relative" ref={projectMenuRef}>
          <button
            onClick={() => setShowProjectMenu(!showProjectMenu)}
            className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-surface border border-surface-border hover:border-accent/50 transition-colors text-sm text-gray-200"
          >
            <svg className="w-4 h-4 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
            </svg>
            <span className="max-w-[120px] truncate">{currentProject?.name || "选择项目"}</span>
            <svg className="w-3 h-3 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          {showProjectMenu && (
            <div className="absolute top-full left-0 mt-1 w-56 bg-surface-card border border-surface-border rounded-lg shadow-xl z-50 py-1">
              {projects.map((p) => (
                <div
                  key={p.id}
                  className={`flex items-center justify-between px-3 py-2 text-sm cursor-pointer hover:bg-surface transition-colors ${
                    p.id === selectedProjectId ? "text-accent bg-accent/5" : "text-gray-300"
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
                    onClick={(e) => { e.stopPropagation(); handleDeleteProject(p.id); }}
                    className="ml-2 text-gray-500 hover:text-red-400 transition-colors"
                    title="删除项目"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  </button>
                </div>
              ))}

              <div className="border-t border-surface-border mt-1 pt-1">
                <button
                  onClick={() => setShowFolderPicker(true)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-400 hover:text-accent hover:bg-surface transition-colors"
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

        <div className="ml-auto flex items-center gap-4">
          {/* Model Settings gear icon */}
          <button
            onClick={() => setShowModelSettings(true)}
            className="text-gray-400 hover:text-gray-200 transition-colors"
            title="Model Settings"
          >
            <svg className="w-4.5 h-4.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
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
              className="w-24 px-2 py-1 text-xs bg-surface border border-accent/50 rounded text-gray-200 focus:outline-none focus:border-accent"
            />
          ) : (
            <button
              onClick={startEditName}
              className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-200 transition-colors"
              title="点击修改你的名称"
            >
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
              </svg>
              <span>{userName}</span>
            </button>
          )}
          <button
            onClick={handlePause}
            className={`flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-colors ${
              paused
                ? "bg-red-500/15 text-red-400 hover:bg-red-500/25"
                : "bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25"
            }`}
            title={paused ? "点击上班，恢复所有 Agent" : "点击下班，暂停所有 Agent"}
          >
            <span className={`w-2 h-2 rounded-full ${paused ? "bg-red-400" : "bg-emerald-400 animate-pulse"}`} />
            <span>{paused ? "已下班" : "上班中"}</span>
          </button>
        </div>
      </header>

      {/* Main Content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left Panel - Org Tree / Office */}
        <div className="flex-1 border-r border-surface-border flex flex-col">
          <div className="px-4 py-3 border-b border-surface-border bg-surface-card flex items-center gap-3">
            {/* View tabs */}
            <div className="flex gap-1 bg-surface rounded-md p-0.5">
              <button
                onClick={() => setActiveView("tree")}
                className={`px-3 py-1 text-xs rounded-md transition-colors ${
                  activeView === "tree"
                    ? "bg-accent/20 text-accent"
                    : "text-gray-400 hover:text-gray-200"
                }`}
              >
                Org Tree
              </button>
              <button
                onClick={() => setActiveView("office")}
                className={`px-3 py-1 text-xs rounded-md transition-colors ${
                  activeView === "office"
                    ? "bg-accent/20 text-accent"
                    : "text-gray-400 hover:text-gray-200"
                }`}
              >
                Office
              </button>
            </div>
            {selectedProjectId && activeView === "tree" && (
              <button
                onClick={() => openAddAgent(null)}
                className="flex items-center gap-1 px-2 py-1 text-xs text-gray-400 hover:text-accent hover:bg-accent/10 rounded-md transition-colors ml-auto"
                title="Create Agent"
              >
                <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                </svg>
                Agent
              </button>
            )}
          </div>
          <div className="flex-1 overflow-hidden">
            {activeView === "tree" ? <OrgTree /> : <OfficeView />}
          </div>
        </div>

        {/* Right Panel - Chat / Agent / Logs */}
        <div className="w-2/5 flex flex-col">
          {/* Tab bar */}
          <div className="px-4 py-2 border-b border-surface-border bg-surface-card flex items-center gap-1">
            <button
              onClick={() => setRightPanelTab("goals")}
              className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
                rightPanelTab === "goals"
                  ? "bg-accent/20 text-accent"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              Goals
            </button>
            {selectedAgentId && (
              <>
                <button
                  onClick={() => setRightPanelTab("chat")}
                  className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
                    rightPanelTab === "chat"
                      ? "bg-accent/20 text-accent"
                      : "text-gray-400 hover:text-gray-200"
                  }`}
                >
                  Chat
                </button>
                <button
                  onClick={() => setRightPanelTab("agent")}
                  className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
                    rightPanelTab === "agent"
                      ? "bg-accent/20 text-accent"
                      : "text-gray-400 hover:text-gray-200"
                  }`}
                >
                  Agent
                </button>
                <button
                  onClick={() => setRightPanelTab("logs")}
                  className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
                    rightPanelTab === "logs"
                      ? "bg-accent/20 text-accent"
                      : "text-gray-400 hover:text-gray-200"
                  }`}
                >
                  Logs
                </button>
                <button
                  onClick={() => setRightPanelTab("monitor")}
                  className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
                    rightPanelTab === "monitor"
                      ? "bg-accent/20 text-accent"
                      : "text-gray-400 hover:text-gray-200"
                  }`}
                >
                  监控
                </button>
              </>
            )}
          </div>

          {/* Tab content */}
          <div className="flex-1 overflow-hidden">
            {rightPanelTab === "goals" ? (
              selectedProjectId ? (
                <GoalsPanel projectId={selectedProjectId} />
              ) : (
                <div className="h-full flex items-center justify-center text-gray-500 text-sm">
                  请先选择一个项目
                </div>
              )
            ) : !selectedAgentId ? (
              <div className="h-full flex items-center justify-center text-gray-500 text-sm">
                <div className="text-center">
                  <div className="w-16 h-16 rounded-full bg-surface-card border border-surface-border flex items-center justify-center mx-auto mb-4">
                    <svg className="w-8 h-8 text-gray-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
                    </svg>
                  </div>
                  <p>从左侧 Org Tree 选择一个 Agent</p>
                </div>
              </div>
            ) : rightPanelTab === "chat" ? (
              <ChatPanel key={selectedAgentId} agentId={selectedAgentId} />
            ) : rightPanelTab === "agent" ? (
              <AgentDetailPanel agentId={selectedAgentId} />
            ) : rightPanelTab === "monitor" ? (
              <MonitorPanel key={selectedAgentId} agentId={selectedAgentId} />
            ) : (
              <WorkLogPanel key={selectedAgentId} agentId={selectedAgentId} />
            )}
          </div>
        </div>
      </div>

      {/* Add Agent Dialog */}
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

      {/* Folder Picker for workspace selection */}
      {showFolderPicker && (
        <FolderPicker
          onSelect={(path) => {
            handleCreateProjectFromFolder(path);
            setShowFolderPicker(false);
          }}
          onCancel={() => setShowFolderPicker(false)}
        />
      )}

      {/* Model Settings Modal */}
      {showModelSettings && (
        <ModelSettings onClose={() => setShowModelSettings(false)} />
      )}

      {/* Question Dialog — global, polled */}
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
    </div>
  );
}

export default App;
