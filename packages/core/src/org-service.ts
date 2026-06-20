import { db } from "@hiveweave/db";
import { agents, modules } from "@hiveweave/db";
import { eq, isNull } from "drizzle-orm";
import { randomUUID } from "crypto";

/** Recursive tree node representing an agent in the organization hierarchy. */
export interface OrgNode {
  id: string;
  name: string;
  role: string;
  status: string;
  permissionType: string;
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
  /**
   * Get the full organization tree starting from root agents.
   * Roots are agents whose `parentId` is null.
   */
  async getOrgTree(): Promise<OrgNode[]> {
    const allAgents = await db.select().from(agents);
    const roots = allAgents.filter(a => !a.parentId);
    return roots.map(r => this.buildTree(r, allAgents));
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
      name: agent.name,
      role: agent.role,
      status: agent.status,
      permissionType: agent.permissionType,
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
    permissionType: "coordinator" | "executor";
  }): Promise<string> {
    const id = randomUUID();
    const now = Date.now();

    await db.insert(agents).values({
      id,
      name: input.name,
      role: input.role,
      goal: input.goal,
      backstory: input.backstory,
      skills: JSON.stringify(input.skills),
      parentId: input.parentId || null,
      moduleId: input.moduleId || null,
      status: "created",
      permissionType: input.permissionType,
      createdAt: now,
      updatedAt: now,
    });

    return id;
  }

  /**
   * Get an agent by its ID.
   *
   * @param agentId - UUID of the agent to retrieve.
   * @returns The agent record, or null if not found.
   */
  async getAgent(agentId: string) {
    const result = await db.select().from(agents).where(eq(agents.id, agentId));
    return result[0] || null;
  }

  /**
   * Get all direct children of an agent.
   *
   * @param agentId - UUID of the parent agent.
   * @returns Array of agent records whose parentId matches the given ID.
   */
  async getChildren(agentId: string) {
    return db.select().from(agents).where(eq(agents.parentId, agentId));
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
    await db.update(agents)
      .set({ status, updatedAt: Date.now() })
      .where(eq(agents.id, agentId));
  }

  /**
   * Get all registered modules (used to map agents to their working domain).
   */
  async getModules() {
    return db.select().from(modules);
  }
}
