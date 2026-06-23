const BASE = "/api";

async function fetchJSON(url: string, init?: RequestInit) {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
  return res.json();
}

export async function getOrgTree(projectId?: string) {
  const url = projectId ? `${BASE}/org?projectId=${projectId}` : `${BASE}/org`;
  return fetchJSON(url);
}

export async function getProjects() {
  return fetchJSON(`${BASE}/projects`);
}

export async function createProject(name: string, workspacePath?: string, description?: string, orgParadigm?: string) {
  return fetchJSON(`${BASE}/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, workspacePath, description, orgParadigm }),
  });
}

export async function deleteProject(id: string) {
  return fetchJSON(`${BASE}/projects/${id}`, { method: "DELETE" });
}

export async function updateProject(projectId: string, updates: { description?: string | null; orgParadigm?: string | null }) {
  return fetchJSON(`${BASE}/projects/${projectId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
}

export async function updateWorkspacePath(projectId: string, workspacePath: string | null) {
  return fetchJSON(`${BASE}/projects/${projectId}/workspace`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspacePath }),
  });
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

export async function updateAgent(id: string, data: { name?: string; goal?: string; status?: string; backstory?: string; modelId?: string | null; reasoningEffort?: string | null }) {
  return fetchJSON(`${BASE}/org/agents/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export function streamChat(agentId: string, message: string, images: string[] | undefined, onEvent: (event: { type: string; data: string }) => void): AbortController {
  const controller = new AbortController();
  fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agentId, message, images: images?.length ? images : undefined }),
    signal: controller.signal,
  }).then(async (res) => {
    if (!res.ok) {
      if (res.status === 409) {
        onEvent({ type: "busy", data: "Agent is busy" });
        return;
      }
      const errText = await res.text().catch(() => "");
      onEvent({ type: "error", data: errText || `Server error: ${res.status}` });
      return;
    }
    if (!res.body) {
      onEvent({ type: "error", data: "Response body is empty" });
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Parse SSE events from buffer
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const part of parts) {
        const eventMatch = part.match(/event: (\w+)/);
        if (!eventMatch) continue;
        // Collect all "data: " lines and join (SSE spec: multi-line data)
        const dataLines = part.match(/^data: (.*)$/gm);
        const data = dataLines
          ? dataLines.map((l) => l.replace(/^data: /, "")).join("\n")
          : "";
        onEvent({ type: eventMatch[1], data });
      }
    }
    onEvent({ type: "done", data: "" });
  }).catch((err) => {
    if (err.name !== "AbortError") {
      onEvent({ type: "error", data: err.message });
    }
  });
  return controller;
}

/**
 * Subscribe to real-time agent processing status via SSE.
 * Calls onSnapshot with all currently-processing agent IDs on connect,
 * then onStatus for each incremental change.
 * Auto-reconnects after 3 seconds on disconnect.
 * Returns an AbortController to stop the subscription.
 */
export function subscribeAgentStatus(
  onSnapshot: (agentIds: string[], paused?: boolean) => void,
  onStatus: (agentId: string, processing: boolean) => void,
  onActivity?: (event: { agentId: string; agentName: string; type: string; content?: string; toolName?: string; toolInput?: string; toolResult?: string; errorMessage?: string; timestamp: number }) => void,
): AbortController {
  const controller = new AbortController();
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  function connect() {
    if (controller.signal.aborted) return;

    fetch(`${BASE}/chat/status`, { signal: controller.signal })
      .then(async (res) => {
        if (!res.ok || !res.body) {
          throw new Error(`Status SSE: HTTP ${res.status}`);
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";

          for (const part of parts) {
            const typeMatch = part.match(/event: (\w+)/);
            if (!typeMatch) continue;
            const type = typeMatch[1];
            const dataMatch = part.match(/^data: (.*)$/m);
            if (!dataMatch) continue;

            try {
              const json = JSON.parse(dataMatch[1]);
              if (type === "snapshot" && Array.isArray(json.agentIds)) {
                onSnapshot(json.agentIds, json.paused ?? false);
              } else if (type === "status" && typeof json.agentId === "string") {
                onStatus(json.agentId, !!json.processing);
              } else if (type === "activity" && typeof json.agentId === "string") {
                onActivity?.(json);
              }
            } catch {
              // Malformed SSE event — skip
            }
          }
        }

        // Stream ended — reconnect after 3s (server may have restarted)
        if (!controller.signal.aborted) {
          reconnectTimer = setTimeout(connect, 3000);
        }
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        // Reconnect after 3 seconds
        if (!controller.signal.aborted) {
          reconnectTimer = setTimeout(connect, 3000);
        }
      });
  }

  connect();

  // Wrap abort to also clear reconnect timer
  const origAbort = controller.abort.bind(controller);
  controller.abort = ((...args: any[]) => {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    origAbort(...args);
  }) as typeof controller.abort;

  return controller;
}

export async function pauseSystem(): Promise<{ paused: boolean }> {
  return fetchJSON(`${BASE}/chat/pause`, { method: "POST" });
}

export async function resumeSystem(): Promise<{ paused: boolean }> {
  return fetchJSON(`${BASE}/chat/resume`, { method: "POST" });
}

export async function getPausedState(): Promise<{ paused: boolean }> {
  return fetchJSON(`${BASE}/chat/paused`);
}

export async function getWorkLogs(agentId: string, limit = 10) {
  return fetchJSON(`${BASE}/logs/${agentId}?limit=${limit}`);
}

export async function deleteAgent(id: string) {
  return fetchJSON(`${BASE}/org/agents/${id}`, { method: "DELETE" });
}

export async function getChatMessages(agentId: string) {
  return fetchJSON(`${BASE}/chat/messages/${agentId}`);
}

export async function getUnreadMessages(agentId: string) {
  return fetchJSON(`${BASE}/chat/unread/${agentId}`);
}

export async function markMessagesRead(ids: string[]) {
  return fetchJSON(`${BASE}/chat/mark-read`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
}

export interface ActiveCommunication {
  id: string;
  fromAgentId: string;
  toAgentId: string;
  type: "dispatch" | "message" | "trigger";
  createdAt: number;
}

export async function getCommunications(): Promise<ActiveCommunication[]> {
  return fetchJSON(`${BASE}/org/communications`);
}

// --- Permission management ---

export async function getPermissionRules(agentId: string) {
  return fetchJSON(`${BASE}/permissions/rules/${agentId}`);
}

export async function updatePermissionRules(agentId: string, rules: {
  permissionMode?: string;
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

export async function getPendingApprovals(agentId: string) {
  return fetchJSON(`${BASE}/permissions/pending/${agentId}`);
}

export async function getProjectPendingApprovals(projectId: string) {
  return fetchJSON(`${BASE}/permissions/pending/project/${projectId}`);
}

export async function respondToApproval(requestId: string, approved: boolean, remember = false, userNote?: string) {
  return fetchJSON(`${BASE}/permissions/respond`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ requestId, approved, remember, userNote }),
  });
}

// --- Roster (Personnel Records) ---

export async function getRoster(projectId: string) {
  return fetchJSON(`${BASE}/org/roster/${projectId}`);
}

export async function getAgentRoster(agentId: string) {
  return fetchJSON(`${BASE}/org/roster/agent/${agentId}`);
}

// --- Filesystem browse (for folder picker) ---

export interface BrowseResult {
  currentPath: string;
  parentPath: string | null;
  entries: Array<{ name: string; isDir: boolean; fullPath: string }>;
  drives: string[];
  isRoot: boolean;
}

export async function browseDirectory(dirPath?: string): Promise<BrowseResult> {
  const url = dirPath
    ? `${BASE}/fs/browse?path=${encodeURIComponent(dirPath)}`
    : `${BASE}/fs/browse`;
  return fetchJSON(url);
}

// --- LLM Models ---

export interface LlmModel {
  id: string;
  name: string;
  modelId: string;
  baseUrl: string;
  apiKey: string;
  contextWindow: number;
  maxOutputTokens: number;
  supportsThinking: boolean;
  defaultReasoningEffort: string | null;
  temperature: string | null;
  isActive: boolean;
  createdAt: number;
  updatedAt: number;
}

export async function getModels(): Promise<LlmModel[]> {
  return fetchJSON(`${BASE}/models`);
}

export async function getAllModels(): Promise<LlmModel[]> {
  return fetchJSON(`${BASE}/models/all`);
}

export async function getModel(id: string): Promise<LlmModel> {
  return fetchJSON(`${BASE}/models/${id}`);
}

export async function createModel(data: Partial<LlmModel>): Promise<LlmModel> {
  return fetchJSON(`${BASE}/models`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateModel(id: string, data: Partial<LlmModel>): Promise<LlmModel> {
  return fetchJSON(`${BASE}/models/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function deleteModel(id: string): Promise<void> {
  return fetchJSON(`${BASE}/models/${id}`, { method: "DELETE" });
}

export async function testModel(id: string): Promise<{ ok: boolean; latencyMs: number; error?: string }> {
  return fetchJSON(`${BASE}/models/${id}/test`, { method: "POST" });
}

// --- Agent Templates ---

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
  originalFile: string;
  promptBody?: string;
}

export async function getTemplates(params?: { source?: string; division?: string; role?: string; search?: string }): Promise<AgentTemplate[]> {
  const query = new URLSearchParams();
  if (params?.source) query.set("source", params.source);
  if (params?.division) query.set("division", params.division);
  if (params?.role) query.set("role", params.role);
  if (params?.search) query.set("search", params.search);
  const qs = query.toString();
  return fetchJSON(`${BASE}/templates${qs ? `?${qs}` : ""}`);
}

export async function getTemplateDivisions(): Promise<Array<{ division: string; count: number }>> {
  return fetchJSON(`${BASE}/templates/divisions`);
}

export async function getTemplate(id: string): Promise<AgentTemplate> {
  return fetchJSON(`${BASE}/templates/${id}`);
}
