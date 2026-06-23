/**
 * Migration: Create conversation_turns table.
 *
 * Run with: npx tsx packages/db/scripts/migrate-conversation-turns.ts
 */
import Database from "better-sqlite3";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { mkdirSync } from "fs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const DB_PATH = process.env.DB_PATH || resolve(__dirname, "../data/hiveweave.db");

mkdirSync(dirname(DB_PATH), { recursive: true });

const db = new Database(DB_PATH);
db.pragma("journal_mode = WAL");

console.log(`Migrating: ${DB_PATH}`);

// Create conversation_turns table
db.exec(`
  CREATE TABLE IF NOT EXISTS conversation_turns (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    raw_messages TEXT NOT NULL,
    approx_tokens INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
  )
`);

// Create index for efficient queries by agent + turn order
db.exec(`
  CREATE INDEX IF NOT EXISTS idx_conv_turns_agent
    ON conversation_turns (agent_id, turn_index)
`);

console.log("✓ conversation_turns table created (or already exists)");

db.close();
