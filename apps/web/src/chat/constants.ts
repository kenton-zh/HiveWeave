/**
 * Visual-only motion tokens. Inlined as a <style> tag because index.css
 * is owned by another workstream — keep these scoped with the `hw-` prefix.
 */
export const CHAT_MOTION_CSS = `
@keyframes hw-msg-in {
  from { opacity: 0; transform: translateY(8px) scale(.985); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}
@keyframes hw-dot-bounce {
  0%, 80%, 100% { transform: translateY(0); opacity: .45; }
  40%           { transform: translateY(-4px); opacity: 1; }
}
@keyframes hw-cursor-blink {
  0%, 100% { opacity: .9; }
  50%      { opacity: .15; }
}
@keyframes hw-glow-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(52, 168, 83, .5); }
  70%  { box-shadow: 0 0 0 5px rgba(52, 168, 83, 0); }
  100% { box-shadow: 0 0 0 0 rgba(52, 168, 83, 0); }
}
@keyframes hw-shimmer {
  0%   { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}
@keyframes hw-badge-pop {
  0%   { transform: scale(.5); }
  60%  { transform: scale(1.18); }
  100% { transform: scale(1); }
}
.hw-msg-in { animation: hw-msg-in .28s cubic-bezier(.21, 1.02, .73, 1) both; }
.hw-typing-dot { animation: hw-dot-bounce 1.15s ease-in-out infinite; }
.hw-stream-cursor { animation: hw-cursor-blink 1s ease-in-out infinite; }
.hw-status-live { animation: hw-glow-pulse 1.8s ease-out infinite; }
.hw-thinking-shimmer {
  background: linear-gradient(90deg, #6d7482 25%, #4f46e5 50%, #6d7482 75%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: transparent;
  animation: hw-shimmer 2.2s linear infinite;
}
.hw-badge-pop { animation: hw-badge-pop .32s ease-out both; }
@media (prefers-reduced-motion: reduce) {
  .hw-msg-in, .hw-typing-dot, .hw-stream-cursor,
  .hw-status-live, .hw-thinking-shimmer, .hw-badge-pop {
    animation: none !important;
  }
}
`;

export const roleLabels: Record<string, string> = {
  ceo: "CEO",
  hr: "HR",
  architect: "Architect",
  manager: "Manager",
  developer: "Developer",
  module_dev: "Developer",
  qa: "QA",
  devops: "DevOps",
};

export const toolCategories: Record<string, { color: string; bg: string; label: string }> = {
  dispatch_task: { color: "text-g-blue", bg: "bg-g-blue-vivid/15", label: "Dispatch" },
  write_work_log: { color: "text-g-green", bg: "bg-g-green-vivid/15", label: "Log" },
  read_work_logs: { color: "text-g-green", bg: "bg-g-green-vivid/15", label: "Read Logs" },
  report_completion: { color: "text-g-green", bg: "bg-g-green-vivid/15", label: "Complete" },
  approve_work: { color: "text-g-purple", bg: "bg-g-purple-vivid/15", label: "Approve" },
  reject_work: { color: "text-g-red", bg: "bg-g-red-vivid/15", label: "Reject" },
  review_code: { color: "text-g-purple", bg: "bg-g-purple-vivid/15", label: "Review" },
  read_memory: { color: "text-g-yellow", bg: "bg-g-yellow-vivid/15", label: "Memory" },
  trigger_integration: { color: "text-g-yellow", bg: "bg-g-yellow-vivid/15", label: "Integration" },
  message_superior: { color: "text-g-green", bg: "bg-g-green-vivid/15", label: "Report Up" },
  message_peer: { color: "text-g-blue", bg: "bg-g-blue-vivid/15", label: "Peer Msg" },
  send_message: { color: "text-g-blue", bg: "bg-g-blue-vivid/15", label: "Send" },
  read_agent_status: { color: "text-g-green", bg: "bg-g-green-vivid/15", label: "Status" },
  check_agent_status: { color: "text-g-green", bg: "bg-g-green-vivid/15", label: "Status" },
  list_subordinates: { color: "text-g-blue", bg: "bg-g-blue-vivid/15", label: "Team" },
  create_agent: { color: "text-g-purple", bg: "bg-g-purple-bg/60", label: "Hire" },
  transfer_agent: { color: "text-g-yellow", bg: "bg-g-yellow-vivid/15", label: "Transfer" },
  dismiss_agent: { color: "text-g-red", bg: "bg-g-red-vivid/15", label: "Dismiss" },
  update_roster: { color: "text-g-red", bg: "bg-g-red-vivid/15", label: "Roster" },
  read_roster: { color: "text-g-red", bg: "bg-g-red-vivid/15", label: "View Roster" },
  list_all_agents: { color: "text-g-blue", bg: "bg-g-blue-vivid/15", label: "List All" },
  read_file: { color: "text-g-fg-2", bg: "bg-g-fg-3/15", label: "Read" },
  write_file: { color: "text-g-fg-2", bg: "bg-g-fg-3/15", label: "Write" },
  edit_file: { color: "text-g-fg-2", bg: "bg-g-fg-3/15", label: "Edit" },
  list_files: { color: "text-g-fg-2", bg: "bg-g-fg-3/15", label: "List" },
  search_files: { color: "text-g-fg-2", bg: "bg-g-fg-3/15", label: "Search" },
  delete_file: { color: "text-g-red", bg: "bg-g-red-vivid/15", label: "Delete" },
  glob: { color: "text-g-fg-2", bg: "bg-g-fg-3/15", label: "Glob" },
  fetch_url: { color: "text-g-blue", bg: "bg-g-blue-vivid/15", label: "Fetch" },
  read_charter: { color: "text-g-purple", bg: "bg-g-purple-vivid/15", label: "Charter" },
  save_charter: { color: "text-g-purple", bg: "bg-g-purple-vivid/15", label: "Save Charter" },
};

export const statusLabels: Record<string, { text: string; color: string }> = {
  created: { text: "Created", color: "text-g-fg-3" },
  active: { text: "Active", color: "text-g-green" },
  promoted: { text: "Promoted", color: "text-g-blue" },
  receiving: { text: "Receiving", color: "text-g-yellow" },
  merging: { text: "Merging", color: "text-g-purple" },
  dissolving: { text: "Dissolving", color: "text-g-red" },
  archived: { text: "Archived", color: "text-g-fg-4" },
  idle: { text: "Idle", color: "text-g-fg-3" },
  waiting_human: { text: "等待你验收", color: "text-g-yellow" },
  waiting_agent: { text: "等待同事", color: "text-g-yellow" },
  blocked: { text: "阻塞", color: "text-g-red" },
  complete: { text: "已交付", color: "text-g-blue" },
  runnable: { text: "Idle", color: "text-g-fg-3" },
  working: { text: "Working", color: "text-g-green" },
  error: { text: "Error", color: "text-g-red" },
  waiting: { text: "Waiting", color: "text-g-yellow" },
};

