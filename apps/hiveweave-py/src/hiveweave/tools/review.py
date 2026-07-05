"""review tool — 5 specialized code review functions.

契约 02: 工具执行器 — review 子模块
- runCodeReview    — 5-axis: correctness, readability, architecture, security, perf
- runSecurityAudit — OWASP Top 10 + secrets + input validation + auth + deps
- runTestReview    — coverage gaps, test quality, edge cases, structure
- runPerfAudit     — bundle, rendering, loading, network, runtime, assets
- runFullReview    — runs all 4 in parallel (asyncio.gather + return_exceptions)

Each review:
  1. Reads file contents from workspace (sandboxed)
  2. Builds a specialized system prompt
  3. Calls an LLM via a provided callback (system_prompt, user_prompt) -> text
  4. Parses JSON output; on parse failure returns a structured failure result

The LLM callback is injected by the caller (no direct model registry dependency).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog

from hiveweave.tools.security import is_sensitive_path

log = structlog.get_logger(__name__)

# ── Types ───────────────────────────────────────────────────

ReviewLLMCallback = Callable[[str, str], Awaitable[str]]


# ── System prompts ─────────────────────────────────────────

CODE_REVIEW_SYSTEM = (
    "You are a senior code reviewer performing a five-axis review:\n"
    "1. **Correctness** — bugs, edge cases, error handling gaps\n"
    "2. **Readability** — naming, comments, complexity, clarity\n"
    "3. **Architecture** — separation of concerns, coupling, patterns\n"
    "4. **Security** — injection, auth, data exposure (not a full audit)\n"
    "5. **Performance** — obvious bottlenecks, N+1 queries, memory leaks\n\n"
    "Return ONLY valid JSON, no markdown or commentary:\n"
    "{\n"
    '  "passed": true/false,\n'
    '  "score": 0-100,\n'
    '  "summary": "<one-paragraph overall assessment>",\n'
    '  "issues": [\n'
    "    {\n"
    '      "severity": "critical" | "major" | "minor" | "info",\n'
    '      "file": "<file path>",\n'
    '      "line": <number or null>,\n'
    '      "title": "<short title>",\n'
    '      "description": "<detailed explanation>",\n'
    '      "suggestion": "<how to fix>"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "CRITICAL = security hole, data loss, crash. "
    "MAJOR = wrong behavior, broken feature. "
    "MINOR = style, naming, minor duplication. "
    "INFO = observation, no action needed."
)

SECURITY_AUDIT_SYSTEM = (
    "You are a security engineer performing a focused vulnerability audit. "
    "Check for:\n"
    "1. **OWASP Top 10** — injection, broken auth, sensitive data exposure, "
    "XXE, access control, misconfig, XSS, deserialization, known vulns, "
    "logging gaps\n"
    "2. **Secrets & Keys** — hardcoded API keys, tokens, passwords, "
    "private keys\n"
    "3. **Input Validation** — missing sanitization, unsafe deserialization, "
    "prototype pollution\n"
    "4. **Auth & Authz** — missing auth checks, privilege escalation paths, "
    "session issues\n"
    "5. **Dependencies** — note any risky imports or patterns "
    "(can't check versions)\n\n"
    "Return ONLY valid JSON:\n"
    "{\n"
    '  "passed": true/false,\n'
    '  "score": 0-100,\n'
    '  "summary": "<one-paragraph assessment>",\n'
    '  "issues": [\n'
    "    {\n"
    '      "severity": "critical" | "major" | "minor" | "info",\n'
    '      "file": "<file path>",\n'
    '      "line": <number or null>,\n'
    '      "title": "<short title>",\n'
    '      "description": "<detailed explanation>",\n'
    '      "suggestion": "<how to fix>"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "CRITICAL = exploitable vulnerability, exposed secret. "
    "MAJOR = insecure pattern, missing protection. "
    "MINOR = best-practice deviation. INFO = observation."
)

TEST_REVIEW_SYSTEM = (
    "You are a QA engineer analyzing test quality and coverage. Review the "
    "following code and tests:\n\n"
    "1. **Coverage gaps** — which code paths are untested?\n"
    "2. **Test quality** — are tests meaningful or just coverage padding?\n"
    "3. **Edge cases** — missing boundary conditions, error paths, "
    "null/undefined\n"
    "4. **Test structure** — clarity, isolation, setup/teardown\n"
    "5. **Missing test types** — unit, integration, snapshot, e2e gaps\n\n"
    "Return ONLY valid JSON:\n"
    "{\n"
    '  "passed": true/false,\n'
    '  "score": 0-100,\n'
    '  "summary": "<one-paragraph assessment>",\n'
    '  "issues": [...]\n'
    "}\n"
    "CRITICAL = core logic completely untested, broken test. "
    "MAJOR = significant coverage gap. "
    "MINOR = weak assertions, missing edge-case test. "
    "INFO = style suggestion."
)

PERF_AUDIT_SYSTEM = (
    "You are a web performance engineer auditing frontend code. Check for:\n\n"
    "1. **Bundle size** — large imports, tree-shaking issues, duplicate deps\n"
    "2. **Rendering** — unnecessary re-renders, missing memo, "
    "large component trees\n"
    "3. **Loading** — missing lazy loading, code splitting gaps, "
    "waterfall requests\n"
    "4. **Network** — unoptimized assets, missing compression hints, "
    "chatty APIs\n"
    "5. **Runtime** — memory leaks (event listeners, intervals), "
    "heavy computations on main thread\n"
    "6. **Images & Assets** — missing srcset, unoptimized formats, "
    "layout shift\n\n"
    "Return ONLY valid JSON:\n"
    "{\n"
    '  "passed": true/false,\n'
    '  "score": 0-100,\n'
    '  "summary": "<one-paragraph assessment>",\n'
    '  "issues": [...]\n'
    "}\n"
    "CRITICAL = blocking perf issue (>3s impact). "
    "MAJOR = significant slowdown. "
    "MINOR = optimization opportunity. INFO = observation."
)

# Map of review_type -> system prompt
_REVIEW_PROMPTS = {
    "code_review": CODE_REVIEW_SYSTEM,
    "security_audit": SECURITY_AUDIT_SYSTEM,
    "test_review": TEST_REVIEW_SYSTEM,
    "perf_audit": PERF_AUDIT_SYSTEM,
}


# ── Helpers ─────────────────────────────────────────────────

def _safe_read_file(workspace_path: str, file_path: str) -> str | None:
    """Read a file from the workspace; return None if not found / escapes."""
    if not file_path:
        return None
    # 敏感文件保护（C6）— 不读取 .env / *.pem / credentials 等内容
    if is_sensitive_path(file_path):
        log.warning("review.blocked_sensitive_file", path=file_path)
        return None
    try:
        ws = Path(workspace_path).resolve()
        candidate = Path(file_path)
        if candidate.is_absolute():
            try:
                rel = candidate.relative_to(ws)
                full = ws / rel
            except ValueError:
                return None
        else:
            full = (ws / file_path).resolve()
        if full != ws:
            try:
                full.relative_to(ws)
            except ValueError:
                return None
        if not full.exists() or not full.is_file():
            return None
        return full.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _build_file_list(files: list[str]) -> str:
    return "\n".join(f"  - {f}" for f in files)


def _parse_review_result(raw: str) -> dict[str, Any]:
    """Parse LLM output as a review result dict."""
    try:
        parsed = json.loads(raw)
        return {
            "passed": bool(parsed.get("passed",
                                       not parsed.get("issues"))),
            "summary": parsed.get("summary") or "Review complete.",
            "issues": parsed.get("issues") or [],
            "score": parsed.get("score"),
        }
    except (json.JSONDecodeError, TypeError):
        return {
            "passed": False,
            "score": None,
            "summary": ("Review tool returned unstructured output — review "
                        "could not be completed. Raw output: "
                        f"{raw[:500]}"),
            "issues": [{
                "severity": "critical",
                "title": "Review parse failure",
                "description": ("The LLM returned output that could not be "
                                "parsed as JSON. The review was NOT "
                                "performed. Re-run the review."),
            }],
        }


def _format_result(result: dict[str, Any]) -> str:
    """Format a review result dict as a human-readable string for the LLM."""
    lines: list[str] = []
    score = result.get("score")
    score_str = f"{score}/100" if score is not None else "N/A"
    lines.append(f"Passed: {result.get('passed')}")
    lines.append(f"Score: {score_str}")
    lines.append(f"Summary: {result.get('summary', '')}")
    issues = result.get("issues") or []
    if issues:
        lines.append(f"Issues ({len(issues)}):")
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "?")
            title = issue.get("title", "(no title)")
            file = issue.get("file", "?")
            line = issue.get("line")
            loc = f"{file}:{line}" if line else file
            desc = issue.get("description", "")
            sugg = issue.get("suggestion", "")
            lines.append(f"  {i}. [{sev}] {title} ({loc})")
            if desc:
                lines.append(f"     {desc}")
            if sugg:
                lines.append(f"     Suggestion: {sugg}")
    return "\n".join(lines)


async def _execute_single_review(
    review_type: str,
    source_files: list[str],
    test_files: list[str],
    workspace_path: str,
    call_llm: ReviewLLMCallback,
) -> dict[str, Any]:
    """Run a single review (code/security/test/perf)."""
    system_prompt = _REVIEW_PROMPTS.get(review_type)
    if system_prompt is None:
        return {"passed": False, "summary": f"Unknown review type: {review_type}",
                "issues": [], "score": None}

    files: dict[str, str] = {}
    not_found: list[str] = []
    for fp in source_files:
        content = _safe_read_file(workspace_path, fp)
        if content is None:
            not_found.append(fp)
        else:
            files[fp] = content[:12000]

    if not files:
        return {
            "passed": True,
            "score": 0,
            "summary": (f"No files found to review. Checked: "
                        f"{_build_file_list(source_files)}"
                        + (f"\nNot found: {_build_file_list(not_found)}"
                           if not_found else "")),
            "issues": [],
        }

    user_parts = [f"### {path}\n```\n{code}\n```"
                  for path, code in files.items()]
    user_prompt = "\n\n".join(user_parts)

    if test_files:
        test_blocks: list[str] = []
        for fp in test_files:
            content = _safe_read_file(workspace_path, fp)
            if content is not None:
                test_blocks.append(f"### {fp}\n```\n{content[:8000]}\n```")
        if test_blocks:
            user_prompt += "\n\n## Test Files\n\n" + "\n\n".join(test_blocks)

    try:
        raw = await call_llm(system_prompt, user_prompt)
    except Exception as exc:  # noqa: BLE001
        return {
            "passed": False,
            "score": None,
            "summary": f"Review failed: {exc}",
            "issues": [{
                "severity": "critical",
                "title": "Review execution failed",
                "description": str(exc)[:500],
            }],
        }

    result = _parse_review_result(raw)
    if not_found:
        result["summary"] += (
            f"\n(Note: some files not found: {', '.join(not_found)})"
        )
    return result


# ── Public API ─────────────────────────────────────────────

async def run_code_review(
    workspace_path: str,
    file_paths: list[str],
    call_llm: ReviewLLMCallback,
) -> dict[str, Any]:
    """5-axis code review."""
    return await _execute_single_review(
        "code_review", file_paths, [], workspace_path, call_llm
    )


async def run_security_audit(
    workspace_path: str,
    file_paths: list[str],
    call_llm: ReviewLLMCallback,
) -> dict[str, Any]:
    """Focused security audit."""
    return await _execute_single_review(
        "security_audit", file_paths, [], workspace_path, call_llm
    )


async def run_test_review(
    workspace_path: str,
    source_files: list[str],
    test_files: list[str],
    call_llm: ReviewLLMCallback,
) -> dict[str, Any]:
    """Test quality and coverage analysis."""
    return await _execute_single_review(
        "test_review", source_files, test_files, workspace_path, call_llm
    )


async def run_perf_audit(
    workspace_path: str,
    file_paths: list[str],
    call_llm: ReviewLLMCallback,
) -> dict[str, Any]:
    """Web performance audit."""
    return await _execute_single_review(
        "perf_audit", file_paths, [], workspace_path, call_llm
    )


async def run_full_review(
    workspace_path: str,
    file_paths: list[str],
    test_files: list[str],
    call_llm: ReviewLLMCallback,
) -> dict[str, Any]:
    """Run all 4 reviews in parallel; aggregate scores."""
    results = await asyncio.gather(
        run_code_review(workspace_path, file_paths, call_llm),
        run_security_audit(workspace_path, file_paths, call_llm),
        run_test_review(workspace_path, file_paths, test_files, call_llm),
        run_perf_audit(workspace_path, file_paths, call_llm),
        return_exceptions=True,
    )

    fallback = lambda reason: {
        "passed": False,
        "score": None,
        "summary": f"Review failed: {reason}",
        "issues": [{
            "severity": "critical",
            "title": "Review execution failed",
            "description": str(reason)[:500],
        }],
    }

    code_review = results[0] if not isinstance(results[0], Exception) \
        else fallback(results[0])
    security_audit = results[1] if not isinstance(results[1], Exception) \
        else fallback(results[1])
    test_review = results[2] if not isinstance(results[2], Exception) \
        else fallback(results[2])
    perf_audit = results[3] if not isinstance(results[3], Exception) \
        else fallback(results[3])

    all_results = [code_review, security_audit, test_review, perf_audit]
    effective = [r for r in all_results
                 if r.get("score") is not None
                 and not r.get("summary", "").startswith("No files found")]
    scores = [r["score"] for r in effective if r.get("score") is not None]
    overall_score = (
        round(sum(scores) / len(scores)) if scores else 0
    )
    overall_passed = (
        all(r.get("passed") for r in effective) if effective else False
    )

    return {
        "codeReview": code_review,
        "securityAudit": security_audit,
        "testReview": test_review,
        "perfAudit": perf_audit,
        "overallScore": overall_score,
        "overallPassed": overall_passed,
    }


# ── Tool entrypoint ────────────────────────────────────────

async def execute_review(
    review_type: str,
    file_paths: list[str],
    test_files: list[str] | None,
    workspace_path: str,
    call_llm: ReviewLLMCallback | None = None,
) -> dict[str, Any]:
    """Dispatch a review by type. Returns {success, output, error}.

    review_type: "code_review" | "security_audit" | "test_review" |
                 "perf_audit" | "full_review"
    """
    if not file_paths:
        return {"success": False, "output": "",
                "error": "Error: filePaths is required "
                         "(list of file paths to review)"}

    if call_llm is None:
        return {"success": False, "output": "",
                "error": "Error: No LLM callback configured for review"}

    test_files = test_files or []

    try:
        if review_type == "code_review":
            result = await run_code_review(workspace_path, file_paths, call_llm)
        elif review_type == "security_audit":
            result = await run_security_audit(workspace_path, file_paths,
                                              call_llm)
        elif review_type == "test_review":
            result = await run_test_review(workspace_path, file_paths,
                                           test_files, call_llm)
        elif review_type == "perf_audit":
            result = await run_perf_audit(workspace_path, file_paths, call_llm)
        elif review_type == "full_review":
            full = await run_full_review(workspace_path, file_paths,
                                         test_files, call_llm)
            # Format the full review as a combined report
            parts = [
                "## Code Review",
                _format_result(full["codeReview"]),
                "",
                "## Security Audit",
                _format_result(full["securityAudit"]),
                "",
                "## Test Review",
                _format_result(full["testReview"]),
                "",
                "## Performance Audit",
                _format_result(full["perfAudit"]),
                "",
                "---",
                (f'Overall Score: {full["overallScore"]}/100 — '
                 f'{"PASS" if full["overallPassed"] else "FAIL"}'),
            ]
            return {"success": True, "output": "\n".join(parts),
                    "error": None}
        else:
            return {"success": False, "output": "",
                    "error": f"Error: Unknown review type: {review_type}"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "output": "",
                "error": f"Error: Review failed — {exc}"}

    return {"success": True, "output": _format_result(result), "error": None}
