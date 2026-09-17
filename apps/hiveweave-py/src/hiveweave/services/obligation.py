"""Obligation Ledger — structured obligations with deadlines and escalation.

TEST16 D2: Replaces pure message-driven coordination ("hope they read inbox")
with platform-enforced obligations. The game tick scans for overdue obligations
and escalates to the org parent automatically.

Obligation types:
- merge: task creator / merge owner (or MERGE-capable ancestor) must merge
  assignee worktree — 不是审批人（2026-08-11 复盘：代审场景 merge 义务
  曾错误记在 reviewer 名下）
- review: reviewer must review a submitted task
- verify: QA must verify post-merge evidence

Lifecycle: pending → fulfilled | escalated (→ re-assigned to parent)
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import (
    ProjectDbError,
    ensure_project_db,
)

log = structlog.get_logger(__name__)

# Default deadlines (ms from creation)
MERGE_DEADLINE_MS = 10 * 60 * 1000  # 10 minutes
REVIEW_DEADLINE_MS = 15 * 60 * 1000  # 15 minutes
VERIFY_DEADLINE_MS = 20 * 60 * 1000  # 20 minutes
# Dispatch registers review obligation but does not start the clock
# (TEST18 P0-1). Submit activates by resetting to REVIEW_DEADLINE_MS.
# Inert placeholder (M7/H4 sweep): submit activation unconditionally resets
# the deadline, scan_overdue skips escalation until the task is actually
# submitted/reviewing, and claimed/running periods are covered by task-stall
# nudges — shortening this value is a pure hygiene no-op.
_REVIEW_PARKED_DEADLINE_MS = 2 * 60 * 60 * 1000  # 2 hours

# Escalation: after deadline passes, escalate every N ms
ESCALATION_INTERVAL_MS = 5 * 60 * 1000  # 5 minutes between escalations
MAX_ESCALATIONS = 3  # stop escalating after 3 levels

# Review escalate only when the task is actually awaiting review
_REVIEW_ESCALATABLE_STATUSES = frozenset({"submitted", "reviewing"})

#: 「等人裁决」义务的默认 deadline（PLATFORM-ISSUES §11.6）。
#:
#: 比 review/verify 长：这类义务的成因是「等一个 agent 做裁决」，升级太快会
#: 变成噪音（裁决本身可能合理地需要跨轮往返）。30min 与
#: `lifecycle.ARBITRATION_GRACE_MS` 对齐 —— 先让 reconcile 有机会解封，
#: 确认真的卡住了再升级。
ARBITRATION_DEADLINE_MS = 30 * 60 * 1000  # 30 minutes

#: arbitration 义务**不套用** `_REVIEW_ESCALATABLE_STATUSES`（TEST18 P0-1 的
#: parked 语义只针对 review）。它的判据是「任务仍在 blocked」—— 任务一旦离开
#: blocked（被裁决 / 被 reconcile 解封 / 关闭），义务就该被结清而不是升级。
_ARBITRATION_ESCALATABLE_STATUSES = frozenset({"blocked"})


# ── DB helpers (Pattern B: keyed by project_id) ──────────────


async def _conn(project_id: str):
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
    return await ensure_project_db(workspace)


async def _query(project_id: str, sql: str, params: list[Any] | None = None):
    conn = await _conn(project_id)
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description] if cursor.description else []
    return [dict(zip(cols, r)) for r in rows]


async def _execute(project_id: str, sql: str, params: list[Any] | None = None):
    await execute_by_project(project_id, sql, params)


# ── Locked writes（per-workspace 写锁纪律，TEST18 审计 S1）─────────────
from hiveweave.db.project import execute_by_project


# ── Service ──────────────────────────────────────────────────


async def _normalize_task_id(
    project_id: str, task_id: str | None
) -> str | None:
    """Canonicalize task_id (full UUID or unique prefix) for ledger keys.

    TEST6 evening P1-1: agents often pass 8-char prefixes that get_task
    resolves, but obligations stored full UUIDs — exact-match fulfill
    silently hit 0 rows. Normalize at the ledger boundary so every
    create/fulfill/cancel shares one hygiene standard.
    """
    if not task_id:
        return None
    raw = str(task_id).strip()
    if not raw:
        return None
    try:
        from hiveweave.services.task import TaskService

        resolved = await TaskService().resolve_task_id(project_id, raw)
        if resolved:
            if resolved != raw:
                log.debug(
                    "obligation.task_id_normalized",
                    project_id=project_id,
                    raw=raw,
                    resolved=resolved,
                )
            return resolved
    except Exception as e:
        log.warning(
            "obligation.task_id_normalize_failed",
            project_id=project_id,
            raw=raw,
            error=str(e),
        )
    return raw


class ObligationLedger:
    """Platform-level obligation tracking with deadline enforcement."""

    async def create(
        self,
        project_id: str,
        owner_agent_id: str,
        obligation_type: str,
        task_id: str | None = None,
        context: dict | None = None,
        deadline_ms: int | None = None,
    ) -> str:
        """Create a new obligation. Returns obligation id.

        If a pending obligation of the same type+task already exists for
        this owner, returns the existing id (idempotent).
        """
        now = int(time.time() * 1000)
        if deadline_ms is None:
            deadline_ms = {
                "merge": MERGE_DEADLINE_MS,
                "review": REVIEW_DEADLINE_MS,
                "verify": VERIFY_DEADLINE_MS,
                "arbitration": ARBITRATION_DEADLINE_MS,
            }.get(obligation_type, REVIEW_DEADLINE_MS)

        task_id = await _normalize_task_id(project_id, task_id)

        # TEST18 P0-1: dispatch only registers; clock starts on submit.
        # Far-future deadline until submit activates/resets it.
        ctx = context or {}
        if obligation_type == "review" and ctx.get("source") == "dispatch":
            deadline_ms = _REVIEW_PARKED_DEADLINE_MS

        # Idempotency:
        # - review: one pending per task (any owner) — avoid dispatch+submit dual owners
        # - other types: same owner + type + task
        if task_id:
            if obligation_type == "review":
                existing = await _query(
                    project_id,
                    "SELECT id, owner_agent_id FROM obligations "
                    "WHERE obligation_type = 'review' AND task_id = ? "
                    "AND status = 'pending' LIMIT 1",
                    [task_id],
                )
                if existing:
                    ob_id = existing[0]["id"]
                    prev_owner = str(existing[0].get("owner_agent_id") or "")
                    # Submit activates the clock: reset deadline + clear escalations.
                    # Also retarget owner when pinned reviewer differs from dispatch.
                    if ctx.get("source") == "submit":
                        new_deadline = now + (
                            REVIEW_DEADLINE_MS
                            if deadline_ms == _REVIEW_PARKED_DEADLINE_MS
                            else deadline_ms
                        )
                        new_owner = (
                            owner_agent_id
                            if owner_agent_id
                            else prev_owner
                        )
                        await _execute(
                            project_id,
                            "UPDATE obligations SET owner_agent_id = ?, "
                            "context_json = ?, deadline = ?, "
                            "escalation_count = 0, escalated_at = NULL, "
                            "escalated_to = NULL WHERE id = ?",
                            [
                                new_owner,
                                json.dumps(ctx),
                                new_deadline,
                                ob_id,
                            ],
                        )
                        if prev_owner and new_owner and prev_owner != str(new_owner):
                            log.info(
                                "obligation.review_owner_retargeted",
                                obligation_id=ob_id,
                                from_owner=prev_owner,
                                to_owner=new_owner,
                                task_id=task_id,
                            )
                        log.info(
                            "obligation.review_deadline_activated",
                            obligation_id=ob_id,
                            task_id=task_id,
                            deadline=new_deadline,
                        )
                    return ob_id
            else:
                existing = await _query(
                    project_id,
                    "SELECT id FROM obligations WHERE owner_agent_id = ? "
                    "AND obligation_type = ? AND task_id = ? AND status = 'pending' "
                    "LIMIT 1",
                    [owner_agent_id, obligation_type, task_id],
                )
                if existing:
                    return existing[0]["id"]

        ob_id = str(uuid.uuid4())
        await _execute(
            project_id,
            "INSERT INTO obligations "
            "(id, project_id, owner_agent_id, obligation_type, task_id, "
            " context_json, status, created_at, deadline, escalation_count) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, 0)",
            [
                ob_id, project_id, owner_agent_id, obligation_type,
                task_id, json.dumps(ctx),
                now, now + deadline_ms,
            ],
        )
        log.info(
            "obligation.created",
            project_id=project_id,
            obligation_id=ob_id,
            owner=owner_agent_id,
            type=obligation_type,
            task_id=task_id,
            deadline_ms=deadline_ms,
        )
        return ob_id

    async def fulfill(
        self,
        project_id: str,
        task_id: str,
        obligation_type: str,
        *,
        merge_commit: str | None = None,
    ) -> int:
        """Mark all pending obligations of this type+task as fulfilled.

        Returns count of obligations fulfilled.

        merge_commit（duty 增强第二部分）：merge 义务 fulfill 时传入本次
        merge 的 MAIN HEAD（git_worktree_merge 成功结果里的 ``hash``——
        平台唯一在握的 commit 来源），透传给依赖解封路径，让 [DEPENDENCY
        MET] 文案带上「基于新 HEAD 复核」。缺省 None 时回退扫描义务
        context_json 的 commit 字段（2026-09-05 取证：现行 merge 义务写入
        方都不落 commit，此扫描为前向兼容），仍取不到 → None → 现状文案。
        """
        raw_ref = (task_id or "").strip()
        task_id = await _normalize_task_id(project_id, task_id) or raw_ref
        now = int(time.time() * 1000)
        # Match canonical UUID and any legacy prefix rows (audit P1-6)
        id_candidates = [task_id]
        if raw_ref and raw_ref != task_id:
            id_candidates.append(raw_ref)
        placeholders = ",".join("?" * len(id_candidates))
        rows = await _query(
            project_id,
            f"SELECT id, context_json FROM obligations WHERE task_id IN ({placeholders}) "
            "AND obligation_type = ? AND status = 'pending'",
            [*id_candidates, obligation_type],
        )
        if not rows:
            log.warning(
                "obligation.fulfill_miss",
                project_id=project_id,
                task_id=task_id,
                raw_ref=raw_ref if raw_ref != task_id else None,
                type=obligation_type,
            )
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await _execute(
            project_id,
            f"UPDATE obligations SET status = 'fulfilled', fulfilled_at = ? "
            f"WHERE id IN ({placeholders})",
            [now] + ids,
        )
        log.info(
            "obligation.fulfilled",
            project_id=project_id,
            task_id=task_id,
            type=obligation_type,
            count=len(ids),
        )

        # TEST16 D1: immediately wake blocked tasks that depend on this task.
        # Don't wait for the 2-min reconcile tick — merge landed, QA can go.
        if obligation_type == "merge" and task_id:
            if not merge_commit:
                merge_commit = self._merge_commit_from_contexts(rows)
            await self._wake_dependent_tasks(
                project_id, task_id, merge_commit=merge_commit
            )

        return len(ids)

    @staticmethod
    def _merge_commit_from_contexts(rows: list) -> str | None:
        """Scan merge-obligation contexts for a commit field (fail-open → None).

        2026-09-05 取证：现行 merge 义务 context 只写 reason/source/branch/
        short_id/detail（misc_tools.py / close.py / reconcile.py / review.py），
        没有 commit —— 本扫描是前向兼容；真 commit 由 fulfill 调用方
        （git_worktree_merge 工具路径）以 merge_commit= 显式传入。

        行型兼容：obligation._query 返回 dict，但防御 aiosqlite.Row 直传
        （索引式取值优先，KeyError/TypeError 兜底 .get），杜绝行型漂移
        让扫描静默失效（审计 P1-A）。
        """
        for r in rows:
            try:
                raw = r["context_json"]
            except (KeyError, IndexError, TypeError):
                raw = r.get("context_json") if isinstance(r, dict) else None
            try:
                ctx = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(ctx, dict):
                continue
            for key in ("merge_commit", "commit", "commit_sha", "head"):
                val = str(ctx.get(key) or "").strip()
                if val:
                    return val
        return None

    async def fulfill_by_owner(
        self, project_id: str, owner_agent_id: str, obligation_type: str
    ) -> int:
        """Fulfill all pending obligations of a type for a given owner."""
        now = int(time.time() * 1000)
        rows = await _query(
            project_id,
            "SELECT id FROM obligations WHERE owner_agent_id = ? "
            "AND obligation_type = ? AND status = 'pending'",
            [owner_agent_id, obligation_type],
        )
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await _execute(
            project_id,
            f"UPDATE obligations SET status = 'fulfilled', fulfilled_at = ? "
            f"WHERE id IN ({placeholders})",
            [now] + ids,
        )
        return len(ids)

    async def scan_overdue(self, project_id: str) -> list[dict]:
        """Scan for overdue pending obligations. Returns list of overdue rows.

        Called by game tick. For each overdue obligation:
        - If escalation_count < MAX_ESCALATIONS and cooldown passed:
          escalate to org parent, send inbox notification.
        """
        now = int(time.time() * 1000)
        overdue = await _query(
            project_id,
            "SELECT * FROM obligations "
            "WHERE status = 'pending' AND deadline < ?",
            [now],
        )
        if not overdue:
            return []

        escalated = []
        for ob in overdue:
            # Check escalation cooldown
            last_esc = ob.get("escalated_at") or 0
            if now - last_esc < ESCALATION_INTERVAL_MS:
                continue
            if (ob.get("escalation_count") or 0) >= MAX_ESCALATIONS:
                continue

            # TEST18 P0-1: review obligations only escalate when the task is
            # actually awaiting review — never while running/claimed/created.
            # arbitration（2026-09-17）：判据独立 —— 只认「任务仍在 blocked」，
            # **不套用** _REVIEW_ESCALATABLE_STATUSES（那套是 review 专属的
            # parked 语义；套用会让 arbitration 在 blocked 态永不升级，
            # 正是本机制要修的那个洞）。
            task_status: str | None = None
            ob_type = ob.get("obligation_type")
            if ob_type in ("review", "arbitration") and ob.get("task_id"):
                task_status = await self._task_status(
                    project_id, str(ob["task_id"])
                )
                escalatable = (
                    _REVIEW_ESCALATABLE_STATUSES
                    if ob_type == "review"
                    else _ARBITRATION_ESCALATABLE_STATUSES
                )
                if task_status is None:
                    # Fail-open lookup miss — observable so ops can spot
                    # ledger/task drift; skip behavior unchanged.
                    # 事件名用**固定字面量**（不插值）：review 保持原字面量
                    # （既有测试 test_m7_h8_h4_sweep.py:226 依赖它），
                    # 其余类型用通用名，类型由 obligation_type 字段区分
                    # （OCR 评审 2026-09-17：插值事件名不利于按名检索）。
                    log.warning(
                        "obligation.review_escalate_task_missing"
                        if ob_type == "review"
                        else "obligation.escalate_task_missing",
                        obligation_id=ob["id"],
                        obligation_type=ob_type,
                        task_id=ob.get("task_id"),
                    )
                if task_status not in escalatable:
                    log.debug(
                        "obligation.review_escalate_skipped"
                        if ob_type == "review"
                        else "obligation.escalate_skipped",
                        obligation_id=ob["id"],
                        obligation_type=ob_type,
                        task_id=ob.get("task_id"),
                        task_status=task_status,
                    )
                    continue

            parent_id = await self._find_escalation_target(
                project_id, ob["owner_agent_id"]
            )
            if not parent_id:
                continue

            # Escalate: notify parent, update obligation
            esc_count = (ob.get("escalation_count") or 0) + 1
            await _execute(
                project_id,
                "UPDATE obligations SET escalated_to = ?, escalated_at = ?, "
                "escalation_count = ? WHERE id = ?",
                [parent_id, now, esc_count, ob["id"]],
            )

            # Send inbox notification to the escalation target
            await self._notify_escalation(
                project_id, ob, parent_id, esc_count, task_status=task_status
            )
            escalated.append(ob)
            log.warning(
                "obligation.escalated",
                project_id=project_id,
                obligation_id=ob["id"],
                owner=ob["owner_agent_id"],
                escalated_to=parent_id,
                escalation_count=esc_count,
                type=ob["obligation_type"],
                task_id=ob.get("task_id"),
                task_status=task_status,
            )

        return escalated

    async def get_pending_for_agent(
        self, project_id: str, agent_id: str
    ) -> list[dict]:
        """Get all pending obligations for an agent."""
        return await _query(
            project_id,
            "SELECT * FROM obligations WHERE owner_agent_id = ? "
            "AND status = 'pending' ORDER BY deadline ASC",
            [agent_id],
        )

    async def cancel_for_task(self, project_id: str, task_id: str) -> int:
        """Cancel all pending obligations for a task (e.g., task cancelled)."""
        raw_ref = (task_id or "").strip()
        task_id = await _normalize_task_id(project_id, task_id) or raw_ref
        rows = await _query(
            project_id,
            "SELECT id FROM obligations WHERE task_id = ? AND status = 'pending'",
            [task_id],
        )
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await _execute(
            project_id,
            f"UPDATE obligations SET status = 'cancelled' "
            f"WHERE id IN ({placeholders})",
            ids,
        )
        return len(ids)

    async def reconcile_closed_task(
        self, project_id: str, task_id: str
    ) -> int:
        """Fail-open: closed tasks must not leave pending obligations.

        TEST6 evening P1-1 backstop — prefix-miss or missed fulfill paths
        leave pending rows that keep escalating. Fulfill all pending for
        the task and warn.
        """
        raw_ref = (task_id or "").strip()
        task_id = await _normalize_task_id(project_id, task_id) or raw_ref
        if not task_id:
            return 0
        id_candidates = [task_id]
        if raw_ref and raw_ref != task_id:
            id_candidates.append(raw_ref)
        # Also match legacy 8-char prefix stored as task_id
        if len(task_id) >= 8:
            id_candidates.append(task_id[:8])
        # Dedupe while preserving order
        seen: set[str] = set()
        uniq: list[str] = []
        for c in id_candidates:
            if c and c not in seen:
                seen.add(c)
                uniq.append(c)
        placeholders = ",".join("?" * len(uniq))
        rows = await _query(
            project_id,
            f"SELECT id, obligation_type FROM obligations "
            f"WHERE task_id IN ({placeholders}) AND status = 'pending'",
            uniq,
        )
        if not rows:
            return 0
        now = int(time.time() * 1000)
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await _execute(
            project_id,
            f"UPDATE obligations SET status = 'fulfilled', fulfilled_at = ? "
            f"WHERE id IN ({placeholders})",
            [now] + ids,
        )
        types = sorted({str(r.get("obligation_type") or "") for r in rows})
        log.warning(
            "obligation.reconcile_closed_fulfilled",
            project_id=project_id,
            task_id=task_id,
            count=len(ids),
            types=types,
        )
        return len(ids)

    async def settle_arbitration_on_unblock(
        self, project_id: str, task_id: str
    ) -> int:
        """任务离开 blocked ⇒ 结清 pending 的 arbitration 义务（审计 P1）。

        2026-09-17（PLATFORM-ISSUES §11.6）：arbitration 义务的 escalatable
        判据是「任务仍在 blocked」—— 任务离开 blocked（裁决解封 / reconcile
        解封）后升级会被跳过，但行仍停在 pending。陈旧 pending 行（deadline
        早已过期、escalation_count 续用）会让任务**下一次** block 时**立即**
        升级（30min 宽限被旧账吞掉），甚至 MAX_ESCALATIONS 提前耗尽后彻底
        静默（审计实证）。故解封点必须结清。

        与 `reconcile_closed_task`（任务终态 → 结清全部类型）互补：本方法只
        清 ``arbitration`` —— blocked 期间 review/merge 等义务可能仍有效
        （parked review 义务不得被解封顺手清掉）。无 pending arbitration 行
        是**常态**（义务只在陈旧 blocked 上登记），故静默返回 0——
        不学 ``fulfill`` 打 fulfill_miss 警告（解封是高频路径）。
        """
        raw_ref = (task_id or "").strip()
        task_id = await _normalize_task_id(project_id, task_id) or raw_ref
        if not task_id:
            return 0
        id_candidates = [task_id]
        if raw_ref and raw_ref != task_id:
            id_candidates.append(raw_ref)
        if len(task_id) >= 8:
            id_candidates.append(task_id[:8])
        seen: set[str] = set()
        uniq: list[str] = []
        for c in id_candidates:
            if c and c not in seen:
                seen.add(c)
                uniq.append(c)
        placeholders = ",".join("?" * len(uniq))
        rows = await _query(
            project_id,
            f"SELECT id FROM obligations WHERE task_id IN ({placeholders}) "
            "AND obligation_type = 'arbitration' AND status = 'pending'",
            uniq,
        )
        if not rows:
            return 0
        now = int(time.time() * 1000)
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await _execute(
            project_id,
            f"UPDATE obligations SET status = 'fulfilled', fulfilled_at = ? "
            f"WHERE id IN ({placeholders})",
            [now] + ids,
        )
        log.info(
            "obligation.arbitration_settled",
            project_id=project_id,
            task_id=task_id,
            count=len(ids),
        )
        return len(ids)

    async def audit_missing_review_obligations(
        self, project_id: str, *, limit: int = 40
    ) -> list[str]:
        """Backfill review obligations for open submitted/reviewing tasks.

        TEST6 S11: status≠closed tasks in the review pipe should have a
        pending review obligation. Creates missing ones (fail-open).
        Returns task ids that were backfilled.
        """
        rows = await _query(
            project_id,
            "SELECT id, creator_id, reviewer_id, assignee_id, status "
            "FROM tasks WHERE is_archived = 0 "
            "AND status IN ('submitted', 'reviewing') "
            "ORDER BY updated_at DESC LIMIT ?",
            [max(1, int(limit))],
        )
        fixed: list[str] = []
        for row in rows or []:
            tid = str(row.get("id") or "")
            if not tid:
                continue
            existing = await _query(
                project_id,
                "SELECT id FROM obligations WHERE task_id = ? "
                "AND obligation_type = 'review' AND status = 'pending' "
                "LIMIT 1",
                [tid],
            )
            if existing:
                continue
            owner = (
                row.get("reviewer_id")
                or row.get("creator_id")
                or row.get("assignee_id")
            )
            if not owner:
                continue
            try:
                await self.create(
                    project_id,
                    str(owner),
                    "review",
                    task_id=tid,
                    context={"source": "audit_backfill"},
                )
                fixed.append(tid)
            except Exception as e:
                log.warning(
                    "obligation.audit_backfill_failed",
                    task_id=tid,
                    error=str(e),
                )
        return fixed

    async def audit_missing_arbitration_obligations(
        self, project_id: str, *, limit: int = 40
    ) -> list[str]:
        """Backfill escalation obligations for stale「等人裁决」blocked tasks.

        2026-09-17（PLATFORM-ISSUES §11.6）：`reconcile_blocked_tasks` 只覆盖
        「等依赖 / 等定时器」两类出口。**第三类成因「等人裁决」没有出口** ——
        没有任何机制把它推回裁决者面前（`[BLOCKED STALE]` / `[BLOCKED ESCALATION]`
        inbox 已在 `game_time.py` 明写禁用）。本方法给这类 blocked 登记一条
        `arbitration` 义务，由 `scan_overdue` 统一升级到 org parent。

        ⚠ **只登记、不推进状态**：解封仍由 `reconcile_blocked_tasks`（deps/timer）
        或**人的裁决动作**完成 ⇒ 不产生第二解封路径、不与既有 timer 出口打架。
        ⚠ 判据在 `blocked_task_needs_arbitration_escalation`（**纯状态判据**，
        唯一登记点；禁止文案）。
        ⚠ 幂等：同 task 已有 pending `arbitration` 义务则跳过。
        """
        from hiveweave.services.tasks.lifecycle import (
            blocked_task_needs_arbitration_escalation,
        )

        rows = await _query(
            project_id,
            "SELECT id, status, is_archived, claimed_at, assignee_id, creator_id, "
            "wait_kind, wake_at, depends_on, updated_at FROM tasks "
            "WHERE status = 'blocked' AND is_archived = 0 "
            "ORDER BY updated_at ASC LIMIT ?",
            [max(1, int(limit))],
        )
        created: list[str] = []
        for row in rows or []:
            tid = str(row.get("id") or "")
            if not tid:
                continue
            if not blocked_task_needs_arbitration_escalation(row):
                continue
            existing = await _query(
                project_id,
                "SELECT id FROM obligations WHERE task_id = ? "
                "AND obligation_type = 'arbitration' AND status = 'pending' "
                "LIMIT 1",
                [tid],
            )
            if existing:
                continue
            # 升级对象 = creator（派单方）；无 creator 的已在判据里排除。
            owner = str(row.get("creator_id") or "")
            if not owner:
                continue
            try:
                await self.create(
                    project_id,
                    owner,
                    "arbitration",
                    task_id=tid,
                    context={
                        "source": "arbitration_backfill",
                        "assignee_id": row.get("assignee_id"),
                        "wait_kind": row.get("wait_kind"),
                    },
                )
                created.append(tid)
                log.warning(
                    "obligation.arbitration_registered",
                    project_id=project_id,
                    task_id=tid,
                    owner=owner,
                    wait_kind=row.get("wait_kind"),
                )
            except Exception as e:
                log.warning(
                    "obligation.arbitration_backfill_failed",
                    task_id=tid,
                    error=str(e),
                )
        return created

    # ── Internal helpers ─────────────────────────────────────

    async def _wake_dependent_tasks(
        self,
        project_id: str,
        fulfilled_task_id: str,
        merge_commit: str | None = None,
    ) -> None:
        """TEST16 D1: wake blocked tasks that depend on a fulfilled merge.

        Finds tasks in 'blocked' state with fulfilled_task_id in their
        depends_on list, unblocks them, and triggers their assignee.

        duty 增强第二部分：唤醒时补发 [DEPENDENCY MET]（此前此路径只
        trigger 不落 inbox，QA 被唤醒却不知道 HEAD 已变）——merge_commit
        非空时文案带 MAIN HEAD 复核语义；并给 creator 发 wake=False FYI
        （同 commit 幂等）。
        """
        try:
            rows = await _query(
                project_id,
                "SELECT id, assignee_id, creator_id, title, depends_on "
                "FROM tasks WHERE status = 'blocked' AND is_archived = 0",
            )
            if not rows:
                return

            from hiveweave.services.task import TaskService

            ts = TaskService()
            woken = 0
            for row in rows:
                deps = row.get("depends_on") or "[]"
                if isinstance(deps, str):
                    try:
                        deps = json.loads(deps)
                    except (json.JSONDecodeError, TypeError):
                        deps = []
                if not isinstance(deps, list):
                    deps = []
                if fulfilled_task_id not in deps:
                    continue
                # Check ALL deps are satisfied (not just this one)
                all_met = await self._all_deps_met(project_id, deps)
                if not all_met:
                    continue
                # Unblock and trigger
                # Timeline v4 §4.6: 走 _transition 而非裸 UPDATE ——
                # blocked→running 在 _TRANSITIONS 合法，_transition 顺带
                # 清 blocked_reason/wait_kind/wake_at 并写 task_events。
                tid = row["id"]
                try:
                    await ts._transition(
                        project_id,
                        tid,
                        "running",
                        reason_code="dependency_fulfilled",
                        detail=f"deps fulfilled by {fulfilled_task_id[:8]}",
                    )
                except Exception as e:
                    # 并发漂移可能使该任务状态已变（IllegalTransition 等）：
                    # 单任务失败不得中断整批唤醒（原裸 UPDATE 无此风险）。
                    log.warning(
                        "obligation.merge_dependent_wake_failed",
                        project_id=project_id,
                        task_id=tid[:12],
                        error=str(e),
                    )
                    continue
                assignee = row.get("assignee_id")
                if assignee:
                    try:
                        from hiveweave.agents.trigger import trigger_subordinate
                        from hiveweave.services.inbox import InboxService
                        from hiveweave.services.tasks.lifecycle import (
                            _dependency_met_message,
                        )

                        await InboxService().send_message(
                            "system",
                            assignee,
                            _dependency_met_message(
                                fulfilled_task_id,
                                (row.get("title") or "")[:80],
                                merge_commit,
                            ),
                            message_type="system",
                            priority="urgent",
                            task_id=tid,
                            # 显式同键（审计 D）：与 lifecycle 实现共用，
                            # 文案漂移不再靠内容哈希巧合防双发
                            idempotency_key=(
                                f"dep-met:{tid}:{fulfilled_task_id}:"
                                f"{str(merge_commit or '')[:12]}"
                            ),
                        )
                        await trigger_subordinate(assignee)
                    except Exception:
                        pass
                # duty 增强第二部分：creator FYI（wake=False，同 commit 幂等）
                try:
                    from hiveweave.services.tasks.lifecycle import (
                        _notify_creator_deps_merged,
                    )

                    await _notify_creator_deps_merged(
                        project_id,
                        {
                            "id": tid,
                            "title": row.get("title"),
                            "creator_id": row.get("creator_id"),
                            "assignee_id": assignee,
                        },
                        merge_commit,
                    )
                except Exception:
                    pass
                woken += 1
                log.info(
                    "obligation.merge_dependent_woken",
                    project_id=project_id,
                    task_id=tid,
                    fulfilled_task=fulfilled_task_id,
                    assignee=assignee,
                )
            if woken:
                log.info(
                    "obligation.merge_wake_summary",
                    project_id=project_id,
                    fulfilled_task=fulfilled_task_id,
                    woken=woken,
                )
        except Exception as e:
            log.warning(
                "obligation.wake_dependent_failed",
                project_id=project_id,
                task_id=fulfilled_task_id,
                error=str(e),
            )

    async def _all_deps_met(
        self, project_id: str, deps: list[str]
    ) -> bool:
        """Check if all dependency task IDs are in a completed state."""
        if not deps:
            return True
        placeholders = ",".join("?" * len(deps))
        rows = await _query(
            project_id,
            f"SELECT id FROM tasks WHERE id IN ({placeholders}) "
            "AND status IN ('approved', 'verifying', 'closed') "
            "AND is_archived = 0",
            deps,
        )
        return len(rows) >= len(deps)

    async def _task_status(
        self, project_id: str, task_id: str
    ) -> str | None:
        """Lookup task status for escalate gating (fail-open → None)."""
        try:
            rows = await _query(
                project_id,
                "SELECT status FROM tasks WHERE id = ? LIMIT 1",
                [task_id],
            )
            if rows:
                return str(rows[0].get("status") or "") or None
            # Prefix fallback for legacy short refs
            if len(task_id) >= 8:
                rows = await _query(
                    project_id,
                    "SELECT status FROM tasks WHERE id LIKE ? LIMIT 1",
                    [task_id[:8] + "%"],
                )
                if rows:
                    return str(rows[0].get("status") or "") or None
        except Exception as e:
            log.warning(
                "obligation.task_status_lookup_failed",
                task_id=task_id,
                error=str(e),
            )
        return None

    async def _find_escalation_target(
        self, project_id: str, owner_agent_id: str
    ) -> str | None:
        """Find the org parent to escalate to."""
        from hiveweave.services.org import OrgService

        org = OrgService()
        agent = await org.get_agent(owner_agent_id)
        if not agent:
            return None
        parent_id = agent.get("parent_id")
        if not parent_id:
            return None
        # Verify parent is active
        parent = await org.get_agent(parent_id)
        if not parent or parent.get("is_archived"):
            return None
        return parent_id

    async def _notify_escalation(
        self,
        project_id: str,
        ob: dict,
        parent_id: str,
        esc_count: int,
        *,
        task_status: str | None = None,
    ) -> None:
        """Send inbox notification about an escalated obligation."""
        from hiveweave.services.inbox import InboxService

        ob_type = ob.get("obligation_type", "unknown")
        task_id = ob.get("task_id", "?")
        owner = ob.get("owner_agent_id", "?")
        status_note = f" status={task_status}" if task_status else ""

        msg = (
            f"[OBLIGATION ESCALATION #{esc_count}] "
            f"Agent {owner[:8]} has an overdue {ob_type} obligation "
            f"(task {task_id[:8] if task_id else '?'}{status_note}). "
            f"Deadline passed. Please intervene: "
        )
        if ob_type == "merge":
            msg += "run git_worktree_merge on the assignee's worktree, or reassign the merge duty."
        elif ob_type == "arbitration":
            # 2026-09-17：第三类出口（等人裁决）。文案必须给**正确下一步**，
            # 且不声称任务已解封（本机制只升级、不推进状态）。
            # ⚠ 不说 "wait path never fired"：user/external 本就无自动出口
            # （审计 P5）；status 已在公共前缀里给过，此处不重复。
            msg += (
                "this task is blocked waiting on a decision and nothing "
                "has auto-unblocked it. Decide it: unblock it "
                "(update_task_status status=running) once the decision is "
                "made, or reassign / cancel it."
            )
        elif ob_type == "review":
            # TEST18 P0-1: never claim "submitted" unless status confirms it
            if task_status in _REVIEW_ESCALATABLE_STATUSES:
                msg += (
                    f"review the {task_status} task or reassign the reviewer."
                )
            else:
                msg += (
                    f"task status is {task_status or 'unknown'} — "
                    f"confirm it is awaiting review, or reassign."
                )
        else:
            msg += "ensure the obligation is fulfilled or reassign."

        try:
            inbox = InboxService()
            await inbox.send_message(
                from_agent_id="system",
                to_agent_id=parent_id,
                message=msg,
                message_type="escalation",
                priority="urgent",
                task_id=task_id,
                wake=True,
                idempotency_key=f"ob-esc-{ob['id']}-{esc_count}",
            )
        except Exception as e:
            log.warning(
                "obligation.escalation_notify_failed",
                obligation_id=ob["id"],
                error=str(e),
            )
