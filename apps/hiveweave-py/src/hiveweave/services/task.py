"""Task service — Task Ledger core service layer.

任务账本 (Task Ledger) 的核心服务层。
状态机: created → claimed → running → blocked/submitted → reviewing → approved/rework → verifying → closed

合法转换 (_TRANSITIONS):
    created   → claimed | closed
    claimed   → running | created
    running   → blocked | submitted
    blocked   → running | closed
    submitted → reviewing | running
    reviewing → approved | rework
    approved  → verifying | closed
    verifying → closed
    rework    → running
    closed    → (终态)

- create_task: JSON 序列化 acceptance_criteria/depends_on/expected_modules/tags
- 所有状态转换校验合法性，非法转换 raise ValueError
- review_task 的 rework 路径两步合一: reviewing → rework → running
- schema.py 的 tasks 表缺 due_at 列，启动时 ALTER TABLE 补齐（幂等）
"""

import json
import time
import uuid

import aiosqlite
import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ProjectDbError, ensure_project_db

log = structlog.get_logger(__name__)


def resolve_task_policy(
    title: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
) -> str:
    """Infer attestation policy_id from task metadata.

    Returns: ``ui_browser_e2e`` | ``docs_only`` | ``generic_tests``.
    """
    from hiveweave.services.attestation import resolve_task_policy as _resolve

    return _resolve(title, tags, description)

# 合法状态转换
_TRANSITIONS: dict[str, set[str]] = {
    "created": {"claimed", "closed", "blocked"},    # blocked: system VERIFY w/o QA
    "claimed": {"running", "created"},              # 开始执行或放弃认领
    # running → claimed 已移除：防止 LLM 超时后 RESUME 时误调 claim_task
    # 导致 running↔claimed 无限弹跳。如需放弃任务请用 blocked。
    "running": {"blocked", "submitted"},             # 阻塞/提交
    "blocked": {"running", "closed"},               # 解除阻塞或关闭
    "submitted": {"reviewing", "running"},          # 进入评审或撤回
    "reviewing": {"approved", "rework"},            # 审批通过或返工
    "approved": {"verifying", "closed", "rework"},   # VERIFY、关闭、或 merge 冲突返工
    "verifying": {"closed", "approved"},              # VERIFY 通过 → 关闭；VERIFY 取消 → 回 approved
    "rework": {"running"},                          # 返工回到运行
    "closed": set(),                                # 终态
}

# schema.py 的 tasks 表缺列，启动时 ALTER TABLE 补齐（幂等）
_MISSING_COLUMNS = [
    ("due_at", "INTEGER"),
    ("wait_kind", "TEXT"),
    ("wake_at", "INTEGER"),
    ("policy_id", "TEXT"),
    # archive 审计字段（cancel_task 工具写入）：谁在什么时间为什么废弃
    ("archived_by", "TEXT"),
    ("archived_reason", "TEXT"),
    ("archived_at", "INTEGER"),
    # reviewer_id: pinned on submit (default creator); start_review may overwrite
    ("reviewer_id", "TEXT"),
    # Slice-driven work mode P0: declarative acceptance contract
    ("contract_json", "TEXT"),
]
_migrated: set[str] = set()


async def _conn(project_id: str) -> aiosqlite.Connection:
    """Resolve project_id to per-project DB connection."""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
    return await ensure_project_db(workspace)


async def _query(project_id: str, sql: str, params: list | None = None) -> list:
    conn = await _conn(project_id)
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def _execute(project_id: str, sql: str, params: list | None = None) -> None:
    conn = await _conn(project_id)
    await conn.execute(sql, params or [])
    await conn.commit()


async def _execute_tx(
    project_id: str, statements: list[tuple[str, list]]
) -> None:
    """Execute multiple SQL statements in a single transaction.

    Used by the Transactional Outbox: the state transition and the event
    record are written atomically — either both commit or neither does.
    """
    conn = await _conn(project_id)
    try:
        for sql, params in statements:
            await conn.execute(sql, params)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def _ensure_schema(project_id: str) -> None:
    """Add missing columns to tasks table (idempotent)."""
    if project_id in _migrated:
        return
    for col_name, col_def in _MISSING_COLUMNS:
        try:
            await _execute(project_id,
                           f"ALTER TABLE tasks ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass  # Column already exists
    _migrated.add(project_id)


# Progress floors driven by lifecycle events (LLM may only raise further)
_PROGRESS_FLOORS: dict[str, int] = {
    "claimed": 10,
    "running": 20,
    "test_attestation": 70,
    "submitted": 90,
    "reviewing": 92,
    "approved": 95,
    "verifying": 97,
    "rework": 40,
    "closed": 100,
}


class TaskService:
    """Task Ledger — task lifecycle from creation to closure with rework support."""

    # 列顺序与 tasks 表一致（含 due_at / wait_kind / wake_at / policy_id / reviewer_id）
    _COLUMNS = (
        "id, project_id, title, description, assignee_id, creator_id, "
        "status, priority, progress, tags, parent_task_id, depends_on, "
        "acceptance_criteria, evidence, expected_modules, blocked_reason, source, "
        "retry_count, created_at, claimed_at, submitted_at, closed_at, updated_at, "
        "is_archived, due_at, wait_kind, wake_at, policy_id, reviewer_id, "
        "contract_json"
    )

    async def _raise_progress_floor(
        self, project_id: str, task_id: str, floor: int
    ) -> None:
        """Raise progress to at least ``floor`` (never decrease)."""
        if floor <= 0:
            return
        await _ensure_schema(project_id)
        rows = await _query(
            project_id, "SELECT progress FROM tasks WHERE id = ?", [task_id]
        )
        if not rows:
            return
        current = int(rows[0]["progress"] or 0)
        if current >= floor:
            return
        now_ms = int(time.time() * 1000)
        await _execute(
            project_id,
            "UPDATE tasks SET progress = ?, updated_at = ? WHERE id = ?",
            [floor, now_ms, task_id],
        )

    async def emit_task_event(
        self,
        project_id: str,
        task_id: str,
        event: str,
        *,
        agent_id: str | None = None,
        summary: str | None = None,
    ) -> None:
        """System event: progress floor + optional work_log (best-effort).

        ``event`` keys match ``_PROGRESS_FLOORS`` (claimed/running/submitted/…).
        """
        floor = _PROGRESS_FLOORS.get(event, 0)
        try:
            if floor:
                await self._raise_progress_floor(project_id, task_id, floor)
        except Exception as e:
            log.warning(
                "emit_task_event_progress_failed",
                task_id=task_id,
                event=event,
                error=str(e),
            )
        if not agent_id:
            return
        try:
            from hiveweave.services.work_log import WorkLogService

            await WorkLogService().append_log(
                project_id,
                agent_id,
                log_type="task_event",
                summary=summary
                or f"[{event}] task {task_id[:8]}",
                details={"task_id": task_id, "event": event},
            )
        except Exception as e:
            log.warning(
                "emit_task_event_worklog_failed",
                task_id=task_id,
                event=event,
                error=str(e),
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

    async def ensure_assignee_claimed(
        self, project_id: str, task_id: str
    ) -> bool:
        """If task is assigned + created (non-VERIFY), promote to claimed.

        Returns True if a claim transition ran. Idempotent for already-claimed
        / VERIFY / unassigned rows.
        """
        task = await self.get_task(project_id, task_id)
        if not task:
            return False
        if task.get("status") != "created":
            return False
        if self._is_verify_task(task):
            return False
        assignee = task.get("assignee_id")
        if not assignee:
            return False
        await self.claim_task(project_id, task_id, assignee)
        return True

    async def promote_assigned_created(
        self, project_id: str, agent_id: str | None = None
    ) -> int:
        """Heal legacy rows: assignee set + status=created → claimed (non-VERIFY).

        Used so task-advance obligations see assign=claim for older data.
        """
        await _ensure_schema(project_id)
        if agent_id:
            rows = await _query(
                project_id,
                f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0 "
                "AND status = 'created' AND assignee_id = ?",
                [agent_id],
            )
        else:
            rows = await _query(
                project_id,
                f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0 "
                "AND status = 'created' AND assignee_id IS NOT NULL",
            )
        n = 0
        for r in rows:
            d = self._row(r)
            if self._is_verify_task(d):
                continue
            tid = d.get("id")
            aid = d.get("assignee_id")
            if not tid or not aid:
                continue
            try:
                await self.claim_task(project_id, tid, aid)
                n += 1
            except Exception as e:
                log.warning(
                    "promote_assigned_created_failed",
                    task_id=tid,
                    error=str(e),
                )
        return n

    async def _transition(self, project_id: str, task_id: str, target: str) -> None:
        """Validate and execute a status transition.

        Raises ValueError if the task is not found or the transition is illegal.
        Writes a task_events row in the same transaction (Transactional Outbox).

        Leaving ``blocked`` clears wait metadata (blocked_reason / wait_kind /
        wake_at) in the same transaction — state-machine invariant: not blocked
        means not waiting. Fixes residual wait_kind on running tasks when
        ``start_task`` is used instead of ``unblock_task`` (TEST11 #5-L1).
        """
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT status FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current = rows[0]["status"]
        if target not in _TRANSITIONS.get(current, set()):
            raise ValueError(f"Illegal transition: {current} → {target}")
        now_ms = int(time.time() * 1000)
        event_id = str(uuid.uuid4())
        if current == "blocked":
            # Defensive: any exit from blocked clears wait metadata
            try:
                await _execute_tx(project_id, [
                    ("UPDATE tasks SET status = ?, blocked_reason = NULL, "
                     "wait_kind = NULL, wake_at = NULL, updated_at = ? WHERE id = ?",
                     [target, now_ms, task_id]),
                    ("INSERT INTO task_events (id, project_id, task_id, event_type, "
                     "from_status, to_status, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [event_id, project_id, task_id, f"task.{target}",
                      current, target, now_ms]),
                ])
            except Exception as e:
                # Prefer status transition over abort; then best-effort clear
                log.warning(
                    "blocked_exit_clear_metadata_failed",
                    task_id=task_id,
                    error=str(e),
                )
                await _execute_tx(project_id, [
                    ("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                     [target, now_ms, task_id]),
                    ("INSERT INTO task_events (id, project_id, task_id, event_type, "
                     "from_status, to_status, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [event_id, project_id, task_id, f"task.{target}",
                      current, target, now_ms]),
                ])
                try:
                    await _execute(
                        project_id,
                        "UPDATE tasks SET blocked_reason = NULL, wait_kind = NULL, "
                        "wake_at = NULL, updated_at = ? WHERE id = ?",
                        [now_ms, task_id],
                    )
                except Exception as e2:
                    log.warning(
                        "blocked_exit_metadata_retry_failed",
                        task_id=task_id,
                        error=str(e2),
                    )
        else:
            await _execute_tx(project_id, [
                ("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                 [target, now_ms, task_id]),
                ("INSERT INTO task_events (id, project_id, task_id, event_type, "
                 "from_status, to_status, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [event_id, project_id, task_id, f"task.{target}",
                  current, target, now_ms]),
            ])
        log.info("task_transition", task_id=task_id,
                 from_status=current, to_status=target)

    async def _transition_multi(self, project_id: str, task_id: str,
                               *targets: str) -> None:
        """Validate and execute a multi-step transition atomically.

        Validates each step against _TRANSITIONS, then performs a single
        UPDATE to the final state — no intermediate state is ever visible
        to concurrent readers. Writes a task_events row in the same tx.

        Example: _transition_multi(pid, tid, "rework", "running")
        validates reviewing → rework → running, then UPDATEs directly
        to "running" in one statement.
        """
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT status FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current = rows[0]["status"]
        # Validate each step
        state = current
        for target in targets:
            if target not in _TRANSITIONS.get(state, set()):
                raise ValueError(f"Illegal transition: {state} → {target}")
            state = target
        # Single UPDATE to final state — atomic, no intermediate visible
        now_ms = int(time.time() * 1000)
        final = targets[-1]
        event_id = str(uuid.uuid4())
        await _execute_tx(project_id, [
            ("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
             [final, now_ms, task_id]),
            ("INSERT INTO task_events (id, project_id, task_id, event_type, "
             "from_status, to_status, created_at) "
             "VALUES (?, ?, ?, ?, ?, ?, ?)",
             [event_id, project_id, task_id, f"task.{final}",
              current, final, now_ms]),
        ])
        log.info("task_transition_multi", task_id=task_id,
                 from_status=current, through=list(targets[:-1]),
                 to_status=final)

    async def claim_task(self, project_id: str, task_id: str, agent_id: str) -> None:
        """Claim a task (created → claimed). Sets assignee_id + claimed_at.

        Assign = claim: if the task is already claimed/running by this agent
        (e.g. create_task/dispatch set assignee), this is a no-op. Only
        'created' tasks transition. Wrong-assignee or illegal states raise.
        """
        task_id = await self.require_task_id(project_id, task_id)
        rows = await _query(project_id,
            "SELECT status, assignee_id FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current = rows[0]["status"]
        existing_assignee = rows[0]["assignee_id"]
        if current in ("claimed", "running") and existing_assignee == agent_id:
            # Idempotent: already assigned to this agent (assign=claim path)
            return
        if current != "created":
            raise ValueError(
                f"Task {task_id[:8]} is already '{current}'. "
                f"Only 'created' tasks can be claimed. "
                f"If the task is running, continue working and submit_task when done."
            )
        await self._transition(project_id, task_id, "claimed")
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "UPDATE tasks SET assignee_id = ?, claimed_at = ?, updated_at = ? "
            "WHERE id = ?", [agent_id, now_ms, now_ms, task_id])
        await self.emit_task_event(
            project_id,
            task_id,
            "claimed",
            agent_id=agent_id,
            summary=f"[claimed] task {task_id[:8]} by agent",
        )

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

    async def start_task(self, project_id: str, task_id: str) -> None:
        """Start a task (claimed → running).

        If the task is currently ``blocked``, delegates to ``unblock_task`` so
        wait metadata is cleared (TEST11 #5-L1). Callers that used to rely on
        ``blocked → running`` being a legal ``_transition`` must not skip that.

        Slice P0: if ``contract_json`` present, enforce ready gate (upstream
        verified) before transitioning; then set slice_status=in_progress.
        """
        task_id = await self.require_task_id(project_id, task_id)
        rows = await _query(
            project_id, "SELECT status, assignee_id FROM tasks WHERE id = ?",
            [task_id],
        )
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        if rows[0]["status"] == "blocked":
            # blocked must go through unblock_task to clear wait metadata
            await self.unblock_task(project_id, task_id)
            agent_id = rows[0]["assignee_id"]
            await self.emit_task_event(
                project_id,
                task_id,
                "running",
                agent_id=agent_id,
                summary=f"[running] task {task_id[:8]} unblocked via start_task",
            )
            return

        # READY GATE (slice-driven)
        task = await self.get_task(project_id, task_id)
        if task and task.get("contract_json"):
            from hiveweave.services.task_contract import (
                check_ready_gate,
                ensure_slice_status,
                parse_contract,
            )

            async def _lookup_tid(tid: str):
                return await self.get_task(project_id, tid)

            async def _lookup_sid(sid: str):
                return await self.find_task_by_slice_id(project_id, sid)

            ready_err = await check_ready_gate(
                project_id,
                task,
                lookup_by_slice_id=_lookup_sid,
                lookup_by_task_id=_lookup_tid,
            )
            if ready_err:
                raise ValueError(ready_err)
            contract = parse_contract(task.get("contract_json"))
            if contract:
                contract = ensure_slice_status(contract, "ready")
                # Will flip to in_progress after transition succeeds

        await self._transition(project_id, task_id, "running")
        agent_id = rows[0]["assignee_id"]

        if task and task.get("contract_json"):
            from hiveweave.services.task_contract import (
                ensure_slice_status,
                parse_contract,
            )

            contract = parse_contract(task.get("contract_json"))
            if contract:
                contract = ensure_slice_status(contract, "in_progress")
                await self._persist_contract_json(project_id, task_id, contract)

        await self.emit_task_event(
            project_id,
            task_id,
            "running",
            agent_id=agent_id,
            summary=f"[running] task {task_id[:8]} started",
        )

    async def block_task(
        self,
        project_id: str,
        task_id: str,
        reason: str,
        *,
        depends_on_task_id: str | None = None,
    ) -> None:
        """Block a task (running → blocked). Sets blocked_reason.

        Prefer typed prefixes in reason: dependency: / timer: / user: / external:
        When ``depends_on_task_id`` is set, merges it into ``depends_on`` so
        ``_wake_dependent_tasks`` can auto-unblock (TEST11 #5-L2).
        """
        task_id = await self.require_task_id(project_id, task_id)
        if depends_on_task_id:
            depends_on_task_id = await self.require_task_id(
                project_id, depends_on_task_id
            )
        await self._transition(project_id, task_id, "blocked")
        now_ms = int(time.time() * 1000)
        reason = (reason or "Blocked by agent").strip()
        wait_kind = self._infer_wait_kind(reason)
        # Best-effort: set wait_kind / clear wake_at when columns exist
        try:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = ?, wait_kind = ?, "
                "wake_at = CASE WHEN ? = 'timer' THEN wake_at ELSE NULL END, "
                "updated_at = ? WHERE id = ?",
                [reason, wait_kind, wait_kind, now_ms, task_id],
            )
        except Exception:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = ?, updated_at = ? WHERE id = ?",
                [reason, now_ms, task_id],
            )
        # Structured dependency ref → merge into depends_on (auto-wake path)
        dep = (depends_on_task_id or "").strip()
        if dep:
            try:
                rows = await _query(
                    project_id,
                    "SELECT depends_on FROM tasks WHERE id = ?",
                    [task_id],
                )
                deps: list = []
                if rows and rows[0]["depends_on"]:
                    raw = rows[0]["depends_on"]
                    try:
                        deps = json.loads(raw) if isinstance(raw, str) else list(raw)
                    except (json.JSONDecodeError, TypeError):
                        deps = []
                if not isinstance(deps, list):
                    deps = []
                if dep not in deps:
                    deps.append(dep)
                    await _execute(
                        project_id,
                        "UPDATE tasks SET depends_on = ?, updated_at = ? WHERE id = ?",
                        [json.dumps(deps), now_ms, task_id],
                    )
            except Exception as e:
                log.warning(
                    "block_task_depends_on_merge_failed",
                    task_id=task_id,
                    error=str(e),
                )

    async def unblock_task(self, project_id: str, task_id: str) -> None:
        """Unblock a task (blocked → running). Clears blocked_reason."""
        task_id = await self.require_task_id(project_id, task_id)
        await self._transition(project_id, task_id, "running")
        now_ms = int(time.time() * 1000)
        try:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = NULL, wait_kind = NULL, "
                "wake_at = NULL, updated_at = ? WHERE id = ?",
                [now_ms, task_id],
            )
        except Exception:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = NULL, updated_at = ? WHERE id = ?",
                [now_ms, task_id],
            )

    @staticmethod
    def _infer_wait_kind(reason: str) -> str | None:
        r = (reason or "").strip().lower()
        for kind in ("dependency", "timer", "user", "external"):
            if r.startswith(f"{kind}:"):
                return kind
        return None

    async def _wake_dependent_tasks(
        self, project_id: str, completed_task_id: str
    ) -> None:
        """Unblock + notify assignees whose depends_on are all approved/closed."""
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE status = 'blocked' AND is_archived = 0",
            [],
        )
        if not rows:
            return

        completed = set()
        done_rows = await _query(
            project_id,
            "SELECT id FROM tasks WHERE status IN ('approved','closed') "
            "AND is_archived = 0",
            [],
        )
        completed = {r["id"] for r in done_rows}
        completed.add(completed_task_id)

        for row in rows:
            task = self._row(row)
            tid = task["id"]
            deps = task.get("depends_on") or []
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except (json.JSONDecodeError, TypeError):
                    deps = []
            if not isinstance(deps, list):
                deps = []

            reason = (task.get("blocked_reason") or "").strip()
            reason_l = reason.lower()
            mentions = completed_task_id in reason or completed_task_id[:8] in reason

            # Auto-unblock only with structured evidence (task id in depends_on
            # or dependency: reason mentioning the completed task id).
            # Agent-name-only weak match removed (TEST11 audit H3) — CEO/HR
            # and zero-assignment agents were false-positive "all done".
            if completed_task_id not in deps and not (
                reason_l.startswith("dependency:") and mentions
            ):
                continue

            # All explicit depends_on must be done (if any)
            if deps and not all(d in completed for d in deps):
                continue

            assignee = task.get("assignee_id")
            try:
                await self.unblock_task(project_id, tid)
            except Exception as e:
                log.warning(
                    "dependent_unblock_failed",
                    task_id=tid,
                    completed=completed_task_id,
                    error=str(e),
                )
                continue

            log.info(
                "dependent_task_unblocked",
                task_id=tid,
                completed=completed_task_id,
                assignee=assignee,
            )
            if not assignee:
                continue
            try:
                from hiveweave.services.inbox import InboxService
                from hiveweave.agents.trigger import trigger_subordinate

                title = (task.get("title") or "")[:80]
                await InboxService().send_message(
                    "system",
                    assignee,
                    (
                        f"[DEPENDENCY MET] Blocker {completed_task_id[:8]}… is done. "
                        f"Your blocked task '{title}' is unblocked (running). "
                        f"Continue work or submit_task."
                    ),
                    message_type="system",
                    priority="urgent",
                    task_id=tid,
                )
                await trigger_subordinate(assignee)
            except Exception as e:
                log.warning(
                    "dependent_wake_failed",
                    task_id=tid,
                    error=str(e),
                )

    async def reconcile_blocked_tasks(self, project_id: str) -> int:
        """Sweep blocked tasks: met deps / expired timers → unblock (TEST11 #8).

        Returns number of tasks unblocked. Idempotent.
        """
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE status = 'blocked' AND is_archived = 0",
            [],
        )
        if not rows:
            return 0

        done_rows = await _query(
            project_id,
            "SELECT id FROM tasks WHERE status IN ('approved','closed') "
            "AND is_archived = 0",
            [],
        )
        completed = {r["id"] for r in done_rows}
        woken = 0
        for row in rows:
            task = self._row(row)
            tid = task["id"]
            wait_kind = (task.get("wait_kind") or "").lower()
            wake_at = task.get("wake_at")
            deps = task.get("depends_on") or []
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except (json.JSONDecodeError, TypeError):
                    deps = []
            if not isinstance(deps, list):
                deps = []

            should_wake = False
            reason_tag = ""
            if wait_kind == "timer" and wake_at is not None:
                try:
                    if int(wake_at) <= now_ms:
                        should_wake = True
                        reason_tag = "timer_expired"
                except (TypeError, ValueError):
                    pass
            if deps and all(d in completed for d in deps):
                should_wake = True
                reason_tag = reason_tag or "depends_on_met"

            if not should_wake:
                continue
            assignee = task.get("assignee_id")
            try:
                await self.unblock_task(project_id, tid)
                woken += 1
            except Exception as e:
                log.warning(
                    "reconcile_blocked_unblock_failed",
                    task_id=tid,
                    error=str(e),
                )
                continue
            log.info(
                "reconcile_blocked_unblocked",
                task_id=tid,
                reason=reason_tag,
                assignee=assignee,
            )
            if not assignee:
                continue
            try:
                from hiveweave.services.inbox import InboxService
                from hiveweave.agents.trigger import trigger_subordinate

                title = (task.get("title") or "")[:80]
                await InboxService().send_message(
                    "system",
                    assignee,
                    (
                        f"[BLOCKED RECONCILED] Task '{title}' ({tid[:8]}) "
                        f"unblocked ({reason_tag}). Continue or submit_task."
                    ),
                    message_type="system",
                    priority="urgent",
                    task_id=tid,
                )
                await trigger_subordinate(assignee)
            except Exception as e:
                log.warning(
                    "reconcile_blocked_notify_failed",
                    task_id=tid,
                    error=str(e),
                )
        return woken

    async def submit_task(self, project_id: str, task_id: str,
                          evidence: dict) -> None:
        """Submit a task (running → submitted). Sets evidence (JSON) + submitted_at.

        BUG-P1b: 保留既有 evidence.merged_by —— VERIFY spawn 时写入的
        合并人标记是 review_task 独立审门排除合并人的唯一依据，submit
        整体覆盖 evidence 会让该门禁失效。

        TEST11 #3: on submit, pin ``reviewer_id`` (default creator_id) so the
        designated reviewer has obligations from the submitted window onward —
        not only after they call start_review.

        Slice P0: if ``contract_json`` present, L0 machine clauses must pass
        against the assignee worktree (or project root) before transition.
        """
        task_id = await self.require_task_id(project_id, task_id)
        task = await self.get_task(project_id, task_id)

        # SUBMITTED MACHINE PRE-RUN (slice-driven L0)
        if task and task.get("contract_json"):
            from hiveweave.services.task_contract import (
                ensure_slice_status,
                format_prerun_failure,
                parse_contract,
                run_machine_acceptance,
            )

            contract = parse_contract(task.get("contract_json"))
            if contract:
                ws_root = await self._resolve_evidence_workspace(
                    project_id, task
                )
                prerun = run_machine_acceptance(
                    contract, workspace_root=ws_root
                )
                contract = dict(contract)
                contract["machine_pre_run"] = {
                    **prerun.to_dict(),
                    "at_ms": int(time.time() * 1000),
                    "workspace": str(ws_root),
                }
                if not prerun.passed:
                    await self._persist_contract_json(
                        project_id, task_id, contract
                    )
                    raise ValueError(format_prerun_failure(prerun))
                contract = ensure_slice_status(contract, "submitted")
                await self._persist_contract_json(
                    project_id, task_id, contract
                )
                if isinstance(evidence, dict):
                    evidence = dict(evidence)
                    evidence["machine_pre_run"] = contract["machine_pre_run"]

        await self._transition(project_id, task_id, "submitted")
        if isinstance(evidence, dict) and "merged_by" not in evidence:
            rows0 = await _query(
                project_id, "SELECT evidence FROM tasks WHERE id = ?", [task_id]
            )
            if rows0 and rows0[0]["evidence"]:
                try:
                    prev = rows0[0]["evidence"]
                    prev = json.loads(prev) if isinstance(prev, str) else dict(prev)
                except (json.JSONDecodeError, TypeError):
                    prev = {}
                if isinstance(prev, dict) and prev.get("merged_by"):
                    evidence = dict(evidence)
                    evidence["merged_by"] = prev["merged_by"]
        now_ms = int(time.time() * 1000)
        # Pin reviewer at submit: existing column wins; evidence.reviewer_id
        # only fills when column is empty (non-VERIFY). VERIFY always → creator.
        meta_rows = await _query(
            project_id,
            "SELECT assignee_id, creator_id, reviewer_id, tags, title "
            "FROM tasks WHERE id = ?",
            [task_id],
        )
        agent_id = meta_rows[0]["assignee_id"] if meta_rows else None
        reviewer_id = None
        if meta_rows:
            creator_id = meta_rows[0]["creator_id"]
            existing_reviewer = meta_rows[0]["reviewer_id"]
            draft = {
                "tags": meta_rows[0]["tags"],
                "title": meta_rows[0]["title"],
            }
            if self._is_verify_task(draft):
                reviewer_id = creator_id
            elif existing_reviewer:
                reviewer_id = existing_reviewer
            elif isinstance(evidence, dict) and evidence.get("reviewer_id"):
                reviewer_id = str(evidence["reviewer_id"])
            else:
                reviewer_id = creator_id
        if reviewer_id:
            await _execute(
                project_id,
                "UPDATE tasks SET evidence = ?, submitted_at = ?, "
                "reviewer_id = ?, updated_at = ? WHERE id = ?",
                [json.dumps(evidence), now_ms, reviewer_id, now_ms, task_id],
            )
        else:
            await _execute(
                project_id,
                "UPDATE tasks SET evidence = ?, submitted_at = ?, updated_at = ? "
                "WHERE id = ?",
                [json.dumps(evidence), now_ms, now_ms, task_id],
            )
        await self.emit_task_event(
            project_id,
            task_id,
            "submitted",
            agent_id=agent_id,
            summary=f"[submitted] task {task_id[:8]}",
        )

    async def _resolve_evidence_workspace(
        self, project_id: str, task: dict
    ) -> str:
        """Prefer assignee write worktree; fall back to project root."""
        from hiveweave.db import meta as meta_db

        project_ws = await meta_db.get_project_workspace(project_id) or ""
        assignee_id = task.get("assignee_id")
        if not assignee_id:
            return project_ws
        try:
            from hiveweave.services.org import OrgService

            agent = await OrgService().get_agent(str(assignee_id))
            wt = (agent or {}).get("workspace_path") or ""
            if wt:
                from pathlib import Path

                if Path(wt).is_dir():
                    return wt
        except Exception as e:
            log.debug(
                "evidence_workspace_fallback",
                task_id=(task.get("id") or "")[:12],
                error=str(e),
            )
        return project_ws

    async def start_review(self, project_id: str, task_id: str,
                           reviewer_id: str | None = None) -> None:
        """Start review (submitted → reviewing). Store reviewer_id for obligations."""
        task_id = await self.require_task_id(project_id, task_id)
        await self._transition(project_id, task_id, "reviewing")
        if reviewer_id:
            now_ms = int(time.time() * 1000)
            await _execute(project_id,
                "UPDATE tasks SET reviewer_id = ?, updated_at = ? WHERE id = ?",
                [reviewer_id, now_ms, task_id])

    async def review_task(self, project_id: str, task_id: str, decision: str,
                          feedback: str | None = None,
                          reviewer_id: str | None = None) -> None:
        """Review a task (reviewing → approved/rework, or approved → rework).

        decision='approve': reviewing → approved.
        decision='rework':  reviewing|approved → rework → running (两步合一).
        feedback stored in evidence.review_feedback; reviewer_id stored in
        evidence.reviewed_by (merge 自有分支门 / VERIFY 独立性依赖它).
        """
        task_id = await self.require_task_id(project_id, task_id)
        await _ensure_schema(project_id)
        decision = decision.lower()
        if decision not in ("approve", "rework"):
            raise ValueError(
                f"Invalid decision: {decision} (expected 'approve' or 'rework')")

        # 取现有 evidence 以便合并 feedback（不覆盖已提交的 evidence）
        rows = await _query(project_id,
            "SELECT evidence, status FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current_status = rows[0]["status"]
        existing = rows[0]["evidence"]
        evidence: dict = {}
        if existing:
            try:
                evidence = json.loads(existing) if isinstance(existing, str) \
                    else dict(existing)
            except (json.JSONDecodeError, TypeError):
                evidence = {}
        if feedback is not None:
            evidence["review_feedback"] = feedback
        if reviewer_id:
            evidence["reviewed_by"] = reviewer_id

        now_ms = int(time.time() * 1000)
        if decision == "approve":
            if current_status != "reviewing":
                raise ValueError(
                    f"Illegal transition: {current_status} → approved"
                )
            # reviewing → approved
            await self._transition(project_id, task_id, "approved")
            await _execute(project_id,
                "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
                [json.dumps(evidence), now_ms, task_id])
            # Slice P0: mark contract verified so downstream ready gates unlock
            try:
                task_row = await self.get_task(project_id, task_id)
                from hiveweave.services.task_contract import (
                    ensure_slice_status,
                    parse_contract,
                )

                c = parse_contract((task_row or {}).get("contract_json"))
                if c:
                    c = ensure_slice_status(c, "verified")
                    await self._persist_contract_json(project_id, task_id, c)
            except Exception as e:
                log.warning(
                    "slice_mark_verified_failed",
                    task_id=task_id,
                    error=str(e),
                )
            log.info("task_reviewed", task_id=task_id, decision=decision,
                     has_feedback=feedback is not None)
            await self._wake_dependent_tasks(project_id, task_id)
            # VERIFY child: close VERIFY + close parent in one lifecycle step
            try:
                task = await self.get_task(project_id, task_id)
                if task and self._is_verify_task(task):
                    await self._close_verify_and_parent(project_id, task)
            except Exception as e:
                log.warning(
                    "verify_auto_close_failed",
                    task_id=task_id,
                    error=str(e),
                )
            await self.emit_task_event(
                project_id,
                task_id,
                "approved",
                summary=f"[approved] task {task_id[:8]}",
            )
        else:
            # rework from reviewing (normal) or approved (merge conflict)
            if current_status not in ("reviewing", "approved"):
                raise ValueError(
                    f"Illegal transition: {current_status} → rework"
                )
            await self._transition_multi(project_id, task_id, "rework", "running")
            await _execute(project_id,
                "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
                [json.dumps(evidence), now_ms, task_id])
            try:
                task_row = await self.get_task(project_id, task_id)
                from hiveweave.services.task_contract import (
                    ensure_slice_status,
                    parse_contract,
                )

                c = parse_contract((task_row or {}).get("contract_json"))
                if c:
                    c = ensure_slice_status(c, "failed")
                    await self._persist_contract_json(project_id, task_id, c)
            except Exception as e:
                log.warning(
                    "slice_mark_failed_failed",
                    task_id=task_id,
                    error=str(e),
                )
            log.info("task_reviewed", task_id=task_id, decision=decision,
                     has_feedback=feedback is not None,
                     from_status=current_status)
            rows2 = await _query(
                project_id,
                "SELECT assignee_id FROM tasks WHERE id = ?",
                [task_id],
            )
            aid = rows2[0]["assignee_id"] if rows2 else None
            await self.emit_task_event(
                project_id,
                task_id,
                "rework",
                agent_id=aid,
                summary=f"[rework] task {task_id[:8]}",
            )

    async def close_task(self, project_id: str, task_id: str) -> None:
        """Close a task (approved|verifying → closed). Sets closed_at."""
        task_id = await self.require_task_id(project_id, task_id)
        await self._transition(project_id, task_id, "closed")
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "UPDATE tasks SET closed_at = ?, updated_at = ? WHERE id = ?",
            [now_ms, now_ms, task_id])
        await self.emit_task_event(
            project_id,
            task_id,
            "closed",
            summary=f"[closed] task {task_id[:8]}",
        )
        await self._wake_dependent_tasks(project_id, task_id)

    async def archive_task(
        self,
        project_id: str,
        task_id: str,
        *,
        archived_by: str,
        reason: str,
    ) -> str:
        """废弃任务（任意非 closed 状态 → archived）。coordinator 纠错通道。

        背景（井字棋实测 #5）：误绑的 task 卡在 claimed，状态机无出口
        （claimed 只能 →running/created），没有废弃路径 → 僵尸任务永远挂着，
        还会一直占据 assignee 的 obligations 导致 exit-gate 误判。

        archive 不走 _TRANSITIONS（它是生命周期外的纠偏操作，不是状态机的一环），
        但必须留审计痕迹：archived_by / archived_reason / archived_at。
        所有查询（list/obligations/stall）已过滤 is_archived=0，立即生效。

        Returns: 任务废弃前的状态。
        """
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("archive_task requires a non-empty reason (audit)")
        task_id = await self.require_task_id(project_id, task_id)
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            "SELECT status, is_archived FROM tasks WHERE id = ?", [task_id],
        )
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current, is_arch = rows[0]["status"], rows[0]["is_archived"]
        if is_arch:
            raise ValueError(f"Task {task_id[:8]} is already archived")
        if current == "closed":
            raise ValueError(
                f"Task {task_id[:8]} is already closed; archiving is a no-op. "
                "Closed tasks are the terminal success state."
            )
        now_ms = int(time.time() * 1000)
        event_id = str(uuid.uuid4())
        arch_payload = json.dumps({
            "archived_by": archived_by,
            "reason": reason[:500],
        })
        await _execute_tx(project_id, [
            # 根因修复：归档时同步置终态 status='cancelled'，避免
            # archived=1 但 status 停留在 verifying/submitted 等非终态
            # 导致数据矛盾（直接查 DB / task_events 审计 / 外部脚本困惑）
            ("UPDATE tasks SET is_archived = 1, status = 'cancelled', "
            "archived_by = ?, archived_reason = ?, archived_at = ?, "
            "wake_at = NULL, updated_at = ? WHERE id = ?",
            [archived_by, reason[:500], now_ms, now_ms, task_id]),
            ("INSERT INTO task_events (id, project_id, task_id, event_type, "
             "from_status, to_status, actor_id, payload, created_at) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
             [event_id, project_id, task_id, "task.archived",
              current, "cancelled", archived_by, arch_payload, now_ms]),
        ])
        log.info(
            "task_archived",
            project_id=project_id,
            task_id=task_id,
            from_status=current,
            archived_by=archived_by,
            reason=reason[:120],
        )

        # B2: VERIFY 归档时级联父任务 —— 如果归档的是 VERIFY 子任务，
        # 其父任务可能卡在 verifying 状态无法前进。回退到 approved，
        # 让 CEO/coordinator 可以重新走 merge+VERIFY 流程或直接 close。
        # （archive_task 在 current=="closed" 时已 raise，此处 current 必非 closed）
        archived_task = await self.get_task(project_id, task_id)
        if archived_task and self._is_verify_task(archived_task):
            parent_id = archived_task.get("parent_task_id")
            if parent_id:
                parent_rows = await _query(
                    project_id,
                    "SELECT status FROM tasks WHERE id = ?",
                    [parent_id],
                )
                if parent_rows and parent_rows[0]["status"] == "verifying":
                    try:
                        await self._transition(project_id, parent_id, "approved")
                        log.info(
                            "verify_archived_parent_reverted",
                            project_id=project_id,
                            verify_task_id=task_id,
                            parent_task_id=parent_id,
                            from_status="verifying",
                            to_status="approved",
                        )
                    except Exception as e:
                        log.warning(
                            "verify_archived_parent_revert_failed",
                            parent_task_id=parent_id,
                            error=str(e),
                        )

        return current

    async def unclaim_task(self, project_id: str, task_id: str) -> None:
        """释放认领（claimed → created），清空 assignee 供重新分配。

        误绑纠正的另一半：coordinator 把任务绑错人后，release 回 created
        再 dispatch 给正确的人（不必像过去那样新建任务、留僵尸）。
        """
        rows = await _query(
            project_id, "SELECT status FROM tasks WHERE id = ?", [task_id]
        )
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        await self._transition(project_id, task_id, "created")
        now_ms = int(time.time() * 1000)
        await _execute(
            project_id,
            "UPDATE tasks SET assignee_id = NULL, claimed_at = NULL, "
            "updated_at = ? WHERE id = ?",
            [now_ms, task_id],
        )
        log.info("task_unclaimed", project_id=project_id, task_id=task_id)

    async def mark_verifying(self, project_id: str, task_id: str) -> None:
        """Parent task enters verifying after VERIFY child is spawned."""
        rows = await _query(
            project_id, "SELECT status, creator_id FROM tasks WHERE id = ?", [task_id]
        )
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current = rows[0]["status"]
        creator_id = rows[0]["creator_id"]
        if current == "verifying":
            await self._clear_merge_pending_inbox(task_id, creator_id)
            return
        if current == "approved":
            await self._transition(project_id, task_id, "verifying")
            await self.emit_task_event(
                project_id,
                task_id,
                "verifying",
                summary=f"[verifying] task {task_id[:8]}",
            )
            await self._clear_merge_pending_inbox(task_id, creator_id)
            return
        if current == "closed":
            await self._clear_merge_pending_inbox(task_id, creator_id)
            return
        raise ValueError(f"Cannot mark verifying from status={current}")

    async def _clear_merge_pending_inbox(
        self, task_id: str, creator_id: str | None
    ) -> None:
        """Mark stale [MERGE PENDING] for this task as read (merge already done)."""
        if not creator_id or not task_id:
            return
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().supersede_watchdog_messages(
                creator_id,
                prefixes=["[MERGE PENDING]", "[MERGE PROXY]"],
                contains=task_id[:8],
            )
        except Exception as e:
            log.warning(
                "clear_merge_pending_failed",
                task_id=task_id,
                creator_id=creator_id,
                error=str(e),
            )

    @staticmethod
    def _is_verify_task(task: dict) -> bool:
        title = task.get("title") or ""
        tags = task.get("tags") or []
        if isinstance(title, str) and title.startswith("VERIFY:"):
            return True
        if isinstance(tags, list) and "verify" in [str(x).lower() for x in tags]:
            return True
        return False

    async def _close_verify_and_parent(
        self, project_id: str, verify_task: dict
    ) -> None:
        """Close VERIFY child and its parent (approved|verifying → closed).

        Also archives/closes sibling open VERIFY tasks for the same parent
        (system + manual duplicates left behind after one VERIFY succeeds).
        """
        verify_id = verify_task.get("id")
        if not verify_id:
            return
        # Close VERIFY itself (approved → closed)
        try:
            await self.close_task(project_id, verify_id)
        except Exception as e:
            log.warning(
                "verify_child_close_failed",
                task_id=verify_id,
                error=str(e),
            )
            return

        # Mark verification case as passed
        try:
            vcs = VerificationCaseService()
            await vcs.mark_passed(project_id, verify_id)
        except Exception:
            pass  # best-effort

        parent_id = verify_task.get("parent_task_id")
        if parent_id:
            await self._close_sibling_verify_tasks(
                project_id, parent_id, except_id=verify_id
            )

        if not parent_id:
            # Infer: title "VERIFY: <parent title>" + same assignee
            return
        parent = await self.get_task(project_id, parent_id)
        if not parent:
            return
        status = parent.get("status")
        if status in ("approved", "verifying"):
            try:
                await self.close_task(project_id, parent_id)
                log.info(
                    "verify_parent_closed",
                    verify_id=verify_id,
                    parent_id=parent_id,
                )
            except Exception as e:
                log.warning(
                    "verify_parent_close_failed",
                    parent_id=parent_id,
                    error=str(e),
                )

    async def _close_sibling_verify_tasks(
        self,
        project_id: str,
        parent_id: str,
        *,
        except_id: str | None = None,
    ) -> int:
        """Close/archive other open VERIFY children of the same parent."""
        await _ensure_schema(project_id)
        tasks = await self.list_tasks(project_id)
        closed = 0
        for t in tasks:
            tid = t.get("id")
            if not tid or tid == except_id:
                continue
            if t.get("parent_task_id") != parent_id:
                continue
            if not self._is_verify_task(t):
                continue
            if t.get("status") in ("closed",):
                continue
            try:
                # Prefer close when legal; else archive so they leave the ledger
                st = t.get("status")
                if st in ("approved", "verifying", "submitted", "reviewing"):
                    # Force closed via archive path for non-closable states
                    await self.archive_task(
                        project_id,
                        tid,
                        archived_by="system",
                        reason="sibling VERIFY closed; duplicate cleaned up",
                    )
                elif st in ("created", "claimed", "running", "blocked"):
                    await self.archive_task(
                        project_id,
                        tid,
                        archived_by="system",
                        reason="sibling VERIFY closed; duplicate cleaned up",
                    )
                else:
                    await self.close_task(project_id, tid)
                closed += 1
            except Exception as e:
                log.warning(
                    "verify_sibling_cleanup_failed",
                    task_id=tid,
                    parent_id=parent_id,
                    error=str(e),
                )
        if closed:
            log.info(
                "verify_siblings_cleaned",
                parent_id=parent_id,
                closed=closed,
            )
        return closed

    async def migrate_orphan_approved(self, project_id: str) -> dict:
        """One-shot: approved with open VERIFY → verifying; else → closed."""
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE is_archived = 0 AND status = 'approved'",
        )
        to_verifying = 0
        to_closed = 0
        for r in rows:
            task = self._row(r)
            if self._is_verify_task(task):
                # Orphan approved VERIFY → close (and parent if any)
                await self._close_verify_and_parent(project_id, task)
                to_closed += 1
                continue
            tid = task["id"]
            children = await _query(
                project_id,
                f"SELECT {self._COLUMNS} FROM tasks "
                "WHERE parent_task_id = ? AND is_archived = 0",
                [tid],
            )
            has_open_verify = False
            for ch in children:
                child = self._row(ch)
                if self._is_verify_task(child) and child.get("status") not in (
                    "closed",
                ):
                    has_open_verify = True
                    break
            if has_open_verify:
                await self.mark_verifying(project_id, tid)
                to_verifying += 1
            else:
                await self.close_task(project_id, tid)
                to_closed += 1
        return {"verifying": to_verifying, "closed": to_closed}

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

    async def find_similar_open_task(
        self,
        project_id: str,
        title: str,
        assignee_id: str | None = None,
    ) -> dict | None:
        """Find an open (non-terminal) task with same assignee + similar title.

        Used to block duplicate scaffold/module tickets. Similarity = normalized
        title equality or shared prefix (≥12 chars).
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
        if assignee_id:
            sql += "AND assignee_id = ? "
            params.append(assignee_id)
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

    async def get_tasks_for_agent(self, project_id: str,
                                  agent_id: str) -> list[dict]:
        """Get tasks assigned to an agent. Excludes archived. ORDER BY created_at DESC."""
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE assignee_id = ? AND is_archived = 0 ORDER BY created_at DESC",
            [agent_id])
        return [self._row(r) for r in rows]

    async def get_actionable_obligations(
        self, project_id: str, agent_id: str
    ) -> list[dict]:
        """Tasks this agent must act on now (open-task reminder / stall helpers).

        - As assignee: claimed | running | rework | verifying (VERIFY assignee)
          Assign = claim: assigned non-VERIFY tasks are promoted from created
          before this query. VERIFY stays created until merge/stale nudge.
        - As reviewer: submitted | reviewing (TEST11 #3 — obligation from submit)
        - As creator: submitted | reviewing | approved
          When reviewer_id is set and ≠ creator, review obligation sits on the
          reviewer only (creator keeps approved → merge).
          approved (non-VERIFY) = must git_worktree_merge (CREATOR_MUST_MERGE).
          VERIFY children never stay as creator merge obligations.
        Excludes blocked / closed / archived.
        Each dict includes role_hint: 'assignee' | 'reviewer' | 'creator'.
        """
        await _ensure_schema(project_id)
        # Heal legacy assign-without-claim rows so obligations stay consistent
        try:
            await self.promote_assigned_created(project_id, agent_id)
        except Exception as e:
            log.warning(
                "promote_assigned_created_on_obligations_failed",
                agent_id=agent_id,
                error=str(e),
            )
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0 AND ("
            "  (assignee_id = ? AND status IN "
            "   ('claimed','running','rework','verifying'))"
            "  OR (reviewer_id = ? AND status IN ('submitted','reviewing'))"
            "  OR (creator_id = ? AND status IN "
            "   ('submitted','reviewing','approved'))"
            ") ORDER BY updated_at DESC",
            [agent_id, agent_id, agent_id],
        )
        out: list[dict] = []
        for r in rows:
            d = self._row(r)
            status = d.get("status")
            if d.get("assignee_id") == agent_id and status in (
                "claimed", "running", "rework", "verifying",
            ):
                # verifying on non-VERIFY assignee is not actionable for them
                if status == "verifying" and not self._is_verify_task(d):
                    continue
                d["role_hint"] = "assignee"
            elif d.get("reviewer_id") == agent_id and status in (
                "submitted", "reviewing",
            ):
                # reviewer obligation from submit onward (TEST11 #3)
                d["role_hint"] = "reviewer"
            else:
                # Creator merge obligation: skip VERIFY (closed on approve)
                if status == "approved" and self._is_verify_task(d):
                    continue
                # Designated reviewer ≠ creator owns the review window
                if status in ("submitted", "reviewing"):
                    rid = d.get("reviewer_id")
                    if rid and rid != agent_id:
                        continue
                d["role_hint"] = "creator"
            out.append(d)
        return out

    async def set_wake_at(
        self, project_id: str, task_id: str, wake_at_ms: int | None
    ) -> None:
        """Set or clear wake_at (real-time ms) for timer waits."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        await _execute(
            project_id,
            "UPDATE tasks SET wake_at = ?, updated_at = ? WHERE id = ?",
            [wake_at_ms, now_ms, task_id],
        )

    async def update_progress(self, project_id: str, task_id: str,
                              progress: int) -> None:
        """Update progress (0-100). Never decreases below current value.

        Lifecycle floors (claim/start/submit/…) set a lower bound; LLM
        ``update_progress`` may only raise further.
        """
        if not 0 <= progress <= 100:
            raise ValueError(f"progress must be 0-100, got {progress}")
        await _ensure_schema(project_id)
        rows = await _query(
            project_id, "SELECT progress FROM tasks WHERE id = ?", [task_id]
        )
        current = int(rows[0]["progress"] or 0) if rows else 0
        new_val = max(current, progress)
        if new_val == current:
            return
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "UPDATE tasks SET progress = ?, updated_at = ? WHERE id = ?",
            [new_val, now_ms, task_id])

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


class TaskEventService:
    """Read task_events outbox — for relay (step 3) and audit queries.

    Event types: task.created, task.claimed, task.running, task.blocked,
    task.submitted, task.reviewing, task.approved, task.rework, task.closed,
    task.archived, task.verifying
    """

    async def get_undelivered(
        self, project_id: str, limit: int = 50
    ) -> list[dict]:
        """Fetch undelivered task events (for relay consumption)."""
        try:
            rows = await _query(
                project_id,
                "SELECT id, task_id, event_type, from_status, to_status, "
                "actor_id, payload, created_at "
                "FROM task_events WHERE project_id = ? AND delivered = 0 "
                "ORDER BY created_at ASC LIMIT ?",
                [project_id, limit],
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("task_events_query_failed", error=str(e))
            return []

    async def mark_delivered(self, project_id: str, event_ids: list[str]) -> None:
        """Mark events as delivered (idempotent)."""
        if not event_ids:
            return
        now_ms = int(time.time() * 1000)
        placeholders = ",".join("?" * len(event_ids))
        try:
            await _execute(
                project_id,
                f"UPDATE task_events SET delivered = 1, delivered_at = ? "
                f"WHERE id IN ({placeholders})",
                [now_ms, *event_ids],
            )
        except Exception as e:
            log.warning("task_events_mark_delivered_failed", error=str(e))

    async def get_task_history(
        self, project_id: str, task_id: str, limit: int = 50
    ) -> list[dict]:
        """Get event history for a single task (audit trail)."""
        try:
            rows = await _query(
                project_id,
                "SELECT id, event_type, from_status, to_status, actor_id, "
                "payload, created_at, delivered, delivered_at "
                "FROM task_events WHERE task_id = ? "
                "ORDER BY created_at ASC LIMIT ?",
                [task_id, limit],
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("task_events_history_failed", error=str(e))
            return []


class VerificationCaseService:
    """Single authoritative entity for the VERIFY lifecycle.

    Links original_task → verify_task → merger → QA reviewer.
    Status: pending → in_review → passed | failed | waived
    """

    async def create_case(
        self,
        project_id: str,
        original_task_id: str,
        verify_task_id: str | None = None,
        merger_agent_id: str | None = None,
    ) -> str | None:
        """Create a verification case when VERIFY is spawned."""
        case_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "INSERT INTO verification_cases "
                "(id, project_id, original_task_id, verify_task_id, "
                "merger_agent_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                [case_id, project_id, original_task_id, verify_task_id,
                 merger_agent_id, now_ms, now_ms],
            )
            log.info("verification_case_created", case_id=case_id,
                     original_task=original_task_id[:8],
                     verify_task=(verify_task_id or "")[:8])
            return case_id
        except Exception as e:
            log.warning("verification_case_create_failed", error=str(e))
            return None

    async def update_verify_task(
        self, project_id: str, original_task_id: str,
        verify_task_id: str,
    ) -> None:
        """Link the verify task to an existing case."""
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "UPDATE verification_cases SET verify_task_id = ?, "
                "status = 'in_review', updated_at = ? "
                "WHERE original_task_id = ? AND status = 'pending'",
                [verify_task_id, now_ms, original_task_id],
            )
        except Exception as e:
            log.warning("verification_case_update_verify_failed", error=str(e))

    async def set_reviewer(
        self, project_id: str, verify_task_id: str,
        qa_agent_id: str,
    ) -> None:
        """Set the QA reviewer for a verification case."""
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "UPDATE verification_cases SET qa_agent_id = ?, "
                "status = 'in_review', updated_at = ? "
                "WHERE verify_task_id = ?",
                [qa_agent_id, now_ms, verify_task_id],
            )
        except Exception as e:
            log.warning("verification_case_set_reviewer_failed", error=str(e))

    async def mark_passed(
        self, project_id: str, verify_task_id: str,
        notes: str = "",
    ) -> None:
        """Mark verification as passed (VERIFY approved)."""
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "UPDATE verification_cases SET status = 'passed', "
                "review_notes = ?, closed_at = ?, updated_at = ? "
                "WHERE verify_task_id = ?",
                [notes[:500], now_ms, now_ms, verify_task_id],
            )
        except Exception as e:
            log.warning("verification_case_mark_passed_failed", error=str(e))

    async def mark_failed(
        self, project_id: str, verify_task_id: str,
        notes: str = "",
    ) -> None:
        """Mark verification as failed (VERIFY rejected/rework)."""
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "UPDATE verification_cases SET status = 'failed', "
                "review_notes = ?, updated_at = ? "
                "WHERE verify_task_id = ?",
                [notes[:500], now_ms, verify_task_id],
            )
        except Exception as e:
            log.warning("verification_case_mark_failed_failed", error=str(e))

    async def mark_waived(
        self, project_id: str, original_task_id: str,
        reason: str = "",
    ) -> None:
        """Mark verification as waived (attestation waiver)."""
        now_ms = int(time.time() * 1000)
        try:
            await _execute(project_id,
                "UPDATE verification_cases SET status = 'waived', "
                "review_notes = ?, closed_at = ?, updated_at = ? "
                "WHERE original_task_id = ? AND status NOT IN ('passed', 'waived')",
                [reason[:500], now_ms, now_ms, original_task_id],
            )
        except Exception as e:
            log.warning("verification_case_mark_waived_failed", error=str(e))

    async def get_case_for_task(
        self, project_id: str, task_id: str
    ) -> dict | None:
        """Get verification case by original or verify task ID."""
        try:
            rows = await _query(project_id,
                "SELECT * FROM verification_cases "
                "WHERE original_task_id = ? OR verify_task_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                [task_id, task_id],
            )
            return dict(rows[0]) if rows else None
        except Exception:
            return None
