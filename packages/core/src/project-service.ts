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
    // Check for duplicate workspace path
    if (workspacePath) {
      const normalized = workspacePath.replace(/\\/g, "/").replace(/\/+$/, "");
      const existing = await this.metaDb
        .select({ id: projects.id, name: projects.name })
        .from(projects)
        .where(eq(projects.workspacePath, workspacePath));
      if (existing.length > 0) {
        throw new Error(`Workspace path "${workspacePath}" is already used by project "${existing[0].name}". Each project must have a unique workspace.`);
      }
    }

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

  async getGoals(projectId: string): Promise<EnterpriseGoals | null> {
    const project = await this.getProject(projectId);
    if (!project?.goalsJson) return null;
    try { return JSON.parse(project.goalsJson); } catch { return null; }
  }

  async saveGoals(projectId: string, goals: EnterpriseGoals): Promise<void> {
    await this.metaDb.update(projects)
      .set({ goalsJson: JSON.stringify({ ...goals, updatedAt: Date.now() }) })
      .where(eq(projects.id, projectId));
  }

}

/** Enterprise goals / workboard — visible to all agents, authored by CEO + user. */
export interface EnterpriseGoals {
  objective: string;
  focus: string;
  keyResults: Array<{ text: string; status: "todo" | "doing" | "done"; owner?: string }>;
  updatedAt?: number;
}

/** Format goals into a compact, token-efficient prompt block. */
export function formatGoalsForPrompt(goals: EnterpriseGoals | null): string {
  if (!goals?.objective) return "";
  const done = goals.keyResults.filter((k) => k.status === "done").length;
  const total = goals.keyResults.length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const statusIcon = { todo: "○", doing: "◐", done: "●" } as const;
  const krs = goals.keyResults
    .map((kr) => `${statusIcon[kr.status] || "○"} ${kr.text}${kr.owner ? ` [${kr.owner}]` : ""}`)
    .join("\n");
  return `## Enterprise Goals (Workboard)
Objective: ${goals.objective}
Progress: ${done}/${total} (${pct}%)
Current Focus: ${goals.focus || "—"}
Key Results:
${krs}
— This workboard is set by leadership and the user. Align your work with these goals.`;
}
