"""VERIFY 时长比监控 — 纯只读指标（不改任何任务行为/门禁）。

口径定义见 ``docs/2026-09-05/verify-efficiency-metric.md``。动机（42 轮
L9-2 实证）：VERIFY 任务总时长 4.45h 而有效验证仅约 60min（4.4×）——
换人修复/组织恢复力把机制缺口吸收进完美终态，账面看不出假循环；比值
监控让「大量墙钟时间没有验证 run 支撑」的缺口显形。

归因事实（schema 决定，不硬造字段）：
- ``agent_runs`` 无 ``task_id`` → 只能按「assignee 集合 × 任务窗口」代理归因；
- assignee 集合 = 当前 ``tasks.assignee_id`` ∪ ``task_events``
  ``task.claimed`` actor ∪ ``task.reassigned`` payload.to_assignee
  （换人前后都算有效）；
- run 贡献 = 与归因窗口的交集时长，排除 ``status='error'``，
  残留 ``running`` clamp 到窗口终点。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import structlog

from hiveweave.db.project import readonly_project_conn

from .db import _ensure_schema
from .verify import is_verify_task

log = structlog.get_logger(__name__)

# closed 任务内存过滤 is_verify_task 前的最大扫描行数
_SCAN_LIMIT = 2000
# IN (?) 分块（SQLite 参数上限防御，同 timeline._chunked）
_CHUNK = 500
# 有效时长低于该值（分钟）视为「无有效验证活动」
_MIN_EFFECTIVE_MIN = 0.1
# 比值 flag 阈值（42 轮 L9-2 实证 4.4×）
_RATIO_FLAG_THRESHOLD = 3.0


@asynccontextmanager
async def _read_tx(conn: Any) -> AsyncIterator[Any]:
    """显式只读读事务：BEGIN ... COMMIT，异常 ROLLBACK 后上抛。

    SELECT 不隐式开事务（WAL 读事务首次读才钉快照），BEGIN 必须显式；
    语义与 ``services/tasks/timeline.py::_read_tx`` 一致。
    """
    if conn.in_transaction:
        log.warning("verify_efficiency_read_tx_stale_rollback")
        try:
            await conn.rollback()
        except Exception:
            pass
    await conn.execute("BEGIN")
    try:
        yield conn
        await conn.execute("COMMIT")
    except BaseException:
        try:
            await conn.execute("ROLLBACK")
        except BaseException:
            pass
        raise


async def _fetchall(conn: Any, sql: str, params: list | None = None) -> list:
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


def _chunked(seq: list, n: int = _CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _overlap_seconds(
    run_start: int, run_end: int, window_start: int, window_end: int
) -> float:
    """run 区间与任务归因窗口的交集秒数（无交集为 0）。"""
    return max(
        0.0, min(run_end, window_end) - max(run_start, window_start)
    ) / 1000.0


def _reassign_to_assignee(payload: Any) -> str | None:
    """从 task.reassigned payload 取 to_assignee（坏 payload 保守跳过）。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    to = payload.get("to_assignee")
    return str(to) if to else None


def _build_row(
    task: dict,
    assignees: set[str],
    reassignments: int,
    effective_seconds: float,
) -> dict:
    """按口径合成单任务报告行（docs §3）。"""
    created_at = int(task.get("created_at") or 0)
    # 归因窗口起点：claimed_at 优先（created→claimed 是排队，无 run 可归因）
    window_start = int(task.get("claimed_at") or created_at or 0)
    # closed_at 正常路径必写（close.py）；缺失退回 updated_at（防御）
    window_end = int(task.get("closed_at") or task.get("updated_at") or 0)
    total_minutes = max(0.0, (window_end - created_at)) / 60000.0

    flags: list[str] = []
    # 时钟倒挂双判：window_end ≤ window_start，或 window_end < created_at
    # （后者 total 被 max(0,…) 压 0 → 假 ratio=0.00，必须显式 flag）
    anomalous = window_end <= window_start or (
        created_at > 0 and window_end < created_at
    )
    if anomalous:
        flags.append("anomalous_window")
        effective_seconds = 0.0
    else:
        # cap 到窗口长度：交接重叠 run 同窗双计时不让有效超过墙钟（ratio≥1）
        window_seconds = (window_end - window_start) / 1000.0
        effective_seconds = min(effective_seconds, window_seconds)

    effective_minutes = effective_seconds / 60.0
    if effective_minutes < _MIN_EFFECTIVE_MIN:
        ratio: float | None = None
        ratio_display = "∞"
        flags.append("no_effective_activity")
    else:
        ratio = round(total_minutes / effective_minutes, 2)
        ratio_display = f"{ratio:.2f}"
        if ratio > _RATIO_FLAG_THRESHOLD:
            flags.append("ratio_gt_3")
    if reassignments > 0:
        flags.append("reassigned")

    return {
        "task_id": task.get("id"),
        "title": task.get("title"),
        "total_minutes": round(total_minutes, 1),
        "effective_minutes": round(effective_minutes, 1),
        "ratio": ratio,
        "ratio_display": ratio_display,
        "assignees": sorted(a for a in assignees if a),
        "reassignments": reassignments,
        "stale_flags": flags,
        "window": {
            "created_at": created_at,
            "claimed_at": task.get("claimed_at"),
            "closed_at": task.get("closed_at"),
        },
    }


async def verify_efficiency_report(
    project_id: str, limit: int = 20
) -> dict:
    """最近 N 个 closed VERIFY 任务的时长比报告（只读）。

    返回 ``{project_id, count, scanned, truncated, tasks: [...]}``，每行
    ``{task_id, title, total_minutes, effective_minutes, ratio,
    ratio_display, assignees, reassignments, stale_flags, window}``。
    ``scanned`` = SQL 触顶前扫描的 closed 行数；``truncated`` = 触顶
    ``_SCAN_LIMIT`` 且过滤后不足 limit（更老 VERIFY 可能被静默截断）。
    项目不存在 / workspace 已驱逐时抛 ``ProjectDbError``（API 层转 404）。
    """
    # 只读连接不做 ALTER —— 先确保 schema 已迁移再读（同 timeline.py:163）
    await _ensure_schema(project_id)

    async with readonly_project_conn(project_id) as conn:
        async with _read_tx(conn):
            closed = await _fetchall(
                conn,
                "SELECT id, title, kind, assignee_id, created_at, claimed_at, "
                "closed_at, updated_at FROM tasks "
                "WHERE status = 'closed' AND is_archived = 0 "
                "ORDER BY closed_at DESC LIMIT ?",
                [_SCAN_LIMIT],
            )
            # VERIFY 判定收口 is_verify_task（读 kind；标题已不参与判定，见 #11）
            selected = [
                t for t in closed if is_verify_task(t)
            ][: max(0, limit)]
            # P2-1 可观测：触顶且过滤后不足 limit → 更老 VERIFY 可能被截断
            truncated = len(closed) >= _SCAN_LIMIT and len(selected) < max(
                0, limit
            )
            if not selected:
                return {
                    "project_id": project_id,
                    "count": 0,
                    "tasks": [],
                    "scanned": len(closed),
                    "truncated": truncated,
                }

            task_ids = [t["id"] for t in selected]
            events: list[dict] = []
            for chunk in _chunked(task_ids):
                placeholders = ",".join("?" * len(chunk))
                events.extend(
                    await _fetchall(
                        conn,
                        "SELECT task_id, event_type, actor_id, payload "
                        "FROM task_events WHERE task_id IN "
                        f"({placeholders})",
                        chunk,
                    )
                )

            # 按 task 归组 assignee / 改派数（换人前后都算有效）
            assignees_by_task: dict[str, set[str]] = {
                tid: set() for tid in task_ids
            }
            reassignments_by_task: dict[str, int] = {tid: 0 for tid in task_ids}
            for t in selected:
                if t.get("assignee_id"):
                    assignees_by_task[t["id"]].add(str(t["assignee_id"]))
            for ev in events:
                tid = ev.get("task_id")
                if tid not in assignees_by_task:
                    continue
                etype = ev.get("event_type")
                if etype == "task.claimed":
                    if ev.get("actor_id"):
                        assignees_by_task[tid].add(str(ev["actor_id"]))
                elif etype == "task.reassigned":
                    reassignments_by_task[tid] += 1
                    to = _reassign_to_assignee(ev.get("payload"))
                    if to:
                        assignees_by_task[tid].add(to)

            # 全局窗口预过滤（跨任务粗界，交集在 Python 侧精算）
            global_start = min(
                int(t.get("claimed_at") or t.get("created_at") or 0)
                for t in selected
            )
            global_end = max(
                int(t.get("closed_at") or t.get("updated_at") or 0)
                for t in selected
            )
            all_assignees = sorted(
                {a for s in assignees_by_task.values() for a in s if a}
            )
            runs: list[dict] = []
            for chunk in _chunked(all_assignees):
                placeholders = ",".join("?" * len(chunk))
                runs.extend(
                    await _fetchall(
                        conn,
                        "SELECT agent_id, status, started_at, ended_at "
                        "FROM agent_runs WHERE agent_id IN "
                        f"({placeholders}) AND started_at <= ? "
                        # NULL-ended（残留 running）视为延伸至今、必然覆盖
                        # 窗口——第二界若用 COALESCE(ended_at, started_at)，
                        # started 早于窗口起点的残留 running 会被整行丢弃，
                        # Python 侧 clamp 永不生效 → 有效少计、ratio 虚高
                        "AND (ended_at IS NULL OR ended_at >= ?)",
                        [*chunk, global_end, global_start],
                    )
                )

            runs_by_agent: dict[str, list[dict]] = {}
            for run in runs:
                runs_by_agent.setdefault(str(run.get("agent_id")), []).append(
                    run
                )

            rows: list[dict] = []
            for t in selected:
                tid = t["id"]
                window_start = int(t.get("claimed_at") or t.get("created_at") or 0)
                window_end = int(t.get("closed_at") or t.get("updated_at") or 0)
                effective = 0.0
                for agent_id in assignees_by_task[tid]:
                    for run in runs_by_agent.get(agent_id, []):
                        # status='error' 不计有效工作（口径 docs §3.3）
                        if run.get("status") == "error":
                            continue
                        start = int(run.get("started_at") or 0)
                        # 残留 running（未落 ended_at）clamp 到窗口终点；
                        # cap 逻辑保证多 run 同窗不超墙钟
                        end = int(run.get("ended_at") or window_end)
                        effective += _overlap_seconds(
                            start, end, window_start, window_end
                        )
                rows.append(
                    _build_row(
                        t,
                        assignees_by_task[tid],
                        reassignments_by_task[tid],
                        effective,
                    )
                )

    return {
        "project_id": project_id,
        "count": len(rows),
        "tasks": rows,
        "scanned": len(closed),
        "truncated": truncated,
    }
