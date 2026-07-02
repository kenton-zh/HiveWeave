import { Socket, Channel } from "phoenix";

/**
 * Phoenix.js WebSocket-based API client.
 *
 * This replaces the old SSE-based client (fetch + EventSource).
 * The Elixir backend now uses Phoenix Channels over WebSocket.
 *
 * Channels used:
 *   - "lobby:status"  - global agent processing status
 *   - "agent:<id>"    - per-agent chat stream + status + inbox
 *   - "project:<id>"  - per-project game time + status
 */

// ---------------------------------------------------------------------------
// Socket setup
// ---------------------------------------------------------------------------

const SOCKET_URL =
  (import.meta.env?.VITE_WS_URL as string | undefined) ||
  (typeof window !== "undefined" && window.location.hostname === "localhost"
    ? "ws://localhost:4000/socket"
    : "/socket");

let _socket: Socket | null = null;
let _activeChannel: any = null; // Track the active agent channel to prevent duplicates

export function getSocket(): Socket {
  if (!_socket) {
    const params: Record<string, string> = {};
    if (_apiKey) params.api_key = _apiKey;
    _socket = new Socket(SOCKET_URL, {
      params,
      reconnectAfterMs: (tries: number) => [1000, 2000, 5000, 10000][tries - 1] ?? 10000,
      heartbeatIntervalMs: 30_000,
    });
    _socket.connect();
  }
  return _socket;
}

// ---------------------------------------------------------------------------
// REST helpers
// ---------------------------------------------------------------------------

const BASE = "/api";

let _apiKey: string | null = null;

export function setApiKey(key: string | null) {
  _apiKey = key;
}

async function fetchJSON<T = any>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (_apiKey && !headers.has("x-api-key")) {
    headers.set("x-api-key", _apiKey);
  }
  const res = await fetch(url, { ...init, headers });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export async function getProjects(): Promise<Project[]> {
  const data = await fetchJSON<{ projects: Project[] }>(`${BASE}/projects`);
  return data.projects || [];
}

export async function createProject(name: string, workspacePath?: string, description?: string, orgParadigm?: string, language?: string) {
  return fetchJSON(`${BASE}/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, workspacePath, description, orgParadigm, language: language || "zh" }),
  });
}

export async function deleteProject(id: string) {
  return fetchJSON(`${BASE}/projects/${id}`, { method: "DELETE" });
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
    body: JSON.stringify(goals),
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
  return fetchJSON(`${BASE}/org/agents/${id}`);
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

export async function deleteAgent(id: string) {
  return fetchJSON(`${BASE}/org/agents/${id}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Chat (HTTP trigger - actual stream via WebSocket)
// ---------------------------------------------------------------------------

export interface ChatEvent {
  type: "text" | "tool_use" | "tool_result" | "message_id" | "error" | "done" | "busy";
  data: string;
}

export function streamChat(
  agentId: string,
  message: string,
  images: string[] | undefined,
  onEvent: (event: ChatEvent) => void
): { abort: () => void } {
  const socket = getSocket();

  // Leave any previous active channel to prevent duplicate event delivery
  if (_activeChannel) {
    try { _activeChannel.leave(); } catch {}
    _activeChannel = null;
  }

  const channel = socket.channel(`agent:${agentId}`);
  _activeChannel = channel;

  // Translate phoenix.js events into the legacy SSE event shape
  // so the existing ChatPanel.tsx business logic keeps working unchanged.
  channel.on("init", (payload) => {
    // No-op for now; payload could include initial state
  });

  channel.on("message_id", (payload) => {
    onEvent({ type: "message_id", data: JSON.stringify(payload) });
  });

  channel.on("stream_chunk", (payload) => {
    const text = typeof payload === "string" ? payload : payload.text || "";
    if (typeof payload === "object" && payload.delta) {
      // Real-time token delta — forward as "text" event so ChatPanel's
      // existing incremental-append logic handles it.
      const deltaId = payload.deltaId || "";
      if (payload.reasoning) {
        onEvent({ type: "thinking_delta", data: text, deltaId });
      } else {
        onEvent({ type: "text", data: text });
      }
    } else {
      // Non-delta (full text or reasoning)
      onEvent({ type: "text", data: text });
    }
  });

  channel.on("stream_tool", (payload) => {
    if (payload.type === "tool_use") {
      onEvent({ type: "tool_use", data: JSON.stringify(payload) });
    } else if (payload.type === "tool_result") {
      onEvent({ type: "tool_result", data: JSON.stringify(payload) });
    }
  });

  channel.on("status_change", (payload) => {
    // Forward as "done"-like marker; ChatPanel mostly uses this to know activity ended
  });

  channel.on("done", () => {
    onEvent({ type: "done", data: "" });
    // Channel cleanup is handled by ChatPanel's abort; just clear the ref
    if (_activeChannel === channel) _activeChannel = null;
  });

  channel.on("error", (payload) => {
    onEvent({ type: "error", data: payload?.message || "Unknown error" });
  });

  channel.join().receive("ok", () => {
    channel.push("chat", { message, images: images?.length ? images : undefined });
  }).receive("error", (resp) => {
    onEvent({ type: "error", data: JSON.stringify(resp) });
  });

  return {
    abort: () => {
      channel.push("cancel", {});
      channel.leave();
      if (_activeChannel === channel) _activeChannel = null;
    },
  };
}

// ---------------------------------------------------------------------------
// Agent status subscription (was SSE-based before)
// ---------------------------------------------------------------------------

export interface ActivityEntry {
  agentId: string;
  agentName: string;
  type: string;
  content?: string;
  deltaId?: string;
  toolName?: string;
  // The Elixir backend sometimes forwards these as raw objects (from the
  // stream_event) and sometimes as JSON strings (from the activity broadcast).
  // Renderers must handle both shapes.
  toolInput?: string | object;
  toolResult?: string | object;
  errorMessage?: string;
  timestamp: number;
}

export function subscribeAgentStatus(
  onSnapshot: (agentIds: string[], paused?: boolean) => void,
  onStatus: (agentId: string, processing: boolean) => void,
  onActivity?: (event: ActivityEntry) => void,
  onOrgChanged?: () => void
): { abort: () => void } {
  const socket = getSocket();
  const channel = socket.channel("lobby:status");

  channel.on("init", (payload) => {
    if (Array.isArray(payload.agentIds)) {
      onSnapshot(payload.agentIds, payload.paused ?? false);
    }
  });

  channel.on("status_change", (payload) => {
    if (typeof payload.agentId === "string") {
      onStatus(payload.agentId, !!payload.processing);
    }
  });

  channel.on("org_changed", () => {
    onOrgChanged?.();
  });

  channel.on("activity", (payload) => {
    if (onActivity && typeof payload.agentId === "string") {
      onActivity({
        agentId: payload.agentId,
        agentName: payload.agentName || "",
        type: payload.type || "",
        content: payload.content,
        deltaId: payload.deltaId,
        toolName: payload.toolName,
        toolInput: payload.toolInput,
        toolResult: payload.toolResult,
        errorMessage: payload.errorMessage,
        timestamp: payload.timestamp || Date.now(),
      });
    }
  });

  channel.join().receive("ok", () => {
    // Initial snapshot is pushed via "init" event
  }).receive("error", () => {
    onSnapshot([], false);
  });

  return {
    abort: () => {
      channel.leave();
    },
  };
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
  return fetchJSON(`${BASE}/permissions/pending/${agentId}`);
}

/** Get all pending approval requests for a project. */
export async function getProjectPendingApprovals(projectId: string): Promise<PendingApproval[]> {
  return fetchJSON(`${BASE}/permissions/pending/project/${projectId}`);
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
  return fetchJSON(`${BASE}/communications${qs ? "?" + qs : ""}`);
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
  id: string;
  agentId: string;
  agentName?: string;
  type: string;
  content: string;
  toolName?: string;
  toolInput?: string;
  timestamp: number;
  read: boolean;
}

export async function getUserPings(opts?: { unreadOnly?: boolean; limit?: number }): Promise<UserPing[]> {
  const params = new URLSearchParams();
  if (opts?.unreadOnly) params.set("unreadOnly", "true");
  if (opts?.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return fetchJSON(`${BASE}/user-pings${qs ? "?" + qs : ""}`);
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

export async function getProjectAlarms(projectId: string, opts?: { includeFired?: boolean }): Promise<ProjectAlarm[]> {
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
  agentId: string;
  action: string;
  summary: string;
  details?: string;
  metadata?: Record<string, any>;
  createdAt: number;
}

export async function getWorkLogs(agentId: string, limit: number = 50): Promise<WorkLog[]> {
  return fetchJSON(`${BASE}/logs/${agentId}?limit=${limit}`);
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
  status: "pending" | "answered" | "expired";
  answer?: string;
  createdAt: number;
  answeredAt?: number;
}

export async function getQuestions(opts?: { agentId?: string; status?: string }): Promise<PendingQuestion[]> {
  const params = new URLSearchParams();
  if (opts?.agentId) params.set("agentId", opts.agentId);
  if (opts?.status) params.set("status", opts.status);
  const qs = params.toString();
  return fetchJSON(`${BASE}/chat/questions${qs ? "?" + qs : ""}`);
}

export async function answerQuestion(id: string, answer: string) {
  return fetchJSON(`${BASE}/chat/questions/${id}/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer }),
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


// Re-export the Channel for advanced usage
export { Channel };
