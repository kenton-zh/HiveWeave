"""Task CRUD, lookup, and row helpers."""
from __future__ import annotations

import json
import time
import uuid

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query
from .policy import resolve_task_policy

log = structlog.get_logger(__name__)


class CrudMixin:
    """create / get / list / resolve / update / dedup helpers."""

    # 列顺序与 tasks 表一致（含 due_at / wait_kind / wake_at / policy_id / reviewer_id）
    _COLUMNS = (
        "id, project_id, title, description, assignee_id, creator_id, "
        "status, priority, progress, tags, parent_task_id, depends_on, "
        "acceptance_criteria, evidence, expected_modules, blocked_reason, source, "
        "retry_count, created_at, claimed_at, submitted_at, closed_at, updated_at, "
        "is_archived, due_at, wait_kind, wake_at, policy_id, reviewer_id, "
        "contract_json, implementer_id, implementer_worktree, owner_parked"
    )

    async def create_task(self, project_id: str, title: str, description: str,
                          creator_id: str, assignee_id: str | None = None,
                          priority: int = 2, due_at: int | None = None,
                          acceptance_criteria: list | None = None,
                          parent_task_id: str | None = None,
                          depends_on: list[str] | None = None,
                          expected_modules: list[str] | None = None,
                          tags: list[str] | None = None,
                          source: str = "agent",
                          evidence: dict | None = None,
                          contract_json: dict | None = None) -> str:
        """Create a task. JSON-serializes list/dict fields. Returns task_id.

        Assign = claim: if ``assignee_id`` is set and the task is not VERIFY,
        insert as ``claimed`` (with ``claimed_at``). Unassigned drafts and
        VERIFY children stay ``created`` until claimed / post-merge nudge.

        When ``contract_json`` is set the task is a slice: validated, given an
        initial ``slice_status`` (draft|ready), and subject to ready / pre-run
        gates on start/submit.
        """
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        task_id = str(uuid.uuid4())
        policy_id = resolve_task_policy(title, tags, description)

        # Normalize parent_task_id: agents may pass 8-char prefixes; always
        # store the full UUID so downstream queries (siblings, umbrella) match.
        if parent_task_id:
            resolved_parent = await self.resolve_task_id(project_id, parent_task_id)
            if resolved_parent:
                parent_task_id = resolved_parent

        contract_blob = None
        if contract_json is not None:
            from hiveweave.services.task_contract import (
                ensure_slice_status,
                parse_contract,
                validate_contract,
                compute_initial_slice_status,
                check_ready_gate,
            )

            parsed = parse_contract(contract_json)
            if parsed is None:
                raise ValueError("contract_json must be a non-empty object")
            verr = validate_contract(parsed)
            if verr:
                raise ValueError(verr)

            async def _lookup_tid(tid: str):
                return await self.get_task(project_id, tid)

            async def _lookup_sid(sid: str):
                return await self.find_task_by_slice_id(project_id, sid)

            # Probe upstream for initial status (ignore gate error — just classify)
            probe_task = {
                "contract_json": parsed,
                "depends_on": depends_on or [],
            }
            ready_err = await check_ready_gate(
                project_id,
                probe_task,
                lookup_by_slice_id=_lookup_sid,
                lookup_by_task_id=_lookup_tid,
            )
            initial = compute_initial_slice_status(
                parsed, upstream_all_verified=(ready_err is None)
            )
            parsed = ensure_slice_status(parsed, initial)
            contract_blob = json.dumps(parsed, ensure_ascii=False)

        # Assign = claim (VERIFY stays created until post-merge / stale nudge)
        draft = {
            "title": title,
            "tags": tags or [],
        }
        assign_is_claim = bool(assignee_id) and not self._is_verify_task(draft)
        status = "claimed" if assign_is_claim else "created"
        claimed_at = now_ms if assign_is_claim else None
        event_id = str(uuid.uuid4())
        event_type = "task.claimed" if assign_is_claim else "task.created"
        payload = json.dumps({
            "title": title[:200],
            "creator_id": creator_id,
            "assignee_id": assignee_id,
            "priority": priority,
        })
        await _execute_tx(project_id, [
            ("INSERT INTO tasks (id, project_id, title, description, assignee_id, "
            "creator_id, status, priority, progress, tags, parent_task_id, depends_on, "
            "acceptance_criteria, evidence, expected_modules, blocked_reason, source, "
            "retry_count, created_at, claimed_at, submitted_at, closed_at, updated_at, "
            "is_archived, due_at, policy_id, contract_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, NULL, ?, "
            "0, ?, ?, NULL, NULL, ?, 0, ?, ?, ?)",
            [task_id, project_id, title, description, assignee_id, creator_id,
             status, priority, json.dumps(tags) if tags else None, parent_task_id,
             json.dumps(depends_on) if depends_on else None,
             json.dumps(acceptance_criteria) if acceptance_criteria else None,
             json.dumps(evidence) if evidence else None,
             json.dumps(expected_modules) if expected_modules else None,
             source, now_ms, claimed_at, now_ms, due_at, policy_id, contract_blob]),
            ("INSERT INTO task_events (id, project_id, task_id, event_type, "
             "from_status, to_status, actor_id, payload, created_at) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
             [event_id, project_id, task_id, event_type,
              None, status, creator_id, payload, now_ms]),
        ])
        log.info("task_created", task_id=task_id, title=title[:60],
                 creator_id=creator_id, assignee_id=assignee_id,
                 status=status, policy_id=policy_id,
                 has_contract=bool(contract_blob))
        if assign_is_claim:
            await self.emit_task_event(
                project_id,
                task_id,
                "claimed",
                agent_id=assignee_id,
                summary=f"[claimed] task {task_id[:8]} on assign",
            )
        return task_id

    async def find_task_by_slice_id(
        self, project_id: str, slice_id: str
    ) -> dict | None:
        """Find a non-archived task whose contract_json.id/slice_id matches."""
        await _ensure_schema(project_id)
        sid = (slice_id or "").strip()
        if not sid:
            return None
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0 "
            "AND contract_json IS NOT NULL",
        )
        for r in rows:
            d = self._row(r)
            from hiveweave.services.task_contract import (
                parse_contract,
                slice_id_of,
            )

            c = parse_contract(d.get("contract_json"))
            if c and slice_id_of(c) == sid:
                return d
        return None

    async def _persist_contract_json(
        self, project_id: str, task_id: str, contract: dict
    ) -> None:
        now_ms = int(time.time() * 1000)
        await _execute(
            project_id,
            "UPDATE tasks SET contract_json = ?, updated_at = ? WHERE id = ?",
            [json.dumps(contract, ensure_ascii=False), now_ms, task_id],
        )

    async def resolve_task_id(self, project_id: str, ref: str) -> str | None:
        """Resolve a task reference to a full UUID.

        Accepts: full UUID, 8-char prefix (UI short id).
        Returns None if not found / ambiguous.
        """
        await _ensure_schema(project_id)
        raw = (ref or "").strip()
        if not raw:
            return None
        # Exact id
        rows = await _query(
            project_id, "SELECT id FROM tasks WHERE id = ? LIMIT 1", [raw]
        )
        if rows:
            return rows[0]["id"] if isinstance(rows[0], dict) else rows[0][0]
        # 8+ char prefix (UUID without dashes or first segment)
        prefix = raw.lower().replace("-", "")
        if len(raw) >= 8:
            # Match id starting with raw (case-insensitive) or dashed form
            all_rows = await _query(
                project_id,
                "SELECT id FROM tasks WHERE lower(id) LIKE ? OR replace(lower(id), '-', '') LIKE ?",
                [f"{raw.lower()}%", f"{prefix}%"],
            )
            ids = [
                (r["id"] if isinstance(r, dict) else r[0]) for r in all_rows
            ]
            # Prefer non-archived if multiple
            if len(ids) == 1:
                return ids[0]
            if len(ids) > 1:
                open_rows = await _query(
                    project_id,
                    "SELECT id FROM tasks WHERE is_archived = 0 AND ("
                    "lower(id) LIKE ? OR replace(lower(id), '-', '') LIKE ?)",
                    [f"{raw.lower()}%", f"{prefix}%"],
                )
                open_ids = [
                    (r["id"] if isinstance(r, dict) else r[0]) for r in open_rows
                ]
                if len(open_ids) == 1:
                    return open_ids[0]
                return None  # ambiguous
        return None

    async def require_task_id(self, project_id: str, ref: str) -> str:
        """Resolve task ref or raise ValueError with candidates when ambiguous.

        claim/submit/review/cancel/update must call this — agents often pass
        the 8-char prefix shown in get_tasks lists.
        """
        raw = (ref or "").strip()
        if not raw:
            raise ValueError("Task id is required")
        resolved = await self.resolve_task_id(project_id, raw)
        if resolved:
            return resolved
        # Distinguish ambiguous vs missing for actionable errors
        if len(raw) >= 8:
            prefix = raw.lower().replace("-", "")
            all_rows = await _query(
                project_id,
                "SELECT id, title, status FROM tasks WHERE "
                "lower(id) LIKE ? OR replace(lower(id), '-', '') LIKE ? "
                "LIMIT 8",
                [f"{raw.lower()}%", f"{prefix}%"],
            )
            if len(all_rows) > 1:
                bits = []
                for r in all_rows[:5]:
                    tid = r["id"] if isinstance(r, dict) else r[0]
                    title = (
                        (r["title"] if isinstance(r, dict) else "") or ""
                    )[:30]
                    bits.append(f"{tid[:8]}:{title}")
                raise ValueError(
                    f"Ambiguous task id prefix '{raw}' — matches "
                    f"{len(all_rows)} tasks [{', '.join(bits)}]. "
                    f"Pass the full UUID from get_tasks."
                )
        raise ValueError(
            f"Task not found: {raw}. Copy the full id=… from get_tasks "
            f"(8-char prefixes work only when unique)."
        )

    async def get_task(self, project_id: str, task_id: str) -> dict | None:
        """Get a single task by id (full UUID or short prefix). Returns all fields or None."""
        await _ensure_schema(project_id)
        resolved = await self.resolve_task_id(project_id, task_id)
        if not resolved:
            return None
        rows = await _query(project_id,
            f"SELECT {self._COLUMNS} FROM tasks WHERE id = ?", [resolved])
        return self._row(rows[0]) if rows else None

    async def list_tasks(self, project_id: str, status: str | None = None,
                         assignee_id: str | None = None,
                         *, include_archived: bool = False) -> list[dict]:
        """List tasks with optional filters. Excludes archived unless include_archived=True. ORDER BY created_at DESC."""
        await _ensure_schema(project_id)
        sql = f"SELECT {self._COLUMNS} FROM tasks"
        params: list = []
        if not include_archived:
            sql += " WHERE is_archived = 0"
        if status is not None:
            sql += " AND status = ?" if not include_archived else " WHERE status = ?"
            params.append(status)
        if assignee_id is not None:
            sql += " AND assignee_id = ?" if "WHERE" in sql or "AND" in sql else " WHERE assignee_id = ?"
            params.append(assignee_id)
        sql += " ORDER BY created_at DESC"
        rows = await _query(project_id, sql, params)
        return [self._row(r) for r in rows]

    @staticmethod
    def modules_fingerprint(modules: object | None) -> str | None:
        """Stable hash of expected_modules for language-agnostic dedup (TEST21 M3)."""
        import hashlib

        raw = modules
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return None
        norm = sorted(
            {str(m).strip().lower() for m in raw if str(m).strip()}
        )
        if not norm:
            return None
        return hashlib.md5("|".join(norm).encode("utf-8")).hexdigest()[:16]

    async def find_similar_open_task(
        self,
        project_id: str,
        title: str,
        assignee_id: str | None = None,
        *,
        include_unassigned: bool = False,
    ) -> dict | None:
        """Find an open (non-terminal) task with similar title.

        Similarity = normalized title equality or shared prefix (≥12 chars).
        TEST21 M3: ``include_unassigned`` folds assignee IS NULL into the domain
        so drafts without an owner still block duplicate creates.
        """
        await _ensure_schema(project_id)
        norm = " ".join((title or "").lower().split())
        if not norm:
            return None
        prefix = norm[:24]
        sql = (
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE is_archived = 0 "
            "AND status NOT IN ('done','cancelled','archived','completed','closed') "
        )
        params: list = []
        if assignee_id and include_unassigned:
            sql += "AND (assignee_id = ? OR assignee_id IS NULL OR assignee_id = '') "
            params.append(assignee_id)
        elif assignee_id:
            sql += "AND assignee_id = ? "
            params.append(assignee_id)
        elif include_unassigned:
            sql += "AND (assignee_id IS NULL OR assignee_id = '') "
        sql += "ORDER BY created_at DESC LIMIT 40"
        rows = await _query(project_id, sql, params)
        for r in rows:
            row = self._row(r)
            other = " ".join((row.get("title") or "").lower().split())
            if not other:
                continue
            if other == norm or (
                len(prefix) >= 12
                and (other.startswith(prefix) or norm.startswith(other[:24]))
            ):
                return row
        return None

    async def find_structured_open_dup(
        self,
        project_id: str,
        *,
        parent_task_id: str | None = None,
        expected_modules: object | None = None,
        exclude_task_id: str | None = None,
    ) -> dict | None:
        """Language-agnostic dup: same parent_task_id + expected_modules hash.

        TEST21 M3 — title text is NOT used. Includes unassigned open tasks.
        """
        await _ensure_schema(project_id)
        parent = (parent_task_id or "").strip() or None
        fp = self.modules_fingerprint(expected_modules)
        if not parent and not fp:
            return None
        sql = (
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE is_archived = 0 "
            "AND status NOT IN ('done','cancelled','archived','completed','closed') "
        )
        params: list = []
        if parent:
            sql += "AND parent_task_id = ? "
            params.append(parent)
        sql += "ORDER BY created_at DESC LIMIT 80"
        rows = await _query(project_id, sql, params)
        for r in rows:
            row = self._row(r)
            if exclude_task_id and row.get("id") == exclude_task_id:
                continue
            if parent and row.get("parent_task_id") != parent:
                continue
            if fp:
                other_fp = self.modules_fingerprint(row.get("expected_modules"))
                if other_fp != fp:
                    continue
            elif parent:
                # parent-only match when both lack modules — still a dup signal
                if self.modules_fingerprint(row.get("expected_modules")):
                    continue
            return row
        return None

    async def get_tasks_for_agent(self, project_id: str,
                                  agent_id: str) -> list[dict]:
        """Get tasks assigned to an agent. Excludes archived. ORDER BY created_at DESC."""
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE assignee_id = ? AND is_archived = 0 ORDER BY created_at DESC",
            [agent_id])
        return [self._row(r) for r in rows]

    async def update_task(self, project_id: str, task_id: str, **fields) -> None:
        """Generic PATCH update.

        Supports: title, description, priority, due_at, assignee_id, tags,
        expected_modules. JSON-serializes list fields. Updates updated_at.
        """
        allowed = {"title", "description", "priority", "due_at", "assignee_id",
                   "tags", "expected_modules"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        await _ensure_schema(project_id)
        set_clauses: list[str] = []
        params: list = []
        for k, v in updates.items():
            if k in ("tags", "expected_modules"):
                v = json.dumps(v) if v is not None else None
            set_clauses.append(f"{k} = ?")
            params.append(v)
        now_ms = int(time.time() * 1000)
        set_clauses.append("updated_at = ?")
        params.append(now_ms)
        params.append(task_id)
        await _execute(project_id,
            f"UPDATE tasks SET {', '.join(set_clauses)} WHERE id = ?", params)
        # Assign = claim when PATCH sets an assignee on a created (non-VERIFY) task
        if "assignee_id" in updates and updates.get("assignee_id"):
            try:
                await self.ensure_assignee_claimed(project_id, task_id)
            except Exception as e:
                log.warning(
                    "update_task_ensure_claimed_failed",
                    task_id=task_id,
                    error=str(e),
                )

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        # JSON 反序列化
        for k in ("tags", "depends_on", "acceptance_criteria", "evidence",
                  "expected_modules", "contract_json"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

