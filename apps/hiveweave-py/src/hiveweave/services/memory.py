"""Three-layer memory service — project / agent / archive.

契约 05: 三层记忆
- project: 全员共享（宪章），30s 缓存
- agent: 单 agent 私有工作记忆，5min 缓存
- archive: 已解散 agent 冻结记忆，按 module_id 检索，5min 缓存

缓存：内存字典 + TTL（time.time 时间戳）。单进程时 TTL 足够保证可见性。
"""

import json
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ensure_project_db

log = structlog.get_logger(__name__)

# Cache TTL (seconds)
_PROJECT_TTL = 30.0       # 30s — project constitution changes frequently
_AGENT_TTL = 300.0        # 5min — agent private memories change rarely
_ARCHIVE_TTL = 300.0      # 5min — archives are write-once
_TRUNCATE_LEN = 200       # content truncation in build_agent_context

# In-memory cache: key tuple → (data, expires_at)
# key: (project_id, "project") | (project_id, "agent", agent_id, scope)
#      | (project_id, "archive", module_id)
_cache: dict[tuple, tuple[list, float]] = {}


class MemoryService:
    """Three-layer memory with TTL cache."""

    # ── Cache helpers ─────────────────────────────────────────

    @staticmethod
    def _cache_get(key: tuple) -> list[dict] | None:
        entry = _cache.get(key)
        if entry is None:
            return None
        data, expires = entry
        if time.time() > expires:
            _cache.pop(key, None)
            return None
        return data

    @staticmethod
    def _cache_put(key: tuple, data: list[dict], ttl: float) -> None:
        _cache[key] = (data, time.time() + ttl)

    @classmethod
    def invalidate(cls, project_id: str, *, agent_id: str | None = None,
                   scope: str | None = None, module_id: str | None = None) -> None:
        """Clear cached memories matching the given filters (契约 05: write 后失效).

        R5: 定向失效 — 只清除可能受写入影响的缓存，而非全项目清空。
        缓存 key 格式:
          - project 层: (project_id, "project")
          - agent 层:   (project_id, "agent", agent_id, scope)
          - archive 层: (project_id, "archive", module_id)

        - 不传过滤参数 → 清除该项目全部缓存（向后兼容）。
        - scope='project' → 只清 project 层。
        - agent_id 指定 → 只清该 agent 的缓存（可再用 scope 收窄）。
        - module_id 指定 → 只清该 module 的 archive 缓存。
        """
        to_remove = []
        for k in _cache:
            if k[0] != project_id:
                continue
            layer = k[1] if len(k) > 1 else None
            if layer == "project":
                # (project_id, "project")
                if scope == "project" or (
                    agent_id is None and scope is None and module_id is None
                ):
                    to_remove.append(k)
            elif layer == "agent":
                # (project_id, "agent", agent_id, scope)
                if agent_id is not None:
                    if k[2] == agent_id and (scope is None or k[3] == scope):
                        to_remove.append(k)
                elif scope == "agent" or (
                    agent_id is None and scope is None and module_id is None
                ):
                    to_remove.append(k)
            elif layer == "archive":
                # (project_id, "archive", module_id)
                if module_id is not None:
                    if k[2] == module_id:
                        to_remove.append(k)
                elif scope == "archive" or (
                    agent_id is None and scope is None and module_id is None
                ):
                    to_remove.append(k)
        for k in to_remove:
            _cache.pop(k, None)
        log.debug("memory_cache_invalidated", project_id=project_id,
                  agent_id=agent_id, scope=scope, module_id=module_id,
                  cleared=len(to_remove))

    # ── DB helper ─────────────────────────────────────────────

    @staticmethod
    async def _conn(project_id: str):
        """Resolve project_id to per-project DB connection."""
        workspace = await meta_db.get_project_workspace(project_id)
        if not workspace:
            raise ValueError(f"Workspace not found for project {project_id}")
        return await ensure_project_db(workspace)

    # ── Public API ────────────────────────────────────────────

    async def get_project_memories(self, project_id: str) -> list[dict]:
        """Get all project-scope memories (shared constitution). 30s TTL."""
        key = (project_id, "project")
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        conn = await self._conn(project_id)
        cursor = await conn.execute(
            "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, "
            "metadata, created_at, updated_at FROM memories WHERE scope = 'project' "
            "ORDER BY created_at ASC LIMIT 100")
        rows = await cursor.fetchall()
        await cursor.close()
        result = [self._row_to_memory(r) for r in rows]
        self._cache_put(key, result, _PROJECT_TTL)
        return result

    async def get_agent_memories(self, agent_id: str, project_id: str,
                                 scope: str = "agent") -> list[dict]:
        """Get an agent's memories for a given scope. 5min TTL."""
        key = (project_id, "agent", agent_id, scope)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        conn = await self._conn(project_id)
        cursor = await conn.execute(
            "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, "
            "metadata, created_at, updated_at FROM memories "
            "WHERE scope = ? AND agent_id = ? ORDER BY created_at ASC LIMIT 100",
            [scope, agent_id])
        rows = await cursor.fetchall()
        await cursor.close()
        result = [self._row_to_memory(r) for r in rows]
        self._cache_put(key, result, _AGENT_TTL)
        return result

    async def get_archived_memories(self, project_id: str, module_id: str) -> list[dict]:
        """Get archived memories for a module (from predecessors). 5min TTL."""
        key = (project_id, "archive", module_id)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        conn = await self._conn(project_id)
        cursor = await conn.execute(
            "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, "
            "metadata, created_at, updated_at FROM memories WHERE scope = 'archive' "
            "AND module_id = ? ORDER BY created_at ASC LIMIT 100", [module_id])
        rows = await cursor.fetchall()
        await cursor.close()
        result = [self._row_to_memory(r) for r in rows]
        self._cache_put(key, result, _ARCHIVE_TTL)
        return result

    async def add_entry(self, agent_id: str, project_id: str,
                         content: str, category: str = "tool_written",
                         module_id: str | None = None,
                         tags: list | None = None,
                         source_agent_id: str | None = None,
                         metadata: dict | None = None) -> str:
        """Write a memory entry (tool-facing alias for save_memory).

        Maps category → type for the underlying save_memory call.
        """
        scope = "agent"  # Tool-written memories default to agent scope
        return await self.save_memory(
            agent_id=agent_id, project_id=project_id, scope=scope,
            content=content, type=category, module_id=module_id,
            source_agent_id=source_agent_id,
            metadata=(metadata or {}) | ({"tags": tags} if tags else {}),
        )

    async def save_memory(self, agent_id: str, project_id: str, scope: str,
                          content: str, type: str = "fact", module_id: str | None = None,
                          source_agent_id: str | None = None,
                          metadata: dict | None = None) -> str:
        """Write a new memory entry and invalidate cache."""
        mem_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        meta_json = json.dumps(metadata) if metadata else "{}"
        conn = await self._conn(project_id)
        await conn.execute(
            "INSERT INTO memories (id, agent_id, scope, module_id, type, content, "
            "source_agent_id, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [mem_id, agent_id, scope, module_id, type, content,
             source_agent_id, meta_json, now_ms, now_ms])
        await conn.commit()
        # R5: 定向失效 — 只清受影响的缓存层，而非全项目
        self.invalidate(project_id, agent_id=agent_id, scope=scope,
                        module_id=module_id)
        log.info("memory_saved", scope=scope, type=type, agent_id=agent_id,
                 preview=content[:80])
        return mem_id

    async def archive_agent_memories(self, agent_id: str, project_id: str) -> int:
        """Archive an agent's private memories (scope: agent → archive)."""
        now_ms = int(time.time() * 1000)
        conn = await self._conn(project_id)
        cursor = await conn.execute(
            "UPDATE memories SET scope = 'archive', updated_at = ? "
            "WHERE agent_id = ? AND scope = 'agent'", [now_ms, agent_id])
        await conn.commit()
        count = max(cursor.rowcount, 0)
        await cursor.close()
        # R5: 只清该 agent 的私有缓存（archive 层由 TTL 自然过期）
        self.invalidate(project_id, agent_id=agent_id, scope="agent")
        log.info("memory_archived", agent_id=agent_id, count=count)
        return count

    async def build_agent_context(self, agent_id: str, project_id: str,
                                  module_id: str | None = None) -> str | None:
        """Build memory context string for system prompt injection.

        Each memory's content is truncated to 200 chars.
        Returns None when all three layers are empty (契约 05: 空时返回 nil).
        """
        blocks: list[str] = []

        project_mems = await self.get_project_memories(project_id)
        if project_mems:
            items = "\n".join(
                f"- [{m['type']}] {self._truncate(m['content'])}" for m in project_mems)
            blocks.append(f"## Project Constitution (Shared)\n{items}")

        agent_mems = await self.get_agent_memories(agent_id, project_id)
        if agent_mems:
            items = "\n".join(
                f"- [{m['type']}] {self._truncate(m['content'])}" for m in agent_mems)
            blocks.append(f"## Your Private Working Memory\n{items}")

        if module_id:
            archived = await self.get_archived_memories(project_id, module_id)
            if archived:
                items = "\n".join(
                    f"- [{m['type']}] {self._truncate(m['content'])}" for m in archived)
                blocks.append(
                    "## Archived Memories (from predecessors on this module)\n" + items)

        if not blocks:
            return None
        return "\n\n".join(blocks)

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _row_to_memory(row) -> dict:
        d = dict(row)
        d["metadata"] = MemoryService._parse_json(d.get("metadata"))
        return d

    @staticmethod
    def _truncate(text: str | None, length: int = _TRUNCATE_LEN) -> str:
        if not text:
            return ""
        return text[:length] + "..." if len(text) > length else text

    @staticmethod
    def _parse_json(s):
        if not s:
            return {}
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return {}
