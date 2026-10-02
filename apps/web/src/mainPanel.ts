import { lazy, type ComponentType } from "react";

export type LeftPanelKind = "tree" | "timeline" | "token" | "token-empty" | "office";

/**
 * 办公室界面总开关（暂时关闭）。
 *
 * false = 主界面回到「组织图 + Chat」三栏工作台：左栏不渲染 Office 页签、
 * 不挂载 OfficeWorkspace/PixiJS 场景、右下角无回办公室入口。
 * 办公室相关代码（OfficeView / OfficeWorkspace / gamewindow / office/*）**全部保留**，
 * 只由本开关切断入口 —— 翻回 true 即恢复，无需再改其它文件。
 */
export const OFFICE_UI_ENABLED = false;

/**
 * 左栏形态解析。Token 不能落到 Office 的 Suspense（英文 "Loading..."）；
 * 办公室关闭时 `office` 视图（深链/残留 state）一律回落组织图，避免空白面板。
 */
export function resolveLeftPanel(
  activeView: string,
  projectId: string | null | undefined,
): LeftPanelKind {
  if (activeView === "timeline") return "timeline";
  if (activeView === "token") return projectId ? "token" : "token-empty";
  if (activeView === "office") return OFFICE_UI_ENABLED ? "office" : "tree";
  return "tree";
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
