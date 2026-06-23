import { handoffs } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, and, desc, or } from "drizzle-orm";
import { randomUUID } from "crypto";

/**
 * HandoffService — manages the task handoff lifecycle between agents.
 *
 * Flow:
 *   1. Coordinator calls dispatch_task → createHandoff() → status = "pending"
 *   2. Subordinate starts chatting → acceptPendingHandoffs() → status = "accepted"
 *   3. Subordinate calls report_completion → completeHandoff() → status = "completed"
 *   4. Coordinator reviews work:
 *      a. approve_work → approveHandoff() → status = "approved" (terminal)
 *      b. reject_work → reopenHandoff() → status = "accepted" (subordinate reworks)
 */
export class HandoffService {
  constructor(private readonly db: Database) {}

  /**
   * Create a new handoff record (called when coordinator dispatches a task).
   */
  async createHandoff(input: {
    fromAgentId: string;
    toAgentId: string;
    summary: string;
    moduleId?: string | null;
    expectReport?: boolean;
  }): Promise<string> {
    const id = randomUUID();
    await this.db.insert(handoffs).values({
      id,
      fromAgentId: input.fromAgentId,
      toAgentId: input.toAgentId,
      moduleId: input.moduleId || null,
      summary: input.summary,
      expectReport: input.expectReport ?? false,
      status: "pending",
      createdAt: Date.now(),
    });
    return id;
  }

  /**
   * Get pending handoffs for a subordinate agent (tasks waiting to be picked up).
   */
  async getPendingHandoffs(toAgentId: string) {
    return this.db
      .select()
      .from(handoffs)
      .where(and(eq(handoffs.toAgentId, toAgentId), eq(handoffs.status, "pending")))
      .orderBy(desc(handoffs.createdAt));
  }

  /**
   * Get accepted (in-progress) handoffs for a subordinate agent.
   */
  async getAcceptedHandoffs(toAgentId: string) {
    return this.db
      .select()
      .from(handoffs)
      .where(and(eq(handoffs.toAgentId, toAgentId), eq(handoffs.status, "accepted")))
      .orderBy(desc(handoffs.createdAt));
  }

  /**
   * Auto-accept all pending handoffs for a subordinate when they start a chat session.
   * Returns the number of handoffs accepted.
   */
  async acceptPendingHandoffs(toAgentId: string): Promise<number> {
    const pending = await this.getPendingHandoffs(toAgentId);
    if (pending.length === 0) return 0;

    const ids = pending.map((h) => h.id);
    for (const id of ids) {
      await this.db
        .update(handoffs)
        .set({ status: "accepted", updatedAt: Date.now() })
        .where(eq(handoffs.id, id));
    }
    return pending.length;
  }

  /**
   * Complete a specific handoff, or the most recent accepted handoff if no ID given.
   * Called when subordinate reports completion.
   */
  async completeHandoff(
    toAgentId: string,
    handoffId?: string,
  ): Promise<{ completed: boolean; handoffId?: string }> {
    let targetId = handoffId;

    if (!targetId) {
      // Find the most recent accepted handoff for this agent
      const accepted = await this.getAcceptedHandoffs(toAgentId);
      if (accepted.length === 0) {
        // Fall back to most recent pending
        const pending = await this.getPendingHandoffs(toAgentId);
        if (pending.length === 0) return { completed: false };
        targetId = pending[0].id;
      } else {
        targetId = accepted[0].id;
      }
    }

    await this.db
      .update(handoffs)
      .set({ status: "completed", updatedAt: Date.now() })
      .where(eq(handoffs.id, targetId));

    return { completed: true, handoffId: targetId };
  }

  /**
   * Get completed handoffs FROM a specific subordinate (for coordinator review).
   */
  async getCompletedFromSubordinate(fromAgentId: string, toAgentId: string, limit = 5) {
    return this.db
      .select()
      .from(handoffs)
      .where(
        and(
          eq(handoffs.fromAgentId, fromAgentId),
          eq(handoffs.toAgentId, toAgentId),
          eq(handoffs.status, "completed"),
        ),
      )
      .orderBy(desc(handoffs.updatedAt))
      .limit(limit);
  }

  /**
   * Get all handoffs related to an agent (sent or received).
   */
  async getHandoffsForAgent(agentId: string, limit = 10) {
    return this.db
      .select()
      .from(handoffs)
      .where(or(eq(handoffs.fromAgentId, agentId), eq(handoffs.toAgentId, agentId)))
      .orderBy(desc(handoffs.createdAt))
      .limit(limit);
  }

  /**
   * Get accepted handoffs with expectReport=true that have NOT been reported up yet.
   * Used by triggerCoordinator's self-check to avoid repeated reporting.
   */
  async getUnreportedAcceptedHandoffs(toAgentId: string) {
    return this.db
      .select()
      .from(handoffs)
      .where(
        and(
          eq(handoffs.toAgentId, toAgentId),
          eq(handoffs.status, "accepted"),
          eq(handoffs.expectReport, true),
          eq(handoffs.reportedUp, false),
        ),
      )
      .orderBy(desc(handoffs.createdAt));
  }

  /**
   * Mark all accepted handoffs with expectReport=true as reported up for a given agent.
   * Called after the coordinator successfully calls message_superior.
   */
  async markReportedUp(toAgentId: string): Promise<number> {
    const unreported = await this.getUnreportedAcceptedHandoffs(toAgentId);
    if (unreported.length === 0) return 0;
    for (const h of unreported) {
      await this.db
        .update(handoffs)
        .set({ reportedUp: true, updatedAt: Date.now() })
        .where(eq(handoffs.id, h.id));
    }
    return unreported.length;
  }

  /**
   * Get the most recent completed handoff from coordinator to subordinate.
   * Used to check if there's work awaiting approval.
   */
  async getLatestCompleted(fromAgentId: string, toAgentId: string) {
    const results = await this.db
      .select()
      .from(handoffs)
      .where(
        and(
          eq(handoffs.fromAgentId, fromAgentId),
          eq(handoffs.toAgentId, toAgentId),
          eq(handoffs.status, "completed"),
        ),
      )
      .orderBy(desc(handoffs.updatedAt))
      .limit(1);
    return results[0] || null;
  }

  /**
   * Approve a completed handoff — transitions it to the "approved" terminal state.
   * Called when coordinator calls approve_work.
   */
  async approveHandoff(
    fromAgentId: string,
    toAgentId: string,
  ): Promise<{ approved: boolean; handoffId?: string }> {
    const handoff = await this.getLatestCompleted(fromAgentId, toAgentId);
    if (!handoff) return { approved: false };

    await this.db
      .update(handoffs)
      .set({ status: "approved", updatedAt: Date.now() })
      .where(eq(handoffs.id, handoff.id));

    return { approved: true, handoffId: handoff.id };
  }

  /**
   * Reopen a completed handoff back to "accepted" so the subordinate can rework.
   * Called when coordinator calls reject_work.
   * Finds the most recent completed handoff for the subordinate from ANY coordinator.
   */
  async reopenHandoff(
    fromAgentId: string,
    toAgentId: string,
  ): Promise<{ reopened: boolean; handoffId?: string }> {
    const handoff = await this.getLatestCompleted(fromAgentId, toAgentId);
    if (!handoff) return { reopened: false };

    await this.db
      .update(handoffs)
      .set({ status: "accepted", updatedAt: Date.now() })
      .where(eq(handoffs.id, handoff.id));

    return { reopened: true, handoffId: handoff.id };
  }
}
