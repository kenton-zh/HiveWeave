import { useState, useEffect, useCallback } from "react";
import {
  getModels,
  deleteModel,
  getTierConfig,
  getImageGenConfig,
} from "../api";
import type { ImageGenConfig, LlmModel, TierConfig } from "../api";
import { useAppStore } from "../store";
import ConfirmDialog from "./ConfirmDialog";
import ProviderPickerDialog from "./model-config/ProviderPickerDialog";
import ModelFormDialog from "./model-config/ModelFormDialog";
import ModelCard from "./model-config/ModelCard";
import TierSlotSection from "./model-config/TierSlotSection";
import ImageGenSection from "./model-config/ImageGenSection";
import type { FormMode } from "./model-config/types";
import { SECTION_TITLE_CLS } from "./model-config/styles";

interface Props {
  onClose: () => void;
}

const EMPTY_TIER: TierConfig = {
  managementPrimary: null,
  managementBackup: null,
  executorPrimary: null,
  executorBackup: null,
  visionPrimary: null,
  visionBackup: null,
};

const EMPTY_IMAGE_GEN: ImageGenConfig = {
  modelId: "",
  baseUrl: "",
  apiKeySet: false,
  apiKeyMasked: "",
};

export default function ModelConfigPage({ onClose }: Props) {
  const showToast = useAppStore((s) => s.showToast);

  const [models, setModels] = useState<LlmModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [tierConfig, setTierConfig] = useState<TierConfig>(EMPTY_TIER);
  const [imageGenConfig, setImageGenConfig] = useState<ImageGenConfig>(EMPTY_IMAGE_GEN);

  // 对话框编排：picker → form（preset/custom）；edit 直接进 form
  const [showPicker, setShowPicker] = useState(false);
  const [formMode, setFormMode] = useState<FormMode | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  const loadModels = useCallback(async () => {
    try {
      setModels(await getModels());
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
      setImageGenConfig(await getImageGenConfig());
    } catch (err) {
      console.error("Failed to load image-gen config:", err);
    }
  }, []);

  useEffect(() => {
    loadModels();
    loadTierConfig();
    loadImageGenConfig();
  }, [loadModels, loadTierConfig, loadImageGenConfig]);

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
              知名服务商只填 API Key 即可接入；其他兼容网关走「自定义模型」。层级槽位与生图配置在下方。
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
          {/* ─── Part 1: Model cards ─── */}
          <section>
            <div className="flex items-center justify-between mb-4">
              <h3 className={SECTION_TITLE_CLS}>
                <span className="w-1 h-4 rounded-full bg-g-blue shrink-0" />
                模型清单
                <span className="text-g-fg-4 font-normal normal-case tracking-normal">({models.length})</span>
              </h3>
              <button
                onClick={() => setShowPicker(true)}
                className="px-3.5 py-1.5 text-sm font-medium bg-g-blue text-white rounded-gm shadow-gm-sm hover:shadow-gm hover:brightness-105 active:scale-[0.97] transition-all"
              >
                + 添加模型
              </button>
            </div>

            {loading ? (
              <div className="text-center text-g-fg-3 py-10 text-sm">加载中...</div>
            ) : models.length === 0 ? (
              <div className="text-center py-12 border border-dashed border-g-border-strong rounded-gmLg bg-g-bg-soft/40">
                <div className="text-3xl mb-3">🔌</div>
                <p className="text-sm text-g-fg-3">还没有模型，点击「添加模型」开始。</p>
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5">
                {models.map((m) => (
                  <ModelCard
                    key={m.id}
                    model={m}
                    onEdit={(model) => setFormMode({ kind: "edit", model })}
                    onDelete={(id) => setConfirmDeleteId(id)}
                  />
                ))}
              </div>
            )}
          </section>

          {/* ─── Part 2: Tier slots ─── */}
          <TierSlotSection
            models={models}
            tierConfig={tierConfig}
            onChange={setTierConfig}
            onSaved={loadTierConfig}
          />

          {/* ─── Part 3: Image gen ─── */}
          {/* key 强制重挂载：配置异步加载/保存后刷新时用新值重建表单，避免捕获空 props */}
          <ImageGenSection
            key={`${imageGenConfig.modelId}|${imageGenConfig.baseUrl}|${imageGenConfig.apiKeySet}`}
            config={imageGenConfig}
            onSaved={loadImageGenConfig}
          />
        </div>
      </div>

      {showPicker && !formMode && (
        <ProviderPickerDialog
          onPick={(mode) => {
            setShowPicker(false);
            setFormMode(mode);
          }}
          onClose={() => setShowPicker(false)}
        />
      )}

      {formMode && (
        <ModelFormDialog
          mode={formMode}
          onBack={
            formMode.kind === "edit"
              ? undefined
              : () => {
                  setFormMode(null);
                  setShowPicker(true);
                }
          }
          onSaved={() => {
            setFormMode(null);
            loadModels();
          }}
          onClose={() => setFormMode(null)}
        />
      )}

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
