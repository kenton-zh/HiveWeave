/**
 * useLiveStatusPoll — per-agent 实时活动相位轮询（4s），写入共享 store。
 *
 * 单一数据源纪律（09-08 #9）：此前轮询局部活在 OrgTree 里，ChatPanel
 * 头部用聊天流派生状态，两处对同一 agent 同帧给出「LLM」和「空闲」两
 * 个答案。现在轮询上移到 App 根挂载一次，OrgTree 徽章与 ChatPanel 头
 * 部都读 store.liveMap。
 */

import { useEffect } from "react";
import { getAgentsLiveStatus, type AgentLiveStatus } from "../api";
import { useAppStore } from "../store";

export function useLiveStatusPoll(projectId: string | null | undefined) {
  const setLiveMap = useAppStore((s) => s.setLiveMap);
  useEffect(() => {
    if (!projectId) {
      setLiveMap({});
      return;
    }
    let alive = true;
    const poll = async () => {
      const rows = await getAgentsLiveStatus(projectId);
      if (!alive) return;
      const map: Record<string, AgentLiveStatus> = {};
      for (const r of rows) map[r.agent_id] = r;
      setLiveMap(map);
    };
    void poll();
    const i = setInterval(poll, 4000);
    return () => {
      alive = false;
      clearInterval(i);
    };
  }, [projectId, setLiveMap]);
}
