/** Shared types for the model-config component family. */

import type { LlmModel, ProviderPreset } from "../../api";

/** 思考三态：''=跟随默认 / 'on' / 'off'（用户意图，与能力位分离） */
export type ThinkingMode = "" | "on" | "off";

export interface ModelFormState {
  name: string;
  modelId: string;
  baseUrl: string;
  apiKey: string;
  providerType: string;
  /** 完整 URL 模式：粘贴的端点不再自动剥路径 */
  fullUrl: boolean;
  contextWindow: number;
  maxOutputTokens: number;
  modelFamily: string;
  toolCallRounds: string; // 空 = 默认
  supportsVision: boolean;
  thinkingMode: ThinkingMode;
  thinkingFormat: string;
  defaultReasoningEffort: string;
  temperature: string; // 空 = 默认
  topP: string;
  topK: string;
}

export const EMPTY_FORM: ModelFormState = {
  name: "",
  modelId: "",
  baseUrl: "",
  apiKey: "",
  providerType: "openai-compatible",
  fullUrl: false,
  contextWindow: 128000,
  maxOutputTokens: 8192,
  modelFamily: "",
  toolCallRounds: "",
  supportsVision: false,
  thinkingMode: "",
  thinkingFormat: "",
  defaultReasoningEffort: "high",
  temperature: "",
  topP: "",
  topK: "",
};

/** 表单三种打开方式 */
export type FormMode =
  | { kind: "preset"; preset: ProviderPreset }
  | { kind: "custom" }
  | { kind: "edit"; model: LlmModel };

export function formFromModel(m: LlmModel): ModelFormState {
  return {
    ...EMPTY_FORM,
    name: m.name,
    modelId: m.modelId,
    baseUrl: m.baseUrl,
    apiKey: "", // 列表返回脱敏 Key；留空 = 保持原 Key
    providerType: m.providerType || "openai-compatible",
    contextWindow: m.contextWindow,
    maxOutputTokens: m.maxOutputTokens,
    modelFamily: m.modelFamily || "",
    toolCallRounds: m.toolCallRounds != null ? String(m.toolCallRounds) : "",
    supportsVision: Boolean(m.supportsVision),
    thinkingMode: (m.thinkingMode as ThinkingMode) || "",
    thinkingFormat: m.thinkingFormat || "",
    defaultReasoningEffort: m.defaultReasoningEffort || "high",
    temperature: m.temperature != null && m.temperature !== "" ? String(m.temperature) : "",
    topP: m.topP != null ? String(m.topP) : "",
    topK: m.topK != null ? String(m.topK) : "",
  };
}
