const BASE = "/api";

export async function getOrgTree() {
  const res = await fetch(`${BASE}/org`);
  return res.json();
}

export async function getAgent(id: string) {
  const res = await fetch(`${BASE}/org/agents/${id}`);
  return res.json();
}

export async function createAgent(data: any) {
  const res = await fetch(`${BASE}/org/agents`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  return res.json();
}

export function streamChat(agentId: string, message: string, onEvent: (event: { type: string; data: string }) => void): AbortController {
  const controller = new AbortController();
  fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agentId, message }),
    signal: controller.signal,
  }).then(async (res) => {
    const reader = res.body!.getReader();
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
        const dataMatch = part.match(/data: (.+)/);
        if (eventMatch && dataMatch) {
          onEvent({ type: eventMatch[1], data: dataMatch[1] });
        }
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
  const res = await fetch(`${BASE}/logs/${agentId}?limit=${limit}`);
  return res.json();
}
