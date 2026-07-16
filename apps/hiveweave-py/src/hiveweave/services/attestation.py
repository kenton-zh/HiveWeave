"""Tool attestations — hard evidence for submit_task / review gates (P0 Phase 3)."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

import structlog

from hiveweave.config import settings
from hiveweave.db import meta as meta_db
from hiveweave.db.project import ensure_project_db

log = structlog.get_logger(__name__)

_migrated: set[str] = set()

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS tool_attestations (
    id TEXT PRIMARY KEY,
    tool_call_id TEXT,
    task_id TEXT,
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    command_or_url TEXT,
    exit_code INTEGER,
    workspace TEXT,
    commit_hash TEXT,
    stdout_hash TEXT,
    artifact_hashes TEXT,
    console_errors INTEGER,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    project_id TEXT NOT NULL
)
"""

# npm test, pytest, vitest, yarn/pnpm test, go test, cargo test, etc.
_TEST_COMMAND_RE = re.compile(
    r"(?:"
    r"\bnpm\s+(?:run\s+)?test\b|"
    r"\bnpx\s+vitest\b|"
    r"\bvitest\b|"
    r"\bpytest\b|"
    r"\bpython\s+-m\s+pytest\b|"
    r"\byarn\s+(?:run\s+)?test\b|"
    r"\bpnpm\s+(?:run\s+)?test\b|"
    r"\bgo\s+test\b|"
    r"\bcargo\s+test\b|"
    r"\bmaven\s+test\b|"
    r"\bmvn\s+test\b|"
    r"\bgradle\s+test\b|"
    r"\bdotnet\s+test\b|"
    r"\bjest\b|"
    r"\bmocha\b|"
    r"\buv\s+run\s+pytest\b"
    r")",
    re.IGNORECASE,
)

DEFAULT_MAX_AGE_MS = 24 * 60 * 60 * 1000


async def _conn(project_id: str):
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        return None
    return await ensure_project_db(workspace)


class AttestationService:
    """CRUD + verify for tool_attestations rows."""

    async def ensure_schema(self, project_id: str) -> None:
        if project_id in _migrated:
            return
        conn = await _conn(project_id)
        if conn is None:
            return
        await conn.execute(CREATE_SQL)
        try:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_attestations_task "
                "ON tool_attestations(task_id, kind)"
            )
        except Exception:
            pass
        await conn.commit()
        _migrated.add(project_id)

    async def create(
        self,
        project_id: str,
        *,
        agent_id: str,
        kind: str,
        tool_call_id: str | None = None,
        task_id: str | None = None,
        command_or_url: str | None = None,
        exit_code: int | None = None,
        workspace: str | None = None,
        commit_hash: str | None = None,
        stdout_hash: str | None = None,
        stdout: str | None = None,
        artifact_hashes: list | dict | None = None,
        console_errors: int | None = None,
        ttl_ms: int | None = None,
        # alias kept for callers that still pass commit=
        commit: str | None = None,
    ) -> str:
        await self.ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            raise ValueError(f"No project DB for {project_id}")
        now = int(time.time() * 1000)
        max_age = ttl_ms or int(
            getattr(settings, "attestation_max_age_ms", None) or DEFAULT_MAX_AGE_MS
        )
        att_id = str(uuid.uuid4())
        if stdout_hash is None and stdout is not None:
            stdout_hash = hash_stdout(stdout)
        art = None
        if artifact_hashes is not None:
            art = (
                json.dumps(artifact_hashes)
                if not isinstance(artifact_hashes, str)
                else artifact_hashes
            )
        ch = commit_hash if commit_hash is not None else commit
        await conn.execute(
            "INSERT INTO tool_attestations "
            "(id, tool_call_id, task_id, agent_id, kind, command_or_url, "
            "exit_code, workspace, commit_hash, stdout_hash, artifact_hashes, "
            "console_errors, created_at, expires_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                att_id,
                tool_call_id or str(uuid.uuid4()),
                task_id,
                agent_id,
                kind,
                command_or_url,
                exit_code,
                workspace,
                ch,
                stdout_hash,
                art,
                console_errors,
                now,
                now + max_age,
                project_id,
            ],
        )
        await conn.commit()
        log.info(
            "attestation_created",
            id=att_id,
            kind=kind,
            agent_id=agent_id,
            task_id=task_id,
        )
        return att_id

    async def get(self, project_id: str, attestation_id: str) -> dict | None:
        await self.ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return None
        cur = await conn.execute(
            "SELECT * FROM tool_attestations WHERE id = ? AND project_id = ?",
            [attestation_id, project_id],
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def verify_ids(
        self,
        project_id: str,
        attestation_ids: list[str],
        *,
        expected_agent_id: str | None = None,
        expected_kinds: list[str] | frozenset[str] | None = None,
        task_id: str | None = None,
        max_age_ms: int | None = None,
    ) -> tuple[bool, str]:
        """Verify attestations exist, not expired, and match constraints.

        Returns (ok, error_str). error_str empty on success.
        """
        if not attestation_ids:
            return False, "No attestation_ids provided"
        await self.ensure_schema(project_id)
        now = int(time.time() * 1000)
        max_age = max_age_ms or int(
            getattr(settings, "attestation_max_age_ms", None) or DEFAULT_MAX_AGE_MS
        )
        kinds_ok = set(expected_kinds) if expected_kinds else None
        seen_kinds: set[str] = set()

        for aid in attestation_ids:
            if not aid:
                continue
            row = await self.get(project_id, aid)
            if not row:
                return False, f"Attestation not found: {aid}"
            exp = row.get("expires_at")
            created = row.get("created_at") or 0
            if exp is not None and int(exp) <= now:
                return False, f"Attestation expired: {aid}"
            if created and (now - int(created)) > max_age:
                return False, f"Attestation too old: {aid}"
            if expected_agent_id and row.get("agent_id") != expected_agent_id:
                return (
                    False,
                    f"Attestation agent mismatch: {aid} "
                    f"(expected {expected_agent_id[:8]})",
                )
            kind = row.get("kind") or ""
            if kinds_ok is not None and kind not in kinds_ok:
                return (
                    False,
                    f"Attestation kind '{kind}' not in expected {sorted(kinds_ok)}",
                )
            if task_id and row.get("task_id") and row.get("task_id") != task_id:
                return False, f"Attestation task_id mismatch: {aid}"
            if not row.get("stdout_hash"):
                return False, f"Attestation missing stdout_hash: {aid}"
            seen_kinds.add(kind)

        if kinds_ok is not None and not (seen_kinds & kinds_ok):
            return (
                False,
                f"No attestation of required kind(s) {sorted(kinds_ok)}; "
                f"got {sorted(seen_kinds) or 'none'}",
            )
        return True, ""


def is_test_command(cmd: str) -> bool:
    """True if command looks like a test runner invocation."""
    if not cmd or not str(cmd).strip():
        return False
    return bool(_TEST_COMMAND_RE.search(str(cmd)))


def hash_stdout(s: str) -> str:
    """SHA-256 hex truncated to 16 chars."""
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def resolve_task_policy(
    title: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
) -> str:
    """Infer attestation policy_id from task metadata.

    Returns: ``ui_browser_e2e`` | ``docs_only`` | ``generic_tests``.
    """
    import re

    tag_list = [str(t).lower() for t in (tags or [])] if isinstance(tags, list) else []
    tag_str = " ".join(tag_list)
    blob = f"{title or ''} {description or ''} {tag_str}".lower()

    def _hit(markers: tuple[str, ...]) -> bool:
        for m in markers:
            # Short tokens (ui/ux) must be whole words — avoid "suite" → ui
            if len(m) <= 2:
                if re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", blob):
                    return True
            elif m in blob:
                return True
        return False

    ui_markers = ("ui", "frontend", "页面", "browse", "e2e", "浏览器", "ux")
    docs_markers = ("docs", "explore", "文档", "调研", "readme", "spec only", "探索")

    # Post-merge VERIFY: default generic_tests unless explicitly UI-scoped
    if "verify" in tag_list:
        if _hit(("frontend", "页面", "browse", "e2e", "浏览器")) or (
            "ui" in tag_list or "frontend" in tag_list
        ):
            return "ui_browser_e2e"
        if re.search(r"(?<![a-z0-9])ui(?![a-z0-9])", f"{title or ''} {tag_str}".lower()):
            return "ui_browser_e2e"
        return "generic_tests"

    if _hit(ui_markers):
        return "ui_browser_e2e"
    # docs/explore only — no code attestation required
    if _hit(docs_markers):
        code_markers = ("implement", "fix", "bug", "代码", "实现", "api", "refactor")
        if not _hit(code_markers):
            return "docs_only"
    return "generic_tests"


POLICY_REQUIRED_KINDS: dict[str, frozenset[str] | None] = {
    "ui_browser_e2e": frozenset({"browse_e2e"}),
    "generic_tests": frozenset({"test_run"}),
    "docs_only": None,
}


def required_attestation_kinds(policy_id: str) -> frozenset[str] | None:
    """Kinds required for a policy, or None if attestations not required."""
    return POLICY_REQUIRED_KINDS.get(policy_id, frozenset({"test_run"}))


async def check_task_attestations(
    project_id: str,
    task: dict[str, Any],
    attestation_ids: list[str] | None,
    *,
    expected_agent_id: str | None = None,
) -> str | None:
    """Return deny reason or None if attestation gate passes.

    Rejects ui_browser_e2e / generic_tests when only testsPassed=true
    without valid attestations.
    """
    policy = (
        task.get("policy_id")
        or resolve_task_policy(
            task.get("title"),
            task.get("tags") if isinstance(task.get("tags"), list) else None,
            task.get("description"),
        )
    )
    required = POLICY_REQUIRED_KINDS.get(policy)
    if required is None:
        return None  # docs_only — no attestations required

    ids = [i for i in (attestation_ids or []) if i]
    evidence = task.get("evidence") or {}
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except Exception:
            evidence = {}
    if not ids and isinstance(evidence, dict):
        raw = evidence.get("attestation_ids") or evidence.get("attestationIds") or []
        if isinstance(raw, list):
            ids = [str(x) for x in raw if x]

    if not ids:
        return (
            f"REJECT: policy '{policy}' requires attestation(s) of kind "
            f"{sorted(required)}. Run real tests/browse first; "
            f"testsPassed=true alone is insufficient. Pass attestationIds."
        )

    svc = attestation_service
    ok, err = await svc.verify_ids(
        project_id,
        ids,
        expected_agent_id=expected_agent_id,
        expected_kinds=required,
        task_id=task.get("id"),
    )
    if not ok:
        return f"REJECT: attestation verify failed — {err}"
    return None


attestation_service = AttestationService()
