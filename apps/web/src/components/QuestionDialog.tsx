import { useEffect, useState } from "react";
import { getQuestions, answerQuestion, type PendingQuestion } from "../api";
import { useAppStore } from "../store";

export default function QuestionDialog() {
  const [questions, setQuestions] = useState<PendingQuestion[]>([]);
  const [customAnswers, setCustomAnswers] = useState<Record<string, string>>({});

  // Poll for pending questions
  useEffect(() => {
    const timer = setInterval(async () => {
      try {
        const qs = await getQuestions();
        if (qs.length > 0) setQuestions(qs);
      } catch { /* ignore poll errors */ }
    }, 2000);
    return () => clearInterval(timer);
  }, []);

  const handleAnswer = async (id: string, answer: string) => {
    await answerQuestion(id, answer);
    setQuestions((prev) => prev.filter((q) => q.id !== id));
  };

  if (questions.length === 0) return null;

  // Show the first pending question
  const q = questions[0];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={(e) => e.target === e.currentTarget && undefined}>
      <div className="bg-surface-card border border-surface-border rounded-xl shadow-2xl w-[480px] max-h-[80vh] overflow-auto p-6">
        <div className="flex items-center gap-2 mb-4">
          <span className="text-lg">📋</span>
          <h3 className="text-sm font-semibold text-gray-300">Agent 需要你的决定</h3>
        </div>

        <p className="text-gray-100 text-base mb-6 whitespace-pre-wrap">{q.question}</p>

        {q.options && q.options.length > 0 && (
          <div className="space-y-2 mb-4">
            {q.options.map((opt, i) => (
              <button
                key={i}
                onClick={() => handleAnswer(q.id, opt.label)}
                className="w-full text-left px-4 py-3 rounded-lg bg-surface border border-surface-border hover:border-accent hover:bg-accent/10 transition-colors"
              >
                <div className="text-sm font-medium text-gray-200">{opt.label}</div>
                {opt.description && <div className="text-xs text-gray-500 mt-0.5">{opt.description}</div>}
              </button>
            ))}
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
