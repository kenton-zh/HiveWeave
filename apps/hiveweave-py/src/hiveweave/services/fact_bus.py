"""L3 事实总线（repair-plan-20260902.md §L3，2026-09-08 完整形态）。

事实成为一等事件：dispatch/merge/verify/fs 探测等动作产出**带核验时间戳**
的事实，推送 bus 供订阅方（按事实唤醒、工作簿进度推导、观测面板）消费。

完整形态（本次落地）：
- **四元组 + 主体**：kind / subject / payload / verified_at / source /
  project_id（项目级事实持久化到 per-project ``facts`` 表，平台级只走内存环）。
- **持久化**：publish 时 best-effort 落库（cap 500 条/项目，FIFO 淘汰），
  重启不丢——「等待可订阅事实变化」需要持久订阅语义的底气；落库失败只
  告警不阻断发布方（事实是参考上下文，不是关键路径）。
- **按事实唤醒**：wait_contract ``kind="fact"`` 等待 + game_time 订阅总线
  投递 ``[FACT_OBSERVED]``（见 wait_contract.wake_fact_waiters）。

防劣化护栏（repair-plan 原文）：
- 事实必须带核验时间戳——过期事实比没有更危险
- 事实是**参考上下文**非义务——执行者可复核任何一条
- 平台只自动采**廉价可机核**事实（fs/git/http 探测）
- 语义事实（"模块没执行"）必须来自 agent 取证并标注来源
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

import structlog

log = structlog.get_logger(__name__)

#: 每项目持久化事实上限（FIFO 淘汰最旧）。
FACTS_TABLE_CAP = 500

# 表 DDL 权威在 db/schema.py PROJECT_DB_TABLES（审计 P2-3：勿在本模块
# 复制一份——分叉迟早事故）。存量库由 ensure_project_db 按清单补建。


@dataclass(frozen=True)
class Fact:
    """一条已核验事实。

    Attributes:
        kind: 事实类型（merge_landed / task_closed / fs.absent / …）。
        subject: 事实主体（task_id / file path / branch name 等）。
        payload: 事实详情（结构化，由发布方定义 schema）。
        verified_at: 核验时间戳（epoch ms）——过期事实比没有更危险。
        source: 发布来源标识（"platform" / agent short_id 等）。
        project_id: 项目归属（None=平台级，只走内存环不落库）。
    """

    kind: str
    subject: str
    payload: dict[str, Any]
    verified_at: int
    source: str
    project_id: str | None = None


Subscriber = Callable[[Fact], None]

_subscribers: list[Subscriber] = []
_recent: list[Fact] = []
_MAX_RECENT = 200
_persist_failed_projects: set[str] = set()
# P2-4（审计）：持住 fire-and-forget task 引用——asyncio 文档明确未持引用
# 的 task 可能被 GC 半路消失。done 后自清。
_background_tasks: set[asyncio.Task] = set()


async def _persist(fact: Fact) -> None:
    """项目级事实落库（best-effort，cap FIFO）。失败降温：每项目只告警一次。"""
    if not fact.project_id:
        return
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.db.project import (
            execute_by_project,
            get_workspace_write_lock,
            ensure_project_db,
        )

        workspace = await meta_db.get_project_workspace(fact.project_id)
        if not workspace:
            return
        lock = await get_workspace_write_lock(workspace)
        async with lock:
            conn = await ensure_project_db(workspace)
            cur = await conn.execute(
                "INSERT INTO facts (kind, subject, payload, source, project_id, "
                "verified_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    fact.kind,
                    fact.subject,
                    json.dumps(fact.payload, ensure_ascii=False),
                    fact.source,
                    fact.project_id,
                    fact.verified_at,
                ],
            )
            await cur.close()
            # cap FIFO：超出上限删最旧
            del_cur = await conn.execute(
                "DELETE FROM facts WHERE project_id = ? AND id NOT IN ("
                "SELECT id FROM facts WHERE project_id = ? "
                "ORDER BY id DESC LIMIT ?)",
                [fact.project_id, fact.project_id, FACTS_TABLE_CAP],
            )
            await del_cur.close()
            await conn.commit()
    except Exception as e:
        if fact.project_id not in _persist_failed_projects:
            _persist_failed_projects.add(fact.project_id)
            log.warning(
                "fact_persist_failed",
                project_id=fact.project_id,
                kind=fact.kind,
                error=str(e),
            )


def publish(
    kind: str,
    subject: str,
    payload: dict[str, Any] | None = None,
    *,
    source: str = "platform",
    project_id: str | None = None,
) -> Fact:
    """发布一条事实：内存环 + 订阅方通知 +（项目级）best-effort 落库。

    同步、进程内、fire-and-forget；落库在后台事件循环排空（发布方不等待、
    不受落库失败影响）。无运行中事件循环时跳过落库（TTL 兜底语义不受影响）。
    """
    fact = Fact(
        kind=kind,
        subject=subject,
        payload=payload or {},
        verified_at=int(time.time() * 1000),
        source=source,
        project_id=project_id,
    )
    _recent.append(fact)
    if len(_recent) > _MAX_RECENT:
        del _recent[: len(_recent) - _MAX_RECENT]
    _schedule_persist(fact)
    for sub in _subscribers:
        try:
            sub(fact)
        except Exception as e:
            log.warning("fact_subscriber_error", kind=kind, error=str(e))
    log.debug("fact_published", kind=kind, subject=subject[:80])
    return fact


def _schedule_persist(fact: Fact) -> None:
    """把落库排进运行中的事件循环；无循环（纯同步上下文）则跳过。"""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_persist(fact))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def subscribe(fn: Subscriber) -> None:
    """注册事实订阅方（同步回调；异常被总线吞掉不阻塞发布方）。

    回调内如需异步操作，自行 ``asyncio.get_running_loop().create_task``。
    """
    _subscribers.append(fn)


def unsubscribe(fn: Subscriber) -> None:
    try:
        _subscribers.remove(fn)
    except ValueError:
        pass


def recent_facts(kind: str | None = None, limit: int = 20) -> list[Fact]:
    """查询最近事实（可选按 kind 过滤）。观测/工作簿进度推导用。"""
    if kind:
        return [f for f in _recent if f.kind == kind][-limit:]
    return list(_recent[-limit:])


def reset_for_tests() -> None:
    _recent.clear()
    _subscribers.clear()
    _persist_failed_projects.clear()
    for t in list(_background_tasks):
        t.cancel()
    _background_tasks.clear()
