import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useAppStore } from "../store";

const roleColors: Record<string, { bg: string; text: string; label: string }> = {
  hr: { bg: "bg-rose-500/20", text: "text-rose-300", label: "HR" },
  architect: { bg: "bg-purple-500/20", text: "text-purple-300", label: "Architect" },
  manager: { bg: "bg-blue-500/20", text: "text-blue-300", label: "Manager" },
  developer: { bg: "bg-green-500/20", text: "text-green-300", label: "Developer" },
  module_dev: { bg: "bg-green-500/20", text: "text-green-300", label: "Developer" },
  qa: { bg: "bg-amber-500/20", text: "text-amber-300", label: "QA" },
  devops: { bg: "bg-cyan-500/20", text: "text-cyan-300", label: "DevOps" },
};

/** Generic fallback for unknown/freeform roles — show the raw role name with a neutral style */
const defaultRoleStyle = { bg: "bg-gray-500/20", text: "text-gray-300" };

const statusColors: Record<string, string> = {
  created: "bg-gray-400",
  active: "bg-emerald-400 animate-pulse",
  promoted: "bg-blue-400",
  receiving: "bg-amber-400 animate-pulse",
  merging: "bg-purple-400 animate-pulse",
  dissolving: "bg-red-400",
  archived: "bg-gray-500",
  // Legacy
  idle: "bg-gray-400",
  working: "bg-emerald-400 animate-pulse",
  error: "bg-red-400",
  waiting: "bg-amber-400 animate-pulse",
};

function AgentNode({ data, id }: NodeProps) {
  const selectedAgentId = useAppStore((s) => s.selectedAgentId);
  const setSelectedAgent = useAppStore((s) => s.setSelectedAgent);
  const pendingApprovals = useAppStore((s) => s.pendingApprovals);
  const openAddAgent = useAppStore((s) => s.openAddAgent);
  const processingAgents = useAppStore((s) => s.processingAgents);
  const userPingAgentIds = useAppStore((s) => s.userPingAgentIds);

  const isSelected = selectedAgentId === id;
  const role = (data.role as string) || "module_dev";
  const status = (data.status as string) || "idle";
  const name = (data.name as string) || "Agent";
  const matchedRole = roleColors[role];
  const roleInfo = matchedRole || { ...defaultRoleStyle, label: role.charAt(0).toUpperCase() + role.slice(1) };

  // Runtime status: only "active" agents show working/idle; other lifecycle states keep their color
  const isProcessing = processingAgents.includes(id);
  const statusColor =
    status === "active"
      ? isProcessing
        ? "bg-emerald-400 animate-pulse"
        : "bg-gray-400"
      : statusColors[status] || statusColors.idle;

  // Check if this agent has pending approval requests
  const agentApprovals = pendingApprovals[id] || [];
  const hasPendingApprovals = agentApprovals.length > 0;
  const onApprovalClick = data.onApprovalClick as ((agentId: string) => void) | undefined;
  const hasUserPing = userPingAgentIds.includes(id);

  return (
    <div
      onClick={() => setSelectedAgent(id)}
      className={`
        w-[200px] h-[80px] rounded-xl bg-surface-card border-2 transition-all duration-200
        cursor-pointer flex flex-col justify-center px-4 gap-2 relative
        hover:border-accent/60 hover:shadow-lg hover:shadow-accent/5
        ${isSelected ? "border-accent shadow-lg shadow-accent/10" : "border-surface-border"}
      `}
    >
      {/* Target handle (top) */}
      <Handle
        type="target"
        position={Position.Top}
        className="!w-2 !h-2 !bg-accent !border-surface-card !border-2"
      />

      {/* User ping indicator (top-left) */}
      {hasUserPing && (
        <div
          className="absolute -top-2 -left-2 w-5 h-5 bg-red-500 rounded-full flex items-center justify-center shadow-lg animate-pulse z-10"
          title="该 Agent 有消息给你"
        >
          <span className="text-[10px] text-white font-bold">!</span>
        </div>
      )}

      {/* Pending approval indicator (top-right) */}
      {hasPendingApprovals && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            if (onApprovalClick) {
              onApprovalClick(id);
            } else {
              setSelectedAgent(id);
            }
          }}
          className="absolute -top-2 -right-2 w-6 h-6 bg-amber-500 rounded-full flex items-center justify-center shadow-lg hover:bg-amber-400 transition-colors z-10"
          title={`${agentApprovals.length} 个待审批请求`}
        >
          <svg className="w-3.5 h-3.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
          </svg>
          {agentApprovals.length > 1 && (
            <span className="absolute -bottom-1 -right-1 w-4 h-4 bg-red-500 rounded-full text-[9px] text-white font-bold flex items-center justify-center">
              {agentApprovals.length}
            </span>
          )}
        </button>
      )}

      {/* Agent Name + Status */}
      <div className="flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full shrink-0 ${statusColor}`} />
        <span className="text-sm font-medium text-gray-100 truncate">
          {name}
        </span>
      </div>

      {/* Role Badge */}
      <div className="flex items-center justify-between">
        <span
          className={`
            inline-block text-[10px] font-medium px-2 py-0.5 rounded-full
            ${roleInfo.bg} ${roleInfo.text}
          `}
        >
          {roleInfo.label}
        </span>
        {/* Add child agent button */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            openAddAgent(id);
          }}
          className="w-5 h-5 rounded-md bg-surface hover:bg-accent/20 text-gray-500 hover:text-accent transition-colors flex items-center justify-center"
          title="创建子 Agent"
        >
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
          </svg>
        </button>
      </div>

      {/* Source handle (bottom) */}
      <Handle
        type="source"
        position={Position.Bottom}
        className="!w-2 !h-2 !bg-accent !border-surface-card !border-2"
      />
    </div>
  );
}

export default memo(AgentNode);
