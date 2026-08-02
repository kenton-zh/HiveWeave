import { useState, useEffect, useCallback } from "react";
import {
  getModels,
  createModel,
  updateModel,
  deleteModel,
  detectCapabilities,
  getTierConfig,
  saveTierConfig,
  getImageGenConfig,
  saveImageGenConfig,
} from "../api";
import type { LlmModel, TierConfig } from "../api";
import { useAppStore } from "../store";
import ConfirmDialog from "./ConfirmDialog";

interface Props {
  onClose: () => void;
}

const EMPTY_FORM = {
  name: "",
  modelId: "",
  baseUrl: "",
  apiKey: "",
  contextWindow: 128000,
  maxOutputTokens: 8192,
  supportsThinking: false,
};

const EMPTY_IMAGE_GEN = {
  modelId: "",
  baseUrl: "https://ark.cn-beijing.volces.com/api/plan/v3",
  apiKey: "",
};

const DEFAULT_PLAN_ROOT = "https://ark.cn-beijing.volces.com/api/plan/v3";

// 统一输入框样式，保证整页一致
const INPUT_CLS =
  "w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-gm text-g-fg placeholder:text-g-fg-4 focus:outline-none focus:border-g-blue/50 focus:ring-2 focus:ring-g-blue/15 transition-shadow";
const LABEL_CLS = "block text-xs font-medium text-g-fg-3 mb-1.5";

export default function ModelConfigPage({ onClose }: Props) {
  const showToast = useAppStore((s) => s.showToast);

  // ── Part 1: model list ──
  const [models, setModels] = useState<LlmModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [saving, setSaving] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  // ── Part 2: tier config ──
  const [tierConfig, setTierConfig] = useState<TierConfig>({
    managementPrimary: null,
    managementBackup: null,
    executorPrimary: null,
    executorBackup: null,
    visionPrimary: null,
    visionBackup: null,
  });
  const [tierSaving, setTierSaving] = useState(false);

  // ── Part 3: dedicated image-gen (Seedream) ──
  const [imageGenForm, setImageGenForm] = useState({ ...EMPTY_IMAGE_GEN });
  const [imageGenKeySet, setImageGenKeySet] = useState(false);
  const [imageGenKeyMasked, setImageGenKeyMasked] = useState("");
  const [imageGenSaving, setImageGenSaving] = useState(false);

  // 入场动效
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  const loadModels = useCallback(async () => {
    try {
      const data = await getModels();
      setModels(data);
    } catch (err) {
      console.error("Failed to load models:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadTierConfig = useCallback(async () => {
    try {
      setTierConfig(await getTierConfig());
    } catch (err) {
      console.error("Failed to load tier config:", err);
    }
  }, []);

  const loadImageGenConfig = useCallback(async () => {
    try {
      const cfg = await getImageGenConfig();
      setImageGenForm({
        modelId: cfg.modelId || "",
        baseUrl: cfg.baseUrl || DEFAULT_PLAN_ROOT,
        apiKey: "",
      });
      setImageGenKeySet(cfg.apiKeySet);
      setImageGenKeyMasked(cfg.apiKeyMasked || "");
    } catch (err) {
      console.error("Failed to load image-gen config:", err);
    }
  }, []);

  useEffect(() => {
    loadModels();
    loadTierConfig();
    loadImageGenConfig();
  }, [loadModels, loadTierConfig, loadImageGenConfig]);

  const maskApiKey = (key: string) => {
    if (!key) return "—";
    if (key.length <= 12) return "••••••••";
    return key.slice(0, 8) + "••••" + key.slice(-4);
  };

  // ── Form handlers ──
  const resetForm = () => {
    setForm({ ...EMPTY_FORM });
    setEditingId(null);
    setShowForm(false);
  };

  const startEdit = (model: LlmModel) => {
    setForm({
      name: model.name,
      modelId: model.modelId,
      baseUrl: model.baseUrl,
      apiKey: "", // 列表返回的是脱敏 Key，编辑时留空 = 保持原 Key 不变
      contextWindow: model.contextWindow,
      maxOutputTokens: model.maxOutputTokens,
      supportsThinking: model.supportsThinking,
    });
    setEditingId(model.id);
    setShowForm(true);
  };

  const setField = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const handleDetect = async () => {
    if (!form.baseUrl.trim() || !form.modelId.trim()) {
      showToast("请先填写 Base URL 和模型 ID", "warning");
      return;
    }
    setDetecting(true);
    try {
      const caps = await detectCapabilities({
        baseUrl: form.baseUrl.trim(),
        apiKey: form.apiKey.trim() || undefined,
        modelId: form.modelId.trim(),
      });
      setForm((f) => ({
        ...f,
        contextWindow: caps.contextWindow ?? f.contextWindow,
        maxOutputTokens: caps.maxOutputTokens ?? f.maxOutputTokens,
        supportsThinking: caps.supportsThinking ?? f.supportsThinking,
      }));
      const filled: string[] = [];
      if (caps.contextWindow != null) filled.push("上下文");
      if (caps.maxOutputTokens != null) filled.push("最大输出");
      if (caps.supportsThinking != null) filled.push("思考");
      if (filled.length > 0) {
        showToast(`已探测: ${filled.join(" / ")}（来源 ${caps.source}），可手动调整`, "success");
      } else {
        showToast(caps.error || "未能探测到能力信息，请手动填写", "warning");
      }
    } catch (err: any) {
      showToast(err.message || "探测失败", "error");
    } finally {
      setDetecting(false);
    }
  };

  const handleSubmit = async () => {
    if (!form.name.trim() || !form.modelId.trim() || !form.baseUrl.trim()) {
      showToast("名称、模型 ID、Base URL 为必填项", "warning");
      return;
    }
    if (!form.contextWindow || form.contextWindow <= 0) {
      showToast("上下文窗口必须大于 0", "warning");
      return;
    }
    if (!form.maxOutputTokens || form.maxOutputTokens <= 0) {
      showToast("最大输出必须大于 0", "warning");
      return;
    }
    if (form.maxOutputTokens >= form.contextWindow) {
      showToast("最大输出必须小于上下文窗口", "warning");
      return;
    }
    setSaving(true);
    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      modelId: form.modelId.trim(),
      baseUrl: form.baseUrl.trim(),
      contextWindow: form.contextWindow,
      maxOutputTokens: form.maxOutputTokens,
      supportsThinking: form.supportsThinking,
    };
    // 仅在用户填写了 Key 时才提交，避免编辑其他字段时用脱敏值覆盖真实 Key
    if (form.apiKey.trim()) {
      payload.apiKey = form.apiKey.trim();
    }
    try {
      if (editingId) {
        await updateModel(editingId, payload);
        showToast("模型已更新", "success");
      } else {
        await createModel(payload);
        showToast("模型已添加", "success");
      }
      resetForm();
      await loadModels();
    } catch (err: any) {
      showToast(err.message || "保存失败", "error");
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!confirmDeleteId) return;
    const id = confirmDeleteId;
    setConfirmDeleteId(null);
    try {
      await deleteModel(id);
      showToast("模型已删除", "success");
      await loadModels();
      await loadTierConfig();
    } catch (err: any) {
      showToast(err.message || "删除失败", "error");
    }
  };

  // ── Tier config handlers ──
  const setTierSlot = (slot: keyof TierConfig, value: string) =>
    setTierConfig((c) => ({ ...c, [slot]: value || null }));

  const handleSaveTier = async () => {
    setTierSaving(true);
    try {
      await saveTierConfig(tierConfig);
      showToast("层级配置已保存", "success");
    } catch (err: any) {
      showToast(err.message || "保存失败", "error");
    } finally {
      setTierSaving(false);
    }
  };

  const setImageGenField = <K extends keyof typeof imageGenForm>(
    key: K,
    value: (typeof imageGenForm)[K],
  ) => setImageGenForm((f) => ({ ...f, [key]: value }));

  const handleSaveImageGen = async () => {
    if (!imageGenForm.modelId.trim() || !imageGenForm.baseUrl.trim()) {
      showToast("生图模型 ID 与 Base URL 为必填项", "warning");
      return;
    }
    if (!imageGenForm.apiKey.trim() && !imageGenKeySet) {
      showToast("请填写 API Key", "warning");
      return;
    }
    setImageGenSaving(true);
    try {
      await saveImageGenConfig({
        modelId: imageGenForm.modelId.trim(),
        baseUrl: imageGenForm.baseUrl.trim(),
        apiKey: imageGenForm.apiKey.trim() || undefined,
      });
      showToast("生图模型配置已保存", "success");
      await loadImageGenConfig();
    } catch (err: any) {
      showToast(err.message || "保存失败", "error");
    } finally {
      setImageGenSaving(false);
    }
  };

  const modelOptions = (
    <>
      <option value="">（未设置）</option>
      {models.map((m) => (
        <option key={m.id} value={m.id}>
          {m.name} · {m.modelId}
        </option>
      ))}
    </>
  );

  const SELECT_CLS: Record<string, string> = {
    management:
      "w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-gm text-g-fg focus:outline-none focus:border-g-blue/50 focus:ring-2 focus:ring-g-blue/15 transition-shadow",
    executor:
      "w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-gm text-g-fg focus:outline-none focus:border-g-green/50 focus:ring-2 focus:ring-g-green/15 transition-shadow",
    vision:
      "w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-gm text-g-fg focus:outline-none focus:border-amber-500/50 focus:ring-2 focus:ring-amber-500/15 transition-shadow",
  };

  return (
    <div
      className={`fixed inset-0 bg-black/50 backdrop-blur-[2px] flex items-center justify-center z-50 transition-opacity duration-200 ${entered ? "opacity-100" : "opacity-0"}`}
      onClick={onClose}
    >
      <div
        className={`bg-g-bg border border-g-border rounded-gmLg shadow-gm-pop w-[960px] max-h-[88vh] flex flex-col transform transition-all duration-200 ease-gm-out ${entered ? "opacity-100 translate-y-0 scale-100" : "opacity-0 translate-y-3 scale-[0.98]"}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-7 py-5 border-b border-g-border shrink-0">
          <div>
            <h2 className="text-xl font-semibold text-g-fg tracking-tight">模型配置</h2>
            <p className="text-[13px] text-g-fg-3 mt-1">
              管理对话模型清单、层级槽位，以及独立的生图（Seedream）配置
            </p>
          </div>
          <button
            onClick={onClose}
            className="w-9 h-9 flex items-center justify-center rounded-gm text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted transition-colors"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-7 py-6 space-y-9">
          {/* ─── Part 1: Model List ─── */}
          <section>
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-[13px] font-semibold text-g-fg-2 uppercase tracking-wider flex items-center gap-2">
                <span className="w-1 h-4 rounded-full bg-g-blue shrink-0" />
                模型清单
                <span className="text-g-fg-4 font-normal normal-case tracking-normal">({models.length})</span>
              </h3>
              <button
                onClick={() => { resetForm(); setShowForm(true); }}
                className="px-3.5 py-1.5 text-sm font-medium bg-g-blue text-white rounded-gm shadow-gm-sm hover:shadow-gm hover:brightness-105 active:scale-[0.97] transition-all"
              >
                + 添加模型
              </button>
            </div>

            {loading ? (
              <div className="text-center text-g-fg-3 py-10 text-sm">加载中...</div>
            ) : models.length === 0 && !showForm ? (
              <div className="text-center py-12 border border-dashed border-g-border-strong rounded-gmLg bg-g-bg-soft/40">
                <div className="text-3xl mb-3">🔌</div>
                <p className="text-sm text-g-fg-3">还没有模型，点击「添加模型」开始。</p>
              </div>
            ) : (
              <div className="border border-g-border rounded-gmLg overflow-hidden shadow-gm-sm">
                <table className="w-full table-fixed text-sm">
                  <colgroup>
                    <col className="w-11" />
                    <col className="w-[26%]" />
                    <col className="w-[19%]" />
                    <col className="w-[25%]" />
                    <col className="w-[15%]" />
                    <col className="w-28" />
                  </colgroup>
                  <thead>
                    <tr className="bg-g-bg-soft text-g-fg-3 text-[11px] uppercase tracking-wide">
                      <th className="px-3 py-2.5 text-left font-semibold">#</th>
                      <th className="px-3 py-2.5 text-left font-semibold">名称</th>
                      <th className="px-3 py-2.5 text-left font-semibold">模型 ID</th>
                      <th className="px-3 py-2.5 text-left font-semibold">Base URL</th>
                      <th className="px-3 py-2.5 text-left font-semibold">API Key</th>
                      <th className="px-3 py-2.5 text-right font-semibold">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {models.map((model, idx) => (
                      <tr key={model.id} className="border-t border-g-border hover:bg-g-bg-soft/60 transition-colors align-middle">
                        <td className="px-3 py-3 text-g-fg-4 tabular-nums text-xs">{idx + 1}</td>
                        <td className="px-3 py-3">
                          <div className="flex items-center gap-1.5 min-w-0">
                            <span className="font-medium text-g-fg truncate" title={model.name}>{model.name}</span>
                            {model.supportsThinking && (
                              <span className="shrink-0 whitespace-nowrap text-[10px] leading-none px-1.5 py-1 bg-purple-50 text-purple-600 rounded-gm font-medium">思考</span>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-3 font-mono text-xs text-g-fg-3 truncate" title={model.modelId}>{model.modelId}</td>
                        <td className="px-3 py-3 font-mono text-xs text-g-fg-3 truncate" title={model.baseUrl}>{model.baseUrl}</td>
                        <td className="px-3 py-3 font-mono text-xs text-g-fg-4 truncate" title="API Key 已脱敏">{maskApiKey(model.apiKey)}</td>
                        <td className="px-3 py-3">
                          <div className="flex items-center justify-end gap-1.5">
                            <button
                              onClick={() => startEdit(model)}
                              className="px-2.5 py-1 text-xs rounded-gm border border-g-border text-g-fg-3 hover:text-g-blue hover:border-g-blue/40 hover:bg-g-blue-bg/40 active:scale-[0.96] transition-all"
                            >
                              编辑
                            </button>
                            <button
                              onClick={() => setConfirmDeleteId(model.id)}
                              className="px-2.5 py-1 text-xs rounded-gm border border-g-border text-g-fg-3 hover:text-g-red hover:border-g-red/30 hover:bg-g-red-bg/50 active:scale-[0.96] transition-all"
                            >
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Add/Edit Form */}
            {showForm && (
              <div className="mt-4 border border-g-blue/25 rounded-gmLg bg-g-bg-soft/50 p-5 shadow-gm-sm animate-slide-up">
                <div className="flex items-center justify-between mb-4">
                  <h4 className="text-sm font-semibold text-g-fg">{editingId ? "编辑模型" : "添加模型"}</h4>
                  <button
                    onClick={handleDetect}
                    disabled={detecting}
                    className="px-3 py-1.5 text-xs font-medium bg-purple-600 text-white rounded-gm shadow-gm-sm hover:bg-purple-700 active:scale-[0.97] transition-all disabled:opacity-50 flex items-center gap-1.5"
                  >
                    {detecting ? (
                      <>
                        <span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                        探测中...
                      </>
                    ) : (
                      <>⚡ 自动探测能力</>
                    )}
                  </button>
                </div>
                <div className="space-y-4">
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className={LABEL_CLS}>名称 <span className="text-g-red">*</span></label>
                      <input
                        value={form.name}
                        onChange={(e) => setField("name", e.target.value)}
                        placeholder="例如 DeepSeek V4 Flash"
                        className={INPUT_CLS}
                      />
                    </div>
                    <div>
                      <label className={LABEL_CLS}>模型 ID <span className="text-g-red">*</span></label>
                      <input
                        value={form.modelId}
                        onChange={(e) => setField("modelId", e.target.value)}
                        placeholder="例如 deepseek-v4-flash"
                        className={`${INPUT_CLS} font-mono`}
                      />
                    </div>
                  </div>
                  <div>
                    <label className={LABEL_CLS}>Base URL <span className="text-g-red">*</span></label>
                    <input
                      value={form.baseUrl}
                      onChange={(e) => setField("baseUrl", e.target.value)}
                      placeholder="例如 https://openrouter.ai/api/v1"
                      className={`${INPUT_CLS} font-mono`}
                    />
                  </div>
                  <div>
                    <label className={LABEL_CLS}>API Key</label>
                    <input
                      type="password"
                      value={form.apiKey}
                      onChange={(e) => setField("apiKey", e.target.value)}
                      placeholder={editingId ? "留空则保持原 Key 不变" : "sk-..."}
                      className={`${INPUT_CLS} font-mono`}
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className={LABEL_CLS}>上下文窗口</label>
                      <input
                        type="number"
                        value={form.contextWindow}
                        onChange={(e) => setField("contextWindow", Number(e.target.value))}
                        className={INPUT_CLS}
                      />
                    </div>
                    <div>
                      <label className={LABEL_CLS}>最大输出</label>
                      <input
                        type="number"
                        value={form.maxOutputTokens}
                        onChange={(e) => setField("maxOutputTokens", Number(e.target.value))}
                        className={INPUT_CLS}
                      />
                    </div>
                  </div>
                  <label className="flex items-center gap-2.5 cursor-pointer select-none pt-0.5">
                    <input
                      type="checkbox"
                      checked={form.supportsThinking}
                      onChange={(e) => setField("supportsThinking", e.target.checked)}
                      className="w-4 h-4 rounded border-g-border-strong bg-g-bg text-g-blue focus:ring-g-blue/30"
                    />
                    <span className="text-sm text-g-fg-2">支持思考（推理模型）</span>
                  </label>
                  <div className="flex items-center gap-2 pt-1">
                    <button
                      onClick={handleSubmit}
                      disabled={saving}
                      className="px-4 py-2 text-sm font-medium bg-g-blue text-white rounded-gm shadow-gm-sm hover:shadow-gm hover:brightness-105 active:scale-[0.97] transition-all disabled:opacity-50"
                    >
                      {saving ? "保存中..." : editingId ? "保存修改" : "添加模型"}
                    </button>
                    <button
                      onClick={resetForm}
                      className="px-4 py-2 text-sm text-g-fg-3 hover:text-g-fg rounded-gm hover:bg-g-bg-muted active:scale-[0.97] transition-all"
                    >
                      取消
                    </button>
                  </div>
                </div>
              </div>
            )}
          </section>

          {/* ─── Part 2: Tier Configuration ─── */}
          <section>
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-[13px] font-semibold text-g-fg-2 uppercase tracking-wider flex items-center gap-2">
                <span className="w-1 h-4 rounded-full bg-g-blue shrink-0" />
                层级模型配置
              </h3>
              <button
                onClick={handleSaveTier}
                disabled={tierSaving}
                className="px-3.5 py-1.5 text-sm font-medium bg-g-blue text-white rounded-gm shadow-gm-sm hover:shadow-gm hover:brightness-105 active:scale-[0.97] transition-all disabled:opacity-50"
              >
                {tierSaving ? "保存中..." : "保存配置"}
              </button>
            </div>
            <p className="text-[13px] text-g-fg-3 mb-4 leading-relaxed">
              管理层（CEO / Coordinator）与执行层（Executor / QA / HR）各自指定主用与备用模型。主用模型故障（429 / 5xx）时自动切换到备用。多模态模型专供「帮你看图片」；识图主用失败时自动切备用（同 API Key 跳过）。生图请用下方独立「生图模型设置」面板。
            </p>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
              {/* Management tier */}
              <div className="border border-g-blue/25 rounded-gmLg p-5 bg-g-blue-bg/15 shadow-gm-sm">
                <div className="flex items-center gap-2 mb-4">
                  <span className="w-2.5 h-2.5 rounded-full bg-g-blue" />
                  <span className="text-sm font-semibold text-g-fg">管理层</span>
                  <span className="text-[11px] text-g-fg-4">CEO · Coordinator</span>
                </div>
                <div className="space-y-4">
                  <div>
                    <label className={LABEL_CLS}>主用模型</label>
                    <select
                      value={tierConfig.managementPrimary || ""}
                      onChange={(e) => setTierSlot("managementPrimary", e.target.value)}
                      className={SELECT_CLS.management}
                    >
                      {modelOptions}
                    </select>
                  </div>
                  <div>
                    <label className={LABEL_CLS}>备用模型</label>
                    <select
                      value={tierConfig.managementBackup || ""}
                      onChange={(e) => setTierSlot("managementBackup", e.target.value)}
                      className={SELECT_CLS.management}
                    >
                      {modelOptions}
                    </select>
                  </div>
                </div>
              </div>

              {/* Executor tier */}
              <div className="border border-g-green/25 rounded-gmLg p-5 bg-g-green-bg/20 shadow-gm-sm">
                <div className="flex items-center gap-2 mb-4">
                  <span className="w-2.5 h-2.5 rounded-full bg-g-green" />
                  <span className="text-sm font-semibold text-g-fg">执行层</span>
                  <span className="text-[11px] text-g-fg-4">Executor · QA · HR</span>
                </div>
                <div className="space-y-4">
                  <div>
                    <label className={LABEL_CLS}>主用模型</label>
                    <select
                      value={tierConfig.executorPrimary || ""}
                      onChange={(e) => setTierSlot("executorPrimary", e.target.value)}
                      className={SELECT_CLS.executor}
                    >
                      {modelOptions}
                    </select>
                  </div>
                  <div>
                    <label className={LABEL_CLS}>备用模型</label>
                    <select
                      value={tierConfig.executorBackup || ""}
                      onChange={(e) => setTierSlot("executorBackup", e.target.value)}
                      className={SELECT_CLS.executor}
                    >
                      {modelOptions}
                    </select>
                  </div>
                </div>
              </div>

              {/* Vision / multimodal */}
              <div className="border border-amber-500/25 rounded-gmLg p-5 bg-g-yellow-bg/40 shadow-gm-sm">
                <div className="flex items-center gap-2 mb-4">
                  <span className="w-2.5 h-2.5 rounded-full bg-amber-500" />
                  <span className="text-sm font-semibold text-g-fg">多模态模型配置</span>
                  <span className="text-[11px] text-g-fg-4">帮你看图片</span>
                </div>
                <div className="space-y-4">
                  <div>
                    <label className={LABEL_CLS}>主用模型</label>
                    <select
                      value={tierConfig.visionPrimary || ""}
                      onChange={(e) => setTierSlot("visionPrimary", e.target.value)}
                      className={SELECT_CLS.vision}
                    >
                      {modelOptions}
                    </select>
                  </div>
                  <div>
                    <label className={LABEL_CLS}>备用模型</label>
                    <select
                      value={tierConfig.visionBackup || ""}
                      onChange={(e) => setTierSlot("visionBackup", e.target.value)}
                      className={SELECT_CLS.vision}
                    >
                      {modelOptions}
                    </select>
                  </div>
                </div>
              </div>
            </div>
          </section>

          {/* ─── Part 3: Image generation (Seedream) ─── */}
          <section>
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-[13px] font-semibold text-g-fg-2 uppercase tracking-wider flex items-center gap-2">
                <span className="w-1 h-4 rounded-full bg-violet-500 shrink-0" />
                生图模型设置
                <span className="text-g-fg-4 font-normal normal-case tracking-normal">
                  generate_image · Seedream
                </span>
              </h3>
              <button
                onClick={handleSaveImageGen}
                disabled={imageGenSaving}
                className="px-3.5 py-1.5 text-sm font-medium bg-violet-600 text-white rounded-gm shadow-gm-sm hover:shadow-gm hover:brightness-105 active:scale-[0.97] transition-all disabled:opacity-50"
              >
                {imageGenSaving ? "保存中..." : "保存生图配置"}
              </button>
            </div>
            <p className="text-[13px] text-g-fg-3 mb-4 leading-relaxed">
              专用于 Agent 工具 generate_image。Base URL 填 Agent Plan 根地址（须含{" "}
              <code className="text-[11px]">/api/plan/</code>
              ，勿混用普通 v3 / Coding）。Model ID 填控制台 Seedream id。仅写码角色可用。
            </p>
            <div className="border border-violet-500/25 rounded-gmLg p-5 bg-violet-500/5 shadow-gm-sm space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className={LABEL_CLS}>模型 ID</label>
                  <input
                    value={imageGenForm.modelId}
                    onChange={(e) => setImageGenField("modelId", e.target.value)}
                    placeholder="例如 doubao-seedream-5.0-lite"
                    className={INPUT_CLS}
                  />
                </div>
                <div>
                  <label className={LABEL_CLS}>Base URL</label>
                  <input
                    value={imageGenForm.baseUrl}
                    onChange={(e) => setImageGenField("baseUrl", e.target.value)}
                    placeholder={DEFAULT_PLAN_ROOT}
                    className={INPUT_CLS}
                  />
                </div>
              </div>
              <div>
                <label className={LABEL_CLS}>
                  API Key
                  {imageGenKeySet && !imageGenForm.apiKey && (
                    <span className="ml-2 font-normal text-g-fg-4">
                      已保存 {imageGenKeyMasked}（留空则保持不变）
                    </span>
                  )}
                </label>
                <input
                  type="password"
                  value={imageGenForm.apiKey}
                  onChange={(e) => setImageGenField("apiKey", e.target.value)}
                  placeholder={imageGenKeySet ? "留空保持原 Key" : "ark-…"}
                  className={INPUT_CLS}
                  autoComplete="off"
                />
              </div>
            </div>
          </section>
        </div>
      </div>

      {confirmDeleteId && (
        <ConfirmDialog
          title="删除模型"
          message="确定要删除此模型吗？使用该模型的 Agent 将回退到层级默认模型。"
          confirmLabel="删除"
          danger
          onConfirm={confirmDelete}
          onCancel={() => setConfirmDeleteId(null)}
        />
      )}
    </div>
  );
}
