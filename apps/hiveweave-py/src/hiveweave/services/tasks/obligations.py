"""Actionable obligation queries."""
from __future__ import annotations

import json
import os
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

# T28（D79 木棉）：中层自派任务「零进展」判定阈值 —— 任务行 updated_at
# 无任何写触碰（progress 无变化、状态未迁移）持续超过它，即视为长时间
# 零进展。update_progress / 各状态迁移统一盖 updated_at（crud.py /
# progress.py），故它是「零进展写」的现成代理，无需新增历史表。
# env 可调，默认 30 分钟。
DORMANT_OWN_TASK_STALL_MS = int(
    os.environ.get("HIVEWEAVE_DORMANT_OWN_TASK_STALL_MS", "1800000")
    or "1800000"
)


def _row_dict(r: Any) -> dict:
    """aiosqlite.Row → dict（本包 ``_query`` 返回 Row，无 ``.get``）。

    TEST_DSH_77 的休眠形态判定要查 ``agents`` 表 —— 那不是 tasks
    ``_COLUMNS`` 域，不能走 ``self._row``；这里按 Row.keys() 通用转换。
    """
    try:
        return {k: r[k] for k in r.keys()}
    except Exception:
        return dict(r) if not isinstance(r, dict) else r


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

        = get_open_work_obligations 非空 或 中层休眠形态（TEST_DSH_77：
        ``has_dormant_idle_team``，账外债）或 有未清 ask 契约，
        且未被未过期 wait 契约冻结（wait = 合法停泊，负向豁免）。

        fail-closed：内部查询异常按 True（有活）处理——宁可多醒，
        不可漏醒（与 game_time complete 豁免 fail-closed 同语义）；
        **例外**：休眠形态判定 fail-open（增量启发式，防风暴论证见调用侧）。
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
        # TEST_DSH_77：账面义务空 ≠ 无活 —— 中层休眠形态（coordinator 且
        # CEO 直属 + active executor 下属全员零被派任务 + 自己零未结任务 +
        # 无 active run）在台账上不可见，却正是延续唤醒链的债：中层完成
        # 设计定稿 + 招齐人后不再被任何义务点亮，force 唤醒链在「少接一层」
        # 处蒸发（ADR-001）。故此处必须判有活。
        # fail-open（异常按不适用）：本判定是**增量启发式**，若 fail-closed
        # 会让 DB 抖动时所有 idle agent（含全部 executor）被判「有活」，
        # 看门狗对全员按有人待办档反复唤醒 —— 唤醒风暴。形态误报的代价
        # （多醒一次中层）远小于漏报（中层永久睡死），而查询失效的代价
        # 被有意导向「维持现状」而非「全员唤醒」。
        try:
            if await self.has_dormant_idle_team(
                project_id, agent_id, obligations=[]
            ):
                return True
            # ↑ obligations=[] 是实参契约：走到这里时账面义务已判空，
            # has_dormant_idle_team 内部只走「零义务经典形态」；T28 的
            # 「自派任务零进展」放宽分支在此构造性不可达（有自派任务时
            # 上方 get_open_work_obligations 已非空、早已 return True）。
        except Exception as e:
            log.debug(
                "has_open_work_dormant_lead_check_failed",
                project_id=project_id, agent_id=agent_id, error=str(e),
            )
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

    async def dormant_stuck_own_task(
        self,
        project_id: str,
        agent_id: str,
        *,
        obligations: list[dict] | None = None,
    ) -> dict | None:
        """T28：自派任务「长时间零进展」的代表行（不卡死 ⇒ None）。

        全部满足才算卡死（窄口径防误报）：

        - 名下未结任务**全部** ``role_hint == 'assignee'``——有 reviewer /
          creator 义务 = 有真实可行动作（待审 / 待合并不是休眠，不适用）；
        - 每一条 ``now - updated_at ≥ DORMANT_OWN_TASK_STALL_MS``
          （env ``HIVEWEAVE_DORMANT_OWN_TASK_STALL_MS``，默认 30 分钟）。
          updated_at 由 progress 上调 / 状态迁移统一盖章 ⇒「零写 = 零进展」；
          正常执行中的任务 progress 在涨、updated_at 新鲜 ⇒ 不命中。
          「run 活跃」由 ``has_dormant_idle_team`` 条件 5 单独把关，本方法
          不重复判。

        Returns 代表行 ``{"task_id", "title", "stall_ms", "stall_min"}``
        （updated_at 最新的一条 —— obligations 按 updated_at DESC 排序，
        取首条即可），供 notice/上下文文案展示；调用方只需真伪时以
        ``is None`` 判定。只读。
        """
        if obligations is None:
            obligations = await self.get_open_work_obligations(
                project_id, agent_id
            )
        if not obligations:
            return None  # 零义务是经典形态（条件 3 原样），不是「自派卡死」
        now_ms = int(time.time() * 1000)
        rep: dict | None = None
        for o in obligations:
            if (o.get("role_hint") or "") != "assignee":
                return None
            try:
                stall_ms = now_ms - int(o.get("updated_at") or 0)
            except (TypeError, ValueError):
                return None
            if stall_ms < DORMANT_OWN_TASK_STALL_MS:
                return None  # 有任务仍在推进（防误报：progress 在涨）
            if rep is None:
                rep = {
                    "task_id": str(o.get("id") or ""),
                    "title": str(o.get("title") or "")[:80],
                    "stall_ms": stall_ms,
                    "stall_min": max(1, stall_ms // 60000),
                }
        return rep

    async def has_dormant_idle_team(
        self,
        project_id: str,
        agent_id: str,
        *,
        obligations: list[dict] | None = None,
    ) -> bool:
        """TEST_DSH_77 中层休眠形态：下属全员闲置而自己零派活 ⇒ 有活。

        五条件**全部**满足才判 True（窄口径，防误报/唤醒风暴）：

        1. 中层形态：``infer_role_family`` 为 coordinator 且 org parent 为
           CEO（CEO 直属 = 中层；coordinator 套 coordinator 不适用）；
        2. 有 ≥1 名 status='active' 的 direct executor 下属
           （infer_role_family == executor；QA/HR 子级不计）；
        3. 自己名下 zero 个未结任务（``get_open_work_obligations`` 空；
           调用方已算过可经 ``obligations=`` 传入免重查）。
           **T28 放宽（D79 木棉）**：名下有未结任务时不再直接短路——若
           该任务把骨架派给自己（claimed 永久占位）且**长时间零进展**
           （``dormant_stuck_own_task`` 命中，env 可调默认 30 分钟），
           其余条件仍满足 ⇒ 仍判 dormant。否则维持不适用（正常执行中）；
        4. 这些下属 zero 个被派任务 —— assignee 侧**任何非终态**行都算
           已派（含 created 未认领：已派未领 ≠ 没派，处方已履行）；
        5. 自己当前无 active run（processing 中不判定 —— 唤醒侧本就
           skip processing，这里判 False 防止语义打架）。

        查询整体 fail-open（异常按 False）：见 ``has_open_work`` 调用侧的
        防风暴论证 —— 启发式失效不得把全体 idle 判成有活。

        wait 停泊不进本方法：``has_open_work`` 的 wait 负项在更早处直接
        返回 False（合法停泊优先于一切唤醒理由），本方法无须重复判断。
        """
        try:
            if obligations is None:
                obligations = await self.get_open_work_obligations(
                    project_id, agent_id
                )
            if obligations:
                # T28：条件 3 放宽 —— 自派任务占位不再结构性掐死 [DORMANT
                # TEAM]（D79：反刍 3h 无人升级、8 executor 全闲置）。名下
                # 任务**全部**长时间零进展才继续走下方形态条件；否则维持
                # 不适用（正常执行中 / 有真实 reviewer-creator 义务）。
                stuck_own = await self.dormant_stuck_own_task(
                    project_id, agent_id, obligations=obligations
                )
                if stuck_own is None:
                    return False  # 条件 3 不满足：名下任务并非全部零进展
            rows = await _query(
                project_id,
                "SELECT id, role, permission_type, parent_id, status "
                "FROM agents WHERE id = ? LIMIT 1",
                [agent_id],
            )
            if not rows:
                return False
            agent = _row_dict(rows[0])
            if str(agent.get("status") or "") != "active":
                return False
            from hiveweave.services.policy import infer_role_family

            if infer_role_family(agent) != "coordinator":
                return False  # 条件 1：executor/QA/HR/CEO 不适用
            parent_id = str(agent.get("parent_id") or "")
            if not parent_id:
                return False
            parent_rows = await _query(
                project_id,
                "SELECT id, role, permission_type FROM agents "
                "WHERE id = ? LIMIT 1",
                [parent_id],
            )
            if not parent_rows or infer_role_family(
                _row_dict(parent_rows[0])
            ) != "ceo":
                return False  # 条件 1：非 CEO 直属（深层 coordinator）不适用
            kids = await _query(
                project_id,
                "SELECT id, role, permission_type, status FROM agents "
                "WHERE parent_id = ?",
                [agent_id],
            )
            exec_ids = [
                str(k["id"])
                for k in (_row_dict(x) for x in kids)
                if str(k.get("status") or "") == "active"
                and infer_role_family(k) == "executor"
            ]
            if not exec_ids:
                return False  # 条件 2：无 active executor 下属
            # 条件 4：下属零被派任务（任何非终态 assignee 行都算已派）
            kid_ph = ",".join("?" for _ in exec_ids)
            neg = ",".join("?" for _ in TERMINAL_STATUSES)
            cnt_rows = await _query(
                project_id,
                f"SELECT COUNT(*) AS c FROM tasks WHERE is_archived = 0 "
                f"AND assignee_id IN ({kid_ph}) AND status NOT IN ({neg})",
                [*exec_ids, *TERMINAL_STATUSES],
            )
            cnt = 0
            if cnt_rows:
                try:
                    cnt = int(cnt_rows[0]["c"] or 0)
                except (KeyError, IndexError, TypeError, ValueError):
                    cnt = 1  # 行型漂移：按「已派」处理（不适用，防误唤醒）
            if cnt > 0:
                return False
            # 条件 5：自己当前无 active run
            try:
                from hiveweave.agents.supervisor import agent_manager

                for run_aid, _run_pid in agent_manager.list_processing():
                    if run_aid == agent_id:
                        return False
            except Exception as e:
                # 管理器不可用 fail-open：不否定判定（唤醒侧本就 skip
                # processing，此处漏判不产生错误唤醒）。
                log.debug(
                    "dormant_idle_team_run_check_failed",
                    project_id=project_id, agent_id=agent_id, error=str(e),
                )
            return True
        except Exception as e:
            log.warning(
                "has_dormant_idle_team_failed",
                project_id=project_id, agent_id=agent_id, error=str(e),
            )
            return False  # fail-open（防风暴论证见 has_open_work 调用侧）

    async def has_pending_upstream_recovery(
        self, project_id: str, agent_id: str
    ) -> bool:
        """上游死亡 durable 重醒未到期 → True（dwell/义务时钟应暂停）。

        TEST_DSH_63 批3 组4：agent 死于上游错误后、自动重醒到期前，它不是
        「僵死」而是「平台已排定的合法等待」——dwell 不累计、auto-submit /
        VERIFY 改派不触发；唤醒触发后等待行被 clear_expired 清掉，本谓词
        自然回到 False（时钟恢复，无需任何解除动作）。

        ⚠ 跳过判据的**真正消费点**不在本文件：dwell 时钟住在
        ``game_time._nudge_stale_ledger``，其 ``live_wait_agents``（来自
        ``wait_contract_service.list_all_active``）已把任何 active wait 的
        agent 排除出 stall 计数 / auto-submit / VERIFY 改派 —— upstream
        recovery 等待行因此**天然**被覆盖，无需改 game_time。本谓词是该
        子集的具名读法（wait_contract.has_active_upstream_recovery 的转发），
        供观测、定向豁免点与测试复用；fail-open False（查询失败按无重醒，
        不改变任何既有跳过语义）。
        """
        try:
            from hiveweave.services.wait_contract import (
                has_active_upstream_recovery,
            )

            return await has_active_upstream_recovery(project_id, agent_id)
        except Exception as e:  # noqa: BLE001 — fail-open：不影响既有判定
            log.debug(
                "has_pending_upstream_recovery_failed",
                project_id=project_id,
                agent_id=agent_id,
                error=str(e),
            )
            return False

    async def can_idle(self, project_id: str, agent_id: str) -> bool:
        """F6 本体：**「许可收尾 / 许可不唤醒」的唯一判定源**。

        = ``not has_open_work(...)``。为什么必须存在一个具名的许可谓词：
        F6 的根因不是"没有正确判据"，而是**判据没被工程化管住** ——
        两个消费点（``poll._build_obligations_snapshot`` /
        ``turn_exit.build_exit_contract_hint``）各自拿**白名单**
        ``get_actionable_obligations`` 判空并把空结果当"许可收尾"输出，
        账本因此对 agent 说谎（砺石五次被告知「名下无待办」）。
        ADR-001 已把另外 4 个消费者切到闭式谓词、漏了这 2 个 ——
        「每处各判一次」正是漏网形态；本方法是那"唯一一次"。

        不可删答辩（设计稿 §4.1.3）：删掉它 = 回到"每个消费点自己挑
        一个谓词"的世界 —— 白名单对 agent 不可见地长大（未来新增
        ACTIVE 状态默认不进白名单），而闭式负空间自动把新状态算作"有活"
        （fail-safe）。许可语义必须锚在一个**闭式**谓词上，且名字要
        把"许可"说出口，守卫（``scripts/verify_commit_license.py``）
        才有可判定的登记点。

        fail-closed：``has_open_work`` 查询异常按 True（不许 idle）。
        """
        return not await self.has_open_work(project_id, agent_id)

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

