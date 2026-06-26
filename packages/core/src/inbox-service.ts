import { inbox } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, and, desc } from "drizzle-orm";
import { randomUUID } from "crypto";

/**
 * InboxService — manages messages between agents (subordinate→superior, peer→peer).
 *
 * When an agent calls message_superior or message_peer, a record is written to
 * the inbox table. The recipient sees pending inbox messages injected into their
 * system prompt on their next chat turn.
 */
export class InboxService {
  constructor(private readonly db: Database) {}

  /**
   * Send a message to another agent (superior or peer).
   * @param messageType - "superior" for upward reports, "peer" for lateral communication
   */
  async sendMessage(
    fromAgentId: string,
    toAgentId: string,
    message: string,
    messageType: "superior" | "peer" | "alarm" = "superior",
    expectReport: boolean = false,
    priority: "low" | "normal" | "urgent" = "normal",
  ): Promise<string> {
    const id = randomUUID();
    await this.db.insert(inbox).values({
      id,
      fromAgentId,
      toAgentId,
      message,
      messageType,
      expectReport,
      priority,
      read: false,
      createdAt: Date.now(),
    });
    return id;
  }

  /**
   * Get unread inbox messages for an agent.
   * @param messageType - Optional filter: "superior" or "peer". Omit to get all.
   */
  async getPendingMessages(toAgentId: string, limit = 10, messageType?: "superior" | "peer" | "alarm") {
    const conditions = [eq(inbox.toAgentId, toAgentId), eq(inbox.read, false)];
    if (messageType) {
      conditions.push(eq(inbox.messageType, messageType));
    }
    return this.db
      .select()
      .from(inbox)
      .where(and(...conditions))
      .orderBy(desc(inbox.createdAt))
      .limit(limit);
  }

  /**
   * Mark messages for an agent as read.
   * @param messageType - Optional filter. Omit to mark all as read.
   */
  async markAsRead(toAgentId: string, messageType?: "superior" | "peer" | "alarm"): Promise<number> {
    const pending = await this.getPendingMessages(toAgentId, 100, messageType);
    if (pending.length === 0) return 0;
    for (const msg of pending) {
      await this.db.update(inbox).set({ read: true }).where(eq(inbox.id, msg.id));
    }
    return pending.length;
  }
}
