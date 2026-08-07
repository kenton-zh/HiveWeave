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


async def _diff_touches_scope(
    main_ws: str, ch: str, main_tip: str, scope_files: set[str]
) -> bool | None:
    """Return True if ``ch..main_tip`` touches any path in *scope_files*.

    Issue #5: baseline staleness should be judged by whether the newer
    commits actually touch the verification scope, not by raw tip distance.
    Returns None when the scope is empty or undecidable (caller keeps the
    stale reject — fail-closed), False when provably untouched, True when
    touched.

    A scope entry that lives in an area git cannot reflect in a diff —
    gitignored/untracked paths (e.g. ``.hiveweave/``, ``node_modules/``,
    worktree-scoped evidence files whose prefix was normalized away) — can
    never match a ``git diff --name-only`` output. Treating such an entry as
    "untouched" would silently approve on a scope git cannot verify. We
    decide per-entry: an entry is *verifiable* iff it correspond to a tracked
    path (probed with ``git ls-files``, which is case-insensitive-safe); any
    unverifiable entry left in play makes the whole result undecidable (None)
    unless a verifiable entry provably touches the diff.
    """
    if not scope_files or not main_tip or not ch:
        return None
    try:
        from hiveweave.services.git_worktree import _git
        from hiveweave.services.worktree_review import normalize_evidence_path

        if not scope_files:
            return None

        # A scope entry is verifiable only if it matches a tracked path.
        # ``git ls-files`` is case-insensitive-safe (a case-mismatched
        # ``.Hiveweave/...`` simply matches no tracked path → unverifiable),
        # and naturally covers every gitignored/untracked prefix, so this is
        # more robust than a hardcoded ``.hiveweave/`` prefix check.
        ok, tracked_out = await _git(["ls-files"], main_ws)
        if not ok:
            return None  # can't tell what's verifiable → fail-closed
        tracked = {
            normalize_evidence_path(p).casefold().rstrip("/")
            for p in tracked_out.splitlines()
            if p.strip()
        }
        verifiable: set[str] = set()
        unverifiable: set[str] = set()
        for s in scope_files:
            s_norm = s.casefold().rstrip("/")
            if not s_norm:
                continue
            if any(
                t == s_norm or t.startswith(s_norm + "/")
                for t in tracked
            ):
                verifiable.add(s_norm)
            else:
                unverifiable.add(s_norm)

        ok, out = await _git(
            ["diff", "--name-only", ch, main_tip],
            main_ws,
        )
        if not ok:
            return None
        changed = [normalize_evidence_path(p) for p in out.splitlines() if p.strip()]
        # Match (casefold already applied) treating a scope entry as touching
        # when it is a file OR a directory prefix of a changed path. Exact set
        # intersection would miss directory-level scope entries (e.g.
        # scope="frontend/" vs changed="frontend/Button.tsx") and wrongly
        # report "untouched", silently approving a real scope hit.
        changed_folded = {p.casefold().rstrip("/") for p in changed}
        for s_norm in verifiable:
            if any(
                c == s_norm or c.startswith(s_norm + "/")
                for c in changed_folded
            ):
                return True
        # No verifiable entry touched. If any unverifiable entry remains in
        # play we cannot prove the scope was untouched → fail-closed (checked
        # before the empty-diff shortcut, so an unverifiable scope stays
        # undecidable even when the tracked diff is empty).
        if unverifiable:
            return None
        if not changed:
            return False  # no changed files — nothing to re-run
        return False
    except Exception:
        return None

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

# ── Task-id normalization (root cause: short-id prefixes stored verbatim) ──
# Agents routinely pass the 8-char prefix shown in get_tasks instead of the
# full UUID. Attestations wrote that value straight into tool_attestations.task_id,
# so every later exact-match against the full UUID failed (submit gate 49%
# failure, review evidence gate, MATCH-but-mismatch hints). All write + compare
# paths now normalize to the canonical full UUID.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)

# A plausible short-id / dotted UUID reference: 8-32 hex chars (short prefix
# or full UUID with dashes stripped). Anything else is NOT a task id — skip
# the DB round-trip entirely (avoids hangs on non-task refs like "task-1").
_HEX_REF_RE = re.compile(r"^[0-9a-fA-F]{8,32}$")


def _norm_task_ref(s: str | None) -> str:
    """Lowercase + dash-strip a task id string (dash-insensitive equality)."""
    return (str(s or "").strip().lower()).replace("-", "")


def _is_full_uuid(s: str) -> bool:
    return bool(_UUID_RE.fullmatch(str(s or "").strip()))


async def canonical_task_id(project_id: str, task_id: str | None) -> str | None:
    """Resolve a task id (full UUID or 8-char short prefix) to the canonical
    full UUID (dash-stripped, lowercase).

    Fail-open: only canonicalize when the value is a real resolvable UUID
    (full or short-prefix). When it cannot be resolved to a UUID (task gone,
    ambiguous, or a non-UUID synthetic id), return the raw value unchanged —
    never dash-strip it (that would corrupt ids like "t-invalidate-3" into
    "tinvalidate3"). Never raises — attestation logic must not break on a
    weird reference.
    """
    if not task_id:
        return None
    raw = str(task_id).strip()
    if not raw:
        return None
    if _is_full_uuid(raw):
        return _norm_task_ref(raw)
    # Not a UUID-shaped reference (short prefix or full UUID) — return the raw
    # value unchanged WITHOUT a DB round-trip. Guard prevents hangs when a
    # non-task ref (e.g. "task-1") flows through a canonicalization call.
    if not _HEX_REF_RE.fullmatch(raw):
        return raw
    try:
        from hiveweave.services.task import TaskService

        resolved = await TaskService().require_task_id(project_id, raw)
        if resolved:
            return _norm_task_ref(resolved)
    except Exception:
        pass
    return raw


async def _task_ids_equal(project_id: str, a: str | None, b: str | None) -> bool:
    """Equality that treats short-id prefixes and full UUIDs as the same task."""
    ca = await canonical_task_id(project_id, a)
    cb = await canonical_task_id(project_id, b)
    if ca is not None and cb is not None:
        return ca == cb
    return _norm_task_ref(a) == _norm_task_ref(b)


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
        # Root cause: store the canonical full UUID so later exact-match gates
        # (submit verify, review evidence, core-interaction) never fail on a
        # short-id prefix the agent passed at creation time.
        if task_id:
            task_id = await canonical_task_id(project_id, task_id)
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
                # P2-4 task-level pooling: accept attestations from ANY agent
                # on the same task (delegated/wrapped tasks need this — the
                # executor may differ from the original attestation creator).
                # Only enforce agent match for agent-scoped attestations
                # (no task_id on the row).
                row_task = row.get("task_id") or ""
                if not (
                    task_id
                    and row_task
                    and await _task_ids_equal(project_id, task_id, row_task)
                ):
                    return (
                        False,
                        f"Attestation agent mismatch: {aid} "
                        f"(expected {expected_agent_id[:8]}, "
                        f"got {str(row.get('agent_id') or '')[:8]})",
                    )
            kind = row.get("kind") or ""
            if kinds_ok is not None and kind not in kinds_ok:
                return (
                    False,
                    f"Attestation kind '{kind}' not in expected {sorted(kinds_ok)}",
                )
            if task_id and row.get("task_id") and not await _task_ids_equal(
                project_id, task_id, row.get("task_id")
            ):
                return False, f"Attestation task_id mismatch: {aid}"
            if not row.get("stdout_hash"):
                return False, f"Attestation missing stdout_hash: {aid}"
            # visual_check / test_run / browse_e2e with exit≠0 must NOT unlock
            # gates (failed runs are still recorded for audit — TEST6 P0-3).
            if kind in (VISUAL_CHECK_KIND, "test_run", BROWSE_E2E_KIND):
                ec = row.get("exit_code")
                if ec is not None and int(ec) != 0:
                    return (
                        False,
                        f"{kind} {aid} has exit_code={ec}; "
                        f"only pass (exit_code=0) unlocks submit/approve",
                    )
            seen_kinds.add(kind)

        # ALL required kinds must be present (AND), not just any one (OR).
        if kinds_ok is not None and not kinds_ok.issubset(seen_kinds):
            missing = sorted(kinds_ok - seen_kinds)
            return (
                False,
                f"Missing required attestation kind(s) {missing}; "
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
            "SELECT id, task_id, kind, created_at, exit_code FROM tool_attestations "
            "WHERE project_id = ? AND agent_id = ? AND kind != ? "
            "AND (expires_at IS NULL OR expires_at > ?) "
            f"AND created_at >= ?{kind_clause} "
            "AND stdout_hash IS NOT NULL AND TRIM(stdout_hash) != '' "
            "AND (exit_code IS NULL OR exit_code = 0) "
            "ORDER BY created_at DESC LIMIT ?",
            params,
        )
        rows = await cur.fetchall()
        await cur.close()
        if not rows:
            return []
        # Normalize task_id so legacy short-id rows and canonical storage both
        # match the (possibly dotted/short) caller reference. canonical_task_id
        # returns the raw ref unchanged for non-UUID ids (e.g. "task-1"), so we
        # always dash-strip/lowercase the result — otherwise a dashed non-UUID
        # ref would never equal its own normalized row task_id.
        canonical_tid = (
            _norm_task_ref(
                (await canonical_task_id(project_id, task_id)) or task_id
            )
            if task_id
            else None
        )
        matched: list[str] = []
        fallback: list[str] = []
        for r in rows:
            rid = r["id"]
            tid = r["task_id"] or ""
            if canonical_tid and _norm_task_ref(tid) == canonical_tid:
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
# Pixel-grounded UI assertion (assert_visual). Path-only screenshots do not count.
VISUAL_CHECK_KIND = "visual_check"
# Issued by browse tool on successful CLI runs (including screenshot).
BROWSE_E2E_KIND = "browse_e2e"

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
    return (await get_valid_waiver(project_id, task_id)) is not None


async def get_valid_waiver(
    project_id: str, task_id: str | None
) -> dict[str, Any] | None:
    """Return the latest unexpired waiver row (incl. agent_id), or None."""
    if not task_id:
        return None
    await attestation_service.ensure_schema(project_id)
    try:
        conn = await _conn(project_id)
    except ProjectDbError:
        return None
    # Normalize the lookup key to the same canonical form `create()` stored
    # (full UUID, dash-stripped) so lookups never miss on dotted vs short ids.
    tid = await canonical_task_id(project_id, task_id) or str(task_id)
    now = int(time.time() * 1000)
    cur = await conn.execute(
        "SELECT id, agent_id, task_id, kind, created_at, expires_at, "
        "command_or_url, stdout_hash "
        "FROM tool_attestations "
        "WHERE project_id = ? AND task_id = ? AND kind = ? "
        "AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY created_at DESC LIMIT 1",
        [project_id, tid, WAIVER_KIND, now],
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None
    keys = row.keys() if hasattr(row, "keys") else []
    return {k: row[k] for k in keys}


async def count_waivers(project_id: str, task_id: str | None) -> int:
    """Total waiver rows ever issued for a task (including expired)."""
    if not task_id:
        return 0
    await attestation_service.ensure_schema(project_id)
    try:
        conn = await _conn(project_id)
    except ProjectDbError:
        return 0
    tid = await canonical_task_id(project_id, task_id) or str(task_id)
    cur = await conn.execute(
        "SELECT COUNT(*) AS c FROM tool_attestations "
        "WHERE project_id = ? AND task_id = ? AND kind = ?",
        [project_id, tid, WAIVER_KIND],
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return 0
    try:
        return int(row["c"] if "c" in row.keys() else 0)
    except Exception:
        return 0


async def invalidate_valid_waivers(project_id: str, task_id: str | None) -> int:
    """Invalidate all unexpired waivers for a task (set expires_at=now).

    Called on rework so waived_by third-party isolation does not persist
    across review rounds — a new submit/review cycle starts fresh.
    Lifetime count (count_waivers) is preserved for the MAX_WAIVERS_PER_TASK
    cap; only the active waiver is retired. Rows are kept for audit.

    Returns the number of waivers retired.
    """
    if not task_id:
        return 0
    await attestation_service.ensure_schema(project_id)
    try:
        conn = await _conn(project_id)
    except ProjectDbError:
        return 0
    tid = await canonical_task_id(project_id, task_id) or str(task_id)
    now = int(time.time() * 1000)
    cur = await conn.execute(
        "UPDATE tool_attestations SET expires_at = ? "
        "WHERE project_id = ? AND task_id = ? AND kind = ? "
        "AND (expires_at IS NULL OR expires_at > ?)",
        [now, project_id, tid, WAIVER_KIND, now],
    )
    retired = cur.rowcount or 0
    await conn.commit()
    await cur.close()
    if retired > 0:
        log.info(
            "waiver_invalidated_on_rework",
            project_id=project_id,
            task_id=task_id,
            retired=retired,
        )
    return retired


# Max waiver rows per task (lifetime). Escape hatch must stay narrower than
# the front door (TEST6 P0-2: 9/9 approves via waive).
MAX_WAIVERS_PER_TASK = 2

# Execution evidence kinds that may unlock a waiver (not read_file / free text).
WAIVER_EVIDENCE_KINDS = frozenset(
    {"test_run", BROWSE_E2E_KIND, VISUAL_CHECK_KIND, DOC_REVIEW_KIND}
)


async def find_core_interaction_attestation(
    project_id: str,
    task_id: str,
    agent_id: str | None = None,
    *,
    max_age_ms: int | None = None,
) -> str | None:
    """Return a fresh browse_e2e attestation id tagged core_interaction=1.

    TEST6 P0-1: UI VERIFY gate must consume attestation, not only a boolean.
    Marker lives in command_or_url as ``[core_interaction=1]`` (see browse_tools).
    """
    if not task_id:
        return None
    await attestation_service.ensure_schema(project_id)
    try:
        conn = await _conn(project_id)
    except ProjectDbError:
        return None
    now = int(time.time() * 1000)
    max_age = max_age_ms or int(
        getattr(settings, "attestation_max_age_ms", None) or DEFAULT_MAX_AGE_MS
    )
    min_created = now - max_age
    # Canonicalize so we match the (now canonical) stored task_id; also try the
    # raw reference for legacy short-id rows.
    canonical_tid = await canonical_task_id(project_id, task_id) or task_id
    refs = [canonical_tid, _norm_task_ref(task_id)]
    agent_clause = ""
    params: list[Any] = [
        project_id,
        *refs,
        BROWSE_E2E_KIND,
        now,
        min_created,
        "%[core_interaction=1]%",
    ]
    if agent_id:
        agent_clause = "AND agent_id = ? "
        params.append(agent_id)
    task_clause = " AND task_id IN ({}) ".format(
        ",".join("?" for _ in refs)
    )
    cur = await conn.execute(
        "SELECT id FROM tool_attestations "
        "WHERE project_id = ? "
        f"{task_clause}"
        "AND kind = ? "
        "AND (expires_at IS NULL OR expires_at > ?) "
        "AND created_at >= ? "
        "AND command_or_url LIKE ? "
        f"{agent_clause}"
        "ORDER BY created_at DESC LIMIT 1",
        params,
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None
    return str(row["id"] if "id" in row.keys() else "") or None


def count_reported_test_failures(test_output: str | None) -> int | None:
    """Parse machine test-runner failure counts from stdout (not NL intent).

    Recognizes vitest/jest/pytest common ``N failed`` / ``failed=N`` forms.
    Returns None when no failure counter is present.

    When multiple counters appear (e.g. ``Suites: 0 failed`` then
    ``Tests: 3 failed``), take the **max** — first-match would under-count
    and skip the VERIFY failuresAcknowledged gate.
    """
    import re

    text = test_output or ""
    if not text.strip():
        return None
    counts: list[int] = []
    for pat in (
        r"\b(\d+)\s+failed\b",
        r"=+\s*(\d+)\s+failed\b",
        r"\b(?:failed|failures)\s*[:=]\s*(\d+)\b",
    ):
        for m in re.finditer(pat, text, re.IGNORECASE):
            counts.append(int(m.group(1)))
    if not counts:
        return None
    return max(counts)


MAX_FAILURE_ACKS_REQUIRED = 20


def required_failure_acks(fail_n: int) -> int:
    """How many failuresAcknowledged entries VERIFY submit must supply."""
    return min(max(0, int(fail_n)), MAX_FAILURE_ACKS_REQUIRED)


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
    # UI: live browse evidence AND pixel-grounded assert_visual (AND).
    # Path-only PNG or prose-without-browse is rejected.
    "ui_browser_e2e": frozenset({VISUAL_CHECK_KIND, BROWSE_E2E_KIND}),
    # Soft for others — coordinator judges browse/test evidence on review
    "generic_tests": None,
    "coordinator_review": None,
}


def required_attestation_kinds(policy_id: str) -> frozenset[str] | None:
    """Kinds required at submit/approve for ``policy_id``, or None (soft)."""
    return POLICY_REQUIRED_KINDS.get(policy_id)


# ── P0-2: Reviewer-side execution evidence ──────────────────────────────
# The submitter's attestation proves THEY ran tests. The reviewer must
# ALSO execute independently — "12-second approve" without running a single
# command is the root cause of P0-1's CHANGELOG loss going undetected.
REVIEWER_KIND = "test_run"

REVIEWER_REQUIRED_KINDS: dict[str, frozenset[str] | None] = {
    # Code tasks: reviewer must have their own fresh test_run attestation
    "ui_browser_e2e": frozenset({REVIEWER_KIND}),
    "generic_tests": frozenset({REVIEWER_KIND}),
    "coordinator_review": frozenset({REVIEWER_KIND}),
    # Docs: doc_review by reviewer is sufficient (or waiver)
    "docs_only": None,
}


def reviewer_required_kinds(policy_id: str) -> frozenset[str] | None:
    """Kinds the REVIEWER must personally hold to approve, or None (exempt)."""
    return REVIEWER_REQUIRED_KINDS.get(policy_id)


async def ancestor_task_ids(
    project_id: str, task_id: str, *, max_depth: int = 8
) -> list[str]:
    """Walk parent_task_id chain (excluding ``task_id`` itself). Fail-open → []."""
    out: list[str] = []
    seen: set[str] = {str(task_id)}
    cur_id: str | None = str(task_id)
    try:
        from hiveweave.services.task import TaskService

        ts = TaskService()
        for _ in range(max_depth):
            row = await ts.get_task(project_id, cur_id)  # type: ignore[arg-type]
            if not row:
                break
            parent = row.get("parent_task_id")
            if not parent:
                break
            pid = str(parent)
            if pid in seen:
                break
            seen.add(pid)
            out.append(pid)
            cur_id = pid
    except Exception:
        return out
    return out


async def find_reviewer_attestation(
    project_id: str,
    task_id: str,
    reviewer_id: str,
    kinds: frozenset[str],
    *,
    consume_agent_ids: list[str] | None = None,
    extra_task_ids: list[str] | None = None,
    reviewer_must_hold: bool = True,
) -> bool:
    """Check if reviewer (or allowed consume agents) has fresh attestation.

    P0-2: mirrors the submitter gate but for the approving agent.
    Soft unblock (facts): ``consume_agent_ids`` lets CEO/reviewer accept a
    same-task subordinate (typically assignee) fresh successful test_run —
    not a structured next-action command.

    TEST6 audit S1: ``extra_task_ids`` (ancestor chain) expands the match
    set so CEO can consume subordinate evidence bound to a parent task.
    ``reviewer_must_hold=False`` (CEO without TEST_RUN) skips the reviewer's
    own row and only accepts consume agents — CEO's lawful path is review
    of subordinate evidence, not self-produced tests.

    Returns True if at least one valid attestation exists.
    """
    from hiveweave.db import meta as meta_db
    from hiveweave.db.project import ensure_project_db

    ws = await meta_db.get_project_workspace(project_id)
    if not ws:
        return False
    db = await ensure_project_db(ws)
    if not db:
        return False
    now_ms = int(time.time() * 1000)
    agent_ids: list[str] = []
    if reviewer_must_hold:
        agent_ids.append(reviewer_id)
    for aid in consume_agent_ids or []:
        a = str(aid or "").strip()
        if a and a not in agent_ids:
            agent_ids.append(a)
    if not agent_ids:
        return False
    task_ids: list[str] = [str(task_id)]
    for tid in extra_task_ids or []:
        t = str(tid or "").strip()
        if t and t not in task_ids:
            task_ids.append(t)
    try:
        agent_ph = ",".join("?" for _ in agent_ids)
        cur = await db.execute(
            "SELECT kind, exit_code, agent_id, task_id FROM tool_attestations "
            f"WHERE agent_id IN ({agent_ph}) "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "AND kind != 'waiver'",
            [*agent_ids, now_ms],
        )
        _rows = await cur.fetchall()
        await cur.close()
        # Normalize task binding: match by canonical id so legacy rows whose
        # task_id was stored as a short-id prefix still satisfy the gate.
        canonical_task_set: set[str] = set()
        for tid in task_ids:
            c = await canonical_task_id(project_id, tid)
            if c:
                canonical_task_set.add(c)
        for row in _rows:
            kind = row["kind"] if "kind" in row.keys() else ""
            if kind not in kinds:
                continue
            if "exit_code" in row.keys():
                ec = row["exit_code"]
                if ec is not None and int(ec) != 0:
                    continue
            row_task = row["task_id"] if "task_id" in row.keys() else None
            if row_task:
                rc = await canonical_task_id(project_id, row_task)
                if rc and rc not in canonical_task_set:
                    continue
            elif task_ids:
                # Unbound attestation does not satisfy a task-scoped gate.
                continue
            return True
    except Exception:
        pass
    return False


async def list_reviewer_attestations_diag(
    project_id: str,
    reviewer_id: str,
    *,
    kinds: frozenset[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Fresh attestations held by reviewer (any task_id) for reject diagnostics.

    TEST6 audit S6: when approve fails on task binding, surface what the
    reviewer actually holds so the mismatch is discoverable in one message.
    """
    from hiveweave.db import meta as meta_db
    from hiveweave.db.project import ensure_project_db

    ws = await meta_db.get_project_workspace(project_id)
    if not ws:
        return []
    db = await ensure_project_db(ws)
    if not db:
        return []
    now_ms = int(time.time() * 1000)
    try:
        cur = await db.execute(
            "SELECT id, kind, task_id, exit_code, created_at FROM tool_attestations "
            "WHERE agent_id = ? "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "AND kind != 'waiver' "
            "ORDER BY created_at DESC LIMIT ?",
            [reviewer_id, now_ms, max(1, int(limit))],
        )
        rows = await cur.fetchall()
        await cur.close()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows or []:
        kind = row["kind"] if "kind" in row.keys() else ""
        if kinds is not None and kind not in kinds:
            continue
        ec = row["exit_code"] if "exit_code" in row.keys() else None
        if ec is not None and int(ec) != 0:
            continue
        out.append({
            "id": row["id"] if "id" in row.keys() else "",
            "kind": kind,
            "task_id": (
                await canonical_task_id(
                    project_id, row["task_id"] if "task_id" in row.keys() else None
                )
                if "task_id" in row.keys() and row["task_id"]
                else None
            ),
        })
    return out


def _task_refs_match(a: str, b: str) -> bool:
    """Dash/case-insensitive equals OR short-id prefix relationship.

    The platform's short id is the 8-char UUID prefix agents copy from
    get_tasks. A legacy attestation row stores exactly that prefix, so a
    naive `_norm_task_ref` equality (full-UUID only) would label a genuinely
    same-task row as ``mismatch`` — the self-contradictory hint the audit
    found. Match when equal or when one is a prefix of the other.
    """
    na = _norm_task_ref(a)
    nb = _norm_task_ref(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # short-id prefix relationship (8+ chars each)
    if len(na) >= 8 and len(nb) >= 8:
        return na.startswith(nb) or nb.startswith(na)
    return False


def format_attestation_mismatch_hint(
    held: list[dict[str, Any]], *, target_task_id: str
) -> str:
    """Human-readable mismatch list for approve reject messages."""
    if not held:
        return (
            "You hold no fresh successful attestation (any task). "
            "Run tests with taskId set to the task under review."
        )
    lines = [
        "You hold fresh attestation(s) that do not match this task:"
    ]
    for h in held[:6]:
        bound = h.get("task_id") or "(unbound)"
        hid = str(h.get("id") or "")[:8]
        kind = h.get("kind") or "?"
        match = (
            "MATCH"
            if _task_refs_match(bound, target_task_id)
            else "mismatch"
        )
        lines.append(
            f"  - {kind} id={hid}… bound_task={str(bound)[:8]} ({match})"
        )
    lines.append(
        f"Target task={str(target_task_id)[:8]}… — re-run tests with "
        f'taskId="{target_task_id}" or consume assignee evidence on this task.'
    )
    return "\n".join(lines)


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


async def check_verify_baseline(
    project_id: str,
    task: dict,
    *,
    max_behind: int = 0,
) -> str | None:
    """Hard gate: VERIFY attestations must be on target/main tip.

    TEST6 evening P1-3: reject approve when all fresh attestations for this
    VERIFY task are pinned to a stale commit (personal worktree baseline).

    Returns error string or None if OK / not applicable.
    """
    title = task.get("title") or ""
    # TEST19 教训: 只认系统 VERIFY: 前缀, 不认 agent 自由 tag "verify"
    is_verify = isinstance(title, str) and title.startswith("VERIFY:")
    if not is_verify:
        return None

    ev = task.get("evidence") or {}
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = {}
    if not isinstance(ev, dict):
        ev = {}

    target = str(
        ev.get("target_merge_commit")
        or ev.get("merge_commit")
        or ev.get("merge_commit_hash")
        or ""
    ).strip()

    from hiveweave.services.worktree_review import project_main_workspace
    from hiveweave.services.git_worktree import _git

    main_ws = await project_main_workspace(project_id)
    main_tip = ""
    if main_ws:
        ok, out = await _git(["rev-parse", "HEAD"], main_ws)
        if ok:
            main_tip = (out or "").strip()

    if not target and main_tip:
        target = main_tip
    if not target:
        return None  # no baseline recorded — cannot hard-fail

    tid = str(task.get("id") or "")
    if not tid:
        return None

    await attestation_service.ensure_schema(project_id)
    try:
        conn = await _conn(project_id)
    except ProjectDbError:
        return None
    now = int(time.time() * 1000)
    # `create()` stores task_id in canonical (dash-stripped, lowercase) form;
    # query with the same canonical key so the VERIFY baseline gate never
    # misses on the dotted id the task ledger carries (attestation short-id
    # normalization audit — otherwise this gate is silently disabled).
    canonical_tid = (await canonical_task_id(project_id, tid)) or tid
    cur = await conn.execute(
        "SELECT id, kind, commit_hash, exit_code FROM tool_attestations "
        "WHERE project_id = ? AND task_id = ? "
        "AND kind IN ('test_run', 'browse_e2e', 'visual_check', 'doc_review') "
        "AND (expires_at IS NULL OR expires_at > ?) "
        "AND (exit_code IS NULL OR exit_code = 0) "
        "ORDER BY created_at DESC LIMIT 20",
        [project_id, canonical_tid, now],
    )
    rows = await cur.fetchall()
    await cur.close()
    if not rows:
        return None  # other gates handle missing attestations

    def _short(h: str) -> str:
        return (h or "")[:12]

    accepted: list[str] = []
    stale: list[str] = []
    allowed = {target.lower()}
    if main_tip:
        allowed.add(main_tip.lower())
        # Also accept short prefixes
        allowed.add(main_tip[:12].lower())
    allowed.add(target[:12].lower())

    # Issue #5: scope-aware baseline — the attestation commit may be behind
    # main tip because *unrelated* merges landed (e.g. a frontend merge landed
    # while this VERIFY targets backend). If the diff between the attestation
    # commit and main tip does NOT touch the verification scope (the files the
    # parent task changed), the baseline is still valid — forcing rework would
    # be pure waste. Compute the scope up-front. Extraction failure → empty
    # scope → _diff_touches_scope returns None → caller keeps stale (fail-closed).
    scope_files: set[str] = set()
    parent_id = str(task.get("parent_task_id") or "").strip()
    if parent_id:
        try:
            from hiveweave.services.task import TaskService
            from hiveweave.services.worktree_review import normalize_files_changed

            parent = await TaskService().get_task(project_id, parent_id)
            if parent:
                pev = parent.get("evidence") or {}
                if isinstance(pev, str):
                    try:
                        pev = json.loads(pev)
                    except Exception:
                        pev = {}
                if isinstance(pev, dict):
                    raw = pev.get("files_changed") or pev.get("filesChanged") or []
                    # Guard against a non-list (e.g. a bare str) leaking in —
                    # normalize_files_changed would iterate a str char-by-char
                    # and produce junk single-char scope entries that never
                    # match a diff path → wrongly "untouched". Treat as empty.
                    if isinstance(raw, list):
                        scope_files = set(normalize_files_changed(raw))
        except Exception:
            scope_files = set()

    for row in rows:
        ch = ""
        if hasattr(row, "keys"):
            ch = str(row["commit_hash"] or "").strip()
        else:
            ch = str(row[2] or "").strip()
        if not ch:
            stale.append("(missing commit_hash)")
            continue
        ch_l = ch.lower()
        ok_match = ch_l in allowed or any(
            a.startswith(ch_l) or ch_l.startswith(a) for a in allowed if len(a) >= 7
        )
        # Ancestor window: attestation commit is an ancestor of main tip
        # (verified on an older tip that fast-forwarded) — allow if
        # max_behind permits AND the merge commit is itself an ancestor of
        # the attestation commit (i.e. the test actually ran on code that
        # includes the merge — TEST18 P0-2). Without the target-side check,
        # a pre-merge worktree base (ancestor of main, behind ≤ max_behind)
        # would pass while never having run the merged code.
        if not ok_match and main_ws and main_tip and max_behind >= 0:
            try:
                ok_anc, _ = await _git(
                    ["merge-base", "--is-ancestor", ch, main_tip],
                    main_ws,
                )
                if ok_anc and target:
                    # Tightening: merge commit must be an ancestor of (or
                    # equal to) the attestation commit — attestation ran on
                    # code containing the merge.
                    ok_target_anc, _ = await _git(
                        ["merge-base", "--is-ancestor", target, ch],
                        main_ws,
                    )
                    if not ok_target_anc:
                        stale.append(_short(ch))
                        continue
                if ok_anc:
                    # Count how far behind
                    ok_cnt, cnt_out = await _git(
                        ["rev-list", "--count", f"{ch}..{main_tip}"],
                        main_ws,
                    )
                    behind = int((cnt_out or "0").strip() or "0") if ok_cnt else 999
                    if behind <= max_behind:
                        ok_match = True
                    elif max_behind >= 0:
                        # Issue #5: beyond the distance window — accept only if
                        # the attestation commit → main tip diff does NOT touch
                        # the verification scope (unrelated merges don't force
                        # rework). Fail-open judge → conservative (keep stale).
                        touched = await _diff_touches_scope(
                            main_ws, ch, main_tip, scope_files
                        )
                        if touched is False:
                            ok_match = True
            except Exception:
                pass
        if ok_match:
            accepted.append(_short(ch))
        else:
            stale.append(_short(ch))

    if accepted:
        return None

    return (
        f"Cannot approve VERIFY: attestation baseline stale. "
        f"target_merge_commit={_short(target)} main_tip={_short(main_tip)}; "
        f"attestation commits={stale or ['(none)']}. "
        f"Re-run tests on MAIN (project root) at the current tip, then approve."
    )


attestation_service = AttestationService()
