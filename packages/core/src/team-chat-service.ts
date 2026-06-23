import { randomUUID } from "crypto";
import type { ChatMessageService } from "./chat-message-service.js";

/**
 * Persists inter-agent team communications into chat_messages so the UI
 * team comms panel can display them.
 */
export class TeamChatService {
  constructor(private readonly chat: ChatMessageService) {}

  /** Record a message received from another agent (inbox / handoff). */
  async recordIncoming(
    toAgentId: string,
    fromAgentId: string,
    content: string,
    dedupeKey?: string,
  ): Promise<void> {
    const id = dedupeKey ? `team-in-${dedupeKey}` : randomUUID();
    try {
      await this.chat.saveMessage({
        id,
        agentId: toAgentId,
        role: "team",
        content,
        teamFromAgentId: fromAgentId,
        isBackground: true,
        isRead: false,
        createdAt: Date.now(),
      });
    } catch {
      // Duplicate dedupe key — already recorded
    }
  }

  /** Record an outgoing team action from this agent. */
  async recordOutgoing(
    fromAgentId: string,
    toAgentId: string,
    content: string,
    toolCalls?: string,
  ): Promise<void> {
    await this.chat.saveMessage({
      id: randomUUID(),
      agentId: fromAgentId,
      role: "team",
      content,
      teamToAgentId: toAgentId,
      toolCalls: toolCalls || "[]",
      isBackground: true,
      isRead: true,
      createdAt: Date.now(),
    });
  }
}
