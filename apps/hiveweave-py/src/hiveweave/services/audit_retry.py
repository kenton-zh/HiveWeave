"""Audit retry queue（审计 epic P1-4，42 轮双项目报告）.

背景：审计 LLM 的 llm_failed 全部是分钟级上游暂态（42 轮实测 16 次），
旧文案教 agent「反复失败就请 coordinator waive」，直接导致 16 次 llm_failed
→ 全部走 waive、6 次级联豁免（真实质量问题被盖戳放行）。

本模块提供 per-project 的持久化重试队列：
  - ``enqueue_failed_audit``：run_code_audit llm_failed 时调用。同
    agent + diff_hash 已有 pending 行则 attempts+1、指数退避
    （2^n * 60s ± 20% jitter）；attempts 耗尽（MAX_ATTEMPTS）→
    status=exhausted 并通知 agent 可考虑 waive（真实人工决策）。
  - ``AuditRetryLoop``：后台单例循环（照 health_supervisor 的跨项目单例
    模式，main.py lifespan 启停成对注册），60s 扫到期 pending 行：
      * 重跑前重算 diff_hash——变了 → status=discarded + 通知「diff 已
        变化，队列审计作废，请重新请求审计」；
      * 重跑 run_code_audit（生产回调 Agent._oneshot_llm；agent 实例不在
        时走同 HTTP 路径的 adhoc callback）——成功 → status=done + 通知
        「可继续提交」；
      * 失败 → run_code_audit 内部 enqueue 命中同一 pending 行
        attempts+1 退避；非 llm_failed 失败由本循环兜底推进退避。
  - 表在 per-project DB，平台重启后 pending 行天然可继续被扫到。

软失败契约：本模块任何函数绝不向调用方 raise（审计门禁是软门）。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import (
    ProjectDbError,
    execute_by_project,
    get_project_db_by_project_id,
)

log = structlog.get_logger(__name__)

# 扫描间隔（秒）——照 health_supervisor.CHECK_INTERVAL_S
RETRY_INTERVAL_S = 60
# 单轮最多处理的到期行数（防止一项目风暴拖垮整轮扫描）
_SCAN_BATCH = 10
# attempts 上限：达到即 exhausted（不再自动重试，通知可走 waive）
MAX_ATTEMPTS = 5
# 退避基数：第 n 次失败后等 2^n * 60s ± 20% jitter
BASE_BACKOFF_S = 60
JITTER_RATIO = 0.2

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_DISCARDED = "discarded"
STATUS_EXHAUSTED = "exhausted"

CREATE_AUDIT_RETRY_SQL = """
CREATE TABLE IF NOT EXISTS audit_retry (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    task_id TEXT,
    request_json TEXT,
    diff_hash TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)
"""

_retry_migrated: set[tuple[str, int]] = set()


async def ensure_schema(project_id: str) -> None:
    """建 audit_retry 表（幂等，best-effort）。

    project 不存在（ProjectDbError）时跳过且**不**记迁移标记——调用方可能在
    project 尚未完全初始化时调用，下次再补（同 attestation.ensure_schema）。

    标记键 = ``(workspace, 连接世代)``（机制见
    :func:`db.project.schema_marker_key_for_project`）—— 按 project_id 记忆的
    旧标记在库整代重建后会继续命中，建表被跳过 → 下游 ``no such table``。
    """
    from hiveweave.db import project as project_db

    key = await project_db.schema_marker_key_for_project(project_id)
    if key in _retry_migrated:
        return
    try:
        await execute_by_project(project_id, CREATE_AUDIT_RETRY_SQL)
    except ProjectDbError:
        return
    except Exception as e:  # noqa: BLE001 — 建表失败不影响审计主流程
        log.warning("audit_retry_schema_failed", project_id=project_id, error=str(e))
        return
    _retry_migrated.add(key)


def _backoff_ms(attempts: int) -> int:
    """第 ``attempts`` 次失败后的退避毫秒数：2^n * 60s ± 20% jitter。"""
    base = (2 ** max(0, int(attempts))) * BASE_BACKOFF_S * 1000
    jitter = base * JITTER_RATIO
    return int(base + random.uniform(-jitter, jitter))


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _fetch_pending_row(
    project_id: str, agent_id: str, diff_hash: str
) -> dict[str, Any] | None:
    """同 agent + diff_hash 的 pending 行（fail-open → None）。"""
    try:
        conn = await get_project_db_by_project_id(project_id)
        cur = await conn.execute(
            "SELECT * FROM audit_retry "
            "WHERE agent_id = ? AND diff_hash = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT 1",
            [agent_id, diff_hash, STATUS_PENDING],
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001
        return None


async def _fetch_row(project_id: str, row_id: str) -> dict[str, Any] | None:
    try:
        conn = await get_project_db_by_project_id(project_id)
        cur = await conn.execute(
            "SELECT * FROM audit_retry WHERE id = ?", [row_id]
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001
        return None


async def _notify_agent(
    project_id: str,
    agent_id: str,
    task_id: str | None,
    message: str,
    idempotency_key: str,
) -> None:
    """收件箱通知（task 通道、wake=1、trusted_platform、幂等键）。

    模式照 tools/tasks/waive.py 的豁免通知。失败仅告警——通知丢了大不了
    agent 自己 request_code_audit，不该把审计主流程拖挂。
    """
    try:
        from hiveweave.services.inbox import InboxService

        await InboxService().send_message(
            from_agent_id=agent_id,
            to_agent_id=agent_id,
            message=message,
            message_type="task",
            priority="normal",
            task_id=task_id,
            wake=True,
            trusted_platform=True,
            idempotency_key=idempotency_key,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "audit_retry_notify_failed",
            agent_id=agent_id,
            project_id=project_id,
            error=str(e),
        )


async def _fetch_latest_row(
    project_id: str, agent_id: str, diff_hash: str
) -> dict[str, Any] | None:
    """同 agent + diff_hash 的**最近一行**（任意 status；fail-open → None）。"""
    try:
        conn = await get_project_db_by_project_id(project_id)
        cur = await conn.execute(
            "SELECT * FROM audit_retry "
            "WHERE agent_id = ? AND diff_hash = ? "
            "ORDER BY created_at DESC LIMIT 1",
            [agent_id, diff_hash],
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001
        return None


async def enqueue_failed_audit(
    project_id: str,
    agent_id: str,
    task_id: str | None,
    diff_hash: str,
    *,
    worktree: str | None = None,
    commit_hash: str | None = None,
    appeal_notes: str | None = None,
) -> dict[str, Any] | None:
    """llm_failed 时入队。返回 ``{"attempts", "exhausted", "resequence"}`` 或 None。

    同 agent + diff_hash 已有 pending 行 → attempts+1、退避、更新
    next_retry_at；attempts 达 MAX_ATTEMPTS → status=exhausted 并通知
    agent「多次自动重试仍失败，可考虑 waive（真实人工决策）」。
    耗尽后 agent 人工重试 → 无 pending 行 → 新插一行 attempts=1，标记
    ``resequence=True``（审计 P2：回执措辞点明这是新一轮重试序列）。
    ``appeal_notes``（审计 P1-4）随 request_json 落库，重跑时透传给
    run_code_audit——重试审计与首次审计看到同样的作者申诉。
    任何失败（无 DB / 异常）返回 None——调用方保持原软失败回执不变。
    """
    dh = str(diff_hash or "").strip()
    if not project_id or not agent_id or not dh:
        return None
    await ensure_schema(project_id)
    now = _now_ms()
    row = await _fetch_pending_row(project_id, agent_id, dh)
    if row is not None:
        attempts = int(row.get("attempts") or 0) + 1
        next_at = now + _backoff_ms(attempts)
        exhausted = attempts >= MAX_ATTEMPTS
        try:
            await execute_by_project(
                project_id,
                "UPDATE audit_retry SET attempts = ?, next_retry_at = ?, "
                f"status = ?, updated_at = ? WHERE id = ?",
                [
                    attempts,
                    next_at,
                    STATUS_EXHAUSTED if exhausted else STATUS_PENDING,
                    now,
                    str(row.get("id")),
                ],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("audit_retry_enqueue_update_failed", error=str(e))
            return None
        if exhausted:
            await _notify_agent(
                project_id,
                agent_id,
                task_id,
                "[AUDIT RETRY] 审计自动重试已达上限（5 次仍失败）。"
                "审计上游可能长时间不可用——如确认如此，可走 "
                "waive_attestation（真实人工决策，需可审计理由）；"
                "否则稍后重试 request_code_audit 即可。",
                f"audit_retry:{row.get('id')}:{attempts}",
            )
        log.info(
            "audit_retry_enqueued_update",
            agent_id=agent_id,
            attempts=attempts,
            exhausted=exhausted,
        )
        return {
            "attempts": attempts,
            "exhausted": exhausted,
            "resequence": False,
        }

    # 耗尽后再人工重试：无 pending 行但存在 exhausted 行 → 新一轮序列
    latest = await _fetch_latest_row(project_id, agent_id, dh)
    resequence = bool(
        latest and str(latest.get("status") or "") == STATUS_EXHAUSTED
    )
    rid = str(uuid.uuid4())
    request = {
        "project_id": project_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "diff_hash": dh,
        "worktree": worktree,
        "commit_hash": commit_hash,
        # 审计 P1-4：作者申诉随队列落库，重跑时透传（重试审计与首次
        # 审计看到同样的申诉上下文）。
        "appeal_notes": (str(appeal_notes).strip() or None)
        if appeal_notes
        else None,
        # 重跑策略：run_code_audit 会在重试时重新收集 diff / 重选模型，
        # 这里记录的是入队时刻的快照（审计溯源用）。
        "rerun": "run_code_audit(project_id, agent_id, task_id, oneshot_llm)",
    }
    attempts = 1
    try:
        await execute_by_project(
            project_id,
            "INSERT INTO audit_retry "
            "(id, agent_id, task_id, request_json, diff_hash, attempts, "
            "next_retry_at, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                rid,
                agent_id,
                task_id,
                json.dumps(request, ensure_ascii=False),
                dh,
                attempts,
                now + _backoff_ms(attempts),
                STATUS_PENDING,
                now,
                now,
            ],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("audit_retry_enqueue_insert_failed", error=str(e))
        return None
    log.info(
        "audit_retry_enqueued",
        agent_id=agent_id,
        task_id=task_id,
        diff_hash=dh[:12],
        attempts=attempts,
        resequence=resequence,
    )
    # 2026-09-22 缺口①：**首次入队也要回执**。
    # 此前只有"耗尽"（:249）/"成功"（:540）/"兜底"（:583）/"作废"（:608）四处
    # 通知，**唯独入队那一步没有** —— 而 `request_code_audit` 对 agent 的承诺
    # 恰恰是"已入队、平台稍后自动重试"。承诺与回执缺一边 ⇒ agent 只能靠
    # "再调一次看结果"自证，或干脆不信（承诺不可核对）。
    # ⚠ 幂等键 `:0` = 首轮第 0 次通知，与同域其余四键不撞号 —— 它们的尾段
    #   分别是 `:{attempts}`（耗尽，见本文件顶部 `_notify_agent` 上方第一处
    #   调用）/ `:{attempts_before}`（成功）/ `:{attempts}`（兜底）/ `:discarded`
    #   （作废）；**尾段为 0 的只有本处**，而 `attempts` 在 INSERT 路径上恒为
    #   `1`（见下方 `attempts = 1`）⇒ 更新路径永远发不出 `:0`。
    #   （判据：`grep -n 'audit_retry:{' services/audit_retry.py` 应恰为 5 处。）
    # ⚠ 它还必须**排在耗尽通知之前**：`tests/test_audit_epic_fixes.py` 以 `[-1]`
    #   取末条断言耗尽文案。本分支 `exhausted` 恒为 False，同一次调用里不会再
    #   发耗尽通知 ⇒ 天然满足；另有 `test_first_receipt_precedes_exhaustion_notice`
    #   把这条隐式依赖显式钉住。
    await _notify_agent(
        project_id,
        agent_id,
        task_id,
        "[AUDIT RETRY] 代码审计因上游失败已入队（第 1 次重试已排期）。"
        "平台会自动重试，不需要你重复调用 request_code_audit；"
        "队列实况可在 platform_state 的 `audit_retry.queued` 逐字段核对。",
        f"audit_retry:{rid}:0",
    )
    return {
        "attempts": attempts,
        "exhausted": False,
        "resequence": resequence,
    }


async def _resolve_oneshot_callback(agent_id: str):
    """重跑用的 oneshot 回调：优先在册 Agent 实例，否则同 HTTP 路径 adhoc。

    生产回调是 ``Agent._oneshot_llm(model_config, system, user)``；后台循环
    里 agent 实例可能已停（off-duty / 重启），此时用 adhoc 版本走同一条
    provider + retry 栈，不依赖 Agent 进程内状态。
    """
    try:
        from hiveweave.agents.supervisor import agent_manager

        inst = agent_manager.get_agent(agent_id)
        if inst is not None and hasattr(inst, "_oneshot_llm"):
            return inst._oneshot_llm
    except Exception:  # noqa: BLE001
        pass
    return _adhoc_oneshot_llm


async def _adhoc_oneshot_llm(
    model_config: dict, system_prompt: str, user_prompt: str
) -> str:
    """Agent._oneshot_llm 的无实例版本（同 HTTP 路径，见 agents/agent.py）。"""
    if not model_config:
        from hiveweave.services.model import NoModelConfiguredError

        raise NoModelConfiguredError("No model configured for oneshot LLM")

    from hiveweave.agents.agent import _review_llm_post_with_retry
    from hiveweave.llm.provider import provider_factory
    from hiveweave.llm.streamer.constants import _get_llm_semaphore

    provider = provider_factory.create(model_config)
    body = provider.build_body(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        stream=False,
        temperature=0.3,
    )
    headers = provider.build_headers()
    headers["Accept"] = "application/json"
    return await _review_llm_post_with_retry(
        provider.build_url(), body, headers, _get_llm_semaphore()
    )


class AuditRetryLoop:
    """跨项目单例后台循环（照 health_supervisor 的启停模式）。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        log.info("audit_retry_loop_started")

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        log.info("audit_retry_loop_stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(RETRY_INTERVAL_S)
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                log.error("audit_retry_loop_error", error=str(e))
                await asyncio.sleep(10)

    async def scan_once(self) -> int:
        """扫一遍所有在岗项目的到期 pending 行，返回处理行数（测试钩子）。"""
        try:
            rows = await meta_db.query(
                "SELECT id FROM projects WHERE is_started = 1"
            )
        except Exception as e:  # noqa: BLE001
            log.warning("audit_retry_list_projects_failed", error=str(e))
            return 0
        processed = 0
        for p in rows or []:
            # meta 库行是 sqlite3.Row（无 .get）；Row 与 dict 都支持 ["id"]。
            try:
                pid = str(p["id"] or "")
            except Exception:  # noqa: BLE001
                continue
            if not pid:
                continue
            try:
                processed += await self._scan_project(pid)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "audit_retry_project_scan_failed",
                    project_id=pid,
                    error=str(e),
                )
        return processed

    async def _scan_project(self, project_id: str) -> int:
        await ensure_schema(project_id)
        now = _now_ms()
        try:
            conn = await get_project_db_by_project_id(project_id)
        except Exception:  # noqa: BLE001
            return 0
        try:
            cur = await conn.execute(
                "SELECT * FROM audit_retry "
                "WHERE status = ? AND attempts < ? "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "ORDER BY next_retry_at ASC LIMIT ?",
                [STATUS_PENDING, MAX_ATTEMPTS, now, _SCAN_BATCH],
            )
            rows = await cur.fetchall()
            await cur.close()
        except Exception as e:  # noqa: BLE001
            log.warning(
                "audit_retry_scan_failed", project_id=project_id, error=str(e)
            )
            return 0
        processed = 0
        for r in rows or []:
            try:
                await self._process_row(project_id, dict(r))
                processed += 1
            except Exception as e:  # noqa: BLE001 — 单行失败不拖垮整轮
                log.warning(
                    "audit_retry_process_failed",
                    project_id=project_id,
                    row_id=dict(r).get("id"),
                    error=str(e),
                )
        return processed

    async def _process_row(self, project_id: str, row: dict[str, Any]) -> None:
        from hiveweave.services.attestation import hash_stdout
        from hiveweave.services.code_audit import (
            collect_worktree_diff,
            run_code_audit,
        )
        from hiveweave.services.worktree_review import agent_worktree_path

        agent_id = str(row.get("agent_id") or "")
        task_id = row.get("task_id")
        row_id = str(row.get("id") or "")
        old_hash = str(row.get("diff_hash") or "").strip()
        attempts_before = int(row.get("attempts") or 0)
        if not agent_id or not row_id:
            return

        # 重跑前重算 diff 哈希——diff 已变 = 要审的内容变了，旧结论作废。
        try:
            worktree = await agent_worktree_path(agent_id)
            if not worktree:
                await self._discard_row(
                    project_id, row, "worktree 不可用"
                )
                return
            diff = await collect_worktree_diff(worktree)
            new_hash = hash_stdout(diff)
        except Exception as e:  # noqa: BLE001 — 哈希失败本轮跳过，不烧 attempts
            log.warning(
                "audit_retry_rehash_failed", row_id=row_id, error=str(e)
            )
            await execute_by_project(
                project_id,
                "UPDATE audit_retry SET next_retry_at = ?, updated_at = ? "
                "WHERE id = ?",
                [_now_ms() + _backoff_ms(max(1, attempts_before)), _now_ms(), row_id],
            )
            return
        if not old_hash or new_hash != old_hash:
            await self._discard_row(project_id, row, "diff 已变化")
            return

        oneshot = await _resolve_oneshot_callback(agent_id)
        # 审计 P1-4：重跑透传入队时的作者申诉（与首次审计同一申诉上下文）
        appeal_notes: str | None = None
        try:
            raw_req = json.loads(str(row.get("request_json") or ""))
            if isinstance(raw_req, dict):
                appeal = str(raw_req.get("appeal_notes") or "").strip()
                appeal_notes = appeal or None
        except Exception:  # noqa: BLE001 — 脏 request_json 不阻断重跑
            appeal_notes = None
        result = await run_code_audit(
            project_id, agent_id, task_id,
            oneshot_llm=oneshot,
            appeal_notes=appeal_notes,
        )
        if result.get("audited"):
            await execute_by_project(
                project_id,
                "UPDATE audit_retry SET status = ?, updated_at = ? "
                "WHERE id = ?",
                [STATUS_DONE, _now_ms(), row_id],
            )
            verdict = result.get("verdict") or "UNKNOWN"
            att = str(result.get("attestation_id") or "")
            await _notify_agent(
                project_id,
                agent_id,
                task_id,
                f"[AUDIT RETRY] 审计重试成功（凭证 {att}，结论 {verdict}），"
                "可继续提交。无需再次 request_code_audit。",
                f"audit_retry:{row_id}:{attempts_before}",
            )
            log.info(
                "audit_retry_success",
                agent_id=agent_id,
                row_id=row_id,
                verdict=verdict,
            )
            return

        # 重跑仍失败：llm_failed 路径 run_code_audit 内部已对同一 pending
        # 行 attempts+1 / 退避。其余失败原因（no_model 等，上游配置问题）
        # enqueue 不接管——这里兜底推进退避，防止死扫；达上限按 exhausted。
        fresh = await _fetch_row(project_id, row_id)
        if not fresh or fresh.get("status") != STATUS_PENDING:
            return
        if int(fresh.get("attempts") or 0) > attempts_before:
            return  # enqueue 已接管
        attempts = attempts_before + 1
        exhausted = attempts >= MAX_ATTEMPTS
        try:
            await execute_by_project(
                project_id,
                "UPDATE audit_retry SET attempts = ?, next_retry_at = ?, "
                "status = ?, updated_at = ? WHERE id = ?",
                [
                    attempts,
                    _now_ms() + _backoff_ms(attempts),
                    STATUS_EXHAUSTED if exhausted else STATUS_PENDING,
                    _now_ms(),
                    row_id,
                ],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("audit_retry_fallback_backoff_failed", error=str(e))
            return
        if exhausted:
            await _notify_agent(
                project_id,
                agent_id,
                task_id,
                "[AUDIT RETRY] 审计自动重试已达上限（5 次仍失败）。"
                "审计上游可能长时间不可用——如确认如此，可走 "
                "waive_attestation（真实人工决策，需可审计理由）；"
                "否则稍后重试 request_code_audit 即可。",
                f"audit_retry:{row_id}:{attempts}",
            )

    async def _discard_row(
        self, project_id: str, row: dict[str, Any], reason: str
    ) -> None:
        row_id = str(row.get("id") or "")
        try:
            await execute_by_project(
                project_id,
                "UPDATE audit_retry SET status = ?, updated_at = ? "
                "WHERE id = ?",
                [STATUS_DISCARDED, _now_ms(), row_id],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("audit_retry_discard_failed", row_id=row_id, error=str(e))
            return
        await _notify_agent(
            project_id,
            str(row.get("agent_id") or ""),
            row.get("task_id"),
            f"[AUDIT RETRY] {reason}，队列审计作废（原 diff 结论不再适用），"
            "请重新调用 request_code_audit 审计当前代码。",
            f"audit_retry:{row_id}:discarded",
        )
        log.info(
            "audit_retry_discarded",
            agent_id=row.get("agent_id"),
            row_id=row_id,
            reason=reason,
        )


# Singleton（main.py lifespan 启停成对注册）
audit_retry_loop = AuditRetryLoop()
