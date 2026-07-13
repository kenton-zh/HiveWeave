"""Charter service — project charter and enterprise goals management.

契约 14: 项目章程与企业目标
- agent_charters table in per-project DB (save: DELETE + INSERT in single transaction)
- goals workbook in project_meta.goals_json (JSON: objective/focus/keyResults/userInvolvement)
- Goals dirty-flag sync via in-memory version dict (replaces Elixir ETS)
- userInvolvement default: "宏观决策+技术选型" (medium level)
- key_results normalization: string → {text, status:"doing", owner:nil}
"""

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ensure_project_db, get_project_db_by_project_id
from hiveweave.services.agent_router import agent_router

logger = structlog.get_logger()

DEFAULT_USER_INVOLVEMENT = "宏观决策+技术选型"  # medium level (契约 14 RECONCILE)


class CharterService:
    """Manages project charters and enterprise goals with dirty-flag sync."""

    def __init__(self) -> None:
        # In-memory goals version (replaces Elixir ETS :hiveweave_goals_sync)
        self._goals_version: dict[str, int] = {}
        self._agent_goals_version: dict[tuple[str, str], int] = {}

    async def save_charter(
        self, project_id: str, agent_id: str, content: str | dict
    ) -> str:
        """Save or update the project charter (DELETE + INSERT).

        content: str (charter body) or dict {title, content, status, project_rules}.
        Returns the new charter ID.

        Writes to the per-project DB (agent_charters table). The project_id
        is resolved via AgentRouter from agent_id to ensure correct routing.
        """
        if isinstance(content, dict):
            title = content.get("title", "Project Charter")
            body = content.get("content", "")
            status = content.get("status", "active")
            project_rules = content.get("project_rules", "")
        else:
            title = "Project Charter"
            body = str(content)
            status = "active"
            project_rules = ""

        charter_id = str(uuid.uuid4())
        now = int(time.time() * 1000)

        # Route to per-project DB via AgentRouter
        routed_pid = agent_router.get_project_id(agent_id)
        if routed_pid is None:
            raise ValueError(
                f"Agent {agent_id} not found in AgentRouter"
            )
        workspace = await meta_db.get_project_workspace(routed_pid)
        if workspace is None:
            raise ValueError(
                f"Workspace not found for project {routed_pid}"
            )
        conn = await ensure_project_db(workspace)
        if conn is None:
            raise ValueError(
                f"Per-project DB unavailable for workspace {workspace}"
            )

        # Use per-project DB connection for transaction (DELETE + INSERT atomically)
        try:
            await conn.execute(
                "DELETE FROM agent_charters WHERE project_id = ?",
                [routed_pid],
            )
            await conn.execute(
                """INSERT INTO agent_charters
                   (id, project_id, agent_id, title, content, status,
                    project_rules, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [charter_id, routed_pid, agent_id, title, body,
                 status, project_rules, now, now],
            )
            await conn.commit()
        except Exception as e:
            # C1 fix: 事务失败必须 rollback，否则失败的 DELETE 残留在连接上
            # 会被后续操作的 commit 误提交，导致 charter 数据丢失
            await conn.rollback()
            logger.error("charter.save_failed", project_id=routed_pid,
                         error=str(e))
            raise

        logger.info("charter.saved", project_id=routed_pid, title=title[:80])
        return charter_id

    async def read_charter(self, project_id: str) -> dict | None:
        """Read the current project charter from per-project DB.

        Returns dict with 'formatted' or None.
        """
        conn = await get_project_db_by_project_id(project_id)
        if conn is None:
            return None
        try:
            cursor = await conn.execute(
                """SELECT id, project_id, agent_id, title, content, status,
                          project_rules, created_at, updated_at
                   FROM agent_charters WHERE project_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                [project_id],
            )
            row = await cursor.fetchone()
            await cursor.close()
        except Exception:
            return None
        if row is None:
            return None
        d = dict(row)
        d["formatted"] = self._format_charter(d)
        return d

    async def read_goals(self, project_id: str) -> dict:
        """Read enterprise goals from project_meta.goals_json (agent-facing).

        Uses the dedicated goals_json column to avoid overwriting the human-facing
        charter_json. Falls back to charter_json.goals for old data.

        Returns dict {objective, focus, keyResults, userInvolvement} or {}.
        """
        conn = await get_project_db_by_project_id(project_id)
        if conn is None:
            return {}
        try:
            cursor = await conn.execute(
                "SELECT goals_json, charter_json FROM project_meta WHERE project_id = ?",
                [project_id],
            )
            row = await cursor.fetchone()
            await cursor.close()
        except Exception:
            return {}
        if row is None:
            return {}
        # Prefer goals_json (agent-facing column)
        raw = row["goals_json"]
        if raw:
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    return decoded
            except json.JSONDecodeError:
                pass
        # Fallback: extract goals from charter_json (human-facing, nested format)
        charter_raw = row["charter_json"]
        if charter_raw:
            try:
                charter = json.loads(charter_raw)
                if isinstance(charter, dict) and "goals" in charter:
                    return charter["goals"]
            except json.JSONDecodeError:
                pass
        return {}

    async def update_goals(self, project_id: str, goals: dict) -> None:
        """Update enterprise goals (merge with existing, bump version).

        Writes to project_meta.goals_json in the per-project DB.
        """
        existing = await self.read_goals(project_id)

        objective = goals.get("objective") or existing.get("objective", "")
        focus = goals.get("focus") or existing.get("focus", "")

        # Normalize key_results: accept string or object arrays
        kr_raw = goals.get("key_results") or goals.get("keyResults") or []
        if kr_raw:
            key_results = [self._normalize_kr(kr) for kr in kr_raw]
        else:
            key_results = existing.get("keyResults", [])

        user_involvement = (
            goals.get("user_involvement")
            or goals.get("userInvolvement")
            or existing.get("userInvolvement", DEFAULT_USER_INVOLVEMENT)
        )

        goals_json = json.dumps({
            "objective": objective,
            "focus": focus,
            "keyResults": key_results,
            "userInvolvement": user_involvement,
        })

        # Write to project_meta.goals_json in per-project DB (agent-facing).
        # Uses UPSERT to handle the case where the project_meta row doesn't exist yet.
        conn = await get_project_db_by_project_id(project_id)
        if conn is None:
            logger.warning(
                "goals_update_no_db", project_id=project_id
            )
            return
        now = int(time.time() * 1000)
        await conn.execute(
            """INSERT INTO project_meta (project_id, goals_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                   goals_json = excluded.goals_json,
                   updated_at = excluded.updated_at""",
            [project_id, goals_json, now],
        )
        await conn.commit()
        self.touch_goals_version(project_id)
        logger.info("charter.goals_updated", project_id=project_id)

        # 推送 WebSocket 事件 — 前端 GoalsPanel 监听后重新拉取
        try:
            from hiveweave.realtime.event_bus import status_event_bus
            await status_event_bus.publish_goals_updated(project_id)
        except Exception as e:
            logger.warning("goals_updated_push_failed", project_id=project_id, error=str(e))

    # ── Goals dirty-flag sync ─────────────────────────────────

    # Sentinel for "never initialized" — distinct from "synced to empty state".
    # Prevents 0==0 bug where agent synced to version 0 is always dirty.
    _VERSION_UNSET: int = -1

    def touch_goals_version(self, project_id: str) -> None:
        """Bump the goals version for a project (monotonic ns)."""
        self._goals_version[project_id] = time.monotonic_ns()

    def get_goals_version(self, project_id: str) -> int:
        """Get the current goals version (0 if never set)."""
        return self._goals_version.get(project_id, 0)

    async def get_agent_goals_version(self, agent_id: str) -> int:
        """Get the version an agent last read. Returns _VERSION_UNSET if never read."""
        project_id = await meta_db.get_agent_project_id(agent_id)
        if project_id is None:
            return self._VERSION_UNSET
        return self._agent_goals_version.get(
            (project_id, agent_id), self._VERSION_UNSET
        )

    async def set_agent_goals_version(self, agent_id: str, version: int) -> None:
        """Mark that an agent has read the goals at the given version."""
        project_id = await meta_db.get_agent_project_id(agent_id)
        if project_id is None:
            return
        self._agent_goals_version[(project_id, agent_id)] = version

    def goals_dirty(self, agent_id: str, project_id: str) -> bool:
        """Check if an agent needs to re-read the goals.

        Uses _VERSION_UNSET (-1) as sentinel for "never read" to avoid
        the 0==0 infinite-dirty bug after backend restart.
        """
        v_cur = self._goals_version.get(project_id, 0)
        v_read = self._agent_goals_version.get(
            (project_id, agent_id), self._VERSION_UNSET
        )
        # Never read at all → dirty
        if v_read == self._VERSION_UNSET:
            return True
        # Already read but version changed → dirty
        return v_cur != v_read

    # ── Helpers ───────────────────────────────────────────────

    def _normalize_kr(self, kr: Any) -> dict:
        """Normalize a key_result to {text, status, owner} object."""
        if isinstance(kr, str):
            return {"text": kr, "status": "doing", "owner": None}
        if isinstance(kr, dict):
            return {
                "text": kr.get("text", ""),
                "status": kr.get("status", "doing"),
                "owner": kr.get("owner"),
            }
        return {"text": str(kr), "status": "doing", "owner": None}

    def _format_charter(self, d: dict) -> str:
        """Build Markdown formatted string for the charter."""
        title = d.get("title", "Untitled")
        content = d.get("content", "")
        status = d.get("status", "unknown")
        created = d.get("created_at")
        time_str = "unknown"
        if created:
            try:
                dt = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
                time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                time_str = "unknown"
        return (
            f"## Project Charter: {title}\n\n"
            f"{content}\n\n"
            f"_Status: {status}, Created: {time_str}_"
        )


charter_service = CharterService()
