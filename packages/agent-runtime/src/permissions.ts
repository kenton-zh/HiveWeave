/**
 * HiveWeave Permission Matrix
 *
 * Defines tool access boundaries for different agent permission levels:
 * - Coordinator: read-only + dispatch/review (cannot write code)
 * - Executor: read + write code + run commands (cannot spawn sub-agents)
 *
 * This enforces the principle of least privilege across the agent hierarchy.
 */

// ---------------------------------------------------------------------------
// Built-in Claude Code SDK tools
// ---------------------------------------------------------------------------

/** Coordinator tools — read-only + dispatch/review */
export const COORDINATOR_TOOLS = [
  "Read",
  "Grep",
  "Glob",
] as const;

/** Executor tools — read + write code + run commands */
export const EXECUTOR_TOOLS = [
  "Read",
  "Edit",
  "Bash",
  "Grep",
  "Glob",
] as const;

// ---------------------------------------------------------------------------
// HiveWeave custom MCP tools
// ---------------------------------------------------------------------------

/** Custom MCP tools available to coordinators */
export const HIVWEAVE_COORDINATOR_MCP_TOOLS = [
  "hiveweave__dispatch_task",
  "hiveweave__read_work_logs",
  "hiveweave__review_code",
  "hiveweave__approve_work",
  "hiveweave__reject_work",
  "hiveweave__trigger_integration",
] as const;

/** Custom MCP tools available to executors */
export const HIVWEAVE_EXECUTOR_MCP_TOOLS = [
  "hiveweave__write_work_log",
  "hiveweave__report_completion",
  "hiveweave__read_project_memory",
] as const;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type CoordinatorTool = (typeof COORDINATOR_TOOLS)[number];
export type ExecutorTool = (typeof EXECUTOR_TOOLS)[number];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Get the full list of allowed tools for a given permission type.
 * Combines built-in Claude Code SDK tools with HiveWeave MCP tools.
 */
export function getToolsForPermissionType(
  permissionType: "coordinator" | "executor",
): string[] {
  if (permissionType === "coordinator") {
    return [...COORDINATOR_TOOLS, ...HIVWEAVE_COORDINATOR_MCP_TOOLS];
  }
  return [...EXECUTOR_TOOLS, ...HIVWEAVE_EXECUTOR_MCP_TOOLS];
}
