import { useState } from "react";
import { testModel } from "../../api";
import type { LlmModel } from "../../api";
import { protocolLabel } from "../../utils/wireEndpoint";

interface Props {
  model: LlmModel;
  onEdit: (model: LlmModel) => void;
  onDelete: (id: string) => void;
}

function fmtTokens(n?: number | null): string {
  if (!n) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

function Badge({ text, tone }: { text: string; tone: "gray" | "purple" | "blue" | "amber" | "green" }) {
  const tones: Record<string, string> = {
    gray: "bg-g-bg-muted text-g-fg-3",
    purple: "bg-purple-50 text-purple-600",
    blue: "bg-g-blue-bg/60 text-g-blue",
    amber: "bg-amber-50 text-amber-600",
    green: "bg-g-green-bg/60 text-g-green",
  };
  return (
    <span className={`shrink-0 whitespace-nowrap text-[10px] leading-none px-1.5 py-1 rounded-gm font-medium ${tones[tone]}`}>
      {text}
    </span>
  );
}

function thinkingLabel(m: LlmModel): { text: string; tone: "purple" | "gray" } | null {
  if (m.thinkingMode === "on") return { text: "思考·开", tone: "purple" };
  if (m.thinkingMode === "off") return { text: "思考·关", tone: "gray" };
  if (m.supportsThinking) return { text: "思考", tone: "purple" };
  return null;
}

export default function ModelCard({ model, onEdit, onDelete }: Props) {
  const [testing, setTesting] = useState(false);
  const [testState, setTestState] = useState<{ ok: boolean; text: string } | null>(null);

  const handleTest = async () => {
    setTesting(true);
    setTestState(null);
    try {
      const r = await testModel(model.id);
      setTestState(
        r.ok
          ? { ok: true, text: `${r.latencyMs}ms` }
          : { ok: false, text: (r.error || "失败").slice(0, 60) },
      );
    } catch (err: any) {
      setTestState({ ok: false, text: (err.message || "失败").slice(0, 60) });
    } finally {
      setTesting(false);
    }
  };

  const thinking = thinkingLabel(model);
  const tierLabel =
    model.tier === "management" ? "管理层" : model.tier === "executor" ? "执行层" : null;

  return (
    <div className="border border-g-border rounded-gmLg bg-g-bg p-4 shadow-gm-sm hover:shadow-gm transition-shadow flex flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="font-medium text-sm text-g-fg truncate" title={model.name}>
              {model.name}
            </span>
            {!model.isActive && <Badge text="已停用" tone="gray" />}
          </div>
          <div className="mt-0.5 font-mono text-xs text-g-fg-3 truncate" title={model.modelId}>
            {model.modelId}
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={handleTest}
            disabled={testing}
            className="px-2 py-1 text-xs rounded-gm border border-g-border text-g-fg-3 hover:text-g-blue hover:border-g-blue/40 hover:bg-g-blue-bg/40 active:scale-[0.96] transition-all disabled:opacity-50"
            title="连通性测试"
          >
            {testing ? "…" : "测试"}
          </button>
          <button
            onClick={() => onEdit(model)}
            className="px-2 py-1 text-xs rounded-gm border border-g-border text-g-fg-3 hover:text-g-blue hover:border-g-blue/40 hover:bg-g-blue-bg/40 active:scale-[0.96] transition-all"
          >
            编辑
          </button>
          <button
            onClick={() => onDelete(model.id)}
            className="px-2 py-1 text-xs rounded-gm border border-g-border text-g-fg-3 hover:text-g-red hover:border-g-red/30 hover:bg-g-red-bg/50 active:scale-[0.96] transition-all"
          >
            删除
          </button>
        </div>
      </div>

      <div className="flex items-center gap-1.5 flex-wrap">
        <Badge text={protocolLabel(model.providerType)} tone="gray" />
        {thinking && <Badge text={thinking.text} tone={thinking.tone} />}
        {model.supportsVision && <Badge text="视觉" tone="blue" />}
        {tierLabel && <Badge text={tierLabel} tone="amber" />}
        {model.modelFamily && <Badge text={model.modelFamily} tone="green" />}
      </div>

      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-g-fg-3 tabular-nums">
        <div>上下文 <span className="text-g-fg font-medium">{fmtTokens(model.contextWindow)}</span></div>
        <div>最大输出 <span className="text-g-fg font-medium">{fmtTokens(model.maxOutputTokens)}</span></div>
        <div className="col-span-2 truncate" title={model.baseUrl}>
          地址 <span className="font-mono">{model.baseUrl || "—"}</span>
        </div>
      </div>

      {testState && (
        <div
          className={`text-[11px] px-2.5 py-1.5 rounded-gm truncate ${
            testState.ok ? "bg-g-green-bg/40 text-g-green" : "bg-g-red-bg/50 text-g-red"
          }`}
          title={testState.text}
        >
          {testState.ok ? `✓ ${testState.text}` : `✗ ${testState.text}`}
        </div>
      )}
    </div>
  );
}
