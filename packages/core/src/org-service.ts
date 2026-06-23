import { agents, modules, workLogs } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import type { Agent } from "@hiveweave/db";
import { registerAgent } from "@hiveweave/db";
import { eq, isNull, like } from "drizzle-orm";
import { randomUUID } from "crypto";

/** Recursive tree node representing an agent in the organization hierarchy. */
export interface OrgNode {
  id: string;
  shortId: string | null;
  name: string;
  role: string;
  status: string;
  permissionType: string;
  permissionMode: string;
  mcpServers: string[];
  boundSkills: string[];
  goal: string;
  moduleId: string | null;
  children: OrgNode[];
}

/**
 * Organization tree CRUD service.
 *
 * Manages the hierarchical agent structure used by HiveWeave,
 * providing read/write operations and tree-building utilities
 * consumed by the frontend React Flow visualisation.
 */
export class OrgService {
  constructor(
    private readonly db: Database,
    private readonly workspacePath?: string,
  ) {}

  /**
   * Generate the next sequential short ID (e.g., A001, A002, A003...).
   * Finds the current maximum and increments.
   */
  private async generateNextShortId(): Promise<string> {
    const rows = await this.db.select({ shortId: agents.shortId }).from(agents);
    let maxNum = 0;
    for (const row of rows) {
      if (row.shortId && /^A\d+$/.test(row.shortId)) {
        const num = parseInt(row.shortId.slice(1), 10);
        if (num > maxNum) maxNum = num;
      }
    }
    return `A${String(maxNum + 1).padStart(3, "0")}`;
  }

  /**
   * Get the full organization tree starting from root agents.
   * Roots are agents whose `parentId` is null.
   */
  async getOrgTree(projectId?: string): Promise<OrgNode[]> {
    const allAgents = projectId
      ? await this.db.select().from(agents).where(eq(agents.projectId, projectId))
      : await this.db.select().from(agents);
    // Exclude archived agents from the org tree
    const activeAgents = allAgents.filter((a: any) => a.status !== "archived");
    const roots = activeAgents.filter(a => !a.parentId);
    return roots.map(r => this.buildTree(r, activeAgents));
  }

  /**
   * Build a tree node recursively from a flat agent list.
   *
   * @param agent     - The current agent record to convert into an OrgNode.
   * @param allAgents - The complete flat list of agent records.
   * @returns A fully-populated OrgNode with nested children.
   */
  private buildTree(agent: any, allAgents: any[]): OrgNode {
    const children = allAgents
      .filter(a => a.parentId === agent.id)
      .map(a => this.buildTree(a, allAgents));

    return {
      id: agent.id,
      shortId: agent.shortId || null,
      name: agent.name,
      role: agent.role,
      status: agent.status,
      permissionType: agent.permissionType,
      permissionMode: agent.permissionMode || "full",
      mcpServers: JSON.parse(agent.mcpServers || "[]"),
      boundSkills: JSON.parse(agent.boundSkills || "[]"),
      goal: agent.goal,
      moduleId: agent.moduleId,
      children,
    };
  }

  /**
   * Create a new agent in the organization.
   *
   * @param input - Agent creation parameters including name, role, goal,
   *                backstory, skills, optional parent/module references,
   *                and the permission type (coordinator or executor).
   * @returns The UUID of the newly created agent.
   */
  async createAgent(input: {
    name: string;
    role: string;
    goal: string;
    backstory: string;
    skills: string[];
    parentId?: string;
    moduleId?: string;
    projectId?: string;
    permissionType: "coordinator" | "executor";
    permissionMode?: string;
    allowedTools?: string[];
    deniedTools?: string[];
    askTools?: string[];
    mcpServers?: string[];
    boundSkills?: string[];
    modelId?: string;
    reasoningEffort?: string;
  }): Promise<string> {
    const id = randomUUID();
    const shortId = await this.generateNextShortId();
    const now = Date.now();

    await this.db.insert(agents).values({
      id,
      shortId,
      name: input.name,
      role: input.role as Agent["role"],
      goal: input.goal,
      backstory: input.backstory,
      skills: JSON.stringify(input.skills),
      parentId: input.parentId || null,
      moduleId: input.moduleId || null,
      projectId: input.projectId || null,
      status: "active",
      permissionType: input.permissionType,
      permissionMode: input.permissionMode || "full",
      allowedTools: JSON.stringify(input.allowedTools || []),
      deniedTools: JSON.stringify(input.deniedTools || []),
      askTools: JSON.stringify(input.askTools || []),
      mcpServers: JSON.stringify(input.mcpServers || []),
      boundSkills: JSON.stringify(input.boundSkills || []),
      modelId: input.modelId || null,
      reasoningEffort: input.reasoningEffort || null,
      createdAt: now,
      updatedAt: now,
    });

    // Register agent in the global registry for cross-project lookup
    if (this.workspacePath) {
      registerAgent(id, this.workspacePath);
    }

    return id;
  }

  /**
   * Get an agent by its ID.
   *
   * @param agentId - UUID of the agent to retrieve.
   * @returns The agent record, or null if not found.
   */
  async getAgent(agentId: string) {
    const result = await this.db.select().from(agents).where(eq(agents.id, agentId));
    return result[0] || null;
  }

  /**
   * Resolve an agent by short ID, full UUID, or UUID prefix.
   * Priority: shortId exact match → UUID exact → UUID prefix.
   *
   * @param agentIdOrShortId - Short ID (e.g. "A007"), full UUID, or UUID prefix.
   * @returns The agent record, or null if not found.
   */
  async resolveAgent(agentIdOrShortId: string) {
    const input = agentIdOrShortId.trim();

    // 1. Try shortId exact match (case-insensitive)
    if (/^[aA]\d{1,4}$/.test(input)) {
      const normalized = "A" + input.slice(1).toUpperCase();
      const byShortId = await this.db.select().from(agents).where(eq(agents.shortId, normalized));
      if (byShortId.length > 0) return byShortId[0];
    }

    // 2. Try UUID exact match
    const exact = await this.getAgent(input);
    if (exact) return exact;

    // 3. Fall back to UUID prefix match
    if (input.length < 36 && input.length >= 6) {
      const matches = await this.db.select().from(agents).where(like(agents.id, `${input}%`));
      if (matches.length === 1) return matches[0];
      if (matches.length > 1) return matches[0];
    }
    return null;
  }

  /**
   * Get all direct children of an agent.
   *
   * @param agentId - UUID of the parent agent.
   * @returns Array of agent records whose parentId matches the given ID.
   */
  async getChildren(agentId: string) {
    const all = await this.db.select().from(agents).where(eq(agents.parentId, agentId));
    // Filter out archived agents — they should not receive tasks or appear as subordinates
    return all.filter((a: any) => a.status !== "archived");
  }

  /**
   * Walk up the parent chain and return every ancestor from root to the
   * immediate parent of the given agent.
   *
   * @param agentId - UUID of the agent whose ancestors are requested.
   * @returns Ordered array of ancestor agent records (root first).
   */
  async getAncestors(agentId: string) {
    const ancestors: any[] = [];
    let current = await this.getAgent(agentId);

    while (current?.parentId) {
      const parent = await this.getAgent(current.parentId);
      if (!parent) break;
      ancestors.unshift(parent);
      current = parent;
    }

    return ancestors;
  }

  /**
   * Update the status field of an agent (e.g. "created" → "active").
   *
   * @param agentId - UUID of the agent to update.
   * @param status  - New status string to persist.
   */
  async updateStatus(agentId: string, status: string) {
    await this.db.update(agents)
      .set({ status: status as Agent["status"], updatedAt: Date.now() })
      .where(eq(agents.id, agentId));
  }

  /**
   * Update the parent of an agent (transfer in the org hierarchy).
   *
   * @param agentId    - UUID of the agent to re-parent.
   * @param newParentId - UUID of the new parent, or null to make root-level.
   */
  async updateParent(agentId: string, newParentId: string | null) {
    await this.db.update(agents)
      .set({ parentId: newParentId, updatedAt: Date.now() })
      .where(eq(agents.id, agentId));
  }

  /**
   * Delete an agent from the organization.
   *
   * Safety rules:
   *   - Refuses deletion if the agent has subordinate children (they must be
   *     removed or reassigned first).
   *   - Work logs are deleted alongside the agent record.
   *   - Memory archival is handled separately by the caller (via MemoryService
   *     .archiveAgentMemories) so that this method stays focused on DB cleanup.
   *
   * @param agentId - UUID of the agent to delete.
   * @returns An object indicating success or failure with a reason.
   */
  async deleteAgent(agentId: string): Promise<{ ok: boolean; reason?: string }> {
    const agent = await this.getAgent(agentId);
    if (!agent) {
      return { ok: false, reason: "Agent not found" };
    }

    // Refuse if the agent has children
    const children = await this.getChildren(agentId);
    if (children.length > 0) {
      return {
        ok: false,
        reason: `Agent has ${children.length} subordinate(s). Remove or reassign them first.`,
      };
    }

    // Delete work logs belonging to this agent
    await this.db.delete(workLogs).where(eq(workLogs.agentId, agentId));

    // Delete the agent record
    await this.db.delete(agents).where(eq(agents.id, agentId));

    return { ok: true };
  }

  /**
   * Update arbitrary fields on an agent record.
   *
   * @param agentId - UUID of the agent to update.
   * @param updates - Key-value pairs of fields to set.
   */
  async updateAgent(agentId: string, updates: Record<string, any>) {
    await this.db.update(agents).set(updates).where(eq(agents.id, agentId));
  }

  /**
   * Get all agents belonging to a project (lightweight projection for lookups).
   * Returns id, shortId, and name only — used by roster display etc.
   */
  async getProjectAgents(projectId: string) {
    return this.db.select({ id: agents.id, shortId: agents.shortId, name: agents.name })
      .from(agents).where(eq(agents.projectId, projectId));
  }

  /**
   * Get all registered modules (used to map agents to their working domain).
   */
  async getModules() {
    return this.db.select().from(modules);
  }

  /** Find the first active agent with the given role in a project. */
  async findAgentByRole(projectId: string, role: string) {
    const all = await this.db.select().from(agents).where(eq(agents.projectId, projectId));
    const normalized = role.toLowerCase();
    return all.find((a: any) => a.status !== "archived" && String(a.role).toLowerCase() === normalized) || null;
  }

}
