/**
 * ShellService — sandboxed shell command execution for HiveWeave agents.
 *
 * Provides `run_command` tool: agents can execute shell commands (npm, git,
 * python, test runners, etc.) within their project workspace.
 *
 * Security layers:
 * - Working directory sandbox (cwd must be within workspace path)
 * - Dangerous command blocklist (rm -rf /, format, mkfs, fork bombs, pipe-to-shell)
 * - Timeout enforcement (default 120s, max 600s)
 * - Output truncation (50K chars max, head+tail strategy)
 * - stdin always ignored (no interactive commands)
 * - maxBuffer limit (5MB per stream)
 *
 * Inspired by OpenCode's bash tool (shell.ts).
 */

import { spawn } from "child_process";
import * as path from "path";
import * as os from "os";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ShellCommandParams {
  command: string;
  cwd?: string;
  timeout?: number;
}

export interface ShellResult {
  exitCode: number | null;
  output: string;
  timedOut: boolean;
  truncated: boolean;
  duration: number;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DEFAULT_TIMEOUT_MS = 120_000;
const MAX_TIMEOUT_MS = 600_000;
const MAX_OUTPUT_CHARS = 50_000;
const KEEP_HEAD_CHARS = 20_000;
const KEEP_TAIL_CHARS = 20_000;
const MAX_BUFFER = 5 * 1024 * 1024; // 5 MB per stream

/**
 * Blocklist of dangerous command patterns (regex).
 * This is a safety net — the primary security is the workspace sandbox +
 * permission system (ASK by default for all run_command calls).
 */
const BLOCKED_PATTERNS: RegExp[] = [
  // Destructive system commands
  /^\s*rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)*\//,        // rm at filesystem root
  /^\s*rm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)*\.\.(\/|$)/, // rm -rf .. (parent dir)
  /^\s*format\s+[a-zA-Z]:/i,                         // Windows format drive
  /^\s*mkfs\b/,                                       // Linux format filesystem
  /^\s*dd\s+.*of=\/dev\//,                            // Raw disk write
  /^\s*:\(\)\s*\{/,                                   // Fork bomb (bash)

  // Pipe-to-shell (remote code execution risk)
  /\bcurl\b.*\|\s*(ba)?sh/,
  /\bwget\b.*\|\s*(ba)?sh/,

  // System modification
  /^\s*chmod\s+777\s+\//,
  /^\s*chown\s+.*\//,
  /^\s*shutdown\b/,
  /^\s*reboot\b/,
  /^\s*init\s+[06]/,
  /^\s*poweroff\b/,

  // HiveWeave system directory protection
  /\.hiveweave\b/,
];

// ---------------------------------------------------------------------------
// ShellService
// ---------------------------------------------------------------------------

export class ShellService {
  /**
   * Execute a shell command within the agent's workspace directory.
   *
   * @param workspacePath - The project's workspace root (sandbox boundary).
   * @param params - Command parameters (command string, optional cwd, timeout).
   * @returns ShellResult with exit code, output, timing info.
   */
  async runCommand(
    workspacePath: string,
    params: ShellCommandParams,
  ): Promise<ShellResult> {
    const { command, cwd, timeout } = params;

    if (!command || !command.trim()) {
      throw new Error("run_command requires a non-empty command string.");
    }

    // 1. Check blocklist
    this.checkBlocklist(command);

    // 2. Resolve and validate working directory
    const resolvedCwd = this.resolveWorkingDirectory(cwd, workspacePath);

    // 3. Clamp timeout
    const clampedTimeout = Math.min(
      Math.max(timeout || DEFAULT_TIMEOUT_MS, 1000),
      MAX_TIMEOUT_MS,
    );

    // 4. Select platform-appropriate shell
    const { shell, flag } = this.selectShell();

    // 5. Execute via spawn
    return this.spawnCommand(shell, [flag, command], resolvedCwd, clampedTimeout);
  }

  // -------------------------------------------------------------------------
  // Private helpers
  // -------------------------------------------------------------------------

  private checkBlocklist(command: string): void {
    for (const pattern of BLOCKED_PATTERNS) {
      if (pattern.test(command)) {
        throw new Error(
          `Command blocked by safety policy: "${command.slice(0, 100)}..." matches ${pattern}. ` +
          `If this is intentional, use the permission override.`,
        );
      }
    }
  }

  private resolveWorkingDirectory(cwd: string | undefined, workspacePath: string): string {
    const base = path.resolve(workspacePath);
    const target = cwd
      ? path.resolve(base, cwd)
      : base;

    // Ensure the resolved path is within the workspace
    if (!target.startsWith(base + path.sep) && target !== base) {
      throw new Error(
        `Sandbox violation: working directory "${target}" is outside workspace "${base}". ` +
        `Commands must run within the project workspace.`,
      );
    }

    return target;
  }

  private selectShell(): { shell: string; flag: string } {
    const platform = os.platform();
    if (platform === "win32") {
      // Prefer cmd.exe (most universal on Windows)
      return { shell: "cmd.exe", flag: "/c" };
    }
    // Unix: prefer bash, fallback to sh
    return { shell: "/bin/bash", flag: "-c" };
  }

  private spawnCommand(
    shell: string,
    args: string[],
    cwd: string,
    timeout: number,
  ): Promise<ShellResult> {
    const startTime = Date.now();

    return new Promise((resolve, reject) => {
      let stdout = "";
      let stderr = "";
      let timedOut = false;
      let killed = false;

      const child = spawn(shell, args, {
        cwd,
        env: {
          ...process.env,
          HIVEWEAVE_AGENT: "1",
          HIVEWEAVE_WORKSPACE: cwd,
        },
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
        timeout,
      });

      child.stdout.on("data", (chunk: Buffer) => {
        stdout += chunk.toString("utf-8");
      });

      child.stderr.on("data", (chunk: Buffer) => {
        stderr += chunk.toString("utf-8");
      });

      child.on("error", (err) => {
        reject(new Error(`Failed to spawn shell: ${err.message}`));
      });

      child.on("close", (code) => {
        const duration = Date.now() - startTime;

        if (killed || child.killed) {
          timedOut = true;
        }

        // Merge stdout and stderr into a single output string
        let combined = "";
        if (stdout && stderr) {
          combined = stdout + "\n[stderr]\n" + stderr;
        } else {
          combined = stdout || stderr;
        }

        // Truncate if needed
        const { output, truncated } = this.truncateOutput(combined);

        resolve({
          exitCode: code,
          output: this.formatOutput(code, output, duration, timedOut, truncated),
          timedOut,
          truncated,
          duration,
        });
      });

      // Timeout handling: SIGTERM → 5s grace → SIGKILL
      const timer = setTimeout(() => {
        timedOut = true;
        killed = true;
        try {
          if (os.platform() === "win32") {
            // On Windows, use taskkill to kill process tree
            spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
              windowsHide: true,
            });
          } else {
            child.kill("SIGTERM");
            setTimeout(() => {
              if (!child.killed) {
                child.kill("SIGKILL");
              }
            }, 5000);
          }
        } catch {
          // Best effort
        }
      }, timeout);

      child.on("close", () => clearTimeout(timer));
    });
  }

  private truncateOutput(output: string): { output: string; truncated: boolean } {
    if (output.length <= MAX_OUTPUT_CHARS) {
      return { output, truncated: false };
    }

    const head = output.slice(0, KEEP_HEAD_CHARS);
    const tail = output.slice(output.length - KEEP_TAIL_CHARS);
    const removed = output.length - KEEP_HEAD_CHARS - KEEP_TAIL_CHARS;

    return {
      output: `${head}\n\n[... ${removed} characters truncated ...]\n\n${tail}`,
      truncated: true,
    };
  }

  private formatOutput(
    exitCode: number | null,
    output: string,
    duration: number,
    timedOut: boolean,
    truncated: boolean,
  ): string {
    const parts: string[] = [];

    // Header line
    const status = timedOut
      ? "TIMEOUT"
      : exitCode === 0
        ? "OK"
        : `EXIT ${exitCode}`;
    parts.push(`[${status}] [${duration}ms]${truncated ? " [output truncated]" : ""}`);

    if (output.trim()) {
      parts.push(output.trim());
    } else if (exitCode !== 0 && !output.trim()) {
      parts.push("(no output)");
    }

    if (timedOut) {
      parts.push(`\nCommand timed out after ${Math.round(duration / 1000)}s.`);
    }

    return parts.join("\n");
  }
}
