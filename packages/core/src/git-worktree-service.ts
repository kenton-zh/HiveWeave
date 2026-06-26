/**
 * GitWorktreeService — isolated worktrees per agent, managed by coordinators.
 *
 * Each leaf agent CAN get an isolated git worktree, created by their manager/CEO.
 * Worktrees live under `.hiveweave/worktrees/<shortId>/` with branches named
 * `hw/<shortId>/<task-slug>`.  Coordinators control the full lifecycle.
 *
 * Design:
 *   - Tools are coordinator-only — executors cannot create/merge worktrees.
 *   - Checkpoints are lightweight commits (add -A + commit) on the agent branch.
 *   - Merge is a fast-forward merge into the main branch, then cleanup.
 *   - Rollback is git reset --hard to a specific commit (or HEAD~1 by default).
 */

import { execSync } from "child_process";
import { existsSync, mkdirSync } from "fs";
import { join } from "path";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface WorktreeEntry {
  /** Agent's short ID (e.g. A001). */
  shortId: string;
  /** Worktree path on disk. */
  path: string;
  /** Branch name: hw/<shortId>/<task>. */
  branch: string;
  /** HEAD commit hash (7 chars). */
  head: string;
  /** Whether the worktree directory exists. */
  active: boolean;
}

export interface CheckpointEntry {
  hash: string;
  date: string;
  message: string;
}

export interface WorktreeStatus {
  shortId: string;
  branch: string;
  active: boolean;
  hasUncommitted: boolean;
  head: string;
  checkpoints: CheckpointEntry[];
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const WORKTREE_DIR = ".hiveweave/worktrees";
const CHECKPOINT_PREFIX = "checkpoint:";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function git(args: string, cwd: string, timeoutMs = 30_000): string {
  try {
    return execSync(`git ${args}`, { cwd, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"], timeout: timeoutMs }).trim();
  } catch (err: any) {
    const stderr = err.stderr || "";
    throw new Error(`git ${args.split(" ")[0]} failed: ${stderr.slice(0, 300)}`);
  }
}

/** Sanitize a task name into a branch-safe slug. */
function slugify(name: string): string {
  return name
    .replace(/[\s/\\]+/g, "-")
    .replace(/[^a-zA-Z0-9一-鿿\-_]/g, "")
    .slice(0, 40)
    .replace(/^-+|-+$/g, "")
    || "task";
}

// ---------------------------------------------------------------------------
// GitWorktreeService
// ---------------------------------------------------------------------------

export class GitWorktreeService {
  private worktreeRoot: string;

  constructor(private readonly workspacePath: string) {
    this.worktreeRoot = join(workspacePath, WORKTREE_DIR);
  }

  /** Absolute path to an agent's worktree directory. */
  getWorktreePath(shortId: string): string {
    return join(this.worktreeRoot, shortId);
  }

  /** Branch name for an agent + task. */
  static branchName(shortId: string, task: string): string {
    return `hw/${shortId}/${slugify(task)}`;
  }

  // -----------------------------------------------------------------------
  // 0. Ensure the workspace is a git repo
  // -----------------------------------------------------------------------

  /**
   * Check whether the workspace is a git repo. If not, init one.
   * Returns { initialized: true } if a new repo was created.
   */
  async ensureGitRepo(): Promise<{ isRepo: boolean; initialized: boolean }> {
    // Check if .git exists
    const dotGit = join(this.workspacePath, ".git");
    const { existsSync } = await import("fs");
    if (existsSync(dotGit)) return { isRepo: true, initialized: false };

    // Check if git CLI is available
    try {
      execSync("git --version", { encoding: "utf-8", stdio: "pipe", timeout: 5000 });
    } catch {
      throw new Error("Git is not installed or not on PATH. Install git and try again.");
    }

    // Init the repo
    try {
      execSync("git init", { cwd: this.workspacePath, encoding: "utf-8", stdio: "pipe", timeout: 10000 });
      execSync('git commit --allow-empty -m "root: initialized by HiveWeave"', { cwd: this.workspacePath, encoding: "utf-8", stdio: "pipe", timeout: 10000 });
    } catch (err: any) {
      throw new Error(`Failed to initialize git repository: ${err.message?.slice(0, 200)}`);
    }

    return { isRepo: true, initialized: true };
  }

  // -----------------------------------------------------------------------
  // 1. CREATE — allocate an isolated worktree + branch for a subordinate
  // -----------------------------------------------------------------------

  async createWorktree(
    shortId: string,
    taskName: string,
    baseBranch?: string,
  ): Promise<{ path: string; branch: string }> {
    await this.ensureGitRepo(); // auto-init git if workspace isn't a repo yet
    mkdirSync(this.worktreeRoot, { recursive: true });

    const path = this.getWorktreePath(shortId);
    const branch = GitWorktreeService.branchName(shortId, taskName);
    const base = baseBranch || "main";

    // If worktree already exists, return it
    if (existsSync(join(path, ".git"))) {
      return { path, branch };
    }

    try {
      git(`worktree add "${path}" -b "${branch}" "origin/${base}"`, this.workspacePath);
    } catch {
      // Fallback: create from local branch
      try {
        git(`worktree add "${path}" -b "${branch}" "${base}"`, this.workspacePath);
      } catch (err: any) {
        throw new Error(`Failed to create worktree for ${shortId}: ${err.message}`);
      }
    }

    return { path, branch };
  }

  // -----------------------------------------------------------------------
  // 2. CHECKPOINT — snapshot current state in the agent's worktree
  // -----------------------------------------------------------------------

  async checkpoint(
    shortId: string,
    message: string,
  ): Promise<{ hash: string; count: number }> {
    const path = this.getWorktreePath(shortId);
    if (!existsSync(path)) throw new Error(`Worktree for ${shortId} does not exist.`);

    // Stage everything
    git("add -A", path);

    // Check if there's anything to commit
    const status = git("status --porcelain", path);
    if (!status) return { hash: git("rev-parse --short HEAD", path), count: 0 };

    const commitMsg = `${CHECKPOINT_PREFIX} ${message}`;
    // No --allow-empty: empty checkpoints are noise. The status check above already
    // guards against truly empty commits. A race with another checkpoint call may
    // still produce one, but the cost (occasional empty commit) is acceptable vs
    // the complexity of a file-level lock.
    git(`commit -m "${commitMsg.replace(/"/g, '\\"')}"`, path);
    const hash = git("rev-parse --short HEAD", path);

    // Count checkpoints
    const log = git(`log --oneline --grep="${CHECKPOINT_PREFIX}" --since="7 days ago"`, path);
    const count = log ? log.split("\n").length : 1;

    return { hash, count };
  }

  // -----------------------------------------------------------------------
  // 3. MERGE — QA passed → merge agent branch into main, cleanup worktree
  // -----------------------------------------------------------------------

  async merge(
    shortId: string,
    taskName: string,
    targetBranch?: string,
  ): Promise<{ merged: boolean; hash: string }> {
    const branch = GitWorktreeService.branchName(shortId, taskName);
    const path = this.getWorktreePath(shortId);
    const target = targetBranch || "main";

    // Checkout target branch and merge the agent's feature branch
    try {
      git(`checkout "${target}"`, this.workspacePath);
      git(`merge "${branch}" --no-edit`, this.workspacePath);
    } catch (err: any) {
      // Merge conflict — abort and report
      try { git("merge --abort", this.workspacePath); } catch {}
      throw new Error(`Merge conflict for ${shortId} into ${target}: ${err.message}. Resolve manually or rollback.`);
    }

    const hash = git("rev-parse --short HEAD", this.workspacePath);

    // Cleanup: remove worktree + branch
    await this.removeWorktree(shortId, taskName);

    return { merged: true, hash };
  }

  // -----------------------------------------------------------------------
  // 4. ROLLBACK — reset agent's worktree to a previous checkpoint
  // -----------------------------------------------------------------------

  async rollback(
    shortId: string,
    commitHash?: string,
  ): Promise<{ hash: string; message: string }> {
    const path = this.getWorktreePath(shortId);
    if (!existsSync(path)) throw new Error(`Worktree for ${shortId} does not exist.`);

    let target = commitHash;
    if (!target) {
      // Default: rollback to last checkpoint
      try {
        target = git(`log --format=%H --grep="${CHECKPOINT_PREFIX}" -1`, path);
      } catch {
        throw new Error(`No checkpoints found for ${shortId}.`);
      }
    }

    git(`reset --hard "${target}"`, path);
    const hash = git("rev-parse --short HEAD", path);
    const msg = git("log -1 --format=%s", path);

    return { hash, message: msg };
  }

  // -----------------------------------------------------------------------
  // 5. REMOVE — discard agent's worktree (rejected / obsolete)
  // -----------------------------------------------------------------------

  async removeWorktree(
    shortId: string,
    taskName?: string,
  ): Promise<{ removed: boolean }> {
    const path = this.getWorktreePath(shortId);

    // Prune the worktree from git's registry
    try {
      git(`worktree remove "${path}" --force`, this.workspacePath);
    } catch {
      // Worktree may not be registered — just delete the directory
      try { execSync(`rmdir /s /q "${path}"`, { encoding: "utf-8" }); } catch {}
    }

    // Delete the branch
    if (taskName) {
      const branch = GitWorktreeService.branchName(shortId, taskName);
      try { git(`branch -D "${branch}"`, this.workspacePath); } catch {}
    }

    return { removed: true };
  }

  // -----------------------------------------------------------------------
  // 6. LIST — show all worktrees and checkpoints
  // -----------------------------------------------------------------------

  async listWorktrees(): Promise<WorktreeEntry[]> {
    let raw: string;
    try {
      raw = git("worktree list", this.workspacePath);
    } catch {
      return [];
    }

    const entries: WorktreeEntry[] = [];
    for (const line of raw.split("\n")) {
      // Format: "<path>  <hash> [<branch>]"
      const match = line.match(/^(.+?)\s+([a-f0-9]+)\s*(?:\[(.+?)\])?$/);
      if (!match) continue;

      const [, wtPath, hash, branch] = match;
      const dirName = wtPath.replace(/\\/g, "/").split("/").pop() || "";

      // Only include hiveweave-managed worktrees
      if (!wtPath.includes(WORKTREE_DIR)) continue;

      entries.push({
        shortId: dirName,
        path: wtPath.trim(),
        branch: (branch || "").trim(),
        head: hash.trim().slice(0, 7),
        active: existsSync(wtPath.trim()),
      });
    }

    return entries;
  }

  // -----------------------------------------------------------------------
  // 7. STATUS — detailed status of one agent's worktree
  // -----------------------------------------------------------------------

  async getStatus(shortId: string): Promise<WorktreeStatus | null> {
    const path = this.getWorktreePath(shortId);
    if (!existsSync(path)) return null;

    let head = "";
    let branch = "";
    let hasUncommitted = false;
    const checkpoints: CheckpointEntry[] = [];

    try {
      head = git("rev-parse --short HEAD", path);
      branch = git("rev-parse --abbrev-ref HEAD", path);
      const st = git("status --porcelain", path);
      hasUncommitted = st.length > 0;
    } catch {
      return null;
    }

    // List checkpoints
    try {
      const log = git(`log --oneline --grep="${CHECKPOINT_PREFIX}" -20`, path);
      if (log) {
        for (const line of log.split("\n")) {
          const m = line.match(/^([a-f0-9]+)\s+(.+)$/);
          if (m) {
            checkpoints.push({
              hash: m[1],
              date: "", // short log doesn't include date
              message: m[2].replace(CHECKPOINT_PREFIX + " ", ""),
            });
          }
        }
      }
    } catch {}

    // Get dates via fuller log
    if (checkpoints.length > 0) {
      try {
        const fullLog = git(`log --format="%h|%ad|%s" --date=short --grep="${CHECKPOINT_PREFIX}" -20`, path);
        for (const line of fullLog.split("\n")) {
          const [h, date, ...msg] = line.split("|");
          const cp = checkpoints.find((c) => c.hash === h);
          if (cp) cp.date = date;
        }
      } catch {}
    }

    return { shortId, branch, active: true, hasUncommitted, head, checkpoints };
  }
}
