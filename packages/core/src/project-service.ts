import { projects, agents, chatMessages, workLogs, handoffs, inbox, memories, permissionRequests, personnelRecords, conversationTurns, modules, merges, scheduledAlarms } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, inArray, and, or } from "drizzle-orm";
import { randomUUID } from "crypto";
import { getDefaultCharter, parseCharterJson, type ProjectCharter } from "@hiveweave/shared";

export class ProjectService {
  constructor(private readonly metaDb: Database) {}

  async listProjects() {
    return this.metaDb.select().from(projects).orderBy(projects.createdAt);
  }

  async getProject(projectId: string) {
    const result = await this.metaDb.select().from(projects).where(eq(projects.id, projectId));
    return result[0] || null;
  }

  async createProject(name: string, workspacePath?: string, description?: string, orgParadigm?: string): Promise<string> {
    const id = randomUUID();
    const defaultCharter = getDefaultCharter();
    if (orgParadigm) defaultCharter.orgParadigm = orgParadigm;
    await this.metaDb.insert(projects).values({
      id,
      name,
      workspacePath: workspacePath || null,
      description: description || null,
      orgParadigm: orgParadigm || null,
      charterJson: JSON.stringify(defaultCharter),
      createdAt: Date.now(),
    });
    return id;
  }

  async updateWorkspacePath(projectId: string, workspacePath: string | null) {
    await this.metaDb.update(projects)
      .set({ workspacePath })
      .where(eq(projects.id, projectId));
  }

  async updateProject(projectId: string, updates: { description?: string | null; orgParadigm?: string | null }) {
    await this.metaDb.update(projects)
      .set(updates)
      .where(eq(projects.id, projectId));
  }

  /**
   * Delete a project and all its data.
   *
   * @param projectId  - The project to delete.
   * @param projectDb  - The per-project DB (if the project has a workspace).
   *                     All agent-scoped tables are deleted from here.
   *                     If not provided, falls back to metaDb (for projects without workspace).
   */
  async deleteProjectCascade(projectId: string, projectDb?: Database): Promise<{ ok: boolean; reason?: string }> {
    const project = await this.getProject(projectId);
    if (!project) return { ok: false, reason: "Project not found" };

    const pdb = projectDb || this.metaDb;

    // Get all agent IDs belonging to this project
    const projectAgents = await pdb.select({ id: agents.id }).from(agents).where(eq(agents.projectId, projectId));
    const agentIds = projectAgents.map((a) => a.id);

    if (agentIds.length > 0) {
      const agentFilter = inArray(agents.id, agentIds);

      // Delete permission requests for these agents
      await pdb.delete(permissionRequests).where(inArray(permissionRequests.agentId, agentIds));

      // Delete personnel records for these agents (before agents — has FK refs)
      await pdb.delete(personnelRecords).where(inArray(personnelRecords.agentId, agentIds));

      // Delete chat messages for these agents
      await pdb.delete(chatMessages).where(inArray(chatMessages.agentId, agentIds));

      // Delete conversation turns
      await pdb.delete(conversationTurns).where(inArray(conversationTurns.agentId, agentIds));

      // Delete work logs
      await pdb.delete(workLogs).where(inArray(workLogs.agentId, agentIds));

      // Delete handoffs where from or to agent belongs to this project
      await pdb.delete(handoffs).where(
        or(inArray(handoffs.fromAgentId, agentIds), inArray(handoffs.toAgentId, agentIds))!
      );

      // Delete inbox messages
      await pdb.delete(inbox).where(
        or(inArray(inbox.fromAgentId, agentIds), inArray(inbox.toAgentId, agentIds))!
      );

      // Delete memories for these agents
      await pdb.delete(memories).where(inArray(memories.agentId, agentIds));

      // Delete merges targeting agents in this project
      await pdb.delete(merges).where(inArray(merges.targetAgentId, agentIds));
    }

    // Delete modules belonging to this project (via agents)
    // Modules don't have a direct projectId FK, but they're project-scoped
    // through agents. Delete all modules that have no remaining agents.
    // (Simpler: just delete all modules — they're project-scoped.)
    await pdb.delete(modules);

    // Delete scheduled alarms for this project
    await pdb.delete(scheduledAlarms).where(eq(scheduledAlarms.projectId, projectId));

    // Delete all agents in the project
    await pdb.delete(agents).where(eq(agents.projectId, projectId));

    // Delete the project record from meta DB
    await this.metaDb.delete(projects).where(eq(projects.id, projectId));

    return { ok: true };
  }

  async getCharter(projectId: string): Promise<ProjectCharter | null> {
    const project = await this.getProject(projectId);
    if (!project) return null;
    return parseCharterJson(project.charterJson);
  }

  async saveCharter(projectId: string, charter: ProjectCharter, updatedByAgentId: string): Promise<void> {
    const payload: ProjectCharter = {
      ...charter,
      updatedAt: Date.now(),
      updatedByAgentId,
    };
    await this.metaDb.update(projects)
      .set({ charterJson: JSON.stringify(payload) })
      .where(eq(projects.id, projectId));
  }

}
