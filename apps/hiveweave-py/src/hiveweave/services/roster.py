"""Roster service — personnel records management.

契约 17: 人事花名册
- 维护每个 agent 的人事记录（职位/部门/职责/状态/入职日期）
- update_roster 按 (project_id, agent_id) upsert（单连接下 DELETE+INSERT 安全）
- get_roster 返回格式化文本供 coordinator/HR 审查

schema.py 的 personnel_records 表缺 hire_date 列，首次访问时 ALTER TABLE 补齐（幂等）。
"""

import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

# Idempotent migration tracking: project_ids whose schema has been checked
_migrated: set[str] = set()


async def _conn(project_id: str):
    """Resolve project_id to per-project DB connection."""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ValueError(f"Workspace not found for project {project_id}")
    return await project_db.ensure_project_db(workspace)


async def _ensure_schema(project_id: str) -> None:
    """Add missing hire_date column to personnel_records table (idempotent)."""
    if project_id in _migrated:
        return
    conn = await _conn(project_id)
    try:
        await conn.execute("ALTER TABLE personnel_records ADD COLUMN hire_date TEXT")
        await conn.commit()
    except Exception:
        pass  # Column already exists — safe to ignore
    _migrated.add(project_id)


class RosterService:
    """Personnel records CRUD — HR manages agent roster.

    所有 DB 操作路由到 per-project DB（通过 project_id → workspace_path）。
    """

    async def create(self, project_id: str, agent_id: str, attrs: dict) -> dict:
        """Create a personnel record. Returns the created record dict.

        attrs 字段: position, department, responsibilities, status, hire_date, updated_by
        """
        await _ensure_schema(project_id)
        record_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        position = attrs.get("position", "")
        department = attrs.get("department", "")
        responsibilities = attrs.get("responsibilities", "")
        status = attrs.get("status", "active")
        hire_date = attrs.get("hire_date", "")
        updated_by = attrs.get("updated_by", agent_id)

        conn = await _conn(project_id)
        await conn.execute(
            "INSERT INTO personnel_records (id, project_id, agent_id, position, "
            "department, responsibilities, status, hire_date, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [record_id, project_id, agent_id, position, department,
             responsibilities, status, hire_date, updated_by, now_ms])
        await conn.commit()
        log.info("roster_created", project_id=project_id, agent_id=agent_id,
                 position=position, department=department)
        return {
            "id": record_id, "project_id": project_id, "agent_id": agent_id,
            "position": position, "department": department,
            "responsibilities": responsibilities, "status": status,
            "hire_date": hire_date, "updated_by": updated_by, "updated_at": now_ms,
        }

    async def get(self, project_id: str, agent_id: str) -> dict | None:
        """Get a personnel record by (project_id, agent_id). Returns None if not found."""
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "SELECT id, project_id, agent_id, position, department, responsibilities, "
            "notes, status, hire_date, updated_by, updated_at "
            "FROM personnel_records WHERE project_id = ? AND agent_id = ? LIMIT 1",
            [project_id, agent_id])
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def update(self, project_id: str, agent_id: str, attrs: dict) -> bool:
        """Update a personnel record by (project_id, agent_id).

        Only non-None fields are updated. Returns True if a row was affected.
        """
        await _ensure_schema(project_id)
        fields: list[str] = []
        params: list = []
        for key in ("position", "department", "responsibilities", "status",
                    "hire_date", "updated_by"):
            if key in attrs and attrs[key] is not None:
                fields.append(f"{key} = ?")
                params.append(attrs[key])
        if not fields:
            return False
        now_ms = int(time.time() * 1000)
        fields.append("updated_at = ?")
        params.append(now_ms)
        params.extend([project_id, agent_id])

        conn = await _conn(project_id)
        cursor = await conn.execute(
            f"UPDATE personnel_records SET {', '.join(fields)} "
            f"WHERE project_id = ? AND agent_id = ?", params)
        await conn.commit()
        ok = cursor.rowcount > 0
        await cursor.close()
        return ok

    async def delete(self, project_id: str, agent_id: str) -> bool:
        """Delete a personnel record by (project_id, agent_id). Returns True if deleted."""
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "DELETE FROM personnel_records WHERE project_id = ? AND agent_id = ?",
            [project_id, agent_id])
        await conn.commit()
        ok = cursor.rowcount > 0
        await cursor.close()
        return ok

    async def list_by_project(self, project_id: str) -> list[dict]:
        """List all personnel records for a project, ordered by department, position."""
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "SELECT id, project_id, agent_id, position, department, responsibilities, "
            "notes, status, hire_date, updated_by, updated_at "
            "FROM personnel_records WHERE project_id = ? "
            "ORDER BY department, position", [project_id])
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    # ── Contract high-level API ───────────────────────────────

    async def update_roster(self, project_id: str, agent_id: str, attrs: dict) -> str:
        """Upsert personnel record by (project_id, agent_id).

        契约 17: 单连接下 DELETE + INSERT 等效原子 upsert。
        status 默认 'active'，可由调用方传入覆盖（修复 E6）。
        """
        await _ensure_schema(project_id)
        record_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        position = attrs.get("position", "")
        department = attrs.get("department", "")
        responsibilities = attrs.get("responsibilities", "")
        status = attrs.get("status", "active")
        hire_date = attrs.get("hire_date", "")
        updated_by = attrs.get("updated_by", agent_id)

        conn = await _conn(project_id)
        await conn.execute(
            "DELETE FROM personnel_records WHERE project_id = ? AND agent_id = ?",
            [project_id, agent_id])
        await conn.execute(
            "INSERT INTO personnel_records (id, project_id, agent_id, position, "
            "department, responsibilities, status, hire_date, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [record_id, project_id, agent_id, position, department,
             responsibilities, status, hire_date, updated_by, now_ms])
        await conn.commit()
        log.info("roster_upserted", project_id=project_id, agent_id=agent_id,
                 position=position, department=department)
        return "Roster updated"

    async def get_roster(self, project_id: str) -> str:
        """Get formatted roster text for a project.

        契约 17: markdown 格式，每条含 name/short_id/role + position/department/...
        LEFT JOIN agents 取 name/role/short_id。空结果返回提示文本。
        """
        records = await self.list_by_project(project_id)
        if not records:
            return "Roster is empty. No personnel records found."

        entries: list[str] = []
        for r in records:
            agent = await meta_db.get_agent_by_id(r["agent_id"])
            if agent:
                name = agent.get("name", "")
                short_id = agent.get("short_id", "") or ""
                role = agent.get("role", "")
                if short_id:
                    header = f"{name} ({short_id}) — {role}"
                else:
                    header = f"{name} — {role}"
            else:
                header = r["agent_id"]
            lines = [
                header,
                f"  Position: {r.get('position') or ''}",
                f"  Department: {r.get('department') or ''}",
                f"  Responsibilities: {r.get('responsibilities') or ''}",
                f"  Status: {r.get('status') or 'active'}",
            ]
            entries.append("\n".join(lines))
        return "## Personnel Roster\n\n" + "\n---\n".join(entries)
