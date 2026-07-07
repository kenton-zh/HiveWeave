"""Charter service — project charter and enterprise goals management.

契约 14: 项目章程与企业目标
- agent_charters table in Meta DB (save: DELETE + INSERT in single transaction)
- goals workbook in projects.charter_json (JSON: objective/focus/keyResults/userInvolvement)
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

        # Use raw connection for transaction (DELETE + INSERT atomically)
        db = await meta_db.get_meta_db()
        try:
            await db.execute(
                "DELETE FROM agent_charters WHERE project_id = ?",
                [project_id],
            )
            await db.execute(
                """INSERT INTO agent_charters
                   (id, project_id, agent_id, title, content, status,
                    project_rules, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [charter_id, project_id, agent_id, title, body,
                 status, project_rules, now, now],
            )
            await db.commit()
        except Exception as e:
            # C1 fix: 事务失败必须 rollback，否则失败的 DELETE 残留在连接上
            # 会被后续操作的 commit 误提交，导致 charter 数据丢失
            await db.rollback()
            logger.error("charter.save_failed", project_id=project_id,
                         error=str(e))
            raise

        logger.info("charter.saved", project_id=project_id, title=title[:80])
        return charter_id

    async def read_charter(self, project_id: str) -> dict | None:
        """Read the current project charter. Returns dict with 'formatted' or None."""
        try:
            row = await meta_db.query_one(
                """SELECT id, project_id, agent_id, title, content, status,
                          project_rules, created_at, updated_at
                   FROM agent_charters WHERE project_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                [project_id],
            )
        except Exception:
            return None
        if row is None:
            return None
        d = dict(row)
        d["formatted"] = self._format_charter(d)
        return d

    async def read_goals(self, project_id: str) -> dict:
        """Read enterprise goals from projects.charter_json.

        Returns dict {objective, focus, keyResults, userInvolvement} or {}.
        Old-format plain text is wrapped as {objective: text}.
        """
        row = await meta_db.query_one(
            "SELECT charter_json FROM projects WHERE id = ?", [project_id]
        )
        if row is None or row["charter_json"] is None:
            return {}
        raw = row["charter_json"]
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            pass
        # Old format: raw text → wrap as objective
        return {"objective": raw, "focus": None, "keyResults": []}

    async def update_goals(self, project_id: str, goals: dict) -> None:
        """Update enterprise goals (merge with existing, bump version)."""
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

        await meta_db.execute(
            "UPDATE projects SET charter_json = ? WHERE id = ?",
            [goals_json, project_id],
        )
        self.touch_goals_version(project_id)
        logger.info("charter.goals_updated", project_id=project_id)

    # ── Goals dirty-flag sync ─────────────────────────────────

    def touch_goals_version(self, project_id: str) -> None:
        """Bump the goals version for a project (monotonic ns)."""
        self._goals_version[project_id] = time.monotonic_ns()

    def get_goals_version(self, project_id: str) -> int:
        """Get the current goals version (0 if never set)."""
        return self._goals_version.get(project_id, 0)

    async def get_agent_goals_version(self, agent_id: str) -> int:
        """Get the version an agent last read (0 if never read)."""
        project_id = await meta_db.get_agent_project_id(agent_id)
        if project_id is None:
            return 0
        return self._agent_goals_version.get((project_id, agent_id), 0)

    async def set_agent_goals_version(self, agent_id: str, version: int) -> None:
        """Mark that an agent has read the goals at the given version."""
        project_id = await meta_db.get_agent_project_id(agent_id)
        if project_id is None:
            return
        self._agent_goals_version[(project_id, agent_id)] = version

    def goals_dirty(self, agent_id: str, project_id: str) -> bool:
        """Check if an agent needs to re-read the goals.

        - v_cur == 0 (never versioned): dirty iff agent never read (v_read == 0)
        - v_cur != 0: dirty iff v_cur != v_read
        """
        v_cur = self._goals_version.get(project_id, 0)
        v_read = self._agent_goals_version.get((project_id, agent_id), 0)
        if v_cur == 0:
            return v_read == 0
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
