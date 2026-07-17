import { useState, useEffect, useRef } from "react";
import { getProjectGoals, updateProjectGoals } from "../api";
import type { GoalsData } from "../api";
import { useAppStore } from "../store";

interface Props {
  projectId: string;
}

const STATUS_ICON = { todo: "○", doing: "◐", done: "●" } as const;
const STATUS_COLOR = {
  todo: "text-g-fg-4",
  doing: "text-amber-400",
  done: "text-emerald-400",
} as const;
const STATUS_BG = {
  todo: "bg-gray-500/10",
  doing: "bg-amber-500/10",
  done: "bg-emerald-500/10",
} as const;
const STATUS_LABEL = { todo: "待办", doing: "进行中", done: "已完成" } as const;

const DEFAULT_GOALS: GoalsData = { objective: "", focus: "", keyResults: [], userInvolvement: "宏观决策+技术选型" };

function nextStatus(s: "todo" | "doing" | "done"): "todo" | "doing" | "done" {
  return s === "todo" ? "doing" : s === "doing" ? "done" : "todo";
}

export default function GoalsPanel({ projectId }: Props) {
  const [goals, setGoals] = useState<GoalsData>(DEFAULT_GOALS);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newKR, setNewKR] = useState("");
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editText, setEditText] = useState("");

  // 订阅 goalsVersion — agent 通过 update_goals 工具更新后，后端推 WebSocket 事件，
  // App.tsx bump goalsVersion，触发此处重新 fetch
  const goalsVersion = useAppStore((s) => s.goalsVersion);
  const goalsUpdatedProjectId = useAppStore((s) => s.goalsUpdatedProjectId);
  // 防止 dirty 时外部更新覆盖用户未保存的编辑
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  const fetchGoals = () => {
    setLoading(true);
    setError(null);
    getProjectGoals(projectId)
      .then((res) => {
        const normalized = res?.goals
          ? { ...DEFAULT_GOALS, ...res.goals, keyResults: res.goals.keyResults || [] }
          : DEFAULT_GOALS;
        setGoals(normalized);
      })
      .catch((err) => {
        console.warn("Failed to load goals:", err);
        setError(err instanceof Error ? err.message : "加载目标失败");
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchGoals();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // goalsVersion 变化时重新 fetch — 但跳过用户有未保存改动的情况，
  // 且只响应当前 projectId 的更新事件
  useEffect(() => {
    if (goalsVersion === 0) return;
    if (dirtyRef.current) return; // 用户正在编辑，不覆盖
    if (goalsUpdatedProjectId && goalsUpdatedProjectId !== projectId) return; // 其他项目的更新，跳过
    fetchGoals();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [goalsVersion]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const res = await updateProjectGoals(projectId, goals);
      if (res.goals) setGoals(res.goals);
      setDirty(false);
    } catch (err) {
      console.error("Failed to save goals:", err);
      useAppStore.getState().showToast("保存目标失败", "error");
    } finally {
      setSaving(false);
    }
  };

  const updateField = (field: "objective" | "focus" | "userInvolvement", value: string) => {
    setGoals((g) => ({ ...g, [field]: value }));
    setDirty(true);
  };

  const addKeyResult = () => {
    if (!newKR.trim()) return;
    setGoals((g) => ({
      ...g,
      keyResults: [...g.keyResults, { text: newKR.trim(), status: "todo", owner: "" }],
    }));
    setNewKR("");
    setDirty(true);
  };

  const toggleStatus = (idx: number) => {
    setGoals((g) => {
      const krs = [...g.keyResults];
      krs[idx] = { ...krs[idx], status: nextStatus(krs[idx].status) };
      return { ...g, keyResults: krs };
    });
    setDirty(true);
  };

  const removeKR = (idx: number) => {
    setGoals((g) => ({
      ...g,
      keyResults: g.keyResults.filter((_, i) => i !== idx),
    }));
    setDirty(true);
  };

  const startEdit = (idx: number) => {
    setEditingIdx(idx);
    setEditText(goals.keyResults[idx].text);
  };

  const saveEdit = () => {
    if (editingIdx === null) return;
    setGoals((g) => {
      const krs = [...g.keyResults];
      krs[editingIdx] = { ...krs[editingIdx], text: editText.trim() || krs[editingIdx].text };
      return { ...g, keyResults: krs };
    });
    setEditingIdx(null);
    setDirty(true);
  };

  const updateOwner = (idx: number, owner: string) => {
    setGoals((g) => {
      const krs = [...g.keyResults];
      krs[idx] = { ...krs[idx], owner };
      return { ...g, keyResults: krs };
    });
    setDirty(true);
  };

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center text-g-fg-4 text-sm">
        加载中...
      </div>
    );
  }

  // BUG-007 修复：API 失败时显示错误+重试，而非静默空表单
  if (error) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3 text-sm">
        <div className="text-red-600">{error}</div>
        <button
          onClick={fetchGoals}
          className="px-3 py-1 text-xs bg-g-bg border border-g-border hover:border-g-blue/40 text-g-fg rounded-md transition-colors"
        >
          重试
        </button>
      </div>
    );
  }

  const done = goals.keyResults.filter((k) => k.status === "done").length;
  const total = goals.keyResults.length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <div className="h-full overflow-y-auto p-5 space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold text-g-fg flex items-center gap-2">
          <svg className="w-5 h-5 text-g-blue" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
          </svg>
          企业目标工作簿
        </h2>
        {dirty && (
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-3 py-1 text-xs bg-g-blue text-white rounded-gm shadow-gm-sm hover:bg-blue-600 active:scale-[0.97] transition-all disabled:opacity-50"
          >
            {saving ? "保存中..." : "保存"}
          </button>
        )}
      </div>

      {/* Progress bar */}
      {total > 0 && (
        <div className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-sm p-4 space-y-2">
          <div className="flex items-center justify-between text-xs text-g-fg-3">
            <span className="font-medium">整体进度</span>
            <span className="font-mono">{done}/{total} ({pct}%)</span>
          </div>
          <div className="h-2 bg-g-bg-muted rounded-full overflow-hidden">
            <div
              className="h-full bg-gradient-to-r from-g-blue to-blue-400 rounded-full transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      )}

      {/* Basics card */}
      <div className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-sm p-4 space-y-4">
      {/* Objective */}
      <div className="space-y-1.5">
        <label className="text-xs text-g-fg-3 font-medium">目标 Objective</label>
        <input
          value={goals.objective}
          onChange={(e) => updateField("objective", e.target.value)}
          placeholder="项目总体目标..."
          className="w-full px-3 py-2 bg-g-bg-soft border border-g-border rounded-gm text-sm text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue/50 focus:ring-2 focus:ring-g-blue/15 transition-shadow"
        />
      </div>

      {/* Focus */}
      <div className="space-y-1.5">
        <label className="text-xs text-g-fg-3 font-medium">当前重点 Current Focus</label>
        <input
          value={goals.focus}
          onChange={(e) => updateField("focus", e.target.value)}
          placeholder="当前开发重点..."
          className="w-full px-3 py-2 bg-g-bg-soft border border-g-border rounded-gm text-sm text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue/50 focus:ring-2 focus:ring-g-blue/15 transition-shadow"
        />
      </div>

      {/* User Involvement */}
      <div className="space-y-1.5">
        <label className="text-xs text-g-fg-3 font-medium">用户参与度 User Involvement</label>
        <input
          value={goals.userInvolvement || ""}
          onChange={(e) => updateField("userInvolvement", e.target.value)}
          placeholder="例如:宏观决策+技术选型"
          className="w-full px-3 py-2 bg-g-bg-soft border border-g-border rounded-gm text-sm text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue/50 focus:ring-2 focus:ring-g-blue/15 transition-shadow"
        />
        <p className="text-xs text-g-fg-4/70">定义哪些类型的问题该问用户。例如"宏观决策+技术选型"表示用户参与宏观和技术选型决策;改为"纯宏观决策"则技术问题由 AI 链式上报。</p>
      </div>
      </div>

      {/* Key Results */}
      <div className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-sm p-4 space-y-2">
        <label className="text-xs text-g-fg-3 font-medium">关键结果 Key Results</label>

        <div className="space-y-1.5">
          {goals.keyResults.map((kr, idx) => (
            <div
              key={idx}
              className={`flex items-center gap-2 px-3 py-2 rounded-gm border border-g-border ${STATUS_BG[kr.status]} hover:border-g-border-strong hover:shadow-gm-sm transition-all`}
            >
              {/* Status toggle */}
              <button
                onClick={() => toggleStatus(idx)}
                className={`text-lg leading-none ${STATUS_COLOR[kr.status]} hover:scale-125 transition-transform shrink-0`}
                title={`点击切换: ${STATUS_LABEL[kr.status]} → ${STATUS_LABEL[nextStatus(kr.status)]}`}
              >
                {STATUS_ICON[kr.status]}
              </button>

              {/* Text */}
              {editingIdx === idx ? (
                <input
                  value={editText}
                  onChange={(e) => setEditText(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") saveEdit(); if (e.key === "Escape") setEditingIdx(null); }}
                  onBlur={saveEdit}
                  autoFocus
                  className="flex-1 min-w-0 px-2 py-0.5 bg-g-bg border border-g-blue/50 rounded text-sm text-g-fg focus:outline-none"
                />
              ) : (
                <span
                  onClick={() => startEdit(idx)}
                  className={`flex-1 min-w-0 text-sm cursor-pointer ${
                    kr.status === "done" ? "line-through text-g-fg-4" : "text-g-fg"
                  }`}
                >
                  {kr.text}
                </span>
              )}

              {/* Owner */}
              <input
                value={kr.owner || ""}
                onChange={(e) => updateOwner(idx, e.target.value)}
                placeholder="负责人"
                className="w-20 px-2 py-0.5 bg-transparent border border-transparent hover:border-g-border rounded text-xs text-g-fg-3 placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue/30 shrink-0 text-right"
              />

              {/* Delete */}
              <button
                onClick={() => removeKR(idx)}
                className="p-1 rounded-gm text-g-fg-4/70 hover:text-red-600 hover:bg-red-50 transition-colors shrink-0"
                title="删除"
              >
                <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          ))}
          {goals.keyResults.length === 0 && (
            <div className="flex flex-col items-center justify-center py-6 text-center">
              <span className="text-2xl mb-1.5">🎯</span>
              <p className="text-xs text-g-fg-4">尚未设定关键结果</p>
              <p className="text-[10px] text-g-fg-4/70 mt-0.5">在下方输入框添加第一条 KR</p>
            </div>
          )}
        </div>

        {/* Add new KR */}
        <div className="flex items-center gap-2 pt-1">
          <input
            value={newKR}
            onChange={(e) => setNewKR(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") addKeyResult(); }}
            placeholder="添加关键结果..."
            className="flex-1 px-3 py-1.5 bg-g-bg-soft border border-g-border rounded-gm text-sm text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue/50 focus:ring-2 focus:ring-g-blue/15 transition-shadow"
          />
          <button
            onClick={addKeyResult}
            disabled={!newKR.trim()}
            className="px-3 py-1.5 text-xs bg-g-bg border border-g-border hover:border-g-blue/50 hover:text-g-blue hover:bg-g-blue-bg/40 text-g-fg rounded-gm active:scale-[0.97] transition-all disabled:opacity-30"
          >
            添加
          </button>
        </div>
      </div>

      {/* Info note */}
      <p className="text-xs text-g-fg-4/70 leading-relaxed px-3 py-2.5 bg-g-bg-soft border border-g-border/60 rounded-gm">
        此工作簿对所有 AI Agent 可见。高层和用户共同决定开发方向，每位 Agent 会在系统提示中看到这些目标并据此对齐工作。
        点击状态图标切换: ○ 待办 → ◐ 进行中 → ● 已完成。
      </p>
    </div>
  );
}
