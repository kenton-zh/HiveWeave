import EmptyState from "./EmptyState";
import { useState, useEffect, useCallback, useRef } from "react";
import { useAppStore } from "../store";
import { getPendingApprovals, respondToApproval, type PendingApproval } from "../api";

interface ApprovalDialogProps {
  agentId: string;
  onClose: () => void;
}

export default function ApprovalDialog({ agentId, onClose }: ApprovalDialogProps) {
  // approvals 的类型是 rest.ts 归一层输出的 PendingApproval（FE-08）：
  // snake_case 真实载荷已在 API 边界归一 + 做过关键字段校验（malformed 标记）。
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [loading, setLoading] = useState(true);
  const [processing, setProcessing] = useState<string | null>(null);
  const [remember, setRemember] = useState(false);
  const [userNote, setUserNote] = useState("");

  const setPendingApprovals = useAppStore((s) => s.setPendingApprovals);
  const removeApproval = useAppStore((s) => s.removeApproval);

  // 入场动效（纯视觉）：遮罩淡入 + 面板滑入
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  const fetchApprovals = useCallback(async () => {
    try {
      const data = await getPendingApprovals(agentId);
      setApprovals(data);
      setPendingApprovals(agentId, data);
    } catch (err) {
      console.error("Failed to fetch approvals:", err);
    } finally {
      setLoading(false);
    }
  }, [agentId, setPendingApprovals]);

  useEffect(() => {
    fetchApprovals();
  }, [fetchApprovals]);

  // Auto-refresh: poll every 10 seconds (P4-1: was 3s) to pick up new requests while dialog is open
  useEffect(() => {
    const timer = setInterval(fetchApprovals, 10000); // P4-1：3s 过密 → 10s（20→6 req/min）
    return () => clearInterval(timer);
  }, [fetchApprovals]);

  // 提交互斥锁（ref 同步判据）：同一时刻只允许一个 respond 在途 ——
  // 防双击/批量并发重复提交（后端幂等兜底 already_resolved，前端不叠加并发）。
  const processingRef = useRef<string | null>(null);

  const handleRespond = async (requestId: string, approved: boolean) => {
    if (processingRef.current !== null) return; // 已有提交在途
    processingRef.current = requestId;
    setProcessing(requestId);
    try {
      const res = await respondToApproval(requestId, approved, remember, userNote || undefined);
      // FE-08：处理成功必须明确确认；未确认成功不移除条目（可重试）
      if (!res?.ok) {
        console.error("Approval respond returned not-ok:", res);
        return;
      }
      removeApproval(requestId);
      setApprovals((prev) => prev.filter((a) => a.id !== requestId));
      setUserNote("");
      setRemember(false);
    } catch (err) {
      console.error("Failed to respond to approval:", err);
    } finally {
      processingRef.current = null;
      setProcessing(null);
    }
  };

  const handleBulkRespond = async (approved: boolean) => {
    // 畸形条目不可操作（缺关键字段，无法安全批准）
    const actionable = approvals.filter((a) => !a.malformed);
    for (const approval of actionable) {
      await handleRespond(approval.id, approved);
    }
  };

  const formatToolArgs = (argsStr: string) => {
    try {
      const args = JSON.parse(argsStr);
      if (Object.keys(args).length === 0) return null;
      return JSON.stringify(args, null, 2);
    } catch {
      return argsStr;
    }
  };

  const formatToolName = (name: string) => {
    return name.replace(/^hiveweave__/, "").replace(/_/g, " ");
  };

  const actionableCount = approvals.filter((a) => !a.malformed).length;

  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-[2px] transition-opacity duration-200 ${entered ? "opacity-100" : "opacity-0"}`}
      onClick={onClose}
    >
      <div
        className={`bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg w-full max-w-lg max-h-[80vh] flex flex-col transform transition-all duration-200 ease-out ${entered ? "opacity-100 translate-y-0 scale-100" : "opacity-0 translate-y-3 scale-[0.98]"}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-g-border">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-g-yellow-bg ring-1 ring-g-yellow/60 rounded-gm flex items-center justify-center">
              <svg className="w-5 h-5 text-g-yellow-vivid" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
              </svg>
            </div>
            <div>
              <h3 className="text-base font-semibold text-g-fg">权限审批请求</h3>
              <p className="text-xs text-g-fg-3">{approvals.length} 个待审批</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-g-fg-3 hover:text-g-fg transition-colors"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {loading ? (
            <div className="flex items-center justify-center py-8">
              <div className="w-6 h-6 border-2 border-g-blue border-t-transparent rounded-full animate-spin" />
            </div>
          ) : approvals.length === 0 ? (
            <EmptyState
              icon={<span className="text-g-green text-2xl leading-none">✓</span>}
              title="暂无待审批的请求"
              description="Agent 发起敏感操作时会出现在这里"
            />
          ) : (
            approvals.map((approval) => {
              // FE-08：缺关键字段的畸形条目 —— 可见占位，不静默丢弃，也不可误批
              if (approval.malformed) {
                return (
                  <div
                    key={approval.id}
                    data-testid="approval-malformed"
                    className="bg-g-bg rounded-gmLg border border-g-red/50 shadow-gm-sm p-4"
                  >
                    <div className="flex items-center gap-2 text-sm font-medium text-g-red-vivid">
                      <svg className="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                      </svg>
                      数据异常：该审批请求缺少关键字段
                    </div>
                    <p className="text-xs text-g-fg-3 mt-1.5">
                      缺失字段：{approval.missingFields?.join("、")}
                      （请求 id：{approval.id}）。本条已保留显示但不可批准/拒绝；
                      刷新后若仍出现，请到该成员的运行日志排查来源。
                    </p>
                  </div>
                );
              }
              const formattedArgs = formatToolArgs(approval.toolArguments);
              return (
                <div
                  key={approval.id}
                  className="bg-g-bg rounded-gmLg border border-g-border shadow-gm-sm hover:shadow-gm transition-shadow p-4"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      {/* 发起成员（§11.3：哪个成员发起） */}
                      <div className="flex items-center gap-1.5 mb-1.5 text-xs min-w-0">
                        <span className="text-g-fg-4 shrink-0">发起成员</span>
                        <span
                          className="font-mono text-g-fg truncate"
                          title={approval.agentId}
                        >
                          {approval.agentId}
                        </span>
                        <span className="text-g-fg-4 shrink-0">
                          · {new Date(approval.createdAt).toLocaleTimeString()}
                        </span>
                      </div>
                      {/* 请求执行的操作（§11.3：请求执行的操作） */}
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-xs text-g-fg-4 shrink-0">请求操作</span>
                        <span
                          className="text-xs font-mono bg-g-blue-bg text-g-blue px-2 py-0.5 rounded-gm truncate"
                          title={approval.toolName}
                        >
                          {formatToolName(approval.toolName)}
                        </span>
                      </div>
                      {/* 说明（§11.3：为什么需要权限） */}
                      {approval.description && (
                        <p className="text-sm text-g-fg mt-2 break-words">
                          <span className="text-xs text-g-fg-4 mr-1.5">说明</span>
                          {approval.description}
                        </p>
                      )}
                      {/* 影响范围（§11.3：影响的文件、目录或资源范围） */}
                      {formattedArgs && (
                        <div className="mt-2">
                          <div className="text-xs text-g-fg-4 mb-1">影响范围（参数）</div>
                          <pre className="text-xs text-g-fg-3 bg-g-bg-soft border border-g-border/60 rounded-gm p-2 overflow-x-auto max-h-32">
                            {formattedArgs}
                          </pre>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Per-request actions：语义明确（仅本次生效 vs 本次不执行） */}
                  <div className="flex items-center gap-2 mt-3 pt-3 border-t border-g-border/60">
                    <button
                      onClick={() => handleRespond(approval.id, true)}
                      disabled={processing !== null}
                      className="flex-1 px-3 py-1.5 text-xs font-medium bg-g-green hover:bg-g-green-vivid disabled:opacity-50 text-white rounded-gm shadow-gm-sm active:scale-[0.97] transition-all"
                    >
                      {processing === approval.id ? "处理中..." : "批准（仅本次）"}
                    </button>
                    <button
                      onClick={() => handleRespond(approval.id, false)}
                      disabled={processing !== null}
                      className="flex-1 px-3 py-1.5 text-xs font-medium bg-g-red hover:bg-g-red-vivid disabled:opacity-50 text-white rounded-gm shadow-gm-sm active:scale-[0.97] transition-all"
                    >
                      {processing === approval.id ? "处理中..." : "拒绝（本次不执行）"}
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Footer with bulk actions and remember option */}
        {approvals.length > 0 && (
          <div className="px-6 py-4 border-t border-g-border space-y-3">
            {/* Remember checkbox（§11.3：记住选择要说明记住到什么范围、在哪撤销） */}
            <label className="flex items-start gap-2 text-sm text-g-fg cursor-pointer">
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
                className="mt-0.5 rounded-gm border-g-border bg-g-bg text-g-blue focus:ring-g-blue/50"
              />
              <span>
                记住此选择（仅随本次操作生效）：该成员<span className="font-medium">同类工具</span>
                之后自动按此处理，写入其权限规则，可在成员的权限规则中撤销
              </span>
            </label>

            {/* Note input */}
            <input
              type="text"
              value={userNote}
              onChange={(e) => setUserNote(e.target.value)}
              placeholder="添加备注（可选）"
              className="w-full px-3 py-2 text-sm bg-g-bg-soft border border-g-border rounded-gm text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue focus:ring-2 focus:ring-g-blue/15 transition-shadow"
            />

            {/* Bulk actions（畸形条目自动排除） */}
            {actionableCount > 1 && (
              <div className="flex items-center gap-2">
                <button
                  onClick={() => handleBulkRespond(true)}
                  disabled={processing !== null}
                  className="flex-1 px-3 py-2 text-sm font-medium bg-g-green hover:bg-g-green-vivid disabled:opacity-50 text-white rounded-gm shadow-gm-sm active:scale-[0.97] transition-all"
                >
                  全部同意 ({actionableCount})
                </button>
                <button
                  onClick={() => handleBulkRespond(false)}
                  disabled={processing !== null}
                  className="flex-1 px-3 py-2 text-sm font-medium bg-g-red hover:bg-g-red-vivid disabled:opacity-50 text-white rounded-gm shadow-gm-sm active:scale-[0.97] transition-all"
                >
                  全部拒绝
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
