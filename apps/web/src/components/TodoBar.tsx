import { useEffect, useState } from "react";
import { getAgentTodos, type AgentTodos } from "../api";

interface Props {
  agentId: string;
}

const statusLabels: Record<string, { icon: string; color: string }> = {
  completed: { icon: "✅", color: "text-emerald-400" },
  in_progress: { icon: "🔄", color: "text-amber-400" },
  pending: { icon: "⬜", color: "text-gray-500" },
  cancelled: { icon: "❌", color: "text-red-400/60" },
};

export default function TodoBar({ agentId }: Props) {
  const [todos, setTodos] = useState<AgentTodos | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    let mounted = true;
    async function poll() {
      try {
        const data = await getAgentTodos(agentId);
        if (mounted) setTodos(data);
      } catch { /* ignore */ }
    }
    poll();
    const timer = setInterval(poll, 3000);
    return () => { mounted = false; clearInterval(timer); };
  }, [agentId]);

  if (!todos || todos.todos.length === 0) return null;

  const done = todos.todos.filter((t) => t.status === "completed").length;
  const total = todos.todos.length;

  return (
    <div className="border-b border-surface-border bg-surface-card shrink-0">
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center gap-2 px-6 py-2 text-xs hover:bg-surface/50 transition-colors"
      >
        <span className="text-gray-400">
          {collapsed ? (
            <svg className="w-3 h-3 rotate-180" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          ) : (
            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          )}
        </span>
        <span className="text-gray-400 font-medium">📋 任务清单</span>
        <span className="text-gray-500">{done}/{total} 完成</span>
        {done === total && total > 0 && (
          <span className="text-emerald-400 text-[10px]">全部完成</span>
        )}
      </button>

      {!collapsed && (
        <div className="px-6 pb-2 space-y-0.5">
          {todos.todos.map((todo, i) => {
            const s = statusLabels[todo.status] || statusLabels.pending;
            return (
              <div key={i} className={`flex items-center gap-2 text-xs py-0.5 ${todo.status === "cancelled" ? "line-through opacity-50" : ""}`}>
                <span className="w-4 text-center">{s.icon}</span>
                <span className={s.color}>{todo.content}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
