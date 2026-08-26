import { useState } from "react";
import { saveTierConfig } from "../../api";
import type { LlmModel, TierConfig } from "../../api";
import { useAppStore } from "../../store";
import { LABEL_CLS, SECTION_TITLE_CLS } from "./styles";

interface Props {
  models: LlmModel[];
  tierConfig: TierConfig;
  onChange: (cfg: TierConfig) => void;
  onSaved: () => void;
}

const SELECT_BASE =
  "w-full px-3 py-2 text-sm bg-g-bg border border-g-border rounded-gm text-g-fg focus:outline-none transition-shadow";

const SLOT_SELECT_CLS: Record<string, string> = {
  management: `${SELECT_BASE} focus:border-g-blue/50 focus:ring-2 focus:ring-g-blue/15`,
  executor: `${SELECT_BASE} focus:border-g-green/50 focus:ring-2 focus:ring-g-green/15`,
  vision: `${SELECT_BASE} focus:border-amber-500/50 focus:ring-2 focus:ring-amber-500/15`,
};

function TierCard({
  title,
  subtitle,
  dotCls,
  cardCls,
  selectCls,
  models,
  primary,
  backup,
  onSlot,
}: {
  title: string;
  subtitle: string;
  dotCls: string;
  cardCls: string;
  selectCls: string;
  models: LlmModel[];
  primary: string | null;
  backup: string | null;
  onSlot: (which: "primary" | "backup", value: string) => void;
}) {
  const options = (
    <>
      <option value="">（未设置）</option>
      {models.map((m) => (
        <option key={m.id} value={m.id}>
          {m.name} · {m.modelId}
        </option>
      ))}
    </>
  );
  return (
    <div className={`border rounded-gmLg p-5 shadow-gm-sm ${cardCls}`}>
      <div className="flex items-center gap-2 mb-4">
        <span className={`w-2.5 h-2.5 rounded-full ${dotCls}`} />
        <span className="text-sm font-semibold text-g-fg">{title}</span>
        <span className="text-[11px] text-g-fg-4">{subtitle}</span>
      </div>
      <div className="space-y-4">
        <div>
          <label className={LABEL_CLS}>主用模型</label>
          <select value={primary || ""} onChange={(e) => onSlot("primary", e.target.value)} className={selectCls}>
            {options}
          </select>
        </div>
        <div>
          <label className={LABEL_CLS}>备用模型</label>
          <select value={backup || ""} onChange={(e) => onSlot("backup", e.target.value)} className={selectCls}>
            {options}
          </select>
        </div>
      </div>
    </div>
  );
}

export default function TierSlotSection({ models, tierConfig, onChange, onSaved }: Props) {
  const showToast = useAppStore((s) => s.showToast);
  const [saving, setSaving] = useState(false);

  const setSlot = (slot: keyof TierConfig, value: string) =>
    onChange({ ...tierConfig, [slot]: value || null });

  const handleSave = async () => {
    setSaving(true);
    try {
      await saveTierConfig(tierConfig);
      showToast("层级配置已保存", "success");
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
          <span className="w-1 h-4 rounded-full bg-g-blue shrink-0" />
          层级模型配置
        </h3>
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-3.5 py-1.5 text-sm font-medium bg-g-blue text-white rounded-gm shadow-gm-sm hover:shadow-gm hover:brightness-105 active:scale-[0.97] transition-all disabled:opacity-50"
        >
          {saving ? "保存中..." : "保存配置"}
        </button>
      </div>
      <p className="text-[13px] text-g-fg-3 mb-4 leading-relaxed">
        管理层与执行层各自指定主用与备用。主用故障（429 / 5xx）时切备用。截图会注入主对话，多模态模型自己读图；「帮你看图片」是可选辅助（空则用管理层主用）。生图用下方独立面板。
      </p>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
        <TierCard
          title="管理层"
          subtitle="CEO · Coordinator"
          dotCls="bg-g-blue"
          cardCls="border-g-blue/25 bg-g-blue-bg/15"
          selectCls={SLOT_SELECT_CLS.management}
          models={models}
          primary={tierConfig.managementPrimary}
          backup={tierConfig.managementBackup}
          onSlot={(w, v) => setSlot(w === "primary" ? "managementPrimary" : "managementBackup", v)}
        />
        <TierCard
          title="执行层"
          subtitle="Executor · QA · HR"
          dotCls="bg-g-green"
          cardCls="border-g-green/25 bg-g-green-bg/20"
          selectCls={SLOT_SELECT_CLS.executor}
          models={models}
          primary={tierConfig.executorPrimary}
          backup={tierConfig.executorBackup}
          onSlot={(w, v) => setSlot(w === "primary" ? "executorPrimary" : "executorBackup", v)}
        />
        <TierCard
          title="多模态模型配置"
          subtitle="可选 · 帮你看图片"
          dotCls="bg-amber-500"
          cardCls="border-amber-500/25 bg-g-yellow-bg/40"
          selectCls={SLOT_SELECT_CLS.vision}
          models={models}
          primary={tierConfig.visionPrimary}
          backup={tierConfig.visionBackup}
          onSlot={(w, v) => setSlot(w === "primary" ? "visionPrimary" : "visionBackup", v)}
        />
      </div>
    </section>
  );
}
