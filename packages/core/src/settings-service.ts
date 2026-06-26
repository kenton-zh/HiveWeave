import { eq } from "drizzle-orm";
import { globalSettings } from "@hiveweave/db";
import type { MetaDatabase } from "@hiveweave/db";

/**
 * Service for reading/writing global application settings (key-value store).
 * Lives in the meta DB — shared across all projects.
 */
export class SettingsService {
  constructor(private readonly metaDb: MetaDatabase) {}

  /** Get a single setting value. Returns defaultValue if not set. */
  async get(key: string, defaultValue = ""): Promise<string> {
    const rows = await this.metaDb
      .select({ value: globalSettings.value })
      .from(globalSettings)
      .where(eq(globalSettings.key, key))
      .limit(1);
    return rows.length > 0 ? rows[0].value : defaultValue;
  }

  /** Get all settings as a key-value map. */
  async getAll(): Promise<Record<string, string>> {
    const rows = await this.metaDb.select().from(globalSettings);
    const map: Record<string, string> = {};
    for (const row of rows) {
      map[row.key] = row.value;
    }
    return map;
  }

  /** Set a single setting (upsert). */
  async set(key: string, value: string): Promise<void> {
    const now = Date.now();
    const existing = await this.metaDb
      .select({ key: globalSettings.key })
      .from(globalSettings)
      .where(eq(globalSettings.key, key))
      .limit(1);

    if (existing.length > 0) {
      await this.metaDb
        .update(globalSettings)
        .set({ value, updatedAt: now })
        .where(eq(globalSettings.key, key));
    } else {
      await this.metaDb.insert(globalSettings).values({ key, value, updatedAt: now });
    }
  }

  /** Set multiple settings at once (upsert each). */
  async setMany(settings: Record<string, string>): Promise<void> {
    for (const [key, value] of Object.entries(settings)) {
      await this.set(key, value);
    }
  }
}
