import { useState } from "react";
import { saveImageGenConfig } from "../../api";
import type { ImageGenConfig } from "../../api";
import { useAppStore } from "../../store";
import { INPUT_CLS, LABEL_CLS, SECTION_TITLE_CLS } from "./styles";

interface Props {
  config: ImageGenConfig;
  onSaved: () => void;
}

const DEFAULT_PLAN_ROOT = "https://ark.cn-beijing.volces.com/api/plan/v3";

export default function ImageGenSection({ config, onSaved }: Props) {
  const showToast = useAppStore((s) => s.showToast);
  const [modelId, setModelId] = useState(config.modelId);
  const [baseUrl, setBaseUrl] = useState(config.baseUrl || DEFAULT_PLAN_ROOT);
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    if (!modelId.trim() || !baseUrl.trim()) {
      showToast("生图模型 ID 与 Base URL 为必填项", "warning");
      return;
    }
    if (!apiKey.trim() && !config.apiKeySet) {
      showToast("请填写 API Key", "warning");
      return;
    }
    setSaving(true);
    try {
      await saveImageGenConfig({
        modelId: modelId.trim(),
        baseUrl: baseUrl.trim(),
        apiKey: apiKey.trim() || undefined,
      });
      showToast("生图模型配置已保存", "success");
      setApiKey("");
      onSaved();
    } catch (err: any) {
      showToast(err.message || "保存失败", "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section>
      <div className="flex items-center justify-between mb-4">
        <h3 className={SECTION_TITLE_CLS}>
          <span className="w-1 h-4 rounded-full bg-violet-500 shrink-0" />
          生图模型设置
          <span className="text-g-fg-4 font-normal normal-case tracking-normal">
            generate_image · Seedream
          </span>
        </h3>
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-3.5 py-1.5 text-sm font-medium bg-violet-600 text-white rounded-gm shadow-gm-sm hover:shadow-gm hover:brightness-105 active:scale-[0.97] transition-all disabled:opacity-50"
        >
          {saving ? "保存中..." : "保存生图配置"}
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
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
              placeholder="例如 doubao-seedream-5.0-lite"
              className={INPUT_CLS}
            />
          </div>
          <div>
            <label className={LABEL_CLS}>Base URL</label>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={DEFAULT_PLAN_ROOT}
              className={INPUT_CLS}
            />
          </div>
        </div>
        <div>
          <label className={LABEL_CLS}>
            API Key
            {config.apiKeySet && !apiKey && (
              <span className="ml-2 font-normal text-g-fg-4">
                已保存 {config.apiKeyMasked}（留空则保持不变）
              </span>
            )}
          </label>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={config.apiKeySet ? "留空保持原 Key" : "ark-…"}
            className={INPUT_CLS}
            autoComplete="off"
          />
        </div>
      </div>
    </section>
  );
}
