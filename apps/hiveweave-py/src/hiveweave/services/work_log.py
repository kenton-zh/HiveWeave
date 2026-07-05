"""Work log service — agent work activity logging.

契约 17: 工作日志
- 记录 agent 工作行为（discussion/completion/error/decision 四类粗粒度）
- write_work_log 写入；get_recent / get_logs 读取（newest first）
- get_since 增量读取（oldest first，created_at > since_ts）
- dispatch_task / approve_work / reject_work 高层封装
- details 字段 JSON 序列化（dict → JSON string）；读取时反序列化（失败返回 {}）

work_logs 表 schema 已完整（含 type/action/details/metadata/session_id/task_id），无需迁移。
"""

import json
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)


async def _conn(project_id: str):
    """Resolve project_id to per-project DB connection."""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ValueError(f"Workspace not found for project {project_id}")
    return await project_db.ensure_project_db(workspace)


class WorkLogService:
    """Work log CRUD — records agent work activity on per-project DB."""

    async def append_log(self, project_id: str, agent_id: str, log_type: str,
                         summary: str, session_id: str | None = None,
                         details: dict | str | None = None) -> str:
        """Write a work log entry. Returns the log ID.

        契约 17: write_work_log
        - type 缺省 → 'discussion'
        - details: dict → JSON 序列化；string → 直用；None → '{}'
        """
        log_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        if log_type is None:
            log_type = "discussion"
        if details is None:
            details_json = "{}"
        elif isinstance(details, str):
            details_json = details
        else:
            details_json = json.dumps(details, ensure_ascii=False)

        conn = await _conn(project_id)
        await conn.execute(
            "INSERT INTO work_logs (id, agent_id, project_id, session_id, type, "
            "summary, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [log_id, agent_id, project_id, session_id, log_type,
             summary, details_json, now_ms])
        await conn.commit()
        log.info("work_log_written", agent_id=agent_id, log_type=log_type,
                 summary=summary[:80])
        return log_id

    async def get_logs(self, project_id: str, agent_id: str,
                       limit: int = 20) -> list[dict]:
        """Get agent's work logs (newest first). Default limit 20.

        契约 17: get_agent_logs — get_recent 的别名。
        """
        return await self.get_recent(project_id, agent_id, limit)

    async def get_recent(self, project_id: str, agent_id: str,
                         limit: int = 10) -> list[dict]:
        """Get recent work logs for an agent (newest first). Default limit 10.

        契约 17: get_subordinate_logs
        - SELECT id, agent_id, type, summary, details, created_at
        - WHERE agent_id=? ORDER BY created_at DESC LIMIT ?
        - details 字段 JSON 反序列化（失败返回 {}）
        """
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "SELECT id, agent_id, type, summary, details, created_at "
            "FROM work_logs WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            [agent_id, limit])
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_log(r) for r in rows]

    async def get_since(self, project_id: str, agent_id: str,
                        since_ts: int) -> list[dict]:
        """Get work logs since a timestamp (oldest first, for incremental reads).

        契约 17: get_subordinate_logs_since
        - WHERE created_at > since_ts ORDER BY created_at ASC
        """
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "SELECT id, agent_id, type, summary, details, created_at "
            "FROM work_logs WHERE agent_id = ? AND created_at > ? "
            "ORDER BY created_at ASC",
            [agent_id, since_ts])
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_log(r) for r in rows]

    # ── Contract high-level API ───────────────────────────────

    async def write_work_log(self, project_id: str, agent_id: str,
                             session_id: str | None, log_type: str | None,
                             summary: str,
                             details: dict | str | None = None) -> str:
        """契约 17: write_work_log — full signature with session_id."""
        return await self.append_log(
            project_id, agent_id, log_type or "discussion", summary,
            session_id=session_id, details=details)

    async def dispatch_task(self, project_id: str, from_agent_id: str,
                            to_agent_id: str, description: str,
                            session_id: str | None = None) -> dict:
        """契约 17: dispatch_task — write a discussion log on task dispatch.

        details = {from_agent_id, to_agent_id, description}
        Returns {task_id, from_agent_id, to_agent_id, description}
        """
        details = {
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "description": description,
        }
        log_id = await self.append_log(
            project_id, from_agent_id, "discussion", description,
            session_id=session_id, details=details)
        return {
            "task_id": log_id,
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "description": description,
        }

    async def approve_work(self, project_id: str, coordinator_id: str,
                           session_id: str | None, subordinate_id: str,
                           review: str | None = None) -> str:
        """契约 17: approve_work — write a completion log.

        details = {subordinate_id, review}
        """
        summary = f"Approved work of {subordinate_id}"
        if review:
            summary += f": {review}"
        details = {"subordinate_id": subordinate_id, "review": review}
        return await self.append_log(
            project_id, coordinator_id, "completion", summary,
            session_id=session_id, details=details)

    async def reject_work(self, project_id: str, coordinator_id: str,
                          session_id: str | None, subordinate_id: str,
                          feedback: str) -> str:
        """契约 17: reject_work — write an error log.

        details = {subordinate_id, feedback}
        """
        summary = f"Rejected work of {subordinate_id}: {feedback}"
        details = {"subordinate_id": subordinate_id, "feedback": feedback}
        return await self.append_log(
            project_id, coordinator_id, "error", summary,
            session_id=session_id, details=details)

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _row_to_log(row) -> dict:
        d = dict(row)
        d["details"] = WorkLogService._parse_json(d.get("details"))
        return d

    @staticmethod
    def _parse_json(s):
        if not s:
            return {}
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return {}
