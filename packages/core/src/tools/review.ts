/**
 * Review Tools — stateless code review functions called by the Reviewer agent.
 *
 * Each tool reads code, constructs a specialized review prompt, calls an LLM
 * via the provided callback, and returns a structured result. The Reviewer agent
 * only sees results — not the raw code — keeping its context clean.
 *
 * These are TOOLS, not agents. No memory. No state. Pure function + LLM call.
 */

import { readFileSync, existsSync } from "fs";
import { join, resolve, relative } from "path";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ReviewIssue {
  severity: "critical" | "major" | "minor" | "info";
  file?: string;
  line?: number;
  title: string;
  description: string;
  suggestion?: string;
}

export interface ReviewResult {
  passed: boolean;
  summary: string;
  issues: ReviewIssue[];
  score?: number; // 0-100
}

/** Callback to invoke the LLM for review. Takes system + user prompts, returns raw text. */
export type ReviewLLMCallback = (systemPrompt: string, userPrompt: string) => Promise<string>;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function safeReadFile(workspacePath: string, filePath: string): string | null {
  // Resolve to absolute and validate it stays within workspace (prevents ../ escape)
  const wsAbs = resolve(workspacePath);
  const fullPath = resolve(wsAbs, filePath);
  const rel = relative(wsAbs, fullPath);
  if (rel.startsWith("..") || fullPath === wsAbs) return null;
  if (!existsSync(fullPath)) return null;
  try {
    return readFileSync(fullPath, "utf-8");
  } catch {
    return null;
  }
}

function buildFileList(files: string[]): string {
  return files.map((f) => `  - ${f}`).join("\n");
}

function parseReviewResult(raw: string): ReviewResult {
  try {
    // Try to parse as JSON
    const json = JSON.parse(raw);
    return {
      passed: json.passed ?? (json.issues?.length === 0),
      summary: json.summary || "Review complete.",
      issues: json.issues || [],
      score: json.score,
    };
  } catch {
    // JSON parse failed — the LLM didn't return valid structured output.
    // Mark as FAIL so the Reviewer agent knows the review was incomplete.
    return {
      passed: false,
      score: undefined,
      summary: `⚠️ Review tool returned unstructured output — review could not be completed. Raw output: ${raw.slice(0, 500)}`,
      issues: [{
        severity: "critical",
        title: "Review parse failure",
        description: "The LLM returned output that could not be parsed as JSON. The review was NOT performed. Re-run the review.",
      }],
    };
  }
}

// ---------------------------------------------------------------------------
// 1. Code Review (5-axis: correctness, readability, architecture, security, perf)
// ---------------------------------------------------------------------------

const CODE_REVIEW_SYSTEM = `You are a senior code reviewer performing a five-axis review:
1. **Correctness** — bugs, edge cases, error handling gaps
2. **Readability** — naming, comments, complexity, clarity
3. **Architecture** — separation of concerns, coupling, patterns
4. **Security** — injection, auth, data exposure (not a full audit)
5. **Performance** — obvious bottlenecks, N+1 queries, memory leaks

Return ONLY valid JSON, no markdown or commentary:
{
  "passed": true/false,
  "score": 0-100,
  "summary": "<one-paragraph overall assessment>",
  "issues": [
    {
      "severity": "critical" | "major" | "minor" | "info",
      "file": "<file path>",
      "line": <number or null>,
      "title": "<short title>",
      "description": "<detailed explanation>",
      "suggestion": "<how to fix>"
    }
  ]
}
CRITICAL = security hole, data loss, crash. MAJOR = wrong behavior, broken feature.
MINOR = style, naming, minor duplication. INFO = observation, no action needed.`;

export async function runCodeReview(
  workspacePath: string,
  filePaths: string[],
  callLLM: ReviewLLMCallback,
): Promise<ReviewResult> {
  const files: Record<string, string> = {};
  const notFound: string[] = [];

  for (const fp of filePaths) {
    const content = safeReadFile(workspacePath, fp);
    if (content === null) {
      notFound.push(fp);
    } else {
      files[fp] = content;
    }
  }

  if (Object.keys(files).length === 0) {
    return {
      passed: true,
      score: 0,
      summary: `No files found to review. Checked: ${buildFileList(filePaths)}${notFound.length ? `\nNot found: ${buildFileList(notFound)}` : ""}`,
      issues: [],
    };
  }

  const userPrompt = Object.entries(files)
    .map(([path, code]) => `### ${path}\n\`\`\`\n${code.slice(0, 12000)}\n\`\`\``)
    .join("\n\n");

  const raw = await callLLM(CODE_REVIEW_SYSTEM, userPrompt);
  const result = parseReviewResult(raw);

  if (notFound.length > 0) {
    result.summary += `\n(Note: some files not found: ${notFound.join(", ")})`;
  }

  return result;
}

// ---------------------------------------------------------------------------
// 2. Security Audit
// ---------------------------------------------------------------------------

const SECURITY_AUDIT_SYSTEM = `You are a security engineer performing a focused vulnerability audit. Check for:
1. **OWASP Top 10** — injection, broken auth, sensitive data exposure, XXE, access control, misconfig, XSS, deserialization, known vulns, logging gaps
2. **Secrets & Keys** — hardcoded API keys, tokens, passwords, private keys
3. **Input Validation** — missing sanitization, unsafe deserialization, prototype pollution
4. **Auth & Authz** — missing auth checks, privilege escalation paths, session issues
5. **Dependencies** — note any risky imports or patterns (can't check versions)

Return ONLY valid JSON:
{
  "passed": true/false,
  "score": 0-100,
  "summary": "<one-paragraph assessment>",
  "issues": [
    {
      "severity": "critical" | "major" | "minor" | "info",
      "file": "<file path>",
      "line": <number or null>,
      "title": "<short title>",
      "description": "<detailed explanation>",
      "suggestion": "<how to fix>"
    }
  ]
}
CRITICAL = exploitable vulnerability, exposed secret. MAJOR = insecure pattern, missing protection.
MINOR = best-practice deviation. INFO = observation.`;

export async function runSecurityAudit(
  workspacePath: string,
  filePaths: string[],
  callLLM: ReviewLLMCallback,
): Promise<ReviewResult> {
  const files: Record<string, string> = {};
  const notFound: string[] = [];

  for (const fp of filePaths) {
    const content = safeReadFile(workspacePath, fp);
    if (content === null) {
      notFound.push(fp);
    } else {
      files[fp] = content;
    }
  }

  if (Object.keys(files).length === 0) {
    return {
      passed: true,
      score: 0,
      summary: `No files found to audit. Checked: ${buildFileList(filePaths)}`,
      issues: [],
    };
  }

  const userPrompt = Object.entries(files)
    .map(([path, code]) => `### ${path}\n\`\`\`\n${code.slice(0, 12000)}\n\`\`\``)
    .join("\n\n");

  const raw = await callLLM(SECURITY_AUDIT_SYSTEM, userPrompt);
  return parseReviewResult(raw);
}

// ---------------------------------------------------------------------------
// 3. Test Execution (analyzes test coverage & runs tests)
// ---------------------------------------------------------------------------

const TEST_REVIEW_SYSTEM = `You are a QA engineer analyzing test quality and coverage. Review the following code and tests:

1. **Coverage gaps** — which code paths are untested?
2. **Test quality** — are tests meaningful or just coverage padding?
3. **Edge cases** — missing boundary conditions, error paths, null/undefined
4. **Test structure** — clarity, isolation, setup/teardown
5. **Missing test types** — unit, integration, snapshot, e2e gaps

Return ONLY valid JSON:
{
  "passed": true/false,
  "score": 0-100,
  "summary": "<one-paragraph assessment>",
  "issues": [
    {
      "severity": "critical" | "major" | "minor" | "info",
      "file": "<file path>",
      "line": <number or null>,
      "title": "<short title>",
      "description": "<detailed explanation>",
      "suggestion": "<how to fix>"
    }
  ]
}
CRITICAL = core logic completely untested, broken test. MAJOR = significant coverage gap.
MINOR = weak assertions, missing edge-case test. INFO = style suggestion.`;

export async function runTestReview(
  workspacePath: string,
  sourceFiles: string[],
  testFiles: string[],
  callLLM: ReviewLLMCallback,
): Promise<ReviewResult> {
  const sourceContents: Record<string, string> = {};
  const testContents: Record<string, string> = {};
  const notFound: string[] = [];

  for (const fp of sourceFiles) {
    const content = safeReadFile(workspacePath, fp);
    if (content) sourceContents[fp] = content;
    else notFound.push(fp);
  }
  for (const fp of testFiles) {
    const content = safeReadFile(workspacePath, fp);
    if (content) testContents[fp] = content;
    else notFound.push(fp);
  }

  if (Object.keys(sourceContents).length === 0) {
    return {
      passed: true,
      score: 0,
      summary: "No source files found to analyze for test coverage.",
      issues: [],
    };
  }

  const sourceBlock = Object.entries(sourceContents)
    .map(([path, code]) => `### ${path}\n\`\`\`\n${code.slice(0, 8000)}\n\`\`\``)
    .join("\n\n");

  const testBlock = Object.keys(testContents).length > 0
    ? "\n\n## Test Files\n\n" + Object.entries(testContents)
        .map(([path, code]) => `### ${path}\n\`\`\`\n${code.slice(0, 8000)}\n\`\`\``)
        .join("\n\n")
    : "\n\n(No test files provided — review source for testability and suggest what tests are needed)";

  const raw = await callLLM(TEST_REVIEW_SYSTEM, sourceBlock + testBlock);
  return parseReviewResult(raw);
}

// ---------------------------------------------------------------------------
// 4. Web Performance Audit
// ---------------------------------------------------------------------------

const PERF_AUDIT_SYSTEM = `You are a web performance engineer auditing frontend code. Check for:

1. **Bundle size** — large imports, tree-shaking issues, duplicate deps
2. **Rendering** — unnecessary re-renders, missing memo, large component trees
3. **Loading** — missing lazy loading, code splitting gaps, waterfall requests
4. **Network** — unoptimized assets, missing compression hints, chatty APIs
5. **Runtime** — memory leaks (event listeners, intervals), heavy computations on main thread
6. **Images & Assets** — missing srcset, unoptimized formats, layout shift

Return ONLY valid JSON:
{
  "passed": true/false,
  "score": 0-100,
  "summary": "<one-paragraph assessment>",
  "issues": [
    {
      "severity": "critical" | "major" | "minor" | "info",
      "file": "<file path>",
      "line": <number or null>,
      "title": "<short title>",
      "description": "<detailed explanation>",
      "suggestion": "<how to fix>"
    }
  ]
}
CRITICAL = blocking perf issue (>3s impact). MAJOR = significant slowdown.
MINOR = optimization opportunity. INFO = observation.`;

export async function runPerfAudit(
  workspacePath: string,
  filePaths: string[],
  callLLM: ReviewLLMCallback,
): Promise<ReviewResult> {
  const files: Record<string, string> = {};
  const notFound: string[] = [];

  for (const fp of filePaths) {
    const content = safeReadFile(workspacePath, fp);
    if (content === null) {
      notFound.push(fp);
    } else {
      files[fp] = content;
    }
  }

  if (Object.keys(files).length === 0) {
    return {
      passed: true,
      score: 0,
      summary: `No files found to audit. Checked: ${buildFileList(filePaths)}`,
      issues: [],
    };
  }

  const userPrompt = Object.entries(files)
    .map(([path, code]) => `### ${path}\n\`\`\`\n${code.slice(0, 12000)}\n\`\`\``)
    .join("\n\n");

  const raw = await callLLM(PERF_AUDIT_SYSTEM, userPrompt);
  return parseReviewResult(raw);
}

// ---------------------------------------------------------------------------
// Combined: run all 4 reviews at once
// ---------------------------------------------------------------------------

export async function runFullReview(
  workspacePath: string,
  filePaths: string[],
  testFiles: string[],
  callLLM: ReviewLLMCallback,
): Promise<{
  codeReview: ReviewResult;
  securityAudit: ReviewResult;
  testReview: ReviewResult;
  perfAudit: ReviewResult;
  overallScore: number;
  overallPassed: boolean;
}> {
  // Use allSettled — transient failure on one dimension doesn't discard the other three
  const results = await Promise.allSettled([
    runCodeReview(workspacePath, filePaths, callLLM),
    runSecurityAudit(workspacePath, filePaths, callLLM),
    runTestReview(workspacePath, filePaths, testFiles, callLLM),
    runPerfAudit(workspacePath, filePaths, callLLM),
  ]);

  const fallback = (reason: any): ReviewResult => ({
    passed: false,
    score: undefined,
    summary: `Review failed: ${reason?.message || reason || "unknown error"}`,
    issues: [{ severity: "critical", title: "Review execution failed", description: String(reason || "unknown").slice(0, 500) }],
  });

  const codeReview    = results[0].status === "fulfilled" ? results[0].value : fallback(results[0].reason);
  const securityAudit = results[1].status === "fulfilled" ? results[1].value : fallback(results[1].reason);
  const testReview    = results[2].status === "fulfilled" ? results[2].value : fallback(results[2].reason);
  const perfAudit     = results[3].status === "fulfilled" ? results[3].value : fallback(results[3].reason);

  const allResults = [codeReview, securityAudit, testReview, perfAudit];

  // Count how many reviews actually analyzed files (not "no files found" empty reviews)
  const effectiveResults = allResults.filter((r) => r.score !== undefined && !r.summary.startsWith("No files found"));
  const scores = effectiveResults.map((r) => r.score!).filter((s) => s !== undefined);
  const overallScore = scores.length > 0
    ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length)
    : null as any as number; // null signals "no effective reviews"
  const overallPassed = effectiveResults.length > 0
    ? effectiveResults.every((r) => r.passed)
    : false; // nothing was actually reviewed

  return { codeReview, securityAudit, testReview, perfAudit, overallScore, overallPassed };
}
