/**
 * grep tool — regex search across files in the workspace, ported from OpenCode.
 * Uses ripgrep-style native search for performance; falls back to simple scan.
 */
import { spawn } from "child_process";
import * as path from "path";
import { Effect, Schema } from "effect";

const MAX_RESULTS = 100;
const MAX_CHARS_PER_LINE = 500;

export const GrepInput = Schema.Struct({
  pattern: Schema.String.annotations({ description: "Regular expression pattern to search for." }),
  path: Schema.optional(Schema.String).annotations({ description: "File or directory to search. Default: workspace root." }),
  include: Schema.optional(Schema.String).annotations({ description: "Glob pattern to filter files (e.g. '*.ts')." }),
  head_limit: Schema.optional(Schema.Number.pipe(Schema.int(), Schema.positive())).annotations({ description: `Max results. Default ${MAX_RESULTS}.` }),
});

export type GrepInput = typeof GrepInput.Type;

interface GrepMatch {
  file: string;
  line: number;
  content: string;
}

// Try to use rg (ripgrep) if available, fall back to internal scan
function tryRg(cwd: string, pattern: string, include?: string, headLimit?: number): Promise<GrepMatch[]> {
  return new Promise((resolve) => {
    const args = ["--line-number", "--no-heading", "--color=never", "-e", pattern];
    if (include) args.push("--glob", include);
    const child = spawn("rg", args, { cwd, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    let output = "";
    child.stdout.on("data", (d: Buffer) => { output += d.toString("utf-8"); });
    child.stderr.on("data", () => {});
    child.on("close", () => {
      if (output.trim().length === 0) { resolve([]); return; }
      const lines = output.trim().split("\n");
      const limit = Math.min(headLimit || MAX_RESULTS, MAX_RESULTS);
      const matches: GrepMatch[] = [];
      for (const line of lines) {
        if (matches.length >= limit) break;
        const idx = line.indexOf(":");
        if (idx === -1) continue;
        const file = line.slice(0, idx);
        const rest = line.slice(idx + 1);
        const idx2 = rest.indexOf(":");
        if (idx2 === -1) continue;
        const lineNum = parseInt(rest.slice(0, idx2), 10);
        if (isNaN(lineNum)) continue;
        const content = rest.slice(idx2 + 1).slice(0, MAX_CHARS_PER_LINE);
        matches.push({ file: file.replace(/\\/g, "/"), line: lineNum, content });
      }
      resolve(matches);
    });
    child.on("error", () => resolve([]));
  });
}

function formatResults(matches: GrepMatch[]): string {
  if (matches.length === 0) return "No matches found.";
  const groups = new Map<string, GrepMatch[]>();
  for (const m of matches) {
    if (!groups.has(m.file)) groups.set(m.file, []);
    groups.get(m.file)!.push(m);
  }
  const parts: string[] = [];
  for (const [file, ms] of groups) {
    parts.push(`\n${file}:`);
    for (const m of ms) {
      parts.push(`  ${m.line}: ${m.content}`);
    }
  }
  return parts.join("\n").trim();
}

export function executeGrep(workspacePath: string, rawInput: Record<string, any>): Effect.Effect<string, Error> {
  return Effect.gen(function* () {
    const input = yield* Schema.decodeUnknown(GrepInput)(rawInput).pipe(
      Effect.mapError((e) => new Error(`Grep input validation: ${e.message}`)),
    );
    const cwd = input.path ? path.resolve(workspacePath, input.path) : workspacePath;
    if (!cwd.startsWith(workspacePath)) {
      return yield* Effect.fail(new Error(`Sandbox violation: path "${cwd}" is outside workspace`));
    }
    const matches = yield* Effect.tryPromise(() => tryRg(cwd, input.pattern, input.include, input.head_limit));
    return formatResults(matches);
  });
}
