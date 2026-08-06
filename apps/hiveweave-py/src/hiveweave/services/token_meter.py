"""TokenMeter — per-agent LLM token metering service.

每行 llm_usage = 一次 LLM 请求，归属 agent/run/task/project 四级，覆盖
主对话 / 压缩 / 子代理三条调用路径。所有方法 best-effort：错误仅记日志、
绝不阻塞主流程（与 run_ledger 一致）。

写入路径:
- record_rounds: 主对话 / 子代理，带 usage_rounds 列表（每轮一条）
- record_compaction: 压缩调用（绕过 Streamer，单条）

聚合路径:
- agent_summary / project_by_agent / daily_summary / run_summary / platform_overview
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger()


def _now_ms() -> int:
    return int(time.time() * 1000)


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
        for r in rounds:
            if not isinstance(r, dict):
                continue
            statements.append((
                "INSERT INTO llm_usage "
                "(id, agent_id, project_id, run_id, task_id, model_id, "
                "request_type, provider, input_tokens, output_tokens, "
                "cache_read_tokens, cache_creation_tokens, total_tokens, "
                "duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    now,
                ],
            ))
        if not statements:
            return
        try:
            await project_db.execute_transaction(agent_id, statements)  # type: ignore[arg-type]
        except Exception as e:
            log.warning(
                "token_meter.record_rounds_failed",
                agent_id=agent_id,
                n=len(statements),
                error=str(e),
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
            log.warning("token_meter.record_compaction_failed",
                        agent_id=agent_id, error=str(e))

    # ── 聚合查询 ─────────────────────────────────────────────

    @staticmethod
    def _row_to_summary(r: Any) -> dict[str, int]:
        """把聚合行转成统一响应口径。"""
        return {
            "llm_calls": int(r["llm_calls"] or 0),
            "input_tokens": int(r["input_tokens"] or 0),
            "output_tokens": int(r["output_tokens"] or 0),
            "cache_read_tokens": int(r["cache_read_tokens"] or 0),
            "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
            "total_tokens": int(r["total_tokens"] or 0),
            "duration_ms": int(r["duration_ms"] or 0),
        }

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
            "SUM(duration_ms) AS duration_ms "
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
            "SUM(duration_ms) AS duration_ms "
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
        return [dict(r) for r in rows]

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
            "SUM(total_tokens) AS total_tokens "
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
        return [dict(r) for r in rows]

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
                    "SUM(duration_ms) AS duration_ms "
                    "FROM llm_usage WHERE run_id = ? "
                    "GROUP BY agent_id, request_type",
                    [run_id],
                )
            except Exception as e:
                log.warning("token_meter.run_scan_failed",
                            project_id=pid, run_id=run_id, error=str(e))
                continue
            for r in rows:
                out.append({
                    "project_id": pid,
                    "project_name": proj.get("name"),
                    **dict(r),
                })
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
                "SUM(total_tokens) AS total_tokens "
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
                out.append({
                    "project_id": pid,
                    "project_name": proj.get("name"),
                    "agent_id": r["agent_id"],
                    "llm_calls": int(r["llm_calls"] or 0),
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                })
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