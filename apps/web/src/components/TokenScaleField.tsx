import { useEffect, useState } from "react";

export type TokenUnit = "K" | "M";

const SCALE: Record<TokenUnit, number> = { K: 1_000, M: 1_000_000 };

function formatUnitValue(tokens: number, unit: TokenUnit): string {
  const n = tokens / SCALE[unit];
  if (!Number.isFinite(n) || n <= 0) return tokens > 0 ? String(n) : "";
  const decimals = unit === "M" ? 6 : 3;
  const rounded = Number(n.toFixed(decimals));
  if (rounded === 0 && tokens > 0) return n.toFixed(8).replace(/0+$/, "").replace(/\.$/, "");
  return String(rounded);
}

function parseUnitValue(raw: string, unit: TokenUnit): number | null {
  const trimmed = raw.trim().toLowerCase();
  if (!trimmed) return null;
  // 支持带单位后缀输入（如 "1M" / "1000k" / "2.5m"），无后缀则按当前单位
  const m = /^(\d*\.?\d+)\s*([km])?$/.exec(trimmed);
  if (!m) return null;
  const n = Number(m[1]);
  if (!Number.isFinite(n) || n <= 0) return null;
  const suffixScale = m[2] === "k" ? 1_000 : m[2] === "m" ? 1_000_000 : SCALE[unit];
  return Math.max(1, Math.round(n * suffixScale));
}

interface Props {
  label: string;
  value: number;
  onChange: (tokens: number) => void;
  minTokens: number;
  maxTokens: number;
}

export default function TokenScaleField({
  label,
  value,
  onChange,
  minTokens,
  maxTokens,
}: Props) {
  const [unit, setUnit] = useState<TokenUnit>("K");
  const [draft, setDraft] = useState<string | null>(null);

  useEffect(() => {
    setDraft(null);
  }, [value, unit]);

  const scale = SCALE[unit];
  const sliderMin = minTokens / scale;
  const sliderMax = Math.max(maxTokens, value) / scale;
  const sliderValue = Math.min(Math.max(value / scale, sliderMin), sliderMax);
  const shown = draft ?? formatUnitValue(value, unit);

  const commit = (raw: string, withUnit: TokenUnit = unit) => {
    const next = parseUnitValue(raw, withUnit);
    setDraft(null);
    if (next != null) onChange(next);
  };

  return (
    <div>
      <label className="block text-xs font-medium text-g-fg-3 mb-1.5">{label}</label>
      <input
        type="range"
        min={sliderMin}
        max={sliderMax}
        step="any"
        value={Number.isFinite(sliderValue) ? sliderValue : sliderMin}
        onChange={(e) => onChange(Math.max(1, Math.round(Number(e.target.value) * scale)))}
        className="w-full h-1.5 mb-2 cursor-pointer accent-g-blue"
        style={{ accentColor: "#4f46e5" }}
      />
      <div className="flex items-center gap-2">
        <input
          type="text"
          inputMode="decimal"
          value={shown}
          onChange={(e) => {
            const raw = e.target.value;
            setDraft(raw);
            // 输入有效值即同步父组件，避免「直接点保存」时 blur 未触发导致旧值提交
            const next = parseUnitValue(raw, unit);
            if (next != null) onChange(next);
          }}
          onBlur={() => {
            if (draft == null) return;
            commit(draft);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
          className="flex-1 min-w-0 px-3 py-2 text-sm font-mono bg-g-bg border border-g-border rounded-gm text-g-fg focus:outline-none focus:border-g-blue/50 focus:ring-2 focus:ring-g-blue/15"
        />
        <div className="inline-flex shrink-0 rounded-gm border border-g-border overflow-hidden">
          {(["K", "M"] as const).map((u) => (
            <button
              key={u}
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                if (u === unit) return;
                if (draft != null) commit(draft, u);
                setUnit(u);
              }}
              className={`px-2.5 py-2 text-xs font-semibold transition-colors ${
                unit === u
                  ? "bg-g-blue text-white"
                  : "bg-g-bg text-g-fg-3 hover:bg-g-bg-muted"
              }`}
            >
              {u}
            </button>
          ))}
        </div>
      </div>
      <p className="mt-1 text-[11px] text-g-fg-4 tabular-nums">
        {value.toLocaleString()} tokens
      </p>
    </div>
  );
}
