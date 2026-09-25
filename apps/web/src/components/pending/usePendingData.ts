/**
 * FE-14「待我处理」数据 hook —— 组件内自轮询既有 API（设计方案 §11.1）
 *
 * 为什么不读 store：OrgTree 把轮询结果**有损压缩**后才进 store
 * （approvals 按成员分组 / pings 只剩 agentId / alarms 只剩每成员最近一条），
 * 面板要展示摘要与等待时长 ⇒ 复用同一批 API 自取原始行。
 * 轮询节奏 30s（App.tsx/OrgTree 已有 15s 级轮询同源端点，此处减半频率，
 * 只做兜底刷新）；问题的实时性走既有 questionVersion 信号（WS
 * question_asked → bumpQuestionVersion），不额外加频。
 *
 * 口径：全部按**当前选中项目**过滤（getProjectPendingApprovals /
 * listTasks / alarms 本就按项目查询；pings/questions 传 projectId）。
 * 网络失败按组记录（§15.3：错误不伪装成空列表），后台刷新保留旧数据。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getProjectAlarms,
  getProjectPendingApprovals,
  getQuestions,
  getUserPings,
  listTasks,
  type PendingApproval,
  type PendingQuestion,
  type ProjectAlarm,
  type UserPing,
} from "../../api";
import { useAppStore } from "../../store";
import {
  buildPendingItems,
  groupPendingItems,
  type HealthErrorRow,
  type PendingGroupId,
  type VerifyingTaskRow,
} from "./model";

/** 轮询间隔：≥30s（面板是聚合兜底入口，实时性由 WS 信号补足问题组）。 */
export const PENDING_POLL_MS = 30_000;

interface PendingData {
  questions: PendingQuestion[];
  approvals: PendingApproval[];
  tasks: VerifyingTaskRow[];
  pings: UserPing[];
  alarms: ProjectAlarm[];
  alarmNowGameSeconds?: number;
}

const EMPTY_DATA: PendingData = {
  questions: [],
  approvals: [],
  tasks: [],
  pings: [],
  alarms: [],
};

export type PendingGroupErrors = Record<PendingGroupId, boolean>;

const NO_ERRORS: PendingGroupErrors = {
  questions: false,
  approvals: false,
  acceptance: false,
  attention: false,
};

function isAbortLike(e: unknown): boolean {
  return !!e && typeof e === "object" && (e as { name?: string }).name === "AbortError";
}

export function usePendingData() {
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);
  const questionVersion = useAppStore((s) => s.questionVersion);
  const agentHealth = useAppStore((s) => s.agentHealth);

  const [data, setData] = useState<PendingData>(EMPTY_DATA);
  const [errors, setErrors] = useState<PendingGroupErrors>(NO_ERRORS);
  /** 仅首轮加载显示骨架；之后的后台刷新保留旧数据（§15.3）。 */
  const [loading, setLoading] = useState(false);
  /** 等待时长的时间基准：每轮刷新时更新一次，避免每秒 tick 的渲染开销。 */
  const [now, setNow] = useState(() => Date.now());

  // 过期响应护栏：项目切换 / 重叠轮询时旧响应直接丢弃
  const genRef = useRef(0);

  const fetchRound = useCallback(async (projectId: string, isInitial: boolean) => {
    const gen = ++genRef.current;
    if (isInitial) setLoading(true);
    const [q, a, t, p, al] = await Promise.allSettled([
      getQuestions({ projectId, status: "pending" }),
      getProjectPendingApprovals(projectId),
      listTasks(projectId, { status: "verifying" }),
      getUserPings({ projectId }),
      getProjectAlarms(projectId),
    ]);
    if (gen !== genRef.current) return; // 已被更新的轮次/项目切换取代
    const next: PendingData = {
      questions: q.status === "fulfilled" && Array.isArray(q.value) ? q.value : [],
      approvals: a.status === "fulfilled" && Array.isArray(a.value) ? a.value : [],
      tasks: t.status === "fulfilled" && Array.isArray(t.value) ? t.value : [],
      pings: p.status === "fulfilled" && Array.isArray(p.value) ? p.value : [],
      alarms:
        al.status === "fulfilled" && Array.isArray(al.value?.alarms) ? al.value.alarms : [],
      alarmNowGameSeconds: al.status === "fulfilled" ? al.value?.currentGameSeconds : undefined,
    };
    const nextErrors: PendingGroupErrors = {
      questions: q.status === "rejected" && !isAbortLike(q.reason),
      approvals: a.status === "rejected" && !isAbortLike(a.reason),
      acceptance: t.status === "rejected" && !isAbortLike(t.reason),
      attention:
        (p.status === "rejected" && !isAbortLike(p.reason)) ||
        (al.status === "rejected" && !isAbortLike(al.reason)),
    };
    setData(next);
    setErrors(nextErrors);
    setNow(Date.now());
    if (isInitial) setLoading(false);
  }, []);

  // 切项目 ⇒ 清旧数据并立即拉一轮；之后 30s 兜底轮询
  useEffect(() => {
    if (!selectedProjectId) {
      genRef.current++; // 在途响应作废
      setData(EMPTY_DATA);
      setErrors(NO_ERRORS);
      setLoading(false);
      return;
    }
    const projectId = selectedProjectId;
    void fetchRound(projectId, true);
    const timer = setInterval(() => void fetchRound(projectId, false), PENDING_POLL_MS);
    return () => {
      clearInterval(timer);
      genRef.current++; // 卸载/切换：在途响应即使 settle 也不再写 state
    };
  }, [selectedProjectId, fetchRound]);

  // 问题的实时信号：WS question_asked → bumpQuestionVersion ⇒ 只补拉问题组。
  // 过期判据 = 起点之后的 genRef 是否前进（整轮刷新会覆盖问题组，无需自增 gen，
  // 否则会把在途整轮的其余四组结果一并作废）。
  const lastQuestionVersionRef = useRef(questionVersion);
  useEffect(() => {
    if (lastQuestionVersionRef.current === questionVersion) return;
    lastQuestionVersionRef.current = questionVersion;
    const projectId = useAppStore.getState().selectedProjectId;
    if (!projectId) return;
    const genAtStart = genRef.current;
    getQuestions({ projectId, status: "pending" })
      .then((qs) => {
        if (genRef.current !== genAtStart || useAppStore.getState().selectedProjectId !== projectId) return;
        setData((prev) => ({ ...prev, questions: Array.isArray(qs) ? qs : [] }));
        setErrors((prev) => ({ ...prev, questions: false }));
        setNow(Date.now());
      })
      .catch(() => {
        if (genRef.current !== genAtStart) return;
        setErrors((prev) => ({ ...prev, questions: true }));
      });
  }, [questionVersion]);

  // store 里的运行错误（WS agent_health）→ 需关注条目。
  // projectId 过滤同 OrgTree 卡片判据：无归属视为仍有效，异项目丢弃。
  const healthErrors = useMemo<HealthErrorRow[]>(() => {
    if (!selectedProjectId) return [];
    const rows: HealthErrorRow[] = [];
    for (const [agentId, info] of Object.entries(agentHealth)) {
      if (!info || info.health !== "error") continue;
      if (info.projectId && info.projectId !== selectedProjectId) continue;
      rows.push({ agentId, message: info.message, at: info.at });
    }
    return rows;
  }, [agentHealth, selectedProjectId]);

  const items = useMemo(
    () => buildPendingItems({ ...data, healthErrors }),
    [data, healthErrors],
  );
  const groups = useMemo(() => groupPendingItems(items), [items]);

  const countByGroup = useMemo(() => {
    const counts: Record<PendingGroupId, number> = {
      questions: 0,
      approvals: 0,
      acceptance: 0,
      attention: 0,
    };
    for (const g of groups) counts[g.id] = g.items.length;
    return counts;
  }, [groups]);

  const total = items.length;

  const refresh = useCallback(() => {
    const projectId = useAppStore.getState().selectedProjectId;
    if (projectId) void fetchRound(projectId, false);
  }, [fetchRound]);

  return { projectId: selectedProjectId, loading, errors, groups, countByGroup, total, now, refresh };
}
