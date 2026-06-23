import { useState, useEffect } from "react";
import { getModels, createModel, updateModel, deleteModel, testModel } from "../api";
import type { LlmModel } from "../api";

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
      alert(err.message || "Failed to save model");
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this model? Agents using it will fall back to the default model.")) return;
    try {
      await deleteModel(id);
      loadModels();
    } catch (err: any) {
      console.error("Failed to delete model:", err);
      alert(err.message || "Failed to delete model");
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
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-surface-card border border-surface-border rounded-xl shadow-2xl w-[640px] max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-border">
          <h2 className="text-lg font-semibold text-gray-100">Model Settings</h2>
          <div className="flex items-center gap-2">
            <button
              onClick={() => { resetForm(); setShowForm(true); }}
              className="px-3 py-1.5 text-sm bg-accent text-white rounded-md hover:bg-accent/90 transition-colors"
            >
              + Add Model
            </button>
            <button onClick={onClose} className="text-gray-400 hover:text-gray-200 transition-colors">
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {loading ? (
            <div className="text-center text-gray-400 py-8">Loading models...</div>
          ) : models.length === 0 ? (
            <div className="text-center text-gray-400 py-8">
              No models configured. Add a model to get started.
            </div>
          ) : (
            <div className="space-y-3">
              {models.map((model, idx) => (
                <div
                  key={model.id}
                  className={`border rounded-lg p-4 transition-colors ${
                    idx === 0 ? "border-accent/50 bg-accent/5" : "border-surface-border bg-surface"
                  }`}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-gray-100 truncate">{model.name}</span>
                        {idx === 0 && (
                          <span className="text-[10px] px-1.5 py-0.5 bg-accent/20 text-accent rounded font-medium">
                            DEFAULT
                          </span>
                        )}
                        {model.supportsThinking && (
                          <span className="text-[10px] px-1.5 py-0.5 bg-purple-500/20 text-purple-400 rounded font-medium">
                            THINKING
                          </span>
                        )}
                      </div>
                      <div className="mt-1 text-xs text-gray-400 space-y-0.5">
                        <div>Model: <span className="text-gray-300 font-mono">{model.modelId}</span></div>
                        <div>API: <span className="text-gray-300 font-mono">{maskApiKey(model.apiKey)}</span></div>
                        <div>Context: <span className="text-gray-300">{model.contextWindow.toLocaleString()} tokens</span> · Output: <span className="text-gray-300">{model.maxOutputTokens.toLocaleString()} tokens</span></div>
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5 ml-3">
                      <button
                        onClick={() => handleTest(model.id)}
                        disabled={testingId === model.id}
                        className="px-2 py-1 text-xs rounded border border-surface-border text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-colors disabled:opacity-50"
                      >
                        {testingId === model.id ? "Testing..." : "Test"}
                      </button>
                      <button
                        onClick={() => startEdit(model)}
                        className="px-2 py-1 text-xs rounded border border-surface-border text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-colors"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleDelete(model.id)}
                        className="px-2 py-1 text-xs rounded border border-surface-border text-gray-400 hover:text-red-400 hover:border-red-400/50 transition-colors"
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                  {testResult && testingId === null && (
                    <div className={`mt-2 text-xs px-2 py-1 rounded ${testResult.ok ? "bg-emerald-500/10 text-emerald-400" : "bg-red-500/10 text-red-400"}`}>
                      {testResult.ok ? `Connection OK (${testResult.latencyMs}ms)` : `Failed: ${testResult.error || "Unknown error"}`}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Add/Edit Form */}
          {showForm && (
            <div className="mt-4 border border-accent/30 rounded-lg p-4 bg-surface">
              <h3 className="text-sm font-medium text-gray-200 mb-3">
                {editingId ? "Edit Model" : "Add New Model"}
              </h3>
              <div className="space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Display Name</label>
                    <input
                      value={formName}
                      onChange={(e) => setFormName(e.target.value)}
                      placeholder="e.g. DeepSeek V4 Flash"
                      className="w-full px-3 py-2 text-sm bg-surface-card border border-surface-border rounded-md text-gray-200 focus:outline-none focus:border-accent/50"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Model ID</label>
                    <input
                      value={formModelId}
                      onChange={(e) => setFormModelId(e.target.value)}
                      placeholder="e.g. deepseek-v4-flash"
                      className="w-full px-3 py-2 text-sm bg-surface-card border border-surface-border rounded-md text-gray-200 font-mono focus:outline-none focus:border-accent/50"
                    />
                  </div>
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Base URL</label>
                  <input
                    value={formBaseUrl}
                    onChange={(e) => setFormBaseUrl(e.target.value)}
                    placeholder="e.g. https://api.deepseek.com"
                    className="w-full px-3 py-2 text-sm bg-surface-card border border-surface-border rounded-md text-gray-200 font-mono focus:outline-none focus:border-accent/50"
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1">API Key</label>
                  <input
                    type="password"
                    value={formApiKey}
                    onChange={(e) => setFormApiKey(e.target.value)}
                    placeholder="sk-..."
                    className="w-full px-3 py-2 text-sm bg-surface-card border border-surface-border rounded-md text-gray-200 font-mono focus:outline-none focus:border-accent/50"
                  />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Context Window</label>
                    <input
                      type="number"
                      value={formContextWindow}
                      onChange={(e) => setFormContextWindow(Number(e.target.value))}
                      className="w-full px-3 py-2 text-sm bg-surface-card border border-surface-border rounded-md text-gray-200 focus:outline-none focus:border-accent/50"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Max Output Tokens</label>
                    <input
                      type="number"
                      value={formMaxOutputTokens}
                      onChange={(e) => setFormMaxOutputTokens(Number(e.target.value))}
                      className="w-full px-3 py-2 text-sm bg-surface-card border border-surface-border rounded-md text-gray-200 focus:outline-none focus:border-accent/50"
                    />
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Temperature (optional)</label>
                    <input
                      value={formTemperature}
                      onChange={(e) => setFormTemperature(e.target.value)}
                      placeholder="e.g. 0.7"
                      className="w-full px-3 py-2 text-sm bg-surface-card border border-surface-border rounded-md text-gray-200 focus:outline-none focus:border-accent/50"
                    />
                  </div>
                  <div className="flex items-end">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={formSupportsThinking}
                        onChange={(e) => setFormSupportsThinking(e.target.checked)}
                        className="rounded border-surface-border bg-surface-card text-accent focus:ring-accent/30"
                      />
                      <span className="text-sm text-gray-300">Supports Thinking</span>
                    </label>
                  </div>
                </div>
                {formSupportsThinking && (
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Default Reasoning Effort</label>
                    <select
                      value={formReasoningEffort}
                      onChange={(e) => setFormReasoningEffort(e.target.value)}
                      className="w-full px-3 py-2 text-sm bg-surface-card border border-surface-border rounded-md text-gray-200 focus:outline-none focus:border-accent/50"
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
                    className="px-4 py-2 text-sm bg-accent text-white rounded-md hover:bg-accent/90 transition-colors"
                  >
                    {editingId ? "Save Changes" : "Add Model"}
                  </button>
                  <button
                    onClick={resetForm}
                    className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
