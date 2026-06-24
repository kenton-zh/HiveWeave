import { db, projects, ensureProjectDb } from "@hiveweave/db";
import {
  getGameTimeService,
  AlarmService,
  OrgService,
  InboxService,
  ProjectService,
} from "@hiveweave/core";

type TriggerAgentFn = (agentId: string) => Promise<void>;

let triggerAgent: TriggerAgentFn | null = null;

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

    await gameTimeService.initProject(proj.id);
    await gameTimeService.persistProject(proj.id);

    const currentGameSeconds = gameTimeService.getCurrentGameSeconds(proj.id);
    const projectDb = ensureProjectDb(proj.workspacePath);
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
