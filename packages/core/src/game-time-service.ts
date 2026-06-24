import { projects } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq } from "drizzle-orm";
import {
  buildGameTimeSnapshot,
  formatGameTime,
  realMsToGameSeconds,
  type GameTimeSnapshot,
} from "@hiveweave/shared";

interface SessionState {
  accumulatedGameSeconds: number;
  sessionStartRealMs: number;
}

/**
 * Tracks per-project simulated time. Time advances only while the server process
 * is running; accumulated seconds are persisted to the meta DB on tick/shutdown.
 */
export class GameTimeService {
  private readonly sessions = new Map<string, SessionState>();

  constructor(private readonly metaDb: Database) {}

  async initProject(projectId: string): Promise<void> {
    if (this.sessions.has(projectId)) return;
    const project = await this.metaDb.select().from(projects).where(eq(projects.id, projectId));
    const row = project[0];
    const accumulated = row?.gameTimeAccumulatedSeconds ?? 0;
    this.sessions.set(projectId, {
      accumulatedGameSeconds: accumulated,
      sessionStartRealMs: Date.now(),
    });
  }

  async initAllProjects(projectIds: string[]): Promise<void> {
    for (const id of projectIds) {
      await this.initProject(id);
    }
  }

  getCurrentGameSeconds(projectId: string): number {
    const session = this.sessions.get(projectId);
    if (!session) return 0;
    const elapsedRealMs = Date.now() - session.sessionStartRealMs;
    return session.accumulatedGameSeconds + realMsToGameSeconds(elapsedRealMs);
  }

  getSnapshot(projectId: string): GameTimeSnapshot {
    return buildGameTimeSnapshot(this.getCurrentGameSeconds(projectId));
  }

  async persistProject(projectId: string): Promise<void> {
    const session = this.sessions.get(projectId);
    if (!session) return;
    const current = this.getCurrentGameSeconds(projectId);
    await this.metaDb
      .update(projects)
      .set({ gameTimeAccumulatedSeconds: current })
      .where(eq(projects.id, projectId));
    session.accumulatedGameSeconds = current;
    session.sessionStartRealMs = Date.now();
  }

  async persistAll(): Promise<void> {
    const ids = [...this.sessions.keys()];
    for (const id of ids) {
      await this.persistProject(id);
    }
  }

  formatAt(projectId: string, gameSeconds: number): string {
    return formatGameTime(gameSeconds);
  }
}

let singleton: GameTimeService | null = null;

export function getGameTimeService(metaDb: Database): GameTimeService {
  if (!singleton) singleton = new GameTimeService(metaDb);
  return singleton;
}
