import { useEffect, useState } from "react";
import { getProviderPresets } from "../../api";
import type { ProviderPreset } from "../../api";
import type { FormMode } from "./types";

interface Props {
  onPick: (mode: FormMode) => void;
  onClose: () => void;
}

/** 每个服务商一个辨识色（宫格圆点/字母底色），无 logo 资产时的干净兜底 */
const PRESET_COLORS: Record<string, string> = {
  deepseek: "#4d6bfe",
  "moonshotai-cn": "#1a1a1a",
  moonshotai: "#6b7280",
  "zai-coding-cn": "#3b5bfd",
  zai: "#7c5bfd",
  xiaomi: "#ff6900",
  "minimax-cn": "#e64141",
  minimax: "#b33030",
  "kimi-coding": "#111827",
  openrouter: "#64748b",
};

function PresetTile({
  label,
  color,
  onClick,
}: {
  label: string;
  color: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-2.5 px-3.5 py-3 border border-g-border rounded-gmLg bg-g-bg hover:border-g-blue/40 hover:bg-g-blue-bg/30 hover:shadow-gm-sm active:scale-[0.98] transition-all text-left"
    >
      <span
        className="w-7 h-7 shrink-0 rounded-full flex items-center justify-center text-white text-[11px] font-bold"
        style={{ backgroundColor: color }}
      >
        {label.slice(0, 1)}
      </span>
      <span className="text-sm text-g-fg font-medium truncate">{label}</span>
    </button>
  );
}

export default function ProviderPickerDialog({ onPick, onClose }: Props) {
  const [presets, setPresets] = useState<ProviderPreset[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    getProviderPresets()
      .then(setPresets)
      .catch(() => setFailed(true))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div
      className="fixed inset-0 bg-black/50 backdrop-blur-[2px] flex items-center justify-center z-[60]"
      onClick={onClose}
    >
      <div
        className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-pop w-[560px] max-h-[80vh] flex flex-col animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-g-border shrink-0">
          <h3 className="text-base font-semibold text-g-fg">添加模型</h3>
          <button
            onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-gm text-g-fg-3 hover:text-g-fg hover:bg-g-bg-muted transition-colors"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <div className="p-5 overflow-y-auto">
          <div className="grid grid-cols-2 gap-2.5">
            <PresetTile label="自定义模型" color="#8b5cf6" onClick={() => onPick({ kind: "custom" })} />
            {presets.map((p) => (
              <PresetTile
                key={p.id}
                label={p.name}
                color={PRESET_COLORS[p.id] ?? "#64748b"}
                onClick={() => onPick({ kind: "preset", preset: p })}
              />
            ))}
          </div>
          {loading && (
            <p className="mt-3 text-xs text-g-fg-4 flex items-center gap-1.5">
              <span className="w-3 h-3 border-2 border-g-border-strong border-t-g-blue rounded-full animate-spin" />
              正在加载服务商预设...
            </p>
          )}
          {failed && (
            <p className="mt-3 text-xs text-g-fg-4">
              服务商预设加载失败，仍可使用「自定义模型」手动配置。
            </p>
          )}
          <p className="mt-4 text-[11px] text-g-fg-4 leading-relaxed">
            知名服务商只需填写 API Key，地址与模型能力已预置；其他 OpenAI / Anthropic 兼容网关走「自定义模型」。
          </p>
        </div>
      </div>
    </div>
  );
}
