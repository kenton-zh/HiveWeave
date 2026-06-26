import { useState, useEffect } from "react";
import { getProjectGoals, updateProjectGoals } from "../api";
import type { GoalsData, KeyResult } from "../api";

interface Props {
  projectId: string;
}

const STATUS_ICON = { todo: "○", doing: "◐", done: "●" } as const;
const STATUS_COLOR = {
  todo: "text-gray-500",
  doing: "text-amber-400",
  done: "text-emerald-400",
} as const;
const STATUS_BG = {
  todo: "bg-gray-500/10",
  doing: "bg-amber-500/10",
  done: "bg-emerald-500/10",
} as const;
const STATUS_LABEL = { todo: "待办", doing: "进行中", done: "已完成" } as const;

function nextStatus(s: "todo" | "doing" | "done"): "todo" | "doing" | "done" {
  return s === "todo" ? "doing" : s === "doing" ? "done" : "todo";
}

export default function GoalsPanel({ projectId }: Props) {
  const [goals, setGoals] = useState<GoalsData>({ objective: "", focus: "", keyResults: [] });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [newKR, setNewKR] = useState("");
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editText, setEditText] = useState("");

  useEffect(() => {
    setLoading(true);
    getProjectGoals(projectId)
      .then((res) => {
        if (res.goals) setGoals(res.goals);
      })
      .catch((err) => console.warn("Failed to load goals:", err))
      .finally(() => setLoading(false));
  }, [projectId]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const res = await updateProjectGoals(projectId, goals);
      if (res.goals) setGoals(res.goals);
      setDirty(false);
    } catch (err) {
      console.error("Failed to save goals:", err);
      alert("保存目标失败");
    } finally {
      setSaving(false);
    }
  };

  const updateField = (field: "objective" | "focus", value: string) => {
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
      <div className="h-full flex items-center justify-center text-gray-500 text-sm">
        加载中...
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
        <h2 className="text-base font-semibold text-gray-100 flex items-center gap-2">
          <svg className="w-5 h-5 text-accent" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
          </svg>
          企业目标工作簿
        </h2>
        {dirty && (
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-3 py-1 text-xs bg-accent hover:bg-accent/80 text-white rounded-md transition-colors disabled:opacity-50"
          >
            {saving ? "保存中..." : "保存"}
          </button>
        )}
      </div>

      {/* Progress bar */}
      {total > 0 && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-xs text-gray-400">
            <span>整体进度</span>
            <span>{done}/{total} ({pct}%)</span>
          </div>
          <div className="h-2 bg-surface rounded-full overflow-hidden">
            <div
              className="h-full bg-accent rounded-full transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      )}

      {/* Objective */}
      <div className="space-y-1.5">
        <label className="text-xs text-gray-400 font-medium">目标 Objective</label>
        <input
          value={goals.objective}
          onChange={(e) => updateField("objective", e.target.value)}
          placeholder="项目总体目标..."
          className="w-full px-3 py-2 bg-surface border border-surface-border rounded-lg text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-accent/50"
        />
      </div>

      {/* Focus */}
      <div className="space-y-1.5">
        <label className="text-xs text-gray-400 font-medium">当前重点 Current Focus</label>
        <input
          value={goals.focus}
          onChange={(e) => updateField("focus", e.target.value)}
          placeholder="当前开发重点..."
          className="w-full px-3 py-2 bg-surface border border-surface-border rounded-lg text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-accent/50"
        />
      </div>

      {/* Key Results */}
      <div className="space-y-2">
        <label className="text-xs text-gray-400 font-medium">关键结果 Key Results</label>

        <div className="space-y-1.5">
          {goals.keyResults.map((kr, idx) => (
            <div
              key={idx}
              className={`flex items-center gap-2 px-3 py-2 rounded-lg border border-surface-border ${STATUS_BG[kr.status]} transition-colors`}
            >
              {/* Status toggle */}
              <button
                onClick={() => toggleStatus(idx)}
                className={`text-lg leading-none ${STATUS_COLOR[kr.status]} hover:opacity-80 transition-opacity shrink-0`}
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
                  className="flex-1 min-w-0 px-2 py-0.5 bg-surface border border-accent/50 rounded text-sm text-gray-100 focus:outline-none"
                />
              ) : (
                <span
                  onClick={() => startEdit(idx)}
                  className={`flex-1 min-w-0 text-sm cursor-pointer ${
                    kr.status === "done" ? "line-through text-gray-500" : "text-gray-200"
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
                className="w-20 px-2 py-0.5 bg-transparent border border-transparent hover:border-surface-border rounded text-xs text-gray-400 placeholder-gray-600 focus:outline-none focus:border-accent/30 shrink-0 text-right"
              />

              {/* Delete */}
              <button
                onClick={() => removeKR(idx)}
                className="text-gray-600 hover:text-red-400 transition-colors shrink-0"
                title="删除"
              >
                <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          ))}
        </div>

        {/* Add new KR */}
        <div className="flex items-center gap-2 pt-1">
          <input
            value={newKR}
            onChange={(e) => setNewKR(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") addKeyResult(); }}
            placeholder="添加关键结果..."
            className="flex-1 px-3 py-1.5 bg-surface border border-surface-border rounded-md text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-accent/50"
          />
          <button
            onClick={addKeyResult}
            disabled={!newKR.trim()}
            className="px-3 py-1.5 text-xs bg-surface-card border border-surface-border hover:border-accent/50 text-gray-300 rounded-md transition-colors disabled:opacity-30"
          >
            添加
          </button>
        </div>
      </div>

      {/* Info note */}
      <p className="text-xs text-gray-600 leading-relaxed pt-2 border-t border-surface-border">
        此工作簿对所有 AI Agent 可见。高层和用户共同决定开发方向，每位 Agent 会在系统提示中看到这些目标并据此对齐工作。
        点击状态图标切换: ○ 待办 → ◐ 进行中 → ● 已完成。
      </p>
    </div>
  );
}
