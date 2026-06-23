import { agents } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq } from "drizzle-orm";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type PermissionMode = "readonly" | "readwrite" | "full" | "custom";
export type PermissionResult = "allow" | "ask" | "deny";

// ---------------------------------------------------------------------------
// Preset tool lists for each permission mode
// ---------------------------------------------------------------------------

/** readonly: can only read data, no writes */
const READONLY_ALLOWED = [
  "list_subordinates",
  "read_work_logs",
  "read_project_memory",
  "review_code",
];

/** readwrite: can read + write logs, report completions, message others */
const READWRITE_ALLOWED = [
  ...READONLY_ALLOWED,
  "write_work_log",
  "report_completion",
  "message_superior",
  "message_peer",
  "dispatch_task",
  "approve_work",
  "reject_work",
];

/** full: all tools are allowed (default for main agents) — no additional deny/ask rules needed */

// ---------------------------------------------------------------------------
// Glob-like pattern matching for tool rules
// Supports:
//   "Read"           — matches tool name exactly
//   "Bash(npm *)"    — matches tool name + argument prefix
//   "mcp__github__*" — matches tool name prefix
// ---------------------------------------------------------------------------

function matchToolRule(rule: string, toolName: string, toolArgs?: Record<string, any>): boolean {
  // Pattern: "ToolName(argPattern)"
  const parenMatch = rule.match(/^(\w+)\((.+)\)$/);
  if (parenMatch) {
    const [, ruleTool, argPattern] = parenMatch;
    if (ruleTool !== toolName) return false;
    // Match argument pattern against serialized args
    const argStr = toolArgs ? JSON.stringify(toolArgs) : "";
    return globMatch(argPattern, argStr);
  }

  // Pattern with wildcard: "mcp__github__*"
  if (rule.includes("*")) {
    return globMatch(rule, toolName);
  }

  // Exact match
  return rule === toolName;
}

function globMatch(pattern: string, str: string): boolean {
  const escaped = pattern
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*");
  return new RegExp(`^${escaped}$`).test(str);
}

// ---------------------------------------------------------------------------
// PermissionService
// ---------------------------------------------------------------------------

/** Cached agent permission config with TTL */
interface CachedPermConfig {
  mode: PermissionMode;
  allowed: string[];
  denied: string[];
  ask: string[];
  mcpServers: string[];
  boundSkills: string[];
  fetchedAt: number;
}

const PERM_CACHE_TTL = 30_000; // 30 seconds

export class PermissionService {
  private cache = new Map<string, CachedPermConfig>();

  constructor(private readonly db: Database) {}

  /**
   * Load agent permission config from cache or DB.
   * Cached for 30 seconds to avoid repeated DB queries during a single turn.
   */
  private async getAgentConfig(agentId: string): Promise<CachedPermConfig | null> {
    const cached = this.cache.get(agentId);
    if (cached && Date.now() - cached.fetchedAt < PERM_CACHE_TTL) {
      return cached;
    }

    const agentRows = await this.db.select().from(agents).where(eq(agents.id, agentId));
    if (agentRows.length === 0) return null;

    const agent = agentRows[0];
    const config: CachedPermConfig = {
      mode: (agent.permissionMode || "full") as PermissionMode,
      allowed: JSON.parse(agent.allowedTools || "[]"),
      denied: JSON.parse(agent.deniedTools || "[]"),
      ask: JSON.parse(agent.askTools || "[]"),
      mcpServers: JSON.parse(agent.mcpServers || "[]"),
      boundSkills: JSON.parse(agent.boundSkills || "[]"),
      fetchedAt: Date.now(),
    };
    this.cache.set(agentId, config);
    return config;
  }

  /** Invalidate cache for a specific agent (called after rule updates). */
  private invalidateCache(agentId: string) {
    this.cache.delete(agentId);
  }
  /**
   * Check whether a tool invocation is allowed, needs approval, or is denied.
   *
   * Evaluation order (Claude Code-style): deny → ask → allow
   * First match wins. If nothing matches, the mode's default applies.
   *
   * @returns "allow" | "ask" | "deny"
   */
  async checkPermission(
    agentId: string,
    toolName: string,
    toolArgs?: Record<string, any>,
  ): Promise<PermissionResult> {
    const config = await this.getAgentConfig(agentId);
    if (!config) return "deny";

    const mode = config.mode;

    // Strip hiveweave__ prefix for matching against rules
    const cleanTool = toolName.replace(/^hiveweave__/, "");

    // 1. Check deny rules first (highest priority)
    for (const rule of config.denied) {
      if (matchToolRule(rule, cleanTool, toolArgs)) {
        console.log(`[PERM] DENY agent=${agentId.slice(0, 8)} tool=${cleanTool} rule=${rule}`);
        return "deny";
      }
    }

    // 2. Check ask rules
    for (const rule of config.ask) {
      if (matchToolRule(rule, cleanTool, toolArgs)) {
        console.log(`[PERM] ASK agent=${agentId.slice(0, 8)} tool=${cleanTool} rule=${rule}`);
        return "ask";
      }
    }

    // 3. Check allow rules
    for (const rule of config.allowed) {
      if (matchToolRule(rule, cleanTool, toolArgs)) return "allow";
    }

    // 4. Fall back to preset mode defaults
    const fallback = (() => {
      switch (mode) {
        case "readonly":
          return READONLY_ALLOWED.includes(cleanTool) ? "allow" : "deny";
        case "readwrite":
          return READWRITE_ALLOWED.includes(cleanTool) ? "allow" : "deny";
        case "full":
          return "allow";
        case "custom":
          return "deny";
        default:
          return "allow";
      }
    })();

    if (fallback !== "allow") {
      console.log(`[PERM] ${fallback.toUpperCase()} agent=${agentId.slice(0, 8)} tool=${cleanTool} mode=${mode} (fallback)`);
    }
    return fallback as PermissionResult;
  }

  /**
   * Get the effective tool rules for an agent, combining preset mode
   * defaults with any custom overrides.
   */
  async getEffectiveRules(agentId: string) {
    const config = await this.getAgentConfig(agentId);
    if (!config) return null;

    return {
      mode: config.mode,
      allowed: config.allowed,
      denied: config.denied,
      ask: config.ask,
      mcpServers: config.mcpServers,
      boundSkills: config.boundSkills,
    };
  }

  /**
   * Update permission rules for an agent.
   */
  async updateRules(
    agentId: string,
    updates: {
      permissionMode?: PermissionMode;
      allowedTools?: string[];
      deniedTools?: string[];
      askTools?: string[];
      mcpServers?: string[];
      boundSkills?: string[];
    },
  ) {
    const setFields: Record<string, any> = { updatedAt: Date.now() };

    if (updates.permissionMode !== undefined) setFields.permissionMode = updates.permissionMode;
    if (updates.allowedTools !== undefined) setFields.allowedTools = JSON.stringify(updates.allowedTools);
    if (updates.deniedTools !== undefined) setFields.deniedTools = JSON.stringify(updates.deniedTools);
    if (updates.askTools !== undefined) setFields.askTools = JSON.stringify(updates.askTools);
    if (updates.mcpServers !== undefined) setFields.mcpServers = JSON.stringify(updates.mcpServers);
    if (updates.boundSkills !== undefined) setFields.boundSkills = JSON.stringify(updates.boundSkills);

    await this.db.update(agents).set(setFields).where(eq(agents.id, agentId));
    this.invalidateCache(agentId);
  }

  /**
   * Add a permanent allow rule (used when user clicks "remember this choice" on approval).
   */
  async addAllowRule(agentId: string, toolRule: string) {
    const agentRows = await this.db.select().from(agents).where(eq(agents.id, agentId));
    if (agentRows.length === 0) return;

    // Strip hiveweave__ prefix so it matches during checkPermission (which also strips)
    const cleanRule = toolRule.replace(/^hiveweave__/, "");

    const current: string[] = JSON.parse(agentRows[0].allowedTools || "[]");
    if (!current.includes(cleanRule)) {
      current.push(cleanRule);
      await this.db.update(agents)
        .set({ allowedTools: JSON.stringify(current), updatedAt: Date.now() })
        .where(eq(agents.id, agentId));
      this.invalidateCache(agentId);
    }
  }
}
