/** Thinking dialect: which JSON fields carry reasoning. Protocol is the HTTP path. */

import {
  PROTOCOL_ANTHROPIC,
  PROTOCOL_GOOGLE,
  PROTOCOL_RESPONSES,
} from "./wireEndpoint";

export const THINKING_AUTO = "";
export const THINKING_OFF = "off";
export const THINKING_OPENAI_EFFORT = "openai-effort";
export const THINKING_RESPONSES = "openai-responses";
export const THINKING_DEEPSEEK = "deepseek";
export const THINKING_ANTHROPIC = "anthropic";
export const THINKING_GEMINI = "gemini";
export const THINKING_QWEN = "qwen";

export const THINKING_OPTIONS = [
  { value: THINKING_AUTO, label: "自动（跟协议）" },
  { value: THINKING_OPENAI_EFFORT, label: "reasoning_effort（Chat Completions）" },
  { value: THINKING_RESPONSES, label: "reasoning.effort（Responses）" },
  { value: THINKING_DEEPSEEK, label: "thinking.type（DeepSeek）" },
  { value: THINKING_ANTHROPIC, label: "thinking.budget（Anthropic）" },
  { value: THINKING_GEMINI, label: "thinkingConfig（Gemini）" },
  { value: THINKING_QWEN, label: "enable_thinking（Qwen）" },
] as const;

export const THINKING_OPTIONS_EN = [
  { value: THINKING_AUTO, label: "Auto (from protocol)" },
  { value: THINKING_OPENAI_EFFORT, label: "reasoning_effort (Chat Completions)" },
  { value: THINKING_RESPONSES, label: "reasoning.effort (Responses)" },
  { value: THINKING_DEEPSEEK, label: "thinking.type (DeepSeek)" },
  { value: THINKING_ANTHROPIC, label: "thinking.budget (Anthropic)" },
  { value: THINKING_GEMINI, label: "thinkingConfig (Gemini)" },
  { value: THINKING_QWEN, label: "enable_thinking (Qwen)" },
] as const;

const EFFORT_LABELS: Record<string, { label: string; labelEn: string }> = {
  none: { label: "无", labelEn: "None" },
  minimal: { label: "最低", labelEn: "Minimal" },
  low: { label: "低", labelEn: "Low" },
  medium: { label: "中", labelEn: "Medium" },
  high: { label: "高", labelEn: "High" },
  xhigh: { label: "最高", labelEn: "Extra high" },
  max: { label: "最大", labelEn: "Max" },
};

const EFFORTS_OPENAI = ["none", "minimal", "low", "medium", "high", "xhigh"];
const EFFORTS_DEEPSEEK = ["high", "max"];
const EFFORTS_BUDGET = ["low", "medium", "high", "max"];
const EFFORTS_CHAT = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];

function packEfforts(values: string[]) {
  return values.map((value) => ({ value, ...EFFORT_LABELS[value] }));
}

const VALID = new Set<string>([
  THINKING_OFF,
  THINKING_OPENAI_EFFORT,
  THINKING_RESPONSES,
  THINKING_DEEPSEEK,
  THINKING_ANTHROPIC,
  THINKING_GEMINI,
  THINKING_QWEN,
]);

export function normalizeThinkingFormat(value?: string | null): string {
  const v = (value || "").trim().toLowerCase();
  if (VALID.has(v)) return v;
  return THINKING_AUTO;
}

/** Same auto rule as the backend: empty format follows protocol. */
export function resolveThinkingFormatForUi(
  thinkingFormat: string | null | undefined,
  protocol: string | null | undefined,
): string {
  const explicit = normalizeThinkingFormat(thinkingFormat);
  if (explicit && explicit !== THINKING_OFF) return explicit;
  const proto = protocol === "openai" ? "openai-compatible" : (protocol || "");
  if (proto === PROTOCOL_RESPONSES) return THINKING_RESPONSES;
  if (proto === PROTOCOL_ANTHROPIC) return THINKING_ANTHROPIC;
  if (proto === PROTOCOL_GOOGLE) return THINKING_GEMINI;
  return THINKING_OPENAI_EFFORT;
}

export function effortsForThinking(
  thinkingFormat?: string | null,
  protocol?: string | null,
) {
  const fmt = resolveThinkingFormatForUi(thinkingFormat, protocol);
  if (fmt === THINKING_RESPONSES) return packEfforts(EFFORTS_OPENAI);
  if (fmt === THINKING_DEEPSEEK) return packEfforts(EFFORTS_DEEPSEEK);
  if (fmt === THINKING_ANTHROPIC || fmt === THINKING_GEMINI || fmt === THINKING_QWEN) {
    return packEfforts(EFFORTS_BUDGET);
  }
  return packEfforts(EFFORTS_CHAT);
}

/** Old 最大 was stored as max; Responses' top slot is xhigh. */
export function coerceEffort(
  effort: string | null | undefined,
  options: { value: string }[],
): string {
  const offered = new Set(options.map((o) => o.value));
  let e = (effort || "").trim().toLowerCase();
  if (!e) e = "high";
  if (!offered.has(e) && e === "max" && offered.has("xhigh")) e = "xhigh";
  if (offered.has(e)) return e;
  if (offered.has("high")) return "high";
  return options[0]?.value || "high";
}

export function thinkingFormatLabel(value?: string | null): string {
  const v = normalizeThinkingFormat(value);
  const hit = THINKING_OPTIONS.find((o) => o.value === v);
  return hit?.label ?? "自动（跟协议）";
}
