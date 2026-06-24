/**
 * Bash tool — Effect-based shell command execution, ported from OpenCode.
 *
 * Uses Git Bash on Windows (for proper heredoc, pipes, SSH, docker),
 * falling back to cmd.exe if Git Bash is unavailable.
 */
import { spawn } from "child_process";
import * as path from "path";
import * as fs from "fs";
import { Effect, Schema, pipe, Console } from "effect";
import { String as S } from "effect";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const DEFAULT_TIMEOUT_MS = 2 * 60 * 1_000;
export const MAX_TIMEOUT_MS = 10 * 60 * 1_000;
const MAX_CAPTURE_BYTES = 1024 * 1024; // 1 MB

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------

export const BashInput = Schema.Struct({
  command: Schema.String.annotations({ description: "Shell command string to execute. Supports heredoc, pipes, multi-line with && or ; separators." }),
  workdir: Schema.optional(Schema.String).annotations({ description: "Working directory. Defaults to workspace root." }),
  timeout: Schema.optional(Schema.Number.pipe(Schema.positive(), Schema.lessThanOrEqualTo(MAX_TIMEOUT_MS))).annotations({
    description: `Timeout in milliseconds. Default ${DEFAULT_TIMEOUT_MS}, max ${MAX_TIMEOUT_MS}.`,
  }),
});

export type BashInput = typeof BashInput.Type;

export const BashOutput = Schema.Struct({
  command: Schema.String,
  cwd: Schema.String,
  exitCode: Schema.optional(Schema.Number),
  output: Schema.String,
  truncated: Schema.Boolean,
  timedOut: Schema.optional(Schema.Boolean),
  warnings: Schema.optional(Schema.Array(Schema.String)),
});

export type BashOutput = typeof BashOutput.Type;

// ---------------------------------------------------------------------------
// Shell detection
// ---------------------------------------------------------------------------

const GIT_BASH_PATHS = [
  "C:/Program Files/Git/bin/bash.exe",
  "C:/Program Files (x86)/Git/bin/bash.exe",
  "/usr/bin/bash",
  "/bin/bash",
];

function findShell(): string {
  // Windows: prefer Git Bash for full Unix-like shell features
  if (process.platform === "win32") {
    for (const p of GIT_BASH_PATHS) {
      if (fs.existsSync(p)) return p;
    }
    return process.env.COMSPEC || "cmd.exe";
  }
  // POSIX
  if (fs.existsSync("/bin/bash")) return "/bin/bash";
  return "/bin/sh";
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const compactOutput = (stdout: string, stderr: string): string => {
  if (stdout && stderr) return `${stdout}\n\nstderr:\n${stderr}`;
  if (stderr) return `stderr:\n${stderr}`;
  return stdout || "(no output)";
};

const modelOutput = (res: BashOutput): string => {
  const warnings = res.warnings?.length
    ? `\n\nWarnings:\n${res.warnings.map((w) => `- ${w}`).join("\n")}`
    : "";
  if (res.timedOut) return `${res.output}${warnings}\n\nCommand timed out.`;
  return `${res.output}${warnings}\n\nExit code: ${res.exitCode ?? "N/A"}`;
};

// ---------------------------------------------------------------------------
// External directory advisory scan (ported from OpenCode)
// ---------------------------------------------------------------------------

const shellTokens = (command: string): string[] =>
  command.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [];

const unquote = (value: string): string => value.replace(/^(['"])(.*)\1$/, "$2");

function externalCommandDirectories(command: string, cwd: string): string[] {
  const dirs = new Set<string>();
  for (const token of shellTokens(command)) {
    const value = unquote(token).replace(/[;,|&]+$/, "");
    if (!path.isAbsolute(value)) continue;
    const resolved = path.resolve(value);
    if (resolved.startsWith(cwd)) continue;
    dirs.add(path.dirname(resolved));
  }
  return [...dirs];
}

// ---------------------------------------------------------------------------
// Execute
// ---------------------------------------------------------------------------

export function executeBash(input: BashInput, workspacePath: string): Effect.Effect<BashOutput, Error> {
  return Effect.gen(function* () {
    const shell = findShell();
    const cwd = input.workdir
      ? path.resolve(workspacePath, input.workdir)
      : workspacePath;

    // Validate working directory
    if (!cwd.startsWith(workspacePath)) {
      return yield* Effect.fail(new Error(`Sandbox violation: working directory "${cwd}" is outside workspace`));
    }
    if (!fs.existsSync(cwd)) {
      return yield* Effect.fail(new Error(`Working directory does not exist: ${cwd}`));
    }

    // External directory advisory
    const warnings = externalCommandDirectories(input.command, cwd).map(
      (dir) => `Command argument references external directory ${dir.replace(/\\/g, "/")}/*. Advisory only.`,
    );

    const timeout = input.timeout ?? DEFAULT_TIMEOUT_MS;

    // Build shell args
    const shellFlag = shell.endsWith("bash.exe") || shell.endsWith("/bash") ? "-c" : "/c";

    const result: BashOutput = yield* Effect.async<BashOutput, Error>((resume) => {
      const child = spawn(shell, [shellFlag, input.command], {
        cwd,
        env: {
          ...process.env,
          HIVEWEAVE_BASH: "1",
          HIVEWEAVE_WORKSPACE: cwd,
        },
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });

      let stdout = "";
      let stderr = "";
      let stdoutTruncated = false;
      let stderrTruncated = false;
      let timedOut = false;

      const timer = setTimeout(() => {
        timedOut = true;
        try {
          child.kill("SIGTERM");
          setTimeout(() => { if (!child.killed) child.kill("SIGKILL"); }, 3000);
        } catch { /* best effort */ }
      }, timeout);

      child.stdout!.on("data", (chunk: Buffer) => {
        const text = chunk.toString("utf-8");
        if (stdout.length + text.length > MAX_CAPTURE_BYTES) {
          stdoutTruncated = true;
          stdout += text.slice(0, MAX_CAPTURE_BYTES - stdout.length);
        } else {
          stdout += text;
        }
      });

      child.stderr!.on("data", (chunk: Buffer) => {
        const text = chunk.toString("utf-8");
        if (stderr.length + text.length > MAX_CAPTURE_BYTES) {
          stderrTruncated = true;
          stderr += text.slice(0, MAX_CAPTURE_BYTES - stderr.length);
        } else {
          stderr += text;
        }
      });

      child.on("error", (err) => {
        clearTimeout(timer);
        resume(Effect.fail(new Error(`Failed to spawn shell: ${err.message}`)));
      });

      child.on("close", (code) => {
        clearTimeout(timer);
        const compact = compactOutput(stdout, stderr);
        const notice =
          stdoutTruncated && stderrTruncated
            ? "[stdout and stderr capture truncated]"
            : stdoutTruncated
              ? "[stdout capture truncated]"
              : stderrTruncated
                ? "[stderr capture truncated]"
                : undefined;

        resume(Effect.succeed({
          command: input.command,
          cwd,
          exitCode: code ?? undefined,
          output: notice ? `${compact}\n\n${notice}` : compact,
          truncated: stdoutTruncated || stderrTruncated,
          ...(timedOut ? { timedOut: true } : {}),
          ...(warnings.length ? { warnings } : {}),
        }));
      });
    });

    return result;
  });
}

/**
 * Run bash and return a human-readable result string for the ToolExecutor API.
 */
export function runBashCommand(workspacePath: string, rawInput: Record<string, any>): Effect.Effect<string, Error> {
  return Effect.gen(function* () {
    const validated = yield* Schema.decodeUnknown(BashInput)(rawInput).pipe(
      Effect.mapError((e) => new Error(`Bash input validation: ${e.message}`)),
    );
    const output = yield* executeBash(validated, workspacePath);
    return modelOutput(output as BashOutput);
  });
}
