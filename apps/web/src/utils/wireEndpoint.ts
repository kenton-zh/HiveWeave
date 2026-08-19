/** Mirror of hiveweave.llm.wire_endpoint: Base URL is the /v1 prefix. */

export const PROTOCOL_CHAT = "openai-compatible";
export const PROTOCOL_RESPONSES = "openai-responses";
export const PROTOCOL_ANTHROPIC = "anthropic";
export const PROTOCOL_GOOGLE = "google";

export const PROTOCOL_OPTIONS = [
  { value: PROTOCOL_CHAT, label: "Chat Completions" },
  { value: PROTOCOL_RESPONSES, label: "Responses" },
  { value: PROTOCOL_ANTHROPIC, label: "Anthropic Messages" },
  { value: PROTOCOL_GOOGLE, label: "Gemini" },
] as const;

const PATH_SUFFIXES: Array<[string, string]> = [
  ["/chat/completions", PROTOCOL_CHAT],
  ["/responses", PROTOCOL_RESPONSES],
  ["/messages", PROTOCOL_ANTHROPIC],
];

export function splitWireEndpoint(baseUrl: string): {
  prefix: string;
  inferred: string | null;
} {
  const trimmed = (baseUrl || "").trim();
  if (!trimmed) return { prefix: "", inferred: null };
  const noQuery = trimmed.split("#")[0].split("?")[0].replace(/\/+$/, "");
  const lower = noQuery.toLowerCase();
  for (const [suffix, proto] of PATH_SUFFIXES) {
    if (lower.endsWith(suffix)) {
      return {
        prefix: noQuery.slice(0, -suffix.length).replace(/\/+$/, ""),
        inferred: proto,
      };
    }
  }
  return { prefix: noQuery, inferred: null };
}

export function applyWireEndpoint(
  baseUrl: string,
  providerType?: string | null,
): { prefix: string; protocol: string } {
  const { prefix, inferred } = splitWireEndpoint(baseUrl);
  const explicit = (providerType || "").trim();
  const protocol = inferred || explicit || PROTOCOL_CHAT;
  return { prefix, protocol };
}

export function protocolLabel(value?: string | null): string {
  const v = value === "openai" ? PROTOCOL_CHAT : value;
  const hit = PROTOCOL_OPTIONS.find((o) => o.value === v);
  return hit?.label ?? "Chat Completions";
}

export function normalizeProtocol(value?: string | null): string {
  if (value === "openai") return PROTOCOL_CHAT;
  if (PROTOCOL_OPTIONS.some((o) => o.value === value)) return value as string;
  return PROTOCOL_CHAT;
}
