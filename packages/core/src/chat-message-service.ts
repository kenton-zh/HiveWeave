import { chatMessages } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, and, asc } from "drizzle-orm";

/**
 * ChatMessageService — persists user-visible chat history per agent.
 * The frontend loads messages exclusively from this store; streaming is
 * a live overlay only while the user watches an in-progress reply.
 */
export class ChatMessageService {
  constructor(private readonly db: Database) {}

  /**
   * Save a single chat message (user, assistant, or background).
   */
  async saveMessage(msg: {
    id: string;
    agentId: string;
    role: string;
    content: string;
    toolCalls?: string;
    images?: string | null;
    isBackground?: boolean;
    isRead?: boolean;
    isStreaming?: boolean;
    teamFromAgentId?: string | null;
    teamToAgentId?: string | null;
    createdAt: number;
  }): Promise<void> {
    await this.db.insert(chatMessages).values({
      id: msg.id,
      agentId: msg.agentId,
      role: msg.role,
      content: msg.content,
      toolCalls: msg.toolCalls || "[]",
      images: msg.images ?? null,
      isBackground: msg.isBackground ?? false,
      isRead: msg.isRead ?? true,
      isStreaming: msg.isStreaming ?? false,
      teamFromAgentId: msg.teamFromAgentId ?? null,
      teamToAgentId: msg.teamToAgentId ?? null,
      createdAt: msg.createdAt,
    });
  }

  /**
   * Update an existing message (e.g. finalize a streaming assistant reply).
   */
  async updateMessage(
    id: string,
    patch: {
      content?: string;
      toolCalls?: string;
      isStreaming?: boolean;
      isRead?: boolean;
    },
  ): Promise<void> {
    const values: Record<string, unknown> = {};
    if (patch.content !== undefined) values.content = patch.content;
    if (patch.toolCalls !== undefined) values.toolCalls = patch.toolCalls;
    if (patch.isStreaming !== undefined) values.isStreaming = patch.isStreaming;
    if (patch.isRead !== undefined) values.isRead = patch.isRead;
    if (Object.keys(values).length === 0) return;
    await this.db.update(chatMessages).set(values).where(eq(chatMessages.id, id));
  }

  /**
   * Get all messages for an agent, ordered chronologically (oldest first).
   */
  async getMessages(agentId: string, limit = 200) {
    return this.db
      .select()
      .from(chatMessages)
      .where(eq(chatMessages.agentId, agentId))
      .orderBy(asc(chatMessages.createdAt))
      .limit(limit);
  }

  /**
   * Get unread background messages for an agent (auto-replies from coordinators).
   */
  async getUnreadBackground(agentId: string) {
    return this.db
      .select()
      .from(chatMessages)
      .where(
        and(
          eq(chatMessages.agentId, agentId),
          eq(chatMessages.isBackground, true),
          eq(chatMessages.isRead, false),
        ),
      )
      .orderBy(asc(chatMessages.createdAt));
  }

  /**
   * Mark specific messages as read by their IDs.
   */
  async markAsRead(ids: string[]): Promise<number> {
    if (ids.length === 0) return 0;
    let count = 0;
    for (const id of ids) {
      await this.db.update(chatMessages).set({ isRead: true }).where(eq(chatMessages.id, id));
      count++;
    }
    return count;
  }
}
