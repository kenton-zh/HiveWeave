"""Actionable obligation queries."""
from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from .constants import TERMINAL_STATUSES
from .db import _conn, _ensure_schema, _execute, _execute_tx, _query

log = structlog.get_logger(__name__)

# ADR-001 §1：assignee 负空间 = 无义务状态（未认领 / review 窗口）。
# 不含终态——终态由 TERMINAL_STATUSES 单独收紧（R1：两侧各一个改动点）。
_ASSIGNEE_NO_DUTY_STATUSES = frozenset(
    {"created", "submitted", "reviewing", "approved"}
)


class ObligationsMixin:
    """get_actionable_obligations."""

    if TYPE_CHECKING:
        promote_assigned_created: Any
        _COLUMNS: Any
        _row: Any
        _is_verify_task: Any

    async def get_actionable_obligations(
        self, project_id: str, agent_id: str, *, promote: bool = True
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

        ``promote=False``：跳过 promote_assigned_created 自愈写（只读口径，
        供看门狗探针等观测热路径复用，避免与业务写入竞争写锁）。
        """
        await _ensure_schema(project_id)
        # Heal legacy assign-without-claim rows so obligations stay consistent
        if promote:
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

    async def get_open_work_obligations(
        self, project_id: str, agent_id: str
    ) -> list[dict]:
        """ADR-001 §1：agent 名下的"开放工作"清单（闭式单一判定源）。

        与 ``get_actionable_obligations`` 的分工（R4，两个谓词两种用途）：
        - 本方法（闭式）→ **idle/skip 判定源**（turn-exit 完成闸、
          complete 跳过、silent watchdog 豁免）。assignee 侧用
          claimed_at 锚点 + 负空间：从 claim 起、到终结前**任何**状态
          都算（含 blocked / rework / 未来新增 ACTIVE 状态——新状态
          默认落入"有活"一侧，fail-safe）。
        - ``get_actionable_obligations``（白名单）→ **醒后行动清单**
          （trigger 提示文案、reminder hint）。排 blocked 等不可行动态。

        口径（ADR-001 §1 公式）：
        - assignee（闭式）：is_archived=0 且 claimed_at IS NOT NULL 且
          status NOT IN (TERMINAL_STATUSES ∪ {created, submitted,
          reviewing, approved})；verifying 仅 VERIFY 任务算（非 VERIFY
          的 verifying 等 merged 结转，assignee 无活）。
          出生即 blocked 的 dependency 任务 claimed_at=NULL，不入本表
          （由依赖解除 / dwell 时钟兜底）。
        - reviewer（状态敏感）：submitted | reviewing。
        - creator（状态敏感）：submitted | reviewing | approved 非 VERIFY；
          designated reviewer ≠ creator 时 review 窗口归 reviewer。
        - ask / wait 不在本方法内（``has_open_work`` 组合）。
        只读（无 promote 自愈写），供判定热路径使用。
        """
        await _ensure_schema(project_id)
        neg = TERMINAL_STATUSES | _ASSIGNEE_NO_DUTY_STATUSES
        placeholders = ",".join("?" for _ in neg)
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0 AND ("
            # assignee 闭式：claimed_at 锚点 + 负空间（R1）
            f"  (assignee_id = ? AND claimed_at IS NOT NULL"
            f"   AND status NOT IN ({placeholders}))"
            "  OR (reviewer_id = ? AND status IN ('submitted','reviewing'))"
            "  OR (creator_id = ? AND status IN "
            "      ('submitted','reviewing','approved'))"
            ") ORDER BY updated_at DESC",
            [agent_id, *neg, agent_id, agent_id],
        )
        out: list[dict] = []
        for r in rows:
            d = self._row(r)
            status = d.get("status")
            if d.get("assignee_id") == agent_id and status not in (
                neg
            ):
                # 非 VERIFY 任务的 verifying：等 merged 结转，无 assignee 活
                if status == "verifying" and not self._is_verify_task(d):
                    continue
                d["role_hint"] = "assignee"
            elif d.get("reviewer_id") == agent_id and status in (
                "submitted", "reviewing",
            ):
                d["role_hint"] = "reviewer"
            else:
                # creator 窗口；VERIFY approved 由 VERIFY 收口，非 creator 债
                if status == "approved" and self._is_verify_task(d):
                    continue
                # designated reviewer ≠ creator 持有 review 窗口
                if status in ("submitted", "reviewing"):
                    rid = d.get("reviewer_id")
                    if rid and rid != agent_id:
                        continue
                d["role_hint"] = "creator"
            out.append(d)
        return out

    async def has_open_work(self, project_id: str, agent_id: str) -> bool:
        """ADR-001 铁律判定：该 agent 名下是否有开放工作（唯一 idle 判定源）。

        = get_open_work_obligations 非空 或 有未清 ask 契约，
        且未被未过期 wait 契约冻结（wait = 合法停泊，负向豁免）。

        fail-closed：内部查询异常按 True（有活）处理——宁可多醒，
        不可漏醒（与 game_time complete 豁免 fail-closed 同语义）。
        """
        try:
            # wait 负项：未过期 wait 契约冻结全部义务（合法停泊）。
            # 查询失败不豁免（继续查义务）。
            from hiveweave.services.wait_contract import wait_contract_service

            now_ms = int(time.time() * 1000)
            for w in await wait_contract_service.list_all_active(
                project_id
            ) or []:
                if (w.get("agentId") or "") != agent_id:
                    continue
                exp = w.get("expiresAt")
                if exp is None or int(exp) > now_ms:
                    return False
        except Exception as e:
            log.debug(
                "has_open_work_wait_check_failed",
                project_id=project_id, agent_id=agent_id, error=str(e),
            )
        try:
            if await self.get_open_work_obligations(project_id, agent_id):
                return True
        except Exception as e:
            log.warning(
                "has_open_work_obligations_failed",
                project_id=project_id, agent_id=agent_id, error=str(e),
            )
            return True  # fail-closed
        try:
            from hiveweave.services.inbox import InboxService

            senders = await InboxService().get_outstanding_ask_senders(
                agent_id
            )
            return bool(senders)
        except Exception as e:
            log.warning(
                "has_open_work_ask_check_failed",
                project_id=project_id, agent_id=agent_id, error=str(e),
            )
            return True  # fail-closed

    async def list_delegated_in_flight(
        self, project_id: str, agent_id: str
    ) -> list[dict]:
        """Open tasks this agent created and assigned to someone else.

        Used so ``commit_turn(waiting, kind=task, ref=child)`` parks the
        creator's still-claimed parent (ASSIGNEE_MUST_SUBMIT).
        """
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks WHERE is_archived = 0 "
            "AND creator_id = ? AND assignee_id IS NOT NULL "
            "AND assignee_id != ? "
            "AND status IN ('claimed', 'running', 'rework', 'blocked') "
            "ORDER BY updated_at DESC LIMIT 40",
            [agent_id, agent_id],
        )
        out: list[dict] = []
        for r in rows:
            d = self._row(r)
            out.append(
                {
                    "id": d.get("id"),
                    "parent_task_id": d.get("parent_task_id"),
                    "assignee_id": d.get("assignee_id"),
                    "creator_id": d.get("creator_id"),
                    "status": d.get("status"),
                }
            )
        return out

