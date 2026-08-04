/**
 * useDeepLink — URL hash 深链（Timeline v4 §5.5.1）。
 *
 * 格式：`#view=timeline&task=<id>&since=<ms>&until=<ms>`。
 * 调试时把链接贴给同事/AI 即同视角（TEST18 复盘核心痛点）。
 *
 * 读侧：store.ts 模块加载时调用 parseDeepLink() 恢复初始状态；
 * 写侧：useDeepLinkWriter 在时间轴视图内做 400ms 防抖 replaceState。
 */

import { useEffect, useRef } from "react";

export interface DeepLinkState {
  view: "timeline" | null;
  taskId: string | null;
  since: number | null;
  until: number | null;
}

export function parseDeepLink(): DeepLinkState {
  const empty: DeepLinkState = { view: null, taskId: null, since: null, until: null };
  if (typeof window === "undefined") return empty;
  const raw = window.location.hash;
  if (!raw || raw.length < 2) return empty;
  try {
    const params = new URLSearchParams(raw.slice(1));
    const since = Number(params.get("since"));
    const until = Number(params.get("until"));
    return {
      view: params.get("view") === "timeline" ? "timeline" : null,
      taskId: params.get("task") || null,
      since: Number.isFinite(since) && since > 0 ? since : null,
      until: Number.isFinite(until) && until > 0 ? until : null,
    };
  } catch {
    return empty;
  }
}

export function buildDeepLink(state: {
  taskId: string | null;
  since: number;
  until: number;
}): string {
  const params = new URLSearchParams();
  params.set("view", "timeline");
  if (state.taskId) params.set("task", state.taskId);
  params.set("since", String(Math.round(state.since)));
  params.set("until", String(Math.round(state.until)));
  return `#${params.toString()}`;
}

/**
 * 写侧：仅当处于 timeline 视图时同步 hash（400ms 防抖）。
 * 用 replaceState —— 缩放/平移不应污染浏览器历史。
 */
export function useDeepLinkWriter(opts: {
  active: boolean;
  taskId: string | null;
  since: number;
  until: number;
}): void {
  const { active, taskId, since, until } = opts;
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!active) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      try {
        const next = buildDeepLink({ taskId, since, until });
        if (window.location.hash !== next) {
          history.replaceState(null, "", next);
        }
      } catch {
        /* noop — hash 写失败不影响视图 */
      }
    }, 400);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [active, taskId, since, until]);
}
