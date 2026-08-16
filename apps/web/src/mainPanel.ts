import { lazy, type ComponentType } from "react";

export type LeftPanelKind = "tree" | "timeline" | "token" | "token-empty" | "office";

/** Token must not fall through to Office's Suspense (English "Loading..."). */
export function resolveLeftPanel(
  activeView: string,
  projectId: string | null | undefined,
): LeftPanelKind {
  if (activeView === "tree") return "tree";
  if (activeView === "timeline") return "timeline";
  if (activeView === "token") return projectId ? "token" : "token-empty";
  return "office";
}

/** Vite HMR / chunk hash mismatch rejects React.lazy() once; retry before sticking on fallback. */
export async function importWithRetry<T>(load: () => Promise<T>): Promise<T> {
  try {
    return await load();
  } catch {
    await new Promise((r) => setTimeout(r, 200));
    return await load();
  }
}

export function lazyRetry<T extends ComponentType<any>>(
  load: () => Promise<{ default: T }>,
) {
  return lazy(() => importWithRetry(load));
}
