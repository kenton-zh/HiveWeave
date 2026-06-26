const roleStyles: Record<string, { bg: string; text: string; label: string }> = {
  ceo: { bg: "bg-amber-500/15", text: "text-amber-300", label: "CEO" },
  hr: { bg: "bg-rose-500/15", text: "text-rose-300", label: "HR" },
  architect: { bg: "bg-purple-500/15", text: "text-purple-300", label: "架构师" },
  manager: { bg: "bg-blue-500/15", text: "text-blue-300", label: "经理" },
  developer: { bg: "bg-green-500/15", text: "text-green-300", label: "开发者" },
  module_dev: { bg: "bg-green-500/15", text: "text-green-300", label: "开发者" },
  test_engineer: { bg: "bg-yellow-500/15", text: "text-yellow-300", label: "测试" },
  code_reviewer: { bg: "bg-indigo-500/15", text: "text-indigo-300", label: "审查" },
  security_auditor: { bg: "bg-red-500/15", text: "text-red-300", label: "安全" },
  web_perf_auditor: { bg: "bg-cyan-500/15", text: "text-cyan-300", label: "性能" },
  qa: { bg: "bg-yellow-500/15", text: "text-yellow-300", label: "测试" },
  devops: { bg: "bg-cyan-500/15", text: "text-cyan-300", label: "运维" },
};

const defaultRoleStyle = { bg: "bg-gray-500/15", text: "text-gray-300" };

export function getRoleStyle(role: string) {
  return roleStyles[role] || { ...defaultRoleStyle, label: role };
}

export function getPositionLabel(position?: string, role?: string) {
  if (position) return position;
  if (role) return getRoleStyle(role).label;
  return "";
}
