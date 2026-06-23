import { workLogs } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, desc, asc } from "drizzle-orm";
import { randomUUID } from "crypto";

/** Result returned when a coordinator dispatches a task to a subordinate. */
export interface DispatchResult {
  taskId: string;
  fromAgentId: string;
  toAgentId: string;
  description: string;
}

/**
 * Manages the dispatch → execute → accept/reject workflow.
 *
 * Flow:
 *   Coordinator dispatches task → Executor works → Executor reports completion
 *   → Coordinator reviews (reads code + logs) → Approve or Reject
 *
 * All transitions are recorded as work log entries so that the full audit
 * trail is available to both the coordinator and the frontend timeline view.
 */
export class DispatchService {
  constructor(private readonly db: Database) {}

  /**
   * Coordinator dispatches a task to a subordinate agent.
   *
   * A `discussion`-type work log is written on the coordinator's side so that
   * the dispatch action is traceable in the session timeline.
   *
   * @param input - Dispatch parameters: who is sending, who is receiving,
   *                a human-readable description, and the session context.
   * @returns A DispatchResult containing the generated task/log ID.
   */
  async dispatchTask(input: {
    fromAgentId: string;
    toAgentId: string;
    description: string;
    sessionId: string;
  }): Promise<DispatchResult> {
    const logId = randomUUID();

    await this.db.insert(workLogs).values({
      id: logId,
      agentId: input.fromAgentId,
      sessionId: input.sessionId,
      type: "discussion",
      summary: `Dispatched task to agent ${input.toAgentId}: ${input.description}`,
      details: JSON.stringify({
        type: "dispatch",
        toAgentId: input.toAgentId,
        description: input.description,
      }),
      createdAt: Date.now(),
    });

    return {
      taskId: logId,
      fromAgentId: input.fromAgentId,
      toAgentId: input.toAgentId,
      description: input.description,
    };
  }

  /**
   * Executor writes a work log entry after completing (or progressing on) work.
   *
   * The log is attached to the executor's own agent record and can later be
   * pulled by the coordinator via the log-reading protocol ("日志读取协议").
   *
   * @param input - Work log parameters: agent, session, type tag, summary,
   *                and optional structured details.
   * @returns The UUID of the newly created work log entry.
   */
  async writeWorkLog(input: {
    agentId: string;
    sessionId: string;
    type: string;
    summary: string;
    details?: Record<string, unknown>;
  }): Promise<string> {
    const id = randomUUID();

    await this.db.insert(workLogs).values({
      id,
      agentId: input.agentId,
      sessionId: input.sessionId,
      type: input.type,
      summary: input.summary,
      details: JSON.stringify(input.details || {}),
      createdAt: Date.now(),
    });

    return id;
  }

  /**
   * Coordinator reads a subordinate's recent work logs.
   *
   * This is called automatically before a conversation turn, implementing
   * the log-reading protocol ("日志读取协议") that keeps the coordinator
   * informed of subordinate progress without explicit polling.
   *
   * @param subordinateAgentId - UUID of the subordinate agent whose logs to read.
   * @param limit              - Maximum number of log entries to return (default 10).
   * @returns Array of work log records ordered newest-first.
   */
  async getSubordinateLogs(subordinateAgentId: string, limit = 10): Promise<any[]> {
    return this.db
      .select()
      .from(workLogs)
      .where(eq(workLogs.agentId, subordinateAgentId))
      .orderBy(desc(workLogs.createdAt))
      .limit(limit);
  }

  /**
   * Coordinator reads subordinate logs newer than a given cursor timestamp.
   * Only returns logs created after the cursor, ordered oldest-first for chronological reading.
   *
   * @param subordinateAgentId - UUID of the subordinate agent whose logs to read.
   * @param sinceTimestamp     - Only return logs with createdAt > this timestamp.
   * @returns Array of work log records ordered oldest-first.
   */
  async getSubordinateLogsSince(subordinateAgentId: string, sinceTimestamp: number): Promise<any[]> {
    const logs = await this.db
      .select()
      .from(workLogs)
      .where(eq(workLogs.agentId, subordinateAgentId))
      .orderBy(asc(workLogs.createdAt))
      .all();
    return logs.filter((l: any) => l.createdAt > sinceTimestamp);
  }

  /**
   * Get all work logs for an agent (used to inject context into the agent's
   * own system prompt before a conversation turn).
   *
   * @param agentId - UUID of the agent whose logs to retrieve.
   * @param limit   - Maximum number of log entries to return (default 20).
   * @returns Array of work log records ordered newest-first.
   */
  async getAgentLogs(agentId: string, limit = 20): Promise<any[]> {
    return this.db
      .select()
      .from(workLogs)
      .where(eq(workLogs.agentId, agentId))
      .orderBy(desc(workLogs.createdAt))
      .limit(limit);
  }

  /**
   * Coordinator approves a subordinate's completed work.
   *
   * Writes a "completion"-type work log on the coordinator's side recording
   * the approval decision, so it shows up in the session timeline.
   *
   * @param agentId      - UUID of the coordinator who is approving.
   * @param sessionId    - Current session context.
   * @param subordinateId - UUID of the subordinate whose work is approved.
   * @param review       - Optional review comment.
   * @returns The UUID of the approval work log entry.
   */
  async approveWork(
    agentId: string,
    sessionId: string,
    subordinateId: string,
    review?: string,
  ): Promise<string> {
    return this.writeWorkLog({
      agentId,
      sessionId,
      type: "completion",
      summary: `Approved work from ${subordinateId}: ${review || "LGTM"}`,
      details: { action: "approve", subordinateId, review: review || null },
    });
  }

  /**
   * Coordinator rejects a subordinate's completed work with feedback.
   *
   * Writes an "error"-type work log on the coordinator's side so the
   * rejection (and its reasoning) is visible in the session timeline.
   *
   * @param agentId      - UUID of the coordinator who is rejecting.
   * @param sessionId    - Current session context.
   * @param subordinateId - UUID of the subordinate whose work is rejected.
   * @param feedback     - Required explanation of what needs to be revised.
   * @returns The UUID of the rejection work log entry.
   */
  async rejectWork(
    agentId: string,
    sessionId: string,
    subordinateId: string,
    feedback: string,
  ): Promise<string> {
    return this.writeWorkLog({
      agentId,
      sessionId,
      type: "error",
      summary: `Rejected work from ${subordinateId}: ${feedback || "Needs revision"}`,
      details: { action: "reject", subordinateId, feedback: feedback || null },
    });
  }
}
