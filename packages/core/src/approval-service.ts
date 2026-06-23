import { permissionRequests, agents } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, and, desc, ne, lt } from "drizzle-orm";
import { randomUUID } from "crypto";
import { PermissionService } from "./permission-service.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ApprovalRequestData {
  agentId: string;
  toolName: string;
  toolArguments: Record<string, any>;
  description: string;
}

export interface ApprovalResponseData {
  requestId: string;
  approved: boolean;
  remember?: boolean;
  userNote?: string;
}

// ---------------------------------------------------------------------------
// In-memory event emitter for real-time approval notifications
// The chat route can register a callback to push SSE events to the client
// ---------------------------------------------------------------------------

type ApprovalListener = (agentId: string, requestId: string) => void;
const listeners = new Set<ApprovalListener>();

export function onApprovalRequest(listener: ApprovalListener) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function emitApproval(agentId: string, requestId: string) {
  for (const fn of listeners) {
    try { fn(agentId, requestId); } catch { /* ignore listener errors */ }
  }
}

// ---------------------------------------------------------------------------
// Module-level Promise coordination — shared across ALL ApprovalService instances.
// This is critical: the chat route creates requests (waitForResponse) and the
// permissions route resolves them (respondToRequest), potentially with different
// per-project DB instances. The Promise map MUST be process-global.
// ---------------------------------------------------------------------------

const _pendingResolvers = new Map<string, {
  resolve: (result: { approved: boolean; timedOut: boolean }) => void;
  timer: ReturnType<typeof setTimeout>;
}>();

export async function waitForApprovalResponse(
  requestId: string,
  timeoutMs = 300_000,
): Promise<{ approved: boolean; timedOut: boolean }> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      _pendingResolvers.delete(requestId);
      resolve({ approved: false, timedOut: true });
    }, timeoutMs);
    _pendingResolvers.set(requestId, { resolve, timer });
  });
}

export function cancelApprovalWait(requestId: string) {
  const pending = _pendingResolvers.get(requestId);
  if (pending) {
    clearTimeout(pending.timer);
    _pendingResolvers.delete(requestId);
    pending.resolve({ approved: false, timedOut: false });
  }
}

export function resolvePendingApproval(
  requestId: string,
  approved: boolean,
): boolean {
  const pending = _pendingResolvers.get(requestId);
  if (pending) {
    clearTimeout(pending.timer);
    _pendingResolvers.delete(requestId);
    pending.resolve({ approved, timedOut: false });
    return true;
  }
  return false;
}

/** Clear all in-memory pending resolvers (called on server restart). */
export function clearAllPendingApprovals() {
  for (const [id, { resolve, timer }] of _pendingResolvers) {
    clearTimeout(timer);
    resolve({ approved: false, timedOut: true });
  }
  _pendingResolvers.clear();
}

// ---------------------------------------------------------------------------
// ApprovalService
// ---------------------------------------------------------------------------

export class ApprovalService {
  private permissionService: PermissionService;

  constructor(private readonly db: Database) {
    this.permissionService = new PermissionService(db);
  }

  /**
   * On server startup, resolve all in-memory pending resolvers as "timed out"
   * and mark all DB pending requests as "rejected" with a system note.
   * Prevents orphaned requests from lingering forever.
   */
  async cleanupOrphanedRequests() {
    // Resolve any in-memory resolvers (from previous server instance)
    clearAllPendingApprovals();

    // Mark all DB pending requests as rejected
    const pending = await this.db
      .select({ id: permissionRequests.id })
      .from(permissionRequests)
      .where(eq(permissionRequests.status, "pending"));

    if (pending.length > 0) {
      await this.db
        .update(permissionRequests)
        .set({
          status: "rejected",
          userNote: "Server restarted — request auto-rejected",
          updatedAt: Date.now(),
        })
        .where(eq(permissionRequests.status, "pending"));
      console.log(`Cleaned up ${pending.length} orphaned pending approval request(s)`);
    }
  }

  /**
   * Start a periodic timer to clean up old resolved requests.
   * Call once on server startup.
   */
  startCleanupScheduler(intervalMs = 60 * 60 * 1000) {
    setInterval(() => {
      this.cleanupOldRequests().catch((err) =>
        console.error("Periodic approval cleanup failed:", err),
      );
    }, intervalMs);
  }

  /**
   * Cancel a pending approval wait (e.g., when SSE connection drops).
   * Delegates to module-level cancelApprovalWait.
   */
  cancelWait(requestId: string) {
    cancelApprovalWait(requestId);
  }

  /**
   * Create a new pending approval request.
   * Called when PermissionService returns "ask" for a tool invocation.
   */
  async createRequest(data: ApprovalRequestData): Promise<string> {
    const id = randomUUID();
    const now = Date.now();

    await this.db.insert(permissionRequests).values({
      id,
      agentId: data.agentId,
      toolName: data.toolName,
      toolArguments: JSON.stringify(data.toolArguments),
      description: data.description,
      status: "pending",
      remember: false,
      createdAt: now,
      updatedAt: now,
    });

    // Notify listeners (SSE push)
    emitApproval(data.agentId, id);

    return id;
  }

  /**
   * Get all pending approval requests for an agent.
   */
  async getPendingRequests(agentId: string) {
    return this.db
      .select()
      .from(permissionRequests)
      .where(and(eq(permissionRequests.agentId, agentId), eq(permissionRequests.status, "pending")))
      .orderBy(desc(permissionRequests.createdAt));
  }

  /**
   * Get all pending approval requests across all agents in a project.
   * Used by the frontend to show approval badges on agent nodes.
   */
  async getAllPendingForProject(projectId: string) {
    // Get all agent IDs in the project using drizzle query builder
    const agentRows = await this.db.select({ id: agents.id }).from(agents).where(eq(agents.projectId, projectId));
    const agentIds = agentRows.map((r) => r.id);

    if (agentIds.length === 0) return [];

    // Get pending requests for these agents
    const results: any[] = [];
    for (const agentId of agentIds) {
      const pending = await this.getPendingRequests(agentId);
      results.push(...pending);
    }
    return results;
  }

  /**
   * Process an approval response from the user.
   *
   * Note on atomicity: The check-then-update pattern below is safe under SQLite
   * because SQLite serializes all writes (single-writer model). No concurrent
   * write can interleave between the SELECT and UPDATE. If this is ever migrated
   * to a multi-writer DB (PostgreSQL), wrap in a transaction with SELECT FOR UPDATE.
   *
   * @returns The original request data (tool name + args) so the caller can
   *          proceed with execution or abort.
   */
  async respondToRequest(data: ApprovalResponseData) {
    const rows = await this.db
      .select()
      .from(permissionRequests)
      .where(eq(permissionRequests.id, data.requestId));

    if (rows.length === 0) {
      return { ok: false, reason: "Request not found" };
    }

    const request = rows[0];
    if (request.status !== "pending") {
      return { ok: false, reason: `Request already ${request.status}` };
    }

    const newStatus = data.approved ? "approved" : "rejected";

    await this.db
      .update(permissionRequests)
      .set({
        status: newStatus,
        remember: data.remember || false,
        userNote: data.userNote || null,
        updatedAt: Date.now(),
      })
      .where(eq(permissionRequests.id, data.requestId));

    // If approved with "remember", add a permanent allow rule
    if (data.approved && data.remember) {
      await this.permissionService.addAllowRule(request.agentId, request.toolName);
    }

    // Resolve the in-flight Promise so AgentRuntime can proceed
    resolvePendingApproval(data.requestId, data.approved);

    console.log(
      `[PERM] APPROVAL ${data.approved ? "GRANTED" : "DENIED"} ` +
      `agent=${request.agentId.slice(0, 8)} tool=${request.toolName} ` +
      `remember=${data.remember || false}` +
      (data.userNote ? ` note="${data.userNote}"` : ""),
    );

    return {
      ok: true,
      approved: data.approved,
      agentId: request.agentId,
      toolName: request.toolName,
      toolArguments: JSON.parse(request.toolArguments),
    };
  }

  /**
   * Get a single request by ID.
   */
  async getRequest(requestId: string) {
    const rows = await this.db
      .select()
      .from(permissionRequests)
      .where(eq(permissionRequests.id, requestId));
    return rows[0] || null;
  }

  /**
   * Clean up old resolved requests (older than 7 days).
   */
  async cleanupOldRequests() {
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    // Delete all non-pending requests older than cutoff
    const old = await this.db
      .select({ id: permissionRequests.id })
      .from(permissionRequests)
      .where(and(ne(permissionRequests.status, "pending"), lt(permissionRequests.createdAt, cutoff)));
    for (const row of old) {
      await this.db.delete(permissionRequests).where(eq(permissionRequests.id, row.id));
    }
  }
}
