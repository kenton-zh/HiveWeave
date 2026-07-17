import { useState, useEffect, useRef } from "react";
import { createAgent, getTemplates, getTemplateDivisions, getTemplate, getModels, type AgentTemplate, type LlmModel } from "../api";

interface AddAgentDialogProps {
  projectId: string;
  parentId?: string | null;
  onClose: () => void;
  onCreated: () => void;
}

const ROLE_PRESETS = [
  { value: "ceo", label: "CEO", desc: "项目负责人与章程" },
  { value: "hr", label: "HR", desc: "人力资源管理" },
  { value: "architect", label: "Architect", desc: "系统架构设计与技术决策" },
  { value: "manager", label: "Manager", desc: "项目管理与任务协调" },
  { value: "developer", label: "Developer", desc: "模块开发与功能实现" },
  { value: "qa", label: "QA", desc: "质量保障与测试" },
  { value: "devops", label: "DevOps", desc: "部署运维与CI/CD" },
];

const PERM_TYPES = [
  { value: "coordinator", label: "协调者", desc: "管理和调度子Agent" },
  { value: "executor", label: "执行者", desc: "执行具体任务" },
];

export default function AddAgentDialog({ projectId, parentId, onClose, onCreated }: AddAgentDialogProps) {
  const [mode, setMode] = useState<"manual" | "template">("manual");
  const [name, setName] = useState("");
  const [position, setPosition] = useState("");
  const [role, setRole] = useState("");
  const [customRole, setCustomRole] = useState(false);
  const [goal, setGoal] = useState("");
  const [backstory, setBackstory] = useState("");
  const [permissionType, setPermissionType] = useState<"coordinator" | "executor">("executor");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [models, setModels] = useState<LlmModel[]>([]);
  const [selectedModelId, setSelectedModelId] = useState<string>("");

  // 入场动效（纯视觉）：遮罩淡入 + 面板滑入
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  // Load available models on mount
  useEffect(() => {
    getModels().then(setModels).catch(() => {});
  }, []);

  const canSubmit = name.trim() && role.trim() && goal.trim() && position.trim() && !loading;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setLoading(true);
    setError("");
    try {
      await createAgent({
        name: name.trim(),
        position: position.trim(),
        role: role.trim(),
        goal: goal.trim(),
        backstory: backstory.trim(),
        permissionType,
        projectId,
        parentId: parentId || undefined,
        modelId: selectedModelId || undefined,
      });
      onCreated();
    } catch (err: any) {
      setError(err.message || "创建失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-[2px] transition-opacity duration-200 ${entered ? "opacity-100" : "opacity-0"}`}
      onClick={onClose}
    >
      <div
        className={`bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg flex flex-col transform transition-all duration-200 ease-out ${
          mode === "template" ? "w-full max-w-2xl max-h-[85vh]" : "w-full max-w-md"
        } ${entered ? "opacity-100 translate-y-0 scale-100" : "opacity-0 translate-y-3 scale-[0.98]"}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-g-border shrink-0">
          <h3 className="text-base font-semibold text-g-fg">
            {parentId ? "创建子 Agent" : "创建 Agent"}
          </h3>
          <button onClick={onClose} className="text-g-fg-3 hover:text-g-fg transition-colors">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Mode Toggle */}
        <div className="flex border-b border-g-border shrink-0">
          <button
            onClick={() => setMode("manual")}
            className={`flex-1 px-4 py-2.5 text-sm font-medium transition-colors border-b-2 ${
              mode === "manual"
                ? "text-g-blue border-g-blue"
                : "text-g-fg-4 border-transparent hover:text-g-fg"
            }`}
          >
            手动创建
          </button>
          <button
            onClick={() => setMode("template")}
            className={`flex-1 px-4 py-2.5 text-sm font-medium transition-colors border-b-2 ${
              mode === "template"
                ? "text-g-blue border-g-blue"
                : "text-g-fg-4 border-transparent hover:text-g-fg"
            }`}
          >
            从模板创建
          </button>
        </div>

        {/* Content */}
        {mode === "manual" ? (
          <ManualForm
            name={name} setName={setName}
            position={position} setPosition={setPosition}
            role={role} setRole={setRole}
            customRole={customRole} setCustomRole={setCustomRole}
            goal={goal} setGoal={setGoal}
            backstory={backstory} setBackstory={setBackstory}
            permissionType={permissionType} setPermissionType={setPermissionType}
            models={models} selectedModelId={selectedModelId} setSelectedModelId={setSelectedModelId}
            error={error}
          />
        ) : (
          <TemplatePicker
            onSelectTemplate={async (tpl) => {
              // Load full template to get promptBody
              const full = await getTemplate(tpl.id);
              setName(full.name);
              setRole(full.role);
              setCustomRole(!ROLE_PRESETS.some(r => r.value === full.role));
              setGoal(full.vibe || full.description || `Expert ${full.name.toLowerCase()}`);
              setBackstory(full.promptBody || full.description || "");
              setMode("manual"); // Switch to manual mode so user can review/edit
            }}
          />
        )}

        {/* Footer */}
        <div className="flex justify-end gap-2 px-6 py-4 border-t border-g-border shrink-0">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm text-g-fg-3 hover:text-g-fg rounded-gm hover:bg-g-bg-muted active:scale-[0.97] transition-all"
          >
            取消
          </button>
          <button
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="px-4 py-2 text-sm font-medium bg-g-blue hover:bg-blue-600 disabled:opacity-40 disabled:cursor-not-allowed text-white rounded-gm shadow-gm-sm active:scale-[0.97] transition-all"
          >
            {loading ? "创建中..." : "创建"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────
// Manual form (original UI)
// ────────────────────────────────────────────────────────────────

function ManualForm(props: {
  name: string; setName: (v: string) => void;
  position: string; setPosition: (v: string) => void;
  role: string; setRole: (v: string) => void;
  customRole: boolean; setCustomRole: (v: boolean) => void;
  goal: string; setGoal: (v: string) => void;
  backstory: string; setBackstory: (v: string) => void;
  permissionType: "coordinator" | "executor"; setPermissionType: (v: "coordinator" | "executor") => void;
  models: LlmModel[]; selectedModelId: string; setSelectedModelId: (v: string) => void;
  error: string;
}) {
  const { name, setName, position, setPosition, role, setRole, customRole, setCustomRole, goal, setGoal, backstory, setBackstory, permissionType, setPermissionType, models, selectedModelId, setSelectedModelId, error } = props;

  return (
    <div className="px-6 py-4 space-y-4 overflow-y-auto">
      {/* Name */}
      <div>
        <label className="block text-xs font-medium text-g-fg-3 mb-1">名称</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="例如：张三（中文姓名）"
          className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-lg text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue"
          autoFocus
        />
      </div>

      {/* Position */}
      <div>
        <label className="block text-xs font-medium text-g-fg-3 mb-1">岗位</label>
        <input
          type="text"
          value={position}
          onChange={(e) => setPosition(e.target.value)}
          placeholder="例如：前端工程师、后端开发、产品经理"
          className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-lg text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue"
        />
      </div>

      {/* Role */}
      <div>
        <label className="block text-xs font-medium text-g-fg-3 mb-1">角色</label>
        {customRole ? (
          <div className="flex gap-2">
            <input
              type="text"
              value={role}
              onChange={(e) => setRole(e.target.value)}
              placeholder="自定义角色名称"
              className="flex-1 px-3 py-2 text-sm bg-g-bg border border-g-border rounded-lg text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue"
            />
            <button
              onClick={() => { setCustomRole(false); setRole(""); }}
              className="px-2 text-xs text-g-fg-4 hover:text-g-fg transition-colors"
              title="切换回预设角色"
            >
              预设
            </button>
          </div>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {ROLE_PRESETS.map((r) => (
              <button
                key={r.value}
                onClick={() => setRole(r.value)}
                className={`px-2.5 py-1 text-[11px] font-medium rounded-gm border active:scale-[0.96] transition-all ${
                  role === r.value
                    ? "bg-g-blue/10 border-g-blue/60 text-g-blue shadow-gm-sm"
                    : "bg-g-bg border-g-border text-g-fg-3 hover:border-g-border-strong hover:text-g-fg hover:shadow-gm-sm"
                }`}
                title={r.desc}
              >
                {r.label}
              </button>
            ))}
            <button
              onClick={() => { setCustomRole(true); setRole(""); }}
              className="px-2.5 py-1 text-[11px] font-medium rounded-gm border bg-g-bg border-dashed border-g-border text-g-fg-4 hover:border-g-blue/50 hover:text-g-blue active:scale-[0.96] transition-all"
            >
              + 自定义
            </button>
          </div>
        )}
      </div>

      {/* Permission Type */}
      <div>
        <label className="block text-xs font-medium text-g-fg-3 mb-1">类型</label>
        <div className="grid grid-cols-2 gap-2">
          {PERM_TYPES.map((p) => (
            <button
              key={p.value}
              onClick={() => setPermissionType(p.value as "coordinator" | "executor")}
              className={`px-3 py-2 text-xs rounded-gm border text-left active:scale-[0.98] transition-all ${
                permissionType === p.value
                  ? "bg-g-blue/10 border-g-blue/60 text-g-blue shadow-gm-sm"
                  : "bg-g-bg border-g-border text-g-fg-3 hover:border-g-border-strong hover:shadow-gm-sm"
              }`}
            >
              <span className="font-medium">{p.label}</span>
              <span className="block text-[10px] mt-0.5 opacity-70">{p.desc}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Goal */}
      <div>
        <label className="block text-xs font-medium text-g-fg-3 mb-1">目标</label>
        <textarea
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="这个 Agent 的核心职责和目标是什么？"
          rows={2}
          className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-lg text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue resize-none"
        />
      </div>

      {/* Goal */}
      <div>
        <label className="block text-xs font-medium text-g-fg-3 mb-1">模型 <span className="text-g-fg-4/70">(可选)</span></label>
        {models.length > 0 ? (
          <select
            value={selectedModelId}
            onChange={(e) => setSelectedModelId(e.target.value)}
            className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-lg text-g-fg focus:outline-none focus:border-g-blue"
          >
            <option value="">默认模型</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name} ({m.modelId})
              </option>
            ))}
          </select>
        ) : (
          <div className="text-xs text-g-fg-4 py-2">暂无可用模型，请在设置中添加</div>
        )}
      </div>

      {/* Backstory */}
      <div>
        <label className="block text-xs font-medium text-g-fg-3 mb-1">背景故事 <span className="text-g-fg-4/70">(可选)</span></label>
        <textarea
          value={backstory}
          onChange={(e) => setBackstory(e.target.value)}
          placeholder="赋予 Agent 一个角色背景，影响其回复风格..."
          rows={2}
          className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-lg text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue resize-none"
        />
      </div>

      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────
// Template picker (browsable grid with filters)
// ────────────────────────────────────────────────────────────────

function TemplatePicker({ onSelectTemplate }: { onSelectTemplate: (tpl: AgentTemplate) => void }) {
  const [divisions, setDivisions] = useState<Array<{ division: string; count: number }>>([]);
  const [templates, setTemplates] = useState<AgentTemplate[]>([]);
  const [activeDivision, setActiveDivision] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const searchTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  // Load divisions on mount
  useEffect(() => {
    getTemplateDivisions()
      .then((divs) => setDivisions(divs.map((d) => ({ division: d, count: 0 }))))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  // Load templates when filter changes
  useEffect(() => {
    setLoading(true);
    getTemplates({
      division: activeDivision || undefined,
    })
      .then(setTemplates)
      .catch(() => setTemplates([]))
      .finally(() => setLoading(false));
  }, [activeDivision, search]);

  const handleSearchChange = (value: string) => {
    setSearch(value);
    // Debounce not needed since API call is lightweight, but let's be nice
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => {
      // search state already triggers the useEffect above
    }, 200);
  };

  return (
    <div className="flex flex-col overflow-hidden" style={{ maxHeight: "60vh" }}>
      {/* Filter bar */}
      <div className="px-6 py-3 border-b border-g-border space-y-2 shrink-0">
        {/* Search */}
        <input
          type="text"
          value={search}
          onChange={(e) => handleSearchChange(e.target.value)}
          placeholder="搜索模板..."
          className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-lg text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue"
        />

        {/* Division tabs */}
        <div className="flex flex-wrap gap-1.5">
          <button
            onClick={() => setActiveDivision("")}
            className={`px-2.5 py-1 text-[11px] font-medium rounded-gm border active:scale-[0.96] transition-all ${
              !activeDivision
                ? "bg-g-blue/10 border-g-blue/60 text-g-blue shadow-gm-sm"
                : "bg-g-bg border-g-border text-g-fg-3 hover:border-g-border-strong hover:text-g-fg"
            }`}
          >
            全部
          </button>
          {divisions.map((d) => (
            <button
              key={d.division}
              onClick={() => setActiveDivision(d.division === activeDivision ? "" : d.division)}
              className={`px-2.5 py-1 text-[11px] font-medium rounded-gm border active:scale-[0.96] transition-all ${
                activeDivision === d.division
                  ? "bg-g-blue/10 border-g-blue/60 text-g-blue shadow-gm-sm"
                  : "bg-g-bg border-g-border text-g-fg-3 hover:border-g-border-strong hover:text-g-fg"
              }`}
            >
              {d.division} ({d.count})
            </button>
          ))}
        </div>
      </div>

      {/* Template grid */}
      <div className="flex-1 overflow-y-auto px-6 py-3">
        {loading ? (
          <div className="flex items-center justify-center h-32 text-g-fg-4 text-sm">加载中...</div>
        ) : templates.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-32 gap-1.5">
            <span className="text-2xl">🗂️</span>
            <span className="text-g-fg-4 text-sm">
              {search || activeDivision ? "没有找到匹配的模板" : "模板库为空，请先运行导入脚本"}
            </span>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            {templates.map((tpl) => (
              <button
                key={tpl.id}
                onClick={() => onSelectTemplate(tpl)}
                className="flex items-start gap-2.5 px-3 py-2.5 rounded-gmLg border border-g-border bg-g-bg hover:border-g-blue/40 hover:bg-g-blue-bg/30 hover:shadow-gm active:scale-[0.99] text-left transition-all group"
              >
                <span className="text-lg shrink-0 mt-0.5 group-hover:scale-110 transition-transform">{tpl.emoji || "📋"}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="text-sm font-medium text-g-fg group-hover:text-g-blue transition-colors truncate">
                      {tpl.name}
                    </span>
                    <span className="px-1.5 py-0.5 text-[9px] font-medium rounded bg-g-bg-muted border border-g-border text-g-fg-4 shrink-0">
                      {tpl.role}
                    </span>
                  </div>
                  <p className="text-[11px] text-g-fg-4 mt-0.5 line-clamp-2">
                    {tpl.vibe || tpl.description || "No description"}
                  </p>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Template count footer */}
      <div className="px-6 py-2 text-[10px] text-g-fg-4/70 border-t border-g-border shrink-0">
        {templates.length} 个模板可用
      </div>
    </div>
  );
}
