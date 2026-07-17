import { useEffect, useState, useRef } from "react";
import { getQuestions, answerQuestion, type PendingQuestion } from "../api";
import { useAppStore } from "../store";

export default function QuestionDialog() {
  const [questions, setQuestions] = useState<PendingQuestion[]>([]);
  const [customAnswers, setCustomAnswers] = useState<Record<string, string>>({});
  const dismissedRef = useRef<Set<string>>(new Set());
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);
  const questionVersion = useAppStore((s) => s.questionVersion);

  // 入场动效（纯视觉）：遮罩淡入 + 面板滑入
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  // 立即拉取一次 pending questions（WebSocket question_asked 事件触发）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const qs = await getQuestions({ projectId: selectedProjectId || undefined, status: "pending" });
        if (!cancelled) {
          const visible = qs.filter((q) => !dismissedRef.current.has(q.id));
          setQuestions(visible);
        }
      } catch { /* best-effort */ }
    })();
    return () => { cancelled = true; };
  }, [questionVersion, selectedProjectId]);

  // Poll for pending questions
  // BUG-005 修复：2s → 5s，减少 polling 频率（30→12 req/min）
  useEffect(() => {
    const timer = setInterval(async () => {
      try {
        // 只查 pending 状态的问题，避免已答/超时问题反复弹出
        const qs = await getQuestions({ projectId: selectedProjectId || undefined, status: "pending" });
        // Filter out locally dismissed questions; always sync (clear when server has none)
        const visible = qs.filter((q) => !dismissedRef.current.has(q.id));
        setQuestions(visible);
      } catch (e) { console.warn("QuestionDialog poll failed:", e); }
    }, 5000);
    return () => clearInterval(timer);
  }, [selectedProjectId]);

  const [submitting, setSubmitting] = useState(false);

  const handleAnswer = async (id: string, answer: string, agentId: string) => {
    setSubmitting(true);
    try {
      await answerQuestion(id, answer, agentId);
      setQuestions((prev) => prev.filter((q) => q.id !== id));
    } catch (e) {
      console.error("answerQuestion failed:", e);
      alert("回答发送失败，请检查后端连接后重试");
    } finally {
      setSubmitting(false);
    }
  };

  const handleDismiss = async (id: string, agentId: string) => {
    dismissedRef.current.add(id);
    setQuestions((prev) => prev.filter((q) => q.id !== id));
    try {
      await answerQuestion(id, "[用户暂时跳过了这个问题，请先继续其他工作。如有需要可以稍后重新提问。]", agentId);
    } catch { /* best-effort */ }
  };

  if (questions.length === 0) return null;

  // Show the first pending question
  const q = questions[0];

  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-[2px] transition-opacity duration-200 ${entered ? "opacity-100" : "opacity-0"}`}
      onClick={(e) => { if (e.target === e.currentTarget) handleDismiss(q.id, q.agentId); }}
    >
      <div
        className={`bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg w-[480px] max-h-[80vh] overflow-auto p-6 transform transition-all duration-200 ease-out ${entered ? "opacity-100 translate-y-0 scale-100" : "opacity-0 translate-y-3 scale-[0.98]"}`}
      >
        <div className="flex items-center gap-2 mb-4">
          <span className="text-lg">📋</span>
          <h3 className="text-sm font-semibold text-g-fg flex-1">Agent 需要你的决定</h3>
          <button
            onClick={() => handleDismiss(q.id, q.agentId)}
            disabled={submitting}
            className="text-g-fg-4 hover:text-g-fg transition-colors p-1 rounded hover:bg-g-bg-soft disabled:opacity-50"
            title="暂时忽略"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <p className="text-g-fg text-base mb-6 whitespace-pre-wrap">{q.question}</p>

        {q.options && q.options.length > 0 && (
          <div className="space-y-2 mb-4">
            {q.options.map((opt, i) => {
              const label = typeof opt === "string" ? opt : (opt as any)?.label ?? String(opt);
              const desc = typeof opt === "object" && opt !== null ? (opt as any)?.description : undefined;
              return (
              <button
                key={i}
                onClick={() => handleAnswer(q.id, label, q.agentId)}
                disabled={submitting}
                className="w-full text-left px-4 py-3 rounded-gm bg-g-bg border border-g-border hover:border-g-blue/50 hover:bg-g-blue-bg/40 hover:shadow-gm-sm active:scale-[0.99] transition-all disabled:opacity-50"
              >
                <div className="text-sm font-medium text-g-fg">{label}</div>
                {desc && <div className="text-xs text-g-fg-4 mt-0.5">{desc}</div>}
              </button>
              );
            })}
          </div>
        )}

        <div className="flex gap-2">
          <input
            type="text"
            placeholder="或输入自定义回答..."
            value={customAnswers[q.id] || ""}
            onChange={(e) => setCustomAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))}
            onKeyDown={(e) => {
              if (e.key === "Enter" && customAnswers[q.id]?.trim() && !submitting) {
                handleAnswer(q.id, customAnswers[q.id].trim(), q.agentId);
              }
            }}
            disabled={submitting}
            className="flex-1 px-3 py-2 rounded-gm bg-g-bg-soft border border-g-border text-g-fg placeholder-g-fg-4/70 text-sm focus:outline-none focus:border-g-blue focus:ring-2 focus:ring-g-blue/15 disabled:opacity-50 transition-shadow"
          />
          <button
            onClick={() => {
              if (customAnswers[q.id]?.trim()) handleAnswer(q.id, customAnswers[q.id].trim(), q.agentId);
            }}
            disabled={!customAnswers[q.id]?.trim() || submitting}
            className="px-4 py-2 rounded-gm bg-g-blue text-white text-sm font-medium shadow-gm-sm hover:bg-blue-600 active:scale-[0.97] disabled:opacity-50 disabled:cursor-not-allowed transition-all"
          >
            发送
          </button>
        </div>
      </div>
    </div>
  );
}
