import { useState, useEffect } from "react";
import { getModels, createModel, updateModel, deleteModel, testModel } from "../api";
import type { LlmModel } from "../api";
import { useAppStore } from "../store";
import ConfirmDialog from "./ConfirmDialog";

interface Props {
  onClose: () => void;
}

const REASONING_EFFORTS = ["low", "medium", "high", "max"];

export default function ModelSettings({ onClose }: Props) {
  const [models, setModels] = useState<LlmModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{ ok: boolean; latencyMs: number; error?: string } | null>(null);
  // Test-friendly confirm dialog
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  // 入场动效（纯视觉）：遮罩淡入 + 面板滑入
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  // Form state
  const [formName, setFormName] = useState("");
  const [formModelId, setFormModelId] = useState("");
  const [formBaseUrl, setFormBaseUrl] = useState("");
  const [formApiKey, setFormApiKey] = useState("");
  const [formContextWindow, setFormContextWindow] = useState(128000);
  const [formMaxOutputTokens, setFormMaxOutputTokens] = useState(8192);
  const [formSupportsThinking, setFormSupportsThinking] = useState(false);
  const [formReasoningEffort, setFormReasoningEffort] = useState<string>("");
  const [formTemperature, setFormTemperature] = useState("");

  const loadModels = async () => {
    try {
      const data = await getModels();
      setModels(data);
    } catch (err) {
      console.error("Failed to load models:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadModels(); }, []);

  const resetForm = () => {
    setFormName("");
    setFormModelId("");
    setFormBaseUrl("");
    setFormApiKey("");
    setFormContextWindow(128000);
    setFormMaxOutputTokens(8192);
    setFormSupportsThinking(false);
    setFormReasoningEffort("");
    setFormTemperature("");
    setEditingId(null);
    setShowForm(false);
  };

  const startEdit = (model: LlmModel) => {
    setFormName(model.name);
    setFormModelId(model.modelId);
    setFormBaseUrl(model.baseUrl);
    setFormApiKey(model.apiKey);
    setFormContextWindow(model.contextWindow);
    setFormMaxOutputTokens(model.maxOutputTokens);
    setFormSupportsThinking(model.supportsThinking);
    setFormReasoningEffort(model.defaultReasoningEffort || "");
    setFormTemperature(model.temperature || "");
    setEditingId(model.id);
    setShowForm(true);
  };

  const handleSubmit = async () => {
    if (!formName.trim() || !formModelId.trim() || !formBaseUrl.trim() || !formApiKey.trim()) return;

    const payload = {
      name: formName.trim(),
      modelId: formModelId.trim(),
      baseUrl: formBaseUrl.trim(),
      apiKey: formApiKey.trim(),
      contextWindow: formContextWindow,
      maxOutputTokens: formMaxOutputTokens,
      supportsThinking: formSupportsThinking,
      defaultReasoningEffort: formReasoningEffort || null,
      temperature: formTemperature || null,
    };

    try {
      if (editingId) {
        await updateModel(editingId, payload);
      } else {
        await createModel(payload);
      }
      resetForm();
      loadModels();
    } catch (err: any) {
      console.error("Failed to save model:", err);
      useAppStore.getState().showToast(err.message || "Failed to save model", "error");
    }
  };

  const handleDelete = (id: string) => {
    // Test-friendly: custom ConfirmDialog instead of native window.confirm()
    setConfirmDeleteId(id);
  };

  const confirmDelete = async () => {
    if (!confirmDeleteId) return;
    const id = confirmDeleteId;
    setConfirmDeleteId(null);
    try {
      await deleteModel(id);
      loadModels();
    } catch (err: any) {
      console.error("Failed to delete model:", err);
      useAppStore.getState().showToast(err.message || "Failed to delete model", "error");
    }
  };

  const handleTest = async (id: string) => {
    setTestingId(id);
    setTestResult(null);
    try {
      const result = await testModel(id);
      setTestResult(result);
    } catch (err: any) {
      setTestResult({ ok: false, latencyMs: 0, error: err.message });
    } finally {
      setTestingId(null);
      setTimeout(() => setTestResult(null), 5000);
    }
  };

  const maskApiKey = (key: string) => {
    if (key.length <= 12) return "••••••••";
    return key.slice(0, 8) + "••••" + key.slice(-4);
  };

  return (
    <div
      className={`fixed inset-0 bg-black/60 backdrop-blur-[2px] flex items-center justify-center z-50 transition-opacity duration-200 ${entered ? "opacity-100" : "opacity-0"}`}
      onClick={onClose}
    >
      <div
        className={`bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg w-[640px] max-h-[80vh] flex flex-col transform transition-all duration-200 ease-out ${entered ? "opacity-100 translate-y-0 scale-100" : "opacity-0 translate-y-3 scale-[0.98]"}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-g-border">
          <h2 className="text-lg font-semibold text-g-fg">Model Settings</h2>
          <div className="flex items-center gap-2">
            <button
              onClick={() => { resetForm(); setShowForm(true); }}
              className="px-3 py-1.5 text-sm bg-g-blue text-white rounded-gm shadow-gm-sm hover:bg-blue-600 active:scale-[0.97] transition-all"
            >
              + Add Model
            </button>
            <button onClick={onClose} className="w-8 h-8 flex items-center justify-center rounded-gm text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted transition-colors">
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {loading ? (
            <div className="text-center text-g-fg-3 py-8">Loading models...</div>
          ) : models.length === 0 ? (
            <div className="text-center py-10">
              <div className="text-3xl mb-2">🔌</div>
              <p className="text-sm text-g-fg-3">No models configured. Add a model to get started.</p>
            </div>
          ) : (
            <div className="space-y-3">
              {models.map((model, idx) => (
                <div
                  key={model.id}
                  className={`border rounded-gmLg p-4 transition-all hover:shadow-gm ${
                    idx === 0 ? "border-g-blue/40 bg-g-blue-bg/20 shadow-gm-sm" : "border-g-border bg-g-bg shadow-gm-sm"
                  }`}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-g-fg truncate">{model.name}</span>
                        {idx === 0 && (
                          <span className="text-[10px] px-1.5 py-0.5 bg-g-blue-bg text-g-blue rounded-gm font-medium">
                            DEFAULT
                          </span>
                        )}
                        {model.supportsThinking && (
                          <span className="text-[10px] px-1.5 py-0.5 bg-purple-50 text-purple-700 rounded-gm font-medium">
                            THINKING
                          </span>
                        )}
                      </div>
                      <div className="mt-1 text-xs text-g-fg-3 space-y-0.5">
                        <div>Model: <span className="text-g-fg font-mono">{model.modelId}</span></div>
                        <div>API: <span className="text-g-fg font-mono">{maskApiKey(model.apiKey)}</span></div>
                        <div>Context: <span className="text-g-fg">{model.contextWindow.toLocaleString()} tokens</span> · Output: <span className="text-g-fg">{model.maxOutputTokens.toLocaleString()} tokens</span></div>
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5 ml-3">
                      <button
                        onClick={() => handleTest(model.id)}
                        disabled={testingId === model.id}
                        className="px-2 py-1 text-xs rounded-gm border border-g-border text-g-fg-3 hover:text-g-blue hover:border-g-blue/40 hover:bg-g-blue-bg/40 active:scale-[0.96] transition-all disabled:opacity-50"
                      >
                        {testingId === model.id ? "Testing..." : "Test"}
                      </button>
                      <button
                        onClick={() => startEdit(model)}
                        className="px-2 py-1 text-xs rounded-gm border border-g-border text-g-fg-3 hover:text-g-fg hover:border-g-border-strong hover:bg-g-bg-soft active:scale-[0.96] transition-all"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleDelete(model.id)}
                        className="px-2 py-1 text-xs rounded-gm border border-g-border text-g-fg-3 hover:text-red-600 hover:border-red-200 hover:bg-red-50 active:scale-[0.96] transition-all"
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                  {testResult && testingId === null && (
                    <div className={`mt-2 text-xs px-2 py-1 rounded ${testResult.ok ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>
                      {testResult.ok ? `Connection OK (${testResult.latencyMs}ms)` : `Failed: ${testResult.error || "Unknown error"}`}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Add/Edit Form */}
          {showForm && (
            <div className="mt-4 border border-g-blue/30 rounded-gmLg p-4 bg-g-bg-soft shadow-gm-sm">
              <h3 className="text-sm font-medium text-g-fg mb-3">
                {editingId ? "Edit Model" : "Add New Model"}
              </h3>
              <div className="space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-g-fg-3 mb-1">Display Name</label>
                    <input
                      value={formName}
                      onChange={(e) => setFormName(e.target.value)}
                      placeholder="e.g. DeepSeek V4 Flash"
                      className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-md text-g-fg focus:outline-none focus:border-g-blue/40"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-g-fg-3 mb-1">Model ID</label>
                    <input
                      value={formModelId}
                      onChange={(e) => setFormModelId(e.target.value)}
                      placeholder="e.g. deepseek-v4-flash"
                      className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-md text-g-fg font-mono focus:outline-none focus:border-g-blue/40"
                    />
                  </div>
                </div>
                <div>
                  <label className="block text-xs text-g-fg-3 mb-1">Base URL</label>
                    <input
                      value={formBaseUrl}
                      onChange={(e) => setFormBaseUrl(e.target.value)}
                      placeholder="e.g. https://api.deepseek.com"
                      className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-md text-g-fg font-mono focus:outline-none focus:border-g-blue/40"
                  />
                </div>
                <div>
                  <label className="block text-xs text-g-fg-3 mb-1">API Key</label>
                  <input
                    type="password"
                    value={formApiKey}
                    onChange={(e) => setFormApiKey(e.target.value)}
                    placeholder="sk-..."
                    className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-md text-g-fg font-mono focus:outline-none focus:border-g-blue/40"
                  />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-g-fg-3 mb-1">Context Window</label>
                    <input
                      type="number"
                      value={formContextWindow}
                      onChange={(e) => setFormContextWindow(Number(e.target.value))}
                      className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-md text-g-fg focus:outline-none focus:border-g-blue/40"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-g-fg-3 mb-1">Max Output Tokens</label>
                    <input
                      type="number"
                      value={formMaxOutputTokens}
                      onChange={(e) => setFormMaxOutputTokens(Number(e.target.value))}
                      className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-md text-g-fg focus:outline-none focus:border-g-blue/40"
                    />
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-g-fg-3 mb-1">Temperature (optional)</label>
                    <input
                      value={formTemperature}
                      onChange={(e) => setFormTemperature(e.target.value)}
                      placeholder="e.g. 0.7"
                      className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-md text-g-fg focus:outline-none focus:border-g-blue/40"
                    />
                  </div>
                  <div className="flex items-end">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={formSupportsThinking}
                        onChange={(e) => setFormSupportsThinking(e.target.checked)}
                        className="rounded border-g-border bg-g-bg text-g-blue focus:ring-g-blue/30"
                      />
                      <span className="text-sm text-g-fg">Supports Thinking</span>
                    </label>
                  </div>
                </div>
                {formSupportsThinking && (
                  <div>
                    <label className="block text-xs text-g-fg-3 mb-1">Default Reasoning Effort</label>
                    <select
                      value={formReasoningEffort}
                      onChange={(e) => setFormReasoningEffort(e.target.value)}
                      className="w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-md text-g-fg focus:outline-none focus:border-g-blue/40"
                    >
                      <option value="">None</option>
                      {REASONING_EFFORTS.map((effort) => (
                        <option key={effort} value={effort}>
                          {effort.charAt(0).toUpperCase() + effort.slice(1)}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
                <div className="flex items-center gap-2 pt-2">
                  <button
                    onClick={handleSubmit}
                    className="px-4 py-2 text-sm bg-g-blue text-white rounded-gm shadow-gm-sm hover:bg-blue-600 active:scale-[0.97] transition-all"
                  >
                    {editingId ? "Save Changes" : "Add Model"}
                  </button>
                  <button
                    onClick={resetForm}
                    className="px-4 py-2 text-sm text-g-fg-3 hover:text-g-fg rounded-gm hover:bg-g-bg-muted active:scale-[0.97] transition-all"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Confirm Dialog — test-friendly */}
      {confirmDeleteId && (
        <ConfirmDialog
          title="删除模型"
          message="确定要删除此模型吗？使用该模型的 Agent 将回退到默认模型。"
          confirmLabel="删除"
          danger
          onConfirm={confirmDelete}
          onCancel={() => setConfirmDeleteId(null)}
        />
      )}
    </div>
  );
}
