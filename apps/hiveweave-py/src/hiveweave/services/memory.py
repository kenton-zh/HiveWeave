"""Three-layer memory service — project / agent / archive.

契约 05: 三层记忆
- project: 全员共享（宪章），30s 缓存
- agent: 单 agent 私有工作记忆，5min 缓存
- archive: 已解散 agent 冻结记忆，按 module_id 检索，5min 缓存

缓存：内存字典 + TTL（time.time 时间戳）。单进程时 TTL 足够保证可见性。

注入模型（2026-08-01 用户钦定，替代每轮全量注入）：
- 每轮注入（System 2）：只注入 project 共享层（build_project_context）。
   agent 私有记忆不再每轮注入 —— 对话历史本身就包含 agent 自己写过的
   记忆内容（写入即入历史），重复注入浪费 token。
- 对话压缩后注入（compacted_prefix）：对话压缩（store._do_compaction）
   把旧轮次摘要化后，agent 私有记忆快照（build_agent_context：最新 10 条
   未压缩 + 压缩摘要）追加到压缩摘要系统消息末尾一次 —— 历史被压缩掉了，
   记忆需要补一次快照。快照随 compacted_prefix 持久化，重启后仍生效。
- 主动召回：read_memory 工具查全量（≤100 条，含被压缩的旧条目）。

Token 预算（窗口压缩）：
- 注入窗口：agent 层 = 最新 10 条未压缩记忆 + 1 条压缩摘要（合计 ≤11 条）。
- 压缩触发：仅随对话压缩同步触发（store._do_compaction 调
  maybe_compact_agent_memories），未压缩记忆 ≥20 条时，合并最老 8 条 +
  旧压缩摘要 → LLM 生成新压缩摘要（被压缩条目打 metadata.compressed=true
  标记，不删除，仍可经 read_memory 全量查询——历史不丢，只是不进注入窗口）。
- LLM 失败回退：硬裁剪——删除最老条目，保最新 10 条（有损但 token 有界）。
- 排序：created_at DESC（新记忆优先，修复原 ASC 导致新记忆被挤出窗口的问题）。
"""

import asyncio
import json
import time
import uuid
from typing import Awaitable, Callable

import aiosqlite
import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ProjectDbError, ensure_project_db

log = structlog.get_logger(__name__)

# Cache TTL (seconds)
_PROJECT_TTL = 30.0       # 30s — project constitution changes frequently
_AGENT_TTL = 300.0        # 5min — agent private memories change rarely
_ARCHIVE_TTL = 300.0      # 5min — archives are write-once
_TRUNCATE_LEN = 200       # content truncation in build_agent_context

# Token-budget window compression constants
_FRESH_MEMORY_MAX = 10     # 注入窗口：最新未压缩记忆条数
_COMPACT_TRIGGER = 20      # 未压缩记忆达到该条数才压缩（对话压缩同步触发）
_ARCHIVE_KEYS_INPUT = 8    # 每次压缩合并的最老条数（_COMPACT_TRIGGER - _FRESH_MEMORY_MAX - 2）

# 压缩摘要条目的 type 标记
_COMPRESSED_SUMMARY_TYPE = "compressed_summary"

# LLM 回调类型：(prompt: str) -> summary_text | None（对齐 conversation/compaction.py）
LLMCallback = Callable[[str], Awaitable[str | None]]

# In-memory cache: key tuple → (data, expires_at)
# key: (project_id, "project") | (project_id, "agent", agent_id, scope)
#      | (project_id, "archive", module_id)
_cache: dict[tuple, tuple[list, float]] = {}

# 压缩并发锁：key (project_id, agent_id) → asyncio.Lock（防同一 agent 重复压缩）
_compact_locks: dict[tuple, asyncio.Lock] = {}


def _compact_lock(project_id: str, agent_id: str) -> asyncio.Lock:
    key = (project_id, agent_id)
    lock = _compact_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _compact_locks[key] = lock
    return lock


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
                # 注：压缩摘要缓存 key 的 scope 段是 "compressed_summary"，
                # 视作该 agent 的一部分 — agent 相关失效时一并清除。
                if agent_id is not None:
                    if k[2] == agent_id and (
                        scope is None or k[3] == scope
                        or k[3] == _COMPRESSED_SUMMARY_TYPE
                    ):
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
    async def _conn(project_id: str) -> aiosqlite.Connection:
        """Resolve project_id to per-project DB connection."""
        workspace = await meta_db.get_project_workspace(project_id)
        if not workspace:
            raise ProjectDbError(f"Workspace not found for project {project_id}")
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
                                 scope: str = "agent",
                                 module_id: str | None = None) -> list[dict]:
        """Get an agent's fresh (uncompressed) memories for a given scope. 5min TTL.

        module_id: 可选模块过滤（匹配 memories.module_id 列）。BUG-P1a:
        此前工具层把 moduleId 错当 scope 传入，导致写入 scope='agent'
        的记忆永远读不回。

        窗口压缩：只返回未压缩记忆（metadata.compressed != true），
        新记忆优先（DESC），条数受 _FRESH_MEMORY_MAX 限制。
        被压缩的旧记忆可经 read_memory 工具全量查询，不在此返回。
        """
        key = (project_id, "agent", agent_id, scope, module_id)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        conn = await self._conn(project_id)
        # json_extract: SQLite 3.38+ 内置 JSON1。compressed=true 的记忆不进注入窗口。
        where = "scope = ? AND agent_id = ?"
        params: list = [scope, agent_id]
        if module_id:
            where += " AND module_id = ?"
            params.append(module_id)
        where += (
            " AND (json_extract(COALESCE(metadata, '{}'), '$.compressed') IS NOT 1) "
            "AND type != ?"
        )
        params.append(_COMPRESSED_SUMMARY_TYPE)
        cursor = await conn.execute(
            "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, "
            "metadata, created_at, updated_at FROM memories "
            f"WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            params + [_FRESH_MEMORY_MAX])
        rows = await cursor.fetchall()
        await cursor.close()
        result = [self._row_to_memory(r) for r in rows]
        self._cache_put(key, result, _AGENT_TTL)
        return result

    async def get_all_agent_memories(self, agent_id: str, project_id: str,
                                     module_id: str | None = None) -> list[dict]:
        """Get ALL of an agent's memories, including compressed/old ones.

        read_memory 工具全量查询用（窗口压缩后历史不丢，可主动召回）。
        排序：新记忆优先（DESC），上限 100。
        """
        conn = await self._conn(project_id)
        if module_id:
            cursor = await conn.execute(
                "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, "
                "metadata, created_at, updated_at FROM memories "
                "WHERE scope = 'agent' AND agent_id = ? AND module_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 100",
                [agent_id, module_id])
        else:
            cursor = await conn.execute(
                "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, "
                "metadata, created_at, updated_at FROM memories "
                "WHERE scope = 'agent' AND agent_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 100",
                [agent_id])
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_memory(r) for r in rows]

    async def get_compressed_summary(self, agent_id: str,
                                     project_id: str) -> dict | None:
        """Get the agent's compressed-summary entry (if any). 5min TTL."""
        key = (project_id, "agent", agent_id, "compressed_summary", None)
        cached = self._cache_get(key)
        if cached is not None:
            return cached[0] if cached else None
        conn = await self._conn(project_id)
        cursor = await conn.execute(
            "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, "
            "metadata, created_at, updated_at FROM memories "
            "WHERE scope = 'agent' AND agent_id = ? AND type = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            [agent_id, _COMPRESSED_SUMMARY_TYPE])
        row = await cursor.fetchone()
        await cursor.close()
        result = self._row_to_memory(row) if row else None
        self._cache_put(key, [result] if result else [], _AGENT_TTL)
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
        """Write a memory entry and invalidate cache.

        BUG-040: 当 module_id 非空时，使用 upsert 语义 —
        相同 (agent_id, scope, module_id) 的记录会被 UPDATE 而非 INSERT 新行。
        """
        now_ms = int(time.time() * 1000)
        meta_json = json.dumps(metadata) if metadata else "{}"
        conn = await self._conn(project_id)

        # Upsert: module_id 非空时先查已有记录
        existing_id = None
        if module_id:
            cursor = await conn.execute(
                "SELECT id FROM memories "
                "WHERE agent_id = ? AND scope = ? AND module_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                [agent_id, scope, module_id],
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                existing_id = row["id"]

        if existing_id:
            # UPDATE 已有记录
            await conn.execute(
                "UPDATE memories SET content = ?, type = ?, "
                "source_agent_id = ?, metadata = ?, updated_at = ? "
                "WHERE id = ?",
                [content, type, source_agent_id, meta_json, now_ms, existing_id],
            )
            mem_id = existing_id
        else:
            # INSERT 新记录
            mem_id = str(uuid.uuid4())
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
        # 注意：记忆压缩不在写入时触发（2026-08-01 用户钦定）——
        # 只随对话压缩同步触发一次（store._do_compaction 调
        # maybe_compact_agent_memories）。写入间可无限累积，DB 无压力，
        # 注入窗口恒为 10+1，不会吃 token。
        log.info("memory_saved", scope=scope, type=type, agent_id=agent_id,
                 preview=content[:80])
        return mem_id

    # ── 窗口压缩（token 预算）─────────────────────────────

    async def maybe_compact_agent_memories(
        self, agent_id: str, project_id: str
    ) -> bool:
        """对话压缩时同步调用：未压缩记忆 ≥_COMPACT_TRIGGER 才压缩一次。

        返回 True 表示执行了压缩（LLM 摘要成功或硬裁剪回退）。
        压缩不随写入触发（用户钦定 2026-08-01）；并发锁防重复压缩。
        连接来自共享连接池，只关 cursor，绝不 conn.close()。
        """
        try:
            async with _compact_lock(project_id, agent_id):
                if await self._count_fresh(agent_id, project_id) < _COMPACT_TRIGGER:
                    return False
                return await self._compact_agent_memories(agent_id, project_id)
        except Exception as e:
            log.warning("memory_compact_failed", agent_id=agent_id,
                        error=str(e))
            return False

    async def _count_fresh(self, agent_id: str, project_id: str) -> int:
        """未压缩记忆条数（排除 compressed_summary 条目）。"""
        conn = await self._conn(project_id)
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM memories "
            "WHERE scope = 'agent' AND agent_id = ? "
            "AND (json_extract(COALESCE(metadata, '{}'), '$.compressed') IS NOT 1) "
            "AND type != ?",
            [agent_id, _COMPRESSED_SUMMARY_TYPE],
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["n"] or 0) if row else 0

    async def _compact_agent_memories(
        self,
        agent_id: str,
        project_id: str,
    ) -> bool:
        """压缩 agent 记忆：最老 8 条 + 旧摘要 → LLM 新摘要；旧条目打标不删。

        成功返回 True（窗口回到 10+1）；LLM 失败时硬裁剪保最新 11 条。
        连接来自共享连接池，只关 cursor，绝不 conn.close()。
        """
        conn = await self._conn(project_id)

        # 1. 取最老的 _ARCHIVE_KEYS_INPUT 条未压缩记忆（ASC）
        cursor = await conn.execute(
            "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, "
            "metadata, created_at, updated_at FROM memories "
            "WHERE scope = 'agent' AND agent_id = ? "
            "AND (json_extract(COALESCE(metadata, '{}'), '$.compressed') IS NOT 1) "
            "AND type != ? "
            "ORDER BY created_at ASC, rowid ASC LIMIT ?",
            [agent_id, _COMPRESSED_SUMMARY_TYPE, _ARCHIVE_KEYS_INPUT],
        )
        old_rows = await cursor.fetchall()
        await cursor.close()

        # 2. 取旧压缩摘要（如有）— 合并进输入（直查 DB，不走 TTL 缓存）
        cursor = await conn.execute(
            "SELECT id, agent_id, scope, module_id, type, content, source_agent_id, "
            "metadata, created_at, updated_at FROM memories "
            "WHERE scope = 'agent' AND agent_id = ? AND type = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            [agent_id, _COMPRESSED_SUMMARY_TYPE],
        )
        row = await cursor.fetchone()
        await cursor.close()
        old_summary = self._row_to_memory(row) if row else None

        old_entries: list[dict] = [self._row_to_memory(r) for r in old_rows]
        if not old_entries:
            return False

        # 3. LLM 生成新摘要
        summary_text = None
        callback = await _resolve_compactor_callback(agent_id)
        if callback is not None:
            prompt = _build_memory_compaction_prompt(
                old_entries, old_summary
            )
            try:
                summary_text = await callback(prompt)
            except Exception as e:
                log.warning("memory_compact_llm_failed", agent_id=agent_id,
                            error=str(e))
                summary_text = None

        if summary_text and summary_text.strip():
            # 4a. LLM 成功：旧条目打 compressed 标记（不删），upsert 摘要条目。
            # 共享连接池（契约 11）：多语句写必须显式事务 + 异常回滚，
            # 否则遗留打开事务可能被其他协程意外 commit/rollback。
            now_ms = int(time.time() * 1000)
            old_ids = [e["id"] for e in old_entries]
            try:
                await conn.execute("BEGIN IMMEDIATE")
                if old_ids:
                    # 逐条标记 — SQLite 无批量 JSON 更新，逐条 UPDATE 安全
                    for oid in old_ids:
                        await conn.execute(
                            "UPDATE memories SET metadata = json_set(metadata, "
                            "'$.compressed', true), updated_at = ? WHERE id = ?",
                            [now_ms, oid],
                        )
                # 摘要条目 upsert：存在则 UPDATE，否则 INSERT。
                # 注意：摘要条目不标 compressed=true（该标记专指"被并入摘要的旧条目"；
                # 摘要自身靠 type=compressed_summary 排除出注入窗口）。
                meta_json = json.dumps({
                    "compressed_summary": True,
                    "source_agent_id": agent_id,
                    "archived_at_ms": now_ms,
                })
                summary_id = old_summary.get("id") if old_summary else None
                if summary_id:
                    await conn.execute(
                        "UPDATE memories SET content = ?, metadata = ?, "
                        "updated_at = ? WHERE id = ?",
                        [summary_text.strip()[:2000], meta_json, now_ms,
                         summary_id],
                    )
                else:
                    summary_id = str(uuid.uuid4())
                    await conn.execute(
                        "INSERT INTO memories (id, agent_id, scope, module_id, "
                        "type, content, source_agent_id, metadata, created_at, "
                        "updated_at) VALUES (?, ?, 'agent', NULL, ?, ?, ?, ?, ?, ?)",
                        [summary_id, agent_id, _COMPRESSED_SUMMARY_TYPE,
                         summary_text.strip()[:2000], agent_id, meta_json,
                         now_ms, now_ms],
                    )
                await conn.commit()
            except Exception:
                try:
                    await conn.rollback()
                except Exception:
                    pass
                raise
            self.invalidate(project_id, agent_id=agent_id, scope="agent")
            log.info(
                "memory_compacted",
                agent_id=agent_id,
                merged=len(old_entries),
                summary_chars=len(summary_text),
            )
            return True

        # 4b. LLM 失败回退：硬裁剪 — 删除最老的超窗口条目，保最新 11 条
        # （10 未压缩 + 1 摘要；摘要条目不计数）。
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM memories "
            "WHERE scope = 'agent' AND agent_id = ? "
            "AND (json_extract(COALESCE(metadata, '{}'), '$.compressed') IS NOT 1) "
            "AND type != ?",
            [agent_id, _COMPRESSED_SUMMARY_TYPE],
        )
        row = await cursor.fetchone()
        await cursor.close()
        total = int(row["n"] or 0) if row else 0
        excess = max(0, total - _FRESH_MEMORY_MAX)
        if excess:
            # 删最老 excess 条（保最新 _FRESH_MEMORY_MAX 条原文）
            try:
                await conn.execute("BEGIN IMMEDIATE")
                await conn.execute(
                    "DELETE FROM memories WHERE id IN ("
                    "SELECT id FROM memories WHERE scope = 'agent' "
                    "AND agent_id = ? "
                    "AND (json_extract(COALESCE(metadata, '{}'), '$.compressed') "
                    "IS NOT 1) "
                    "AND type != ? "
                    "ORDER BY created_at ASC, rowid ASC LIMIT ?)",
                    [agent_id, _COMPRESSED_SUMMARY_TYPE, excess],
                )
                await conn.commit()
            except Exception:
                try:
                    await conn.rollback()
                except Exception:
                    pass
                raise
            self.invalidate(project_id, agent_id=agent_id, scope="agent")
            log.info(
                "memory_compacted_fallback_trim",
                agent_id=agent_id,
                removed=excess,
            )
        return False

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

    async def build_project_context(self, project_id: str) -> str | None:
        """Build the project constitution (shared) memory block — per-turn inject.

        2026-08-01 注入模型：每轮 System 2 只注入 project 共享层；
        agent 私有层只在对话压缩后随压缩摘要注入一次。
        Returns None when empty (契约 05: 空时返回 nil).
        """
        project_mems = await self.get_project_memories(project_id)
        if not project_mems:
            return None
        items = "\n".join(
            f"- [{m['type']}] {self._truncate(m['content'])}" for m in project_mems)
        return f"## Project Constitution (Shared)\n{items}"

    async def build_agent_context(self, agent_id: str, project_id: str,
                                  module_id: str | None = None) -> str | None:
        """Build agent private memory snapshot — injected after conversation compaction.

        每轮不再注入（2026-08-01 用户钦定）：对话历史已含 agent 写过的记忆；
        压缩把旧轮次摘要化后，本快照（最新 10 条未压缩 + 压缩摘要 + archive）
        追加到 compacted_prefix 一次性注入。
        Returns None when empty (契约 05: 空时返回 nil).
        """
        blocks: list[str] = []

        agent_mems = await self.get_agent_memories(agent_id, project_id)
        if agent_mems:
            items = "\n".join(
                f"- [{m['type']}] {self._truncate(m['content'])}" for m in agent_mems)
            blocks.append(f"## Your Private Working Memory\n{items}")

        # 压缩摘要块：窗口之外的旧记忆被合并成摘要注入，具体条目可 read_memory 查
        summary = await self.get_compressed_summary(agent_id, project_id)
        if summary and (summary.get("content") or "").strip():
            blocks.append(
                "## Older Memories (compressed summary — use read_memory "
                "to query the full original entries)\n"
                + self._truncate(summary["content"], 800)
            )

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


# ── 记忆压缩 LLM 摘要 ─────────────────────────────────────


def _format_entry_for_summary(entry: dict) -> str:
    """格式化单条记忆为摘要输入行。"""
    meta = entry.get("metadata") or {}
    parts = [f"- [{entry.get('type', 'fact')}] {entry.get('content', '')}"]
    if meta.get("root_cause"):
        parts.append(f"  root_cause: {meta['root_cause']}")
    if meta.get("fix"):
        parts.append(f"  fix: {meta['fix']}")
    if meta.get("tags"):
        parts.append(f"  tags: {meta['tags']}")
    return "\n".join(parts)


def _build_memory_compaction_prompt(
    old_entries: list[dict],
    old_summary: dict | None,
) -> str:
    """构建记忆压缩 prompt — 把旧摘要 + 最老条目合并成新的压缩摘要。

    与 conversation compaction 不同：记忆压缩是"合并"，不是"淘汰"——
    原条目打标保留在库中（read_memory 可全量查），摘要只服务于注入窗口。
    """
    inputs: list[str] = []
    if old_summary and (old_summary.get("content") or "").strip():
        inputs.append(
            "## Previous compressed summary (older memories already summarized):\n"
            + old_summary["content"]
        )
    entries_text = "\n".join(
        _format_entry_for_summary(e) for e in old_entries
    )
    inputs.append(
        "## Memory entries to merge into the summary:\n" + entries_text
    )
    return (
        "You are compressing a working memory for an AI software-engineering "
        "agent. Merge the previous compressed summary (if any) with the memory "
        "entries below into ONE new compressed summary.\n\n"
        "## Rules\n"
        "- Preserve exact file paths, commands, error strings, decisions, and "
        "constraints — they are the most valuable parts.\n"
        "- Deduplicate: if the previous summary already covers an entry, keep "
        "it merged, do not repeat verbatim.\n"
        "- Use concise bullet points, group related facts.\n"
        "- Write in the same language as the entries (Chinese entries → Chinese).\n"
        "- Do NOT mention the summarization process itself.\n"
        "- The summary will be shown to the agent as 'Older Memories' context; "
        "the original full entries remain queryable via read_memory, so "
        "condense aggressively but keep actionable facts.\n\n"
        + "\n\n".join(inputs)
    )


async def _resolve_compactor_callback(agent_id: str) -> LLMCallback | None:
    """解析 agent 的 compactor LLM 回调（复用 conversation compaction 解析）。

    查询 Meta DB 获取 agent 的模型（或首个活跃模型），构建 OpenAI 兼容回调。
    无可用模型时返回 None（压缩将回退到硬裁剪）。
    """
    from hiveweave.conversation.compaction import resolve_compactor_callback

    try:
        return await resolve_compactor_callback(agent_id)
    except Exception as e:
        log.warning("memory_resolve_compactor_failed", agent_id=agent_id,
                    error=str(e))
        return None
