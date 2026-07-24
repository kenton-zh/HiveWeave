"""Tool attestations — hard evidence for submit_task / review gates (P0 Phase 3)."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

import structlog

import aiosqlite

from hiveweave.config import settings
from hiveweave.db import meta as meta_db
from hiveweave.db.project import ensure_project_db, ProjectDbError

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
# 以及 CLI 脚本验证（井字棋实测暴露的盲区）：
#   python verify_ai.py / python test_game.py / python -m unittest / bash check_xx.sh
_TEST_COMMAND_RE = re.compile(
    r"(?:"
    r"\bnpm\s+(?:run\s+)?test\b|"
    r"\bnpx\s+vitest\b|"
    r"\bvitest\b|"
    r"\bpytest\b|"
    r"\bpython3?\s+-m\s+pytest\b|"
    r"\bpython3?\s+-m\s+unittest\b|"
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
    r"\buv\s+run\s+pytest\b|"
    # python/node/bash 直接跑验证/测试脚本（test_*.py, *_test.py,
    # verify_*.py, check_*.py 及对应 .js/.mjs/.ts/.sh 变体）
    r"\b(?:python3?|uv\s+run\s+python3?|node|bash|sh)\s+"
    r"(?:[^\s;&|]*/)?(?:test_|verify_|check_)[^\s;&|]*\.(?:py|[jm]js|ts|sh)\b|"
    r"\b(?:python3?|uv\s+run\s+python3?|node|bash|sh)\s+"
    r"[^\s;&|]*_test\.(?:py|[jm]js|ts|sh)\b"
    r")",
    re.IGNORECASE,
)

DEFAULT_MAX_AGE_MS = 24 * 60 * 60 * 1000


async def _conn(project_id: str) -> aiosqlite.Connection:
    """Resolve project_id to per-project DB connection.

    失败时 raise ProjectDbError（workspace 不存在或被驱逐）。
    """
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(
            f"Workspace not found for project {project_id} (project not registered)"
        )
    return await ensure_project_db(workspace)


class AttestationService:
    """CRUD + verify for tool_attestations rows."""

    async def ensure_schema(self, project_id: str) -> None:
        if project_id in _migrated:
            return
        # project 不存在（ProjectDbError）时静默跳过 schema 创建 —
        # 调用方可能在 project 尚未完全初始化时调用
        try:
            conn = await _conn(project_id)
        except ProjectDbError:
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
        # project 不存在（ProjectDbError）时返回 None（无 attestation）
        try:
            conn = await _conn(project_id)
        except ProjectDbError:
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

    async def find_recent_for_agent(
        self,
        project_id: str,
        *,
        agent_id: str,
        task_id: str | None = None,
        kinds: list[str] | frozenset[str] | None = None,
        max_age_ms: int | None = None,
        limit: int = 8,
    ) -> list[str]:
        """Return recent valid attestation ids for auto-attach on submit_task.

        Prefers rows matching task_id; falls back to agent-scoped attestations
        with null/empty task_id. Excludes waiver kind.
        """
        await self.ensure_schema(project_id)
        try:
            conn = await _conn(project_id)
        except ProjectDbError:
            return []
        now = int(time.time() * 1000)
        max_age = max_age_ms or int(
            getattr(settings, "attestation_max_age_ms", None) or DEFAULT_MAX_AGE_MS
        )
        min_created = now - max_age
        kinds_list = list(kinds) if kinds else None
        params: list[Any] = [project_id, agent_id, WAIVER_KIND, now, min_created]
        kind_clause = ""
        if kinds_list:
            placeholders = ", ".join("?" * len(kinds_list))
            kind_clause = f" AND kind IN ({placeholders})"
            params.extend(kinds_list)
        params.append(limit)
        cur = await conn.execute(
            "SELECT id, task_id, kind, created_at FROM tool_attestations "
            "WHERE project_id = ? AND agent_id = ? AND kind != ? "
            "AND (expires_at IS NULL OR expires_at > ?) "
            f"AND created_at >= ?{kind_clause} "
            "AND stdout_hash IS NOT NULL AND TRIM(stdout_hash) != '' "
            "ORDER BY created_at DESC LIMIT ?",
            params,
        )
        rows = await cur.fetchall()
        await cur.close()
        if not rows:
            return []
        matched: list[str] = []
        fallback: list[str] = []
        for r in rows:
            rid = r["id"]
            tid = r["task_id"] or ""
            if task_id and tid == task_id:
                matched.append(rid)
            elif not tid:
                fallback.append(rid)
            elif not task_id:
                matched.append(rid)
        return (matched or fallback)[: max(1, min(limit, 4))]


def is_test_command(cmd: str) -> bool:
    """True if command looks like a test runner invocation."""
    if not cmd or not str(cmd).strip():
        return False
    return bool(_TEST_COMMAND_RE.search(str(cmd)))


def hash_stdout(s: str) -> str:
    """SHA-256 hex truncated to 16 chars."""
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


# ── Attestation waiver（coordinator 豁免通道）────────────────────
#
# 背景（井字棋实测 #2）：attestation 门禁为 UI/browse 任务设计，纯 CLI 任务
# 没有可 browse 的界面，bash 验证脚本又不一定命中 is_test_command → submit
# 被硬拒。CEO 在 charter 里"口头豁免"无效——工具层不读 charter。
# 这里提供正式的豁免通道：coordinator 显式 waive（落库、可审计、24h 过期），
# 保留硬闸门的防假装完成功能，同时给 CLI/脚本类任务一个留痕出口。

WAIVER_KIND = "waiver"
DOC_REVIEW_KIND = "doc_review"

# Tag tokens that hard-select docs_only policy (narrow — avoid loose "docs").
_DOCS_TAGS = frozenset({"docs_only", "doc_review"})
_UI_TAGS = frozenset({"ui_browser_e2e", "ui", "e2e", "browser"})
_TEST_TAGS = frozenset({"generic_tests", "tests", "test_run"})


async def create_doc_review(
    project_id: str,
    *,
    agent_id: str,
    task_id: str | None,
    files: list[dict[str, Any]],
    workspace: str,
    commit_hash: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Create a ``doc_review`` attestation after verifying files on disk.

    Each entry in ``files`` is ``{path, min_lines?}``. Paths are relative to
    ``workspace`` (usually project root / main). Returns ``(attestation_id,
    report)`` where report lists checked paths and content hashes.

    Raises ``ValueError`` if any required file is missing or too short.
    """
    if not files:
        raise ValueError("doc_review requires at least one file entry")
    from pathlib import Path

    root = Path(workspace)
    if not root.is_dir():
        raise ValueError(f"Workspace not found: {workspace}")

    checked: list[dict[str, Any]] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid file entry: {entry!r}")
        rel = (entry.get("path") or entry.get("file") or "").strip().replace(
            "\\", "/"
        )
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            raise ValueError(f"Unsafe or empty path: {rel!r}")
        root_resolved = root.resolve()
        full = (root / rel).resolve()
        try:
            full.relative_to(root_resolved)
        except ValueError as e:
            raise ValueError(f"Path escapes workspace: {rel}") from e
        if not full.is_file():
            raise ValueError(f"File not found on workspace: {rel}")
        raw = full.read_bytes()
        # Normalize newlines so CRLF/LF checkouts share the same hash (TEST13 P2-3)
        raw_norm = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        text = raw_norm.decode("utf-8", errors="replace")
        lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        min_lines = entry.get("min_lines") or entry.get("minLines")
        if min_lines is not None and lines < int(min_lines):
            raise ValueError(
                f"{rel}: {lines} lines < min_lines={min_lines}"
            )
        digest = hashlib.sha256(raw_norm).hexdigest()
        checked.append(
            {
                "path": rel,
                "sha256": digest,
                "bytes": len(raw_norm),
                "lines": lines,
            }
        )

    stdout_blob = json.dumps(
        {"kind": DOC_REVIEW_KIND, "files": checked},
        ensure_ascii=False,
        sort_keys=True,
    )
    att_id = await attestation_service.create(
        project_id,
        agent_id=agent_id,
        kind=DOC_REVIEW_KIND,
        task_id=task_id,
        command_or_url=f"doc_review:{len(checked)} files",
        workspace=workspace,
        commit_hash=commit_hash,
        stdout=stdout_blob,
        artifact_hashes={c["path"]: c["sha256"] for c in checked},
        exit_code=0,
    )
    return att_id, {"files": checked, "attestation_id": att_id}


async def create_waiver(
    project_id: str,
    *,
    task_id: str,
    waived_by: str,
    reason: str,
    ttl_ms: int | None = None,
) -> str:
    """Coordinator 豁免某任务的 attestation 门禁。返回 waiver attestation id。"""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("waiver reason is required (auditability)")
    return await attestation_service.create(
        project_id,
        agent_id=waived_by,
        kind=WAIVER_KIND,
        task_id=task_id,
        command_or_url=f"waive_attestation: {reason[:450]}",
        stdout=reason,  # 只存 hash，作为审计指纹
        ttl_ms=ttl_ms,
    )


async def has_valid_waiver(project_id: str, task_id: str | None) -> bool:
    """任务是否有未过期的 waiver。"""
    if not task_id:
        return False
    await attestation_service.ensure_schema(project_id)
    # project 不存在（ProjectDbError）时返回 False（无 waiver）
    try:
        conn = await _conn(project_id)
    except ProjectDbError:
        return False
    now = int(time.time() * 1000)
    cur = await conn.execute(
        "SELECT 1 FROM tool_attestations "
        "WHERE project_id = ? AND task_id = ? AND kind = ? "
        "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
        [project_id, task_id, WAIVER_KIND, now],
    )
    row = await cur.fetchone()
    await cur.close()
    return row is not None


def resolve_task_policy(
    title: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
) -> str:
    """Infer attestation policy from **structured tags** (not free-text title).

    Returns: ``ui_browser_e2e`` | ``docs_only`` | ``generic_tests`` |
    ``coordinator_review``.

    Language-agnostic: only tag tokens select a policy. Free-text title /
    description are ignored for gating (HARD RULE: no NL intent scrape).
    """
    del title, description  # unused — keep signature for call-site compat
    tags_l = {str(t).strip().lower() for t in (tags or []) if t}
    if tags_l & _DOCS_TAGS:
        return "docs_only"
    if tags_l & _UI_TAGS:
        return "ui_browser_e2e"
    if tags_l & _TEST_TAGS:
        return "generic_tests"
    return "coordinator_review"


POLICY_REQUIRED_KINDS: dict[str, frozenset[str] | None] = {
    # Document VERIFY/spec tasks: machine-checkable file presence + hash
    "docs_only": frozenset({DOC_REVIEW_KIND}),
    # Soft for others — coordinator judges browse/test evidence on review
    "ui_browser_e2e": None,
    "generic_tests": None,
    "coordinator_review": None,
}


def required_attestation_kinds(policy_id: str) -> frozenset[str] | None:
    """Kinds required at submit/approve for ``policy_id``, or None (soft)."""
    return POLICY_REQUIRED_KINDS.get(policy_id)


async def check_task_attestations(
    project_id: str,
    task: dict[str, Any],
    attestation_ids: list[str] | None,
    *,
    expected_agent_id: str | None = None,
) -> str | None:
    """Validate attestation_ids against the task policy when kinds are required.

    Returns an error string, or None when the gate passes / is soft.
    """
    tags = task.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    evidence = task.get("evidence") or {}
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except Exception:
            evidence = {}
    policy_id = (
        (evidence.get("policy_id") if isinstance(evidence, dict) else None)
        or task.get("policy_id")
        or resolve_task_policy(
            title=task.get("title") or "",
            tags=tags if isinstance(tags, list) else [],
            description=task.get("description") or "",
        )
    )
    needed = required_attestation_kinds(policy_id)
    if not needed:
        return None
    if await has_valid_waiver(project_id, task.get("id")):
        return None
    aids = list(attestation_ids or [])
    if isinstance(evidence, dict) and not aids:
        aids = list(evidence.get("attestation_ids") or [])
    ok, err = await attestation_service.verify_ids(
        project_id,
        [str(x) for x in aids],
        expected_agent_id=expected_agent_id,
        expected_kinds=needed,
        task_id=task.get("id"),
    )
    if ok:
        return None
    return (
        f"attestation gate failed ({policy_id}): {err}. "
        f"For docs_only: call attest_doc_review(taskId, files=[...]) then "
        f"submit/approve with those attestationIds; or coordinator "
        f"waive_attestation as last resort."
    )


attestation_service = AttestationService()
