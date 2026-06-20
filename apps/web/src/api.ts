const BASE = "/api";

async function fetchJSON(url: string, init?: RequestInit) {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
  return res.json();
}

export async function getOrgTree() {
  return fetchJSON(`${BASE}/org`);
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

export function streamChat(agentId: string, message: string, onEvent: (event: { type: string; data: string }) => void): AbortController {
  const controller = new AbortController();
  fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agentId, message }),
    signal: controller.signal,
  }).then(async (res) => {
    if (!res.ok) {
      onEvent({ type: "error", data: `Server error: ${res.status}` });
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

export async function getWorkLogs(agentId: string, limit = 10) {
  return fetchJSON(`${BASE}/logs/${agentId}?limit=${limit}`);
}
