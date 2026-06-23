import { useState, useEffect, useCallback } from "react";
import { useAppStore } from "../store";
import { getPendingApprovals, respondToApproval } from "../api";

interface ApprovalDialogProps {
  agentId: string;
  onClose: () => void;
}

interface PendingApproval {
  id: string;
  agentId: string;
  toolName: string;
  toolArguments: string;
  description: string;
  status: string;
  createdAt: number;
}

export default function ApprovalDialog({ agentId, onClose }: ApprovalDialogProps) {
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [loading, setLoading] = useState(true);
  const [processing, setProcessing] = useState<string | null>(null);
  const [remember, setRemember] = useState(false);
  const [userNote, setUserNote] = useState("");

  const setPendingApprovals = useAppStore((s) => s.setPendingApprovals);
  const removeApproval = useAppStore((s) => s.removeApproval);

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

  // Auto-refresh: poll every 3 seconds to pick up new requests while dialog is open
  useEffect(() => {
    const timer = setInterval(fetchApprovals, 3000);
    return () => clearInterval(timer);
  }, [fetchApprovals]);

  const handleRespond = async (requestId: string, approved: boolean) => {
    setProcessing(requestId);
    try {
      await respondToApproval(requestId, approved, remember, userNote || undefined);
      removeApproval(requestId);
      setApprovals((prev) => prev.filter((a) => a.id !== requestId));
      setUserNote("");
      setRemember(false);
    } catch (err) {
      console.error("Failed to respond to approval:", err);
    } finally {
      setProcessing(null);
    }
  };

  const handleBulkRespond = async (approved: boolean) => {
    for (const approval of approvals) {
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

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="bg-surface-card border border-surface-border rounded-xl shadow-2xl w-full max-w-lg max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-border">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-amber-500/20 rounded-lg flex items-center justify-center">
              <svg className="w-5 h-5 text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
              </svg>
            </div>
            <div>
              <h3 className="text-base font-semibold text-gray-100">权限审批请求</h3>
              <p className="text-xs text-gray-400">{approvals.length} 个待审批</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-200 transition-colors"
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
              <div className="w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
            </div>
          ) : approvals.length === 0 ? (
            <div className="text-center py-8 text-gray-400">
              <p>暂无待审批的请求</p>
            </div>
          ) : (
            approvals.map((approval) => {
              const formattedArgs = formatToolArgs(approval.toolArguments);
              return (
                <div
                  key={approval.id}
                  className="bg-surface rounded-lg border border-surface-border p-4"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xs font-mono bg-blue-500/20 text-blue-300 px-2 py-0.5 rounded">
                          {formatToolName(approval.toolName)}
                        </span>
                        <span className="text-[10px] text-gray-500">
                          {new Date(approval.createdAt).toLocaleTimeString()}
                        </span>
                      </div>
                      {approval.description && (
                        <p className="text-sm text-gray-300 mt-2">{approval.description}</p>
                      )}
                      {formattedArgs && (
                        <pre className="text-xs text-gray-400 bg-surface-card rounded p-2 mt-2 overflow-x-auto max-h-32">
                          {formattedArgs}
                        </pre>
                      )}
                    </div>
                  </div>

                  {/* Per-request actions */}
                  <div className="flex items-center gap-2 mt-3 pt-3 border-t border-surface-border">
                    <button
                      onClick={() => handleRespond(approval.id, true)}
                      disabled={processing === approval.id}
                      className="flex-1 px-3 py-1.5 text-xs font-medium bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white rounded-md transition-colors"
                    >
                      {processing === approval.id ? "处理中..." : "同意"}
                    </button>
                    <button
                      onClick={() => handleRespond(approval.id, false)}
                      disabled={processing === approval.id}
                      className="flex-1 px-3 py-1.5 text-xs font-medium bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white rounded-md transition-colors"
                    >
                      {processing === approval.id ? "处理中..." : "拒绝"}
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Footer with bulk actions and remember option */}
        {approvals.length > 0 && (
          <div className="px-6 py-4 border-t border-surface-border space-y-3">
            {/* Remember checkbox */}
            <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
                className="rounded border-surface-border bg-surface text-accent focus:ring-accent/50"
              />
              <span>记住此选择（以后同类操作自动允许）</span>
            </label>

            {/* Note input */}
            <input
              type="text"
              value={userNote}
              onChange={(e) => setUserNote(e.target.value)}
              placeholder="添加备注（可选）"
              className="w-full px-3 py-2 text-sm bg-surface border border-surface-border rounded-md text-gray-200 placeholder-gray-500 focus:outline-none focus:border-accent"
            />

            {/* Bulk actions */}
            {approvals.length > 1 && (
              <div className="flex items-center gap-2">
                <button
                  onClick={() => handleBulkRespond(true)}
                  disabled={processing !== null}
                  className="flex-1 px-3 py-2 text-sm font-medium bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white rounded-md transition-colors"
                >
                  全部同意 ({approvals.length})
                </button>
                <button
                  onClick={() => handleBulkRespond(false)}
                  disabled={processing !== null}
                  className="flex-1 px-3 py-2 text-sm font-medium bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white rounded-md transition-colors"
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
