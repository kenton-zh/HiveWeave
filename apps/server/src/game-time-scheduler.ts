import { db, projects, ensureProjectDb, agents, handoffs, chatMessages } from "@hiveweave/db";
import { eq, and, or, desc } from "drizzle-orm";
import {
  getGameTimeService,
  AlarmService,
  OrgService,
  InboxService,
  ProjectService,
} from "@hiveweave/core";

type TriggerAgentFn = (agentId: string) => Promise<void>;

let triggerAgent: TriggerAgentFn | null = null;

/** Cooldown per agent — don't send duplicate stall alerts within this window. */
const STALL_ALERT_COOLDOWN_MS = 10 * 60 * 1000; // 10 minutes
const lastStallAlert = new Map<string, number>(); // agentId → timestamp

export function registerAlarmTrigger(fn: TriggerAgentFn): void {
  triggerAgent = fn;
}

/**
 * Tick project time persistence and fire due alarms for all active projects.
 */
export async function runGameTimeTick(): Promise<void> {
  const gameTimeService = getGameTimeService(db);
  const projectService = new ProjectService(db);
  const allProjects = await projectService.listProjects();

  for (const proj of allProjects) {
    if (!proj.workspacePath) continue;

    let projectDb;
    try {
      projectDb = ensureProjectDb(proj.workspacePath);
    } catch (err: any) {
      console.error(`[GameTime] skip project ${proj.id}: ${err.code || err.message?.slice(0, 80)}`);
      continue;
    }

    await gameTimeService.initProject(proj.id);
    await gameTimeService.persistProject(proj.id);

    const currentGameSeconds = gameTimeService.getCurrentGameSeconds(proj.id);
    const alarmService = new AlarmService(projectDb);
    const inboxService = new InboxService(projectDb);
    const orgService = new OrgService(projectDb, proj.workspacePath);

    const fired = await alarmService.processDueAlarms(
      proj.id,
      currentGameSeconds,
      inboxService,
      orgService,
    );

    if (fired.length > 0 && triggerAgent) {
      const triggered = new Set<string>();
      for (const alarm of fired) {
        if (triggered.has(alarm.toAgentId)) continue;
        triggered.add(alarm.toAgentId);
        triggerAgent(alarm.toAgentId).catch((err) => {
          console.error(`[GameTime] Alarm auto-trigger failed for ${alarm.toAgentId}:`, err);
        });
      }
    }

    // --- Exception Escalation: Timeout Detection ---
    // Check for agents with pending tasks that have stalled (> 15 min no activity)
    try {
      const STALL_TIMEOUT_MS = 15 * 60 * 1000; // 15 minutes
      const now = Date.now();

      // Find active agents with pending handoff tasks
      const stalledRows = await projectDb
        .select({
          agentId: agents.id,
          agentName: agents.name,
          agentRole: agents.role,
          agentParentId: agents.parentId,
          handoffId: handoffs.id,
          handoffCreatedAt: handoffs.createdAt,
        })
        .from(agents)
        .innerJoin(handoffs, and(
          eq(handoffs.toAgentId, agents.id),
          eq(handoffs.status, "accepted"),
        ))
        .where(or(eq(agents.status, "active"), eq(agents.status, "created")));

      // Group by agent and find stalled ones
      const stalledAgents = new Map<string, {
        agentId: string;
        agentName: string;
        agentRole: string;
        parentId: string | null;
        handoffCount: number;
        oldestHandoff: number;
      }>();

      for (const row of stalledRows) {
        const existing = stalledAgents.get(row.agentId);
        if (existing) {
          existing.handoffCount++;
          existing.oldestHandoff = Math.min(existing.oldestHandoff, row.handoffCreatedAt);
        } else {
          stalledAgents.set(row.agentId, {
            agentId: row.agentId,
            agentName: row.agentName,
            agentRole: row.agentRole,
            parentId: row.agentParentId,
            handoffCount: 1,
            oldestHandoff: row.handoffCreatedAt,
          });
        }
      }

      for (const [, stalled] of stalledAgents) {
        // Check last message activity
        let lastMsgTime = stalled.oldestHandoff;
        try {
          const recentMsgs = await projectDb
            .select({ createdAt: chatMessages.createdAt })
            .from(chatMessages)
            .where(eq(chatMessages.agentId, stalled.agentId))
            .orderBy(desc(chatMessages.createdAt))
            .limit(1);
          if (recentMsgs.length > 0 && recentMsgs[0].createdAt > lastMsgTime) {
            lastMsgTime = recentMsgs[0].createdAt;
          }
        } catch { /* chat_messages table may not exist in old DBs */ }

        if (now - lastMsgTime > STALL_TIMEOUT_MS) {
          // Agent has stalled — escalate to superior (with cooldown dedup)
          const superiorId = stalled.parentId;
          const lastAlert = lastStallAlert.get(stalled.agentId) || 0;
          if (superiorId && now - lastAlert > STALL_ALERT_COOLDOWN_MS) {
            lastStallAlert.set(stalled.agentId, now);
            const stallMsg = `⚠️ **Agent Stalled Alert**\n\n**${stalled.agentName}** (${stalled.agentRole}) has been inactive for over 15 minutes with ${stalled.handoffCount} pending task(s).\n\nLast activity: ${new Date(lastMsgTime).toLocaleString()}\n\nPlease investigate or reassign.`;
            try {
              await inboxService.sendMessage(
                "system",
                superiorId,
                stallMsg.slice(0, 2000),
                "alarm",
                false,
                "urgent",
              );
              console.log(`[GameTime] Escalated stalled agent ${stalled.agentName} → superior ${superiorId.slice(0, 8)}`);
            } catch (sendErr: any) {
              console.warn(`[GameTime] Failed to send stall alert:`, sendErr.message);
            }
          } else if (!superiorId) {
            console.log(`[GameTime] Top-level agent ${stalled.agentName} stalled (no superior), consider manual intervention`);
          }
        }
      }
    } catch (err: any) {
      console.warn(`[GameTime] Timeout detection error for project ${proj.id}:`, err.message);
    }
  }
}

export async function initGameTimeForAllProjects(): Promise<void> {
  const gameTimeService = getGameTimeService(db);
  const projectService = new ProjectService(db);
  const allProjects = await projectService.listProjects();
  await gameTimeService.initAllProjects(allProjects.map((p) => p.id));
}

export async function shutdownGameTime(): Promise<void> {
  const gameTimeService = getGameTimeService(db);
  await gameTimeService.persistAll();
}

export function getGameTimeApi(projectId: string) {
  const gameTimeService = getGameTimeService(db);
  return gameTimeService.getSnapshot(projectId);
}
