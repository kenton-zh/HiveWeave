"""Timeline aggregation service (Timeline v4 §4.2 / §4.3 / §4.5).

两个只读聚合，都走只读连接池（``db.project.readonly_project_conn``），
并包在显式 ``BEGIN...COMMIT`` 读事务内（WAL 读事务首次读钉快照，
消除四路查询之间的撕裂；Python sqlite3 默认 isolation_level 下
SELECT 不隐式开事务，所以 BEGIN 必须显式）。

- ``get_task_timeline``：单任务统一事件流 —— task_events / handoffs /
  inbox / work_logs 四路按 ts 归并。
- ``get_team_activity``：时间窗内 task_segments（切段算法 §4.5）+
  active_assignments + agents，供团队泳道视图。

设计裁决（v4 §三）：WS 事件只作失效信号、不作数据源——本服务是
前端 timeline 的唯一数据入口，丢事件/乱序/断连全部无害。
"""
from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import structlog

from hiveweave.db.project import readonly_project_conn

from .db import _ensure_schema
from .events import TaskEventService

log = structlog.get_logger(__name__)

# 状态分类（active_assignments 的 busy / waiting，v4 §4.3）
_BUSY_STATUSES = frozenset({"claimed", "running", "rework"})
_WAITING_STATUSES = frozenset(
    {"blocked", "submitted", "reviewing", "approved", "verifying"}
)
# ADR-001 R1：终态判定收敛到唯一常量（原窄口径 {closed, cancelled} 吸收；
# completed/done/cancelled 任务同样不该出现在活跃 assignment/开放线段里）
from hiveweave.services.tasks.constants import TERMINAL_STATUSES

_TERMINAL_STATUSES = TERMINAL_STATUSES

# event_type → 中文标题（单一来源，前端直接渲染）
_EVENT_TITLES: dict[str, str] = {
    "task.created": "创建",
    "task.claimed": "认领",
    "task.running": "开始执行",
    "task.blocked": "阻塞",
    "task.submitted": "提交评审",
    "task.reviewing": "开始评审",
    "task.approved": "评审通过",
    "task.rework": "返工",
    "task.closed": "关闭",
    "task.archived": "归档",
    "task.verifying": "开始验证",
    "task.verify_rehang": "验证重置",
    "task.reassigned": "改派",
    "handoff.created": "交接",
    "inbox.message": "消息",
    "work_log": "工作日志",
}

_INBOX_TITLES: dict[str, str] = {
    "task": "任务派发",
    "escalation": "升级通知",
    "system": "系统消息",
    "user_message": "用户消息",
    "expert_dispatch": "专家派遣",
}

# 合并排序时的同源平局优先级（task_events 最前）
_SOURCE_ORDER = {"task_event": 0, "handoff": 1, "inbox": 2, "work_log": 3}

# 平台转换回声 work_log 的 summary 前缀（"[claimed] task xxx …"，
# progress.py 发出的英文常量）
_ECHO_STATUS_RE = re.compile(r"^\[(\w+)\]")


def _parse_payload(raw: Any) -> dict:
    """payload JSON → dict；畸形/缺失一律返回 {}（聚合路径绝不因脏数据崩）。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


async def _fetchall(conn, sql: str, params: list) -> list:
    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def _fetchone(conn, sql: str, params: list):
    cursor = await conn.execute(sql, params)
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def _fetchval(conn, sql: str, params: list):
    row = await _fetchone(conn, sql, params)
    return row[0] if row is not None else None


def _chunked(seq: list, n: int = 500):
    """IN (?) 占位符分块（SQLite 参数上限防御）。"""
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


@asynccontextmanager
async def _read_tx(conn) -> AsyncIterator:
    """显式只读读事务：BEGIN ... COMMIT，异常 ROLLBACK 后上抛。

    防御：若连接上有未结束事务（持 slot 锁/写锁时不应发生，仅兜底
    共享连接降级路径的历史残留），先 ROLLBACK 再开始。
    """
    if conn.in_transaction:
        log.warning("timeline_read_tx_stale_rollback")
        try:
            await conn.rollback()
        except Exception:
            pass
    await conn.execute("BEGIN")
    try:
        yield conn
        await conn.execute("COMMIT")
    except BaseException:
        # 含 asyncio.CancelledError（BaseException）：取消也必须 ROLLBACK，
        # 否则池内只读连接留着开放读事务，钉住 WAL 快照阻塞 checkpoint。
        try:
            await conn.execute("ROLLBACK")
        except BaseException:
            pass
        raise


class TimelineService:
    """只读聚合：单任务事件流 + 团队活动段。"""

    def __init__(self) -> None:
        self._task_events = TaskEventService()

    # ── 端点 1：单任务全链路 ────────────────────────────────

    async def get_task_timeline(
        self, project_id: str, task_id: str, limit: int = 500
    ) -> dict:
        """四路归并的单任务统一事件流（v4 §4.3 端点 1）。

        返回 ``{task, agents, events, max_event_ts, truncated}``；
        task 不存在时 ``task=None``（API 层转 404）。
        截断语义：task_events 取最新 limit 条，合并后超 limit 丢最旧。
        """
        # reviewer_id/wait_kind 等 _MISSING_COLUMNS 由写路径迁移补齐——
        # 只读连接不做 ALTER，先确保 schema 已迁移再读。
        await _ensure_schema(project_id)
        # 延迟导入避免 services.tasks ↔ services.dispatch 包级环
        from hiveweave.services.dispatch import DispatchService

        dispatch = DispatchService()

        async with readonly_project_conn(project_id) as conn:
            async with _read_tx(conn):
                task_row = await _fetchone(
                    conn,
                    "SELECT id, title, description, status, assignee_id, "
                    "creator_id, reviewer_id, priority, progress, tags, "
                    "parent_task_id, blocked_reason, wait_kind, is_archived, "
                    "created_at, claimed_at, submitted_at, closed_at, "
                    "updated_at, archived_at "
                    "FROM tasks WHERE id = ?",
                    [task_id],
                )
                if task_row is None:
                    return {
                        "task": None, "agents": {}, "events": [],
                        "max_event_ts": 0, "truncated": False,
                    }
                task = dict(task_row)

                # 四路查询，同一读事务内
                te_rows = await self._task_events.get_task_history(
                    project_id, task_id, limit=limit, conn=conn,
                    oldest_first=False,
                )
                handoff_rows = await _fetchall(
                    conn,
                    "SELECT id, from_agent_id, to_agent_id, summary, status, "
                    "created_at, updated_at FROM handoffs "
                    "WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    [task_id, limit],
                )
                handoff_rows.reverse()  # 回放保留近端，超出截最旧
                inbox_rows = await _fetchall(
                    conn,
                    "SELECT id, from_agent_id, to_agent_id, message, "
                    "message_type, expect_report, priority, created_at "
                    "FROM inbox WHERE task_id = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    [task_id, limit],
                )
                inbox_rows.reverse()
                wl_rows = await dispatch.get_work_logs_for_task(
                    project_id, task_id,
                    include_details_fallback=True, conn=conn, limit=limit,
                )

                events: list[dict] = []
                te_events = self._events_from_task_events(task_id, te_rows)
                events.extend(te_events)
                events.extend(self._events_from_handoffs(task_id, handoff_rows))
                events.extend(self._events_from_inbox(task_id, inbox_rows))
                events.extend(
                    self._events_from_work_logs(task_id, wl_rows, te_events)
                )
                events.sort(
                    key=lambda e: (
                        e["ts"], _SOURCE_ORDER.get(e.get("_src", ""), 9)
                    )
                )
                for e in events:
                    e.pop("_src", None)

                max_event_ts = max((e["ts"] for e in events), default=0)
                # 保守截断判定：任一路取回行数达 limit 即可能有更旧行被截
                # （单源溢出时 len(events)==limit，旧判法会谎报未截断）。
                # == limit 包含"恰好整除"的假阳性，但宁多报不漏报。
                truncated = (
                    len(te_rows) >= limit
                    or len(handoff_rows) >= limit
                    or len(inbox_rows) >= limit
                    or len(wl_rows) >= limit
                )
                if len(events) > limit:
                    truncated = True
                    events = events[-limit:]  # 回放保留近端

                # agents 映射：任务三方 + 事件涉及者（同事务内）
                agent_ids: set[str] = set()
                for key in ("assignee_id", "creator_id", "reviewer_id"):
                    if task.get(key):
                        agent_ids.add(task[key])
                for e in events:
                    for key in ("agent_id", "from_agent_id", "to_agent_id"):
                        if e.get(key):
                            agent_ids.add(e[key])
                agents: dict[str, dict] = {}
                for chunk in _chunked(sorted(agent_ids)):
                    ph = ",".join("?" * len(chunk))
                    rows = await _fetchall(
                        conn,
                        f"SELECT id, name, role FROM agents WHERE id IN ({ph})",
                        chunk,
                    )
                    for r in rows:
                        agents[r["id"]] = {
                            "name": r["name"], "role": r["role"],
                        }

        return {
            "task": task,
            "agents": agents,
            "events": events,
            "max_event_ts": max_event_ts,
            "truncated": truncated,
        }

    # ── 四路 → 统一 schema（v4 §4.2）──────────────────────

    def _events_from_task_events(
        self, task_id: str, rows: list
    ) -> list[dict]:
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            payload = _parse_payload(d.get("payload"))
            event_type = d.get("event_type") or "task.unknown"
            reason_code = payload.get("reason_code")
            title = _EVENT_TITLES.get(event_type, event_type)
            # 打回在事件流里是 reviewing→running（_transition_multi 原子一行），
            # 靠 reason_code 识别（v4 §二.2）
            if event_type == "task.running" and reason_code == "review_rework":
                title = "评审打回"
            if event_type == "task.archived" and d.get("to_status") == "cancelled":
                title = "归档取消"
            out.append({
                "id": d.get("id"),
                "ts": d.get("created_at") or 0,
                "type": event_type,
                "task_id": task_id,
                "agent_id": d.get("actor_id"),
                "from_agent_id": payload.get("from_assignee"),
                "to_agent_id": payload.get("to_assignee"),
                "from_status": d.get("from_status"),
                "to_status": d.get("to_status"),
                "reason_code": reason_code,
                "title": title,
                "detail": payload,
                "_src": "task_event",
            })
        return out

    def _events_from_handoffs(self, task_id: str, rows: list) -> list[dict]:
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            # 每个 handoff 只出 1 条：创建时间 + 当前 status（状态流转
            # 是同行 UPDATE，无历史，不承诺轨迹 — v4 §4.3）
            out.append({
                "id": d.get("id"),
                "ts": d.get("created_at") or 0,
                "type": "handoff.created",
                "task_id": task_id,
                "agent_id": d.get("from_agent_id"),
                "from_agent_id": d.get("from_agent_id"),
                "to_agent_id": d.get("to_agent_id"),
                "from_status": None,
                "to_status": None,
                "reason_code": None,
                "title": _EVENT_TITLES["handoff.created"],
                "detail": {
                    "summary": d.get("summary"),
                    "status": d.get("status"),
                    "updated_at": d.get("updated_at"),
                },
                "_src": "handoff",
            })
        return out

    def _events_from_inbox(self, task_id: str, rows: list) -> list[dict]:
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            mtype = d.get("message_type") or ""
            out.append({
                "id": d.get("id"),
                "ts": d.get("created_at") or 0,
                "type": "inbox.message",
                "task_id": task_id,
                "agent_id": d.get("from_agent_id"),
                "from_agent_id": d.get("from_agent_id"),
                "to_agent_id": d.get("to_agent_id"),
                "from_status": None,
                "to_status": None,
                "reason_code": None,
                "title": _INBOX_TITLES.get(mtype, _EVENT_TITLES["inbox.message"]),
                "detail": {
                    "message": (d.get("message") or "")[:500],
                    "message_type": mtype or None,
                    "expect_report": bool(d.get("expect_report")),
                    "priority": d.get("priority"),
                },
                "_src": "inbox",
            })
        return out

    def _events_from_work_logs(
        self,
        task_id: str,
        rows: list,
        task_events: list[dict] | None = None,
    ) -> list[dict]:
        # type='task_event' 的 work_log 是平台转换回声（progress.py
        # emit_task_event 的副产品），与 task_events 表同源——同状态
        # ±10s 内已有正式事件时丢弃（盲区兜底只覆盖无正式事件的缺口，
        # 否则事件流里同一转换出现两行）。标题用 summary（平台协议
        # 前缀 "[claimed] …" 英文常量），不显示裸 "task_event"。
        echoes = [
            (e.get("to_status"), e["ts"])
            for e in (task_events or [])
            if e.get("to_status")
        ]
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            summary = (d.get("summary") or "").strip()
            is_echo = d.get("type") == "task_event"
            if is_echo:
                m = _ECHO_STATUS_RE.match(summary)
                ts = d.get("created_at") or 0
                if m and any(
                    st == m.group(1) and abs(ets - ts) <= 10_000
                    for st, ets in echoes
                ):
                    continue
            if is_echo:
                title = summary or d.get("action") or d.get("type")
            else:
                title = (
                    d.get("action") or d.get("type") or _EVENT_TITLES["work_log"]
                )
            out.append({
                "id": d.get("id"),
                "ts": d.get("created_at") or 0,
                "type": "work_log",
                "task_id": task_id,
                "agent_id": d.get("agent_id"),
                "from_agent_id": None,
                "to_agent_id": None,
                "from_status": None,
                "to_status": None,
                "reason_code": None,
                "title": str(title)[:80],
                "detail": {
                    "log_type": d.get("type"),
                    "summary": (d.get("summary") or "")[:500],
                    "action": d.get("action"),
                },
                "_src": "work_log",
            })
        return out

    # ── 端点 2：团队活动段 ──────────────────────────────────

    async def get_team_activity(
        self,
        project_id: str,
        since_ms: int,
        until_ms: int,
        limit: int = 2000,
        cursor_ts: int | None = None,
        if_changed_since: int | None = None,
    ) -> dict:
        """时间窗聚合（v4 §4.3 端点 2）。

        - ``cursor_ts`` 替代 since_ms 实现「加载更早」游标分页；
        - ``if_changed_since`` 对比全局 task_events 最新 ts，无变化时
          O(1) 返回 ``{changed: False}``（等价 304）。
          **契约**：该短路不校验窗口参数——调用方切换窗口时**必须不
          带** ``if_changed_since``（否则可能拿到旧窗口的 changed=False
          假阴性）。前端负责强制；短路响应回显 window 供客户端自检。
        - ``truncated`` 显式标记超 limit（前端提示缩窗，不静默截断）。
        """
        if until_ms < since_ms:
            raise ValueError("until_ms must be >= since_ms")
        eff_since = cursor_ts if cursor_ts is not None else since_ms
        if eff_since > until_ms:
            eff_since = until_ms

        await _ensure_schema(project_id)
        async with readonly_project_conn(project_id) as conn:
            async with _read_tx(conn):
                global_max = await _fetchval(
                    conn,
                    "SELECT MAX(created_at) FROM task_events "
                    "WHERE project_id = ?",
                    [project_id],
                ) or 0
                if (
                    if_changed_since is not None
                    and global_max <= if_changed_since
                ):
                    return {
                        "changed": False,
                        "max_event_ts": global_max,
                        # 回显窗口供客户端自检（短路不校验窗口，见 docstring）
                        "window": {"since": eff_since, "until": until_ms},
                    }

                agent_rows = await _fetchall(
                    conn,
                    "SELECT id, name, role, parent_id, status, last_active_at "
                    "FROM agents WHERE project_id = ?",
                    [project_id],
                )

                # 与窗口相交的任务：创建早于窗口末，且未终态或终态晚于窗口首。
                # （list_tasks 排除归档，这里必须包含，cancelled 段要可见。）
                task_rows = await _fetchall(
                    conn,
                    "SELECT id, title, status, assignee_id, creator_id, "
                    "reviewer_id, created_at, claimed_at, closed_at, "
                    "updated_at, archived_at, is_archived "
                    "FROM tasks WHERE created_at <= ? "
                    "AND (status NOT IN ('closed', 'cancelled') "
                    "     OR COALESCE(closed_at, archived_at, updated_at) >= ?)",
                    [until_ms, eff_since],
                )
                tasks = [dict(r) for r in task_rows]
                task_ids = [t["id"] for t in tasks]

                # 段边界事件：取窗口内任务的全部历史（含窗口前，切段需要
                # 知道窗口起点的状态/持有人）。全局事件预算：先 COUNT 探测
                # 截断（跨 chunk 求和），再按 DESC 分块取、归并保留全局最新
                # limit 条——超限时丢最旧而非最新（旧实现 ASC LIMIT 会丢
                # 窗口内事件，切断段）。
                events_by_task: dict[str, list[dict]] = {}
                truncated = False
                has_more_earlier = False
                total_events = 0
                for chunk in _chunked(task_ids):
                    ph = ",".join("?" * len(chunk))
                    cnt = await _fetchval(
                        conn,
                        "SELECT COUNT(*) FROM task_events "
                        f"WHERE task_id IN ({ph}) AND created_at <= ?",
                        [*chunk, until_ms],
                    ) or 0
                    total_events += int(cnt)
                    if eff_since > 0 and not has_more_earlier:
                        probe = await _fetchone(
                            conn,
                            "SELECT 1 FROM task_events "
                            f"WHERE task_id IN ({ph}) AND created_at < ? "
                            "LIMIT 1",
                            [*chunk, eff_since],
                        )
                        has_more_earlier = probe is not None
                if total_events > limit:
                    truncated = True

                fetched: list[dict] = []
                for chunk in _chunked(task_ids):
                    ph = ",".join("?" * len(chunk))
                    rows = await _fetchall(
                        conn,
                        "SELECT id, task_id, event_type, from_status, "
                        "to_status, actor_id, payload, created_at "
                        f"FROM task_events WHERE task_id IN ({ph}) "
                        "AND created_at <= ? "
                        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                        [*chunk, until_ms, limit],
                    )
                    fetched.extend(dict(r) for r in rows)
                if truncated:
                    fetched.sort(
                        key=lambda d: (d["created_at"] or 0, d["id"]),
                        reverse=True,
                    )
                    fetched = fetched[:limit]
                fetched.sort(key=lambda d: (d["created_at"] or 0, d["id"]))
                for d in fetched:
                    events_by_task.setdefault(d["task_id"], []).append(d)

                if truncated:
                    # 截断可能把某任务的窗口前历史全丢光 → 窗口起点状态
                    # 未知（切段会从 created/None 错起）。对保留事件够不到
                    # eff_since 之前的任务补一条「种子事件」（eff_since 前
                    # 最新一条），收敛窗口头部的切段误差。
                    need_seed = [
                        t["id"] for t in tasks
                        if (t.get("created_at") or 0) < eff_since
                        and not any(
                            (e.get("created_at") or 0) < eff_since
                            for e in events_by_task.get(t["id"], [])
                        )
                    ]
                    for chunk in _chunked(need_seed):
                        ph = ",".join("?" * len(chunk))
                        rows = await _fetchall(
                            conn,
                            "SELECT id, task_id, event_type, from_status, "
                            "to_status, actor_id, payload, created_at "
                            "FROM (SELECT *, ROW_NUMBER() OVER ("
                            "  PARTITION BY task_id "
                            "  ORDER BY created_at DESC, rowid DESC) AS _rn "
                            f"  FROM task_events WHERE task_id IN ({ph}) "
                            "  AND created_at < ?) WHERE _rn = 1",
                            [*chunk, eff_since],
                        )
                        for r in rows:
                            d = dict(r)
                            # 种子早于该任务全部保留事件 → 头插保持升序
                            events_by_task.setdefault(
                                d["task_id"], []
                            ).insert(0, d)

        segments: list[dict] = []
        # 末段校准只对「触达最新事件」的实时窗口生效：历史回看窗口
        # （until < global_max）用 tasks 表当前值会把未来的状态/持有人
        # 错标到历史段上（审计发现）。
        calibrate = until_ms >= global_max
        for t in tasks:
            segments.extend(
                self._segment_task(
                    t, events_by_task.get(t["id"], []), eff_since, until_ms,
                    calibrate=calibrate,
                )
            )
        segments.sort(key=lambda s: (s["started_at"], s["task_id"]))

        assignments: list[dict] = []
        for t in tasks:
            status = (t.get("status") or "").lower()
            if bool(t.get("is_archived")) or status in _TERMINAL_STATUSES:
                continue
            since = (
                t.get("claimed_at") or t.get("updated_at")
                or t.get("created_at") or 0
            )
            base = {
                "task_id": t["id"],
                "task_title": t.get("title") or "",
                "since": since,
            }
            if status in _BUSY_STATUSES:
                if t.get("assignee_id"):
                    assignments.append(
                        {**base, "agent_id": t["assignee_id"], "kind": "busy"}
                    )
            elif status in _WAITING_STATUSES:
                holders: list[str] = []
                if t.get("assignee_id"):
                    holders.append(t["assignee_id"])
                reviewer = t.get("reviewer_id")
                # waiting 段含 assignee 与 reviewer 两类持有者（v4 §4.3）
                if reviewer and reviewer not in holders:
                    holders.append(reviewer)
                for h in holders:
                    assignments.append(
                        {**base, "agent_id": h, "kind": "waiting"}
                    )

        return {
            "agents": [dict(r) for r in agent_rows],
            "task_segments": segments,
            "active_assignments": assignments,
            "window": {"since": eff_since, "until": until_ms},
            "max_event_ts": global_max,
            "changed": True,
            "truncated": truncated,
            "has_more_earlier": has_more_earlier,
        }

    # ── 切段算法（v4 §4.5）──────────────────────────────────

    def _segment_task(
        self,
        task: dict,
        events: list[dict],
        window_since: int,
        until_ms: int,
        calibrate: bool = True,
    ) -> list[dict]:
        """单任务切 (status, assignee) 段并裁剪到窗口。

        - 段边界由 task_events 的 to_status 驱动；
        - assignee 游标由 task.claimed（payload.assignee_id 优先，
          兜底 actor）/ task.reassigned（payload.to_assignee）/
          unclaim（claimed→created 的 task.created → None）驱动；
        - 末段用 tasks.assignee_id / status 当前值校准（§4.5 步骤 4，
          兜住残余盲区）——仅 ``calibrate=True``（实时窗口）；历史
          回看窗口用当前值会造成时代错乱；
        - blocked 段保留 assignee（block/unblock 不动 assignee_id）。
        """
        created_at = task.get("created_at") or 0
        cur_status = "created"
        cur_assignee: str | None = None
        seg_start = created_at
        raw_segments: list[tuple[int, int | None, str, str | None]] = []

        def close_seg(end_ts: int) -> None:
            nonlocal seg_start
            if end_ts > seg_start:
                raw_segments.append(
                    (seg_start, end_ts, cur_status, cur_assignee)
                )
            seg_start = end_ts

        for e in events:
            ts = e.get("created_at") or 0
            if ts < seg_start:
                ts = seg_start  # 乱序/同刻容错：不产生负长段
            et = e.get("event_type") or ""
            payload = _parse_payload(e.get("payload"))
            new_status = e.get("to_status") or cur_status
            new_assignee = cur_assignee
            if et == "task.claimed":
                # create_task 直写 claimed 时 actor=creator，真实持有人
                # 在 payload.assignee_id
                new_assignee = (
                    payload.get("assignee_id") or e.get("actor_id")
                    or cur_assignee
                )
            elif et == "task.reassigned":
                new_assignee = payload.get("to_assignee") or cur_assignee
            elif et == "task.created" and e.get("from_status") == "claimed":
                new_assignee = None  # unclaim → 待认领空段
            if (new_status, new_assignee) != (cur_status, cur_assignee):
                close_seg(ts)
                cur_status, cur_assignee = new_status, new_assignee

        # 末段终点：终态取 closed_at/archived_at/末事件/updated_at；
        # 非终态 None（进行中，前端画到当前时刻红线）
        status_now = (task.get("status") or "").lower()
        is_terminal = (
            status_now in _TERMINAL_STATUSES or bool(task.get("is_archived"))
        )
        if is_terminal:
            end_ts: int | None = (
                task.get("closed_at") or task.get("archived_at")
                or (events[-1].get("created_at") if events else None)
                or task.get("updated_at") or 0
            )
        else:
            end_ts = None
        raw_segments.append((seg_start, end_ts, cur_status, cur_assignee))

        # §4.5 步骤 4：末段校准当前值（兜住无事件的裸 UPDATE 盲区）
        if calibrate:
            last_start, last_end, last_status, last_assignee = raw_segments[-1]
            cal_status = status_now or last_status
            cal_assignee = (
                task.get("assignee_id") if not is_terminal else last_assignee
            )
            if cal_status != last_status or cal_assignee != last_assignee:
                raw_segments[-1] = (
                    last_start, last_end, cal_status, cal_assignee
                )

        # 裁剪到窗口
        out: list[dict] = []
        for start, end, status, assignee in raw_segments:
            if end is not None and end <= window_since:
                continue
            if start >= until_ms:
                continue
            s = max(start, window_since)
            if end is None:
                clipped_end: int | None = None
            else:
                clipped_end = min(end, until_ms)
                if clipped_end <= s:
                    continue
            out.append({
                "task_id": task["id"],
                "title": task.get("title") or "",
                "assignee_id": assignee,
                "creator_id": task.get("creator_id"),
                "reviewer_id": task.get("reviewer_id"),
                "status": status,
                "started_at": s,
                "ended_at": clipped_end,
                "ongoing": end is None,
            })
        return out


timeline_service = TimelineService()
