import { useMemo } from "react";
import {
  useQueueStore,
  entriesForAgent,
  normalizeProjectId,
  type DeliveryState,
  type PendingMessage,
} from "./queueStore";

/**
 * 待发消息队列面板（§8.3/§8.5）：内容 + 接收人 + 状态可见，可取消 / 失败可重试。
 * 数据唯一来自模块级 queueStore（projectId+agentId 键控），组件重挂不丢；
 * 「仅本地暂存(queued-local)」与「服务器已接收(accepted)」必须视觉可分。
 */

const STATE_META: Record<DeliveryState, { label: string; chip: string }> = {
  "queued-local": {
    label: "仅本地暂存",
    chip: "bg-g-yellow-bg text-g-yellow border-g-yellow/50",
  },
  sending: {
    label: "发送中",
    chip: "bg-g-blue-bg text-g-blue border-g-blue/50",
  },
  accepted: {
    label: "服务器已接收",
    chip: "bg-g-green-bg text-g-green border-g-green/50",
  },
  failed: {
    label: "发送失败",
    chip: "bg-g-red-bg text-g-red border-g-red/50",
  },
};

function StateChip({ state }: { state: DeliveryState }) {
  const meta = STATE_META[state];
  return (
    <span
      data-testid="queue-state-chip"
      className={`shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-gm border leading-none ${meta.chip}`}
    >
      {meta.label}
    </span>
  );
}

function QueueRow({
  msg,
  agentName,
  onRetry,
  onCancel,
}: {
  msg: PendingMessage;
  agentName?: string;
  onRetry: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  const preview =
    msg.text.trim() ||
    (msg.attachments.length ? `（图片 ×${msg.attachments.length}）` : "（空消息）");
  const cancellable = msg.state === "queued-local" || msg.state === "accepted" || msg.state === "failed";
  return (
    <li
      data-testid="queue-item"
      className="flex items-start gap-2 px-1 py-1.5 rounded-gm hover:bg-g-bg-muted/60 transition-colors"
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5 flex-wrap">
          {msg.mode === "interrupt" && (
            <span className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-gm bg-g-purple/10 text-g-purple border border-g-purple/30 leading-none">
              插话
            </span>
          )}
          <span className="text-xs text-g-fg-3 shrink-0">
            → {agentName || msg.agentId.slice(0, 8) + "…"}
          </span>
          <StateChip state={msg.state} />
        </div>
        <p className="text-xs text-g-fg-2 mt-0.5 line-clamp-2 whitespace-pre-wrap break-words">
          {preview}
        </p>
        {msg.state === "failed" && msg.errorMessage && (
          <p className="text-[11px] text-g-red mt-0.5">{msg.errorMessage}</p>
        )}
      </div>
      <div className="shrink-0 flex items-center gap-1 pt-0.5">
        {msg.state === "failed" && (
          <button
            data-testid="queue-retry"
            onClick={() => onRetry(msg.clientMessageId)}
            className="text-[11px] px-2 py-1 rounded-gm border border-g-blue/40 text-g-blue hover:bg-g-blue/10 transition-colors"
            title="重新发送这条消息"
          >
            重试
          </button>
        )}
        {cancellable && (
          <button
            data-testid="queue-cancel"
            onClick={() => onCancel(msg.clientMessageId)}
            className="text-[11px] px-2 py-1 rounded-gm border border-g-border text-g-fg-3 hover:text-g-red hover:border-g-red/40 transition-colors"
            title={
              msg.state === "accepted"
                ? "仅移除这条记录，不会撤回已送达的消息"
                : "取消这条未发送的消息"
            }
          >
            {msg.state === "accepted" ? "移除记录" : "取消"}
          </button>
        )}
      </div>
    </li>
  );
}

export function PendingQueuePanel({
  projectId,
  agentId,
  agentName,
  onRetry,
  onCancel,
}: {
  projectId: string | null;
  agentId: string | null;
  agentName?: string;
  onRetry: (clientMessageId: string) => void;
  onCancel: (clientMessageId: string) => void;
}) {
  const entries = useQueueStore((s) => s.entries);
  const pid = normalizeProjectId(projectId);
  const list = useMemo(
    () => entriesForAgent(entries, pid, agentId),
    [entries, pid, agentId]
  );
  if (!agentId || list.length === 0) return null;

  const pendingCount = list.filter((m) => m.state === "queued-local").length;

  return (
    <div
      data-testid="pending-queue-panel"
      className="mb-2 rounded-gmLg border border-g-border bg-g-bg-soft px-3 py-2 hw-msg-in"
    >
      <div className="flex items-center gap-1.5 text-[11px] font-semibold text-g-fg-3 uppercase tracking-wide">
        <svg className="w-3 h-3 text-g-yellow-vivid" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l2 2m6-2a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        待发消息
        {pendingCount > 0 && (
          <span className="font-normal normal-case tracking-normal text-g-fg-4">
            {pendingCount} 条将在当前回复完成后自动发送
          </span>
        )}
      </div>
      <ul className="mt-1 space-y-0.5">
        {list.map((m) => (
          <QueueRow key={m.clientMessageId} msg={m} agentName={agentName} onRetry={onRetry} onCancel={onCancel} />
        ))}
      </ul>
    </div>
  );
}

export default PendingQueuePanel;
