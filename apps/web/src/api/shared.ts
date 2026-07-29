/**
 * Shared API client state (api key + debug logger).
 * Used by both REST and WebSocket modules.
 */

const BASE = "/api";

let _apiKey: string | null = null;

export function getApiKey(): string | null {
  return _apiKey;
}

export function getBaseUrl(): string {
  return BASE;
}

export function setApiKey(key: string | null) {
  _apiKey = key;
  // 同步到 Zustand store（通过全局引用避免循环依赖）
  try {
    const store = (window as any).__hwStore;
    if (store) store.getState().setApiKey(key);
  } catch { /* noop */ }
  // 持久化到 localStorage
  try {
    if (key) localStorage.setItem("hiveweave_api_key", key);
    else localStorage.removeItem("hiveweave_api_key");
  } catch { /* noop */ }
}

/** 从 localStorage 恢复 apiKey — 应用启动时调用 */
export function initApiKeyFromStorage(): string | null {
  try {
    const key = localStorage.getItem("hiveweave_api_key");
    if (key) {
      _apiKey = key;
      const store = (window as any).__hwStore;
      if (store) store.getState().setApiKey(key);
    }
    return key;
  } catch {
    return null;
  }
}

// Debug log helper — writes to Zustand store without circular import.
// Uses queueMicrotask to avoid "Cannot update a component while rendering
// a different component" warning when API calls happen during render.
export function dbg(category: "api" | "ws" | "error" | "info" | "state", message: string, data?: any) {
  queueMicrotask(() => {
    try {
      const store = (window as any).__hwStore;
      if (store) store.getState().addDebugLog({ category, message, data });
    } catch { /* noop */ }
  });
}
