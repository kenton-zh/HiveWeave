import { useEffect, useState, useRef } from "react";
import { getQuestions, answerQuestion, type PendingQuestion } from "../api";
import { useAppStore } from "../store";

export default function QuestionDialog() {
  const [questions, setQuestions] = useState<PendingQuestion[]>([]);
  const [customAnswers, setCustomAnswers] = useState<Record<string, string>>({});
  const dismissedRef = useRef<Set<string>>(new Set());

  // Poll for pending questions
  useEffect(() => {
    const timer = setInterval(async () => {
      try {
        const qs = await getQuestions();
        // Filter out locally dismissed questions; always sync (clear when server has none)
        const visible = qs.filter((q) => !dismissedRef.current.has(q.id));
        setQuestions(visible);
      } catch { /* ignore poll errors */ }
    }, 2000);
    return () => clearInterval(timer);
  }, []);

  const handleAnswer = async (id: string, answer: string) => {
    await answerQuestion(id, answer);
    setQuestions((prev) => prev.filter((q) => q.id !== id));
  };

  const handleDismiss = async (id: string) => {
    dismissedRef.current.add(id);
    setQuestions((prev) => prev.filter((q) => q.id !== id));
    // Notify the server so the agent's question tool call resolves immediately.
    // Without this, the agent hangs until the 10-minute server-side timeout.
    try {
      await answerQuestion(id, "[用户暂时跳过了这个问题，请先继续其他工作。如有需要可以稍后重新提问。]");
    } catch { /* best-effort */ }
  };

  if (questions.length === 0) return null;

  // Show the first pending question
  const q = questions[0];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={(e) => { if (e.target === e.currentTarget) handleDismiss(q.id); }}>
      <div className="bg-surface-card border border-surface-border rounded-xl shadow-2xl w-[480px] max-h-[80vh] overflow-auto p-6">
        <div className="flex items-center gap-2 mb-4">
          <span className="text-lg">📋</span>
          <h3 className="text-sm font-semibold text-gray-300 flex-1">Agent 需要你的决定</h3>
          <button
            onClick={() => handleDismiss(q.id)}
            className="text-gray-500 hover:text-gray-300 transition-colors p-1 rounded hover:bg-white/10"
            title="暂时忽略"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <p className="text-gray-100 text-base mb-6 whitespace-pre-wrap">{q.question}</p>

        {q.options && q.options.length > 0 && (
          <div className="space-y-2 mb-4">
            {q.options.map((opt, i) => {
              const label = typeof opt === "string" ? opt : (opt as any)?.label ?? String(opt);
              const desc = typeof opt === "object" && opt !== null ? (opt as any)?.description : undefined;
              return (
              <button
                key={i}
                onClick={() => handleAnswer(q.id, label)}
                className="w-full text-left px-4 py-3 rounded-lg bg-surface border border-surface-border hover:border-accent hover:bg-accent/10 transition-colors"
              >
                <div className="text-sm font-medium text-gray-200">{label}</div>
                {desc && <div className="text-xs text-gray-500 mt-0.5">{desc}</div>}
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
              if (e.key === "Enter" && customAnswers[q.id]?.trim()) {
                handleAnswer(q.id, customAnswers[q.id].trim());
              }
            }}
            className="flex-1 px-3 py-2 rounded-lg bg-surface border border-surface-border text-gray-200 text-sm focus:outline-none focus:border-accent"
          />
          <button
            onClick={() => {
              if (customAnswers[q.id]?.trim()) handleAnswer(q.id, customAnswers[q.id].trim());
            }}
            disabled={!customAnswers[q.id]?.trim()}
            className="px-4 py-2 rounded-lg bg-accent text-white text-sm font-medium hover:bg-accent/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            发送
          </button>
        </div>
      </div>
    </div>
  );
}
