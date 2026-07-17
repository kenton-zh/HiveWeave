import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useAppStore } from "../store";

const roleColors: Record<string, { bg: string; text: string; label: string }> = {
  ceo: { bg: "bg-amber-100", text: "text-amber-700", label: "首席执行官" },
  hr: { bg: "bg-rose-100", text: "text-rose-700", label: "人力资源" },
  architect: { bg: "bg-purple-100", text: "text-purple-700", label: "架构师" },
  manager: { bg: "bg-blue-100", text: "text-blue-700", label: "经理" },
  developer: { bg: "bg-green-100", text: "text-green-700", label: "开发者" },
  module_dev: { bg: "bg-green-100", text: "text-green-700", label: "开发者" },
  qa: { bg: "bg-amber-100", text: "text-amber-700", label: "测试" },
  devops: { bg: "bg-cyan-100", text: "text-cyan-700", label: "运维" },
};

/** Generic fallback for unknown/freeform roles — show the raw role name with a neutral style */
const defaultRoleStyle = { bg: "bg-gray-100", text: "text-gray-600" };

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
  const agentDispositions = useAppStore((s) => s.agentDispositions);
  const userPingAgentIds = useAppStore((s) => s.userPingAgentIds);
  const agentHealth = useAppStore((s) => s.agentHealth);
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);

  const isSelected = selectedAgentId === id;
  const role = (data.role as string) || "module_dev";
  const status = (data.status as string) || "idle";
  const name = (data.name as string) || "Agent";
  const position = (data.position as string) || "";
  const displayName = position ? `${position}·${name}` : name;
  const matchedRole = roleColors[role];
  const roleInfo = matchedRole || { ...defaultRoleStyle, label: role.charAt(0).toUpperCase() + role.slice(1) };

  // Runtime: disposition is user-facing; processingAgents is execution-only
  const isProcessing = processingAgents.includes(id);
  const disp = agentDispositions[id] || "";

  let statusColor: string;
  let runtimeLabel = "";
  if (disp === "waiting_human") {
    statusColor = "bg-amber-400";
    runtimeLabel = "等待你验收";
  } else if (disp === "blocked") {
    statusColor = "bg-red-400";
    runtimeLabel = "阻塞";
  } else if (disp === "complete") {
    statusColor = "bg-blue-400";
    runtimeLabel = "已交付";
  } else if (status === "active") {
    statusColor = isProcessing
      ? "bg-emerald-400 animate-pulse"
      : "bg-gray-400";
    runtimeLabel = isProcessing ? "实现中" : "";
  } else {
    statusColor = statusColors[status] || statusColors.idle;
  }

  // Check if this agent has pending approval requests
  const agentApprovals = pendingApprovals[id] || [];
  const hasPendingApprovals = agentApprovals.length > 0;
  const onApprovalClick = data.onApprovalClick as ((agentId: string) => void) | undefined;
  const hasUserPing = userPingAgentIds.includes(id);

  // Health error (LLM/model call failure) — red card + ⚠ icon until an "ok"
  // event clears it. Ignore stale entries from other projects.
  const healthInfo = agentHealth[id];
  const healthError =
    healthInfo &&
    healthInfo.health === "error" &&
    (!healthInfo.projectId || healthInfo.projectId === selectedProjectId)
      ? healthInfo
      : null;

  return (
    <div
      onClick={() => setSelectedAgent(id)}
      className={`
        w-[200px] h-[80px] rounded-gm bg-white border transition-all duration-200
        cursor-pointer flex flex-col justify-center px-4 gap-2 relative
        hover:border-g-blue/50 hover:shadow-gm-md
        ${healthError
          ? "border-red-500 ring-2 ring-red-400/40 shadow-[0_0_12px_rgba(239,68,68,0.35)]"
          : isSelected
            ? "border-g-blue shadow-gm-md ring-1 ring-g-blue/20"
            : "border-g-border shadow-gm-sm"}
      `}
    >
      {/* Target handle (top) */}
      <Handle
        type="target"
        position={Position.Top}
        className="!w-2 !h-2 !bg-g-blue !border-g-bg !border-2"
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
        {healthError && (
          <span
            className="shrink-0 text-red-500 flex items-center"
            title={`模型/LLM 调用出错：${healthError.message || "未知错误"}`}
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
          </span>
        )}
        <span className="text-sm font-medium text-g-fg truncate">
          {displayName}
        </span>
        {runtimeLabel && (
          <span className="text-[10px] text-g-fg-3 truncate shrink-0">
            {runtimeLabel}
          </span>
        )}
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
          className="w-5 h-5 rounded-md bg-g-bg hover:bg-g-blue-bg text-g-fg-3 hover:text-g-blue transition-colors flex items-center justify-center"
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
        className="!w-2 !h-2 !bg-g-blue !border-g-bg !border-2"
      />
    </div>
  );
}

export default memo(AgentNode);
