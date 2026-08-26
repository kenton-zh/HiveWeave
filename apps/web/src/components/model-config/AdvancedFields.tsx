import { useEffect, useState } from "react";
import type { ModelFormState, ThinkingMode } from "./types";
import { INPUT_CLS, LABEL_CLS } from "./styles";
import {
  THINKING_OPTIONS,
  coerceEffort,
  effortsForThinking,
} from "../../utils/thinkingFormat";

interface Props {
  form: ModelFormState;
  setField: <K extends keyof ModelFormState>(key: K, value: ModelFormState[K]) => void;
}

const CTX_PRESETS = [128_000, 256_000, 512_000, 1_000_000];
const OUT_PRESETS = [4_000, 16_000, 32_000, 128_000];

function kLabel(tokens: number): string {
  return tokens >= 1_000_000 ? `${tokens / 1_000_000}M` : `${Math.round(tokens / 1000)}k`;
}

/** 数字输入 + 快捷档位 chips（截图风格的 128k/256k/512k/1M） */
function TokenField({
  label,
  value,
  presets,
  onChange,
}: {
  label: string;
  value: number;
  presets: number[];
  onChange: (tokens: number) => void;
}) {
  // 本地 draft：允许清空重输；失焦时无效值回退为 props 值
  const [draft, setDraft] = useState<string | null>(null);
  useEffect(() => setDraft(null), [value]);

  return (
    <div>
      <label className={LABEL_CLS}>{label}</label>
      <div className="flex items-center gap-2">
        <input
          type="number"
          min={1}
          value={draft ?? (value || "")}
          onChange={(e) => {
            const raw = e.target.value;
            setDraft(raw);
            const n = Number(raw);
            if (raw.trim() && Number.isFinite(n) && n > 0) onChange(Math.round(n));
          }}
          onBlur={() => setDraft(null)}
          className={`${INPUT_CLS} font-mono tabular-nums`}
        />
      </div>
      <div className="flex gap-1.5 mt-1.5">
        {presets.map((p) => (
          <button
            key={p}
            type="button"
            onClick={() => onChange(p)}
            className={`px-2 py-0.5 text-[11px] rounded-gm border transition-colors tabular-nums ${
              value === p
                ? "border-g-blue/50 bg-g-blue-bg/50 text-g-blue font-medium"
                : "border-g-border text-g-fg-3 hover:border-g-blue/30 hover:text-g-blue"
            }`}
          >
            {kLabel(p)}
          </button>
        ))}
      </div>
    </div>
  );
}

/** 留空 = 使用默认 的数字采样参数 */
function OptionalNumber({
  label,
  value,
  placeholder,
  step,
  onChange,
}: {
  label: string;
  value: string;
  placeholder: string;
  step?: string;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <label className={LABEL_CLS}>{label}</label>
      <input
        type="number"
        step={step ?? "1"}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={`${INPUT_CLS} font-mono tabular-nums`}
      />
    </div>
  );
}

const THINKING_MODES: Array<{ value: ThinkingMode; label: string }> = [
  { value: "", label: "跟随模型默认" },
  { value: "on", label: "开启" },
  { value: "off", label: "关闭" },
];

export default function AdvancedFields({ form, setField }: Props) {
  const effortOptions = effortsForThinking(form.thinkingFormat, form.providerType);
  const effortValue = coerceEffort(form.defaultReasoningEffort, effortOptions);

  return (
    <div className="space-y-4">
      <div>
        <label className={LABEL_CLS}>模型系列</label>
        <input
          value={form.modelFamily}
          onChange={(e) => setField("modelFamily", e.target.value)}
          placeholder="如 DeepSeek / GLM / Kimi（展示分组用，可留空）"
          className={INPUT_CLS}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <TokenField
          label="上下文窗口（输入）"
          value={form.contextWindow}
          presets={CTX_PRESETS}
          onChange={(n) => setField("contextWindow", n)}
        />
        <TokenField
          label="最大输出"
          value={form.maxOutputTokens}
          presets={OUT_PRESETS}
          onChange={(n) => setField("maxOutputTokens", n)}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <OptionalNumber
          label="工具调用轮数"
          value={form.toolCallRounds}
          placeholder="留空使用默认"
          onChange={(v) => setField("toolCallRounds", v)}
        />
        <div>
          <label className={LABEL_CLS}>支持图片输入</label>
          <button
            type="button"
            role="switch"
            aria-checked={form.supportsVision}
            onClick={() => setField("supportsVision", !form.supportsVision)}
            className={`relative w-10 h-[22px] rounded-full transition-colors ${
              form.supportsVision ? "bg-g-blue" : "bg-g-border-strong"
            }`}
          >
            <span
              className={`absolute top-[3px] w-4 h-4 rounded-full bg-white shadow transition-transform ${
                form.supportsVision ? "translate-x-[22px]" : "translate-x-[3px]"
              }`}
            />
          </button>
        </div>
      </div>

      <div>
        <label className={LABEL_CLS}>思考模式</label>
        <div className="flex gap-1.5">
          {THINKING_MODES.map((m) => (
            <button
              key={m.value || "auto"}
              type="button"
              onClick={() => setField("thinkingMode", m.value)}
              className={`px-3 py-1.5 text-xs rounded-gm border transition-colors ${
                form.thinkingMode === m.value
                  ? "border-g-blue/50 bg-g-blue-bg/50 text-g-blue font-medium"
                  : "border-g-border text-g-fg-3 hover:border-g-blue/30 hover:text-g-blue"
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>
        <p className="mt-1 text-[11px] text-g-fg-4 leading-snug">
          「跟随默认」按协议与能力自动；「关闭」强制不发思考字段。
        </p>
      </div>

      {form.thinkingMode === "on" && (
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className={LABEL_CLS}>思考方式</label>
            <select
              value={form.thinkingFormat}
              onChange={(e) => setField("thinkingFormat", e.target.value)}
              className={INPUT_CLS}
            >
              {THINKING_OPTIONS.map((opt) => (
                <option key={opt.value || "auto"} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
          <div>
            <label className={LABEL_CLS}>思考强度</label>
            <select
              value={effortValue}
              onChange={(e) => setField("defaultReasoningEffort", e.target.value)}
              className={INPUT_CLS}
            >
              {effortOptions.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
        </div>
      )}

      <div>
        <label className={LABEL_CLS}>采样参数（留空使用默认）</label>
        <div className="grid grid-cols-3 gap-3">
          <OptionalNumber
            label="Temperature"
            value={form.temperature}
            placeholder="默认"
            step="0.1"
            onChange={(v) => setField("temperature", v)}
          />
          <OptionalNumber
            label="Top P"
            value={form.topP}
            placeholder="默认"
            step="0.05"
            onChange={(v) => setField("topP", v)}
          />
          <OptionalNumber
            label="Top K"
            value={form.topK}
            placeholder="默认"
            onChange={(v) => setField("topK", v)}
          />
        </div>
      </div>
    </div>
  );
}
