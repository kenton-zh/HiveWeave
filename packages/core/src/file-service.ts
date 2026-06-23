import * as fs from "fs/promises";
import * as path from "path";
import type { Stats, Dirent } from "fs";

/**
 * FileService — provides sandboxed file operations for HiveWeave agents.
 *
 * All paths are validated against the project's workspace directory.
 * Inspired by opencode (view/write/edit) and OpenClaw (read/write/edit/grep/ls).
 *
 * Security layers:
 * - Path traversal prevention (string prefix check)
 * - Symlink resolution (realpath + re-check)
 * - Sensitive file blocking (.env, keys, credentials)
 * - Binary file detection (null-byte probe)
 *
 * Safety features:
 * - Stale-read guard (reject write if file changed since last read)
 * - No-op guard (skip write if content unchanged)
 * - Append mode for write_file
 * - File deletion with safety checks
 */
export class FileService {
  /** Maximum file size to read (250 KB) */
  private static readonly MAX_READ_SIZE = 250 * 1024;
  /** Maximum lines to return in a single read */
  private static readonly MAX_READ_LINES = 2000;
  /** Maximum line length before truncation */
  private static readonly MAX_LINE_LENGTH = 2000;
  /** Maximum files in a directory listing */
  private static readonly MAX_LIST_FILES = 1000;
  /** Maximum search results */
  private static readonly MAX_SEARCH_RESULTS = 100;
  /** Bytes to probe for binary detection */
  private static readonly BINARY_PROBE_SIZE = 8192;
  /** Directories to skip in listings and searches */
  private static readonly IGNORED_DIRS = new Set([
    "node_modules", ".git", ".svn", ".hg", "__pycache__",
    "dist", "build", "target", ".next", ".nuxt", ".turbo",
    ".cache", "coverage", ".idea", ".vscode",
  ]);
  /** Sensitive file patterns — blocked from read/write/edit */
  private static readonly SENSITIVE_PATTERNS = [
    /^\.env(\..+)?$/i,                    // .env, .env.local, .env.production
    /^id_rsa(\.pub)?$/i,                  // SSH private/public keys
    /^id_ed25519(\.pub)?$/i,             // Ed25519 keys
    /^.*\.pem$/i,                          // PEM certificates
    /^.*\.p12$/i,                          // PKCS12 bundles
    /^.*\.pfx$/i,                          // PFX certificates
    /^credentials(\.json)?$/i,            // credentials, credentials.json
    /^.*\.key$/i,                          // Private key files
    /^\.htpasswd$/i,                       // Apache password file
    /^shadow$/i,                           // /etc/shadow
    /^.*\.keystore$/i,                    // Java keystore
    /^token(\.json)?$/i,                  // token files
    /^secrets?(\.json|\.ya?ml)?$/i,       // secrets.json, secret.yml, etc.
    /^\.npmrc$/i,                          // npm config (may contain tokens)
    /^\.pypirc$/i,                         // PyPI credentials
    /^netrc$/i,                            // netrc credentials
    /^\.aws[\\/]credentials$/i,           // AWS credentials
  ];

  /**
   * Stale-read tracking: maps absPath → mtime (ms) at the time of last read.
   * Before writing to an existing file, we check if its mtime has changed
   * since the agent last read it. This prevents overwriting external modifications.
   * (opencode-style stale-read guard)
   */
  private readHistory = new Map<string, number>();

  // ── Path security ──────────────────────────────────────────

  /**
   * Resolve and validate a file path against the workspace root.
   * Step 1: String-level path traversal check.
   * Step 2: Symlink resolution via realpath + re-check.
   */
  private async resolveSafe(workspacePath: string, filePath: string): Promise<string> {
    const wsAbsolute = path.resolve(workspacePath);
    const normalized = path.resolve(workspacePath, filePath);

    // Step 1: String-level prefix check
    if (!normalized.startsWith(wsAbsolute + path.sep) && normalized !== wsAbsolute) {
      throw new Error(`Path traversal denied: "${filePath}" resolves outside workspace "${wsAbsolute}"`);
    }

    // Step 2: Resolve symlinks and re-check (OpenClaw pattern)
    // The file might not exist yet (for write/create), so we resolve as much as possible.
    let realPath: string;
    try {
      realPath = await fs.realpath(normalized);
    } catch {
      // File doesn't exist — resolve the parent directory instead
      const parentDir = path.dirname(normalized);
      try {
        const realParent = await fs.realpath(parentDir);
        realPath = path.join(realParent, path.basename(normalized));
      } catch {
        // Parent also doesn't exist — fall back to normalized path
        // (write_file will create dirs later, and they'll be within workspace)
        return normalized;
      }
    }

    // Re-check after symlink resolution
    if (!realPath.startsWith(wsAbsolute + path.sep) && realPath !== wsAbsolute) {
      throw new Error(
        `Symlink escape denied: "${filePath}" resolves to "${realPath}" which is outside workspace "${wsAbsolute}"`,
      );
    }

    return realPath;
  }

  // ── Sensitive file check ───────────────────────────────────

  /**
   * Check if a file matches sensitive file patterns.
   * Throws if the file appears to contain secrets/credentials.
   */
  private checkSensitive(filePath: string): void {
    const basename = path.basename(filePath);
    for (const pattern of FileService.SENSITIVE_PATTERNS) {
      if (pattern.test(basename)) {
        throw new Error(
          `Access denied: "${filePath}" matches sensitive file pattern (${pattern}). ` +
          `Reading/writing credential files, private keys, and secret configs is not allowed for security.`,
        );
      }
    }
  }

  // ── Binary file detection ─────────────────────────────────

  /**
   * Check if a file appears to be binary by probing for null bytes.
   * (opencode pattern: check first N bytes for \x00)
   */
  private async isBinaryFile(absPath: string): Promise<boolean> {
    try {
      const handle = await fs.open(absPath, "r");
      try {
        const buf = Buffer.alloc(FileService.BINARY_PROBE_SIZE);
        const { bytesRead } = await handle.read(buf, 0, FileService.BINARY_PROBE_SIZE, 0);
        for (let i = 0; i < bytesRead; i++) {
          if (buf[i] === 0) return true;
        }
        return false;
      } finally {
        await handle.close();
      }
    } catch {
      return false;
    }
  }

  // ── read_file ──────────────────────────────────────────────

  /**
   * Read file contents with line numbers.
   * Returns formatted string with line numbers (opencode-style).
   * Records mtime for stale-read guard.
   */
  async readFile(
    workspacePath: string,
    filePath: string,
    offset?: number,
    limit?: number,
  ): Promise<string> {
    const absPath = await this.resolveSafe(workspacePath, filePath);
    this.checkSensitive(filePath);

    // Check file exists and is not too large
    let stat: Stats;
    try {
      stat = await fs.stat(absPath);
    } catch {
      // Suggest similar files if not found
      const dir = path.dirname(absPath);
      try {
        const siblings = await fs.readdir(dir);
        const base = path.basename(absPath);
        const similar = siblings.filter(f =>
          f.toLowerCase().includes(base.toLowerCase().slice(0, 4))
        ).slice(0, 5);
        if (similar.length > 0) {
          throw new Error(`File not found: "${filePath}". Did you mean: ${similar.join(", ")}?`);
        }
      } catch { /* ignore */ }
      throw new Error(`File not found: "${filePath}"`);
    }

    if (stat.isDirectory()) {
      throw new Error(`"${filePath}" is a directory, not a file. Use list_files instead.`);
    }
    if (stat.size > FileService.MAX_READ_SIZE) {
      throw new Error(`File too large (${(stat.size / 1024).toFixed(0)} KB > ${FileService.MAX_READ_SIZE / 1024} KB). Use offset/limit to read portions.`);
    }

    // Binary detection
    if (await this.isBinaryFile(absPath)) {
      throw new Error(`"${filePath}" appears to be a binary file. Only text files can be read.`);
    }

    const content = await fs.readFile(absPath, "utf-8");

    // Record mtime for stale-read guard (full read or offset=0)
    if (!offset || offset === 0) {
      this.readHistory.set(absPath, stat.mtimeMs);
    }

    const allLines = content.split("\n");
    const startLine = Math.max(0, offset || 0);
    const endLine = Math.min(allLines.length, startLine + (limit || FileService.MAX_READ_LINES));
    const slice = allLines.slice(startLine, endLine);

    // Format with line numbers (6-char right-padded, like opencode)
    const numbered = slice.map((line, i) => {
      const lineNum = startLine + i + 1;
      const padded = String(lineNum).padStart(6);
      const truncated = line.length > FileService.MAX_LINE_LENGTH
        ? line.slice(0, FileService.MAX_LINE_LENGTH) + "..."
        : line;
      return `${padded}|${truncated}`;
    });

    const totalLines = allLines.length;
    const header = `File: ${filePath} (${totalLines} lines total, showing ${startLine + 1}-${endLine})`;
    const footer = endLine < totalLines
      ? `\n... ${totalLines - endLine} more lines. Use offset=${endLine} to read more.`
      : "";

    return `${header}\n${numbered.join("\n")}${footer}`;
  }

  // ── write_file ─────────────────────────────────────────────

  /**
   * Write content to a file. Creates parent directories automatically.
   *
   * Safety features (opencode-inspired):
   * - No-op guard: skips write if content is identical
   * - Stale-read guard: rejects overwrite if file was modified since last read
   * - Append mode: append=true adds content instead of overwriting
   */
  async writeFile(
    workspacePath: string,
    filePath: string,
    content: string,
    append?: boolean,
  ): Promise<string> {
    const absPath = await this.resolveSafe(workspacePath, filePath);
    this.checkSensitive(filePath);

    // Auto-create parent directories
    const dir = path.dirname(absPath);
    await fs.mkdir(dir, { recursive: true });

    // Check if file already exists
    let existed = false;
    let oldSize = 0;
    let oldContent: string | null = null;
    try {
      const existing = await fs.stat(absPath);
      existed = true;
      oldSize = existing.size;

      // No-op guard (opencode): skip if content is identical
      if (!append && existing.size <= FileService.MAX_READ_SIZE) {
        oldContent = await fs.readFile(absPath, "utf-8");
        if (oldContent === content) {
          return `No change: "${filePath}" already has the exact same content. Write skipped.`;
        }
      }

      // Stale-read guard (opencode): reject if file changed since last read
      if (!append) {
        const lastReadMtime = this.readHistory.get(absPath);
        if (lastReadMtime !== undefined && existing.mtimeMs > lastReadMtime) {
          throw new Error(
            `Stale read: "${filePath}" has been modified since you last read it ` +
            `(last read: ${new Date(lastReadMtime).toISOString()}, ` +
            `file modified: ${new Date(existing.mtimeMs).toISOString()}). ` +
            `Please read the file again before writing.`,
          );
        }
      }
    } catch (e: any) {
      // Re-throw stale-read errors, ignore ENOENT
      if (e.message?.startsWith("Stale read:")) throw e;
      /* file doesn't exist — new file */
    }

    // Write or append
    if (append && existed) {
      await fs.appendFile(absPath, content, "utf-8");
    } else {
      await fs.writeFile(absPath, content, "utf-8");
    }

    // Clear stale-read tracking after successful write
    this.readHistory.delete(absPath);

    const finalContent = append && oldContent ? oldContent + content : content;
    const newLines = finalContent.split("\n").length;
    const newSize = Buffer.byteLength(finalContent, "utf-8");
    const action = append
      ? "Appended to"
      : existed
        ? "Updated"
        : "Created";
    const sizeDiff = existed && !append ? ` (${newSize > oldSize ? "+" : ""}${newSize - oldSize} bytes)` : "";

    return `${action}: ${filePath} (${newLines} lines, ${newSize} bytes${sizeDiff})`;
  }

  // ── edit_file ──────────────────────────────────────────────

  /**
   * Find-and-replace in a file. oldText must match exactly once.
   * Supports three modes (inspired by opencode):
   * - oldText + newText: replace content
   * - oldText="" + newText: prepend content (new file)
   * - oldText + newText="": delete content
   */
  async editFile(
    workspacePath: string,
    filePath: string,
    oldText: string,
    newText: string,
  ): Promise<string> {
    const absPath = await this.resolveSafe(workspacePath, filePath);
    this.checkSensitive(filePath);

    // Mode: create new file (oldText is empty)
    if (!oldText && newText) {
      try {
        await fs.access(absPath);
        throw new Error(`File already exists: "${filePath}". Cannot create — use write_file to overwrite.`);
      } catch (e: any) {
        if (e.code !== "ENOENT") throw e;
      }
      const dir = path.dirname(absPath);
      await fs.mkdir(dir, { recursive: true });
      await fs.writeFile(absPath, newText, "utf-8");
      return `Created: ${filePath} (${newText.split("\n").length} lines)`;
    }

    // Read existing content
    let content: string;
    try {
      content = await fs.readFile(absPath, "utf-8");
    } catch {
      throw new Error(`File not found: "${filePath}". Read the file first or use write_file to create it.`);
    }

    // Stale-read guard: check if file changed since last read
    const stat = await fs.stat(absPath);
    const lastReadMtime = this.readHistory.get(absPath);
    if (lastReadMtime !== undefined && stat.mtimeMs > lastReadMtime) {
      throw new Error(
        `Stale read: "${filePath}" has been modified since you last read it. ` +
        `Please read the file again before editing.`,
      );
    }

    // Mode: delete content (newText is empty)
    if (oldText && !newText) {
      if (!content.includes(oldText)) {
        throw new Error(`Text not found in "${filePath}". Make sure oldText matches exactly (including whitespace and indentation).`);
      }
      const count = content.split(oldText).length - 1;
      if (count > 1) {
        throw new Error(`oldText matches ${count} locations in "${filePath}". It must match exactly once. Add more context to make it unique.`);
      }
      const updated = content.replace(oldText, "");
      await fs.writeFile(absPath, updated, "utf-8");
      this.readHistory.delete(absPath);
      return `Deleted content in: ${filePath}`;
    }

    // Mode: replace content
    if (!content.includes(oldText)) {
      throw new Error(`Text not found in "${filePath}". Make sure oldText matches exactly (including whitespace and indentation).`);
    }
    const count = content.split(oldText).length - 1;
    if (count > 1) {
      throw new Error(`oldText matches ${count} locations in "${filePath}". It must match exactly once. Add more context to make it unique.`);
    }

    const updated = content.replace(oldText, newText);
    await fs.writeFile(absPath, updated, "utf-8");
    this.readHistory.delete(absPath);

    // Report diff stats
    const oldLines = oldText.split("\n").length;
    const newLines = newText.split("\n").length;
    return `Edited: ${filePath} (replaced ${oldLines} line(s) with ${newLines} line(s))`;
  }

  // ── delete_file ────────────────────────────────────────────

  /**
   * Delete a file from the workspace.
   * Safety: cannot delete directories (use edit_file to clear contents,
   * or ask the user for directory deletion).
   */
  async deleteFile(
    workspacePath: string,
    filePath: string,
  ): Promise<string> {
    const absPath = await this.resolveSafe(workspacePath, filePath);
    this.checkSensitive(filePath);

    let stat: Stats;
    try {
      stat = await fs.stat(absPath);
    } catch {
      throw new Error(`File not found: "${filePath}"`);
    }

    if (stat.isDirectory()) {
      throw new Error(
        `"${filePath}" is a directory. file_delete only works on individual files. ` +
        `Directory deletion requires user approval — please ask the user.`,
      );
    }

    await fs.unlink(absPath);
    this.readHistory.delete(absPath);

    return `Deleted: ${filePath} (${(stat.size / 1024).toFixed(1)} KB freed)`;
  }

  // ── list_files ─────────────────────────────────────────────

  /**
   * List directory contents as a tree.
   * Skips common ignored directories (node_modules, .git, etc.).
   */
  async listFiles(
    workspacePath: string,
    dirPath?: string,
    recursive?: boolean,
  ): Promise<string> {
    const targetPath = dirPath
      ? await this.resolveSafe(workspacePath, dirPath)
      : path.resolve(workspacePath);

    let stat: Stats;
    try {
      stat = await fs.stat(targetPath);
    } catch {
      throw new Error(`Directory not found: "${dirPath || "."}"`);
    }
    if (!stat.isDirectory()) {
      throw new Error(`"${dirPath || "."}" is a file, not a directory. Use read_file instead.`);
    }

    const relBase = path.relative(path.resolve(workspacePath), targetPath) || ".";
    const entries: string[] = [];
    let fileCount = 0;

    const walk = async (absDir: string, depth: number): Promise<void> => {
      if (fileCount >= FileService.MAX_LIST_FILES) return;

      let items: Dirent[];
      try {
        items = await fs.readdir(absDir, { withFileTypes: true });
      } catch { return; }

      // Sort: directories first, then files, alphabetically
      items.sort((a, b) => {
        if (a.isDirectory() && !b.isDirectory()) return -1;
        if (!a.isDirectory() && b.isDirectory()) return 1;
        return a.name.localeCompare(b.name);
      });

      for (const item of items) {
        if (fileCount >= FileService.MAX_LIST_FILES) break;
        if (item.name.startsWith(".") && item.isDirectory()) continue;
        if (item.isDirectory() && FileService.IGNORED_DIRS.has(item.name)) continue;

        const indent = "  ".repeat(depth);
        if (item.isDirectory()) {
          entries.push(`${indent}📁 ${item.name}/`);
          fileCount++;
          if (recursive !== false) {
            await walk(path.join(absDir, item.name), depth + 1);
          }
        } else {
          const size = await fs.stat(path.join(absDir, item.name)).then(s => s.size).catch(() => 0);
          const sizeStr = size < 1024 ? `${size}B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)}KB` : `${(size / 1024 / 1024).toFixed(1)}MB`;
          entries.push(`${indent}📄 ${item.name} (${sizeStr})`);
          fileCount++;
        }
      }
    };

    await walk(targetPath, 0);

    const header = `## Directory: ${relBase}${recursive === false ? " (non-recursive)" : ""}`;
    const truncated = fileCount >= FileService.MAX_LIST_FILES
      ? `\n... truncated at ${FileService.MAX_LIST_FILES} entries`
      : "";
    return `${header}\n${entries.join("\n")}${truncated}`;
  }

  // ── search_files ───────────────────────────────────────────

  /**
   * Search file contents for a pattern (grep-like).
   * Returns matching lines with file paths and line numbers.
   */
  async searchFiles(
    workspacePath: string,
    pattern: string,
    searchPath?: string,
    include?: string,
  ): Promise<string> {
    const targetDir = searchPath
      ? await this.resolveSafe(workspacePath, searchPath)
      : path.resolve(workspacePath);

    let regex: RegExp;
    try {
      regex = new RegExp(pattern, "gi");
    } catch {
      throw new Error(`Invalid regex pattern: "${pattern}"`);
    }

    const results: Array<{ file: string; line: number; content: string }> = [];
    const wsRoot = path.resolve(workspacePath);

    const searchDir = async (absDir: string): Promise<void> => {
      if (results.length >= FileService.MAX_SEARCH_RESULTS) return;

      let items: Dirent[];
      try {
        items = await fs.readdir(absDir, { withFileTypes: true });
      } catch { return; }

      for (const item of items) {
        if (results.length >= FileService.MAX_SEARCH_RESULTS) break;
        const fullPath = path.join(absDir, item.name);

        if (item.isDirectory()) {
          if (item.name.startsWith(".") || FileService.IGNORED_DIRS.has(item.name)) continue;
          await searchDir(fullPath);
        } else if (item.isFile()) {
          // Apply include filter
          if (include) {
            const ext = path.extname(item.name);
            const glob = include.replace(/^\*\./, ".");
            if (ext !== glob && !item.name.includes(include.replace("*", ""))) continue;
          }

          // Skip large files
          try {
            const stat = await fs.stat(fullPath);
            if (stat.size > FileService.MAX_READ_SIZE) continue;
          } catch { continue; }

          // Skip binary files
          if (await this.isBinaryFile(fullPath)) continue;

          try {
            const content = await fs.readFile(fullPath, "utf-8");
            const lines = content.split("\n");
            for (let i = 0; i < lines.length; i++) {
              if (results.length >= FileService.MAX_SEARCH_RESULTS) break;
              if (regex.test(lines[i])) {
                const relFile = path.relative(wsRoot, fullPath);
                results.push({
                  file: relFile,
                  line: i + 1,
                  content: lines[i].trim().slice(0, 200),
                });
              }
              // Reset regex lastIndex for global flag
              regex.lastIndex = 0;
            }
          } catch { /* skip unreadable files */ }
        }
      }
    };

    await searchDir(targetDir);

    if (results.length === 0) {
      return `No matches found for "${pattern}"${searchPath ? ` in ${searchPath}` : ""}.`;
    }

    const lines = results.map(r => `${r.file}:${r.line}: ${r.content}`);
    const truncated = results.length >= FileService.MAX_SEARCH_RESULTS
      ? `\n... truncated at ${FileService.MAX_SEARCH_RESULTS} matches`
      : "";
    return `## Search: "${pattern}" (${results.length} matches)\n${lines.join("\n")}${truncated}`;
  }

  // ── Glob (pattern-based file finding) ────────────────────────

  /**
   * Find files matching a glob pattern within the workspace.
   * Uses fast-glob for efficient pattern matching.
   */
  async globFiles(
    workspacePath: string,
    pattern: string,
    cwd?: string,
    limit?: number,
  ): Promise<string> {
    if (!pattern) return "Error: glob requires a pattern parameter.";

    const fg = await import("fast-glob");
    const wsRoot = path.resolve(workspacePath);
    const searchDir = cwd ? path.resolve(wsRoot, cwd) : wsRoot;

    // Validate search directory is within workspace
    if (!searchDir.startsWith(wsRoot + path.sep) && searchDir !== wsRoot) {
      return `Error: search directory escapes workspace.`;
    }

    const maxResults = Math.min(limit || 500, 500);

    try {
      const entries = await fg.default(pattern, {
        cwd: searchDir,
        absolute: true,
        dot: false,
        onlyFiles: true,
        followSymbolicLinks: false,
        suppressErrors: true,
        ignore: [
          "**/node_modules/**",
          "**/.git/**",
          "**/dist/**",
          "**/build/**",
          "**/.next/**",
          "**/.nuxt/**",
          "**/.turbo/**",
          "**/coverage/**",
        ],
      });

      // Sort by mtime descending (most recently modified first)
      const withStats = await Promise.all(
        entries.slice(0, maxResults + 100).map(async (absPath) => {
          try {
            const stat = await fs.stat(absPath);
            return { absPath, mtime: stat.mtimeMs, size: stat.size };
          } catch {
            return { absPath, mtime: 0, size: 0 };
          }
        }),
      );
      withStats.sort((a, b) => b.mtime - a.mtime);

      const results = withStats.slice(0, maxResults);
      if (results.length === 0) {
        return `No files matching "${pattern}"${cwd ? ` in ${cwd}` : ""}.`;
      }

      const lines = results.map((r) => {
        const rel = path.relative(wsRoot, r.absPath);
        const sizeStr = r.size > 1024 ? `${Math.round(r.size / 1024)}KB` : `${r.size}B`;
        return `${rel} (${sizeStr})`;
      });

      const truncNote = entries.length > maxResults
        ? `\n... truncated at ${maxResults} results (${entries.length} total matches)`
        : "";

      return `## Files matching "${pattern}" (${results.length} results)\n${lines.join("\n")}${truncNote}`;
    } catch (err: any) {
      return `Error running glob: ${err.message || err}`;
    }
  }

  // ── Move / Rename ────────────────────────────────────────────

  /**
   * Move or rename a file or directory within the workspace.
   */
  async moveFile(
    workspacePath: string,
    source: string,
    destination: string,
    overwrite: boolean = false,
  ): Promise<string> {
    if (!source || !destination) {
      return "Error: move_file requires source and destination.";
    }

    const srcAbs = await this.resolveSafe(workspacePath, source);
    const destAbs = await this.resolveSafe(workspacePath, destination);

    // Verify source exists
    try {
      await fs.access(srcAbs);
    } catch {
      return `Error: source "${source}" does not exist.`;
    }

    // Check destination
    try {
      await fs.access(destAbs);
      if (!overwrite) {
        return `Error: destination "${destination}" already exists. Set overwrite=true to replace.`;
      }
    } catch {
      // Destination doesn't exist — good
    }

    // Ensure destination parent exists
    const destParent = path.dirname(destAbs);
    await fs.mkdir(destParent, { recursive: true });

    try {
      await fs.rename(srcAbs, destAbs);
    } catch (err: any) {
      // Cross-device rename: fall back to copy + delete
      if (err.code === "EXDEV") {
        await fs.cp(srcAbs, destAbs, { recursive: true });
        await fs.rm(srcAbs, { recursive: true });
      } else {
        throw err;
      }
    }

    return `Moved "${source}" → "${destination}".`;
  }

  // ── Create Directory ─────────────────────────────────────────

  /**
   * Create a directory (and any necessary parent directories).
   */
  async createDirectory(
    workspacePath: string,
    dirPath: string,
  ): Promise<string> {
    if (!dirPath) return "Error: create_directory requires a path.";

    const absPath = await this.resolveSafe(workspacePath, dirPath);
    await fs.mkdir(absPath, { recursive: true });
    return `Directory created: "${dirPath}".`;
  }

  // ── Delete Directory ─────────────────────────────────────────

  /**
   * Delete an empty directory. Refuses non-empty directories for safety.
   */
  async deleteDirectory(
    workspacePath: string,
    dirPath: string,
  ): Promise<string> {
    if (!dirPath) return "Error: delete_directory requires a path.";

    const absPath = await this.resolveSafe(workspacePath, dirPath);

    // Use rmdir (fails on non-empty dirs) — NOT rm({recursive: true})
    try {
      await fs.rmdir(absPath);
    } catch (err: any) {
      if (err.code === "ENOTEMPTY") {
        return `Error: directory "${dirPath}" is not empty. Delete files inside first, or use run_command.`;
      }
      throw err;
    }

    return `Directory deleted: "${dirPath}".`;
  }
}
