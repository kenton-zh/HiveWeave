import { useMemo, useState } from "react";
import {
  createModel,
  updateModel,
  detectCapabilities,
  testModelConnection,
} from "../../api";
import type { PresetModel, TestConnectionResult } from "../../api";
import { useAppStore } from "../../store";
import {
  PROTOCOL_OPTIONS,
  applyWireEndpoint,
} from "../../utils/wireEndpoint";
import AdvancedFields from "./AdvancedFields";
import type { FormMode, ModelFormState } from "./types";
import { EMPTY_FORM, formFromModel } from "./types";
import { INPUT_CLS, LABEL_CLS, PRIMARY_BTN_CLS, GHOST_BTN_CLS } from "./styles";

interface Props {
  mode: FormMode;
  /** 返回服务商宫格（preset/custom 从宫格进来时才有） */
  onBack?: () => void;
  onSaved: () => void;
  onClose: () => void;
}

function initForm(mode: FormMode): ModelFormState {
  if (mode.kind === "edit") return formFromModel(mode.model);
  if (mode.kind === "preset") {
    const p = mode.preset;
    const first = p.models[0];
    return {
      ...EMPTY_FORM,
      baseUrl: p.base_url,
      providerType: p.api_format,
      ...(first ? formFromPresetModel(first) : {}),
    };
  }
  return { ...EMPTY_FORM };
}

function formFromPresetModel(m: PresetModel): Partial<ModelFormState> {
  return {
    name: m.name,
    modelId: m.id,
    contextWindow: m.context_window,
    maxOutputTokens: m.max_output_tokens,
    supportsVision: m.vision,
    thinkingMode: "",
    thinkingFormat: m.thinking_format,
  };
}

function parseOptionalNumber(raw: string, isInt: boolean, min = 0): number | undefined {
  const t = raw.trim();
  if (!t) return undefined;
  const n = Number(t);
  if (!Number.isFinite(n) || n < min) return undefined;
  return isInt ? Math.round(n) : n;
}

export default function ModelFormDialog({ mode, onBack, onSaved, onClose }: Props) {
  const showToast = useAppStore((s) => s.showToast);
  const [form, setFormState] = useState<ModelFormState>(() => initForm(mode));
  const [showAdvanced, setShowAdvanced] = useState(mode.kind === "custom");
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(null);

  const isEdit = mode.kind === "edit";
  const preset = mode.kind === "preset" ? mode.preset : null;
  const editingId = isEdit ? mode.model.id : null;

  const setField = <K extends keyof ModelFormState>(key: K, value: ModelFormState[K]) => {
    setFormState((f) => ({ ...f, [key]: value }));
    if (key === "baseUrl" || key === "apiKey" || key === "modelId" || key === "providerType") {
      setTestResult(null); // 连接信息变了，旧测试结果作废
    }
  };

  const presetModels = useMemo(() => preset?.models ?? [], [preset]);

  const pickPresetModel = (modelId: string) => {
    const m = presetModels.find((x) => x.id === modelId);
    if (m) setFormState((f) => ({ ...f, ...formFromPresetModel(m) }));
    setTestResult(null); // 换模型 = 连接目标变了，旧测试结果作废
  };

  const handleUrlBlur = () => {
    if (form.fullUrl || preset) return;
    const { prefix, protocol } = applyWireEndpoint(form.baseUrl, form.providerType);
    setFormState((f) => ({ ...f, baseUrl: prefix, providerType: protocol }));
  };

  const handleDetect = async () => {
    if (!form.baseUrl.trim() || !form.modelId.trim()) {
      showToast("请先填写请求地址和模型 ID", "warning");
      return;
    }
    setDetecting(true);
    try {
      const caps = await detectCapabilities({
        baseUrl: form.baseUrl.trim(),
        apiKey: form.apiKey.trim() || undefined,
        modelId: form.modelId.trim(),
      });
      setFormState((f) => ({
        ...f,
        contextWindow: caps.contextWindow ?? f.contextWindow,
        maxOutputTokens: caps.maxOutputTokens ?? f.maxOutputTokens,
        // 探测到推理能力 → 意图置「开启」；探测不到不动用户选择
        thinkingMode: caps.supportsThinking === true ? "on" : f.thinkingMode,
      }));
      const filled: string[] = [];
      if (caps.contextWindow != null) filled.push("上下文");
      if (caps.maxOutputTokens != null) filled.push("最大输出");
      if (caps.supportsThinking === true) filled.push("思考(已开启)");
      if (filled.length > 0) {
        showToast(`已侦测: ${filled.join(" / ")}，可手动调整`, "success");
      } else {
        showToast(caps.error || "未能侦测到配置信息，请手动填写", "warning");
      }
    } catch (err: any) {
      showToast(err.message || "侦测失败", "error");
    } finally {
      setDetecting(false);
    }
  };

  const handleTest = async () => {
    if (!form.baseUrl.trim() || !form.modelId.trim()) {
      showToast("请先填写请求地址和模型 ID", "warning");
      return;
    }
    if (!form.apiKey.trim()) {
      showToast(
        isEdit ? "测试连接需要重新输入 API Key（保存时留空才会沿用原 Key）" : "请先填写 API Key",
        "warning",
      );
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testModelConnection({
        baseUrl: form.baseUrl.trim(),
        apiKey: form.apiKey.trim(),
        modelId: form.modelId.trim(),
        providerType: form.providerType,
        contextWindow: form.contextWindow,
        maxOutputTokens: form.maxOutputTokens,
        // 带上思考配置，否则 thinkingWarning 按默认方言算、可能与用户选择无关
        supportsThinking: form.thinkingMode === "on" ? true : undefined,
        thinkingFormat: form.thinkingMode === "off" ? "off" : form.thinkingFormat || undefined,
      });
      setTestResult(result);
      if (result.ok) {
        showToast(`连接成功（${result.latencyMs}ms）`, "success");
      } else {
        showToast(result.error || "连接失败", "error");
      }
    } catch (err: any) {
      setTestResult({ ok: false, latencyMs: 0, error: err.message || "请求失败" });
      showToast(err.message || "测试失败", "error");
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    if (!form.name.trim() || !form.modelId.trim() || !form.baseUrl.trim()) {
      showToast("展示名称、模型 ID、请求地址为必填项", "warning");
      return;
    }
    if (!isEdit && !form.apiKey.trim()) {
      showToast("请填写 API Key", "warning");
      return;
    }
    if (form.maxOutputTokens >= form.contextWindow) {
      showToast("最大输出必须小于上下文窗口", "warning");
      return;
    }
    const wired = preset
      ? { prefix: preset.base_url, protocol: preset.api_format }
      : form.fullUrl
        ? { prefix: form.baseUrl.trim().replace(/\/+$/, ""), protocol: form.providerType }
        : applyWireEndpoint(form.baseUrl.trim(), form.providerType);

    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      modelId: form.modelId.trim(),
      baseUrl: wired.prefix,
      providerType: wired.protocol,
      contextWindow: form.contextWindow,
      maxOutputTokens: form.maxOutputTokens,
      supportsVision: form.supportsVision,
      modelFamily: form.modelFamily.trim(),
      thinkingMode: form.thinkingMode,
      // 「off」方言是「关闭思考」写入的持久痕迹：切回 开启/跟随默认 时必须显式清除，
      // 否则方言层仍压制思考且 UI 无入口可解除（审计 P1-2）
      thinkingFormat:
        form.thinkingMode !== "off" && form.thinkingFormat === "off" ? "" : form.thinkingFormat,
    };
    // 思考强度仅在开启时下发；预设模型的推理能力作为已知能力位下发
    if (form.thinkingMode === "on") {
      payload.defaultReasoningEffort = form.defaultReasoningEffort;
    }
    const presetModel = presetModels.find((m) => m.id === form.modelId);
    if (presetModel) {
      payload.supportsThinking = presetModel.reasoning;
    }
    const temperature = parseOptionalNumber(form.temperature, false);
    const topP = parseOptionalNumber(form.topP, false);
    // topK/工具轮数 0 无意义（0 轮 = 禁用工具，语义太重），按留空处理
    const topK = parseOptionalNumber(form.topK, true, 1);
    const rounds = parseOptionalNumber(form.toolCallRounds, true, 1);
    if (temperature !== undefined) payload.temperature = temperature;
    if (topP !== undefined) payload.topP = topP;
    if (topK !== undefined) payload.topK = topK;
    if (rounds !== undefined) payload.toolCallRounds = rounds;
    if (form.apiKey.trim()) payload.apiKey = form.apiKey.trim();

    setSaving(true);
    try {
      if (editingId) {
        await updateModel(editingId, payload);
        showToast("模型已更新", "success");
      } else {
        await createModel(payload);
        showToast("模型已添加", "success");
      }
      onSaved();
    } catch (err: any) {
      showToast(err.message || "保存失败", "error");
    } finally {
      setSaving(false);
    }
  };

  const title = isEdit
    ? "编辑模型"
    : preset
      ? preset.name
      : "自定义模型";

  return (
    <div
      className="fixed inset-0 bg-black/50 backdrop-blur-[2px] flex items-center justify-center z-[60]"
      onClick={onClose}
    >
      <div
        className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-pop w-[620px] max-h-[86vh] flex flex-col animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-g-border shrink-0">
          <div className="flex items-center gap-2.5 min-w-0">
            {onBack && (
              <button
                onClick={onBack}
                className="w-7 h-7 shrink-0 flex items-center justify-center rounded-gm text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted transition-colors"
                title="重选服务商"
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
                </svg>
              </button>
            )}
            <h3 className="text-base font-semibold text-g-fg truncate">{title}</h3>
            {preset && (
              <span className="shrink-0 text-[10px] px-1.5 py-0.5 bg-g-bg-muted text-g-fg-3 rounded-gm font-medium">
                {preset.api_format === "anthropic" ? "Anthropic" : "Chat Completions"}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 shrink-0 flex items-center justify-center rounded-gm text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted transition-colors"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
          {!preset && (
            <div>
              <label className={LABEL_CLS}>API 格式 <span className="text-g-red">*</span></label>
              <select
                value={form.providerType}
                onChange={(e) => setField("providerType", e.target.value)}
                className={INPUT_CLS}
              >
                {PROTOCOL_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>
          )}

          {preset && (
            <div>
              <label className={LABEL_CLS}>请求地址（已预置）</label>
              <div className="px-3 py-2 text-sm font-mono text-g-fg-3 bg-g-bg-soft border border-g-border rounded-gm truncate">
                {preset.base_url}
              </div>
            </div>
          )}

          {!preset && (
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <label className="block text-xs font-medium text-g-fg-3">
                  自定义请求地址 <span className="text-g-red">*</span>
                </label>
                <label className="flex items-center gap-1.5 cursor-pointer select-none">
                  <span className="text-[11px] text-g-fg-4">完整 URL</span>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={form.fullUrl}
                    onClick={() => setField("fullUrl", !form.fullUrl)}
                    className={`relative w-8 h-[18px] rounded-full transition-colors ${
                      form.fullUrl ? "bg-g-blue" : "bg-g-border-strong"
                    }`}
                  >
                    <span
                      className={`absolute top-[2px] w-3.5 h-3.5 rounded-full bg-white shadow transition-transform ${
                        form.fullUrl ? "translate-x-[16px]" : "translate-x-[2px]"
                      }`}
                    />
                  </button>
                </label>
              </div>
              <input
                value={form.baseUrl}
                onChange={(e) => setField("baseUrl", e.target.value)}
                onBlur={handleUrlBlur}
                placeholder="https://api.example.com/v1"
                className={`${INPUT_CLS} font-mono`}
              />
              <p className="mt-1 text-[11px] text-g-fg-4 leading-snug">
                {form.fullUrl
                  ? "完整 URL 模式：原样使用，不自动剥路径。"
                  : "填网关前缀（停在 /v1）；粘贴完整端点时失焦自动剥路径并识别协议。"}
              </p>
            </div>
          )}

          {preset && presetModels.length > 0 && (
            <div>
              <label className={LABEL_CLS}>模型 <span className="text-g-red">*</span></label>
              <select
                value={form.modelId}
                onChange={(e) => pickPresetModel(e.target.value)}
                className={INPUT_CLS}
              >
                {presetModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name} · {(m.context_window / 1000).toFixed(0)}k 上下文
                    {m.vision ? " · 视觉" : ""}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className={preset && presetModels.length > 0 ? "" : "grid grid-cols-2 gap-4"}>
            {(!preset || presetModels.length === 0) && (
              <div>
                <label className={LABEL_CLS}>模型 ID <span className="text-g-red">*</span></label>
                <input
                  value={form.modelId}
                  onChange={(e) => setField("modelId", e.target.value)}
                  placeholder="例如 deepseek-v4-flash"
                  className={`${INPUT_CLS} font-mono`}
                />
              </div>
            )}
            <div>
              <label className={LABEL_CLS}>模型展示名称 <span className="text-g-red">*</span></label>
              <input
                value={form.name}
                onChange={(e) => setField("name", e.target.value)}
                placeholder="例如 DeepSeek V4 Flash"
                className={INPUT_CLS}
              />
            </div>
          </div>

          <div>
            <label className={LABEL_CLS}>API 密钥 <span className="text-g-red">*</span></label>
            <div className="relative">
              <input
                type={showKey ? "text" : "password"}
                value={form.apiKey}
                onChange={(e) => setField("apiKey", e.target.value)}
                placeholder={isEdit ? "留空则保持原 Key 不变" : "sk-..."}
                autoComplete="off"
                className={`${INPUT_CLS} font-mono pr-10`}
              />
              <button
                type="button"
                onClick={() => setShowKey((v) => !v)}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-g-fg-4 hover:text-g-fg transition-colors"
                title={showKey ? "隐藏" : "显示"}
              >
                {showKey ? (
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                  </svg>
                ) : (
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                  </svg>
                )}
              </button>
            </div>
          </div>

          {/* 高级配置（可折叠） */}
          <div className="border border-g-border rounded-gmLg overflow-hidden">
            <button
              type="button"
              onClick={() => setShowAdvanced((v) => !v)}
              className="w-full flex items-center justify-between px-4 py-2.5 bg-g-bg-soft/60 hover:bg-g-bg-soft transition-colors"
            >
              <span className="text-xs font-semibold text-g-fg-2">高级配置</span>
              <svg
                className={`w-4 h-4 text-g-fg-3 transition-transform ${showAdvanced ? "rotate-180" : ""}`}
                fill="none" viewBox="0 0 24 24" stroke="currentColor"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {showAdvanced && (
              <div className="px-4 py-4 border-t border-g-border">
                <AdvancedFields form={form} setField={setField} />
              </div>
            )}
          </div>

          {/* 连接测试结果 */}
          {testResult && (
            <div
              className={`px-4 py-3 rounded-gmLg border text-xs leading-relaxed ${
                testResult.ok
                  ? "border-g-green/30 bg-g-green-bg/30 text-g-green"
                  : "border-g-red/30 bg-g-red-bg/40 text-g-red"
              }`}
            >
              {testResult.ok ? (
                <>
                  ✓ 连接成功 · {testResult.latencyMs}ms
                  {testResult.response ? ` · 回复「${testResult.response.slice(0, 40)}」` : ""}
                  {testResult.detectedSupportsThinking != null &&
                    ` · 思考=${testResult.detectedSupportsThinking ? "支持" : "不支持"}`}
                </>
              ) : (
                <>✗ {testResult.error}</>
              )}
              {(testResult.contextWindowWarning || testResult.maxOutputWarning || testResult.thinkingWarning) && (
                <div className="mt-1.5 text-amber-600">
                  {[testResult.contextWindowWarning, testResult.maxOutputWarning, testResult.thinkingWarning]
                    .filter(Boolean)
                    .join("；")}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-g-border shrink-0">
          <div className="flex items-center gap-2">
            <button
              onClick={handleDetect}
              disabled={detecting}
              className="px-3 py-1.5 text-xs font-medium border border-purple-500/40 text-purple-600 rounded-gm hover:bg-purple-500/10 active:scale-[0.97] transition-all disabled:opacity-50 flex items-center gap-1.5"
            >
              {detecting ? (
                <>
                  <span className="w-3 h-3 border-2 border-purple-500/40 border-t-purple-600 rounded-full animate-spin" />
                  侦测中...
                </>
              ) : (
                <>⚡ 自动侦测配置</>
              )}
            </button>
            <button
              onClick={handleTest}
              disabled={testing}
              className="px-3 py-1.5 text-xs font-medium border border-g-blue/40 text-g-blue rounded-gm hover:bg-g-blue-bg/40 active:scale-[0.97] transition-all disabled:opacity-50 flex items-center gap-1.5"
            >
              {testing ? (
                <>
                  <span className="w-3 h-3 border-2 border-g-blue/40 border-t-g-blue rounded-full animate-spin" />
                  测试中...
                </>
              ) : (
                <>⇄ 测试连接</>
              )}
            </button>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={onClose} className={GHOST_BTN_CLS}>取消</button>
            <button onClick={handleSave} disabled={saving} className={PRIMARY_BTN_CLS}>
              {saving ? "保存中..." : isEdit ? "保存修改" : "添加模型"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
