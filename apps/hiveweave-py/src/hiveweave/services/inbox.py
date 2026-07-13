"""Inbox service — inter-agent message delivery.

契约 06: 收件箱
- 消息类型: normal / alarm / escalation 等（自由字符串）
- 三级优先级: low / normal / urgent
- 未读消息 ASC 排序（FIFO 语义，旧消息优先处理）
- mark_read_by_ids 按 ID 批量标记（避免 mark_all_read 竞态 — 契约 H1）
- 路由: 通过 to_agent_id 查 Meta DB → per-project DB

schema.py 的 inbox 表缺 priority 列，启动时 ALTER TABLE 补齐（幂等）。
"""

import time
import uuid

import structlog

from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

# Idempotent migration tracking: agent_ids whose inbox has been checked
_migrated: set[str] = set()

# Columns missing from older inbox tables
_MISSING_COLUMNS = [
    ("priority", "TEXT DEFAULT 'normal'"),
    ("task_id", "TEXT"),
]


async def _ensure_schema(agent_id: str) -> None:
    """Add missing columns to inbox table (idempotent)."""
    if agent_id in _migrated:
        return
    for col_name, col_def in _MISSING_COLUMNS:
        try:
            await project_db.execute(
                agent_id,
                f"ALTER TABLE inbox ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass  # Column already exists — safe to ignore
    _migrated.add(agent_id)


class InboxService:
    """Agent inbox — message delivery with priority and read tracking."""

    async def send_message(self, from_agent_id: str, to_agent_id: str, message: str,
                           message_type: str = "normal", priority: str = "normal",
                           expect_report: bool = False,
                           task_id: str | None = None) -> dict:
        """Send a message to an agent's inbox.

        Writes to the per-project DB routed via to_agent_id.
        Returns the created message dict.
        """
        await _ensure_schema(to_agent_id)
        msg_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        expect = 1 if expect_report else 0
        await project_db.execute(
            to_agent_id,
            "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
            "created_at, message_type, expect_report, priority, task_id) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            [msg_id, from_agent_id, to_agent_id, message, now_ms,
             message_type, expect, priority, task_id])
        log.info("inbox_sent", from_agent_id=from_agent_id, to_agent_id=to_agent_id,
                 message_type=message_type, priority=priority, preview=message[:80],
                 task_id=task_id)

        # Push real-time event to notify recipient's frontend immediately.
        # Without this, the recipient only learns about new messages via
        # the 5-second inbox watcher poll — or never, if the agent isn't running.
        try:
            from hiveweave.realtime.event_bus import status_event_bus
            await status_event_bus.publish_chat_message(
                to_agent_id,
                {
                    "role": "team",
                    "content": message,
                    "from_agent_id": from_agent_id,
                    "message_type": message_type,
                    "priority": priority,
                    "inbox_id": msg_id,
                },
            )
        except Exception as e:
            log.debug("inbox_event_push_failed", error=str(e))

        return {
            "id": msg_id, "from_agent_id": from_agent_id, "to_agent_id": to_agent_id,
            "message": message, "message_type": message_type, "priority": priority,
            "expect_report": expect_report, "read": False, "created_at": now_ms,
            "task_id": task_id,
        }

    async def get_pending_messages(self, agent_id: str) -> list[dict]:
        """Get unread messages for an agent, ASC by created_at (FIFO). Limit 50."""
        await _ensure_schema(agent_id)
        rows = await project_db.query(
            agent_id,
            "SELECT id, from_agent_id, to_agent_id, message, read, created_at, "
            "message_type, expect_report, priority FROM inbox "
            "WHERE to_agent_id = ? AND read = 0 ORDER BY created_at ASC LIMIT 50",
            [agent_id])
        return [self._row_to_msg(r) for r in rows]

    async def mark_read_by_ids(self, agent_id: str, message_ids: list[str]) -> None:
        """Mark specific messages as read by ID (契约 H1: 避免 mark_all_read 竞态).

        Only the messages actually included in trigger context get marked.
        """
        if not message_ids:
            return
        await _ensure_schema(agent_id)
        placeholders = ", ".join(["?"] * len(message_ids))
        await project_db.execute(
            agent_id,
            f"UPDATE inbox SET read = 1 WHERE to_agent_id = ? AND id IN ({placeholders})",
            [agent_id] + message_ids)

    async def mark_all_read(self, agent_id: str) -> None:
        """Mark all unread messages as read (供前端手动操作，trigger 场景用 mark_read_by_ids)."""
        await _ensure_schema(agent_id)
        await project_db.execute(
            agent_id,
            "UPDATE inbox SET read = 1 WHERE to_agent_id = ? AND read = 0",
            [agent_id])

    async def get_unread_count(self, agent_id: str) -> int:
        """Get unread message count for an agent."""
        await _ensure_schema(agent_id)
        row = await project_db.query_one(
            agent_id,
            "SELECT COUNT(*) AS cnt FROM inbox WHERE to_agent_id = ? AND read = 0",
            [agent_id])
        return row["cnt"] if row else 0

    @staticmethod
    def _row_to_msg(row) -> dict:
        d = dict(row)
        d["read"] = bool(d.get("read"))
        d["expect_report"] = bool(d.get("expect_report"))
        d.setdefault("priority", "normal")
        return d
