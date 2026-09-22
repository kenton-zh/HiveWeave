"""Event audit — lightweight event audit log.

契约 16: 可观测性
- Writes events to agent_events table in per-project DB
- log() is fire-and-forget (asyncio.create_task, returns immediately)
- timeline() returns recent events (default 1 hour, LIMIT 100, DESC)
- payload decoded as dict in timeline results (friendlier than Elixir string)
- Event types: stream_start, stream_chunk, stream_done, stream_fail,
  chat_start, chat_done, crash, circuit_open, circuit_close
"""

import asyncio
import json
import time
import uuid

import structlog

from hiveweave.db import project as project_db

logger = structlog.get_logger()


#: 「未解析」哨兵：区分「调用方没给预解析结果（就地解析）」与「解析出来是 None」。
_UNRESOLVED = object()


def _resolve_project_id(agent_id: str, project_id: str) -> str | None:
    """把两个判据解析成一个 project_id —— **本文件唯一的源选定逻辑**。

    **同步**（纯内存查表，无 I/O）—— 这一点是契约的一部分，见 ``log()`` 的注释：
    解析必须在**调用方的同步段**完成，不能推到后台任务里。

    次序（D67-1）：
    1. **正式路由** `AgentRouter.get_project_id`（权威、全平台同源）；
    2. **瞬态身份** `resolve_transient_project_id` —— 专治 `sub-*`：它们
       **运行时临时生成、从不入库**，正式路由**必然**查不到（2026-09-22 实测
       `event_audit.write_failed` **36/36 全是这一类**，跨 6 个父代理、167.9 分钟）；
    3. 调用方显式传入的 `project_id`（**非空才用**）—— 兜底启动窗口
       （路由表尚未 `rebuild()`），也顺手接住 `recovery` / `code_audit` /
       `reconcile` 三个传真值的调用点。

    三条都不成立 ⇒ `None`。**不许猜**：猜出来的 project_id 会把事件写进
    别人的库（比丢掉更难查）。返回 `None` 时调用方必须**有声报错**。

    ⚠ 为什么**不**去解析 `sub-<parent>-<suffix>` 的字面形状：那是**文本判据**，
    前缀一改即失效，且与「project_id 在产生那一刻由父登记」的状态判据
    相比是更弱的源头（用户 09-14 钦定：禁用文案/字面判据）。
    """
    from hiveweave.services.agent_router import agent_router

    aid = str(agent_id or "").strip()
    if aid:
        routed = agent_router.get_project_id(aid)
        if routed:
            return str(routed)
        transient = agent_router.resolve_transient_project_id(aid)
        if transient:
            logger.debug(
                "event_audit.routed_by_transient",
                agent_id=aid,
                project_id=transient,
            )
            return str(transient)
    pid = str(project_id or "").strip()
    return pid or None



class EventAudit:
    """Lightweight event audit log backed by per-project DB.

    The agent_events table is created by ProjectFactory (schema.py),
    not by this service.
    """

    async def log(
        self,
        agent_id: str,
        project_id: str,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        """Log an event asynchronously (fire-and-forget).

        Returns immediately; the DB write runs in a background task.
        ``project_id`` 是**备用判据**（D67-1）：路由优先用 ``agent_id`` 的
        正式/瞬态身份，两者都解析不出时才用它；三条都为空 ⇒ 后台任务
        报 ``write_failed``（**不静默**，见 ``_write``）。

        ⚠ **源选定在本函数的同步段完成**（不推给后台任务）—— 生产者的瞬态
        身份是**短命**的：`tools/subagent.py::_work` 在 `_run_subagent` 返回后
        立刻 `unregister_transient`。若把解析留到任务体里做，只要子代理在
        「最后一次 `log()` → `unregister`」之间**没有让出事件循环**，解析就
        会在摘表之后发生 ⇒ **症状原地复发**（审计 P1-a 实测：插一个 await
        即让 12 条测试全绿而生产仍失败）。同步解析（纯内存查表）消除该窗口。
        """
        resolved: str | None = None
        resolve_error: str | None = None
        try:
            resolved = _resolve_project_id(agent_id, project_id)
        except Exception as e:  # noqa: BLE001
            # `log()` 是 fire-and-forget 且被多处 try/except 包着 —— 这里
            # **不能抛**（抛出去等于把取证旁支变成调用方的失败），改为把
            # 错误随任务带下去，由 `_write` 记名。
            resolve_error = str(e)
        asyncio.create_task(
            self._write(
                agent_id,
                str(project_id or ""),
                str(event_type),
                payload or {},
                resolved_pid=resolved,
                resolve_error=resolve_error,
            )
        )

    async def timeline(
        self, agent_id: str, hours: int = 1, limit: int = 100
    ) -> list[dict]:
        """Get timeline of events for an agent.

        Default: last 1 hour, max 100 rows, ordered by created_at DESC.
        Returns empty list on error.

        ⚠ 路由与 `_write` **同源**（`_resolve_project_id`）：否则写进去的事件
        读不出来 —— 子代理身份在正式路由里查不到（D67-1 同源缺陷的另一半，
        审计 P2-c）。写入侧修好而读取侧照旧，等于取证数据"存进去了但没人看得见"。
        """
        since = int(time.time() * 1000) - hours * 3_600_000
        try:
            pid = _resolve_project_id(agent_id, "")
            if pid is None:
                logger.warning("event_audit.timeline_unroutable", agent_id=agent_id)
                return []
            rows = await project_db.query_by_project(
                pid,
                """SELECT id, agent_id, event_type, payload, created_at
                   FROM agent_events
                   WHERE agent_id = ? AND created_at > ?
                   ORDER BY created_at DESC LIMIT ?""",
                [agent_id, since, limit],
            )
            result = []
            for r in rows:
                d = dict(r)
                # Decode payload JSON for friendlier Python access
                if d.get("payload"):
                    try:
                        d["payload"] = json.loads(d["payload"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(d)
            return result
        except Exception as e:
            logger.warning("event_audit.timeline_failed",
                           agent_id=agent_id, error=str(e))
            return []

    async def _write(
        self,
        agent_id: str,
        project_id: str,
        event_type: str,
        payload: dict,
        *,
        resolved_pid: str | None | object = _UNRESOLVED,
        resolve_error: str | None = None,
    ) -> None:
        """Internal: write event to per-project DB (async, error-safe).

        路由**先选库、再写**（D67-1）：`_resolve_project_id` 是唯一的源选定点，
        选出 project_id 后统一走 `execute_by_project`（单一的写口 —— 不再有
        "按 agent_id 写 / 按 project_id 写"两条并存路径）。

        ``resolved_pid`` 由 ``log()`` 在**同步段**算好带进来（见那里的窗口说明）。
        不传（= `_UNRESOLVED`）时就地解析 —— 那是**直接调用本函数**的路径
        （测试、以及未来不想经过任务调度的调用方）。
        """
        if resolved_pid is _UNRESOLVED:
            try:
                resolved_pid = _resolve_project_id(agent_id, project_id)
            except Exception as e:  # noqa: BLE001
                resolve_error = str(e)
                resolved_pid = None
        if resolve_error is not None:
            # 本函数是 `asyncio.create_task` 的**任务体**：任何逃逸的异常都只会
            # 变成一句 "Task exception was never retrieved"，连堆栈都不一定看到。
            logger.warning(
                "event_audit.route_resolution_failed",
                agent_id=agent_id,
                project_id=project_id,
                event_type=event_type,
                error=resolve_error,
            )
        pid = resolved_pid
        if pid is None:
            # 两条判据都解析不出 ⇒ **有声**失败。过去的形态是 `execute(agent_id)`
            # 抛 `ProjectDbError` 后被这里吞成一句 warning —— 子代理 id 恰好
            # 100% 落在这条路上，于是 167.9 分钟里"每条事件都丢"（实测 36 条、
            # 跨 6 个父代理、非 sub- 计数 = 0），而统计口径里看总量又"正常"。
            logger.warning(
                "event_audit.write_failed",
                agent_id=agent_id,
                project_id=project_id,
                event_type=event_type,
                error="unroutable: agent_id 既不在正式路由也不在瞬态身份表，"
                      "且调用方传入的 project_id 为空",
            )
            return

        event_id = str(uuid.uuid4())
        created_at = int(time.time() * 1000)
        try:
            payload_json = json.dumps(payload)
        except (TypeError, ValueError):
            # ⚠ 这条兜底过去是**静默**的：不可序列化的 payload 会被整条替换成
            # {"error": "..."}，把真正有用的字段（如 llm_unknown_error_sample 的
            # body_preview）一起丢掉，而且没人知道。凡「悄悄丢数据」都补一条 warning
            # —— 否则失效只在很久以后以"某类事件怎么一条都没有"的形式暴露。
            logger.warning(
                "event_audit.payload_not_serializable",
                agent_id=agent_id,
                event_type=event_type,
                keys=sorted(payload.keys()) if isinstance(payload, dict) else None,
            )
            payload_json = json.dumps(
                {"error": "payload not serializable"}
            )
        try:
            await project_db.execute_by_project(
                pid,
                """INSERT INTO agent_events
                   (id, agent_id, event_type, payload, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [event_id, agent_id, event_type, payload_json, created_at],
            )
        except Exception as e:
            logger.warning("event_audit.write_failed",
                           agent_id=agent_id, project_id=pid, error=str(e))


event_audit = EventAudit()
