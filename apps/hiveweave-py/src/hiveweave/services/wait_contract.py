"""Wait Contract — persisted waiting_on from commit_turn (P0 Hard Gates Phase 2).

Active waits gate wake policy: waiting_human only wakes on matching events.
Default TTLs + clear_expired → WAIT_TIMEOUT; SCC cycle break for agent↔agent waits.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import aiosqlite
import structlog

from hiveweave.config import settings
from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.db.project import (
    ProjectDbError,
    ensure_project_db,
    get_workspace_write_lock,
)
from hiveweave.services.turn_result import WaitingOnItem

log = structlog.get_logger(__name__)

# ── Locked writes（per-workspace 写锁纪律，TEST18 审计 S1）─────────────
from hiveweave.db.project import execute_by_project, execute_transaction_by_project


def _item_kind_ref(item: Any) -> tuple[str, str]:
    if isinstance(item, WaitingOnItem):
        return str(item.kind or "external"), str(item.ref or "")
    if isinstance(item, dict):
        return str(item.get("kind") or "external"), str(item.get("ref") or "")
    return "external", ""


def looks_unbounded_external(kind: str, ref: str) -> bool:
    """Native bg job refs — no 30-minute wait TTL."""
    if str(kind or "").lower() != "external":
        return False
    r = (ref or "").strip()
    return r.startswith(("bg-bash-", "bg-sub-"))


def _should_hold_live_offturn_wait(row: dict) -> bool:
    """True when this wait still names a live bg job."""
    kind = str(row.get("kind") or "").lower()
    ref = str(row.get("ref") or "")
    aid = str(row.get("agentId") or row.get("agent_id") or "")
    try:
        from hiveweave.services.offturn import (
            has_live_jobs_for_agent,
            is_live_job,
        )

        if kind == "external" and is_live_job(ref, agent_id=aid or None):
            return True
        if kind == "task" and aid and has_live_jobs_for_agent(aid):
            return True
    except Exception:
        pass
    return False


async def _execute_rowcount(
    project_id: str, sql: str, params: list[Any] | None = None
) -> int:
    """同纪律单语句写 + 返回 rowcount（execute_by_project 不返回 rowcount）。"""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
    lock = await get_workspace_write_lock(workspace)
    async with lock:
        conn = await ensure_project_db(workspace)
        try:
            cur = await conn.execute(sql, params or [])
            n = cur.rowcount or 0
            await conn.commit()
            await cur.close()
            return n
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


_migrated: set[str] = set()

# Default wake_on events by waiting kind
DEFAULT_WAKE_ON: dict[str, list[str]] = {
    "user": ["user_message", "task_transition", "timeout"],
    "agent": ["ask_reply", "message_from_ref", "timeout"],
    "task": ["task_transition", "timeout", "message_from_ref"],
    "timer": ["alarm", "timeout", "message_from_ref", "ask_reply", "user_message"],
    "external": [
        "external",
        "timeout",
        "message_from_ref",
        "ask_reply",
        "user_message",
    ],
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_waits (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    wake_on TEXT NOT NULL DEFAULT '[]',
    expires_at INTEGER,
    obligation_version TEXT,
    phase TEXT,
    note TEXT,
    created_at INTEGER NOT NULL,
    cleared_at INTEGER
)
"""


def default_ttl_ms(kind: str, agent_id: str | None = None) -> int:
    """Default wait TTL. Agent waits get ±20% deterministic jitter (TEST11 #1d)."""
    k = (kind or "external").lower()
    if k == "user":
        return int(settings.wait_ttl_user_ms)
    if k == "task":
        return int(settings.wait_ttl_task_ms)
    if k == "timer":
        return int(settings.wait_ttl_timer_ms)
    if k == "agent":
        base = int(settings.wait_ttl_agent_ms)
        if agent_id:
            # Deterministic ±20% jitter by agent_id hash — desyncs simultaneous
            # TTL wakes so partners don't re-wait in lockstep.
            h = int(hashlib.md5(agent_id.encode()).hexdigest()[:8], 16)
            factor = 0.8 + (h % 401) / 1000.0  # [0.8, 1.2]
            return max(60_000, int(base * factor))
        return base
    return int(settings.wait_ttl_external_ms)


async def _conn(project_id: str) -> aiosqlite.Connection:
    return await project_db.get_project_db_by_project_id(project_id)


async def _ensure_schema(project_id: str) -> None:
    if project_id in _migrated:
        return
    try:
        await execute_by_project(project_id, CREATE_SQL)
    except ProjectDbError:
        return
    try:
        await execute_by_project(
            project_id,
            "CREATE INDEX IF NOT EXISTS idx_agent_waits_agent "
            "ON agent_waits(agent_id, cleared_at)",
        )
    except Exception:
        pass
    _migrated.add(project_id)


def obligation_version(obligations: list[dict]) -> str:
    parts = sorted(
        f"{t.get('id')}:{t.get('status')}" for t in (obligations or [])
    )
    raw = "|".join(parts) or "empty"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row) if not isinstance(row, dict) else row
    wake_raw = d.get("wake_on") or "[]"
    try:
        wake_on = json.loads(wake_raw) if isinstance(wake_raw, str) else list(wake_raw)
    except Exception:
        wake_on = []
    return {
        "id": d["id"],
        "agentId": d["agent_id"],
        "projectId": d["project_id"],
        "kind": d["kind"],
        "ref": d["ref"],
        "wakeOn": wake_on,
        "expiresAt": d.get("expires_at"),
        "obligationVersion": d.get("obligation_version"),
        "phase": d.get("phase"),
        "note": d.get("note"),
        "createdAt": d.get("created_at"),
        "clearedAt": d.get("cleared_at"),
    }


def _scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCC. Returns components with size >= 1."""
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = index
        lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            result.append(comp)

    nodes = set(graph.keys())
    for outs in graph.values():
        nodes |= outs
    for v in nodes:
        if v not in indices:
            strongconnect(v)
    return result


class WaitContractService:
    """CRUD for active agent wait contracts."""

    async def replace_waits(
        self,
        project_id: str,
        agent_id: str,
        waiting_on: list[WaitingOnItem] | list[dict],
        *,
        phase: str,
        obligations: list[dict] | None = None,
        expires_at: int | None = None,
    ) -> list[dict]:
        """Clear previous active waits and insert new ones from waiting_on."""
        await _ensure_schema(project_id)

        now = int(time.time() * 1000)
        # 全有或全无：先收集语句列表（纯计算、无 await），再一次 BEGIN
        # IMMEDIATE 事务提交 — 循环内 await 会暴露"旧 wait 已清除、新 wait
        # 半插入"的窗口（TEST18 审计 S1）。
        new_external_refs: set[str] = set()
        for it in waiting_on or []:
            kind, ref = _item_kind_ref(it)
            if str(kind).lower() == "external" and ref:
                new_external_refs.add(ref)
        preserved: list[str] = []
        live_lookup_ok = False
        try:
            from hiveweave.services.offturn import live_job_ids_for_agent

            preserved = [
                jid
                for jid in live_job_ids_for_agent(agent_id)
                if jid not in new_external_refs
            ]
            live_lookup_ok = True
        except Exception:
            live_lookup_ok = False
        if not live_lookup_ok:
            # Cannot prove which external waits are dead — do not wipe them.
            statements: list[tuple[str, list[Any] | None]] = [
                (
                    "UPDATE agent_waits SET cleared_at = ? "
                    "WHERE agent_id = ? AND cleared_at IS NULL "
                    "AND kind != 'external'",
                    [now, agent_id],
                )
            ]
        elif preserved:
            placeholders = ",".join("?" * len(preserved))
            statements = [
                (
                    "UPDATE agent_waits SET cleared_at = ? "
                    "WHERE agent_id = ? AND cleared_at IS NULL "
                    f"AND NOT (kind = 'external' AND ref IN ({placeholders}))",
                    [now, agent_id, *preserved],
                )
            ]
        else:
            statements = [
                (
                    "UPDATE agent_waits SET cleared_at = ? "
                    "WHERE agent_id = ? AND cleared_at IS NULL",
                    [now, agent_id],
                )
            ]

        ver = obligation_version(obligations or [])
        created: list[dict] = []
        batch_unbounded = any(
            looks_unbounded_external(*_item_kind_ref(it))
            for it in (waiting_on or [])
        )
        for item in waiting_on or []:
            if isinstance(item, WaitingOnItem):
                kind: str = item.kind
                ref = item.ref
                note = item.note
            else:
                kind = str(item.get("kind") or "external")
                ref = str(item.get("ref") or "")
                note = item.get("note")
            if not ref:
                continue
            wake_on = list(DEFAULT_WAKE_ON.get(kind, ["timeout"]))
            if isinstance(item, dict) and item.get("wake_on"):
                wake_on = list(item["wake_on"])
            wid = str(uuid.uuid4())
            exp = expires_at
            if isinstance(item, dict) and item.get("expires_at") is not None:
                exp = int(item["expires_at"])
            unbounded = looks_unbounded_external(kind, ref) or (
                str(kind).lower() == "task" and batch_unbounded
            )
            if unbounded:
                exp = None
                wake_on = [
                    w for w in wake_on if str(w).lower() != "timeout"
                ]
                if not wake_on:
                    wake_on = (
                        ["external"]
                        if str(kind).lower() == "external"
                        else ["task_transition"]
                    )
            elif exp is None:
                exp = now + default_ttl_ms(kind, agent_id)
            statements.append(
                (
                    "INSERT INTO agent_waits "
                    "(id, agent_id, project_id, kind, ref, wake_on, expires_at, "
                    "obligation_version, phase, note, created_at, cleared_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    [
                        wid,
                        agent_id,
                        project_id,
                        kind,
                        ref,
                        json.dumps(wake_on),
                        exp,
                        ver,
                        phase,
                        note,
                        now,
                    ],
                )
            )
            created.append(
                {
                    "id": wid,
                    "agentId": agent_id,
                    "projectId": project_id,
                    "kind": kind,
                    "ref": ref,
                    "wakeOn": wake_on,
                    "expiresAt": exp,
                    "obligationVersion": ver,
                    "phase": phase,
                    "note": note,
                    "createdAt": now,
                    "clearedAt": None,
                }
            )

        try:
            await execute_transaction_by_project(project_id, statements)
        except ProjectDbError:
            return []
        log.info(
            "wait_contracts_replaced",
            agent_id=agent_id,
            count=len(created),
            phase=phase,
            obligation_version=ver,
        )
        return created

    async def clear_waits(self, project_id: str, agent_id: str) -> int:
        await _ensure_schema(project_id)
        now = int(time.time() * 1000)
        try:
            return await _execute_rowcount(
                project_id,
                "UPDATE agent_waits SET cleared_at = ? "
                "WHERE agent_id = ? AND cleared_at IS NULL",
                [now, agent_id],
            )
        except ProjectDbError:
            return 0

    async def clear_waits_matching_ref(
        self, project_id: str, agent_id: str, ref: str
    ) -> int:
        """Clear active waits whose ref matches an off-turn / external job id.

        Sibling jobs keep their waits. Empty ref is a no-op.
        """
        needle = (ref or "").strip()
        if not needle:
            return 0
        await _ensure_schema(project_id)
        now = int(time.time() * 1000)
        try:
            return await _execute_rowcount(
                project_id,
                "UPDATE agent_waits SET cleared_at = ? "
                "WHERE agent_id = ? AND cleared_at IS NULL AND ref = ?",
                [now, agent_id, needle],
            )
        except ProjectDbError:
            return 0

    async def clear_kind_agent_waits_for_sender(
        self,
        project_id: str,
        waiter_agent_id: str,
        sender_agent_id: str,
    ) -> int:
        """Clear active kind=agent waits whose ref resolves to sender.

        Does not clear kind=external / task / timer / bash-job waits.
        Match uses the same identity resolution as event_matches_waits.
        """
        sender = (sender_agent_id or "").strip()
        if not sender:
            return 0
        await _ensure_schema(project_id)
        waits = await self.list_active(project_id, waiter_agent_id)
        matched = await matching_kind_agent_waits(
            project_id,
            waits,
            from_agent_id=sender,
        )
        ids = [str(w.get("id") or "") for w in matched if w.get("id")]
        if not ids:
            return 0
        now = int(time.time() * 1000)
        placeholders = ",".join("?" * len(ids))
        try:
            return await _execute_rowcount(
                project_id,
                f"UPDATE agent_waits SET cleared_at = ? "
                f"WHERE id IN ({placeholders}) AND agent_id = ? "
                f"AND cleared_at IS NULL AND kind = 'agent'",
                [now, *ids, waiter_agent_id],
            )
        except ProjectDbError:
            return 0

    async def list_active(self, project_id: str, agent_id: str) -> list[dict]:
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return []
        cur = await conn.execute(
            "SELECT * FROM agent_waits "
            "WHERE agent_id = ? AND cleared_at IS NULL "
            "ORDER BY created_at DESC",
            [agent_id],
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_dict(r) for r in rows]

    async def list_all_active(self, project_id: str) -> list[dict]:
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return []
        cur = await conn.execute(
            "SELECT * FROM agent_waits WHERE cleared_at IS NULL "
            "ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_dict(r) for r in rows]

    async def backfill_null_expires(self, project_id: str) -> int:
        """Assign default TTL to legacy rows with NULL expires_at."""
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return 0
        cur = await conn.execute(
            "SELECT id, kind, ref, agent_id, created_at FROM agent_waits "
            "WHERE cleared_at IS NULL AND expires_at IS NULL"
        )
        rows = await cur.fetchall()
        await cur.close()
        now = int(time.time() * 1000)
        statements: list[tuple[str, list[Any] | None]] = []
        n = 0
        for r in rows:
            kind = str(r["kind"] or "external")
            ref = str(r["ref"] or "")
            aid = str(r["agent_id"] or "")
            if looks_unbounded_external(kind, ref):
                continue
            if str(kind).lower() == "task" and _should_hold_live_offturn_wait(
                {"kind": kind, "ref": ref, "agentId": aid}
            ):
                continue
            ttl = default_ttl_ms(kind)
            # Fresh TTL from *now* — created_at + ttl would instantly expire
            # companion task waits after a long off-turn job.
            exp = now + ttl
            statements.append(
                (
                    "UPDATE agent_waits SET expires_at = ? WHERE id = ?",
                    [exp, r["id"]],
                )
            )
            n += 1
        if n:
            await execute_transaction_by_project(project_id, statements)
        return n

    async def clear_expired(
        self, project_id: str, agent_id: str | None = None
    ) -> list[dict]:
        """Clear expired waits; return the wait dicts that were cleared."""
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return []
        now = int(time.time() * 1000)
        if agent_id:
            cur = await conn.execute(
                "SELECT * FROM agent_waits "
                "WHERE agent_id = ? AND cleared_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                [agent_id, now],
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM agent_waits "
                "WHERE cleared_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                [now],
            )
        rows = await cur.fetchall()
        await cur.close()
        candidates = [_row_to_dict(r) for r in rows]
        if agent_id:
            cur2 = await conn.execute(
                "SELECT * FROM agent_waits "
                "WHERE agent_id = ? AND cleared_at IS NULL "
                "AND expires_at IS NULL",
                [agent_id],
            )
        else:
            cur2 = await conn.execute(
                "SELECT * FROM agent_waits "
                "WHERE cleared_at IS NULL AND expires_at IS NULL"
            )
        null_rows = await cur2.fetchall()
        await cur2.close()
        for r in null_rows:
            d = _row_to_dict(r)
            kind = str(d.get("kind") or "")
            ref = str(d.get("ref") or "")
            if looks_unbounded_external(kind, ref):
                candidates.append(d)
        cleared: list[dict] = []
        seen: set[str] = set()
        for c in candidates:
            cid = str(c.get("id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            if _should_hold_live_offturn_wait(c):
                continue
            cleared.append(c)
        if not cleared:
            return []
        ids = [c["id"] for c in cleared]
        placeholders = ",".join("?" * len(ids))
        await execute_by_project(
            project_id,
            f"UPDATE agent_waits SET cleared_at = ? "
            f"WHERE id IN ({placeholders})",
            [now, *ids],
        )
        return cleared

    async def break_wait_cycles(
        self,
        project_id: str,
        resolve_agent_id: Callable[[str], str | None],
        *,
        parent_map: dict[str, str] | None = None,
    ) -> list[dict]:
        """Detect wait SCCs (agent↔agent and task-mediated) and clear ALL members.

        ``resolve_agent_id(ref)`` maps wait.ref (花名/short_id/uuid) → agent_id.
        ``parent_map`` (agent_id → parent_id) lets the caller declare the org
        hierarchy. Edges between a superior and its subordinate are NOT deadlock
        cycles — the superior can adjudicate the subordinate, so mutual waits up
        and down one chain are lawful task dependencies, not a stuck cycle.

        TEST3: previously only cleared ``min(agent_id)``; partners stayed stuck
        until TTL. Now clear every agent in the component and return one break
        record per cycle (``memberIds`` lists everyone to notify).
        """
        active = await self.list_all_active(project_id)
        graph: dict[str, set[str]] = {}

        def _is_hierarchy(a: str, b: str) -> bool:
            """True if a and b are in a direct ancestor/descendant relation."""
            if not parent_map:
                return False

            def _is_ancestor(anc: str, desc: str) -> bool:
                cur = parent_map.get(desc)
                seen: set[str] = set()
                while cur and cur not in seen:
                    if cur == anc:
                        return True
                    seen.add(cur)
                    cur = parent_map.get(cur)
                return False

            return _is_ancestor(a, b) or _is_ancestor(b, a)

        # agent → agent edges
        for w in active:
            if (w.get("kind") or "").lower() != "agent":
                continue
            waiter = w.get("agentId") or ""
            target = resolve_agent_id(str(w.get("ref") or ""))
            if not waiter or not target or waiter == target:
                continue
            if _is_hierarchy(waiter, target):
                continue
            graph.setdefault(waiter, set()).add(target)
            graph.setdefault(target, set())

        # task → assignee/creator edges (peer-review mutual wait via kind=task)
        task_parties = await self._task_party_map(project_id)
        for w in active:
            if (w.get("kind") or "").lower() != "task":
                continue
            waiter = w.get("agentId") or ""
            ref = str(w.get("ref") or "").strip()
            if not waiter or not ref:
                continue
            parties = task_parties.get(ref.lower()) or task_parties.get(ref[:8].lower())
            if not parties:
                continue
            for other in parties:
                if other == waiter or (other and _is_hierarchy(waiter, other)):
                    continue
                if other:
                    graph.setdefault(waiter, set()).add(other)
                    graph.setdefault(other, set())

        breaks: list[dict] = []
        for comp in _scc(graph):
            if len(comp) < 2:
                continue
            members = sorted(comp)
            # TEST11 #1b: pick earliest waiter (by wait created_at) to wake first
            earliest_by_member: dict[str, int] = {}
            for w in active:
                aid = w.get("agentId") or ""
                if aid not in members:
                    continue
                created = int(w.get("createdAt") or 0)
                prev = earliest_by_member.get(aid)
                if prev is None or created < prev:
                    earliest_by_member[aid] = created
            wake_first = min(
                members,
                key=lambda m: (earliest_by_member.get(m, 0), m),
            )
            now = int(time.time() * 1000)
            placeholders = ",".join("?" * len(members))
            try:
                n = await _execute_rowcount(
                    project_id,
                    f"UPDATE agent_waits SET cleared_at = ? "
                    f"WHERE agent_id IN ({placeholders}) AND cleared_at IS NULL",
                    [now, *members],
                )
            except ProjectDbError:
                continue
            if n:
                breaks.append(
                    {
                        "breakerId": wake_first,  # asymmetric wake primary
                        "wakeFirstId": wake_first,
                        "memberIds": members,
                        "cycle": members,
                        "clearedCount": n,
                    }
                )
                log.info(
                    "wait_cycle_broken",
                    project_id=project_id,
                    members=members,
                    cycle=members,
                    wake_first=wake_first,
                    cleared=n,
                )
        return breaks

    async def _task_party_map(
        self, project_id: str
    ) -> dict[str, set[str]]:
        """Map task id / 8-char prefix → {assignee_id, creator_id}."""
        conn = await _conn(project_id)
        if conn is None:
            return {}
        out: dict[str, set[str]] = {}
        try:
            cur = await conn.execute(
                "SELECT id, assignee_id, creator_id FROM tasks "
                "WHERE COALESCE(is_archived, 0) = 0 "
                "AND status NOT IN ('closed', 'cancelled', 'archived')"
            )
            rows = await cur.fetchall()
            await cur.close()
            for r in rows:
                tid = (r["id"] or "").strip()
                if not tid:
                    continue
                parties = {
                    p for p in (r["assignee_id"], r["creator_id"]) if p
                }
                if not parties:
                    continue
                out[tid.lower()] = parties
                out[tid[:8].lower()] = parties
        except Exception as e:
            log.warning(
                "wait_cycle_task_map_failed",
                project_id=project_id,
                error=str(e),
            )
        return out


def _norm_token(value: str | None) -> str:
    return (value or "").strip().lower()


def _exact_identity_tokens(
    *,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
) -> set[str]:
    tokens: set[str] = set()
    for raw in (from_agent_id, from_agent_name, from_short_id):
        t = _norm_token(raw)
        if not t:
            continue
        tokens.add(t)
        tokens.add(t.replace(" ", ""))
    return tokens


def _ref_matches_sender(
    ref: str,
    *,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
) -> bool:
    """Fail-open exact match of wait.ref vs sender id / name / short_id.

    No startswith prefix — prefix coincidence must not wake or clear.
    """
    r = _norm_token(ref)
    if not r:
        return False
    tokens = _exact_identity_tokens(
        from_agent_id=from_agent_id,
        from_agent_name=from_agent_name,
        from_short_id=from_short_id,
    )
    return r in tokens or r.replace(" ", "") in tokens


def _agent_accepts_ref_exact(agent: dict, ref: str) -> bool:
    """True only when ref is exactly this agent's id, name, or short_id."""
    return _ref_matches_sender(
        ref,
        from_agent_id=agent.get("id"),
        from_agent_name=agent.get("name"),
        from_short_id=agent.get("short_id"),
    )


def _wait_not_expired(wait: dict, *, now_ms: int | None = None) -> bool:
    exp = wait.get("expiresAt") if wait.get("expiresAt") is not None else wait.get("expires_at")
    if exp is None:
        return True
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        return int(exp) > now
    except (TypeError, ValueError):
        return True


async def resolve_identity_to_agent_id(
    project_id: str | None,
    token: str,
) -> str | None:
    """Map 花名 / A100 / uuid → agents.id via OrgService.

    Prefix-only OrgService hits (unique name prefix, uuid prefix) are
    rejected so match and clear stay exact on identity fields.
    Fail-open: org errors or misses return None.
    """
    raw = (token or "").strip()
    if not raw or not project_id:
        return None
    try:
        from hiveweave.services.org import OrgService

        agent = await OrgService().resolve_agent_ref(project_id, raw)
    except Exception:
        return None
    if not agent or not _agent_accepts_ref_exact(agent, raw):
        return None
    aid = str(agent.get("id") or "").strip()
    return aid or None


async def matching_kind_agent_waits(
    project_id: str | None,
    waits: list[dict],
    *,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
) -> list[dict]:
    """Active kind=agent waits whose ref is the same person as the sender."""
    if not waits:
        return []
    now = int(time.time() * 1000)
    cache: dict[str, str | None] = {}

    async def _resolved(token: str | None) -> str | None:
        raw = (token or "").strip()
        if not raw:
            return None
        key = raw.lower()
        if key not in cache:
            cache[key] = await resolve_identity_to_agent_id(project_id, raw)
        return cache[key]

    sender_id = None
    for tok in (from_agent_id, from_agent_name, from_short_id):
        sender_id = await _resolved(tok)
        if sender_id:
            break

    matched: list[dict] = []
    for w in waits:
        if str(w.get("kind") or "").lower() != "agent":
            continue
        if not _wait_not_expired(w, now_ms=now):
            continue
        ref = str(w.get("ref") or "")
        wait_id = await _resolved(ref)
        if wait_id and sender_id and wait_id == sender_id:
            matched.append(w)
            continue
        if wait_id and from_agent_id and wait_id == str(from_agent_id).strip():
            matched.append(w)
            continue
        # Fail-open: org miss → exact string equality only (no prefix).
        if _ref_matches_sender(
            ref,
            from_agent_id=from_agent_id,
            from_agent_name=from_agent_name,
            from_short_id=from_short_id,
        ):
            matched.append(w)
    return matched


async def kind_agent_wait_matches_sender(
    project_id: str | None,
    waits: list[dict],
    *,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
) -> bool:
    found = await matching_kind_agent_waits(
        project_id,
        waits,
        from_agent_id=from_agent_id,
        from_agent_name=from_agent_name,
        from_short_id=from_short_id,
    )
    return bool(found)


async def event_matches_waits(
    waits: list[dict],
    *,
    event: str,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
    project_id: str | None = None,
) -> bool:
    """True if any active wait accepts this wake event."""
    if not waits:
        return True  # no contract → fall back to disposition policy
    now = int(time.time() * 1000)
    agent_hits: list[dict] | None = None

    async def _agent_hits() -> list[dict]:
        nonlocal agent_hits
        if agent_hits is None:
            agent_hits = await matching_kind_agent_waits(
                project_id,
                waits,
                from_agent_id=from_agent_id,
                from_agent_name=from_agent_name,
                from_short_id=from_short_id,
            )
        return agent_hits

    for w in waits:
        if not _wait_not_expired(w, now_ms=now):
            continue
        wake_on = w.get("wakeOn") or w.get("wake_on") or []
        if isinstance(wake_on, str):
            try:
                wake_on = json.loads(wake_on)
            except Exception:
                wake_on = []
        kind = (w.get("kind") or "").lower()
        ref = w.get("ref") or ""

        if kind == "agent" and event in (
            "message_from_ref",
            "ask_reply",
            "command",
        ):
            hits = await _agent_hits()
            if any(h.get("id") == w.get("id") for h in hits):
                return True

        if event not in wake_on:
            continue

        if event == "message_from_ref":
            if kind == "agent":
                hits = await _agent_hits()
                if any(h.get("id") == w.get("id") for h in hits):
                    return True
                continue
            if _ref_matches_sender(
                ref,
                from_agent_id=from_agent_id,
                from_agent_name=from_agent_name,
                from_short_id=from_short_id,
            ):
                return True
            continue
        return True
    return False


_FULL_CLEAR_WAKE_SOURCES = frozenset({
    "", "user", "chat",
    "wait_timeout", "wait_cycle", "wait_satisfied",
})


def _is_person_sender(from_agent_id: str | None) -> bool:
    fid = (from_agent_id or "").strip()
    if not fid or fid.lower() == "system":
        return False
    from hiveweave.services.wake_policy import is_user_sender

    return not is_user_sender(fid)


def unique_agent_tokens(*groups: Any) -> list[str]:
    """Stable unique id/name tokens (order-preserving, case-insensitive)."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                _add(item)
            return
        text = str(value).strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(text)

    for group in groups:
        _add(group)
    return out


async def matching_sender_ids_for_waiter(
    project_id: str,
    waiter_agent_id: str,
    candidate_from_ids: list[str] | None,
) -> list[str]:
    """Subset of *candidate_from_ids* that match an active kind=agent wait."""
    pid = (project_id or "").strip()
    waiter = (waiter_agent_id or "").strip()
    tokens = unique_agent_tokens(candidate_from_ids)
    if not pid or not waiter or not tokens:
        return []
    waits = await wait_contract_service.list_active(pid, waiter)
    matched: list[str] = []
    for fid in tokens:
        if await kind_agent_wait_matches_sender(
            pid, waits, from_agent_id=fid
        ):
            matched.append(fid)
    return matched


async def apply_wake_admit_wait_clear(
    project_id: str,
    waiter_agent_id: str,
    *,
    source: str = "",
    from_agent_id: str | None = None,
    from_agent_ids: list[str] | None = None,
    trigger: bool = False,
    clear_waits: bool | None = None,
) -> str:
    """Clear waits on chat admit. Returns ``skip`` / ``scoped`` / ``all``.

    Inbox-from-person (``message_from_ref`` or a peer sender) clears only
    matching kind=agent waits. Pass **all matching senders** in
    ``from_agent_ids`` — ``from_agent_id`` is often the first inbox row,
    not the person the wait names. ``wait_satisfied`` keeps
    ``clear_waits=False`` (sibling bg-bash waits stay). Other user/timeout
    sources still full-clear.
    """
    if clear_waits is False:
        return "skip"
    src = source or ""
    senders = unique_agent_tokens(from_agent_ids, from_agent_id)
    person = src == "message_from_ref" or any(
        _is_person_sender(s) for s in senders
    )
    if person:
        if src == "message_from_ref" and not senders:
            return "skip"
        for fid in senders:
            if not _is_person_sender(fid):
                continue
            await wait_contract_service.clear_kind_agent_waits_for_sender(
                project_id, waiter_agent_id, fid
            )
        return "scoped"
    should = (
        bool(clear_waits)
        or not trigger
        or src in _FULL_CLEAR_WAKE_SOURCES
    )
    if should:
        await wait_contract_service.clear_waits(project_id, waiter_agent_id)
        return "all"
    return "skip"


async def project_id_for_agent(agent_id: str) -> str | None:
    """Best-effort agent_id → project_id (AgentRouter). Fail-open None."""
    aid = (agent_id or "").strip()
    if not aid:
        return None
    try:
        from hiveweave.db import meta as meta_db

        return await meta_db.get_agent_project_id(aid)
    except Exception:
        return None


def category_to_wake_event(
    category: str,
    *,
    from_agent_id: str | None = None,
) -> str:
    from hiveweave.services.wake_policy import is_user_sender

    if is_user_sender(from_agent_id):
        return "user_message"
    if from_agent_id == "system":
        return "timeout"
    if category == "task_transition":
        return "task_transition"
    if category == "ask":
        return "ask_reply"
    if category == "approval":
        return "task_transition"
    return "message_from_ref"


wait_contract_service = WaitContractService()
