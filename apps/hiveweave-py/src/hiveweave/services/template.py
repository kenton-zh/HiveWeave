"""Agent template service — template registry CRUD.

契约 18: TemplateService
- Meta DB 中的 agent_templates 表 CRUD
- list_all 支持 source/division/search 过滤，LIMIT 50
- get 含 prompt_body（完整模板）；list_all 不含 prompt_body（轻量列表）
- create 默认 source='custom', role='specialist'

agent_templates 表 schema 已完整，无需迁移。
"""

import time
import uuid

import structlog

from hiveweave.db import meta as meta_db

log = structlog.get_logger(__name__)

# 契约 18: list_templates LIMIT
_LIST_LIMIT = 50


class TemplateService:
    """Agent template registry — CRUD on Meta DB.

    所有操作路由到 Meta DB（全局单例）。
    """

    async def create(self, attrs: dict) -> dict:
        """Create a template. Returns {id, name}.

        契约 18: create_template
        - id 缺省 → UUID; source 缺省 → 'custom'; role 缺省 → 'specialist'
        - 其他字段缺省 → ''
        """
        template_id = attrs.get("id") or str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        source = attrs.get("source", "custom")
        division = attrs.get("division", "")
        name = attrs.get("name", "")
        role = attrs.get("role", "specialist")
        color = attrs.get("color", "")
        emoji = attrs.get("emoji", "")
        vibe = attrs.get("vibe", "")
        description = attrs.get("description", "")
        prompt_body = attrs.get("prompt_body", "")
        discipline_suite = attrs.get("discipline_suite", "")

        await meta_db.execute(
            "INSERT INTO agent_templates (id, source, division, name, role, "
            "color, emoji, vibe, description, prompt_body, discipline_suite, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [template_id, source, division, name, role, color, emoji, vibe,
             description, prompt_body, discipline_suite, now_ms, now_ms])
        log.info("template_created", template_id=template_id, name=name,
                 source=source)
        return {"id": template_id, "name": name}

    async def get(self, template_id: str) -> dict | None:
        """Get a template by ID (includes prompt_body).

        契约 18: get_template — 返回含 prompt_body 的完整模板。
        """
        row = await meta_db.query_one(
            "SELECT id, source, division, name, role, color, emoji, vibe, "
            "description, prompt_body, discipline_suite, created_at, updated_at "
            "FROM agent_templates WHERE id = ? LIMIT 1",
            [template_id])
        return dict(row) if row else None

    async def update(self, template_id: str, attrs: dict) -> bool:
        """Update a template. Only non-None fields. Returns True if a row was affected."""
        fields: list[str] = []
        params: list = []
        for key in ("source", "division", "name", "role", "color", "emoji",
                    "vibe", "description", "prompt_body", "discipline_suite"):
            if key in attrs and attrs[key] is not None:
                fields.append(f"{key} = ?")
                params.append(attrs[key])
        if not fields:
            return False
        now_ms = int(time.time() * 1000)
        fields.append("updated_at = ?")
        params.append(now_ms)
        params.append(template_id)

        db = await meta_db.get_meta_db()
        cursor = await db.execute(
            f"UPDATE agent_templates SET {', '.join(fields)} WHERE id = ?",
            params)
        await db.commit()
        ok = cursor.rowcount > 0
        await cursor.close()
        return ok

    async def delete(self, template_id: str) -> None:
        """Delete a template by ID."""
        await meta_db.execute(
            "DELETE FROM agent_templates WHERE id = ?", [template_id])
        log.info("template_deleted", template_id=template_id)

    async def list_all(self, opts: dict | None = None) -> list[dict]:
        """List templates with optional filtering. LIMIT 50.

        契约 18: list_templates
        - opts.source: 精确匹配 source
        - opts.division: 精确匹配 division
        - opts.search: name OR description LIKE %search%（模糊匹配）
        - ORDER BY source, division, name LIMIT 50
        - 不含 prompt_body（轻量列表）
        - 异常返回 []
        """
        opts = opts or {}
        conditions: list[str] = []
        params: list = []
        if opts.get("source"):
            conditions.append("source = ?")
            params.append(opts["source"])
        if opts.get("division"):
            conditions.append("division = ?")
            params.append(opts["division"])
        if opts.get("search"):
            conditions.append("(name LIKE ? OR description LIKE ?)")
            search = f"%{opts['search']}%"
            params.extend([search, search])
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

        try:
            rows = await meta_db.query(
                f"SELECT id, source, division, name, role, color, emoji, vibe, "
                f"description, discipline_suite, created_at, updated_at "
                f"FROM agent_templates{where} "
                f"ORDER BY source, division, name LIMIT {_LIST_LIMIT}",
                params)
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("list_templates_failed", error=str(e))
            return []
