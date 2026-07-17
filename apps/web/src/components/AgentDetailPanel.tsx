import { useState, useEffect, useCallback } from "react";
import { getAgent, updateAgent, getPermissionRules, getModels } from "../api";
import type { LlmModel } from "../api";
import { useAppStore } from "../store";

// Safely parse a JSON array field that may be a string, array, or null.
function safeJsonArray(val: unknown): string[] {
  if (Array.isArray(val)) return val as string[];
  if (typeof val === "string") {
    try { const p = JSON.parse(val); return Array.isArray(p) ? p : []; } catch { return []; }
  }
  return [];
}

interface AgentDetail {
  id: string;
  shortId: string | null;
  name: string;
  role: string;
  status: string;
  goal: string;
  backstory: string;
  parentId: string | null;
  projectId: string | null;
  permissionType: string;
  permissionMode: string;
  allowedTools: string[];
  deniedTools: string[];
  askTools: string[];
  mcpServers: string[];
  boundSkills: string[];
  modelId: string | null;
  reasoningEffort: string | null;
  createdAt: number;
  updatedAt: number;
}

interface PermissionRules {
  permissionMode: string;
  allowedTools: string[];
  deniedTools: string[];
  askTools: string[];
  mcpServers: string[];
  boundSkills: string[];
}

const STATUS_CONFIG: Record<string, { label: string; color: string; desc: string }> = {
  created: { label: "待激活", color: "text-g-fg-3", desc: "Agent 已创建，尚未开始工作" },
  active: { label: "工作中", color: "text-emerald-700", desc: "Agent 正在执行任务" },
  promoted: { label: "已晋升", color: "text-blue-700", desc: "Agent 已晋升为协调者" },
  receiving: { label: "交接中", color: "text-amber-700", desc: "正在接收工作交接" },
  merging: { label: "合并中", color: "text-purple-700", desc: "代码正在合并" },
  dissolving: { label: "解散中", color: "text-red-600", desc: "Agent 正在解散" },
  archived: { label: "已归档", color: "text-g-fg-4", desc: "Agent 已归档，不再活跃" },
};

const PERMISSION_MODES = [
  { value: "readonly", label: "只读", desc: "只能读取文件，不能修改或执行" },
  { value: "readwrite", label: "读写", desc: "可以读取和修改文件，不能执行命令" },
  { value: "full", label: "完全", desc: "所有权限，包括执行命令" },
];

const ROLE_LABELS: Record<string, string> = {
  ceo: "CEO",
  hr: "HR",
  architect: "Architect",
  manager: "Manager",
  developer: "Developer",
  module_dev: "Developer",
  qa: "QA",
  devops: "DevOps",
};

export default function AgentDetailPanel({ agentId }: { agentId: string }) {
  const [agent, setAgent] = useState<AgentDetail | null>(null);
  const [permissions, setPermissions] = useState<PermissionRules | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Editing state
  const [editingGoal, setEditingGoal] = useState(false);
  const [editingBackstory, setEditingBackstory] = useState(false);
  const [goalDraft, setGoalDraft] = useState("");
  const [backstoryDraft, setBackstoryDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [models, setModels] = useState<LlmModel[]>([]);
  const [resolvedModel, setResolvedModel] = useState<{ modelName: string; modelId: string } | null>(null);

  const refreshOrgTree = useAppStore((s) => s.refreshOrgTree);
  const setSelectedAgent = useAppStore((s) => s.setSelectedAgent);
  const processingAgents = useAppStore((s) => s.processingAgents);
  const orgTreeVersion = useAppStore((s) => s.orgTreeVersion);

  // Fetch agent details
  const fetchAgent = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const raw = await getAgent(agentId);
      // The Elixir backend wraps the response: `%{agent: serialize_agent(a)}`.
      // Some endpoints (e.g. OrgTree) return the agent fields at the top level.
      // Accept both shapes so missing `id` doesn't crash the render.
      const data = (raw && typeof raw === "object" && "agent" in raw && raw.agent) ? raw.agent : raw;
      if (!data || typeof data !== "object" || !data.id) {
        setError("Agent 不存在");
        setLoading(false);
        return;
      }
      setAgent({
        ...data,
        allowedTools: safeJsonArray(data?.allowedTools),
        deniedTools: safeJsonArray(data?.deniedTools),
        askTools: safeJsonArray(data?.askTools),
        mcpServers: safeJsonArray(data?.mcpServers),
        boundSkills: safeJsonArray(data?.boundSkills),
      });
      setGoalDraft(data.goal || "");
      setBackstoryDraft(data.backstory || "");

      // Also fetch permission rules
      try {
        const perms = await getPermissionRules(agentId);
        setPermissions(perms);
      } catch {
        // Permissions endpoint might not exist yet, use agent data
        setPermissions({
          permissionMode: data.permissionMode || "full",
          allowedTools: safeJsonArray(data?.allowedTools),
          deniedTools: safeJsonArray(data?.deniedTools),
          askTools: safeJsonArray(data?.askTools),
          mcpServers: safeJsonArray(data?.mcpServers),
          boundSkills: safeJsonArray(data?.boundSkills),
        });
      }
    } catch (err: any) {
      setError(err.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    fetchAgent();
  }, [fetchAgent]);

  // Re-fetch agent details when org tree changes (e.g. status updates,
  // new hires, role changes) so the panel stays in sync without a page reload.
  useEffect(() => {
    if (agentId) fetchAgent();
  }, [orgTreeVersion]);

  // Fetch resolved model when agent has no explicit model
  useEffect(() => {
    if (!agentId) return;
    fetch(`/api/chat/resolved-model/${agentId}`)
      .then((r) => r.json())
      .then((data: any) => {
        if (data?.modelName) setResolvedModel({ modelName: data.modelName, modelId: data.modelId });
      })
      .catch(() => setResolvedModel(null));
  }, [agentId, agent?.modelId]);

  // Load available models
  useEffect(() => {
    getModels().then(setModels).catch(() => {});
  }, []);

  // Change agent model
  const changeModel = async (modelId: string) => {
    if (!agent) return;
    setSaving(true);
    try {
      await updateAgent(agent.id, { modelId: modelId || null });
      setAgent({ ...agent, modelId: modelId || null });
      refreshOrgTree();
    } catch (err: any) {
      setError(`保存失败: ${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  // Save field updates
  const saveField = async (field: string, value: string) => {
    if (!agent) return;
    setSaving(true);
    try {
      await updateAgent(agent.id, { [field]: value });
      setAgent({ ...agent, [field]: value });
      refreshOrgTree();
    } catch (err: any) {
      setError(`保存失败: ${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center text-g-fg-4">
        加载中...
      </div>
    );
  }

  if (error && !agent) {
    return (
      <div className="h-full flex items-center justify-center text-red-600 text-sm p-4 text-center">
        {error}
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="h-full flex items-center justify-center text-g-fg-4">
      Agent 不存在
      </div>
    );
  }

  const statusConfig = STATUS_CONFIG[agent.status] || STATUS_CONFIG.created;
  const isProcessing = processingAgents.includes(agentId);
  // Override status display for "active" agents based on runtime processing state
  const runtimeStatus = agent.status === "active"
    ? isProcessing
      ? { label: "工作中", color: "text-emerald-700", desc: "Agent 正在执行任务" }
      : { label: "空闲", color: "text-g-fg-3", desc: "Agent 已激活，等待任务" }
    : statusConfig;
  const roleLabel = ROLE_LABELS[agent.role] || agent.role;
  const createdAt = new Date(agent.createdAt).toLocaleString("zh-CN");

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-2xl mx-auto p-6 space-y-6">
        {/* Error banner */}
        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-2 rounded-gm shadow-gm-sm text-sm">
            {error}
            <button onClick={() => setError("")} className="ml-2 text-red-600 hover:text-red-700">×</button>
          </div>
        )}

        {/* ─── Profile Section ─── */}
        <section>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-g-fg uppercase tracking-wider flex items-center gap-2"><span className="w-1 h-3.5 rounded-full bg-g-blue/70 shrink-0" />基础信息</h3>
            <span className="text-xs text-g-fg-4 font-mono">{agent.shortId || (agent.id || "").slice(0, 8) || "—"}</span>
          </div>

          <div className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-sm hover:shadow-gm transition-shadow p-5 space-y-4">
            {/* Name + Role */}
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-gmLg bg-g-blue-bg flex items-center justify-center text-g-blue font-bold shadow-gm-sm ring-1 ring-g-blue/20">
                {agent.name.charAt(0).toUpperCase()}
              </div>
              <div>
                <h2 className="text-lg font-semibold text-g-fg">{agent.name}</h2>
                <span className="text-xs text-g-fg-3">{roleLabel} · {agent.permissionType === "coordinator" ? "协调者" : "执行者"}</span>
              </div>
            </div>

            {/* Status */}
            <div className="flex items-center gap-2 px-3 py-2 rounded-gm border border-g-border/60 bg-g-bg-soft">
              <span
                className={`w-2.5 h-2.5 rounded-full ${
                  agent.status === "active"
                    ? isProcessing ? "bg-emerald-400 animate-pulse" : "bg-gray-500"
                    : agent.status === "idle" || agent.status === "inactive" ? "bg-gray-500"
                    : agent.status === "promoted" ? "bg-blue-400"
                    : agent.status === "receiving" ? "bg-amber-400 animate-pulse"
                    : agent.status === "merging" ? "bg-purple-400 animate-pulse"
                    : agent.status === "dissolving" || agent.status === "archived" ? "bg-red-600"
                    : "bg-gray-400"
                }`}
              />
              <span className={`text-sm font-medium ${runtimeStatus.color}`}>{runtimeStatus.label}</span>
              <span className="text-xs text-g-fg-4 ml-1">— {runtimeStatus.desc}</span>
            </div>

            {/* Goal (editable) */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs font-medium text-g-fg-3">目标</label>
                {!editingGoal && (
                  <button
                    onClick={() => { setGoalDraft(agent.goal); setEditingGoal(true); }}
                    className="text-xs text-g-blue hover:text-g-blue/80 transition-colors"
                  >
                    编辑
                  </button>
                )}
              </div>
              {editingGoal ? (
                <div className="space-y-2">
                  <textarea
                    value={goalDraft}
                    onChange={(e) => setGoalDraft(e.target.value)}
                    rows={3}
                    className="w-full px-3 py-2 text-sm bg-g-bg border border-g-blue/40 rounded-lg text-g-fg focus:outline-none focus:border-g-blue resize-none"
                    autoFocus
                  />
                  <div className="flex gap-2 justify-end">
                    <button
                      onClick={() => setEditingGoal(false)}
                      className="px-3 py-1 text-xs text-g-fg-3 hover:text-g-fg rounded-gm hover:bg-g-bg-muted transition-colors"
                    >
                      取消
                    </button>
                    <button
                      onClick={() => { saveField("goal", goalDraft); setEditingGoal(false); }}
                      disabled={saving}
                      className="px-3 py-1 text-xs bg-g-blue text-white rounded-gm shadow-gm-sm hover:bg-blue-600 active:scale-[0.97] transition-all disabled:opacity-50"
                    >
                      保存
                    </button>
                  </div>
                </div>
              ) : (
                <p className="text-sm text-g-fg whitespace-pre-wrap">{agent.goal || "(未设置)"}</p>
              )}
            </div>

            {/* Backstory (editable) */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs font-medium text-g-fg-3">背景故事</label>
                {!editingBackstory && (
                  <button
                    onClick={() => { setBackstoryDraft(agent.backstory); setEditingBackstory(true); }}
                    className="text-xs text-g-blue hover:text-g-blue/80 transition-colors"
                  >
                    编辑
                  </button>
                )}
              </div>
              {editingBackstory ? (
                <div className="space-y-2">
                  <textarea
                    value={backstoryDraft}
                    onChange={(e) => setBackstoryDraft(e.target.value)}
                    rows={3}
                    className="w-full px-3 py-2 text-sm bg-g-bg border border-g-blue/40 rounded-lg text-g-fg focus:outline-none focus:border-g-blue resize-none"
                    autoFocus
                  />
                  <div className="flex gap-2 justify-end">
                    <button
                      onClick={() => setEditingBackstory(false)}
                      className="px-3 py-1 text-xs text-g-fg-3 hover:text-g-fg rounded-gm hover:bg-g-bg-muted transition-colors"
                    >
                      取消
                    </button>
                    <button
                      onClick={() => { saveField("backstory", backstoryDraft); setEditingBackstory(false); }}
                      disabled={saving}
                      className="px-3 py-1 text-xs bg-g-blue text-white rounded-gm shadow-gm-sm hover:bg-blue-600 active:scale-[0.97] transition-all disabled:opacity-50"
                    >
                      保存
                    </button>
                  </div>
                </div>
              ) : (
                <p className="text-sm text-g-fg-3 whitespace-pre-wrap">{agent.backstory || "(未设置)"}</p>
              )}
            </div>

            {/* Created at */}
            <div className="text-xs text-g-fg-4 pt-2 border-t border-g-border/50">
              创建于 {createdAt}
            </div>
          </div>
        </section>

        {/* ─── Model Configuration ─── */}
        <section>
          <h3 className="text-sm font-semibold text-g-fg uppercase tracking-wider mb-3 flex items-center gap-2"><span className="w-1 h-3.5 rounded-full bg-g-blue/70 shrink-0" />模型配置</h3>
          <div className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-sm hover:shadow-gm transition-shadow p-5 space-y-3">
            <div>
              <label className="text-xs font-medium text-g-fg-3 mb-2 block">使用模型</label>
              {models.length > 0 ? (
                <select
                  value={agent.modelId || ""}
                  onChange={(e) => changeModel(e.target.value)}
                  disabled={saving}
                  className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-lg text-g-fg focus:outline-none focus:border-g-blue disabled:opacity-50"
                >
                  <option value="">
                    {resolvedModel?.modelName
                      ? `自动 (${resolvedModel.modelName})`
                      : "默认模型"}
                  </option>
                  {models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name} ({m.modelId})
                    </option>
                  ))}
                </select>
              ) : (
                <span className="text-xs text-g-fg-4">
                  {agent.modelId ? `已配置模型 ID: ${agent.modelId.slice(0, 8)}...` :
                   resolvedModel ? `自动选择: ${resolvedModel.modelName} (${resolvedModel.modelId})` :
                   "使用默认模型"}
                </span>
              )}
              {agent.modelId && models.length > 0 && (
                <div className="mt-1.5 text-[10px] text-g-fg-4">
                  {(() => {
                    const m = models.find((x) => x.id === agent.modelId);
                    return m ? `上下文 ${m.contextWindow.toLocaleString()} tokens · 最大输出 ${m.maxOutputTokens.toLocaleString()} tokens${m.supportsThinking ? " · 支持思考" : ""}` : "";
                  })()}
                </div>
              )}
            </div>
          </div>
        </section>

        {/* ─── Permissions Section (read-only; CEO sets charter, HR assigns permissions) ─── */}
        <section>
          <h3 className="text-sm font-semibold text-g-fg uppercase tracking-wider mb-3 flex items-center gap-2"><span className="w-1 h-3.5 rounded-full bg-g-blue/70 shrink-0" />权限配置</h3>
          <p className="text-xs text-g-fg-4 mb-3">CEO 维护项目章程与组织设计；HR 为各 Agent 分配权限与技能绑定。</p>
          <div className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-sm hover:shadow-gm transition-shadow p-5 space-y-4">
            {/* Current permission mode */}
            <div>
              <label className="text-xs font-medium text-g-fg-3 mb-2 block">权限模式</label>
              <div className="grid grid-cols-3 gap-2">
                {PERMISSION_MODES.map((mode) => (
                  <div
                    key={mode.value}
                    className={`px-3 py-2.5 text-xs rounded-gm border text-left transition-all ${
                      permissions?.permissionMode === mode.value
                        ? "bg-g-blue/10 border-g-blue/60 text-g-blue shadow-gm-sm"
                        : "bg-g-bg border-g-border text-g-fg-4 opacity-50"
                    }`}
                  >
                    <div className="font-medium">{mode.label}</div>
                    <div className="text-[10px] mt-0.5 opacity-70">{mode.desc}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* MCP & Skills */}
            <div className="space-y-3 pt-3 border-t border-g-border/50">
              <div>
                <label className="text-xs font-medium text-g-fg-3 mb-1 block">MCP 服务器</label>
                <div className="flex flex-wrap gap-1">
                  {agent.mcpServers.length > 0 ? (
                    agent.mcpServers.map((s, i) => (
                      <span key={i} className="px-2 py-0.5 text-[10px] bg-g-blue-bg text-g-blue rounded-gm">
                        {s}
                      </span>
                    ))
                  ) : (
                    <span className="text-xs text-g-fg-4">未绑定</span>
                  )}
                </div>
              </div>
              <div>
                <label className="text-xs font-medium text-g-fg-3 mb-1 block">绑定技能</label>
                <div className="flex flex-wrap gap-1">
                  {agent.boundSkills.length > 0 ? (
                    agent.boundSkills.map((s, i) => (
                      <span key={i} className="px-2 py-0.5 text-[10px] bg-purple-100 text-purple-700 rounded-gm">
                        {s}
                      </span>
                    ))
                  ) : (
                    <span className="text-xs text-g-fg-4">未绑定</span>
                  )}
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* ─── Hierarchy Section ─── */}
        <section>
          <h3 className="text-sm font-semibold text-g-fg uppercase tracking-wider mb-3 flex items-center gap-2"><span className="w-1 h-3.5 rounded-full bg-g-blue/70 shrink-0" />组织关系</h3>
          <div className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-sm hover:shadow-gm transition-shadow p-5 space-y-3">
            <div>
              <label className="text-xs font-medium text-g-fg-3 mb-1 block">上级</label>
              {agent.parentId ? (
                <button
                  onClick={() => setSelectedAgent(agent.parentId)}
                  className="text-sm text-g-blue hover:text-g-blue/80 transition-colors"
                >
                  查看上级 Agent →
                </button>
              ) : (
                <span className="text-xs text-g-fg-4">无（顶级 Agent）</span>
              )}
            </div>
            <div>
              <label className="text-xs font-medium text-g-fg-3 mb-1 block">所属项目</label>
              <span className="text-sm text-g-fg">{agent.projectId ? agent.projectId.slice(0, 8) + "..." : "未分配"}</span>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
