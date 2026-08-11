"""Code-audit domain core behind the ``request_code_audit`` tool.

Agent-level (key=agent_id) in-memory change ledger + worktree diff
collection (incl. untracked files) + fixed-executor-tier one-shot LLM
audit + attestation + report file.

Soft-gate by design: nothing here raises or blocks — every step degrades
to a soft-fail dict with a machine-readable ``reason``.
"""
from __future__ import annotations

import time
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

CODE_AUDIT_LINE_THRESHOLD = 20
CODE_AUDIT_KIND = "code_audit"
CODE_AUDIT_POLICY = (
    "[CODE AUDIT POLICY] If your code edits total more than 20 lines, "
    "call request_code_audit(taskId=...) BEFORE submit_task."
)
CODE_AUDIT_REMINDER = (
    "[CODE AUDIT REMINDER] Your code edits exceed 20 lines and no fresh "
    "code audit attestation exists. Call request_code_audit(taskId=...) "
    "before submit_task to get an independent LLM audit of your worktree diff."
)

_DIFF_CAP_BYTES = 50 * 1024
_DIFF_CAP_MARKER = "\n... [diff truncated at 50KB] ...\n"
_TOP_ISSUES_MAX = 5
_TOP_ISSUE_MAX_CHARS = 120
_ISSUE_PARSE_CAP = 100


# ── Agent-level in-memory ledger ─────────────────────────────

_ledger: dict[str, int] = {}
_ledger_ts: dict[str, float] = {}


def record_change(agent_id: str, lines: int) -> None:
    """Accumulate edited-line counts for an agent (values clamped at >= 0)."""
    if lines <= 0:
        return
    _ledger[agent_id] = max(0, _ledger.get(agent_id, 0)) + lines
    _ledger_ts[agent_id] = time.time()


def get_unaudited_lines(agent_id: str) -> int:
    """Ledger value for an agent (0 when absent)."""
    return max(0, _ledger.get(agent_id, 0))


def get_last_change_ts(agent_id: str) -> float:
    """Epoch-seconds timestamp of the last recorded edit (0 when absent)."""
    return _ledger_ts.get(agent_id, 0.0)


def reset_ledger(agent_id: str) -> None:
    """Clear the ledger entry for an agent."""
    _ledger.pop(agent_id, None)
    _ledger_ts.pop(agent_id, None)


def ledger_snapshot() -> dict[str, int]:
    """Test helper — copy of the current ledger state."""
    return dict(_ledger)


# ── Line counting (parameter heuristic, language-agnostic) ───

def count_change_lines(tool_name: str, params: dict) -> int:
    """Estimate edited lines from tool params (no output-text parsing).

    write_file → content line count; edit_file → max(old, new);
    apply_patch → sum per op (add: content; update: max(old, new); delete: 0).
    Unknown tool / missing values → 0.
    """
    def _split_lines(value: object) -> int:
        if value is None:
            return 0
        return len(str(value).splitlines())

    def _first(params: dict, *keys: str) -> object:
        for key in keys:
            value = params.get(key)
            if value is not None:
                return value
        return None

    if not params:
        return 0
    if tool_name == "write_file":
        return _split_lines(params.get("content"))
    if tool_name == "edit_file":
        return max(
            _split_lines(_first(params, "old_string", "oldString", "old_str", "oldText", "search")),
            _split_lines(_first(params, "new_string", "newString", "new_str", "newText", "replacement")),
        )
    if tool_name == "apply_patch":
        total = 0
        for patch in params.get("patches") or []:
            if not isinstance(patch, dict):
                continue
            op = (patch.get("op") or "").strip().lower()
            if op == "add":
                total += _split_lines(patch.get("content"))
            elif op == "update":
                total += max(
                    _split_lines(_first(patch, "old_string", "oldString", "old_str", "oldText", "search")),
                    _split_lines(_first(patch, "new_string", "newString", "new_str", "newText", "replacement")),
                )
            # delete / unknown ops count 0
        return total
    return 0


# ── Worktree diff collection ─────────────────────────────────

async def _run_git(args: list[str], worktree_path: str) -> tuple[bool, str]:
    """Lazy-imported ``_git`` (patchable at the package level, no cycles)."""
    from hiveweave.services.git_worktree import _git

    try:
        return await _git(args, worktree_path)
    except Exception as exc:
        log.warning("code_audit.git_failed", error=str(exc))
        return False, ""


async def _resolve_base_branch_lazy(worktree_path: str) -> str | None:
    """Resolve the default base branch (main → master). Fail-open → None."""
    try:
        from hiveweave.services.git_worktree import _resolve_base_branch

        return await _resolve_base_branch(worktree_path)
    except Exception as exc:  # noqa: BLE001 — soft-fail contract
        log.warning("code_audit.base_branch_failed", error=str(exc))
        return None


async def collect_worktree_diff(worktree_path: str) -> str:
    """Collect the worktree diff: <base>...HEAD, HEAD (uncommitted), untracked.

    Base branch is resolved at runtime (main → master fallback) so repos
    whose default branch is not ``main`` still get their committed branch
    changes audited. Untracked files are listed via
    ``git ls-files --others --exclude-standard`` and their content read
    directly — otherwise brand-new files would render an empty diff and get
    auto-PASSed. Total output capped at ~50KB with a truncation marker.
    Empty-safe; never raises.
    """
    parts: list[str] = []
    budget = _DIFF_CAP_BYTES

    base = await _resolve_base_branch_lazy(worktree_path)
    if base:
        ok, out = await _run_git(["diff", f"{base}...HEAD"], worktree_path)
        if ok and out:
            parts.append(f"== diff {base}...HEAD ==")
            parts.append(out)
    ok, out = await _run_git(["diff", "HEAD"], worktree_path)
    if ok and out:
        parts.append("== diff HEAD (uncommitted) ==")
        parts.append(out)

    ok, files = await _run_git(
        ["ls-files", "--others", "--exclude-standard"], worktree_path
    )
    if ok and files:
        for rel in files.splitlines():
            rel = rel.strip()
            if not rel:
                continue
            header = f"== untracked {rel} =="
            path = Path(worktree_path) / rel
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read(max(0, budget))
            except OSError as exc:
                parts.append(f"== untracked {rel} (unreadable: {exc}) ==")
                continue
            parts.append(header)
            parts.append(content)
            budget -= len((header + "\n\n" + content).encode("utf-8", errors="replace"))
            if budget <= 0:
                break

    text = "\n\n".join(parts)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > _DIFF_CAP_BYTES:
        keep = _DIFF_CAP_BYTES - len(_DIFF_CAP_MARKER.encode("utf-8"))
        text = encoded[:keep].decode("utf-8", errors="replace") + _DIFF_CAP_MARKER
    return text


# ── Audit prompt (platform English constants) ────────────────

def build_audit_prompt(diff: str, task_id: str | None) -> tuple[str, str]:
    """Build (system, user) prompt pair for the audit call.

    Platform English constants only (protocol, language-agnostic). The
    model's reply MUST start with ``VERDICT: PASS`` or ``VERDICT: ISSUES``.
    """
    system = (
        "You are an independent code reviewer auditing an agent's worktree "
        "diff. Assess correctness, security, and obvious defects. "
        "Your reply MUST start with exactly one line: 'VERDICT: PASS' or "
        "'VERDICT: ISSUES'. When the verdict is ISSUES, follow with one "
        "line per problem in the form: <file>:<line> [<severity>] <one-line "
        "reason>, severity in high/medium/low. Do not use markdown fences."
    )
    context = (
        f"task context: {task_id}" if task_id else "task context: not provided"
    )
    return system, f"{context}\n\nworktree diff:\n{diff}"


def _parse_verdict(text: str) -> str:
    """First line must be ``VERDICT: PASS``; anything else is ISSUES."""
    lines = (text or "").strip().splitlines()
    if not lines:
        return "ISSUES"
    first = lines[0].strip().upper()
    return "PASS" if first.startswith("VERDICT: PASS") else "ISSUES"


def _parse_issues(text: str) -> list[str]:
    """Issue lines after the verdict line (bullets stripped, capped)."""
    issues: list[str] = []
    for line in (text or "").splitlines()[1:]:
        stripped = line.strip().lstrip("-*•").strip()
        if not stripped or stripped.upper().startswith("VERDICT"):
            continue
        issues.append(stripped[:_TOP_ISSUE_MAX_CHARS])
        if len(issues) >= _ISSUE_PARSE_CAP:
            break
    return issues


# ── Audit runner ─────────────────────────────────────────────

async def run_code_audit(
    project_id: str, agent_id: str, task_id: str | None = None
) -> dict:
    """Run a code audit for an agent's worktree. Never raises.

    Return contract (soft-fail dicts):
      - ``{"audited": False, "reason": "no_worktree" | "no_model" | "llm_failed" | "error"}``
      - ``{"audited": True, "verdict": "PASS" | "ISSUES", ...}``
    """
    try:
        from hiveweave.services.worktree_review import agent_worktree_path

        worktree = await agent_worktree_path(agent_id)
        if not worktree:
            return {"audited": False, "reason": "no_worktree"}

        diff = await collect_worktree_diff(worktree)

        from hiveweave.services.attestation import (
            attestation_service,
            hash_stdout,
        )

        commit_hash: str | None = None
        ok, head = await _run_git(["rev-parse", "HEAD"], worktree)
        if ok and head:
            commit_hash = head.strip()[:40]

        # Auto-PASS without an LLM call: nothing changed and the ledger is
        # within the soft threshold.
        if (
            not diff.strip()
            and get_unaudited_lines(agent_id) <= CODE_AUDIT_LINE_THRESHOLD
        ):
            attestation_id = await attestation_service.create(
                project_id,
                agent_id=agent_id,
                kind=CODE_AUDIT_KIND,
                task_id=task_id,
                exit_code=0,
                workspace=worktree,
                commit_hash=commit_hash,
                stdout_hash=hash_stdout("no changes to audit"),
            )
            return {
                "audited": True,
                "verdict": "PASS",
                "lines_audited": 0,
                "attestation_id": attestation_id,
            }

        # Fixed executor tier — audit calls must never burn management models.
        from hiveweave.services.model import ModelService

        if not await ModelService().resolve_model(tier="executor"):
            return {"audited": False, "reason": "no_model"}

        from hiveweave.llm.oneshot import llm_oneshot

        system, user = build_audit_prompt(diff, task_id)
        text = await llm_oneshot(project_id, "executor", system, user)
        if text is None:
            return {"audited": False, "reason": "llm_failed"}

        verdict = _parse_verdict(text)
        issues = _parse_issues(text)
        exit_code = 0 if verdict == "PASS" else 1

        # Lazy import: tools.executor pulls the whole tool registry.
        from hiveweave.tools.executor import ToolExecutor

        report_path = ToolExecutor._save_tool_output_file(
            text, agent_id, CODE_AUDIT_KIND, worktree
        )

        attestation_id = await attestation_service.create(
            project_id,
            agent_id=agent_id,
            kind=CODE_AUDIT_KIND,
            task_id=task_id,
            exit_code=exit_code,
            workspace=worktree,
            commit_hash=commit_hash,
            stdout_hash=hash_stdout(text),
        )

        lines_audited = get_unaudited_lines(agent_id)
        reset_ledger(agent_id)
        return {
            "audited": True,
            "verdict": verdict,
            "issues_count": len(issues),
            "top_issues": issues[:_TOP_ISSUES_MAX],
            "report_path": report_path,
            "attestation_id": attestation_id,
            "lines_audited": lines_audited,
        }
    except Exception as exc:  # noqa: BLE001 — soft-fail contract
        log.warning("code_audit.error", agent_id=agent_id, error=str(exc))
        return {"audited": False, "reason": "error"}


# ── Shared notice helper ─────────────────────────────────────

def append_code_audit_notice(description: str) -> str:
    """Append CODE_AUDIT_POLICY to a description once (idempotent)."""
    if not description:
        return CODE_AUDIT_POLICY
    if CODE_AUDIT_POLICY in description:
        return description
    return f"{description}\n{CODE_AUDIT_POLICY}"
