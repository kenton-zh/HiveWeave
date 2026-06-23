import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";
import { sql } from "drizzle-orm";
import * as allSchema from "./schema/index.js";
import { mkdirSync, existsSync } from "fs";
import { dirname, resolve, join } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const META_DB_PATH = process.env.DB_PATH || resolve(__dirname, "../data/hiveweave.db");

// ---------------------------------------------------------------------------
// Factory: create a Drizzle database instance at any path
// ---------------------------------------------------------------------------

export function createDb(dbPath: string, schemaObj: typeof allSchema = allSchema) {
  mkdirSync(dirname(dbPath), { recursive: true });
  const sqlite = new Database(dbPath);
  sqlite.pragma("journal_mode = WAL");
  sqlite.pragma("foreign_keys = ON");
  return drizzle(sqlite, { schema: schemaObj });
}

// ---------------------------------------------------------------------------
// Meta DB (global) — projects + agent_templates
// ---------------------------------------------------------------------------

mkdirSync(dirname(META_DB_PATH), { recursive: true });

const metaSqlite = new Database(META_DB_PATH);
metaSqlite.pragma("journal_mode = WAL");
metaSqlite.pragma("foreign_keys = ON");

export const db = drizzle(metaSqlite, { schema: allSchema });
export type MetaDatabase = typeof db;

// ---------------------------------------------------------------------------
// Meta DB initialization — llm_models table + seed
// ---------------------------------------------------------------------------

metaSqlite.exec(`
  CREATE TABLE IF NOT EXISTS llm_models (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_id TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key TEXT NOT NULL,
    context_window INTEGER NOT NULL DEFAULT 128000,
    max_output_tokens INTEGER NOT NULL DEFAULT 8192,
    supports_thinking INTEGER NOT NULL DEFAULT 0,
    default_reasoning_effort TEXT,
    temperature TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
  )
`);


metaSqlite.exec(`
  CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    workspace_path TEXT,
    org_paradigm TEXT,
    charter_json TEXT,
    created_at INTEGER NOT NULL
  )
`);
try { metaSqlite.exec(`ALTER TABLE llm_models ADD COLUMN provider TEXT NOT NULL DEFAULT 'openai-compatible'`); } catch { /* already exists */ }
try { metaSqlite.exec(`ALTER TABLE llm_models ADD COLUMN supports_images INTEGER NOT NULL DEFAULT 0`); } catch { /* already exists */ }
try { metaSqlite.exec(`ALTER TABLE projects ADD COLUMN charter_json TEXT`); } catch { /* already exists */ }

/** Seed the default model if the registry is empty. */
export function seedDefaultModel() {
  const count = metaSqlite.prepare("SELECT COUNT(*) as cnt FROM llm_models").get() as any;
  if (count.cnt > 0) return;

  const now = Date.now();
  const id = crypto.randomUUID();
  metaSqlite.prepare(`
    INSERT INTO llm_models (id, name, model_id, base_url, api_key, context_window, max_output_tokens, supports_thinking, default_reasoning_effort, temperature, is_active, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `).run(
    id,
    "DeepSeek V4 Flash Free",
    "deepseek-v4-flash-free",
    "https://opencode.ai/zen/v1",
    "sk-SLUzQJ8EvnMP4rySJVo85m3njIAcMZvbn7aJSHDE5nk9vIRz7ikvwohstbrYqQ2U",
    128000,
    8192,
    0,
    null,
    null,
    1,
    now,
    now,
  );
  console.log(`[DB] Seeded default model: DeepSeek V4 Flash Free (${id})`);
}

/**
 * Initialize all tables in a per-project database.
 * Uses CREATE TABLE IF NOT EXISTS so it's safe to call on existing DBs.
 */
function initProjectDbTables(projectDb: ReturnType<typeof createDb>) {
  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS projects (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      description TEXT,
      workspace_path TEXT,
      org_paradigm TEXT,
      charter_json TEXT,
      created_at INTEGER NOT NULL
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS agent_templates (
      id TEXT PRIMARY KEY,
      source TEXT NOT NULL DEFAULT 'agency-agents',
      division TEXT NOT NULL DEFAULT '',
      name TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'specialist',
      color TEXT NOT NULL DEFAULT '',
      emoji TEXT NOT NULL DEFAULT '',
      vibe TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      prompt_body TEXT NOT NULL DEFAULT '',
      original_file TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS modules (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      parent_module_id TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      current_agent_id TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS agents (
      id TEXT PRIMARY KEY,
      short_id TEXT UNIQUE,
      project_id TEXT,
      name TEXT NOT NULL,
      role TEXT NOT NULL,
      parent_id TEXT,
      module_id TEXT REFERENCES modules(id),
      status TEXT NOT NULL DEFAULT 'created',
      goal TEXT NOT NULL DEFAULT '',
      backstory TEXT NOT NULL DEFAULT '',
      skills TEXT NOT NULL DEFAULT '[]',
      permission_type TEXT NOT NULL DEFAULT 'executor',
      permission_mode TEXT NOT NULL DEFAULT 'full',
      allowed_tools TEXT NOT NULL DEFAULT '[]',
      denied_tools TEXT NOT NULL DEFAULT '[]',
      ask_tools TEXT NOT NULL DEFAULT '[]',
      mcp_servers TEXT NOT NULL DEFAULT '[]',
      bound_skills TEXT NOT NULL DEFAULT '[]',
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      last_seen_log_at INTEGER
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS memories (
      id TEXT PRIMARY KEY,
      agent_id TEXT,
      scope TEXT NOT NULL,
      module_id TEXT,
      type TEXT NOT NULL,
      content TEXT NOT NULL,
      source_agent_id TEXT,
      metadata TEXT NOT NULL DEFAULT '{}',
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS work_logs (
      id TEXT PRIMARY KEY,
      agent_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      type TEXT NOT NULL,
      summary TEXT NOT NULL,
      details TEXT NOT NULL DEFAULT '{}',
      created_at INTEGER NOT NULL
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS handoffs (
      id TEXT PRIMARY KEY,
      from_agent_id TEXT NOT NULL,
      to_agent_id TEXT,
      module_id TEXT,
      summary TEXT NOT NULL,
      memory_snapshot_id TEXT,
      status TEXT NOT NULL DEFAULT 'pending',
      expect_report INTEGER NOT NULL DEFAULT 0,
      reported_up INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL,
      updated_at INTEGER
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS merges (
      id TEXT PRIMARY KEY,
      source_agent_ids TEXT NOT NULL DEFAULT '[]',
      target_agent_id TEXT NOT NULL,
      summary TEXT NOT NULL,
      conflicts TEXT NOT NULL DEFAULT '[]',
      resolution TEXT NOT NULL DEFAULT '{}',
      created_at INTEGER NOT NULL
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS inbox (
      id TEXT PRIMARY KEY,
      from_agent_id TEXT NOT NULL,
      to_agent_id TEXT NOT NULL,
      message TEXT NOT NULL,
      message_type TEXT NOT NULL DEFAULT 'superior',
      expect_report INTEGER NOT NULL DEFAULT 0,
      read INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS chat_messages (
      id TEXT PRIMARY KEY,
      agent_id TEXT NOT NULL,
      role TEXT NOT NULL,
      content TEXT NOT NULL,
      tool_calls TEXT NOT NULL DEFAULT '[]',
      is_background INTEGER NOT NULL DEFAULT 0,
      is_read INTEGER NOT NULL DEFAULT 1,
      created_at INTEGER NOT NULL
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS permission_requests (
      id TEXT PRIMARY KEY,
      agent_id TEXT NOT NULL REFERENCES agents(id),
      tool_name TEXT NOT NULL,
      tool_arguments TEXT NOT NULL DEFAULT '{}',
      description TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending',
      remember INTEGER NOT NULL DEFAULT 0,
      user_note TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS personnel_records (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
      position TEXT NOT NULL DEFAULT '',
      department TEXT NOT NULL DEFAULT '',
      responsibilities TEXT NOT NULL DEFAULT '',
      notes TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'active',
      created_by TEXT NOT NULL REFERENCES agents(id),
      updated_by TEXT NOT NULL REFERENCES agents(id),
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    )
  `);

  projectDb.run(sql`
    CREATE TABLE IF NOT EXISTS conversation_turns (
      id TEXT PRIMARY KEY,
      agent_id TEXT NOT NULL,
      turn_index INTEGER NOT NULL,
      raw_messages TEXT NOT NULL,
      approx_tokens INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL
    )
  `);

  // Indexes for common queries
  projectDb.run(sql`CREATE INDEX IF NOT EXISTS idx_conv_turns_agent ON conversation_turns (agent_id, turn_index)`);
  projectDb.run(sql`CREATE INDEX IF NOT EXISTS idx_chat_messages_agent ON chat_messages (agent_id)`);
  projectDb.run(sql`CREATE INDEX IF NOT EXISTS idx_agents_project ON agents (project_id)`);
  projectDb.run(sql`CREATE INDEX IF NOT EXISTS idx_work_logs_agent ON work_logs (agent_id)`);

  // ── Migration: add model_id and reasoning_effort to agents ───────────
  // SQLite lacks ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so we try/catch.
  try { projectDb.run(sql`ALTER TABLE agents ADD COLUMN model_id TEXT`); } catch { /* already exists */ }
  try { projectDb.run(sql`ALTER TABLE agents ADD COLUMN reasoning_effort TEXT`); } catch { /* already exists */ }
  try { projectDb.run(sql`ALTER TABLE chat_messages ADD COLUMN is_streaming INTEGER NOT NULL DEFAULT 0`); } catch { /* already exists */ }
  try { projectDb.run(sql`ALTER TABLE chat_messages ADD COLUMN team_from_agent_id TEXT`); } catch { /* already exists */ }
  try { projectDb.run(sql`ALTER TABLE chat_messages ADD COLUMN team_to_agent_id TEXT`); } catch { /* already exists */ }
  try { projectDb.run(sql`ALTER TABLE chat_messages ADD COLUMN images TEXT`); } catch { /* already exists */ }
  try { projectDb.run(sql`ALTER TABLE projects ADD COLUMN charter_json TEXT`); } catch { /* already exists */ }

  // ── Migration: remove cross-DB FK constraints ────────────────────────
  // agents.project_id and personnel_records.project_id originally referenced
  // projects(id), but projects only lives in the meta DB. This FK can never
  // be satisfied in a per-project DB, so we rebuild the tables without it.
  const agentFks = projectDb.all(sql`PRAGMA foreign_key_list(agents)`) as any[];
  if (agentFks.some((fk: any) => fk.table === "projects")) {
    projectDb.run(sql`
      CREATE TABLE agents_new (
        id TEXT PRIMARY KEY,
        short_id TEXT UNIQUE,
        project_id TEXT,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        parent_id TEXT,
        module_id TEXT REFERENCES modules(id),
        status TEXT NOT NULL DEFAULT 'created',
        goal TEXT NOT NULL DEFAULT '',
        backstory TEXT NOT NULL DEFAULT '',
        skills TEXT NOT NULL DEFAULT '[]',
        permission_type TEXT NOT NULL DEFAULT 'executor',
        permission_mode TEXT NOT NULL DEFAULT 'full',
        allowed_tools TEXT NOT NULL DEFAULT '[]',
        denied_tools TEXT NOT NULL DEFAULT '[]',
        ask_tools TEXT NOT NULL DEFAULT '[]',
        mcp_servers TEXT NOT NULL DEFAULT '[]',
        bound_skills TEXT NOT NULL DEFAULT '[]',
        model_id TEXT,
        reasoning_effort TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        last_seen_log_at INTEGER
      )
    `);
    projectDb.run(sql`INSERT INTO agents_new (id, short_id, project_id, name, role, parent_id, module_id, status, goal, backstory, skills, permission_type, permission_mode, allowed_tools, denied_tools, ask_tools, mcp_servers, bound_skills, model_id, reasoning_effort, created_at, updated_at, last_seen_log_at) SELECT id, short_id, project_id, name, role, parent_id, module_id, status, goal, backstory, skills, permission_type, permission_mode, allowed_tools, denied_tools, ask_tools, mcp_servers, bound_skills, model_id, reasoning_effort, created_at, updated_at, last_seen_log_at FROM agents`);
    projectDb.run(sql`DROP TABLE agents`);
    projectDb.run(sql`ALTER TABLE agents_new RENAME TO agents`);
    projectDb.run(sql`CREATE INDEX IF NOT EXISTS idx_agents_project ON agents (project_id)`);
  }

  const prFks = projectDb.all(sql`PRAGMA foreign_key_list(personnel_records)`) as any[];
  if (prFks.some((fk: any) => fk.table === "projects")) {
    projectDb.run(sql`
      CREATE TABLE personnel_records_new (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
        position TEXT NOT NULL DEFAULT '',
        department TEXT NOT NULL DEFAULT '',
        responsibilities TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        created_by TEXT NOT NULL REFERENCES agents(id),
        updated_by TEXT NOT NULL REFERENCES agents(id),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      )
    `);
    projectDb.run(sql`INSERT INTO personnel_records_new SELECT * FROM personnel_records`);
    projectDb.run(sql`DROP TABLE personnel_records`);
    projectDb.run(sql`ALTER TABLE personnel_records_new RENAME TO personnel_records`);
  }
}

// ---------------------------------------------------------------------------
// Per-project DB cache — each project gets its own SQLite in .hiveweave/
// ---------------------------------------------------------------------------

const projectDbCache = new Map<string, ReturnType<typeof createDb>>();

const HIVEWEAVE_DIR = ".hiveweave";
const PROJECT_DB_FILE = "data.db";

/**
 * Get or create a per-project database at `<workspacePath>/.hiveweave/data.db`.
 * Creates the .hiveweave directory and initializes tables if they don't exist.
 */
export function ensureProjectDb(workspacePath: string): ReturnType<typeof createDb> {
  const normalized = workspacePath.replace(/\\/g, "/");
  if (projectDbCache.has(normalized)) {
    return projectDbCache.get(normalized)!;
  }

  const hwDir = join(workspacePath, HIVEWEAVE_DIR);
  mkdirSync(hwDir, { recursive: true });

  const dbPath = join(hwDir, PROJECT_DB_FILE);
  const projectDb = createDb(dbPath, allSchema);

  // Initialize tables for new or existing per-project DBs (idempotent)
  initProjectDbTables(projectDb);

  projectDbCache.set(normalized, projectDb);
  return projectDb;
}

/**
 * Get the .hiveweave directory path for a workspace.
 */
export function getHiveWeaveDir(workspacePath: string): string {
  return join(workspacePath, HIVEWEAVE_DIR);
}

/**
 * Close the per-project DB connection and evict it from cache.
 * Call this before deleting or moving the .hiveweave directory to avoid
 * file-lock issues on Windows (SQLite holds .db-wal/.db-shm handles).
 */
export function evictProjectDb(workspacePath: string): void {
  const normalized = workspacePath.replace(/\\/g, "/");
  const cached = projectDbCache.get(normalized);
  if (cached) {
    try { (cached as any).session?.client?.close(); } catch { /* already closed */ }
    projectDbCache.delete(normalized);
  }
}

// Re-export the full schema for route handlers that need direct table references
export { allSchema };
export type ProjectDatabase = ReturnType<typeof createDb>;
// Legacy alias for backward compatibility
export type Database = MetaDatabase;

// ---------------------------------------------------------------------------
// Agent Registry — maps agentId → workspacePath for cross-project lookups.
// Route handlers receive agentId but need the per-project DB. This registry
// resolves the agentId to the workspace path, which then resolves to the DB.
// ---------------------------------------------------------------------------

const agentRegistry = new Map<string, string>(); // agentId → workspacePath

/** Register an agent to a workspace path (called when agent is created). */
export function registerAgent(agentId: string, workspacePath: string) {
  agentRegistry.set(agentId, workspacePath.replace(/\\/g, "/"));
}

/** Look up the workspace path for an agent. Returns undefined if not registered. */
export function lookupAgentWorkspace(agentId: string): string | undefined {
  return agentRegistry.get(agentId);
}

/** Remove all agent registrations for a workspace (called when project is deleted). */
export function unregisterProjectAgents(workspacePath: string) {
  const normalized = workspacePath.replace(/\\/g, "/");
  for (const [id, ws] of agentRegistry) {
    if (ws === normalized) agentRegistry.delete(id);
  }
}

/** Get the per-project DB for an agent by looking up its workspace path. */
export function getProjectDbForAgent(agentId: string): ReturnType<typeof createDb> | null {
  const ws = lookupAgentWorkspace(agentId);
  if (!ws) return null;
  try { return ensureProjectDb(ws); } catch { return null; }
}

/** Bulk-register agents from a project (called at startup). */
export function registerProjectAgents(workspacePath: string, agentIds: string[]) {
  const normalized = workspacePath.replace(/\\/g, "/");
  for (const id of agentIds) agentRegistry.set(id, normalized);
}
