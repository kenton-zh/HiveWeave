"""Slice contract (contract_json) — schema, ready gate, L0 machine pre-run.

A task with ``contract_json`` is a slice. Platform owns slice_status transitions
up to ``submitted``; auditor-owned verified/failed land in P1.

P0 machine clause types: ``file_exists``, ``content_contains``,
``min_lines`` (via deliverables), ``manual_review`` (deferred — not blocking).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

SLICE_STATUSES = frozenset({
    "draft",
    "ready",
    "in_progress",
    "submitted",
    "auditing",
    "verified",
    "failed",
    "escalated",
})

# Task ledger statuses that count as "upstream verified" for ready gate
# (until dedicated slice_status=verified is written by auditors in P1).
_UPSTREAM_DONE_TASK_STATUSES = frozenset({
    "approved",
    "verifying",
    "closed",
})

_MACHINE_TYPES = frozenset({
    "file_exists",
    "content_contains",
    "min_lines",
    "hash_match",  # reserved; not fully implemented in P0
    "test_command",  # reserved P2
    "json_schema",  # reserved P2
    "manual_review",
})


@dataclass
class ClauseResult:
    id: str
    type: str
    passed: bool
    deferred: bool = False
    message: str = ""
    evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "passed": self.passed,
            "deferred": self.deferred,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass
class PreRunResult:
    passed: bool
    results: list[ClauseResult] = field(default_factory=list)

    def blocking_failures(self) -> list[ClauseResult]:
        return [r for r in self.results if not r.passed and not r.deferred]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "results": [r.to_dict() for r in self.results],
        }


def parse_contract(raw: Any) -> dict[str, Any] | None:
    """Normalize contract_json from DB / tool input. None if absent/empty."""
    if raw is None or raw == "" or raw == {}:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    # Allow either nested under "slice" or flat
    if "slice" in raw and isinstance(raw["slice"], dict):
        c = dict(raw["slice"])
    else:
        c = dict(raw)
    return c if c else None


def validate_contract(contract: dict[str, Any]) -> str | None:
    """Return error message if contract is invalid, else None."""
    if not contract.get("id") and not contract.get("slice_id"):
        return "contract_json requires slice id (id or slice_id)"
    acceptance = contract.get("acceptance") or []
    if not isinstance(acceptance, list):
        return "contract_json.acceptance must be a list"
    for i, clause in enumerate(acceptance):
        if not isinstance(clause, dict):
            return f"acceptance[{i}] must be an object"
        ctype = (clause.get("type") or "").strip()
        if not ctype:
            return f"acceptance[{i}] missing type"
        if ctype not in _MACHINE_TYPES:
            return (
                f"acceptance[{i}] unknown type '{ctype}'. "
                f"Allowed: {sorted(_MACHINE_TYPES)}"
            )
        if ctype == "manual_review" and not (
            clause.get("note") or clause.get("id")
        ):
            return (
                f"acceptance[{i}] manual_review requires note (review focus)"
            )
    return None


def slice_id_of(contract: dict[str, Any]) -> str:
    return str(contract.get("id") or contract.get("slice_id") or "").strip()


def ensure_slice_status(
    contract: dict[str, Any], status: str
) -> dict[str, Any]:
    out = dict(contract)
    if status not in SLICE_STATUSES:
        raise ValueError(f"Invalid slice_status: {status}")
    out["slice_status"] = status
    if "id" not in out and out.get("slice_id"):
        out["id"] = out["slice_id"]
    return out


def is_slice_verified(contract: dict[str, Any] | None, task_status: str) -> bool:
    """Whether this slice counts as verified for downstream ready gates."""
    if contract:
        st = (contract.get("slice_status") or "").lower()
        if st == "verified":
            return True
        if st in ("failed", "escalated", "draft"):
            return False
    return (task_status or "").lower() in _UPSTREAM_DONE_TASK_STATUSES


def collect_upstream_refs(contract: dict[str, Any]) -> list[dict[str, str]]:
    """Extract upstream refs from inputs[] and (optionally) depends hint."""
    refs: list[dict[str, str]] = []
    for inp in contract.get("inputs") or []:
        if not isinstance(inp, dict):
            continue
        sid = str(inp.get("slice") or inp.get("slice_id") or "").strip()
        tid = str(inp.get("task_id") or inp.get("taskId") or "").strip()
        if sid or tid:
            refs.append({"slice_id": sid, "task_id": tid})
    return refs


async def check_ready_gate(
    project_id: str,
    task: dict[str, Any],
    *,
    lookup_by_slice_id=None,
    lookup_by_task_id=None,
) -> str | None:
    """Return error if task cannot enter in_progress; None if ready.

    ``lookup_by_slice_id(slice_id) -> task|None``
    ``lookup_by_task_id(task_id) -> task|None``
    """
    contract = parse_contract(task.get("contract_json"))
    if not contract:
        return None  # non-slice tasks: no ready gate

    status = (contract.get("slice_status") or "draft").lower()
    if status in ("ready", "in_progress", "submitted", "auditing", "verified"):
        # Already past draft — still re-check upstream for safety when starting
        pass
    if status in ("failed", "escalated"):
        return (
            f"Slice {slice_id_of(contract)} is '{status}' — "
            "cannot start until rework / escalation resolves."
        )

    missing: list[str] = []
    for ref in collect_upstream_refs(contract):
        upstream = None
        if ref["task_id"] and lookup_by_task_id:
            upstream = await lookup_by_task_id(ref["task_id"])
        if upstream is None and ref["slice_id"] and lookup_by_slice_id:
            upstream = await lookup_by_slice_id(ref["slice_id"])
        if upstream is None:
            missing.append(
                f"upstream not found (slice={ref['slice_id'] or '?'} "
                f"task={ref['task_id'] or '?'})"
            )
            continue
        u_contract = parse_contract(upstream.get("contract_json"))
        if not is_slice_verified(u_contract, str(upstream.get("status") or "")):
            label = (
                slice_id_of(u_contract)
                if u_contract
                else (upstream.get("id") or "")[:12]
            )
            missing.append(
                f"upstream '{label}' not verified "
                f"(slice_status={(u_contract or {}).get('slice_status')} "
                f"task_status={upstream.get('status')})"
            )

    # Also honor tasks.depends_on as upstream task ids
    depends = task.get("depends_on") or []
    if isinstance(depends, str):
        try:
            depends = json.loads(depends)
        except Exception:
            depends = []
    for dep_id in depends:
        if not dep_id or not lookup_by_task_id:
            continue
        upstream = await lookup_by_task_id(str(dep_id))
        if upstream is None:
            missing.append(f"depends_on task {str(dep_id)[:12]} not found")
            continue
        u_contract = parse_contract(upstream.get("contract_json"))
        if not is_slice_verified(u_contract, str(upstream.get("status") or "")):
            missing.append(
                f"depends_on {str(dep_id)[:12]} not verified "
                f"(status={upstream.get('status')})"
            )

    if missing:
        return (
            "READY GATE: upstream slices not verified — cannot start. "
            + "; ".join(missing)
        )
    return None


def compute_initial_slice_status(
    contract: dict[str, Any],
    *,
    upstream_all_verified: bool,
) -> str:
    if upstream_all_verified and not collect_upstream_refs(contract):
        return "ready"
    if upstream_all_verified:
        return "ready"
    if collect_upstream_refs(contract):
        return "draft"
    return "ready"


def run_machine_acceptance(
    contract: dict[str, Any],
    *,
    workspace_root: str | Path,
) -> PreRunResult:
    """L0 machine pre-run against workspace_root (worktree or project root)."""
    root = Path(workspace_root)
    results: list[ClauseResult] = []

    # Deliverables → implied file_exists + must_contain + min_lines
    for i, d in enumerate(contract.get("deliverables") or []):
        if not isinstance(d, dict):
            continue
        path = str(d.get("path") or "").strip()
        if not path:
            continue
        cid = f"DELIVERABLE-{i + 1}"
        results.append(_check_file_exists(cid, path, root))
        patterns = d.get("must_contain") or []
        if patterns and results[-1].passed:
            results.append(
                _check_content_contains(
                    f"{cid}-content", path, list(patterns), root
                )
            )
        min_lines = d.get("min_lines")
        if isinstance(min_lines, int) and min_lines > 0:
            results.append(_check_min_lines(f"{cid}-lines", path, min_lines, root))

    for clause in contract.get("acceptance") or []:
        if not isinstance(clause, dict):
            continue
        cid = str(clause.get("id") or "AC?")
        ctype = (clause.get("type") or "").strip()
        if ctype == "manual_review":
            results.append(
                ClauseResult(
                    id=cid,
                    type=ctype,
                    passed=True,
                    deferred=True,
                    message="Deferred to auditor (manual_review).",
                    evidence=str(clause.get("note") or "")[:200] or None,
                )
            )
            continue
        if ctype == "file_exists":
            results.append(
                _check_file_exists(cid, str(clause.get("path") or ""), root)
            )
            continue
        if ctype == "content_contains":
            patterns = clause.get("patterns") or clause.get("must_contain") or []
            results.append(
                _check_content_contains(
                    cid,
                    str(clause.get("path") or ""),
                    list(patterns),
                    root,
                )
            )
            continue
        if ctype == "min_lines":
            results.append(
                _check_min_lines(
                    cid,
                    str(clause.get("path") or ""),
                    int(clause.get("min_lines") or clause.get("min") or 0),
                    root,
                )
            )
            continue
        if ctype in ("hash_match", "test_command", "json_schema"):
            results.append(
                ClauseResult(
                    id=cid,
                    type=ctype,
                    passed=True,
                    deferred=True,
                    message=f"{ctype} not executed in P0 (deferred).",
                )
            )
            continue
        results.append(
            ClauseResult(
                id=cid,
                type=ctype,
                passed=False,
                message=f"Unknown clause type: {ctype}",
            )
        )

    blocking = [r for r in results if not r.passed and not r.deferred]
    return PreRunResult(passed=len(blocking) == 0, results=results)


def _resolve(root: Path, rel: str) -> Path:
    rel = rel.replace("\\", "/").lstrip("/")
    return (root / rel).resolve()


def _check_file_exists(cid: str, path: str, root: Path) -> ClauseResult:
    if not path:
        return ClauseResult(cid, "file_exists", False, message="missing path")
    p = _resolve(root, path)
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return ClauseResult(
            cid, "file_exists", False, message=f"path escapes workspace: {path}"
        )
    ok = p.is_file()
    return ClauseResult(
        id=cid,
        type="file_exists",
        passed=ok,
        message="ok" if ok else f"missing: {path}",
        evidence=str(p) if ok else None,
    )


def _check_content_contains(
    cid: str, path: str, patterns: list, root: Path
) -> ClauseResult:
    if not path:
        return ClauseResult(cid, "content_contains", False, message="missing path")
    p = _resolve(root, path)
    if not p.is_file():
        return ClauseResult(
            cid, "content_contains", False, message=f"missing file: {path}"
        )
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ClauseResult(
            cid, "content_contains", False, message=f"read failed: {e}"
        )
    missing = [str(pat) for pat in patterns if str(pat) not in text]
    if missing:
        return ClauseResult(
            cid,
            "content_contains",
            False,
            message=f"missing patterns: {missing}",
            evidence=path,
        )
    return ClauseResult(
        cid, "content_contains", True, message="ok", evidence=path
    )


def _check_min_lines(
    cid: str, path: str, min_lines: int, root: Path
) -> ClauseResult:
    if not path:
        return ClauseResult(cid, "min_lines", False, message="missing path")
    p = _resolve(root, path)
    if not p.is_file():
        return ClauseResult(
            cid, "min_lines", False, message=f"missing file: {path}"
        )
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return ClauseResult(cid, "min_lines", False, message=f"read failed: {e}")
    n = len(lines)
    ok = n >= min_lines
    return ClauseResult(
        id=cid,
        type="min_lines",
        passed=ok,
        message="ok" if ok else f"{n} lines < min_lines={min_lines}",
        evidence=f"{path}:{n}",
    )


def format_prerun_failure(prerun: PreRunResult) -> str:
    parts = ["SUBMIT PRE-RUN FAILED (machine clauses):"]
    for r in prerun.blocking_failures():
        parts.append(f"- [{r.id}] {r.type}: {r.message}")
    deferred = [r for r in prerun.results if r.deferred]
    if deferred:
        parts.append(
            f"({len(deferred)} manual/deferred clause(s) left for auditor)"
        )
    return "\n".join(parts)
