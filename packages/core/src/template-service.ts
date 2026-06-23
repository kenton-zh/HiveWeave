import { agentTemplates } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, and, like, sql } from "drizzle-orm";

export interface TemplateFilter {
  source?: string;
  division?: string;
  role?: string;
  search?: string;
}

/**
 * TemplateService — manages the agent template catalog.
 * Templates are pre-built agent personas that HR can browse and use
 * to quickly create new agents with well-defined roles and prompts.
 */
export class TemplateService {
  constructor(private readonly db: Database) {}

  /** List templates with optional filters. Returns lightweight records (no prompt_body). */
  async listTemplates(filters?: TemplateFilter) {
    const conditions = [];

    if (filters?.source) {
      conditions.push(eq(agentTemplates.source, filters.source));
    }
    if (filters?.division) {
      conditions.push(eq(agentTemplates.division, filters.division));
    }
    if (filters?.role) {
      conditions.push(eq(agentTemplates.role, filters.role));
    }
    if (filters?.search) {
      const term = `%${filters.search}%`;
      conditions.push(
        sql`(${agentTemplates.name} LIKE ${term} OR ${agentTemplates.vibe} LIKE ${term} OR ${agentTemplates.description} LIKE ${term})`
      );
    }

    const where = conditions.length > 0 ? and(...conditions) : undefined;

    // Exclude prompt_body from list queries to keep responses small
    return this.db
      .select({
        id: agentTemplates.id,
        source: agentTemplates.source,
        division: agentTemplates.division,
        name: agentTemplates.name,
        role: agentTemplates.role,
        color: agentTemplates.color,
        emoji: agentTemplates.emoji,
        vibe: agentTemplates.vibe,
        description: agentTemplates.description,
        originalFile: agentTemplates.originalFile,
      })
      .from(agentTemplates)
      .where(where)
      .orderBy(agentTemplates.division, agentTemplates.name);
  }

  /** Get a single template with full prompt_body. */
  async getTemplate(templateId: string) {
    const rows = await this.db
      .select()
      .from(agentTemplates)
      .where(eq(agentTemplates.id, templateId));
    return rows[0] || null;
  }

  /** Get all divisions with template counts. */
  async getDivisions() {
    return this.db
      .select({
        division: agentTemplates.division,
        count: sql<number>`count(*)`.mapWith(Number),
      })
      .from(agentTemplates)
      .groupBy(agentTemplates.division)
      .orderBy(agentTemplates.division);
  }
}
