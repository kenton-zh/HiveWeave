import { useEffect, useState, useRef } from "react";
import { getQuestions, answerQuestion, type PendingQuestion } from "../api";
import { useAppStore } from "../store";
import { getJoinedLobbyChannel } from "../api/ws";

/**
 * UX-05（方案 §11.2「关闭不等于回答」）交互契约：
 *
 * | 操作            | 业务请求              | 后续状态                          |
 * | 关闭 / Esc / 遮罩 | 无（纯 UI 收起）       | 后端仍 pending；可经收起入口/刷新找回 |
 * | 「跳过此问题」     | 是（answerQuestion 固定跳过文案） | 移出弹窗 + toast 说明后果           |
 * | 提交回答（选项/输入）| 是                    | 服务器确认后才移出                   |
 * | 提交/跳过失败     | 无成功请求              | 保留问题与已输入内容，行内错误可重试     |
 *
 * 跳过仍是 answerQuestion 承载（后端没有独立 skip 端点），但只有用户点了
 * 「跳过此问题」才发送 —— 关闭不再偷偷替用户做业务决定。
 */
const SKIP_ANSWER = "[用户暂时跳过了这个问题，请先继续其他工作。如有需要可以稍后重新提问。]";

export default function QuestionDialog() {
  // 服务端当前所有 pending 问题（未过滤）。过滤在渲染期做，关闭/找回才能即时生效。
  const [allPending, setAllPending] = useState<PendingQuestion[]>([]);
  const [customAnswers, setCustomAnswers] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  // 本会话内被「纯关闭」的问题 id。关闭不发请求，后端仍 pending；
  // 该集合只影响本组件的显示，刷新页面即全部找回。
  const closedIdsRef = useRef<Set<string>>(new Set());
  // ref 变更不触发渲染，关闭/找回后 bump 一下（只写不读，纯渲染触发器）。
  const [, setClosedTick] = useState(0);
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);
  const questionVersion = useAppStore((s) => s.questionVersion);
  const showToast = useAppStore((s) => s.showToast);

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
        if (!cancelled) setAllPending(qs);
      } catch { /* best-effort */ }
    })();
    return () => { cancelled = true; };
  }, [questionVersion, selectedProjectId]);

  // Poll for pending questions
  // BUG-005 修复：2s → 5s；P4-1（2026-09-18）：WS `question_asked` 事件
  // 即时刷新（该事件此前在 ws.ts 有 handler 但 onQuestionAsked 无人消费 ——
  // 所以不能只删轮询！），轮询降为 15s 兜底（防 WS 断线漏弹，12→4 req/min）。
  const fetchRef = useRef(async () => {});
  fetchRef.current = async () => {
    try {
      // 只查 pending 状态的问题，避免已答/超时问题反复弹出
      const qs = await getQuestions({ projectId: selectedProjectId || undefined, status: "pending" });
      setAllPending(qs);
    } catch (e) { console.warn("QuestionDialog poll failed:", e); }
  };
  useEffect(() => {
    void fetchRef.current();
    const timer = setInterval(() => void fetchRef.current(), 15000);
    // P4 审计 H-1/H-2：必须绑**已 join** 的 lobby channel 单例（自己
    // channel() 会 new 出未 join 实例，事件被 joinRef 过滤丢弃）；phoenix
    // on() 返回数字 ref，off(event, ref) 按 ref 解绑。单例未就绪时有限次
    // 重试。依赖 selectedProjectId：切项目立即拉取（审计 L-7）。
    let off: (() => void) | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    const bind = () => {
      const lobby = getJoinedLobbyChannel();
      if (!lobby) {
        if (++attempts <= 20) retryTimer = setTimeout(bind, 500);
        return;
      }
      const refAsked = lobby.on("question_asked", () => void fetchRef.current()) as unknown as number;
      // bind(lobby) 同 useLiveStatusPoll：裸调用剥 this ⇒ 读 this.bindings 即 TypeError
      const offByRef = lobby.off.bind(lobby) as unknown as (
        event: string, ref: number
      ) => void;
      off = () => offByRef("question_asked", refAsked);
    };
    bind();
    return () => {
      clearInterval(timer);
      if (retryTimer) clearTimeout(retryTimer);
      off?.();
    };
  }, [selectedProjectId]);

  const [submitting, setSubmitting] = useState(false);

  const visible = allPending.filter((q) => !closedIdsRef.current.has(q.id));
  const recoverable = allPending.filter((q) => closedIdsRef.current.has(q.id));

  /** 纯 UI 关闭（叉/Esc/遮罩共用）：不发送任何请求，问题在后端保持 pending。 */
  const handleCloseDialog = () => {
    for (const q of visible) closedIdsRef.current.add(q.id);
    setClosedTick((t) => t + 1);
  };

  /** 找回被收起的问题：仍 pending 的重新进入弹窗，草稿（customAnswers）保留。 */
  const handleReopenClosed = () => {
    closedIdsRef.current.clear();
    setClosedTick((t) => t + 1);
  };

  /** 明确跳过：发送跳过动作（后端无独立 skip 端点，以固定文案 answerQuestion 承载）。 */
  const handleSkip = async (q: PendingQuestion) => {
    if (submitting) return; // 提交中防重复提交
    setErrors((prev) => ({ ...prev, [q.id]: null }));
    setSubmitting(true);
    try {
      await answerQuestion(q.id, SKIP_ANSWER, q.agentId);
      setAllPending((prev) => prev.filter((p) => p.id !== q.id));
      showToast("已跳过该问题：Agent 会先继续其他工作，如需要可稍后重新提问", "info");
    } catch (e) {
      console.error("skip question failed:", e);
      // 失败不关窗：问题保持待处理，可重试
      setErrors((prev) => ({ ...prev, [q.id]: "跳过失败，请重试（问题保持待处理）" }));
    } finally {
      setSubmitting(false);
    }
  };

  const handleAnswer = async (id: string, answer: string, agentId: string) => {
    if (submitting) return; // 提交中防重复提交
    setErrors((prev) => ({ ...prev, [id]: null }));
    setSubmitting(true);
    try {
      await answerQuestion(id, answer, agentId);
      setAllPending((prev) => prev.filter((p) => p.id !== id));
    } catch (e) {
      console.error("answerQuestion failed:", e);
      // T-11：失败不关窗 —— 保留问题与已输入内容，行内错误 + 可重试
      setErrors((prev) => ({ ...prev, [id]: "回答发送失败，请检查后端连接后重试（草稿已保留）" }));
    } finally {
      setSubmitting(false);
    }
  };

  // Esc 关闭最上层弹窗：纯 UI 关闭（不提交业务操作、不清草稿）。
  // 刻意不设依赖数组：每次渲染重挂监听，闭包始终读到最新的 visible/handler。
  useEffect(() => {
    if (visible.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") handleCloseDialog();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // 无可见问题：若还有被收起但仍 pending 的，给一个找回入口
  if (visible.length === 0) {
    if (recoverable.length === 0) return null;
    return (
      <div className="fixed bottom-5 left-1/2 -translate-x-1/2 z-40" data-testid="question-recover-dock">
        <button
          data-testid="question-reopen"
          onClick={handleReopenClosed}
          className="flex items-center gap-2 px-4 py-2 rounded-full bg-white border border-g-border shadow-gm-md text-xs font-medium text-g-fg-2 hover:text-g-blue hover:border-g-blue/40 transition-all active:scale-[0.97]"
          title="这些问题仍处于待处理状态，点击重新打开（不会丢失已输入内容）"
        >
          <span className="text-sm">📋</span>
          <span>{recoverable.length} 个问题待处理（已收起，点击查看）</span>
        </button>
      </div>
    );
  }

  // Show the first pending question
  const q = visible[0];
  const error = errors[q.id] ?? null;

  return (
    <div
      data-testid="question-overlay"
      className={`fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-[2px] transition-opacity duration-200 ${entered ? "opacity-100" : "opacity-0"}`}
      onClick={(e) => { if (e.target === e.currentTarget) handleCloseDialog(); }}
    >
      <div
        className={`bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg w-[480px] max-h-[80vh] overflow-auto p-6 transform transition-all duration-200 ease-out ${entered ? "opacity-100 translate-y-0 scale-100" : "opacity-0 translate-y-3 scale-[0.98]"}`}
      >
        <div className="flex items-center gap-2 mb-4">
          <span className="text-lg">📋</span>
          <h3 className="text-sm font-semibold text-g-fg flex-1">Agent 需要你的决定</h3>
          <button
            data-testid="question-dialog-close"
            onClick={handleCloseDialog}
            className="text-g-fg-4 hover:text-g-fg transition-colors p-1 rounded-gm hover:bg-g-bg-soft"
            title="关闭（不发送任何回答，问题保持待处理，可稍后在下方入口找回）"
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

        {error && (
          <div
            data-testid="question-error"
            role="alert"
            className="mb-3 px-3 py-2 rounded-gm bg-g-red-bg border border-g-red/30 text-g-red text-xs"
          >
            {error}
          </div>
        )}

        <div className="flex gap-2">
          <button
            data-testid="question-skip"
            onClick={() => handleSkip(q)}
            disabled={submitting}
            className="px-3 py-2 rounded-gm text-sm text-g-fg-3 border border-g-border hover:text-g-fg hover:border-g-border-strong transition-all disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
            title="发送跳过动作：告知 Agent 先跳过此问题继续其他工作（这是明确的业务操作）"
          >
            跳过此问题
          </button>
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
            className="flex-1 min-w-0 px-3 py-2 rounded-gm bg-g-bg-soft border border-g-border text-g-fg placeholder-g-fg-4/70 text-sm focus:outline-none focus:border-g-blue focus:ring-2 focus:ring-g-blue/15 disabled:opacity-50 transition-shadow"
          />
          <button
            onClick={() => {
              if (customAnswers[q.id]?.trim()) handleAnswer(q.id, customAnswers[q.id].trim(), q.agentId);
            }}
            disabled={!customAnswers[q.id]?.trim() || submitting}
            className="px-4 py-2 rounded-gm bg-g-blue text-white text-sm font-medium shadow-gm-sm hover:brightness-110 active:scale-[0.97] disabled:opacity-50 disabled:cursor-not-allowed transition-all"
          >
            发送
          </button>
        </div>
      </div>
    </div>
  );
}
