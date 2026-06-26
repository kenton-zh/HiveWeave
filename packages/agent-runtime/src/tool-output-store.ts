/**
 * Tool Output Store — truncation-to-file service for large tool outputs.
 *
 * Aligned with OpenCode's truncate.ts pattern:
 *   - When a tool output exceeds line/byte limits, the FULL output is written
 *     to a temp directory and a truncated preview is returned with a file path hint.
 *   - The agent can then use read_file / grep tools to inspect the full output
 *     on demand, keeping the main context lean.
 *   - A background cleanup deletes files older than 7 days.
 *
 * Unlike OpenCode (which uses Effect), this is plain Node.js — no Effect dependency.
 */

import { mkdirSync, writeFileSync, readdirSync, unlinkSync, statSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";
import { randomBytes } from "crypto";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Default max lines before truncation kicks in. */
export const DEFAULT_MAX_LINES = 2_000;

/** Default max bytes before truncation kicks in (50 KB). */
export const DEFAULT_MAX_BYTES = 50 * 1024;

/** Retention period for saved truncation files (7 days). */
const RETENTION_MS = 7 * 24 * 60 * 60 * 1000;

/** Cleanup interval (1 hour). */
const CLEANUP_INTERVAL_MS = 60 * 60 * 1000;

/** Directory name under OS temp. */
const DIR_NAME = "hiveweave-truncations";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface TruncateOptions {
  /** Override max lines (default: DEFAULT_MAX_LINES). */
  maxLines?: number;
  /** Override max bytes (default: DEFAULT_MAX_BYTES). */
  maxBytes?: number;
  /** Which end to show in the preview: "head" (default) or "tail". */
  direction?: "head" | "tail";
}

export interface TruncateResult {
  /** The content to store in conversation history (preview + hint). */
  content: string;
  /** Whether truncation occurred. */
  truncated: boolean;
  /** If truncated, the file path where the full output was saved. */
  outputPath?: string;
}

// ---------------------------------------------------------------------------
// ToolOutputStore
// ---------------------------------------------------------------------------

export class ToolOutputStore {
  private readonly dataDir: string;
  private cleanupTimer: ReturnType<typeof setInterval> | null = null;

  constructor(dataDir?: string) {
    this.dataDir = dataDir ?? join(tmpdir(), DIR_NAME);
    try {
      mkdirSync(this.dataDir, { recursive: true });
    } catch {
      // Directory may already exist — that's fine.
    }
    // Run cleanup on construction + hourly
    this.cleanup();
    this.cleanupTimer = setInterval(() => this.cleanup(), CLEANUP_INTERVAL_MS);
    // Prevent timer from keeping the process alive
    this.cleanupTimer.unref?.();
  }

  /**
   * Stop the background cleanup timer. Call on server shutdown.
   */
  destroy(): void {
    if (this.cleanupTimer) {
      clearInterval(this.cleanupTimer);
      this.cleanupTimer = null;
    }
  }

  /**
   * Truncate a tool output if it exceeds limits, saving the full output to disk.
   *
   * Returns a TruncateResult with:
   *   - `content`: preview (head or tail) + file path hint if truncated
   *   - `truncated`: whether truncation occurred
   *   - `outputPath`: file path of saved full output (only if truncated)
   */
  truncateAndSave(text: string, options?: TruncateOptions): TruncateResult {
    if (!text) return { content: text, truncated: false };

    const maxLines = options?.maxLines ?? DEFAULT_MAX_LINES;
    const maxBytes = options?.maxBytes ?? DEFAULT_MAX_BYTES;
    const direction = options?.direction ?? "head";

    const lines = text.split("\n");
    const totalBytes = Buffer.byteLength(text, "utf-8");

    // No truncation needed
    if (lines.length <= maxLines && totalBytes <= maxBytes) {
      return { content: text, truncated: false };
    }

    // Build preview (head or tail)
    const preview: string[] = [];
    let bytes = 0;
    let hitLimit = false;

    if (direction === "head") {
      for (let i = 0; i < lines.length && i < maxLines; i++) {
        const lineBytes = Buffer.byteLength(lines[i], "utf-8") + (i > 0 ? 1 : 0);
        if (bytes + lineBytes > maxBytes) {
          hitLimit = true;
          break;
        }
        preview.push(lines[i]);
        bytes += lineBytes;
      }
    } else {
      for (let i = lines.length - 1; i >= 0 && preview.length < maxLines; i--) {
        const lineBytes = Buffer.byteLength(lines[i], "utf-8") + (preview.length > 0 ? 1 : 0);
        if (bytes + lineBytes > maxBytes) {
          hitLimit = true;
          break;
        }
        preview.unshift(lines[i]);
        bytes += lineBytes;
      }
    }

    const removed = hitLimit
      ? totalBytes - bytes
      : (lines.length - preview.length);
    const unit = hitLimit ? "bytes" : "lines";

    // Save full output to disk
    const outputPath = this.save(text);

    const previewText = preview.join("\n");
    const hint = `[Tool output truncated: ${removed} ${unit} removed. Full output saved to: ${outputPath}. ` +
      `Use hiveweave__read_file or hiveweave__grep to inspect specific sections.]`;

    const content = direction === "head"
      ? `${previewText}\n\n... [${removed} ${unit} truncated] ...\n\n${hint}`
      : `... [${removed} ${unit} truncated] ...\n\n${hint}\n\n${previewText}`;

    return { content, truncated: true, outputPath };
  }

  /**
   * Save text content to the truncation directory. Returns the file path.
   */
  private save(text: string): string {
    const id = `tool_${Date.now()}_${randomBytes(4).toString("hex")}.txt`;
    const filePath = join(this.dataDir, id);
    writeFileSync(filePath, text, "utf-8");
    return filePath;
  }

  /**
   * Delete truncation files older than the retention period.
   */
  cleanup(): void {
    const cutoff = Date.now() - RETENTION_MS;
    try {
      const entries = readdirSync(this.dataDir);
      for (const entry of entries) {
        if (!entry.startsWith("tool_")) continue;
        const filePath = join(this.dataDir, entry);
        try {
          const stat = statSync(filePath);
          if (stat.mtimeMs < cutoff) {
            unlinkSync(filePath);
          }
        } catch {
          // File may have been deleted by another process — ignore.
        }
      }
    } catch {
      // Directory may not exist yet — ignore.
    }
  }
}
