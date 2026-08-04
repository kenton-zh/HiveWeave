/**
 * TaskPicker — 任务选择器（Timeline v4 §5.2）。
 *
 * 两种入口：
 *  1. 搜索下拉：list 端点（后端排除归档），标题/ID 前缀纯客户端过滤；
 *  2. task_id 直达：输入任意 task_id 回车 —— 归档任务的唯一入口
 *     （list 端点不含归档，但端点 1 支持归档任务）。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { listTasks } from "../../api";
import { useAppStore } from "../../store";
import type { TaskSummary } from "./types";
import { statusStyle, STRIPED_OVERLAY } from "./utils";

export default function TaskPicker() {
  const projectId = useAppStore((s) => s.selectedProjectId);
  const selectedTaskId = useAppStore((s) => s.selectedTaskId);
  const setSelectedTask = useAppStore((s) => s.setSelectedTask);

  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  // 项目变化时拉一次任务列表（非归档全集，之后纯客户端过滤）
  useEffect(() => {
    if (!projectId) {
      setTasks([]);
      return;
    }
    let cancelled = false;
    setError(null);
    setTasks([]); // 先清旧项目列表，避免新列表到达前误选旧任务（会 404）
    listTasks(projectId)
      .then((rows) => {
        if (!cancelled) setTasks(rows);
      })
      .catch((e: any) => {
        if (cancelled || e?._aborted) return;
        setError(e?.message || "任务列表加载失败");
        setTasks([]);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // 点击组件外收起下拉
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return tasks.slice(0, 50);
    return tasks
      .filter(
        (t) =>
          t.title.toLowerCase().includes(q) || t.id.toLowerCase().startsWith(q),
      )
      .slice(0, 50);
  }, [tasks, query]);

  const q = query.trim();

  const pick = (id: string) => {
    setSelectedTask(id);
    setQuery("");
    setOpen(false);
  };

  return (
    <div ref={boxRef} className="relative w-full">
      <div className="relative">
        <svg
          className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-g-fg-4 pointer-events-none"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M21 21l-4.35-4.35M17 10a7 7 0 11-14 0 7 7 0 0114 0z"
          />
        </svg>
        <input
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && q) pick(q); // task_id 直达（归档任务唯一入口）
            if (e.key === "Escape") setOpen(false);
          }}
          placeholder="搜索任务，或粘贴 task_id 直达（含已归档）"
          className="w-full pl-8 pr-8 py-1.5 text-xs rounded-gm border border-g-border bg-g-bg text-g-fg placeholder:text-g-fg-4 focus:outline-none focus:border-g-border-focus transition-colors"
        />
        {q && (
          <button
            onClick={() => setQuery("")}
            className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 flex items-center justify-center rounded-full text-g-fg-4 hover:text-g-fg hover:bg-g-bg-muted transition-colors"
            title="清空"
          >
            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        )}
      </div>

      {error && !open && (
        <p className="mt-1 text-[11px] text-g-red">{error}</p>
      )}

      {open && projectId && (
        <div className="absolute z-30 left-0 right-0 mt-1 max-h-72 overflow-y-auto rounded-gm border border-g-border bg-g-bg shadow-gm-pop animate-scale-in">
          {filtered.length === 0 && (
            <div className="px-3 py-4 text-center text-xs text-g-fg-4">
              没有匹配的任务
            </div>
          )}
          {filtered.map((t) => {
            const st = statusStyle(t.status);
            return (
              <button
                key={t.id}
                onClick={() => pick(t.id)}
                className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-g-bg-soft transition-colors"
              >
                <span
                  className={`w-2 h-2 rounded-full shrink-0 ${st.bar}`}
                  style={st.striped ? STRIPED_OVERLAY : undefined}
                />
                <span className="flex-1 min-w-0">
                  <span className="block text-xs text-g-fg truncate">{t.title}</span>
                  <span className="block text-[10px] text-g-fg-4 font-mono truncate">
                    {t.id.slice(0, 8)} · {st.label}
                  </span>
                </span>
              </button>
            );
          })}
          {q && (
            <button
              onClick={() => pick(q)}
              className="w-full flex items-center gap-2 px-3 py-2 text-left border-t border-g-border hover:bg-g-bg-soft transition-colors"
              title="按 task_id 直接打开（支持已归档任务）"
            >
              <svg className="w-3.5 h-3.5 text-g-blue shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7l5 5-5 5M6 12h12" />
              </svg>
              <span className="text-xs text-g-blue truncate">
                直达 task_id：{q.slice(0, 36)}
              </span>
            </button>
          )}
        </div>
      )}

      {/* 当前选中任务提示 */}
      {selectedTaskId && (
        <div className="mt-1 flex items-center gap-1 text-[11px] text-g-fg-3">
          <span className="font-mono truncate">
            当前任务：{selectedTaskId.slice(0, 12)}…
          </span>
          <button
            onClick={() => setSelectedTask(null)}
            className="text-g-fg-4 hover:text-g-red transition-colors"
            title="取消选中"
          >
            清除
          </button>
        </div>
      )}
    </div>
  );
}
