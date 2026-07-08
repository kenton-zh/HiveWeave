const roleStyles: Record<string, { bg: string; text: string; label: string }> = {
  ceo: { bg: "bg-g-yellow-bg", text: "text-amber-700", label: "CEO" },
  hr: { bg: "bg-g-red-bg", text: "text-g-red", label: "HR" },
  architect: { bg: "bg-purple-50", text: "text-purple-700", label: "架构师" },
  manager: { bg: "bg-g-blue-bg", text: "text-g-blue", label: "经理" },
  developer: { bg: "bg-g-green-bg", text: "text-g-green", label: "开发者" },
  module_dev: { bg: "bg-g-green-bg", text: "text-g-green", label: "开发者" },
  test_engineer: { bg: "bg-g-yellow-bg", text: "text-amber-600", label: "测试" },
  code_reviewer: { bg: "bg-indigo-50", text: "text-indigo-700", label: "审查" },
  security_auditor: { bg: "bg-g-red-bg", text: "text-g-red", label: "安全" },
  web_perf_auditor: { bg: "bg-cyan-50", text: "text-cyan-700", label: "性能" },
  qa: { bg: "bg-g-yellow-bg", text: "text-amber-600", label: "测试" },
  devops: { bg: "bg-cyan-50", text: "text-cyan-700", label: "运维" },
};

const defaultRoleStyle = { bg: "bg-g-bg-muted", text: "text-g-fg-3" };

export function getRoleStyle(role: string) {
  return roleStyles[role] || { ...defaultRoleStyle, label: role };
}

export function getPositionLabel(position?: string, role?: string) {
  if (position) return position;
  if (role) return getRoleStyle(role).label;
  return "";
}
