/**
 * useLiveStatusPoll — per-agent 实时活动相位，写入共享 store。
 *
 * 单一数据源纪律（09-08 #9）：此前轮询局部活在 OrgTree 里，ChatPanel
 * 头部用聊天流派生状态，两处对同一 agent 同帧给出「LLM」和「空闲」两
 * 个答案。现在上移到 App 根挂载一次，OrgTree 徽章与 ChatPanel 头部都
 * 读 store.liveMap。
 *
 * P4-1（2026-09-18）：**事件驱动为主 + 长间隔兜底**，不再固定 4s 轮询
 * （15 req/min → 事件驱动 ~2 req/min）：
 * - WS `status_change` / `activity` 事件触发节流刷新（≥2s 间隔）；
 * - 30s 静默（无任何事件/刷新）才由兜底检查器补一轮 —— 删轮询 ≠ 删兜底
 *   （WS 断线时仍能恢复；阳性对照 = 手动断 WS，30s 内刷新恢复）。
 */

import { useEffect } from "react";
import { getAgentsLiveStatus, type AgentLiveStatus } from "../api";
import { getJoinedLobbyChannel } from "../api/ws";
import { useAppStore } from "../store";

const MIN_REFETCH_MS = 2_000;
const FALLBACK_MS = 30_000;
const FALLBACK_CHECK_MS = 5_000;

export function useLiveStatusPoll(projectId: string | null | undefined) {
  const setLiveMap = useAppStore((s) => s.setLiveMap);
  useEffect(() => {
    if (!projectId) {
      setLiveMap({});
      return;
    }
    let alive = true;
    let pending = false;
    let lastFetch = 0;

    const poll = async () => {
      if (pending) return;
      pending = true;
      lastFetch = Date.now();
      try {
        const rows = await getAgentsLiveStatus(projectId);
        if (!alive) return;
        const map: Record<string, AgentLiveStatus> = {};
        for (const r of rows) map[r.agent_id] = r;
        setLiveMap(map);
      } catch {
        /* 瞬态网络错误：下个事件/兜底周期再试 */
      } finally {
        pending = false;
      }
    };

    void poll();

    // WS 事件 kick（节流）：必须绑**已 join** 的 lobby channel 单例
    // （P4 审计 C-1：socket.channel() 每次 new 未 join 实例，推送被
    // joinRef 过滤丢弃）。phoenix on() 返回数字 ref，off(event, ref)
    // 按 ref 解绑（H-2：传 callback 是 no-op）。单例未就绪时有限次重试
    // （App 的 subscribe effect 可能晚于本 effect 注册），超时则纯兜底轮询。
    let off: (() => void) | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    const bind = () => {
      const lobby = getJoinedLobbyChannel();
      if (!lobby) {
        if (alive && ++attempts <= 20) retryTimer = setTimeout(bind, 500);
        return;
      }
      // phoenix 运行时 on() 返回数字 ref，但 .d.ts 声明成 void —— 断言后
      // 供 off(event, ref) 按 ref 解绑（审计 H-2）。
      const refStatus = lobby.on("status_change", kick) as unknown as number;
      const refActivity = lobby.on("activity", kick) as unknown as number;
      // .d.ts 只声明了 off(event, callback) 重载；运行时按 ref 过滤
      // （审计 H-2），经窄化类型调用 ref 形态。
      // 必须 bind(lobby)：裸提取方法会把 this 剥成 undefined，phoenix off
      // 内部读 this.bindings 即 TypeError（09-18 实测＝切项目白屏根因）。
      const offByRef = lobby.off.bind(lobby) as unknown as (
        event: string, ref: number
      ) => void;
      off = () => {
        offByRef("status_change", refStatus);
        offByRef("activity", refActivity);
      };
    };
    const kick = () => {
      if (Date.now() - lastFetch >= MIN_REFETCH_MS) void poll();
    };
    bind();

    // 兜底：仅当超过 FALLBACK_MS 没有任何刷新时才真正发请求
    const i = setInterval(() => {
      if (Date.now() - lastFetch >= FALLBACK_MS - 2_000) void poll();
    }, FALLBACK_CHECK_MS);

    return () => {
      alive = false;
      clearInterval(i);
      if (retryTimer) clearTimeout(retryTimer);
      off?.();
    };
  }, [projectId, setLiveMap]);
}
