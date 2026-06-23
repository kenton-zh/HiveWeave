import { personnelRecords } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, and, ne } from "drizzle-orm";
import { randomUUID } from "crypto";

export interface RosterUpsertInput {
  projectId: string;
  agentId: string;
  position?: string;
  department?: string;
  responsibilities?: string;
  notes?: string;
  status?: string;
  updatedBy: string;
}

/**
 * RosterService — manages the personnel roster (编制表).
 *
 * Only HR agents should write to the roster. Other agents may read it.
 * Each personnel record maps 1:1 to an agent and stores structured
 * position/department/responsibilities metadata.
 */
export class RosterService {
  constructor(private readonly db: Database) {}

  /** Get the full roster for a project (active + probation + inactive, excluding terminated). */
  async getProjectRoster(projectId: string) {
    return this.db
      .select()
      .from(personnelRecords)
      .where(
        and(
          eq(personnelRecords.projectId, projectId),
          ne(personnelRecords.status, "terminated"),
        ),
      );
  }

  /** Get the roster record for a single agent. */
  async getAgentRecord(agentId: string) {
    const rows = await this.db
      .select()
      .from(personnelRecords)
      .where(eq(personnelRecords.agentId, agentId));
    return rows[0] || null;
  }

  /** Create or update a roster record. */
  async upsertRecord(input: RosterUpsertInput) {
    const existing = await this.getAgentRecord(input.agentId);
    const now = Date.now();

    if (existing) {
      // Update existing record
      await this.db
        .update(personnelRecords)
        .set({
          position: input.position ?? existing.position,
          department: input.department ?? existing.department,
          responsibilities: input.responsibilities ?? existing.responsibilities,
          notes: input.notes ?? existing.notes,
          status: input.status ?? existing.status,
          updatedBy: input.updatedBy,
          updatedAt: now,
        })
        .where(eq(personnelRecords.agentId, input.agentId));
      return existing.id;
    }

    // Create new record
    const id = randomUUID();
    await this.db.insert(personnelRecords).values({
      id,
      projectId: input.projectId,
      agentId: input.agentId,
      position: input.position || "",
      department: input.department || "",
      responsibilities: input.responsibilities || "",
      notes: input.notes || "",
      status: input.status || "active",
      createdBy: input.updatedBy,
      updatedBy: input.updatedBy,
      createdAt: now,
      updatedAt: now,
    });
    return id;
  }

  /** Mark an agent's roster record as terminated (soft-delete). */
  async terminateRecord(agentId: string, updatedBy: string) {
    const now = Date.now();
    await this.db
      .update(personnelRecords)
      .set({ status: "terminated", updatedBy, updatedAt: now })
      .where(eq(personnelRecords.agentId, agentId));
  }

  /** Hard-delete a roster record (used in cascade deletion). */
  async deleteByProject(projectId: string) {
    await this.db
      .delete(personnelRecords)
      .where(eq(personnelRecords.projectId, projectId));
  }
}
