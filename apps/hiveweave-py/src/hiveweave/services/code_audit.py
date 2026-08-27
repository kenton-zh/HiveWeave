"""Code-audit domain core behind the ``request_code_audit`` tool.

Agent-level (key=agent_id) in-memory change ledger + worktree diff
collection (incl. untracked files) + one-shot LLM audit + attestation +
report file.

The HTTP path is the same one-shot sub-call as the legacy review suites
(``agents/agent.py:_oneshot_llm``). Model pick (2026-08-16): use a
**teammate's currently resolved model** whose vendor ``model_id`` differs
from the author's. Same callback stack — not a second LLM runtime. If
the live team only has one family, fall back to the author's own model
(do not invent unused catalog / backup slots).

Soft-gate by design: nothing here raises or blocks — every step degrades
to a soft-fail dict with a machine-readable ``reason``.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

from hiveweave.services.model import NoModelConfiguredError

log = structlog.get_logger(__name__)

# TEST_DSH_32 P7：审计是软门——LLM 故障不应烧满共享重试的 ~90s 才降级。
# 单次审计 one-shot 帽 45s，超时按 llm_failed 软放行（此前 90.9s×2 实锤）。
CODE_AUDIT_LLM_TIMEOUT_S = 45

# One-shot review callback contract — same shape as
# ``agents/agent.py:_review_llm_callback`` / tools/review.py ReviewLLMCallback.
# Declared locally (not imported from tools.review) to avoid a tools↔services
# package import cycle.
ReviewLLMCallback = Callable[[str, str], Awaitable[str]]
# (model_config, system, user) -> text. Same HTTP path, caller-chosen config.
OneshotLLMCallback = Callable[[dict, str, str], Awaitable[str]]

CODE_AUDIT_LINE_THRESHOLD = 20
CODE_AUDIT_KIND = "code_audit"
CODE_AUDIT_POLICY = (
    "[CODE AUDIT POLICY] If your code edits total more than 20 lines, "
    "call request_code_audit(taskId=...) BEFORE submit_task."
)
CODE_AUDIT_REMINDER = (
    "[CODE AUDIT REMINDER] Your code edits exceed 20 lines and no fresh "
    "code audit attestation exists. Call request_code_audit(taskId=...) "
    "before submit_task to get a second-pass LLM audit of your worktree diff."
)
CODE_AUDIT_REMINDER_LLM_FAILED = (
    "[CODE AUDIT REMINDER] Code audit was attempted but the LLM call failed "
    "(llm_failed). Retry request_code_audit(taskId=...) once, or continue "
    "with submit_task (soft gate — does not block)."
)
CODE_AUDIT_REMINDER_ATTEMPTED = (
    "[CODE AUDIT REMINDER] Code audit was attempted but did not produce an "
    "attestation ({reason}). Retry request_code_audit(taskId=...) once, or "
    "continue with submit_task (soft gate — does not block)."
)
_SOFT_FAIL_ATTEMPT_REASONS = frozenset({"llm_failed", "no_model", "no_callback"})

_DIFF_CAP_BYTES = 50 * 1024
_DIFF_CAP_MARKER = "\n... [diff truncated at 50KB] ...\n"
_TOP_ISSUES_MAX = 5
_TOP_ISSUE_MAX_CHARS = 120
_ISSUE_PARSE_CAP = 100


# ── Agent-level in-memory ledger ─────────────────────────────

_ledger: dict[str, int] = {}
_ledger_ts: dict[str, float] = {}
# Last request_code_audit attempt that did not produce an attestation.
# Separate from the line ledger so llm_failed is not mistaken for "never audited".
_last_attempt: dict[str, dict[str, Any]] = {}


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


def record_audit_attempt(
    agent_id: str, reason: str, task_id: str | None = None
) -> None:
    """Record a soft-fail audit attempt (no attestation created)."""
    if not agent_id:
        return
    _last_attempt[agent_id] = {
        "ts": time.time(),
        "reason": reason,
        "task_id": task_id,
    }


def get_last_audit_attempt(agent_id: str) -> dict[str, Any] | None:
    """Copy of the last soft-fail attempt, or None."""
    rec = _last_attempt.get(agent_id)
    return dict(rec) if rec else None


def _task_ref_match(left: str | None, right: str | None) -> bool:
    """Same task id, including dashed vs compact / prefix stubs."""
    if not left or not right:
        return False
    a = str(left).replace("-", "").strip().lower()
    b = str(right).replace("-", "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 8 and len(b) >= 8 and (a.startswith(b) or b.startswith(a))


def code_audit_soft_fail_covers(agent_id: str, task_id: str | None) -> bool:
    """True when this agent already attempted audit on *task_id* and soft-failed.

    Aligns the code_audit* submit gate with the tool/prompt contract:
    llm_failed / no_model / no_callback do not block submit.
    """
    rec = get_last_audit_attempt(agent_id)
    if not rec:
        return False
    if rec.get("reason") not in _SOFT_FAIL_ATTEMPT_REASONS:
        return False
    rec_tid = rec.get("task_id")
    if task_id and rec_tid:
        return _task_ref_match(str(task_id), str(rec_tid))
    # Unbound attempt (0/N active tasks at request_code_audit) covers
    # this agent's next submit. Strict match only when both ids exist.
    if task_id and not rec_tid:
        return True
    return not rec_tid


CODE_AUDIT_SOFT_FAIL_EVIDENCE_KEY = "code_audit_soft_fail"


def evidence_has_code_audit_soft_fail(evidence: dict | None) -> bool:
    """True when submit stamped a structured code-audit soft-fail on evidence."""
    if not isinstance(evidence, dict):
        return False
    stamp = evidence.get(CODE_AUDIT_SOFT_FAIL_EVIDENCE_KEY)
    if not isinstance(stamp, dict):
        return False
    return str(stamp.get("reason") or "") in _SOFT_FAIL_ATTEMPT_REASONS


def drop_code_audit_kind_if_soft(
    needed: frozenset[str] | None,
    *,
    agent_id: str | None = None,
    task_id: str | None = None,
    evidence: dict | None = None,
) -> tuple[frozenset[str] | None, bool]:
    """Drop CODE_AUDIT_KIND when evidence is stamped or in-memory attempt matches.

    Approve/HTTP must not re-require code_audit after submit already accepted
    llm_failed — in-memory ``_last_attempt`` is cleared on successful submit.
    """
    if not needed or CODE_AUDIT_KIND not in needed:
        return needed, False
    if evidence_has_code_audit_soft_fail(evidence):
        leftover = frozenset(k for k in needed if k != CODE_AUDIT_KIND)
        return leftover, True
    if agent_id and code_audit_soft_fail_covers(agent_id, task_id):
        leftover = frozenset(k for k in needed if k != CODE_AUDIT_KIND)
        return leftover, True
    return needed, False


def kinds_after_code_audit_soft_fail(
    needed: frozenset[str] | None,
    agent_id: str,
    task_id: str | None,
) -> tuple[frozenset[str] | None, bool]:
    """Submit-time wrapper: in-memory attempt only (evidence not written yet)."""
    return drop_code_audit_kind_if_soft(
        needed, agent_id=agent_id, task_id=task_id
    )


def code_audit_submit_reminder(agent_id: str) -> str:
    """Submit-time reminder when edits exceed the threshold and no fresh attestation.

    llm_failed / no_model / no_callback are worded as attempted-but-failed,
    not "never audited".
    HTTP retry already lives in ``agents.agent._review_llm_post_with_retry`` —
    do not double-retry here.
    """
    attempt = _last_attempt.get(agent_id)
    reason = (attempt or {}).get("reason")
    if reason == "llm_failed":
        return CODE_AUDIT_REMINDER_LLM_FAILED
    if reason in _SOFT_FAIL_ATTEMPT_REASONS:
        return CODE_AUDIT_REMINDER_ATTEMPTED.format(reason=reason)
    return CODE_AUDIT_REMINDER


def reset_ledger(agent_id: str) -> None:
    """Clear the ledger entry and last audit attempt for an agent."""
    _ledger.pop(agent_id, None)
    _ledger_ts.pop(agent_id, None)
    _last_attempt.pop(agent_id, None)


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
        "You are a second-pass code reviewer auditing an agent's worktree "
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


# ── Peer model pick (live team, different vendor model_id) ───

def model_family_key(config: dict | None) -> str:
    """Vendor model family: ``llm_models.model_id``, case-insensitive.

    Same weights behind two API keys / two DB rows still count as one
    family — the point of a peer audit is a different model, not a
    different quota.
    """
    if not config:
        return ""
    return (config.get("model_id") or "").strip().lower()


def select_peer_audit_model(
    author_family: str,
    author_tier: str,
    peers: list[dict[str, Any]],
) -> dict | None:
    """Pick one teammate config whose family ≠ author.

    ``peers`` items: ``{"tier": "management"|"executor", "config": dict}``.
    Prefer a different model tier (management ↔ executor); if several
    remain, lexicographic ``model_id`` then DB ``id`` so the choice is
    stable across calls. Returns None when the live team has no other
    family.
    """
    family = (author_family or "").strip().lower()
    if not family:
        return None
    distinct: list[dict[str, Any]] = []
    for peer in peers:
        cfg = peer.get("config") if isinstance(peer, dict) else None
        key = model_family_key(cfg)
        if not key or key == family:
            continue
        distinct.append(peer)
    if not distinct:
        return None
    other_tier = [
        p for p in distinct
        if (p.get("tier") or "") != author_tier
    ]
    pool = other_tier or distinct
    pool.sort(
        key=lambda p: (
            model_family_key(p.get("config")),
            (p.get("config") or {}).get("id") or "",
        )
    )
    return pool[0].get("config")


async def resolve_peer_audit_model(
    project_id: str,
    author_id: str,
) -> tuple[dict | None, str]:
    """Resolve (config, source) for the audit one-shot.

    ``source`` is ``"peer"`` when a live teammate's resolved model has a
    different vendor ``model_id``, else ``"own"``. Config is the author's
    model when no peer family exists. ``(None, "own")`` only if the
    author themselves has no resolvable model.
    """
    from hiveweave.services.model import ModelService
    from hiveweave.services.org import OrgService
    from hiveweave.services.policy import model_tier_for_agent

    org = OrgService()
    ms = ModelService()
    agents = await org.list_agents(project_id)
    author = next((a for a in agents if a.get("id") == author_id), None)
    if author is None:
        author = await org.get_agent(author_id)
    if not author:
        return None, "own"

    author_tier = model_tier_for_agent(author)
    author_cfg = await ms.resolve_model(
        tier=author_tier, preferred=author.get("model_id"),
    )
    if not author_cfg:
        return None, "own"

    author_family = model_family_key(author_cfg)
    peers: list[dict[str, Any]] = []
    for agent in agents:
        if agent.get("id") == author_id:
            continue
        if (agent.get("status") or "").lower() == "archived":
            continue
        try:
            tier = model_tier_for_agent(agent)
            cfg = await ms.resolve_model(
                tier=tier, preferred=agent.get("model_id"),
            )
        except Exception as exc:  # noqa: BLE001 — skip one teammate, keep picking
            log.debug(
                "code_audit.peer_teammate_unresolved",
                teammate_id=agent.get("id"),
                error=str(exc),
            )
            continue
        if not cfg or model_family_key(cfg) == author_family:
            continue
        peers.append({"tier": tier, "config": cfg})

    chosen = select_peer_audit_model(author_family, author_tier, peers)
    if chosen:
        log.info(
            "code_audit.peer_model",
            agent_id=author_id,
            author_model=author_cfg.get("model_id"),
            audit_model=chosen.get("model_id"),
        )
        return chosen, "peer"
    log.info(
        "code_audit.peer_model_unavailable",
        agent_id=author_id,
        author_model=author_cfg.get("model_id"),
        teammate_count=sum(
            1 for a in agents
            if a.get("id") != author_id
            and (a.get("status") or "").lower() != "archived"
        ),
    )
    return author_cfg, "own"


async def _invoke_audit_llm(
    project_id: str,
    agent_id: str,
    system: str,
    user: str,
    call_llm: ReviewLLMCallback | None,
    oneshot_llm: OneshotLLMCallback | None,
) -> tuple[str | None, dict]:
    """Run the audit completion. Returns (text, meta) or a soft-fail dict.

    When ``oneshot_llm`` is wired, pick a live-team peer model first.
    ``call_llm`` (author's own review callback) is the fallback used by
    tests and when peer resolve fails.
    """
    chosen: dict | None = None
    source = "own"
    if oneshot_llm is not None:
        try:
            chosen, source = await resolve_peer_audit_model(
                project_id, agent_id,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to own callback
            log.warning(
                "code_audit.peer_resolve_failed",
                agent_id=agent_id,
                error=str(exc),
            )
            chosen, source = None, "own"

    try:
        if oneshot_llm is not None and chosen:
            # Verdict must land in ``content`` (``VERDICT: PASS|ISSUES`` on
            # line 1). Thinking models often spend the budget on
            # reasoning_content and return empty content → false llm_failed.
            cfg = dict(chosen)
            cfg["supports_thinking"] = False
            cfg["default_reasoning_effort"] = None
            text = await asyncio.wait_for(
                oneshot_llm(cfg, system, user),
                timeout=CODE_AUDIT_LLM_TIMEOUT_S,
            )
        elif call_llm is not None:
            text = await asyncio.wait_for(
                call_llm(system, user),
                timeout=CODE_AUDIT_LLM_TIMEOUT_S,
            )
            source = "own"
            chosen = None
        elif oneshot_llm is not None:
            return None, {"audited": False, "reason": "no_model"}
        else:
            return None, {"audited": False, "reason": "no_callback"}
    except asyncio.TimeoutError:
        log.warning(
            "code_audit.llm_timeout",
            agent_id=agent_id,
            timeout_s=CODE_AUDIT_LLM_TIMEOUT_S,
        )
        return None, {"audited": False, "reason": "llm_failed"}
    except NoModelConfiguredError as exc:
        log.warning("code_audit.no_model", agent_id=agent_id, error=str(exc))
        return None, {"audited": False, "reason": "no_model"}
    except Exception as exc:  # noqa: BLE001 — soft-fail contract
        log.warning("code_audit.llm_failed", agent_id=agent_id, error=str(exc))
        return None, {"audited": False, "reason": "llm_failed"}
    if not text:
        return None, {"audited": False, "reason": "llm_failed"}
    return text, {
        "audit_model_id": (chosen or {}).get("model_id"),
        "audit_model_source": source,
    }


# ── Audit runner ─────────────────────────────────────────────

async def run_code_audit(
    project_id: str,
    agent_id: str,
    task_id: str | None = None,
    call_llm: ReviewLLMCallback | None = None,
    oneshot_llm: OneshotLLMCallback | None = None,
) -> dict:
    """Run a code audit for an agent's worktree. Never raises.

    ``oneshot_llm`` is ``Agent._oneshot_llm(model_config, system, user)`` —
    production path; picks a live teammate's different ``model_id``.
    ``call_llm`` is the parent agent's review callback
    (``async (system, user) -> str``) — tests and fallback (author's own
    model). ``None`` for both soft-fails with ``no_callback`` once an LLM
    verdict is actually needed; the auto-PASS path never touches either.

    Return contract (soft-fail dicts):
      - ``{"audited": False, "reason": "no_worktree" | "no_callback" | "no_model" | "llm_failed" | "error"}``
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
        # TEST_DSH_32 P7（空 diff 短路）：diff 为空 = 无可审计内容——
        # 无论台账如何（编辑后回滚也算无净变更），直接 auto-PASS，
        # 不再为空 diff 烧一次 LLM（此前实锤 34s 空跑）。
        if not diff.strip():
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
            # 台账同步清零（审计 P3-6）：无净变更也视为已审计口径
            reset_ledger(agent_id)
            return {
                "audited": True,
                "verdict": "PASS",
                "lines_audited": 0,
                "attestation_id": attestation_id,
                "auto_pass_reason": "empty_diff",
            }

        system, user = build_audit_prompt(diff, task_id)
        text, meta = await _invoke_audit_llm(
            project_id, agent_id, system, user, call_llm, oneshot_llm,
        )
        if text is None:
            # No extra HTTP retry here: _oneshot_llm already retries once via
            # _review_llm_post_with_retry. Record the attempt so submit can
            # say "tried, LLM failed" instead of "never audited".
            reason = str(meta.get("reason") or "")
            if reason in _SOFT_FAIL_ATTEMPT_REASONS:
                record_audit_attempt(agent_id, reason, task_id)
            return meta

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
            "audit_model_id": meta.get("audit_model_id"),
            "audit_model_source": meta.get("audit_model_source"),
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
