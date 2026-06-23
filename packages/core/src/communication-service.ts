import { randomUUID } from "crypto";

/** Represents an active communication between two agents. */
export interface ActiveCommunication {
  id: string;
  fromAgentId: string;
  toAgentId: string;
  type: "dispatch" | "message" | "trigger" | "peer";
  createdAt: number;
}

/**
 * In-memory tracker for active agent-to-agent communications.
 *
 * Each communication has a TTL (default 2 minutes). Expired entries
 * are automatically pruned when `getActiveCommunications()` is called.
 *
 * The frontend polls this service to render animated arrows between
 * agents that are currently communicating.
 */
class CommunicationService {
  private comms: Map<string, ActiveCommunication> = new Map();
  private TTL: number;

  constructor(ttlMs = 120_000) {
    this.TTL = ttlMs;
  }

  /**
   * Record a new communication event between two agents.
   *
   * If a communication already exists between the same pair (in either
   * direction), its timestamp is refreshed rather than creating a duplicate.
   */
  addCommunication(
    fromAgentId: string,
    toAgentId: string,
    type: ActiveCommunication["type"],
  ): string {
    // Check for an existing communication between this pair (either direction)
    for (const [, c] of this.comms) {
      if (
        (c.fromAgentId === fromAgentId && c.toAgentId === toAgentId) ||
        (c.fromAgentId === toAgentId && c.toAgentId === fromAgentId)
      ) {
        // Refresh the timestamp
        c.createdAt = Date.now();
        c.type = type;
        return c.id;
      }
    }

    const id = randomUUID();
    this.comms.set(id, {
      id,
      fromAgentId,
      toAgentId,
      type,
      createdAt: Date.now(),
    });
    return id;
  }

  /**
   * Return all active (non-expired) communications.
   * Expired entries are pruned as a side effect.
   */
  getActiveCommunications(): ActiveCommunication[] {
    const now = Date.now();
    const expired: string[] = [];

    for (const [id, c] of this.comms) {
      if (now - c.createdAt > this.TTL) {
        expired.push(id);
      }
    }

    for (const id of expired) {
      this.comms.delete(id);
    }

    return Array.from(this.comms.values());
  }
}

export const communicationService = new CommunicationService();
