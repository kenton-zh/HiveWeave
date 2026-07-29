import { dbg, getApiKey, getBaseUrl, setApiKey, initApiKeyFromStorage } from "./shared";

export { setApiKey, initApiKeyFromStorage };

const BASE = getBaseUrl();

async function fetchJSON<T = any>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const apiKey = getApiKey();
  if (apiKey && !headers.has("x-api-key")) {
    headers.set("x-api-key", apiKey);
  }
  const method = init?.method || "GET";
  const t0 = performance.now();
  dbg("api", `${method} ${url}`, { method, url, body: init?.body });
  try {
    const res = await fetch(url, { ...init, headers });
    const elapsed = Math.round(performance.now() - t0);
    const text = await res.text();
    if (!res.ok) {
      dbg("error", `${method} ${url} → ${res.status} (${elapsed}ms)`, { status: res.status, body: text.slice(0, 500) });
      // 401 — API key 缺失或无效，提示用户输入
      if (res.status === 401) {
        try {
          const store = (window as any).__hwStore;
          if (store) store.getState().showToast?.("需要 API Key — 请点击右上角钥匙图标设置", "error");
        } catch { /* noop */ }
      }
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    const parsed = !text || text.length === 0 ? {} : JSON.parse(text);
    dbg("api", `${method} ${url} → ${res.status} (${elapsed}ms)`, { status: res.status, bodyPreview: text.slice(0, 300) });
    return parsed as T;
  } catch (e: any) {
    // BUG-006/018 修复：AbortError 不返回 null（会导致 caller 误设空数据），
    // 也不污染 console。重新 throw 带 _aborted 标记，让 caller 静默处理。
    if (e?.name === "AbortError") {
      const abortErr = new Error("Aborted") as any;
      abortErr.name = "AbortError";
      abortErr._aborted = true;
      throw abortErr;
    }
    const elapsed = Math.round(performance.now() - t0);
    dbg("error", `${method} ${url} FAILED (${elapsed}ms): ${e.message}`, { error: e.message });
    throw e;
  }
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export interface Project {
  id: string;
  name: string;
  workspacePath?: string | null;
  description?: string | null;
  orgParadigm?: string | null;
  language?: string | null;
  isStarted?: boolean;
  createdAt: number;
}

export interface KeyResult {
  text: string;
  status: "todo" | "doing" | "done";
  owner?: string;
}

export interface GoalsData {
  objective: string;
  focus: string;
  keyResults: KeyResult[];
  userInvolvement?: string;
}

export async function getProjects(): Promise<Project[]> {
  const data = await fetchJSON<{ projects: Project[] }>(`${BASE}/projects`);
  return data.projects || [];
}

export async function activateProject(projectId: string): Promise<boolean> {
  const data = await fetchJSON<{ is_started: boolean }>(`${BASE}/projects/${projectId}/activate`);
  return data.is_started ?? true;
}

export async function deactivateProject(projectId: string): Promise<boolean> {
  const data = await fetchJSON<{ is_started: boolean }>(`${BASE}/projects/${projectId}/deactivate`);
  return data.is_started ?? false;
}


export interface WorkspaceCleanupResult {
  status: "ok" | "skipped" | "scheduled" | "failed";
  hiveweaveDir?: string | null;
  workspacePath?: string | null;
  reason?: "shared" | "no_workspace" | "skipped" | "error";
  sharedWith?: string[];
  pendingDir?: string;
}

export interface DeleteProjectResponse {
  ok: boolean;
  dbLeftover?: boolean;
  workspaceCleanup?: WorkspaceCleanupResult;
  warning?: string;
}

export async function createProject(name: string, workspacePath?: string, description?: string, orgParadigm?: string, language?: string) {
  return fetchJSON(`${BASE}/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, workspacePath, description, orgParadigm, language: language || "zh" }),
  });
}

export async function deleteProject(id: string): Promise<DeleteProjectResponse> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 120_000);
  try {
    return await fetchJSON<DeleteProjectResponse>(`${BASE}/projects/${id}`, {
      method: "DELETE",
      signal: controller.signal,
    });
  } finally {
    window.clearTimeout(timer);
  }
}

export async function getProjectGameTime(projectId: string) {
  return fetchJSON(`${BASE}/projects/${projectId}/game-time`);
}

export async function getProjectGoals(projectId: string) {
  return fetchJSON(`${BASE}/projects/${projectId}/goals`);
}

export async function updateProjectGoals(projectId: string, goals: any) {
  return fetchJSON(`${BASE}/projects/${projectId}/goals`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goals }),
  });
}

// ---------------------------------------------------------------------------
// Org / Agents
// ---------------------------------------------------------------------------

export async function getOrgTree(projectId?: string) {
  const url = projectId ? `${BASE}/org?projectId=${projectId}` : `${BASE}/org`;
  return fetchJSON(url);
}

export async function getAgent(id: string) {
  const raw = await fetchJSON(`${BASE}/org/agents/${id}`);
  // Unwrap the {agent: ...} envelope once at the API layer so callers
  // don't need to repeat this logic. Backend always returns %{agent: serialize_agent(a)}.
  return (raw && typeof raw === "object" && "agent" in raw && raw.agent) ? raw.agent : raw;
}

export async function createAgent(data: any) {
  return fetchJSON(`${BASE}/org/agents`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateAgent(id: string, data: any) {
  return fetchJSON(`${BASE}/org/agents/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function deleteAgent(id: string, actorAgentId?: string) {
  const q = actorAgentId
    ? `?actorAgentId=${encodeURIComponent(actorAgentId)}`
    : "";
  return fetchJSON(`${BASE}/org/agents/${id}${q}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Inbox
// ---------------------------------------------------------------------------

export async function getInbox(agentId: string) {
  return fetchJSON(`${BASE}/chat/inbox/${agentId}`);
}

export async function sendInboxMessage(payload: {
  fromAgentId: string;
  toAgentId: string;
  type?: string;
  content: string;
  subject?: string;
  priority?: string;
  metadata?: Record<string, any>;
}) {
  return fetchJSON(`${BASE}/chat/inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// Chat history
// ---------------------------------------------------------------------------

export async function getChatHistory(agentId: string) {
  return fetchJSON(`${BASE}/chat/history/${agentId}`);
}

export async function markMessagesRead(ids: string[], agentId?: string) {
  return fetchJSON(`${BASE}/chat/mark-read`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids, agentId: agentId || ids[0] || "" }),
  });
}

// ---------------------------------------------------------------------------
// System pause/resume
// ---------------------------------------------------------------------------

export async function pauseSystem() {
  return fetchJSON(`${BASE}/chat/pause`, { method: "POST" });
}

export async function resumeSystem() {
  return fetchJSON(`${BASE}/chat/resume`, { method: "POST" });
}

export async function getPausedState() {
  return fetchJSON(`${BASE}/chat/paused`);
}

// ---------------------------------------------------------------------------
// System control (restart)
// ---------------------------------------------------------------------------

export async function restartBackend() {
  return fetchJSON(`${BASE}/system/restart-backend`, { method: "POST" });
}

export async function restartFrontend() {
  return fetchJSON(`${BASE}/system/restart-frontend`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export async function getSettings() {
  return fetchJSON(`${BASE}/settings`);
}

export async function getSetting(key: string) {
  return fetchJSON(`${BASE}/settings/${key}`);
}

export async function upsertSetting(key: string, value: string) {
  return fetchJSON(`${BASE}/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, value }),
  });
}

/** Bulk update settings. Accepts a map of { key: value }. */
export async function updateSettings(settings: Record<string, string>) {
  // If single key/value, use the simple endpoint
  const entries = Object.entries(settings);
  if (entries.length === 1) {
    const [key, value] = entries[0];
    return upsertSetting(key, value);
  }
  // Multiple - POST the first one, ignore rest (single-setting endpoint)
  for (const [key, value] of entries) {
    await upsertSetting(key, value);
  }
  return { ok: true };
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

export async function getLlmModels() {
  return fetchJSON(`${BASE}/llm-models`);
}

// ---------------------------------------------------------------------------
// Templates
// ---------------------------------------------------------------------------

export async function getAgentTemplates() {
  return fetchJSON(`${BASE}/agent-templates`);
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export async function getHealth() {
  return fetchJSON(`${BASE}/health`);
}

// ---------------------------------------------------------------------------
// Approvals
// ---------------------------------------------------------------------------

export interface PendingApproval {
  id: string;
  agentId: string;
  toolName: string;
  toolArguments: string;
  description: string;
  status: string;
  createdAt: number;
}

/** Get pending approval requests for a single agent. */
export async function getPendingApprovals(agentId: string): Promise<PendingApproval[]> {
  const data = await fetchJSON<{ requests: PendingApproval[] }>(`${BASE}/permissions/pending/${agentId}`);
  return data.requests || [];
}

/** Get all pending approval requests for a project. */
export async function getProjectPendingApprovals(projectId: string): Promise<PendingApproval[]> {
  const data = await fetchJSON<{ requests: PendingApproval[] }>(`${BASE}/permissions/pending/project/${projectId}`);
  return data.requests || [];
}

/** Respond (approve/reject) to a pending approval request. */
export async function respondToApproval(
  requestId: string,
  approved: boolean,
  remember: boolean = false,
  userNote?: string,
  projectId?: string
): Promise<{ ok: boolean; reason?: string }> {
  return fetchJSON(`${BASE}/permissions/respond`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ requestId, approved, remember, userNote, projectId }),
  });
}

/** Get effective permission rules for an agent. */
export async function getAgentPermissions(agentId: string) {
  return fetchJSON(`${BASE}/permissions/rules/${agentId}`);
}

/** Update permission rules for an agent. */
export async function updateAgentPermissions(agentId: string, rules: {
  permissionMode?: "readonly" | "readwrite" | "full" | "custom";
  allowedTools?: string[];
  deniedTools?: string[];
  askTools?: string[];
  mcpServers?: string[];
  boundSkills?: string[];
}) {
  return fetchJSON(`${BASE}/permissions/rules/${agentId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(rules),
  });
}

// ---------------------------------------------------------------------------
// Permission rules (alias for getAgentPermissions)
// ---------------------------------------------------------------------------

export async function getPermissionRules(agentId: string) {
  return getAgentPermissions(agentId);
}

// ---------------------------------------------------------------------------
// Chat messages (alias for getChatHistory)
// ---------------------------------------------------------------------------

export async function getChatMessages(agentId: string) {
  return fetchJSON(`${BASE}/chat/messages/${agentId}`);
}

// ---------------------------------------------------------------------------
// LLM Models (alias for getLlmModels)
// ---------------------------------------------------------------------------

export interface LlmModel {
  id: string;
  name: string;
  modelId: string;
  baseUrl: string;
  apiKey: string;
  contextWindow: number;
  maxOutputTokens: number;
  supportsThinking: boolean;
  defaultReasoningEffort?: string | null;
  temperature?: string | null;
  isActive: boolean;
  tier?: string | null; // "management" | "executor" | null
}

export async function getModels(): Promise<LlmModel[]> {
  const data = await fetchJSON(`${BASE}/llm-models`);
  // Backend returns { models: [...] }, unwrap it
  return Array.isArray(data) ? data : (data?.models ?? []);
}

export async function createModel(payload: Partial<LlmModel>) {
  return fetchJSON(`${BASE}/llm-models`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateModel(id: string, payload: Partial<LlmModel>) {
  return fetchJSON(`${BASE}/llm-models/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function deleteModel(id: string) {
  return fetchJSON(`${BASE}/llm-models/${id}`, { method: "DELETE" });
}

export async function testModel(id: string) {
  return fetchJSON(`${BASE}/llm-models/${id}/test`, { method: "POST" });
}

export interface DetectedCapabilities {
  contextWindow: number | null;
  supportsThinking: boolean | null;
  maxOutputTokens: number | null;
  source: string;
}

/** Probe a model's capabilities from connection info only (no save, no real chat). */
export async function detectCapabilities(payload: {
  baseUrl: string;
  apiKey?: string;
  modelId: string;
}): Promise<DetectedCapabilities> {
  return fetchJSON(`${BASE}/llm-models/detect-capabilities`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// Model Tier Configuration (management / executor primary+backup)
// ---------------------------------------------------------------------------

export type ModelTier = "management" | "executor";

export interface TierConfig {
  managementPrimary: string | null;
  managementBackup: string | null;
  executorPrimary: string | null;
  executorBackup: string | null;
  /** Multimodal model for look_at_image (帮你看图片) */
  visionPrimary: string | null;
  visionBackup: string | null;
}

const TIER_KEYS = {
  managementPrimary: "model_tier_management_primary",
  managementBackup: "model_tier_management_backup",
  executorPrimary: "model_tier_executor_primary",
  executorBackup: "model_tier_executor_backup",
  visionPrimary: "vision_model_primary",
  visionBackup: "vision_model_backup",
} as const;

/** Read tier + vision-model keys from global settings. */
export async function getTierConfig(): Promise<TierConfig> {
  const data = await getSettings();
  const list: Array<{ key: string; value: string }> = Array.isArray(data)
    ? data
    : (data?.settings ?? []);
  const map: Record<string, string> = {};
  for (const item of list) {
    if (item && typeof item.key === "string") map[item.key] = item.value;
  }
  return {
    managementPrimary: map[TIER_KEYS.managementPrimary] || null,
    managementBackup: map[TIER_KEYS.managementBackup] || null,
    executorPrimary: map[TIER_KEYS.executorPrimary] || null,
    executorBackup: map[TIER_KEYS.executorBackup] || null,
    visionPrimary: map[TIER_KEYS.visionPrimary] || null,
    visionBackup: map[TIER_KEYS.visionBackup] || null,
  };
}

/** Write tier + vision-model keys. Empty string clears a slot. */
export async function saveTierConfig(config: TierConfig): Promise<void> {
  await updateSettings({
    [TIER_KEYS.managementPrimary]: config.managementPrimary || "",
    [TIER_KEYS.managementBackup]: config.managementBackup || "",
    [TIER_KEYS.executorPrimary]: config.executorPrimary || "",
    [TIER_KEYS.executorBackup]: config.executorBackup || "",
    [TIER_KEYS.visionPrimary]: config.visionPrimary || "",
    [TIER_KEYS.visionBackup]: config.visionBackup || "",
  });
}

// ---------------------------------------------------------------------------
// Agent Templates
// ---------------------------------------------------------------------------

export interface AgentTemplate {
  id: string;
  source: string;
  division: string;
  name: string;
  role: string;
  color: string;
  emoji: string;
  vibe: string;
  description: string;
  promptBody: string;
  originalFile: string;
  createdAt: number;
}

export async function getTemplates(opts?: { division?: string; role?: string; source?: string }): Promise<AgentTemplate[]> {
  const params = new URLSearchParams();
  if (opts?.division) params.set("division", opts.division);
  if (opts?.role) params.set("role", opts.role);
  if (opts?.source) params.set("source", opts.source);
  const qs = params.toString();
  return fetchJSON(`${BASE}/agent-templates${qs ? "?" + qs : ""}`);
}

export async function getTemplateDivisions(): Promise<string[]> {
  return fetchJSON(`${BASE}/agent-templates/divisions`);
}

export async function getTemplate(id: string): Promise<AgentTemplate> {
  return fetchJSON(`${BASE}/agent-templates/${id}`);
}

// ---------------------------------------------------------------------------
// Communications
// ---------------------------------------------------------------------------

export interface Communication {
  id: string;
  fromAgentId?: string;
  toAgentId?: string;
  type: string;
  subject?: string;
  content: string;
  status: string;
  metadata?: Record<string, any>;
  createdAt: number;
}

export async function getCommunications(opts?: { projectId?: string; limit?: number }): Promise<Communication[]> {
  const params = new URLSearchParams();
  if (opts?.projectId) params.set("projectId", opts.projectId);
  if (opts?.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const data = await fetchJSON<{ communications: Communication[] }>(`${BASE}/communications${qs ? "?" + qs : ""}`);
  // BUG-028 fix: backend wraps in {communications: [...]}, unwrap here
  return data?.communications ?? (Array.isArray(data) ? data : []);
}

export async function sendCommunication(payload: {
  fromAgentId?: string;
  toAgentId: string;
  type: string;
  content: string;
  subject?: string;
  metadata?: Record<string, any>;
}) {
  return fetchJSON(`${BASE}/communications`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// User Pings
// ---------------------------------------------------------------------------

export interface UserPing {
  id?: string;
  agentId?: string;
  agentName?: string;
  type?: string;
  content?: string;
  toolName?: string;
  toolInput?: string;
  timestamp?: number;
  read?: boolean;
  agentIds?: string[];
}

export async function getUserPings(opts?: { projectId?: string; unreadOnly?: boolean; limit?: number }): Promise<UserPing[]> {
  const params = new URLSearchParams();
  if (opts?.projectId) params.set("projectId", opts.projectId);
  if (opts?.unreadOnly) params.set("unreadOnly", "true");
  if (opts?.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const data = await fetchJSON<{ pings: UserPing[] }>(`${BASE}/user-pings${qs ? "?" + qs : ""}`);
  return data?.pings ?? [];
}

export async function markPingRead(id: string) {
  return fetchJSON(`${BASE}/user-pings/${id}/read`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Alarms
// ---------------------------------------------------------------------------

export interface ProjectAlarm {
  id: string;
  fromAgentId?: string;
  toAgentId: string;
  purpose: string;
  fireAtGameSeconds: number;
  fired: boolean;
  firedAt?: number;
  createdAt: number;
}

export async function getProjectAlarms(projectId: string, opts?: { includeFired?: boolean }): Promise<{ alarms: ProjectAlarm[]; currentGameSeconds: number; realTimestamp: number }> {
  const params = new URLSearchParams();
  if (opts?.includeFired) params.set("includeFired", "true");
  const qs = params.toString();
  return fetchJSON(`${BASE}/projects/${projectId}/alarms${qs ? "?" + qs : ""}`);
}

export async function scheduleAlarm(projectId: string, alarm: { fromAgentId?: string; toAgentId: string; purpose: string; fireAtGameSeconds: number }) {
  return fetchJSON(`${BASE}/projects/${projectId}/alarms`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(alarm),
  });
}

export async function cancelAlarm(projectId: string, alarmId: string) {
  return fetchJSON(`${BASE}/projects/${projectId}/alarms/${alarmId}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Todos
// ---------------------------------------------------------------------------

export interface AgentTodos {
  agentId: string;
  todos: Array<{
    id: string;
    content: string;
    status: "pending" | "in_progress" | "completed";
    createdAt: number;
    updatedAt: number;
  }>;
}

export async function getAgentTodos(agentId: string): Promise<AgentTodos> {
  return fetchJSON(`${BASE}/chat/todos/${agentId}`);
}

export async function executeTodoWrite(agentId: string, todos: AgentTodos["todos"]) {
  return fetchJSON(`${BASE}/chat/todos/${agentId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ todos }),
  });
}

// ---------------------------------------------------------------------------
// Work Logs
// ---------------------------------------------------------------------------

export interface WorkLog {
  id: string;
  agentId?: string;
  type: string;
  summary: string;
  details?: string;
  metadata?: Record<string, any>;
  createdAt: number;
}

export async function getWorkLogs(agentId: string, limit: number = 50): Promise<WorkLog[]> {
  const data = await fetchJSON<{ logs: any[]; agentId?: string }>(`${BASE}/logs/${agentId}?limit=${limit}`);
  const rows = data?.logs ?? (Array.isArray(data) ? data : []);
  return rows.map((r: any) => ({
    id: r.id,
    agentId: r.agentId ?? r.agent_id,
    type: r.type ?? r.action ?? "discussion",
    summary: r.summary ?? "",
    details: typeof r.details === "string" ? r.details : (r.details ? JSON.stringify(r.details) : undefined),
    metadata: r.metadata,
    createdAt: r.createdAt ?? r.created_at ?? 0,
  }));
}

// ---------------------------------------------------------------------------
// Questions (Q&A)
// ---------------------------------------------------------------------------

export interface PendingQuestion {
  id: string;
  agentId: string;
  agentName?: string;
  question: string;
  context?: string;
  options?: string[];
  status: "pending" | "answered" | "timeout" | "cancelled" | "expired";
  answer?: string;
  createdAt: number;
  answeredAt?: number;
}

export async function getQuestions(opts?: { agentId?: string; projectId?: string; status?: string }): Promise<PendingQuestion[]> {
  const params = new URLSearchParams();
  if (opts?.agentId) params.set("agentId", opts.agentId);
  if (opts?.projectId) params.set("projectId", opts.projectId);
  if (opts?.status) params.set("status", opts.status);
  const qs = params.toString();
  // 后端返回 { questions: PendingQuestion[] }，这里解包成数组
  const data = await fetchJSON<{ questions: PendingQuestion[] }>(`${BASE}/chat/questions${qs ? "?" + qs : ""}`);
  return data.questions ?? [];
}

export async function answerQuestion(id: string, answer: string, agentId: string) {
  return fetchJSON(`${BASE}/chat/questions/${id}/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer, agentId }),
  });
}

// ---------------------------------------------------------------------------
// Filesystem browse
// ---------------------------------------------------------------------------

export interface BrowseResult {
  path: string;
  parent: string | null;
  currentPath?: string;
  parentPath?: string | null;
  entries: Array<{
    name: string;
    path: string;
    fullPath?: string;
    isDir: boolean;
    is_dir?: boolean;
    size?: number;
    modified?: number;
  }>;
  drives?: string[];
  isRoot?: boolean;
  error?: string;
}

export async function browseDirectory(path?: string): Promise<BrowseResult> {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  const qs = params.toString();
  return fetchJSON(`${BASE}/fs/browse${qs ? "?" + qs : ""}`);
}

// ---------------------------------------------------------------------------
// Debug / Monitoring — Agent LLM Traces
// ---------------------------------------------------------------------------

export interface TraceTurn {
  id: string;
  turn_index: number;
  raw_messages: RawTraceMessage[];
  approx_tokens: number;
  tool_call_count: number;
  summary: string;
  message_count: number;
  created_at: number;
}

export interface RawTraceMessage {
  role: string;
  content?: string | null;
  tool_calls?: any[];
  tool_call_id?: string;
  reasoning_content?: string;
  thinking?: string;
  created_at?: number;
}

export interface TraceEvent {
  id: string;
  agent_id: string;
  event_type: string;
  payload: Record<string, any>;
  created_at: number;
}

export interface AgentTraces {
  turns: TraceTurn[];
  events: TraceEvent[];
}

export async function getAgentTraces(agentId: string): Promise<AgentTraces> {
  return fetchJSON(`${BASE}/debug/agents/${agentId}/traces`);
}
