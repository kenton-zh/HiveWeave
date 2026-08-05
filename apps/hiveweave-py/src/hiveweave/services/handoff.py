"""Handoff service — task lifecycle management.

契约 06: 交接
状态机: pending → accepted → completed → approved (终态)
                completed → accepted (reopen, 重置 context_delivered=0)

- create_handoff 去重: 同 from/to/summary 1 分钟内不重复
- mark_delivered 不可逆 (契约 06 RECONCILE: 崩溃时 handoff delivered 但 inbox 未读 → inbox 保留未读重试)
- complete_handoff 只完成 accepted (不 fallback 到 pending — 以 Elixir 为准)

schema.py 的 handoffs 表缺 module_id/expect_report/reported_up/updated_at/context_delivered
列，启动时 ALTER TABLE 补齐（幂等）。
"""

import asyncio
import contextlib
import json
import time
import uuid
from pathlib import Path

import aiosqlite
import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import (
    ProjectDbError,
    ensure_project_db,
    get_workspace_write_lock,
)

log = structlog.get_logger(__name__)

# Columns missing from schema.py handoffs table
_MISSING_COLUMNS = [
    ("module_id", "TEXT"),
    ("expect_report", "INTEGER DEFAULT 0"),
    ("reported_up", "INTEGER DEFAULT 0"),
    ("updated_at", "INTEGER"),
    ("context_delivered", "INTEGER DEFAULT 0"),
    ("task_id", "TEXT"),
    ("artifact_path", "TEXT"),
    ("context_refs", "TEXT"),
]
_migrated: set[str] = set()


async def _conn(project_id: str) -> aiosqlite.Connection:
    """Resolve project_id to per-project DB connection."""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
    return await ensure_project_db(workspace)


async def _query(project_id: str, sql: str, params: list | None = None) -> list:
    conn = await _conn(project_id)
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def _execute(project_id: str, sql: str, params: list | None = None) -> None:
    conn = await _conn(project_id)
    await conn.execute(sql, params or [])
    await conn.commit()


async def _ensure_schema(project_id: str) -> None:
    """Add missing columns to handoffs table (idempotent).

    H3 (审计 2026-08-05)：区分「重复列」（预期，新库 schema.py 已建）与
    「真实失败」（如锁死）——真实失败不 mark migrated，下次调用重试；
    只有全部列就绪才缓存 project，避免后续写 artifact_path 时 no such column。
    """
    if project_id in _migrated:
        return
    ok = True
    for col_name, col_def in _MISSING_COLUMNS:
        try:
            await _execute(project_id,
                           f"ALTER TABLE handoffs ADD COLUMN {col_name} {col_def}")
        except Exception as e:
            if "duplicate column" in str(e).lower():
                continue  # 已存在——预期
            log.warning("handoff_migrate_column_failed",
                        column=col_name, error=str(e))
            ok = False
    if ok:
        _migrated.add(project_id)


class HandoffService:
    """Task handoff lifecycle — dispatch to approval with rework support."""

    async def create_handoff(self, project_id: str, from_agent_id: str,
                             to_agent_id: str, summary: str,
                             expect_report: bool = False,
                             task_id: str | None = None) -> str:
        """Create a handoff with dedup (同 from/to/summary 1 分钟内不重复)."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        expect = 1 if expect_report else 0
        # Dedup: active handoff with same from/to/summary within last 1 minute
        dedup_cutoff = now_ms - 60_000
        existing = await _query(project_id,
            "SELECT id FROM handoffs WHERE from_agent_id = ? AND to_agent_id = ? "
            "AND summary = ? AND status IN ('pending', 'accepted') "
            "AND created_at > ? LIMIT 1",
            [from_agent_id, to_agent_id, summary, dedup_cutoff])
        if existing:
            log.info("handoff_dedup", existing_id=existing[0]["id"],
                     summary=summary[:60])
            return existing[0]["id"]

        handoff_id = str(uuid.uuid4())
        await _execute(project_id,
            "INSERT INTO handoffs (id, from_agent_id, to_agent_id, module_id, summary, "
            "status, expect_report, reported_up, created_at, updated_at, task_id) "
            "VALUES (?, ?, ?, NULL, ?, 'pending', ?, 0, ?, ?, ?)",
            [handoff_id, from_agent_id, to_agent_id, summary, expect, now_ms, now_ms,
             task_id])
        log.info("handoff_created", from_agent_id=from_agent_id,
                 to_agent_id=to_agent_id, summary=summary[:60], task_id=task_id)
        return handoff_id

    async def accept_pending_handoffs(self, project_id: str, agent_id: str) -> int:
        """Accept all pending handoffs for an agent (pending → accepted). Returns count."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "UPDATE handoffs SET status = 'accepted', updated_at = ? "
            "WHERE to_agent_id = ? AND status = 'pending'", [now_ms, agent_id])
        await conn.commit()
        count = max(cursor.rowcount, 0)
        await cursor.close()
        return count

    async def complete_handoff(self, project_id: str, handoff_id: str) -> bool:
        """Complete a handoff (accepted → completed). Only accepted can be completed."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "UPDATE handoffs SET status = 'completed', updated_at = ? "
            "WHERE id = ? AND status = 'accepted'", [now_ms, handoff_id])
        await conn.commit()
        ok = cursor.rowcount > 0
        await cursor.close()
        log.info("handoff_complete", handoff_id=handoff_id, completed=ok)
        return ok

    async def approve(self, project_id: str, reviewer_id: str,
                       subordinate_id: str) -> dict:
        """Approve a subordinate's completed handoff. Returns {success, message}."""
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "SELECT id FROM handoffs WHERE to_agent_id = ? AND from_agent_id = ? "
            "AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
            [reviewer_id, subordinate_id])
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return {"success": False, "message": "No completed handoff found for this subordinate"}
        handoff_id = row["id"]
        ok = await self.approve_handoff(project_id, handoff_id)
        return {"success": ok, "message": "Approved" if ok else "Failed to approve"}

    async def reject(self, project_id: str, reviewer_id: str,
                      subordinate_id: str, reason: str) -> dict:
        """Reject a subordinate's completed handoff with feedback. Returns {success, message}."""
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "SELECT id FROM handoffs WHERE to_agent_id = ? AND from_agent_id = ? "
            "AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
            [reviewer_id, subordinate_id])
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return {"success": False, "message": "No completed handoff found for this subordinate"}
        handoff_id = row["id"]
        ok = await self.reopen_handoff(project_id, handoff_id)
        return {"success": ok, "message": f"Rejected: {reason}" if ok else "Failed to reject"}

    async def approve_handoff(self, project_id: str, handoff_id: str) -> bool:
        """Approve a handoff (completed → approved, terminal state)."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "UPDATE handoffs SET status = 'approved', updated_at = ? "
            "WHERE id = ? AND status = 'completed'", [now_ms, handoff_id])
        await conn.commit()
        ok = cursor.rowcount > 0
        await cursor.close()
        log.info("handoff_approve", handoff_id=handoff_id, approved=ok)
        return ok

    async def reopen_handoff(self, project_id: str, handoff_id: str) -> bool:
        """Reopen a handoff (completed → accepted, resets context_delivered=0)."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "UPDATE handoffs SET status = 'accepted', context_delivered = 0, "
            "updated_at = ? WHERE id = ? AND status = 'completed'",
            [now_ms, handoff_id])
        await conn.commit()
        ok = cursor.rowcount > 0
        await cursor.close()
        log.info("handoff_reopen", handoff_id=handoff_id, reopened=ok)
        return ok

    async def get_pending_handoffs(self, project_id: str, agent_id: str) -> list[dict]:
        """Get pending handoffs (status=pending AND context_delivered=0)."""
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, "
            "expect_report, reported_up, created_at, updated_at, artifact_path "
            "FROM handoffs "
            "WHERE to_agent_id = ? AND status = 'pending' AND context_delivered = 0 "
            "ORDER BY created_at ASC", [agent_id])
        return [self._row(r) for r in rows]

    async def get_accepted_handoffs(self, project_id: str, agent_id: str) -> list[dict]:
        """Get accepted handoffs (status=accepted AND context_delivered=0)."""
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, "
            "expect_report, reported_up, created_at, updated_at, artifact_path "
            "FROM handoffs "
            "WHERE to_agent_id = ? AND status = 'accepted' AND context_delivered = 0 "
            "ORDER BY created_at ASC", [agent_id])
        return [self._row(r) for r in rows]

    async def mark_delivered(self, project_id: str, handoff_ids: list[str]) -> None:
        """Mark handoffs as context_delivered=1 (不可逆 — 契约 06 RECONCILE)."""
        if not handoff_ids:
            return
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        placeholders = ", ".join(["?"] * len(handoff_ids))
        await _execute(project_id,
            f"UPDATE handoffs SET context_delivered = 1, updated_at = ? "
            f"WHERE id IN ({placeholders})", [now_ms] + handoff_ids)

    async def mark_reported(self, project_id: str, agent_id: str,
                            task_id: str | None = None,
                            to_sender_id: str | None = None) -> int:
        """Mark handoffs as reported_up=1 when agent submits task or sends reply.

        Args:
            agent_id: The agent who is reporting (to_agent_id in handoff).
            task_id: If provided, only clear handoffs with matching task_id.
            to_sender_id: If provided, only clear handoffs FROM this sender
                          (from_agent_id). Used when agent replies to a specific
                          person — should NOT clear obligations from other senders.
        Returns number of handoffs updated.
        """
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)

        conditions = ["to_agent_id = ?", "status = 'accepted'",
                      "expect_report = 1", "reported_up = 0"]
        params: list = [agent_id]

        if task_id:
            conditions.append("(task_id = ? OR task_id IS NULL)")
            params.append(task_id)
        if to_sender_id:
            conditions.append("from_agent_id = ?")
            params.append(to_sender_id)

        where = " AND ".join(conditions)
        rows = await _query(project_id,
            f"SELECT id FROM handoffs WHERE {where}", params)

        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ", ".join(["?"] * len(ids))
        await _execute(project_id,
            f"UPDATE handoffs SET reported_up = 1, updated_at = ? "
            f"WHERE id IN ({placeholders})", [now_ms] + ids)
        return len(ids)

    async def get_unreported_accepted_handoffs(self, project_id: str,
                                               agent_id: str) -> list[dict]:
        """Find accepted handoffs with expect_report=1 AND reported_up=0."""
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, "
            "expect_report, reported_up, created_at, updated_at, artifact_path "
            "FROM handoffs "
            "WHERE to_agent_id = ? AND status = 'accepted' AND expect_report = 1 "
            "AND reported_up = 0 ORDER BY created_at ASC", [agent_id])
        return [self._row(r) for r in rows]

    # ── 产物引用链（context_refs）────────────────────────────

    async def get_incoming_references(self, project_id: str,
                                      agent_id: str) -> list[dict]:
        """该 agent 在职期间收到的交接文档（bootstrap 上下文 / built-on 引用）。

        用于解散交接时收集 context_refs，也可供查询某 agent 的产物来源。
        返回 [{path, title, reason}] 列表。
        """
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            "SELECT artifact_path, summary FROM handoffs "
            "WHERE to_agent_id = ? AND artifact_path IS NOT NULL "
            "AND artifact_path != '' ORDER BY created_at ASC",
            [agent_id],
        )
        seen: set[str] = set()
        out: list[dict] = []
        for r in rows:
            p = (r["artifact_path"] or "").strip()
            if not p or p in seen:
                continue
            seen.add(p)
            out.append({
                "path": p,
                "title": (r["summary"] or "").strip()[:120],
                "reason": "bootstrap context received during this agent's tenure",
            })
        return out

    async def get_reverse_references(self, project_id: str,
                                     artifact_path: str) -> list[dict]:
        """反向追溯：哪些交接文档通过 context_refs 引用了给定产物。

        SQLite json_each 展开 context_refs 数组，按 path 精确匹配。
        返回引用方的 handoff 记录（id/from/to/summary/artifact_path/created_at）。
        """
        await _ensure_schema(project_id)
        # 深入审计 P1-1：WHERE 过滤在 JOIN 后才求值，`json_each('')` 会在过滤前
        # 抛 malformed JSON 炸掉整个查询。故在展开处用 json_valid+json_type 守卫：
        # 非合法数组（NULL/空串/malformed/对象）一律替换为 '[]' → 0 行，安全。
        rows = await _query(
            project_id,
            "SELECT h.id, h.from_agent_id, h.to_agent_id, h.summary, "
            "h.artifact_path, h.created_at FROM handoffs h "
            "JOIN json_each(CASE WHEN json_valid(h.context_refs) "
            "AND json_type(h.context_refs) = 'array' THEN h.context_refs "
            "ELSE '[]' END) AS j "
            "WHERE json_extract(j.value, '$.path') = ? "
            "ORDER BY h.created_at ASC",
            [artifact_path],
        )
        return [dict(r) for r in rows]

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        d["expect_report"] = bool(d.get("expect_report"))
        d["reported_up"] = bool(d.get("reported_up"))
        return d

    # ── Dismissal handoff (私有记忆 → 文档 + 引用交接) ─────────

    async def create_dismissal_handoff(
        self,
        project_id: str,
        agent_id: str,
        *,
        agent_name: str = "",
        short_id: str = "",
        role: str = "",
        parent_id: str | None = None,
    ) -> dict:
        """Dismissal handoff: write an agent's private memories to a doc,
        archive them (scope agent → archive), and record a handoff that
        REFERENCES the doc.

        核心取向（用户定 2026-08-05）：交接只把文档引用交给上级，
        不把记忆内容灌进上级上下文——是否 read_file 读文档由上级决定。
        写文档/归档失败不阻断 dismiss 主流程（best-effort）。

        Returns: {document_path, memory_count, archived, handoff_id}
        """
        from hiveweave.services.memory import MemoryService

        mem = MemoryService()
        now_ms = int(time.time() * 1000)

        # H8 (审计 2026-08-05)：幂等——同一 agent 已有未完成的解散交接（引用过
        # 文档）则直接复用，避免重试生成重复文档 + 重复 handoff。
        try:
            await _ensure_schema(project_id)
            existing = await _query(
                project_id,
                "SELECT artifact_path FROM handoffs WHERE from_agent_id = ? "
                "AND status IN ('pending', 'accepted') "
                "AND artifact_path IS NOT NULL AND artifact_path != '' "
                "ORDER BY created_at DESC LIMIT 1",
                [agent_id],
            )
            if existing and existing[0]["artifact_path"]:
                # 深入审计 P3-1：复用分支补查真实归档数，避免 org.py 用
                # `or 0` 回落到误导的 "0 private memory note(s)"。
                memory_count = 0
                try:
                    rows = await _query(
                        project_id,
                        "SELECT COUNT(*) AS n FROM memories "
                        "WHERE scope = 'archive' AND agent_id = ?",
                        [agent_id],
                    )
                    if rows:
                        memory_count = int(rows[0]["n"] or 0)
                except Exception as e:
                    log.warning("dismissal_reuse_count_failed",
                                agent_id=agent_id, error=str(e))
                log.info("dismissal_handoff_reused", agent_id=agent_id,
                         document_path=existing[0]["artifact_path"])
                return {
                    "document_path": existing[0]["artifact_path"],
                    "memory_count": memory_count,
                    "archived": None,
                    "handoff_id": None,
                    "reused": True,
                }
        except Exception as e:
            log.warning("dismissal_dedup_check_failed",
                        agent_id=agent_id, error=str(e))

        workspace = await meta_db.get_project_workspace(project_id)
        # H2 (审计 2026-08-05)：读快照→写文档→归档 在同一 per-workspace 写锁内
        # 原子完成，避免「读后归档前」又有新记忆写入导致归档但不进文档。
        lock = await get_workspace_write_lock(workspace) if workspace else None
        ctx = lock if lock is not None else contextlib.nullcontext()

        # 产物引用链：收集该 agent 在职期间收到的交接文档（bootstrap 上下文），
        # 作为本交接文档的「built on」引用。只读查询，移出写锁，不延长锁持有
        # 时间（审计 2026-08-05 P2-4）。
        incoming_refs: list[dict] = []
        try:
            incoming_refs = await self.get_incoming_references(
                project_id, agent_id,
            )
        except Exception as e:
            log.warning("dismissal_load_incoming_refs_failed",
                        agent_id=agent_id, error=str(e))

        doc_path = ""
        memories: list[dict] = []
        archived = 0
        async with ctx:
            try:
                # 交接文档要覆盖全部私有记忆（归档后不可再查），不走默认 100 上限。
                memories = await mem.get_all_agent_memories(
                    agent_id, project_id, limit=100_000,
                )
            except Exception as e:
                log.warning("dismissal_load_memories_failed",
                            agent_id=agent_id, error=str(e))
                memories = []

            if memories and workspace:
                try:
                    handoffs_dir = Path(workspace) / ".hiveweave" / "handoffs"
                    handoffs_dir.mkdir(parents=True, exist_ok=True)
                    stamp = time.strftime("%Y%m%d-%H%M%S",
                                          time.localtime(now_ms / 1000))
                    fname = f"{short_id or agent_id[:8]}-dismissal-{stamp}-{uuid.uuid4().hex[:8]}.md"
                    doc_text = self._build_dismissal_document(
                        agent_name=agent_name, short_id=short_id, role=role,
                        memories=memories, now_ms=now_ms,
                        context_refs=incoming_refs,
                    )
                    target = handoffs_dir / fname
                    # H4 (审计 2026-08-05)：同步写盘挪到线程池，避免阻塞事件循环。
                    await asyncio.to_thread(target.write_text, doc_text, "utf-8")
                    doc_path = str(target)
                except Exception as e:
                    log.warning("dismissal_write_document_failed",
                                agent_id=agent_id, error=str(e))

            # 仅在文档成功落盘后才归档（scope agent → archive）。归档不可逆且
            # read_memory/get_*_memories 只查 scope='agent'——文档是归档后私有
            # 记忆唯一存续载体。若文档写失败（doc_path 为空），保留 agent scope，
            # 避免记忆静默永久丢失（审计 2026-08-05）。
            if doc_path:
                try:
                    archived = await mem.archive_agent_memories(
                        agent_id, project_id, _write_lock=lock,
                    )
                except Exception as e:
                    log.warning("dismissal_archive_failed",
                                agent_id=agent_id, error=str(e))

        # H6 (审计 2026-08-05)：无文档则不建引用型的 pending handoff——引用
        # 必须指向真实存在的文档，避免误导的 "(none)" 记录落到上级。
        handoff_id = None
        if parent_id and doc_path:
            try:
                ref_summary = ""
                if incoming_refs:
                    # P2-2 (审计 2026-08-05)：summary 只记数量，不拼完整绝对路径，
                    # 避免 handoffs.summary 存超长文本。明细路径在文档 References
                    # 小节里，上级 read_file 即可看到。
                    ref_summary = (
                        f" Builds on {len(incoming_refs)} prior document(s)."
                    )
                handoff_id = await self.create_handoff(
                    project_id,
                    from_agent_id=agent_id,
                    to_agent_id=parent_id,
                    summary=(
                        f"Agent {short_id or agent_id[:8]} dismissed — "
                        f"{len(memories)} private memory note(s) archived. "
                        f"Handoff document: {doc_path}. Read it "
                        f"via read_file if you need context; content was "
                        f"intentionally not injected.{ref_summary}"
                    ),
                )
                if handoff_id:
                    await _execute(
                        project_id,
                        "UPDATE handoffs SET artifact_path = ?, context_refs = ?, "
                        "updated_at = ? WHERE id = ?",
                        [doc_path, json.dumps(incoming_refs), now_ms, handoff_id],
                    )
            except Exception as e:
                log.warning("dismissal_handoff_create_failed",
                            agent_id=agent_id, error=str(e))

        log.info(
            "dismissal_handoff_created",
            agent_id=agent_id,
            short_id=short_id,
            memory_count=len(memories),
            archived=archived,
            document_path=doc_path or None,
            handoff_id=handoff_id,
        )
        return {
            "document_path": doc_path,
            "memory_count": len(memories),
            "archived": archived,
            "handoff_id": handoff_id,
        }

    @staticmethod
    def _build_dismissal_document(
        *,
        agent_name: str,
        short_id: str,
        role: str,
        memories: list[dict],
        now_ms: int,
        context_refs: list[dict] | None = None,
    ) -> str:
        """Render the agent's private memories as a handoff Markdown document.

        ``context_refs``：可选的产物引用链——本交接文档是在哪些更早交接文档
        之上做的（bootstrap 上下文）。渲染为 References(built on) 小节。
        """
        context_refs = context_refs or []
        lines = [
            f"# 交接文档：{agent_name or short_id or 'Agent'}",
            "",
            f"- short_id: {short_id}",
            f"- role: {role}",
            f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now_ms / 1000))}",
            f"- 私有记忆条数: {len(memories)}",
            f"- 引用的前置产物数: {len(context_refs)}",
            "",
        ]
        if context_refs:
            lines.append("## References (built on)")
            lines.append("")
            lines.append("本交接文档继承自以下前置交接文档（bootstrap 上下文）：")
            lines.append("")
            for ref in context_refs:
                lines.append(f"- `{ref.get('path', '')}`")
                if ref.get("title"):
                    lines.append(f"  - {ref['title']}")
                if ref.get("reason"):
                    lines.append(f"  - reason: {ref['reason']}")
            lines.append("")
        lines.append("## 私有工作记忆")
        lines.append("")
        if not memories:
            lines.append("（该 agent 无私有记忆）")
        for m in memories:
            typ = m.get("type") or "fact"
            content = (m.get("content") or "").strip()
            # H1 (审计 2026-08-05)：legacy 记忆的 metadata 可能是非 dict
            # （JSON 数组/字符串），逐条降级为 {}，坏的只丢该条而非中断整份文档。
            raw_meta = m.get("metadata")
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            lines.append(f"### [{typ}] {content}")
            if meta.get("root_cause"):
                lines.append(f"- root_cause: {meta['root_cause']}")
            if meta.get("fix"):
                lines.append(f"- fix: {meta['fix']}")
            if meta.get("tags"):
                lines.append(f"- tags: {meta['tags']}")
            lines.append("")
        return "\n".join(lines)
