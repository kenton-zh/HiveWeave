import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useAppStore } from "../store";

const roleColors: Record<string, { bg: string; text: string; label: string }> = {
  architect: { bg: "bg-purple-500/20", text: "text-purple-300", label: "Architect" },
  manager: { bg: "bg-blue-500/20", text: "text-blue-300", label: "Manager" },
  module_dev: { bg: "bg-green-500/20", text: "text-green-300", label: "Developer" },
  qa: { bg: "bg-amber-500/20", text: "text-amber-300", label: "QA" },
  devops: { bg: "bg-cyan-500/20", text: "text-cyan-300", label: "DevOps" },
};

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

  const isSelected = selectedAgentId === id;
  const role = (data.role as string) || "module_dev";
  const status = (data.status as string) || "idle";
  const name = (data.name as string) || "Agent";
  const roleInfo = roleColors[role] || roleColors.module_dev;
  const statusColor = statusColors[status] || statusColors.idle;

  return (
    <div
      onClick={() => setSelectedAgent(id)}
      className={`
        nodrag
        w-[200px] h-[80px] rounded-xl bg-surface-card border-2 transition-all duration-200
        cursor-pointer flex flex-col justify-center px-4 gap-2
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

      {/* Agent Name + Status */}
      <div className="flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full shrink-0 ${statusColor}`} />
        <span className="text-sm font-medium text-gray-100 truncate">
          {name}
        </span>
      </div>

      {/* Role Badge */}
      <div>
        <span
          className={`
            inline-block text-[10px] font-medium px-2 py-0.5 rounded-full
            ${roleInfo.bg} ${roleInfo.text}
          `}
        >
          {roleInfo.label}
        </span>
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
