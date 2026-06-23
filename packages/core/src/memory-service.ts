import { memories } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, and } from "drizzle-orm";
import { randomUUID } from "crypto";

/**
 * Memory CRUD service with scope-based isolation.
 *
 * Memories are persisted text fragments that provide long-term context to
 * agents. Three scopes are supported:
 *
 *   - **project** — Shared across every agent in the workspace (the "constitution").
 *   - **agent**   — Private to a single agent (its personal working memory).
 *   - **archive** — Frozen memories from dissolved agents, keyed by module,
 *                   available to successor agents via the revival protocol.
 */
export class MemoryService {
  constructor(private readonly db: Database) {}

  /**
   * Get project-level memories (shared across all agents).
   * These form the project constitution referenced in every agent's context.
   */
  async getProjectMemories(): Promise<any[]> {
    return this.db.select().from(memories).where(eq(memories.scope, "project"));
  }

  /**
   * Get an agent's private memories.
   *
   * @param agentId - UUID of the agent whose private memories to retrieve.
   */
  async getAgentMemories(agentId: string): Promise<any[]> {
    return this.db.select().from(memories).where(
      and(eq(memories.scope, "agent"), eq(memories.agentId, agentId))
    );
  }

  /**
   * Get archived memories for a specific module (used during agent revival).
   *
   * When an agent is dissolved (e.g. during a Handoff), its memories are
   * flipped to "archive" scope. Successor agents assigned to the same module
   * can read these to pick up where the predecessor left off.
   *
   * @param moduleId - UUID of the module whose archived memories to load.
   */
  async getArchivedMemories(moduleId: string): Promise<any[]> {
    return this.db.select().from(memories).where(
      and(eq(memories.scope, "archive"), eq(memories.moduleId, moduleId))
    );
  }

  /**
   * Write a new memory entry.
   *
   * @param input - Memory parameters including scope, content, optional
   *                agent/module references, and arbitrary metadata.
   * @returns The UUID of the newly created memory record.
   */
  async writeMemory(input: {
    agentId?: string;
    scope: "project" | "agent" | "archive";
    moduleId?: string;
    type: string;
    content: string;
    sourceAgentId?: string;
    metadata?: Record<string, unknown>;
  }): Promise<string> {
    const id = randomUUID();
    const now = Date.now();

    await this.db.insert(memories).values({
      id,
      agentId: input.agentId || null,
      scope: input.scope,
      moduleId: input.moduleId || null,
      type: input.type,
      content: input.content,
      sourceAgentId: input.sourceAgentId || null,
      metadata: JSON.stringify(input.metadata || {}),
      createdAt: now,
      updatedAt: now,
    });

    return id;
  }

  /**
   * Archive all agent memories (called during Handoff/dissolution).
   *
   * Every memory currently scoped to the given agent is moved to "archive"
   * scope so that successor agents on the same module can access them while
   * the dissolved agent no longer sees them as active context.
   *
   * @param agentId - UUID of the agent being dissolved.
   * @returns The number of memory records that were archived.
   */
  async archiveAgentMemories(agentId: string): Promise<number> {
    const agentMems = await this.getAgentMemories(agentId);
    const now = Date.now();

    for (const mem of agentMems) {
      await this.db.update(memories)
        .set({ scope: "archive", updatedAt: now })
        .where(eq(memories.id, mem.id));
    }

    return agentMems.length;
  }

  /**
   * Build the full context block injected into an agent's system prompt
   * before each conversation turn.
   *
   * The context is assembled from three sources:
   *   1. Project-level memories (the shared constitution).
   *   2. The agent's own private working memories.
   *   3. Archived memories from predecessors on the same module (if any).
   *
   * @param agentId  - UUID of the agent receiving the context.
   * @param moduleId - Optional module UUID to pull relevant archived memories.
   * @returns A formatted Markdown string ready to be appended to the system prompt.
   */
  async buildAgentContext(agentId: string, moduleId?: string): Promise<string> {
    const projectMems = await this.getProjectMemories();
    const privateMems = await this.getAgentMemories(agentId);

    // If the agent is assigned to a module, also load relevant archived memories
    let archivedMems: any[] = [];
    if (moduleId) {
      archivedMems = await this.getArchivedMemories(moduleId);
    }

    let context = "## Project Constitution (Shared)\n";
    for (const m of projectMems) {
      context += `- [${m.type}] ${m.content}\n`;
    }

    context += "\n## Your Private Working Memory\n";
    if (privateMems.length === 0) {
      context += "(empty — you haven't accumulated any work memories yet)\n";
    } else {
      for (const m of privateMems) {
        context += `- [${m.type}] ${m.content}\n`;
      }
    }

    if (archivedMems.length > 0) {
      context += "\n## Archived Memories (from predecessors on this module)\n";
      for (const m of archivedMems) {
        context += `- [${m.type}] ${m.content}\n`;
      }
    }

    return context;
  }
}
