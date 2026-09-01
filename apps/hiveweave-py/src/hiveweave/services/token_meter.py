"""TokenMeter — per-agent LLM token metering service.

每行 llm_usage = 一次 LLM 请求，归属 agent/run/task/project 四级，覆盖
主对话 / 压缩 / 子代理三条调用路径。所有方法 best-effort：错误仅记日志、
绝不阻塞主流程（与 run_ledger 一致）。

写入路径:
- record_rounds: 主对话 / 子代理，带 usage_rounds 列表（每轮一条）
- record_compaction: 压缩调用（绕过 Streamer，单条）

聚合路径:
- agent_summary / project_by_agent / daily_summary / run_summary / platform_overview

缓存量程（P2-⑨）: ``cache_creation_tokens`` 只有 Anthropic 系上报，OpenAI 系
线上根本不带该字段。因此聚合结果附 ``cache_creation_scope``
（reported/unreported/mixed）+ ``cache_hit_basis``，让命中率能说清分母 ——
不新增列，能力位由既有 ``provider`` 列按 ``llm.util`` 的单一判据现算。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.llm.util import (
    cache_hit_percent,
    provider_reports_cache_creation,
)

log = structlog.get_logger()

#: 聚合 SQL 共用的 provider 收集列 — 能力位在 Python 侧按单一判据现算，
#: 不把 "anthropic" 之类的字面量复写进 SQL。
_PROVIDERS_AGG = "GROUP_CONCAT(DISTINCT provider) AS providers"

#: cache 写入量程：全部上报 / 全部不上报 / 混合。
CACHE_SCOPE_REPORTED = "reported"
CACHE_SCOPE_UNREPORTED = "unreported"
CACHE_SCOPE_MIXED = "mixed"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row_get(row: Any, key: str) -> Any:
    """读聚合行的某列 — sqlite3.Row 无 .get()，缺列时返回 None。"""
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def cache_creation_scope(providers: Any) -> str:
    """按聚合内出现过的 provider 判定 cache 写入量程。

    ``providers`` 为 GROUP_CONCAT 的 CSV（可能为 None/空）。无 provider 记录
    时按 unreported 处理 —— 未知不得当成「确实为 0」。
    """
    names = [
        p.strip() for p in str(providers or "").split(",") if p and p.strip()
    ]
    if not names:
        return CACHE_SCOPE_UNREPORTED
    flags = {provider_reports_cache_creation(p) for p in names}
    if flags == {True}:
        return CACHE_SCOPE_REPORTED
    if flags == {False}:
        return CACHE_SCOPE_UNREPORTED
    return CACHE_SCOPE_MIXED


#: 命中率分母口径说明 — 与 cache_creation_scope 一一对应。
_CACHE_HIT_BASIS = {
    CACHE_SCOPE_REPORTED: "input+cache_read+cache_creation",
    CACHE_SCOPE_UNREPORTED:
        "input+cache_read (cache_creation not reported by provider)",
    CACHE_SCOPE_MIXED:
        "input+cache_read+cache_creation (partial: mixed providers)",
}


def _with_cache_scope(row: dict[str, Any]) -> dict[str, Any]:
    """把 providers CSV 换成量程字段 + 命中率（含分母口径说明）。"""
    scope = cache_creation_scope(row.pop("providers", None))
    row["cache_creation_scope"] = scope
    row["cache_hit_percent"] = cache_hit_percent(
        int(row.get("input_tokens") or 0),
        int(row.get("cache_read_tokens") or 0),
        int(row.get("cache_creation_tokens") or 0),
    )
    row["cache_hit_basis"] = _CACHE_HIT_BASIS[scope]
    return row


class TokenMeter:
    """Token 计量服务（per-project best-effort 写入 + 聚合查询）。"""

    # ── 写入 ─────────────────────────────────────────────────

    async def record_rounds(
        self,
        agent_id: str,
        project_id: str | None,
        rounds: list[dict],
        model_id: str | None = None,
        provider: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        request_type: str = "main",
    ) -> None:
        """批量落库一轮或多轮 usage（主对话 / 子代理路径）。

        rounds 为 streamer 返回的 usage_rounds，每项含
        input/output/cache_read/cache_creation/total/duration_ms。
        """
        if not rounds:
            return
        now = _now_ms()
        statements: list[tuple[str, list[Any]]] = []
        # F11：首请求冷启动标记 —— 该 run 第一条 usage 记录 cache_read=0 且
        # cache_creation=0（cache 全未命中，前缀重建成本无账）→ 打 cold_start=1。
        # provider 不上报 cache_creation 时（OpenAI 系），cache_read=0 且
        # input>0 即冷启动。让 r4 的「20 run 首请求零命中 / 1.7M tokens 无账」
        # 变成可查数字。
        first_round = rounds[0] if isinstance(rounds[0], dict) else None
        is_cold_start = bool(
            first_round
            and int(first_round.get("cache_read", 0) or 0) == 0
            and int(first_round.get("cache_creation", 0) or 0) == 0
            and int(first_round.get("input", 0) or 0) > 0
        )
        for i, r in enumerate(rounds):
            if not isinstance(r, dict):
                continue
            statements.append((
                "INSERT INTO llm_usage "
                "(id, agent_id, project_id, run_id, task_id, model_id, "
                "request_type, provider, input_tokens, output_tokens, "
                "cache_read_tokens, cache_creation_tokens, total_tokens, "
                "duration_ms, cold_start, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(uuid.uuid4()),
                    agent_id,
                    project_id,
                    run_id,
                    task_id,
                    model_id,
                    request_type,
                    provider,
                    int(r.get("input", 0) or 0),
                    int(r.get("output", 0) or 0),
                    int(r.get("cache_read", 0) or 0),
                    int(r.get("cache_creation", 0) or 0),
                    int(r.get("total", 0) or 0),
                    int(r.get("duration_ms", 0) or 0),
                    1 if (i == 0 and is_cold_start) else 0,
                    # P2 观测（八轮 TEST_DSH_38）：优先用 streamer 逐次记录
                    # 的真实时刻；缺失（旧路径/异常分支）才退回批量盖章时刻。
                    int(r.get("ts") or now),
                ],
            ))
        if not statements:
            return
        try:
            await project_db.execute_transaction(agent_id, statements)  # type: ignore[arg-type]
        except Exception as e:
            # 正交结果独立上报（DSH defensive-patterns:7-9 戒律）：记账失败
            # 不得随 best-effort 静默蒸发 —— 升级 error 日志 + agent_events 落
            # usage_recorder_failed 告警事件。TEST_DSH_37 P0-① 曾让 274 次调用
            # 零记账且无任何痕迹，Token 页/命中率/税率全盲。
            log.error(
                "token_meter.record_rounds_failed",
                agent_id=agent_id,
                n=len(statements),
                request_type=request_type,
                run_id=run_id,
                error=str(e),
            )
            try:
                await project_db.execute(
                    agent_id,
                    "INSERT INTO agent_events "
                    "(id, agent_id, event_type, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        str(uuid.uuid4()),
                        agent_id,
                        "usage_recorder_failed",
                        json.dumps({
                            "n_rounds": len(statements),
                            "request_type": request_type,
                            "run_id": run_id,
                            "error": str(e)[:500],
                        }),
                        _now_ms(),
                    ],
                )
            except Exception as ev_err:
                log.error(
                    "token_meter.failure_event_write_failed",
                    agent_id=agent_id,
                    error=str(ev_err),
                )

    async def record_compaction(
        self,
        agent_id: str,
        model_id: str | None,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        kind: str = "conversation",
        provider: str | None = None,
    ) -> None:
        """落库一次压缩 LLM 调用（绕过 Streamer，F3 修正）。

        kind: "conversation" | "memory" → request_type compaction_<kind>。
        total 口径与主/子代理一致 = input + output + cache_creation
        （cache_read 单独列示，不计入 total）。
        压缩调用无 run_id（在 run 外执行），project_id 由 agent 解析。
        """
        try:
            project_id = await meta_db.get_agent_project_id(agent_id)
        except Exception as e:
            log.warning("token_meter.compaction_project_lookup_failed",
                        agent_id=agent_id, error=str(e))
            project_id = None
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        cache_read_tokens = int(cache_read_tokens or 0)
        cache_creation_tokens = int(cache_creation_tokens or 0)
        total = input_tokens + output_tokens + cache_creation_tokens
        try:
            await project_db.execute(
                agent_id,
                "INSERT INTO llm_usage "
                "(id, agent_id, project_id, run_id, task_id, model_id, "
                "request_type, provider, input_tokens, output_tokens, "
                "cache_read_tokens, cache_creation_tokens, total_tokens, "
                "duration_ms, created_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                [
                    str(uuid.uuid4()),
                    agent_id,
                    project_id,
                    model_id,
                    f"compaction_{kind}",
                    provider,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_creation_tokens,
                    total,
                    _now_ms(),
                ],
            )
        except Exception as e:
            # 与 record_rounds 同口径（正交结果独立上报）：压缩记账失败也
            # 不得静默蒸发 —— error 日志 + agent_events 落告警事件。
            log.error(
                "token_meter.record_compaction_failed",
                agent_id=agent_id,
                kind=kind,
                error=str(e),
            )
            try:
                await project_db.execute(
                    agent_id,
                    "INSERT INTO agent_events "
                    "(id, agent_id, event_type, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        str(uuid.uuid4()),
                        agent_id,
                        "usage_recorder_failed",
                        json.dumps({
                            "n_rounds": 1,
                            "request_type": f"compaction_{kind}",
                            "run_id": None,
                            "error": str(e)[:500],
                        }),
                        _now_ms(),
                    ],
                )
            except Exception as ev_err:
                log.error(
                    "token_meter.failure_event_write_failed",
                    agent_id=agent_id,
                    error=str(ev_err),
                )

    # ── 聚合查询 ─────────────────────────────────────────────

    @staticmethod
    def _row_to_summary(r: Any) -> dict[str, Any]:
        """把聚合行转成统一响应口径（含缓存量程口径）。"""
        summary: dict[str, Any] = {
            "llm_calls": int(r["llm_calls"] or 0),
            "input_tokens": int(r["input_tokens"] or 0),
            "output_tokens": int(r["output_tokens"] or 0),
            "cache_read_tokens": int(r["cache_read_tokens"] or 0),
            "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
            "total_tokens": int(r["total_tokens"] or 0),
            "duration_ms": int(r["duration_ms"] or 0),
            "providers": _row_get(r, "providers"),
        }
        return _with_cache_scope(summary)

    async def agent_summary(
        self,
        project_id: str,
        agent_id: str,
        since: int | None = None,
        until: int | None = None,
    ) -> dict[str, Any]:
        """单 agent 的 token 汇总（可带时间窗）。"""
        sql = (
            "SELECT COUNT(*) AS llm_calls, "
            "SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_creation_tokens) AS cache_creation_tokens, "
            "SUM(total_tokens) AS total_tokens, "
            "SUM(duration_ms) AS duration_ms, "
            f"{_PROVIDERS_AGG} "
            "FROM llm_usage WHERE project_id = ? AND agent_id = ?"
        )
        params: list[Any] = [project_id, agent_id]
        if since:
            sql += " AND created_at >= ?"
            params.append(since)
        if until:
            sql += " AND created_at <= ?"
            params.append(until)
        try:
            rows = await project_db.query_by_project(project_id, sql, params)
        except Exception as e:
            log.warning("token_meter.agent_summary_failed",
                        project_id=project_id, agent_id=agent_id, error=str(e))
            return TokenMeter._row_to_summary(_empty_row())
        return TokenMeter._row_to_summary(rows[0] if rows else _empty_row())

    async def project_by_agent(
        self, project_id: str, since: int | None = None
    ) -> list[dict[str, Any]]:
        """按 agent 分组的 token 汇总（含 request_type 拆分）。"""
        sql = (
            "SELECT agent_id, request_type, "
            "COUNT(*) AS llm_calls, "
            "SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_creation_tokens) AS cache_creation_tokens, "
            "SUM(total_tokens) AS total_tokens, "
            "SUM(duration_ms) AS duration_ms, "
            f"{_PROVIDERS_AGG} "
            "FROM llm_usage WHERE project_id = ?"
        )
        params: list[Any] = [project_id]
        if since:
            sql += " AND created_at >= ?"
            params.append(since)
        sql += " GROUP BY agent_id, request_type ORDER BY total_tokens DESC"
        try:
            rows = await project_db.query_by_project(project_id, sql, params)
        except Exception as e:
            log.warning("token_meter.project_by_agent_failed",
                        project_id=project_id, error=str(e))
            return []
        return [_with_cache_scope(dict(r)) for r in rows]

    async def daily_summary(
        self, project_id: str, since_days: int = 30
    ) -> list[dict[str, Any]]:
        """按天分组的 token 汇总（近 N 天）。"""
        since = _now_ms() - since_days * 86400_000
        sql = (
            "SELECT date(created_at / 1000, 'unixepoch', 'localtime') AS day, "
            "COUNT(*) AS llm_calls, "
            "SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_creation_tokens) AS cache_creation_tokens, "
            "SUM(total_tokens) AS total_tokens, "
            f"{_PROVIDERS_AGG} "
            "FROM llm_usage WHERE project_id = ? AND created_at >= ? "
            "GROUP BY day ORDER BY day ASC"
        )
        try:
            rows = await project_db.query_by_project(
                project_id, sql, [project_id, since]
            )
        except Exception as e:
            log.warning("token_meter.daily_summary_failed",
                        project_id=project_id, error=str(e))
            return []
        return [_with_cache_scope(dict(r)) for r in rows]

    async def run_summary(self, run_id: str) -> dict[str, Any] | None:
        """单次 run 的 token 归因（跨项目扫描按 run_id 定位）。"""
        out: list[dict[str, Any]] = []
        for proj in await _list_projects():
            pid = proj["id"]
            try:
                rows = await project_db.query_by_project(
                    pid,
                    "SELECT agent_id, request_type, "
                    "COUNT(*) AS llm_calls, "
                    "SUM(input_tokens) AS input_tokens, "
                    "SUM(output_tokens) AS output_tokens, "
                    "SUM(cache_read_tokens) AS cache_read_tokens, "
                    "SUM(cache_creation_tokens) AS cache_creation_tokens, "
                    "SUM(total_tokens) AS total_tokens, "
                    "SUM(duration_ms) AS duration_ms, "
                    f"{_PROVIDERS_AGG} "
                    "FROM llm_usage WHERE run_id = ? "
                    "GROUP BY agent_id, request_type",
                    [run_id],
                )
            except Exception as e:
                log.warning("token_meter.run_scan_failed",
                            project_id=pid, run_id=run_id, error=str(e))
                continue
            for r in rows:
                out.append(_with_cache_scope({
                    "project_id": pid,
                    "project_name": proj.get("name"),
                    **dict(r),
                }))
        if not out:
            return None
        return {"run_id": run_id, "entries": out}

    async def platform_overview(
        self, since: int | None = None
    ) -> list[dict[str, Any]]:
        """跨项目聚合：遍历各 per-project DB，按 agent 汇总。"""
        out: list[dict[str, Any]] = []
        for proj in await _list_projects():
            pid = proj["id"]
            sql = (
                "SELECT agent_id, COUNT(*) AS llm_calls, "
                "SUM(input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens, "
                "SUM(cache_read_tokens) AS cache_read_tokens, "
                "SUM(cache_creation_tokens) AS cache_creation_tokens, "
                "SUM(total_tokens) AS total_tokens, "
                f"{_PROVIDERS_AGG} "
                "FROM llm_usage WHERE 1=1"
            )
            params: list[Any] = []
            if since:
                sql += " AND created_at >= ?"
                params.append(since)
            sql += " GROUP BY agent_id"
            try:
                rows = await project_db.query_by_project(pid, sql, params)
            except Exception as e:
                log.warning("token_meter.platform_scan_failed",
                            project_id=pid, error=str(e))
                continue
            for r in rows:
                out.append(_with_cache_scope({
                    "project_id": pid,
                    "project_name": proj.get("name"),
                    "agent_id": r["agent_id"],
                    "llm_calls": int(r["llm_calls"] or 0),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "providers": _row_get(r, "providers"),
                }))
        out.sort(key=lambda x: x["total_tokens"], reverse=True)
        return out


async def _list_projects() -> list[dict[str, Any]]:
    """列出所有项目（平台级聚合用）。"""
    try:
        rows = await meta_db.query("SELECT id, name FROM projects")
    except Exception as e:
        log.warning("token_meter.list_projects_failed", error=str(e))
        return []
    return [dict(r) for r in rows]


def _empty_row() -> dict[str, Any]:
    return {
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
        "duration_ms": 0,
    }


# Singleton
token_meter = TokenMeter()