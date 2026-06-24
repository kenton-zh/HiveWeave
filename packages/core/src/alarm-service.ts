import { scheduledAlarms } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, and, lte } from "drizzle-orm";
import { randomUUID } from "crypto";
import { formatGameTime } from "@hiveweave/shared";
import type { InboxService } from "./inbox-service.js";
import type { OrgService } from "./org-service.js";
import type { GameTimeService } from "./game-time-service.js";

export interface ScheduleAlarmInput {
  projectId: string;
  fromAgentId: string;
  toAgentId: string;
  purpose: string;
  fireAtGameSeconds: number;
}

export interface FiredAlarm {
  alarmId: string;
  toAgentId: string;
  fromAgentId: string;
  purpose: string;
  message: string;
}

/**
 * Manages scheduled alarm messages that fire at a specific project time.
 * When due, delivers a simplified inbox message and returns recipients to auto-trigger.
 */
export class AlarmService {
  constructor(private readonly db: Database) {}

  async schedule(input: ScheduleAlarmInput): Promise<string> {
    const id = randomUUID();
    await this.db.insert(scheduledAlarms).values({
      id,
      projectId: input.projectId,
      fromAgentId: input.fromAgentId,
      toAgentId: input.toAgentId,
      purpose: input.purpose,
      fireAtGameSeconds: input.fireAtGameSeconds,
      status: "pending",
      createdAt: Date.now(),
      firedAt: null,
    });
    return id;
  }

  async cancel(alarmId: string): Promise<boolean> {
    const rows = await this.db
      .select()
      .from(scheduledAlarms)
      .where(eq(scheduledAlarms.id, alarmId));
    const alarm = rows[0];
    if (!alarm || alarm.status !== "pending") return false;
    await this.db
      .update(scheduledAlarms)
      .set({ status: "cancelled" })
      .where(eq(scheduledAlarms.id, alarmId));
    return true;
  }

  async listPendingForAgent(agentId: string) {
    return this.db
      .select()
      .from(scheduledAlarms)
      .where(and(eq(scheduledAlarms.toAgentId, agentId), eq(scheduledAlarms.status, "pending")));
  }

  /**
   * Fire all alarms due at or before currentGameSeconds.
   * Returns list of recipients that should be auto-triggered.
   */
  async processDueAlarms(
    projectId: string,
    currentGameSeconds: number,
    inbox: InboxService,
    org: OrgService,
  ): Promise<FiredAlarm[]> {
    const due = await this.db
      .select()
      .from(scheduledAlarms)
      .where(
        and(
          eq(scheduledAlarms.projectId, projectId),
          eq(scheduledAlarms.status, "pending"),
          lte(scheduledAlarms.fireAtGameSeconds, currentGameSeconds),
        ),
      );

    const fired: FiredAlarm[] = [];
    const now = Date.now();

    for (const alarm of due) {
      const fromAgent = await org.getAgent(alarm.fromAgentId);
      const fromName = fromAgent?.name || alarm.fromAgentId.slice(0, 8);
      const timeLabel = formatGameTime(currentGameSeconds);
      const message =
        `[闹钟提醒] 项目时间：${timeLabel}\n` +
        `设置者：${fromName}\n` +
        `提醒事项：${alarm.purpose}`;

      await inbox.sendMessage(alarm.fromAgentId, alarm.toAgentId, message, "alarm");
      await this.db
        .update(scheduledAlarms)
        .set({ status: "fired", firedAt: now })
        .where(eq(scheduledAlarms.id, alarm.id));

      fired.push({
        alarmId: alarm.id,
        toAgentId: alarm.toAgentId,
        fromAgentId: alarm.fromAgentId,
        purpose: alarm.purpose,
        message,
      });
    }

    return fired;
  }

  async deleteForProject(projectId: string): Promise<void> {
    await this.db.delete(scheduledAlarms).where(eq(scheduledAlarms.projectId, projectId));
  }
}
