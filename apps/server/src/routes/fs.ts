import type { FastifyInstance } from "fastify";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";

/**
 * Filesystem browse endpoint for the folder picker UI.
 * Returns directory entries (folders only, since we're picking a workspace directory).
 */
export async function fsRoutes(fastify: FastifyInstance) {
  // GET /api/fs/browse?path=...
  // Returns: { currentPath, parentPath, entries: [{ name, isDir, path }] }
  fastify.get<{ Querystring: { path?: string } }>("/browse", async (request, reply) => {
    let targetPath = request.query.path || os.homedir();

    // Normalize and resolve
    targetPath = path.resolve(targetPath);

    try {
      const stat = await fs.stat(targetPath);
      if (!stat.isDirectory()) {
        return reply.status(400).send({ error: "Not a directory", path: targetPath });
      }
    } catch {
      return reply.status(404).send({ error: "Directory not found", path: targetPath });
    }

    // Get parent path
    const parentPath = path.dirname(targetPath);
    const isRoot = parentPath === targetPath; // e.g. C:\ or /

    // Read directory entries — folders only for workspace picker
    let entries: Array<{ name: string; isDir: boolean; fullPath: string }> = [];
    try {
      const items = await fs.readdir(targetPath, { withFileTypes: true });
      entries = items
        .filter(item => item.isDirectory() && !item.name.startsWith("."))
        .map(item => ({
          name: item.name,
          isDir: true,
          fullPath: path.join(targetPath, item.name),
        }))
        .sort((a, b) => a.name.localeCompare(b.name));
    } catch {
      // Permission denied or other read error — return empty entries
    }

    // Get available drives on Windows
    let drives: string[] = [];
    if (process.platform === "win32") {
      try {
        const { execSync } = await import("child_process");
        const output = execSync("wmic logicaldisk get caption", { encoding: "utf-8", timeout: 5000 });
        drives = output
          .split("\n")
          .map(line => line.trim())
          .filter(line => /^[A-Z]:$/.test(line))
          .map(d => d + "\\");
      } catch {
        // Fallback: try common drive letters
        drives = ["C:\\"];
        for (const letter of "DEFGH") {
          try {
            await fs.access(`${letter}:\\`);
            drives.push(`${letter}:\\`);
          } catch { /* drive doesn't exist */ }
        }
      }
    }

    return {
      currentPath: targetPath,
      parentPath: isRoot ? null : parentPath,
      entries,
      drives,
      isRoot,
    };
  });
}
