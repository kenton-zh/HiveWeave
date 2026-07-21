"""Inbox triage — staging → ready digest before trigger wakes the agent.

Platform builds a deterministic digest, then runs lifecycle hook
``inbox.triage.enrich`` (see docs/spec/lifecycle-hooks.md) so LLM/plugins
can enrich without a feature-private API. Trigger only reads ready digests.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import project as project_db
from hiveweave.services.wake_policy import classify_message

log = structlog.get_logger(__name__)

# Fixed platform priority (higher = sooner). Not LLM-inferred.
CATEGORY_RANK: dict[str, int] = {
    "ask": 500,
    "task_transition": 400,
    "approval": 350,
    "command": 300,
    "progress": 100,
}

ORDER_HINT = "ask > task_transition > approval > command > progress"

# When Inbox digest is present, Messages(detail) expands actionable categories
# (plus expect_report). progress (notify/FYI) stays digest-only.
DETAIL_FULL_CATEGORIES: frozenset[str] = frozenset(
    {"ask", "task_transition", "approval", "command"}
)


def needs_message_detail(
    category: str,
    message: dict | None = None,
    *,
    has_digest: bool,
) -> bool:
    """Whether to emit a Messages(detail) row for this inbox item.

    With a ready digest: only actionable categories (and expect_report) get
    full text. Without digest: keep detail for everything (legacy path).
    """
    if not has_digest:
        return True
    cat = (category or "").strip()
    if cat in DETAIL_FULL_CATEGORIES:
        return True
    if message and message.get("expect_report"):
        return True
    return False

# When ≥ this many actionable messages, mark batch for LLM-path (platform
# digest still used until LLM triage is wired; running lock still applies).
TRIAGE_LLM_THRESHOLD = 8
# Soft lock TTL if status stuck in running
RUNNING_TTL_MS = 60_000
# Progress / background preview truncation in detail block
PREVIEW_CHARS = 160
# Max detail lines for progress category in trigger context
MAX_PROGRESS_DETAIL = 5

_BATCH_TABLE = """
CREATE TABLE IF NOT EXISTS inbox_triage_batches (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    digest_json TEXT,
    message_count INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    ready_at INTEGER,
    consumed_at INTEGER
)
"""

_migrated: set[str] = set()
# Per-agent asyncio locks — serialize prepare_ready flights
_agent_locks: dict[str, asyncio.Lock] = {}


def _agent_lock(agent_id: str) -> asyncio.Lock:
    lock = _agent_locks.get(agent_id)
    if lock is None:
        lock = asyncio.Lock()
        _agent_locks[agent_id] = lock
    return lock


def derive_wake_category(messages: list[dict]) -> str | None:
    """Highest-priority wake category among messages (for agent.chat opts)."""
    best: str | None = None
    best_rank = -1
    for m in messages or []:
        cat = classify_inbox_row(m)
        rank = CATEGORY_RANK.get(cat, 0)
        if rank > best_rank:
            best_rank = rank
            best = cat
    return best


async def ensure_triage_schema(agent_id: str) -> None:
    """Ensure triage batch table + inbox.triage_batch_id column."""
    if agent_id in _migrated:
        return
    try:
        await project_db.execute(agent_id, _BATCH_TABLE)
    except Exception as e:
        log.debug("inbox_triage_table_create_failed", error=str(e))
    for col_name, col_def in (
        ("triage_batch_id", "TEXT"),
        ("wake_category", "TEXT"),
    ):
        try:
            await project_db.execute(
                agent_id,
                f"ALTER TABLE inbox ADD COLUMN {col_name} {col_def}",
            )
        except Exception:
            pass
    try:
        await project_db.execute(
            agent_id,
            "CREATE INDEX IF NOT EXISTS idx_inbox_triage_batch "
            "ON inbox(to_agent_id, triage_batch_id)",
        )
    except Exception:
        pass
    _migrated.add(agent_id)


def _score_message(category: str, priority: str, created_at: int | None) -> int:
    rank = CATEGORY_RANK.get(category, 200)
    urgent_boost = 50 if (priority or "").lower() == "urgent" else 0
    # Older first within same bucket (lower created_at → higher score via invert)
    age = int(created_at or 0)
    # Keep age as tie-breaker without dominating category
    return rank * 1_000_000 + urgent_boost * 10_000 + max(0, 2_000_000_000_000 - age) // 1000


def classify_inbox_row(m: dict) -> str:
    stored = (m.get("wake_category") or "").strip()
    if stored in CATEGORY_RANK:
        return stored
    return classify_message(
        message=m.get("message") or "",
        message_type=m.get("message_type") or "normal",
        expect_report=bool(m.get("expect_report")),
        from_agent_id=m.get("from_agent_id"),
        priority=m.get("priority") or "normal",
        task_id=m.get("task_id"),
    )


def build_platform_digest(
    messages: list[dict],
    *,
    name_by_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build deterministic ready_digest from inbox rows."""
    name_by_id = name_by_id or {}
    counts: dict[str, int] = {}
    priority_counts = {"urgent": 0, "normal": 0}
    by_task: dict[str, dict[str, int]] = {}
    scored: list[tuple[int, dict, str]] = []

    for m in messages:
        cat = classify_inbox_row(m)
        counts[cat] = counts.get(cat, 0) + 1
        pri = (m.get("priority") or "normal").lower()
        if pri == "urgent":
            priority_counts["urgent"] += 1
        else:
            priority_counts["normal"] += 1
        tid = (m.get("task_id") or "").strip()
        if tid:
            bucket = by_task.setdefault(tid[:12], {})
            bucket[cat] = bucket.get(cat, 0) + 1
        scored.append(
            (
                _score_message(cat, pri, m.get("created_at")),
                m,
                cat,
            )
        )

    scored.sort(key=lambda x: x[0], reverse=True)

    # Semantic fold: identical content preview from same sender → keep first
    seen_fingerprints: set[str] = set()
    items: list[dict[str, Any]] = []
    folded: list[str] = []
    for _score, m, cat in scored:
        mid = m.get("id") or ""
        content = (m.get("message") or "").strip()
        fp = f"{m.get('from_agent_id')}|{cat}|{content[:80]}"
        if fp in seen_fingerprints and cat == "progress":
            folded.append(mid)
            continue
        seen_fingerprints.add(fp)
        from_id = m.get("from_agent_id") or ""
        preview = content.replace("\n", " ")
        if len(preview) > PREVIEW_CHARS:
            preview = preview[: PREVIEW_CHARS - 1] + "…"
        action = {
            "ask": "reply",
            "task_transition": "review_or_act_on_task",
            "approval": "acknowledge_approval",
            "command": "execute",
            "progress": "skim",
        }.get(cat, "review")
        items.append(
            {
                "id": mid,
                "category": cat,
                "priority": (m.get("priority") or "normal"),
                "from": name_by_id.get(from_id) or from_id[:8] or "?",
                "task_id": (m.get("task_id") or None),
                "summary": preview,
                "action": action,
                "reply_required": bool(m.get("expect_report")) or cat == "ask",
            }
        )

    return {
        "schema_version": 1,
        "source": "platform",
        "order_hint": ORDER_HINT,
        "counts": counts,
        "priority": priority_counts,
        "total": len(messages),
        "items": items,
        "folded_ids": folded,
        "by_task": [
            {"taskId": tid, **cats} for tid, cats in list(by_task.items())[:12]
        ],
        "instruction": (
            "First todowrite a triage list from this digest; "
            "this slice handle high-priority items (ask / task_transition) first. "
            "Do not skim-only past reply_required=true."
        ),
        "message_ids": [m.get("id") for m in messages if m.get("id")],
    }


def format_digest_block(digest: dict[str, Any]) -> str:
    """Markdown block injected at top of trigger context."""
    counts = digest.get("counts") or {}
    pri = digest.get("priority") or {}
    count_parts = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
    lines = [
        "## Inbox digest (platform)",
        f"counts: {{{count_parts}}} total={digest.get('total', 0)}",
        f"priority: {{urgent:{pri.get('urgent', 0)}, normal:{pri.get('normal', 0)}}}",
        f"order_hint: {digest.get('order_hint', ORDER_HINT)}",
        f"instruction: {digest.get('instruction', '')}",
        "items (process in order):",
    ]
    for i, it in enumerate(digest.get("items") or [], 1):
        tid = it.get("task_id")
        tid_s = f" task={str(tid)[:8]}" if tid else ""
        req = " reply_required" if it.get("reply_required") else ""
        lines.append(
            f"  {i}. [{it.get('category')}|{it.get('priority')}] "
            f"id={str(it.get('id') or '')[:8]} from={it.get('from')}"
            f"{tid_s}{req} action={it.get('action')} — {it.get('summary')}"
        )
    folded = digest.get("folded_ids") or []
    if folded:
        lines.append(f"folded_duplicates: {len(folded)} progress ACKs collapsed")
    by_task = digest.get("by_task") or []
    if by_task:
        lines.append("by_task:")
        for t in by_task[:8]:
            lines.append(f"  - {json.dumps(t, ensure_ascii=False)}")
    return "\n".join(lines)


class InboxTriageService:
    """Create / reuse ready digests for an agent's pending inbox."""

    async def prepare_ready(
        self,
        agent_id: str,
        messages: list[dict],
        *,
        name_by_id: dict[str, str] | None = None,
        truncated: bool = False,
        total_unread: int | None = None,
    ) -> dict[str, Any] | None:
        """Ensure a ready digest covering exactly these messages.

        Returns:
          - digest dict with status ready (and ``_batch_id``)
          - None if triage is running / fail-closed (caller should skip or enqueue)
        """
        async with _agent_lock(agent_id):
            return await self._prepare_ready_locked(
                agent_id,
                messages,
                name_by_id=name_by_id,
                truncated=truncated,
                total_unread=total_unread,
            )

    async def _prepare_ready_locked(
        self,
        agent_id: str,
        messages: list[dict],
        *,
        name_by_id: dict[str, str] | None = None,
        truncated: bool = False,
        total_unread: int | None = None,
    ) -> dict[str, Any] | None:
        await ensure_triage_schema(agent_id)
        if not messages:
            return {
                "schema_version": 1,
                "source": "platform",
                "order_hint": ORDER_HINT,
                "counts": {},
                "priority": {"urgent": 0, "normal": 0},
                "total": 0,
                "items": [],
                "folded_ids": [],
                "by_task": [],
                "instruction": "",
                "message_ids": [],
                "_batch_id": None,
                "_status": "ready",
            }

        msg_ids = [m["id"] for m in messages if m.get("id")]
        msg_id_set = set(msg_ids)
        now = int(time.time() * 1000)

        # Reuse ready batch if it covers the same set
        existing = await self._latest_batch(agent_id)
        if existing and existing.get("status") == "ready":
            dig = self._parse_digest(existing.get("digest_json"))
            if dig and set(dig.get("message_ids") or []) == msg_id_set:
                dig["_batch_id"] = existing["id"]
                dig["_status"] = "ready"
                return dig

        # Running lock — skip wake until ready or TTL
        if existing and existing.get("status") == "running":
            created = int(existing.get("created_at") or 0)
            if now - created < RUNNING_TTL_MS:
                log.info(
                    "inbox_triage_running_skip_wake",
                    agent_id=agent_id,
                    batch_id=existing.get("id"),
                )
                return None
            # Expired running → mark expired, then rebuild
            log.warning(
                "inbox_triage_running_expired",
                agent_id=agent_id,
                batch_id=existing.get("id"),
            )
            try:
                await project_db.execute(
                    agent_id,
                    "UPDATE inbox_triage_batches SET status = ? WHERE id = ?",
                    ["expired", existing["id"]],
                )
            except Exception as e:
                log.debug("inbox_triage_expire_failed", error=str(e))

        batch_id = str(uuid.uuid4())
        # Always insert as running under lock so concurrent waiters see it if
        # we ever release the lock mid-flight; mark ready after enrich.
        await project_db.execute(
            agent_id,
            "INSERT INTO inbox_triage_batches "
            "(id, agent_id, status, digest_json, message_count, created_at, ready_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, NULL)",
            [batch_id, agent_id, "running", len(msg_ids), now],
        )

        # Assign batch id on rows
        if msg_ids:
            placeholders = ", ".join("?" * len(msg_ids))
            await project_db.execute(
                agent_id,
                f"UPDATE inbox SET triage_batch_id = ? "
                f"WHERE id IN ({placeholders})",
                [batch_id, *msg_ids],
            )

        digest = build_platform_digest(messages, name_by_id=name_by_id)
        digest["batch_id"] = batch_id
        if truncated:
            digest["truncated"] = True
            digest["total_unread"] = total_unread or len(msg_ids)
            digest["instruction"] = (
                (digest.get("instruction") or "")
                + f" NOTE: inbox truncated — showing {len(msg_ids)} of "
                f"{digest['total_unread']} unread; process digest items first."
            )
        if len(msg_ids) >= TRIAGE_LLM_THRESHOLD:
            digest["llm_triage"] = "hook_enrich_available"

        # Lifecycle hook — fail-closed aborts triage (do not mark ready).
        try:
            from hiveweave.hooks import INBOX_TRIAGE_ENRICH, HookClosedError, hooks

            hook_out: dict[str, Any] = {"digest": digest}
            await hooks.run(
                INBOX_TRIAGE_ENRICH,
                {
                    "agent_id": agent_id,
                    "messages": messages,
                    "batch_id": batch_id,
                    "digest": digest,
                },
                hook_out,
            )
            enriched = hook_out.get("digest")
            if isinstance(enriched, dict):
                digest = enriched
                digest["batch_id"] = batch_id
        except HookClosedError as e:
            log.error(
                "inbox_triage_enrich_closed",
                agent_id=agent_id,
                batch_id=batch_id,
                error=str(e),
            )
            try:
                await project_db.execute(
                    agent_id,
                    "UPDATE inbox_triage_batches SET status = ? WHERE id = ?",
                    ["failed", batch_id],
                )
            except Exception:
                pass
            return None
        except Exception as e:
            # Unexpected errors outside open-handler swallow — keep platform digest
            log.warning(
                "inbox_triage_enrich_unexpected",
                agent_id=agent_id,
                error=str(e),
            )

        ready_at = int(time.time() * 1000)
        await project_db.execute(
            agent_id,
            "UPDATE inbox_triage_batches SET status = ?, digest_json = ?, "
            "ready_at = ? WHERE id = ?",
            ["ready", json.dumps(digest, ensure_ascii=False), ready_at, batch_id],
        )

        digest["_batch_id"] = batch_id
        digest["_status"] = "ready"
        log.info(
            "inbox_triage_ready",
            agent_id=agent_id,
            batch_id=batch_id,
            total=digest.get("total"),
            counts=digest.get("counts"),
            truncated=bool(digest.get("truncated")),
        )
        return digest

    async def mark_consumed(self, agent_id: str, batch_id: str | None) -> None:
        if not batch_id:
            return
        try:
            await ensure_triage_schema(agent_id)
            now = int(time.time() * 1000)
            await project_db.execute(
                agent_id,
                "UPDATE inbox_triage_batches SET status = ?, consumed_at = ? "
                "WHERE id = ? AND agent_id = ?",
                ["consumed", now, batch_id, agent_id],
            )
        except Exception as e:
            log.debug("inbox_triage_consume_failed", error=str(e))

    async def _latest_batch(self, agent_id: str) -> dict | None:
        try:
            row = await project_db.query_one(
                agent_id,
                "SELECT id, agent_id, status, digest_json, message_count, "
                "created_at, ready_at, consumed_at "
                "FROM inbox_triage_batches WHERE agent_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                [agent_id],
            )
            return dict(row) if row else None
        except Exception:
            return None

    @staticmethod
    def _parse_digest(raw: str | None) -> dict | None:
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def order_messages_by_digest(
        self, messages: list[dict], digest: dict[str, Any]
    ) -> list[dict]:
        """Reorder messages to match digest item order; drop folded progress."""
        folded = set(digest.get("folded_ids") or [])
        by_id = {m["id"]: m for m in messages if m.get("id")}
        ordered: list[dict] = []
        seen: set[str] = set()
        for it in digest.get("items") or []:
            mid = it.get("id")
            if mid and mid in by_id and mid not in folded:
                ordered.append(by_id[mid])
                seen.add(mid)
        for m in messages:
            mid = m.get("id")
            if mid and mid not in seen and mid not in folded:
                ordered.append(m)
        return ordered


inbox_triage_service = InboxTriageService()
