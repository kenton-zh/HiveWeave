"""LessonService — experiential co-learning (跨任务经验沉淀).

ChatDev 式 Experiential Co-Learning 的 HiveWeave 落地：
- 归档：agent 在 commit_turn(done_slice) 时通过 extensions.lessons 显式提交
  "踩坑 → 根因 → 修复" 教训（LLM 主动沉淀，比从 work_log 自动提取可靠且便宜）。
- 召回：新任务 dispatch 时，trigger.context.build hook 按任务文本关键词
  召回 top-N 经验注入上下文，避免同样的坑被不同 agent 反复踩。
- 存储：复用 memories 表新层 scope='lesson'（项目内共享，全员可召回）。
  带 TTL 缓存（对齐契约 05 三层记忆的缓存语义）。

质量门：只收"验证过的根因"——归档方需给出根因 + 修复/规避动作，
空内容或纯吐槽不予归档。召回按 tags 重叠度 + 关键词命中排序。
"""

from __future__ import annotations

import json
import time
import uuid

import aiosqlite
import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import (
    ProjectDbError,
    ensure_project_db,
    get_workspace_write_lock,
)

log = structlog.get_logger(__name__)

_LESSON_TTL = 300.0       # 5min — lessons change rarely
_LESSON_LIMIT = 100       # 项目内 lesson 上限（防膨胀）
_LESSON_CONTENT_MAX = 600 # 单条 lesson 内容上限（字符）
_LESSON_RECALL_MAX = 3    # 召回 top-N

# In-memory cache: (project_id, "lesson") → (list, expires_at)
_cache: dict[tuple, tuple[list, float]] = {}


def _cache_get(project_id: str) -> list[dict] | None:
    key = (project_id, "lesson")
    entry = _cache.get(key)
    if entry is None:
        return None
    data, expires = entry
    if time.time() > expires:
        _cache.pop(key, None)
        return None
    return data


def _cache_put(project_id: str, data: list[dict]) -> None:
    key = (project_id, "lesson")
    _cache[key] = (data, time.time() + _LESSON_TTL)


def _cache_invalidate(project_id: str) -> None:
    _cache.pop((project_id, "lesson"), None)


def _parse_json(s: str | None) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}


class LessonService:
    """Project-scope experiential lessons with keyword recall."""

    @staticmethod
    async def _conn(project_id: str) -> aiosqlite.Connection:
        workspace = await meta_db.get_project_workspace(project_id)
        if not workspace:
            raise ProjectDbError(f"Workspace not found for project {project_id}")
        return await ensure_project_db(workspace)

    # ── Public API ────────────────────────────────────────────

    async def save_lesson(
        self,
        project_id: str,
        agent_id: str,
        lesson: str,
        *,
        tags: list[str] | None = None,
        root_cause: str | None = None,
        fix: str | None = None,
        source_summary: str | None = None,
    ) -> str | None:
        """Archive one lesson. Returns memory id, or None if rejected by quality gate.

        质量门：lesson 必须非空、≤600 字符；纯吐槽（无根因/修复）拒绝。
        tags: 推荐关键词，用于新任务召回匹配（如 ["worktree", "merge"]）。
        """
        content = (lesson or "").strip()
        if not content:
            return None
        if len(content) > _LESSON_CONTENT_MAX:
            log.info("lesson_rejected_too_long", agent_id=agent_id, length=len(content))
            return None
        # 质量门：至少带根因或修复其一，否则视为吐槽/流水账
        # (whitespace-only 根因/修复不算)
        rc = (root_cause or "").strip()
        fx = (fix or "").strip()
        if not (rc or fx):
            log.info("lesson_rejected_no_value", agent_id=agent_id)
            return None
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list):
            tags = None
        tag_list = [str(t) for t in (tags or []) if str(t).strip()][:20]

        metadata: dict = {
            "tags": tag_list,
            "root_cause": rc[:400],
            "fix": fx[:400],
            "source_summary": (source_summary or "")[:200],
            "source_agent_id": agent_id,
            "archived_at_ms": int(time.time() * 1000),
        }
        now_ms = int(time.time() * 1000)
        conn = await self._conn(project_id)

        # 写事务互斥（TEST18 审计 S1 补漏）：count+delete+insert 显式事务
        # 必须整段持 per-workspace 锁，否则与 execute_transaction 并发时
        # 会被他人 BEGIN/rollback 击穿。
        workspace = await meta_db.get_project_workspace(project_id)
        if not workspace:
            raise ProjectDbError(f"workspace not found for project {project_id}")
        write_lock = await get_workspace_write_lock(workspace)

        # 计数防膨胀：超出上限则删除最旧一条（FIFO）。
        # count+delete+insert 放同一事务，避免并发归档时双双通过计数检查
        # 导致行数越界；DELETE 用 LIMIT 1 只删一条，避免同毫秒多条
        # 被 MIN(created_at) 一起误删。
        async with write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE scope = 'lesson'"
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row and int(row["n"] or 0) >= _LESSON_LIMIT:
                    # 只删最旧一条（ORDER BY created_at ASC LIMIT 1），
                    # 避免同毫秒多条被 MIN(created_at) 一起误删。
                    await conn.execute(
                        "DELETE FROM memories WHERE id IN ("
                        "SELECT id FROM memories WHERE scope = 'lesson' "
                        "ORDER BY created_at ASC LIMIT 1)"
                    )

                mem_id = str(uuid.uuid4())
                await conn.execute(
                    "INSERT INTO memories (id, agent_id, scope, module_id, type, content, "
                    "source_agent_id, metadata, created_at, updated_at) "
                    "VALUES (?, ?, 'lesson', NULL, 'lesson', ?, ?, ?, ?, ?)",
                    [mem_id, agent_id, content, agent_id,
                     json.dumps(metadata, ensure_ascii=False), now_ms, now_ms],
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        _cache_invalidate(project_id)
        log.info(
            "lesson_saved",
            project_id=project_id,
            agent_id=agent_id,
            tags=tag_list,
            preview=content[:80],
        )
        return mem_id

    async def recall_lessons(
        self,
        project_id: str,
        keywords: list[str] | None,
        limit: int = _LESSON_RECALL_MAX,
    ) -> list[dict]:
        """Recall top-N lessons matching the task keywords (tags overlap + text).

        keywords: 从任务/trigger 文本提取的词（小写）。
        打分：tags 命中 ×2 + content 子串命中 ×1；无关键词时返回最近 limit 条。
        """
        cached = _cache_get(project_id)
        if cached is None:
            conn = await self._conn(project_id)
            cursor = await conn.execute(
                "SELECT id, agent_id, content, metadata, created_at "
                "FROM memories WHERE scope = 'lesson' "
                "ORDER BY created_at DESC LIMIT ?", [_LESSON_LIMIT])
            rows = await cursor.fetchall()
            await cursor.close()
            cached = []
            for r in rows:
                d = dict(r)
                d["metadata"] = _parse_json(d.get("metadata"))
                cached.append(d)
            # 读取期间可能已有 save 刷新缓存（或新归档）：
            # 若 _cache_get 已非 None，说明有更新的数据，不覆盖。
            if _cache_get(project_id) is None:
                _cache_put(project_id, cached)

        if not cached:
            return []
        if not keywords:
            return cached[:limit]

        kws = {str(k).strip().lower() for k in keywords if str(k).strip()}

        def score(lesson: dict) -> int:
            meta = lesson.get("metadata") or {}
            tags = [str(t).lower() for t in (meta.get("tags") or [])]
            text = ((lesson.get("content") or "") + " " +
                    (meta.get("root_cause") or "") + " " +
                    (meta.get("fix") or "")).lower()
            s = 0
            for k in kws:
                if k in tags:
                    s += 2
                if k in text:
                    s += 1
            return s

        scored = [(score(l), l) for l in cached]
        scored = [(s, l) for s, l in scored if s > 0]
        scored.sort(key=lambda pair: (pair[0], pair[1].get("created_at") or 0), reverse=True)
        return [l for _, l in scored[:limit]]

    @staticmethod
    def extract_keywords(text: str | None, max_keywords: int = 8) -> list[str]:
        """Lightweight keyword extraction from task text.

        不引入分词依赖：按非字母数字切分，取长度 ≥3 的 token（去停用词）。
        """
        if not text:
            return []
        import re
        stopwords = {
            "the", "and", "for", "with", "from", "that", "this", "have",
            "your", "you", "not", "are", "was", "were", "will", "would",
            "can", "could", "should", "must", "into", "about", "task",
            "then", "than", "them", "they", "what", "when", "where",
            "which", "while", "why", "how", "all", "any", "been",
        }
        tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_]{3,}", text or "")]
        seen: list[str] = []
        for t in tokens:
            if t not in stopwords and t not in seen:
                seen.append(t)
        return seen[:max_keywords]
