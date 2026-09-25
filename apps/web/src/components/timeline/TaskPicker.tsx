/**
 * TaskPicker — 任务选择器（Timeline v4 §5.2）。
 *
 * 搜索与直达的边界（FE-10 / SR-03，方案 §9.6，验收 T-09/T-16）：
 *  1. 输入即过滤候选，方向键移动高亮、回车选中高亮项 —— 纯标题回车
 *     绝不把输入当 task_id 跳伪 ID；
 *  2. 只有「完整任务 ID」（UUID 全量或与已知任务 id 精确相等）才直达，
 *     归档任务的唯一入口保留在直达路径上；
 *  3. 无匹配显示「无匹配任务」，不生成伪 ID；
 *  4. Esc 收起下拉；选择后返回列表保留搜索词（不清空）；
 *  5. 候选随现有失效信号（store.timelineVersion）刷新，不只项目变化时拉一次。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { listTasks } from "../../api";
import { useAppStore } from "../../store";
import { openTask } from "../../navigation/commands"; // FE-02：统一任务打开（旧 setSelectedTask 换入）
import type { TaskSummary } from "./types";
import { statusStyle, STRIPED_OVERLAY } from "./utils";

/** UUID v4 形态（后端 task id 生成格式）——完整 ID 直达的格式判据。 */
const FULL_TASK_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * 输入是否「完整任务 ID」：UUID 全量，或与已知任务 id 精确相等。
 * 只有这种情况才允许 Enter 直达（归档任务不在候选列表里，靠格式判据兜住）。
 */
export function isFullTaskId(query: string, tasks: TaskSummary[]): boolean {
  const q = query.trim();
  if (!q) return false;
  if (FULL_TASK_ID_RE.test(q)) return true;
  return tasks.some((t) => t.id.toLowerCase() === q.toLowerCase());
}

export default function TaskPicker() {
  const projectId = useAppStore((s) => s.selectedProjectId);
  const selectedTaskId = useAppStore((s) => s.selectedTaskId);
  const timelineVersion = useAppStore((s) => s.timelineVersion);

  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const prevProjectRef = useRef<string | null>(null);

  // 拉取时机（§9.6）：项目变化（清旧列表防误选）+ 现有失效信号
  // timelineVersion（新建/变更任务后候选刷新），不再只拉一次。
  useEffect(() => {
    if (!projectId) {
      setTasks([]);
      prevProjectRef.current = null;
      return;
    }
    const projectChanged = prevProjectRef.current !== projectId;
    prevProjectRef.current = projectId;
    if (projectChanged) {
      setTasks([]); // 先清旧项目列表，避免新列表到达前误选旧任务（会 404）
      setError(null);
    }
    let cancelled = false;
    listTasks(projectId)
      .then((rows) => {
        if (!cancelled) setTasks(rows);
      })
      .catch((e: any) => {
        // any：fetchJSON 错误形态不固定（AbortError 带 _aborted 标记），
        // 只读 message 展示，沿用本目录既有错误兜底写法。
        if (cancelled || e?._aborted) return;
        if (projectChanged) {
          setError(e?.message || "任务列表加载失败");
          setTasks([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, timelineVersion]);

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
  const directAvailable = isFullTaskId(q, tasks);
  // 行模型：候选 0..filtered.length-1，直达行（若存在）排最后
  const rowCount = filtered.length + (directAvailable ? 1 : 0);

  const pick = (id: string) => {
    openTask(id);
    setOpen(false);
    // 搜索词保留（§9.6：返回任务列表时保留搜索词与筛选），不清空输入
  };

  // 直达 id 规范化：task id 落库为小写 uuid4（services/tasks/crud.py），
  // SQLite 精确匹配大小写敏感 —— 大写粘贴归一化，非 UUID 形态原样保留。
  const directId = () => (FULL_TASK_ID_RE.test(q) ? q.toLowerCase() : q);

  const selectRow = (row: number) => {
    if (row < filtered.length) pick(filtered[row].id);
    else if (directAvailable) pick(directId());
  };

  // 高亮越界钳制（过滤结果变化时保持合法）
  const activeRow = Math.min(Math.max(highlight, 0), Math.max(0, rowCount - 1));

  // 键盘高亮滚动到可视区（jsdom 无 scrollIntoView，运行时能力探测）
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(
      `[data-row="${activeRow}"]`,
    );
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "nearest" });
    }
  }, [activeRow, open]);

  const optionId = (row: number) => `task-picker-opt-${row}`;

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault(); // 防光标移动
      if (!open) {
        setOpen(true);
        return;
      }
      if (rowCount > 0) setHighlight(Math.min(activeRow + 1, rowCount - 1));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      if (rowCount > 0) setHighlight(Math.max(activeRow - 1, 0));
      return;
    }
    if (e.key === "Enter") {
      // SR-03：回车 = 选高亮项；无候选时只有完整 task_id 才直达；
      // 纯标题/残缺 ID 绝不生成伪 ID 跳转（T-09）。
      if (open && rowCount > 0) selectRow(activeRow);
      else if (directAvailable) pick(directId());
      return;
    }
    if (e.key === "Escape") {
      setOpen(false); // 只收起层：不清搜索词（§8.2 Esc 边界）
    }
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
            setHighlight(0);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          role="combobox"
          aria-expanded={open}
          aria-controls="task-picker-listbox"
          aria-activedescendant={open && rowCount > 0 ? optionId(activeRow) : undefined}
          aria-label="搜索任务或按完整 task_id 直达"
          placeholder="搜索任务，或粘贴完整 task_id 直达（含已归档）"
          className="w-full pl-8 pr-8 py-1.5 text-xs rounded-gm border border-g-border bg-g-bg text-g-fg placeholder:text-g-fg-4 focus:border-g-border-focus transition-colors"
        />
        {q && (
          <button
            onClick={() => {
              setQuery("");
              setHighlight(0);
            }}
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
        <div
          ref={listRef}
          id="task-picker-listbox"
          role="listbox"
          aria-label="任务候选"
          className="absolute z-30 left-0 right-0 mt-1 max-h-72 overflow-y-auto rounded-gm border border-g-border bg-g-bg shadow-gm-pop animate-scale-in"
        >
          {filtered.length === 0 && !directAvailable && (
            <div className="px-3 py-4 text-center text-xs text-g-fg-4">
              无匹配任务
            </div>
          )}
          {filtered.map((t, i) => {
            const st = statusStyle(t.status);
            const active = i === activeRow;
            return (
              <button
                key={t.id}
                id={optionId(i)}
                role="option"
                aria-selected={active}
                data-row={i}
                onMouseEnter={() => setHighlight(i)}
                onClick={() => pick(t.id)}
                className={`w-full flex items-center gap-2 px-3 py-2 text-left transition-colors ${
                  active ? "bg-g-bg-soft" : "hover:bg-g-bg-soft"
                }`}
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
          {directAvailable && (
            <button
              id={optionId(filtered.length)}
              role="option"
              aria-selected={filtered.length === activeRow}
              data-row={filtered.length}
              onMouseEnter={() => setHighlight(filtered.length)}
              onClick={() => pick(directId())}
              title="按完整 task_id 直接打开（支持已归档任务）"
              className={`w-full flex items-center gap-2 px-3 py-2 text-left border-t border-g-border transition-colors ${
                filtered.length === activeRow ? "bg-g-bg-soft" : "hover:bg-g-bg-soft"
              }`}
            >
              <svg className="w-3.5 h-3.5 text-g-blue shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7l5 5-5 5M6 12h12" />
              </svg>
              <span className="text-xs text-g-blue truncate">
                直达完整 task_id：{q.slice(0, 36)}
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
            onClick={() => openTask(null)}
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
