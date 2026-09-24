import { useState, useEffect, useCallback, useRef } from "react";
import {
  getAgent,
  updateAgent,
  getPermissionRules,
  getModels,
  getMcpServers,
  bindAgentMcp,
  unbindAgentMcp,
} from "../api";
import type { LlmModel, McpServer } from "../api";
import ConfirmDialog from "./ConfirmDialog";
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
  active: { label: "工作中", color: "text-g-green", desc: "Agent 正在执行任务" },
  promoted: { label: "已晋升", color: "text-g-blue", desc: "Agent 已晋升为协调者" },
  receiving: { label: "交接中", color: "text-g-yellow", desc: "正在接收工作交接" },
  merging: { label: "合并中", color: "text-g-purple", desc: "代码正在合并" },
  dissolving: { label: "解散中", color: "text-g-red", desc: "Agent 正在解散" },
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

/** skills.sh 下载技能 slug 为 owner/repo/skill；纪律/内置技能为无斜杠 kebab-case。 */
function isDownloadedSkill(slug: string): boolean {
  return slug.includes("/");
}

const SKILL_PILL = "px-2 py-0.5 text-[10px] rounded-gm max-w-full break-all";
const SKILL_PILL_DISCIPLINE = `${SKILL_PILL} bg-g-purple-bg text-g-purple`;
const SKILL_PILL_DOWNLOADED = `${SKILL_PILL} bg-g-green-bg text-g-green`;

// ─── 编辑状态机（FE-09：编辑草稿与刷新隔离）─────────────────────────────────
// 每个可编辑字段独立一份状态：
//   查看中 viewing                —— 显示服务器权威数据
//   编辑中·未修改 editing !dirty   —— 可接收最新数据：草稿跟随刷新
//   编辑中·已修改 editing dirty    —— 刷新不覆盖脏草稿；服务器值偏离进入编辑
//                                    时的基线 ⇒ 服务器数据冲突（conflict）
//   保存中 saving                 —— 禁重复提交，草稿保持可读
//   保存成功（瞬态）               —— onSave 更新权威数据后回到查看中
//   保存失败 error != null         —— 留在编辑态，输入保留，可重试
//   服务器数据冲突 conflict        —— 提示「此内容已被更新」，可加载最新或覆盖保存
interface FieldEditorState {
  phase: "viewing" | "editing" | "saving";
  draft: string;
  baseline: string; // 进入编辑时的服务器值；未修改时跟随最新服务器值
  dirty: boolean;
  conflict: boolean;
  error: string | null;
}

interface FieldEditor {
  state: FieldEditorState;
  unsaved: boolean;
  startEdit: () => void;
  changeDraft: (draft: string) => void;
  cancelEdit: () => void;
  loadServerValue: () => void;
  save: () => Promise<void>;
  reset: (serverValue: string) => void;
}

function useFieldEditor(serverValue: string, onSave: (draft: string) => Promise<void>): FieldEditor {
  const onSaveRef = useRef(onSave);
  useEffect(() => { onSaveRef.current = onSave; }, [onSave]);

  const [state, setState] = useState<FieldEditorState>({
    phase: "viewing", draft: serverValue, baseline: serverValue, dirty: false, conflict: false, error: null,
  });
  // 同步防重闸：setState 与重渲染之间连点仍被 savingRef 挡住
  const savingRef = useRef(false);

  // 后台刷新回填（脏保护核心）：仅编辑态响应；未修改 ⇒ 草稿/基线跟随最新值，
  // 已修改 ⇒ 保留脏草稿，若服务器值偏离基线则标记服务器数据冲突。查看中/保存中不动草稿。
  useEffect(() => {
    setState((s) => {
      if (s.phase !== "editing") return s;
      if (!s.dirty) return { ...s, draft: serverValue, baseline: serverValue, conflict: false };
      if (serverValue !== s.baseline) return { ...s, conflict: true };
      return s;
    });
  }, [serverValue]);

  const startEdit = useCallback(() => {
    setState({ phase: "editing", draft: serverValue, baseline: serverValue, dirty: false, conflict: false, error: null });
  }, [serverValue]);

  const changeDraft = useCallback((draft: string) => {
    setState((s) => (s.phase === "editing" ? { ...s, draft, dirty: true } : s));
  }, []);

  // 取消：回到查看中恢复服务器值，绝不自动提交
  const cancelEdit = useCallback(() => {
    setState((s) => ({ ...s, phase: "viewing", dirty: false, conflict: false, error: null }));
  }, []);

  // 冲突处理：放弃本地修改、加载最新服务器内容（停留在编辑态·未修改）
  const loadServerValue = useCallback(() => {
    setState((s) => ({ ...s, draft: serverValue, baseline: serverValue, dirty: false, conflict: false }));
  }, [serverValue]);

  // 切换成员（放弃路径）时整体复位
  const reset = useCallback((value: string) => {
    savingRef.current = false;
    setState({ phase: "viewing", draft: value, baseline: value, dirty: false, conflict: false, error: null });
  }, []);

  const save = async () => {
    if (savingRef.current || state.phase === "saving") return; // 保存中：禁重复提交
    const draft = state.draft;
    savingRef.current = true;
    setState((s) => ({ ...s, phase: "saving", error: null }));
    try {
      await onSaveRef.current(draft);
      // 保存成功：权威数据已在 onSave 内更新 ⇒ 退出编辑回到查看中。
      // 仅当仍在保存态才落定：期间若已 reset（放弃并切换），不污染新成员的字段状态。
      setState((s) =>
        s.phase === "saving"
          ? { phase: "viewing", draft, baseline: draft, dirty: false, conflict: false, error: null }
          : s,
      );
    } catch (err: any) {
      // 保存失败：留在编辑态，输入保留，给出重试（同样仅在仍在保存态时落定）
      setState((s) =>
        s.phase === "saving"
          ? { ...s, phase: "editing", error: err?.message ? String(err.message) : "保存失败" }
          : s,
      );
    } finally {
      savingRef.current = false;
    }
  };

  return {
    state,
    unsaved: state.phase !== "viewing" && state.dirty,
    startEdit,
    changeDraft,
    cancelEdit,
    loadServerValue,
    save,
    reset,
  };
}

/** 保存按钮文案：保存中 / 失败重试 / 保存（避免嵌套三元） */
function fieldSaveLabel(state: FieldEditorState): string {
  if (state.phase === "saving") return "保存中...";
  if (state.error) return "重试";
  return "保存";
}

/** 可编辑字段区块：查看态显示服务器值，编辑态由 FieldEditor 状态机驱动。 */
function EditableFieldBlock({ label, value, editor, displayClassName }: {
  label: string;
  value: string;
  editor: FieldEditor;
  displayClassName: string;
}) {
  const { state } = editor;
  const editing = state.phase !== "viewing";
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <label className="text-xs font-medium text-g-fg-3">{label}</label>
        {!editing && (
          <button
            onClick={editor.startEdit}
            className="text-xs text-g-blue hover:text-g-blue/80 transition-colors"
          >
            编辑
          </button>
        )}
        {editing && state.dirty && state.phase !== "saving" && (
          <span className="text-[10px] text-g-yellow-vivid">未保存</span>
        )}
      </div>
      {editing ? (
        <div className="space-y-2">
          {state.conflict && (
            <div className="bg-g-yellow-bg border border-g-yellow/60 text-g-yellow px-3 py-1.5 rounded-gm text-xs flex items-center justify-between gap-2">
              <span>此内容已被更新（服务器版本已变化）</span>
              <button
                onClick={editor.loadServerValue}
                className="underline shrink-0 hover:opacity-80"
              >
                加载最新内容
              </button>
            </div>
          )}
          <textarea
            value={state.draft}
            onChange={(e) => editor.changeDraft(e.target.value)}
            rows={3}
            aria-label={label}
            readOnly={state.phase === "saving"}
            className={`w-full px-3 py-2 text-sm bg-g-bg border border-g-blue/40 rounded-gm text-g-fg focus:border-g-blue resize-none ${state.phase === "saving" ? "opacity-60" : ""}`}
            autoFocus
          />
          {state.error && (
            <div className="text-xs text-g-red" role="alert">
              保存失败：{state.error}。输入已保留，可点击「重试」。
            </div>
          )}
          <div className="flex gap-2 justify-end">
            <button
              onClick={editor.cancelEdit}
              disabled={state.phase === "saving"}
              className="px-3 py-1 text-xs text-g-fg-3 hover:text-g-fg rounded-gm hover:bg-g-bg-muted transition-colors disabled:opacity-50"
            >
              取消
            </button>
            <button
              onClick={editor.save}
              disabled={state.phase === "saving"}
              className="px-3 py-1 text-xs bg-g-blue text-white rounded-gm shadow-gm-sm hover:brightness-110 active:scale-[0.97] transition-all disabled:opacity-50"
            >
              {fieldSaveLabel(state)}
            </button>
          </div>
        </div>
      ) : (
        <p className={`text-sm whitespace-pre-wrap ${displayClassName}`}>{value || "(未设置)"}</p>
      )}
    </div>
  );
}

export default function AgentDetailPanel({ agentId }: { agentId: string }) {
  const [agent, setAgent] = useState<AgentDetail | null>(null);
  const [permissions, setPermissions] = useState<PermissionRules | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // ── 编辑草稿与刷新隔离（FE-09）──
  // displayedId = 面板当前实际展示/编辑的成员。agentId prop 变化先经过未保存
  // 检查：有脏修改 ⇒ 弹确认（继续编辑=取消切换并回写全局选中；放弃并切换=丢弃）；
  // 无脏修改 ⇒ 静默跟随。所有抓取/保存都以 displayedId 为准并做过期响应核对。
  const [displayedId, setDisplayedId] = useState(agentId);
  const [pendingSwitchTo, setPendingSwitchTo] = useState<string | null>(null);
  const displayedIdRef = useRef(displayedId);
  useEffect(() => { displayedIdRef.current = displayedId; }, [displayedId]);

  const refreshOrgTree = useAppStore((s) => s.refreshOrgTree);
  const setSelectedAgent = useAppStore((s) => s.setSelectedAgent);
  const processingAgents = useAppStore((s) => s.processingAgents);
  const orgTreeVersion = useAppStore((s) => s.orgTreeVersion);
  const agentActiveModel = useAppStore((s) => s.agentActiveModel);

  // 字段保存：成功后更新权威数据 + 刷新组织树；await 期间已切换成员 ⇒ 不回填本地 state
  const saveField = useCallback(async (field: "goal" | "backstory", value: string) => {
    const reqId = displayedId;
    await updateAgent(reqId, { [field]: value });
    if (displayedIdRef.current !== reqId) return; // 已切换成员：丢弃过期保存的回填
    setAgent((prev) => (prev && prev.id === reqId ? { ...prev, [field]: value } : prev));
    refreshOrgTree();
  }, [displayedId, refreshOrgTree]);
  const saveGoal = useCallback(async (draft: string) => saveField("goal", draft), [saveField]);
  const saveBackstory = useCallback(async (draft: string) => saveField("backstory", draft), [saveField]);

  const goalEditor = useFieldEditor(agent?.goal ?? "", saveGoal);
  const backstoryEditor = useFieldEditor(agent?.backstory ?? "", saveBackstory);
  const hasUnsavedEdits = goalEditor.unsaved || backstoryEditor.unsaved;

  // 切换成员时的未保存修改保护（脏数据不随切换被静默覆盖）
  useEffect(() => {
    if (agentId === displayedId) return;
    if (hasUnsavedEdits) {
      setPendingSwitchTo(agentId); // 弹确认：继续编辑 / 放弃修改并切换
    } else {
      setDisplayedId(agentId);
    }
  }, [agentId, displayedId, hasUnsavedEdits]);

  // 弹窗打开期间脏修改因保存成功等原因消失并自动完成切换 ⇒ 收起弹窗
  useEffect(() => {
    if (pendingSwitchTo && agentId === displayedId) setPendingSwitchTo(null);
  }, [pendingSwitchTo, agentId, displayedId]);

  // MCP 服务器管理：全部已配置 server（绑定下拉用）+ 解绑确认
  const [allMcpServers, setAllMcpServers] = useState<McpServer[]>([]);
  const [mcpBusy, setMcpBusy] = useState(false);
  const [pendingUnbind, setPendingUnbind] = useState<string | null>(null);
  const [models, setModels] = useState<LlmModel[]>([]);
  const [resolvedModel, setResolvedModel] = useState<{ modelName: string; modelId: string } | null>(null);
  // 模型下拉/模型变更的独立保存中状态（与字段编辑状态机无关）
  const [saving, setSaving] = useState(false);

  // Fetch agent details
  const fetchAgent = useCallback(async () => {
    const reqId = displayedId;
    try {
      setLoading(true);
      setError("");
      const raw = await getAgent(reqId);
      // 过期响应丢弃（FE-09 / T-15）：await 期间已切换成员 ⇒ 整体丢弃，
      // 不回填 agent / permissions / error / loading 任何 state。
      if (displayedIdRef.current !== reqId) return;
      // The Elixir backend wraps the response: `%{agent: serialize_agent(a)}`.
      // Some endpoints (e.g. OrgTree) return the agent fields at the top level.
      // Accept both shapes so missing `id` doesn't crash the render.
      const data = (raw && typeof raw === "object" && "agent" in raw && raw.agent) ? raw.agent : raw;
      if (!data || typeof data !== "object" || !data.id) {
        setError("Agent 不存在");
        return;
      }
      // 响应对象与请求对象不符（id 对不上）⇒ 视为过期/错配响应，同样丢弃
      if (data.id !== reqId) return;
      setAgent({
        ...data,
        allowedTools: safeJsonArray(data?.allowedTools),
        deniedTools: safeJsonArray(data?.deniedTools),
        askTools: safeJsonArray(data?.askTools),
        mcpServers: safeJsonArray(data?.mcpServers),
        boundSkills: safeJsonArray(data?.boundSkills),
      });
      // 注意：不再直接回填 goal/backstory 草稿 —— 草稿由 useFieldEditor
      // 按脏状态决定是否跟随刷新（编辑中已修改 ⇒ 不覆盖）。

      // Also fetch permission rules
      try {
        const perms = await getPermissionRules(reqId);
        if (displayedIdRef.current !== reqId) return; // 第二次 await 后再次核对
        setPermissions(perms);
      } catch {
        if (displayedIdRef.current !== reqId) return;
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
      if (displayedIdRef.current !== reqId) return;
      setError(err.message || "加载失败");
    } finally {
      if (displayedIdRef.current === reqId) setLoading(false);
    }
  }, [displayedId]);

  useEffect(() => {
    fetchAgent();
  }, [fetchAgent]);

  // Re-fetch agent details when org tree changes (e.g. status updates,
  // new hires, role changes) so the panel stays in sync without a page reload.
  // 经 ref 调用，避免把 fetchAgent 加进依赖导致切换成员时重复抓取。
  const fetchAgentRef = useRef(fetchAgent);
  useEffect(() => { fetchAgentRef.current = fetchAgent; }, [fetchAgent]);
  useEffect(() => {
    const id = displayedIdRef.current;
    if (id) fetchAgentRef.current();
  }, [orgTreeVersion]);

  // Fetch resolved model when agent has no explicit model
  useEffect(() => {
    if (!displayedId) return;
    const reqId = displayedId;
    fetch(`/api/chat/resolved-model/${reqId}`)
      .then((r) => r.json())
      .then((data: any) => {
        if (displayedIdRef.current !== reqId) return; // 过期响应丢弃
        if (data?.modelName) setResolvedModel({ modelName: data.modelName, modelId: data.modelId });
      })
      .catch(() => {
        if (displayedIdRef.current === reqId) setResolvedModel(null);
      });
  }, [displayedId, agent?.modelId]);

  // Load available models
  useEffect(() => {
    getModels().then(setModels).catch(() => {});
  }, []);

  // Load configured MCP servers (for the bind dropdown)
  useEffect(() => {
    getMcpServers().then(setAllMcpServers).catch(() => {});
  }, []);

  // Change agent model
  const changeModel = async (modelId: string) => {
    const reqId = displayedId;
    setSaving(true);
    try {
      await updateAgent(reqId, { modelId: modelId || null });
      if (displayedIdRef.current !== reqId) return; // 已切换成员：不回填
      setAgent((prev) => (prev && prev.id === reqId ? { ...prev, modelId: modelId || null } : prev));
      refreshOrgTree();
    } catch (err: any) {
      setError(`保存失败: ${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  // Bind an MCP server to this agent
  const handleBindMcp = async (server: string) => {
    if (!agent || !server || mcpBusy) return;
    setMcpBusy(true);
    try {
      await bindAgentMcp(agent.id, server);
      setAgent({ ...agent, mcpServers: [...agent.mcpServers, server] });
      refreshOrgTree();
    } catch (err: any) {
      setError(`绑定 MCP 失败: ${err.message}`);
    } finally {
      setMcpBusy(false);
    }
  };

  // Unbind an MCP server (called after ConfirmDialog confirms)
  const handleUnbindMcp = async (server: string) => {
    if (!agent || mcpBusy) return;
    setMcpBusy(true);
    try {
      await unbindAgentMcp(agent.id, server);
      setAgent({ ...agent, mcpServers: agent.mcpServers.filter((s) => s !== server) });
      refreshOrgTree();
    } catch (err: any) {
      setError(`解绑 MCP 失败: ${err.message}`);
    } finally {
      setMcpBusy(false);
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
      <div className="h-full flex items-center justify-center text-g-red text-sm p-4 text-center">
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
  const availableMcpServers = allMcpServers.filter(
    (s) => !agent.mcpServers.includes(s.name),
  );
  const isProcessing = processingAgents.includes(displayedId);
  // Override status display for "active" agents based on runtime processing state
  const runtimeStatus = agent.status === "active"
    ? isProcessing
      ? { label: "工作中", color: "text-g-green", desc: "Agent 正在执行任务" }
      : { label: "空闲", color: "text-g-fg-3", desc: "Agent 已激活，等待任务" }
    : statusConfig;
  const roleLabel = ROLE_LABELS[agent.role] || agent.role;
  const createdAt = new Date(agent.createdAt).toLocaleString("zh-CN");

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-2xl mx-auto p-6 space-y-6">
        {/* Error banner */}
        {error && (
          <div className="bg-g-red-bg border border-g-red text-g-red px-4 py-2 rounded-gm shadow-gm-sm text-sm">
            {error}
            <button onClick={() => setError("")} className="ml-2 text-g-red hover:text-g-red">×</button>
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
                    ? isProcessing ? "bg-g-green-vivid animate-pulse" : "bg-g-fg-3"
                    : agent.status === "idle" || agent.status === "inactive" ? "bg-g-fg-3"
                    : agent.status === "promoted" ? "bg-g-blue-vivid"
                    : agent.status === "receiving" ? "bg-g-yellow-vivid animate-pulse"
                    : agent.status === "merging" ? "bg-g-purple-vivid animate-pulse"
                    : agent.status === "dissolving" || agent.status === "archived" ? "bg-g-red"
                    : "bg-g-fg-4"
                }`}
              />
              <span className={`text-sm font-medium ${runtimeStatus.color}`}>{runtimeStatus.label}</span>
              <span className="text-xs text-g-fg-4 ml-1">— {runtimeStatus.desc}</span>
            </div>

            {/* Goal (editable; state machine keeps drafts isolated from refreshes) */}
            <EditableFieldBlock
              label="目标"
              value={agent.goal}
              editor={goalEditor}
              displayClassName="text-g-fg"
            />

            {/* Backstory (editable; same draft/refresh isolation) */}
            <EditableFieldBlock
              label="背景故事"
              value={agent.backstory}
              editor={backstoryEditor}
              displayClassName="text-g-fg-3"
            />

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
                  className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-gm text-g-fg focus:border-g-blue disabled:opacity-50"
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
              {/* Live model indicator — updates on turn start.
                  自动故障切换已移除（对标 DSH），source 恒为 tier_resolved。 */}
              {agentActiveModel[displayedId] && (
                <div className="mt-2 px-2.5 py-1.5 rounded-gm text-[11px] flex items-center gap-1.5 bg-g-blue/5 text-g-fg-3 border border-g-border/50">
                  <span className="w-1.5 h-1.5 rounded-full shrink-0 bg-g-green-vivid" />
                  <span>当前使用: <b>{agentActiveModel[displayedId].modelName}</b></span>
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
                    agent.mcpServers.map((s) => (
                      <span
                        key={s}
                        className="px-2 py-0.5 text-[10px] bg-g-blue-bg text-g-blue rounded-gm inline-flex items-center gap-1"
                      >
                        {s}
                        <button
                          onClick={() => setPendingUnbind(s)}
                          disabled={mcpBusy}
                          title={`解绑 ${s}`}
                          className="text-g-blue/60 hover:text-g-red transition-colors disabled:opacity-50"
                        >
                          ×
                        </button>
                      </span>
                    ))
                  ) : (
                    <span className="text-xs text-g-fg-4">未绑定</span>
                  )}
                </div>
                <select
                  value=""
                  onChange={(e) => { if (e.target.value) handleBindMcp(e.target.value); }}
                  disabled={mcpBusy}
                  className="mt-1.5 w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-gm text-g-fg focus:border-g-blue disabled:opacity-50"
                >
                  <option value="">
                    + 绑定{availableMcpServers.length === 0 ? "（暂无已配置的服务器）" : " MCP 服务器"}
                  </option>
                  {availableMcpServers.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name}（{s.transport}{s.enabled ? "" : "，已禁用"}）
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <div className="flex items-center justify-between gap-2 mb-1">
                  <label className="text-xs font-medium text-g-fg-3">绑定技能</label>
                  {agent.boundSkills.length > 0 && (
                    <span className="flex items-center gap-2 text-[10px] text-g-fg-4">
                      <span className="inline-flex items-center gap-1">
                        <span className="w-1.5 h-1.5 rounded-full bg-g-purple-vivid" />
                        纪律
                      </span>
                      <span className="inline-flex items-center gap-1">
                        <span className="w-1.5 h-1.5 rounded-full bg-g-green-vivid" />
                        下载
                      </span>
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap gap-1">
                  {agent.boundSkills.length > 0 ? (
                    agent.boundSkills.map((s) => {
                      const downloaded = isDownloadedSkill(s);
                      return (
                        <span
                          key={s}
                          title={downloaded ? "下载技能" : "纪律技能"}
                          className={downloaded ? SKILL_PILL_DOWNLOADED : SKILL_PILL_DISCIPLINE}
                        >
                          {s}
                        </span>
                      );
                    })
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

      {/* 解绑 MCP 确认弹窗 */}
      {pendingUnbind !== null && (
        <ConfirmDialog
          title="解绑 MCP 服务器"
          message={`确定解绑「${pendingUnbind}」？解绑后该 Agent 将无法使用其工具。`}
          danger
          onConfirm={() => {
            const server = pendingUnbind;
            setPendingUnbind(null);
            if (server) handleUnbindMcp(server);
          }}
          onCancel={() => setPendingUnbind(null)}
        />
      )}

      {/* 切换成员但有未保存修改（FE-09）：继续编辑=取消切换（回写全局选中）；
          放弃修改并切换=丢弃脏数据，跟随新成员 */}
      {pendingSwitchTo !== null && (
        <ConfirmDialog
          title="有未保存的修改"
          message={`正在编辑「${agent?.name ?? "当前成员"}」的目标/背景故事，尚未保存。切换成员将丢弃这些修改。`}
          confirmLabel="放弃修改并切换"
          cancelLabel="继续编辑"
          danger
          onConfirm={() => {
            goalEditor.reset("");
            backstoryEditor.reset("");
            setDisplayedId(pendingSwitchTo);
            setPendingSwitchTo(null);
          }}
          onCancel={() => {
            setPendingSwitchTo(null);
            setSelectedAgent(displayedId); // 全局选中切回当前编辑的成员，保持一致
          }}
        />
      )}
    </div>
  );
}
