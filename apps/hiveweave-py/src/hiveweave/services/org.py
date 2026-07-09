"""Organization service — agent CRUD and tree traversal (contract 04).

契约 04: 多 Agent 编排 (org 部分)
- agent 数据存在 Meta DB 的 agents 表 (契约 11 RECONCILE: 全局路由依赖)
- CRUD: create_agent / get_agent / update_agent / delete_agent
- 树遍历: get_subordinates / get_superior / get_full_tree
- resolve_agent: short_id (A007) → UUID exact → UUID prefix
- transfer_agent: 带环检测 (新父不能是后代)
- dismiss_agent: 软删除 (status=archived), 检查无下属
- generate_short_id: A001, A002, ... (TS 模式: 取 max+1)
- JSON 列 (skills / allowed_tools / ...) 自动编解码
"""

import json
import re
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db

log = structlog.get_logger(__name__)


def _fix_mojibake(s: str) -> str:
    """尝试修复双重编码的 UTF-8 字符串。

    BUG-009/012 修复：如果 str 看起来像 UTF-8 bytes 被当 latin-1/cp1252
    解码的结果（mojibake），尝试 encode + decode('utf-8') 还原。

    只在修复后包含 CJK 字符时才采用，避免误修合法的 latin-1 文本。
    """
    if not isinstance(s, str) or len(s) < 2:
        return s
    high_chars = sum(1 for c in s if ord(c) >= 0x80)
    if high_chars < 2 or high_chars < len(s) * 0.3:
        return s
    # 尝试 latin-1 和 cp1252(Windows-1252) 两种编码
    for encoding in ("latin-1", "cp1252"):
        try:
            fixed = s.encode(encoding).decode("utf-8")
            if any("\u4e00" <= c <= "\u9fff" for c in fixed):
                return fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return s


# Columns stored as JSON arrays in the agents table
_JSON_COLS = frozenset({
    "skills",
    "allowed_tools",
    "denied_tools",
    "ask_tools",
    "mcp_servers",
    "bound_skills",
})

# Scalar columns allowed in INSERT/UPDATE (excludes id/short_id/created_at
# which are handled explicitly)
_SCALAR_COLS = (
    "project_id",
    "name",
    "role",
    "parent_id",
    "module_id",
    "status",
    "goal",
    "backstory",
    "model_id",
    "permission_type",
    "permission_mode",
    "reasoning_effort",
    "workspace_path",
    "language",
)

_SHORT_ID_RE = re.compile(r"^[aA]\d{1,4}$")
_SHORT_ID_NUM_RE = re.compile(r"^A(\d+)$")


class OrgService:
    """Organization CRUD + tree traversal — agents live in Meta DB."""

    # ── CREATE ───────────────────────────────────────────────

    async def create_agent(self, attrs: dict) -> dict:
        """Create a new agent. Returns the created agent dict.

        Auto-generates ``id`` (UUID) and ``short_id`` (A001-style) if not
        provided. JSON list columns are encoded automatically.
        """
        agent_id = attrs.get("id") or str(uuid.uuid4())
        short_id = attrs.get("short_id") or await self.generate_short_id()
        now_ms = int(time.time() * 1000)

        cols: list[str] = ["id", "short_id", "created_at", "updated_at"]
        vals: list = [agent_id, short_id, now_ms, now_ms]

        for col in _SCALAR_COLS:
            if col in attrs:
                cols.append(col)
                vals.append(attrs[col])

        for col in _JSON_COLS:
            if col in attrs:
                cols.append(col)
                v = attrs[col]
                vals.append(json.dumps(v) if isinstance(v, list) else v)

        placeholders = ", ".join(["?"] * len(cols))
        col_list = ", ".join(cols)
        await meta_db.execute(
            f"INSERT INTO agents ({col_list}) VALUES ({placeholders})", vals
        )

        log.info("org.create_agent", agent_id=agent_id, short_id=short_id,
                 role=attrs.get("role"), name=attrs.get("name"))

        agent = await self.get_agent(agent_id)
        return agent or {"id": agent_id, "short_id": short_id}

    # ── READ ─────────────────────────────────────────────────

    async def get_agent(self, agent_id: str) -> dict | None:
        """Get agent by full UUID."""
        row = await meta_db.query_one(
            "SELECT * FROM agents WHERE id = ? LIMIT 1", [agent_id]
        )
        return self._row(row) if row else None

    async def get_agent_by_role(self, project_id: str, role: str) -> dict | None:
        """Get first active agent by role within a project."""
        row = await meta_db.query_one(
            "SELECT * FROM agents WHERE project_id = ? AND role = ? "
            "AND status != 'archived' LIMIT 1",
            [project_id, role],
        )
        return self._row(row) if row else None

    async def list_agents(self, project_id: str | None = None) -> list[dict]:
        """List all agents for a project (or all if project_id is None).

        Ordered by ``created_at`` ASC (oldest first).
        """
        if project_id is None:
            rows = await meta_db.query(
                "SELECT * FROM agents ORDER BY created_at ASC"
            )
        else:
            rows = await meta_db.query(
                "SELECT * FROM agents WHERE project_id = ? "
                "ORDER BY created_at ASC",
                [project_id],
            )
        return [d for r in rows if (d := self._row(r)) is not None]

    async def resolve_agent(self, agent_id_or_short_id: str) -> dict | None:
        """Resolve by short_id (A007), full UUID, or UUID prefix.

        Priority: shortId exact → UUID exact → UUID prefix (6-35 chars).
        """
        inp = agent_id_or_short_id.strip()

        # 1. short_id exact match (case-insensitive)
        if _SHORT_ID_RE.match(inp):
            norm = "A" + inp[1:].upper()
            row = await meta_db.query_one(
                "SELECT * FROM agents WHERE short_id = ? LIMIT 1", [norm]
            )
            if row:
                return self._row(row)

        # 2. UUID exact match
        agent = await self.get_agent(inp)
        if agent:
            return agent

        # 3. UUID prefix match (ambiguous → first match)
        if 6 <= len(inp) < 36:
            rows = await meta_db.query(
                "SELECT * FROM agents WHERE id LIKE ? LIMIT 2", [f"{inp}%"]
            )
            if rows:
                return self._row(rows[0])

        return None

    # ── UPDATE ───────────────────────────────────────────────

    async def update_agent(self, agent_id: str, attrs: dict) -> dict | None:
        """Update agent fields. Returns updated agent or None if not found.

        JSON list columns are encoded automatically. ``id`` and ``created_at``
        are immutable.
        """
        agent = await self.get_agent(agent_id)
        if not agent:
            return None

        sets: list[str] = []
        vals: list = []
        for k, v in attrs.items():
            if k in ("id", "created_at", "short_id"):
                continue
            if k in _JSON_COLS and isinstance(v, list):
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            vals.append(v)

        if not sets:
            return agent

        sets.append("updated_at = ?")
        vals.append(int(time.time() * 1000))
        vals.append(agent_id)

        await meta_db.execute(
            f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", vals
        )
        log.info("org.update_agent", agent_id=agent_id,
                 fields=list(attrs.keys()))
        return await self.get_agent(agent_id)

    async def update_status(self, agent_id: str, status: str) -> None:
        """Update only the status field (lightweight)."""
        await meta_db.execute(
            "UPDATE agents SET status = ?, updated_at = ? WHERE id = ?",
            [status, int(time.time() * 1000), agent_id],
        )

    async def update_parent(self, agent_id: str,
                            new_parent_id: str | None) -> None:
        """Update only the parent_id field (re-parenting)."""
        await meta_db.execute(
            "UPDATE agents SET parent_id = ?, updated_at = ? WHERE id = ?",
            [new_parent_id, int(time.time() * 1000), agent_id],
        )

    # ── DELETE ───────────────────────────────────────────────

    async def delete_agent(self, agent_id: str) -> dict:
        """Hard-delete an agent. Refuses if agent has subordinates.

        Returns ``{success}`` or ``{success: False, message}``.
        """
        agent = await self.get_agent(agent_id)
        if not agent:
            return {"success": False, "message": "Agent not found"}

        children = await self.get_subordinates(agent_id)
        if children:
            return {"success": False,
                    "message": f"Agent has {len(children)} subordinate(s). "
                               "Transfer or dismiss them first."}

        await meta_db.execute("DELETE FROM agents WHERE id = ?", [agent_id])
        log.info("org.delete_agent", agent_id=agent_id)
        return {"success": True}

    async def dismiss_agent(self, project_id: str, agent_id: str) -> dict:
        """Soft-delete (archive) an agent. Verifies no subordinates.

        Returns ``{success, agent}`` or ``{success: False, message}``.
        """
        children = await self.get_subordinates(agent_id)
        if children:
            return {"success": False,
                    "message": f"Cannot dismiss agent with {len(children)} "
                               "subordinate(s). Transfer or dismiss them first."}

        updated = await self.update_agent(agent_id, {"status": "archived"})
        if not updated:
            return {"success": False, "message": "Agent not found"}

        # A4 修复：清理该 agent 的所有 pending 闹钟，防止触发到已 archived agent
        try:
            from hiveweave.services.game_time import GameTimeService
            gt = GameTimeService()
            await gt.cancel_alarms_for_agent(project_id, agent_id)
        except Exception as e:
            log.warning("dismiss_cancel_alarms_failed",
                        agent_id=agent_id, error=str(e))

        log.info("org.dismiss_agent", agent_id=agent_id,
                 project_id=project_id)
        return {"success": True, "agent": updated}

    # ── TREE TRAVERSAL ───────────────────────────────────────

    async def get_subordinates(self, agent_id: str) -> list[dict]:
        """Get direct children of an agent (excludes archived)."""
        rows = await meta_db.query(
            "SELECT * FROM agents WHERE parent_id = ? AND status != 'archived'",
            [agent_id],
        )
        return [d for r in rows if (d := self._row(r)) is not None]

    async def get_superior(self, agent_id: str) -> dict | None:
        """Get the parent (superior) of an agent, or None if root."""
        agent = await self.get_agent(agent_id)
        if not agent or not agent.get("parent_id"):
            return None
        return await self.get_agent(agent["parent_id"])

    async def get_all_descendants(self, agent_id: str) -> list[dict]:
        """Get all descendants of an agent (recursive, excludes archived)."""
        children = await self.get_subordinates(agent_id)
        result = list(children)
        for child in children:
            result.extend(await self.get_all_descendants(child["id"]))
        return result

    async def get_full_tree(self, project_id: str) -> list[dict]:
        """Build the org tree for a project.

        Roots = active agents with no parent. Archived agents excluded.
        Each node has ``children`` (list or None if leaf).
        """
        agents = await self.list_agents(project_id)
        active = [a for a in agents if a.get("status") != "archived"]

        children_map: dict[str | None, list[dict]] = {}
        for a in active:
            pid = a.get("parent_id")
            children_map.setdefault(pid, []).append(a)

        def build(parent_id: str | None) -> list[dict]:
            kids = children_map.get(parent_id, [])
            nodes: list[dict] = []
            for kid in kids:
                sub = build(kid["id"])
                nodes.append({
                    "id": kid["id"],
                    "short_id": kid.get("short_id"),
                    "name": kid["name"],
                    "role": kid["role"],
                    "status": kid.get("status", "active"),
                    "permission_type": kid.get("permission_type", "executor"),
                    "goal": kid.get("goal", ""),
                    "model_id": kid.get("model_id"),
                    "config": {
                        "model_id": kid.get("model_id"),
                        "system_prompt": kid.get("system_prompt"),
                        "permission_type": kid.get("permission_type", "executor"),
                    },
                    "children": sub if sub else None,
                })
            return nodes

        return build(None)

    # ── TRANSFER ─────────────────────────────────────────────

    async def transfer_agent(self, project_id: str, agent_id: str,
                             new_parent_id: str | None) -> dict | None:
        """Transfer agent to a new parent. Verifies no cycle.

        Returns the updated agent dict, or ``{success: False, message}`` on
        cycle detection. ``new_parent_id=None`` makes the agent a root.
        """
        if agent_id == new_parent_id:
            return {"success": False,
                    "message": "Cannot transfer an agent to be its own parent"}

        if new_parent_id is not None:
            descendants = await self.get_all_descendants(agent_id)
            if any(d["id"] == new_parent_id for d in descendants):
                return {"success": False,
                        "message": "Cannot transfer: new parent is a descendant "
                                   "(would create a cycle)"}

        return await self.update_agent(agent_id, {"parent_id": new_parent_id})

    # ── SHORT ID ─────────────────────────────────────────────

    async def generate_short_id(self) -> str:
        """Generate next short ID (A001, A002, ...).

        Finds the current maximum A-number and increments (TS pattern).
        """
        rows = await meta_db.query("SELECT short_id FROM agents")
        max_num = 0
        for r in rows:
            sid = r["short_id"]
            if not sid:
                continue
            m = _SHORT_ID_NUM_RE.match(sid)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num
        return f"A{str(max_num + 1).zfill(3)}"

    # ── HELPERS ──────────────────────────────────────────────

    @staticmethod
    def _row(row) -> dict | None:
        """Convert a DB row to a dict, decoding JSON columns to lists."""
        if row is None:
            return None
        d = dict(row)
        for col in _JSON_COLS:
            v = d.get(col)
            if isinstance(v, str):
                try:
                    d[col] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    d[col] = []
            elif v is None:
                d[col] = []
        # BUG-009/012 修复：防御性 mojibake 修复。
        # 如果 str 字段看起来像 UTF-8 bytes 被当 latin-1 解码的结果
        # （双重编码），尝试 encode('latin-1').decode('utf-8') 还原。
        # 只在修复后包含 CJK 字符时才采用，避免误修合法的 latin-1 文本。
        for k, v in d.items():
            if isinstance(v, str) and len(v) >= 2:
                d[k] = _fix_mojibake(v)
        return d
