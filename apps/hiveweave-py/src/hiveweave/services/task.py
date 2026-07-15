"""Task service — Task Ledger core service layer.

任务账本 (Task Ledger) 的核心服务层。
状态机: created → claimed → running → blocked/submitted → reviewing → approved/rework → closed

合法转换 (_TRANSITIONS):
    created   → claimed | closed
    claimed   → running | created
    running   → blocked | submitted
    blocked   → running | closed
    submitted → reviewing | running
    reviewing → approved | rework
    approved  → closed
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

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ensure_project_db

log = structlog.get_logger(__name__)

# 合法状态转换
_TRANSITIONS: dict[str, set[str]] = {
    "created": {"claimed", "closed"},               # 可认领或直接关闭
    "claimed": {"running", "created"},              # 开始执行或放弃认领
    # running → claimed 已移除：防止 LLM 超时后 RESUME 时误调 claim_task
    # 导致 running↔claimed 无限弹跳。如需放弃任务请用 blocked。
    "running": {"blocked", "submitted"},             # 阻塞/提交
    "blocked": {"running", "closed"},               # 解除阻塞或关闭
    "submitted": {"reviewing", "running"},          # 进入评审或撤回
    "reviewing": {"approved", "rework"},            # 审批通过或返工
    "approved": {"closed"},                         # 关闭
    "rework": {"running"},                          # 返工回到运行
    "closed": set(),                                # 终态
}

# schema.py 的 tasks 表缺列，启动时 ALTER TABLE 补齐（幂等）
_MISSING_COLUMNS = [
    ("due_at", "INTEGER"),
    ("wait_kind", "TEXT"),
    ("wake_at", "INTEGER"),
]
_migrated: set[str] = set()


async def _conn(project_id: str):
    """Resolve project_id to per-project DB connection."""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ValueError(f"Workspace not found for project {project_id}")
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


class TaskService:
    """Task Ledger — task lifecycle from creation to closure with rework support."""

    # 列顺序与 tasks 表一致（含 due_at / wait_kind / wake_at）
    _COLUMNS = (
        "id, project_id, title, description, assignee_id, creator_id, "
        "status, priority, progress, tags, parent_task_id, depends_on, "
        "acceptance_criteria, evidence, expected_modules, blocked_reason, source, "
        "retry_count, created_at, claimed_at, submitted_at, closed_at, updated_at, "
        "is_archived, due_at, wait_kind, wake_at"
    )

    async def create_task(self, project_id: str, title: str, description: str,
                          creator_id: str, assignee_id: str | None = None,
                          priority: int = 2, due_at: int | None = None,
                          acceptance_criteria: list | None = None,
                          parent_task_id: str | None = None,
                          depends_on: list[str] | None = None,
                          expected_modules: list[str] | None = None,
                          tags: list[str] | None = None,
                          source: str = "agent") -> str:
        """Create a task. JSON-serializes list/dict fields. Returns task_id."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        task_id = str(uuid.uuid4())
        await _execute(project_id,
            "INSERT INTO tasks (id, project_id, title, description, assignee_id, "
            "creator_id, status, priority, progress, tags, parent_task_id, depends_on, "
            "acceptance_criteria, evidence, expected_modules, blocked_reason, source, "
            "retry_count, created_at, claimed_at, submitted_at, closed_at, updated_at, "
            "is_archived, due_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'created', ?, 0, ?, ?, ?, ?, NULL, ?, NULL, ?, "
            "0, ?, NULL, NULL, NULL, ?, 0, ?)",
            [task_id, project_id, title, description, assignee_id, creator_id,
             priority, json.dumps(tags) if tags else None, parent_task_id,
             json.dumps(depends_on) if depends_on else None,
             json.dumps(acceptance_criteria) if acceptance_criteria else None,
             json.dumps(expected_modules) if expected_modules else None,
             source, now_ms, now_ms, due_at])
        log.info("task_created", task_id=task_id, title=title[:60],
                 creator_id=creator_id, assignee_id=assignee_id)
        return task_id

    async def _transition(self, project_id: str, task_id: str, target: str) -> None:
        """Validate and execute a status transition.

        Raises ValueError if the task is not found or the transition is illegal.
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
        await _execute(project_id,
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            [target, now_ms, task_id])
        log.info("task_transition", task_id=task_id,
                 from_status=current, to_status=target)

    async def _transition_multi(self, project_id: str, task_id: str,
                               *targets: str) -> None:
        """Validate and execute a multi-step transition atomically.

        Validates each step against _TRANSITIONS, then performs a single
        UPDATE to the final state — no intermediate state is ever visible
        to concurrent readers.

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
        await _execute(project_id,
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            [final, now_ms, task_id])
        log.info("task_transition_multi", task_id=task_id,
                 from_status=current, through=list(targets[:-1]),
                 to_status=final)

    async def claim_task(self, project_id: str, task_id: str, agent_id: str) -> None:
        """Claim a task (created → claimed). Sets assignee_id + claimed_at.

        Only 'created' tasks can be claimed. If the task is already
        'claimed' or 'running' (e.g. after an LLM timeout + RESUME),
        raises ValueError with a clear message so the LLM agent knows
        to continue working instead of re-claiming.
        """
        rows = await _query(project_id,
            "SELECT status FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current = rows[0]["status"]
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

    async def start_task(self, project_id: str, task_id: str) -> None:
        """Start a task (claimed → running)."""
        await self._transition(project_id, task_id, "running")

    async def block_task(self, project_id: str, task_id: str, reason: str) -> None:
        """Block a task (running → blocked). Sets blocked_reason.

        Prefer typed prefixes in reason: dependency: / timer: / user: / external:
        """
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

    async def unblock_task(self, project_id: str, task_id: str) -> None:
        """Unblock a task (blocked → running). Clears blocked_reason."""
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

    async def submit_task(self, project_id: str, task_id: str,
                          evidence: dict) -> None:
        """Submit a task (running → submitted). Sets evidence (JSON) + submitted_at."""
        await self._transition(project_id, task_id, "submitted")
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "UPDATE tasks SET evidence = ?, submitted_at = ?, updated_at = ? "
            "WHERE id = ?",
            [json.dumps(evidence), now_ms, now_ms, task_id])

    async def start_review(self, project_id: str, task_id: str) -> None:
        """Start review (submitted → reviewing)."""
        await self._transition(project_id, task_id, "reviewing")

    async def review_task(self, project_id: str, task_id: str, decision: str,
                          feedback: str | None = None) -> None:
        """Review a task (reviewing → approved/rework).

        decision='approve': reviewing → approved.
        decision='rework':  reviewing → rework → running (两步合一).
        feedback stored in evidence.review_feedback.
        """
        await _ensure_schema(project_id)
        decision = decision.lower()
        if decision not in ("approve", "rework"):
            raise ValueError(
                f"Invalid decision: {decision} (expected 'approve' or 'rework')")

        # 取现有 evidence 以便合并 feedback（不覆盖已提交的 evidence）
        rows = await _query(project_id,
            "SELECT evidence FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
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

        now_ms = int(time.time() * 1000)
        if decision == "approve":
            # reviewing → approved
            await self._transition(project_id, task_id, "approved")
            await _execute(project_id,
                "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
                [json.dumps(evidence), now_ms, task_id])
            log.info("task_reviewed", task_id=task_id, decision=decision,
                     has_feedback=feedback is not None)
            await self._wake_dependent_tasks(project_id, task_id)
            # VERIFY tasks: approve completes the loop → auto-close
            title = ""
            try:
                trow = await _query(
                    project_id, "SELECT title, tags FROM tasks WHERE id = ?",
                    [task_id],
                )
                if trow:
                    title = trow[0]["title"] or ""
                    tags_raw = trow[0]["tags"]
                    tags = []
                    if isinstance(tags_raw, str):
                        try:
                            tags = json.loads(tags_raw)
                        except (json.JSONDecodeError, TypeError):
                            tags = []
                    elif isinstance(tags_raw, list):
                        tags = tags_raw
                    if (
                        title.startswith("VERIFY:")
                        or "verify" in [str(x).lower() for x in tags]
                    ):
                        try:
                            await self.close_task(project_id, task_id)
                        except Exception as e:
                            log.warning(
                                "verify_auto_close_failed",
                                task_id=task_id,
                                error=str(e),
                            )
            except Exception as e:
                log.debug("verify_auto_close_check_failed", error=str(e))
        else:
            # rework: reviewing → rework → running (atomic single UPDATE,
            # no intermediate "rework" state visible to concurrent readers)
            await self._transition_multi(project_id, task_id, "rework", "running")
            await _execute(project_id,
                "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
                [json.dumps(evidence), now_ms, task_id])
            log.info("task_reviewed", task_id=task_id, decision=decision,
                     has_feedback=feedback is not None)

    async def close_task(self, project_id: str, task_id: str) -> None:
        """Close a task (approved → closed). Sets closed_at."""
        await self._transition(project_id, task_id, "closed")
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "UPDATE tasks SET closed_at = ?, updated_at = ? WHERE id = ?",
            [now_ms, now_ms, task_id])
        await self._wake_dependent_tasks(project_id, task_id)

    async def get_task(self, project_id: str, task_id: str) -> dict | None:
        """Get a single task by id. Returns all fields or None."""
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            f"SELECT {self._COLUMNS} FROM tasks WHERE id = ?", [task_id])
        return self._row(rows[0]) if rows else None

    async def list_tasks(self, project_id: str, status: str | None = None,
                         assignee_id: str | None = None) -> list[dict]:
        """List tasks with optional filters. Excludes archived. ORDER BY created_at DESC."""
        await _ensure_schema(project_id)
        sql = f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0"
        params: list = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if assignee_id is not None:
            sql += " AND assignee_id = ?"
            params.append(assignee_id)
        sql += " ORDER BY created_at DESC"
        rows = await _query(project_id, sql, params)
        return [self._row(r) for r in rows]

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

        - As assignee: claimed | running | rework
        - As creator: submitted | reviewing
        Excludes blocked / approved / closed / archived.
        Each dict includes role_hint: 'assignee' | 'creator'.
        """
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0 AND ("
            "  (assignee_id = ? AND status IN ('claimed','running','rework'))"
            "  OR (creator_id = ? AND status IN ('submitted','reviewing','approved'))"
            ") ORDER BY updated_at DESC",
            [agent_id, agent_id],
        )
        out: list[dict] = []
        for r in rows:
            d = self._row(r)
            if d.get("assignee_id") == agent_id and d.get("status") in (
                "claimed", "running", "rework",
            ):
                d["role_hint"] = "assignee"
            else:
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
        """Update progress (0-100). Does not change status."""
        if not 0 <= progress <= 100:
            raise ValueError(f"progress must be 0-100, got {progress}")
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "UPDATE tasks SET progress = ?, updated_at = ? WHERE id = ?",
            [progress, now_ms, task_id])

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

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        # JSON 反序列化
        for k in ("tags", "depends_on", "acceptance_criteria", "evidence",
                  "expected_modules"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    pass
        return d
