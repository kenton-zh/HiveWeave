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
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

from hiveweave.services.model import NoModelConfiguredError

log = structlog.get_logger(__name__)

# s3-clone_06 P0-1：45s 帽是「审计为软门」时代的取舍——那时超时只意味着
# 「没审计也放行」，宁可快失败。P0-3（fail-loud）之后语义反转：超时 =
# 提交被门禁拦下或走显式 waive 流程，代价比多等 60 秒高得多。实测本项目
# 成功调用耗时 27–41s、失败全部精确停在 452xx ms（撞 45s 顶），说明审计
# 本就贴着上限跑，上游一抖即全灭（34 次 27 次 llm_failed = 79%）。
#
# **实际可达上限（审计 [2]，勿再自欺）**：本值只是外层 ``asyncio.wait_for``
# 的帽，真正决定成败的是 agents.agent._review_llm_post_with_retry：
#   首读固定 ``_REVIEW_LLM_READ_TIMEOUT_MAX_S``（90s）→ 重试窗
#   ``_REVIEW_LLM_RETRY_WINDOW_S``（45s）→ 首读超时后 remaining = 45-90 < 0
#   直接上抛，第二次尝试根本不会发生。
# 因此 > 90s 的配置**不可达**（调 env 到 300 也只跑 90s）。这里夹到
# agent 侧的真实帽子（取不到则按 90 兜底），并在提示文案里用有效值，
# 杜绝"改了配置没变化"的假象。想真正放宽须改 agent 侧首读帽/重试窗。
def _timeout_from_env(name: str, default: int) -> int:
    """环境变量读取超时秒数——非法值回退默认，绝不在**模块导入期**抛异常。

    ``int(os.environ.get(...))`` 遇到 "60s"/"abc"/"" 会 ValueError，而这是
    模块级常量：一炸就是整个 code_audit 服务不可用（连带 submit 门禁）。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("code_audit.bad_timeout_env", key=name, value=raw, default=default)
        return default
    return value if value > 0 else default


CODE_AUDIT_LLM_TIMEOUT_S = _timeout_from_env("HIVEWEAVE_CODE_AUDIT_TIMEOUT_S", 120)


def effective_audit_timeout_s() -> float:
    """审计实际能等到的秒数 = 请求值 ∩ agent 侧首读帽。

    agent 侧重试助手的首读帽是硬顶（见上注）：外层 wait_for 再大也没用。
    这里**运行时**读取该帽子（不在模块导入期 import agents，避免
    services ↔ agents 循环导入），取不到时按 90 兜底。
    """
    try:
        from hiveweave.agents.agent import (  # noqa: PLC0415 — 见上：避免循环导入
            _REVIEW_LLM_READ_TIMEOUT_MAX_S as cap,
        )

        return float(min(CODE_AUDIT_LLM_TIMEOUT_S, cap))
    except Exception:  # noqa: BLE001 — best-effort，取不到用兜底值
        return float(min(CODE_AUDIT_LLM_TIMEOUT_S, 90))

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
# P0-3（fail-loud）后 llm_failed 不再"静默放行"：code_audit 仍是必需证据，
# submit 会被门禁拦下，出路只有两条——重试到审计成功，或由 coordinator 显式
# waive。旧文案"soft gate — does not block"是 fail-loud 之前的残留，会误导
# Agent 以为可以直接提交（s3-clone_06 实测）。
CODE_AUDIT_REMINDER_LLM_FAILED = (
    "[CODE AUDIT REMINDER] Code audit was attempted but the LLM call failed "
    "(llm_failed). Retry request_code_audit(taskId=...) — {timeout_s}s "
    "timeout, the audit takes 30-90s on real diffs. If it keeps failing, the "
    "submit gate stays CLOSED: ask a coordinator to run "
    "waive_attestation(taskId=..., reason=...) with the fallback evidence "
    "you do have (test_run results). Silently submitting will be rejected."
)
CODE_AUDIT_REMINDER_ATTEMPTED = (
    "[CODE AUDIT REMINDER] Code audit was attempted but did not produce an "
    "attestation ({reason}). Retry request_code_audit(taskId=...) once; if it "
    "still fails, submit stays BLOCKED until a coordinator runs "
    "waive_attestation(taskId=..., reason=...)."
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

    **只表示"有过失败尝试记录"**，不代表放行 —— P0-3（fail-loud）之后本谓词
    服务于 ``code_audit_soft_fail_pending`` 的**拦截**判定：命中即说明该
    agent 在本任务上审计没跑成，submit 会被门禁拦下（出路：重试到成功，或
    coordinator 显式 waive）。旧 docstring「do not block submit」是 fail-loud
    之前的语义残留，勿再据此推断（审计 [1]）。
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
    """Approve/HTTP-time compat: drop CODE_AUDIT_KIND ONLY on a stamped soft-fail.

    P0-3 (TEST_DSH_38) fail-loud: an in-memory llm_failed attempt no longer
    drops the kind anywhere — submit rejects and demands a retry or an
    explicit coordinator waive. This drop survives ONLY for the approve/HTTP
    re-check of a task whose evidence carries the stamp (i.e. submitted under
    legacy rules, or submitted under a valid waiver), so an approve is never
    re-blocked on history the submit gate already decided. The ``agent_id`` /
    ``task_id`` params are kept for signature compat but no longer trigger a
    drop by themselves.
    """
    if not needed or CODE_AUDIT_KIND not in needed:
        return needed, False
    if evidence_has_code_audit_soft_fail(evidence):
        leftover = frozenset(k for k in needed if k != CODE_AUDIT_KIND)
        return leftover, True
    return needed, False


def code_audit_soft_fail_pending(
    needed: frozenset[str] | None,
    agent_id: str | None,
    task_id: str | None,
) -> bool:
    """Submit-time detection: code_audit required and only soft-fail coverage.

    True means the submit gate must NOT silently pass — the agent must retry
    request_code_audit until an attestation exists, or obtain an explicit
    coordinator waive_attestation (logged, 24h expiry).
    """
    if not needed or CODE_AUDIT_KIND not in needed:
        return False
    if not agent_id:
        return False
    return code_audit_soft_fail_covers(agent_id, task_id)


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
        # 报**有效**帽（≤ agent 侧首读帽），不报配置值——否则 Agent 会以为
        # 还有余量而盲目重试。
        _eff = effective_audit_timeout_s()
        _shown = int(_eff) if float(_eff).is_integer() else _eff
        return CODE_AUDIT_REMINDER_LLM_FAILED.format(timeout_s=_shown)
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

    # 用**有效**帽（请求值 ∩ agent 侧首读帽），参见 effective_audit_timeout_s
    _timeout_s = effective_audit_timeout_s()
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
                timeout=_timeout_s,
            )
        elif call_llm is not None:
            text = await asyncio.wait_for(
                call_llm(system, user),
                timeout=_timeout_s,
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
            timeout_s=_timeout_s,
        )
        return None, {"audited": False, "reason": "llm_failed"}
    except NoModelConfiguredError as exc:
        log.warning("code_audit.no_model", agent_id=agent_id, error=str(exc))
        return None, {"audited": False, "reason": "no_model"}
    except Exception as exc:  # noqa: BLE001 — soft-fail contract
        log.warning("code_audit.llm_failed", agent_id=agent_id, error=str(exc))
        return None, {"audited": False, "reason": "llm_failed"}
    if not text:
        # Empty text is as fatal as a raised exception, but was previously
        # swallowed with no log at all — the 6/6 llm_failed in TEST_DSH_35
        # came through this branch with an empty `error` field.
        # Reason stays "llm_failed": _SOFT_FAIL_ATTEMPT_REASONS (:64) is a
        # whitelist and drives the non-blocking wording at :136/:203/:210.
        log.warning(
            "code_audit.empty_text",
            agent_id=agent_id,
            project_id=project_id,
            provider=(chosen or {}).get("provider"),
            model_id=(chosen or {}).get("model_id"),
            source=source,
        )
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
        # TEST_DSH_39 P0-3（审计翻转）：空 diff ≠ 安全。同任务上一轮审计若为
        # ISSUES（exit_code=1），回滚后的 auto-PASS 会把结论 4min 内翻转成
        # PASS，且新 PASS 凭证顶掉旧 ISSUES 凭证——门禁自己跟自己打架。
        # 改为：上一轮 ISSUES 在案时发显式「回滚标注」（不发 PASS 凭证，旧
        # ISSUES 凭证继续生效），要求人工复核/重新指派/取消任务；
        # 无 ISSUES 前科时保留原 auto-PASS（P7 优化不回退）。
        if not diff.strip():
            from hiveweave.services.attestation import (
                find_latest_attestation_by_kind,
                parse_audit_verdict,
            )

            prior = None
            try:
                prior = await find_latest_attestation_by_kind(
                    project_id, agent_id=agent_id, kind=CODE_AUDIT_KIND
                )
            except Exception:  # noqa: BLE001 — 查询失败按无前科处理（P7 优化不回退）
                prior = None
            prior_issues = bool(
                prior
                and parse_audit_verdict(prior) == "ISSUES"
                and _task_ref_match(task_id, prior.get("task_id"))
            )
            if prior_issues:
                reset_ledger(agent_id)
                log.warning(
                    "code_audit.empty_diff_after_issues",
                    agent_id=agent_id,
                    task_id=task_id,
                )
                return {
                    "audited": True,
                    "verdict": "ROLLED_BACK",
                    "lines_audited": 0,
                    "auto_pass_reason": "empty_diff_after_issues",
                    "message": (
                        "[code audit] diff 为空（变更已回滚），但同任务上一轮"
                        "审计结论为 ISSUES——不发新的 PASS 凭证。可执行出口："
                        "① reassign_task 改派（现已支持花名）给其他成员重做；"
                        "② message_user 请求用户裁决；③ 与上级确认后取消任务。"
                        "不要原样重复提交。"
                    ),
                }
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

        # 40 轮报告第 4 步（diff 级缓存）：同 agent + 同 diff 哈希 = 同被审
        # 内容，审计结论可复用（霞光对 683bf23f 审 6 次纯属浪费）。key 用
        # diff 内容哈希而非 HEAD commit——HEAD 相同但工作区改动不同的两次
        # 审计不可互代（审计子代理[阻断]项：干净 worktree 的 PASS 凭证不得
        # 给新未审代码放行）。复用时仍发新凭证（门禁时效性不变），不烧 LLM。
        diff_hash = hash_stdout(diff)
        cached_hit = None
        if diff_hash:
            try:
                cached_hit = await attestation_service.audit_cache_lookup(
                    project_id, agent_id=agent_id, diff_hash=diff_hash
                )
            except Exception:  # noqa: BLE001 — 缓存查询失败退化为正常审计
                cached_hit = None
        if cached_hit is not None:
            cached_exit = int(cached_hit.get("exit_code") or 1)
            verdict_cached = "PASS" if cached_exit == 0 else "ISSUES"
            attestation_id = await attestation_service.create(
                project_id,
                agent_id=agent_id,
                kind=CODE_AUDIT_KIND,
                task_id=task_id,
                exit_code=cached_exit,
                workspace=worktree,
                commit_hash=commit_hash,
                stdout_hash=hash_stdout(f"cached audit reuse {diff_hash}"),
                command_or_url=f"[verdict={verdict_cached}] cached-reuse",
            )
            reset_ledger(agent_id)
            log.info(
                "code_audit.cached_reuse",
                agent_id=agent_id,
                diff_hash=diff_hash[:12],
                verdict=verdict_cached,
            )
            return {
                "audited": True,
                "verdict": verdict_cached,
                "lines_audited": 0,
                "attestation_id": attestation_id,
                "cached_diff_hash": diff_hash[:12],
                "message": (
                    "[code audit] 同一 diff 内容已有审计结论 "
                    f"{verdict_cached}，本次复用未重烧 LLM。若代码有新改动，"
                    "diff 随之变化，将触发全新审计。"
                ),
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

        # 40 轮报告第 4 步：审计结论入缓存（同 agent + 同 diff 哈希复用）
        try:
            await attestation_service.audit_cache_store(
                project_id,
                agent_id=agent_id,
                diff_hash=diff_hash,
                verdict=verdict,
                exit_code=exit_code,
                attestation_id=str(attestation_id),
            )
        except Exception:  # noqa: BLE001 — 缓存写入失败不影响审计主流程
            pass

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
