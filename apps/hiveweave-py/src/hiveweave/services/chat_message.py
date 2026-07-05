"""Chat message service — UI message persistence.

契约 17: ChatMessage（UI 消息持久化）
- 区别于 conversation_turns（契约 03 LLM 历史）：chat_messages 是 UI 展示层消息
- 支持流式状态管理（is_streaming）、僵尸消息清理（clear_stuck_streaming）
- 未读背景消息检测（get_unread_background）、未回复用户消息检测（has_unanswered_user_messages）
- 布尔字段（is_streaming/is_background/is_read/is_context）以 0/1 整数存储

chat_messages 表 schema 已完整（含 images/metadata/tool_call_id），无需迁移。
"""

import json
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)


class ChatMessageService:
    """UI chat message persistence — distinct from conversation_turns.

    所有 agent 级操作路由到 per-project DB（通过 agent_id）。
    clear_stuck_streaming 遍历所有 project DB。

    R12: 构造函数接受可选 project_id，供 main.py lifespan 等场景按项目实例化。
    """

    def __init__(self, project_id: str | None = None) -> None:
        self._project_id = project_id

    async def save_message(self, attrs: dict) -> dict:
        """Save a UI message. Returns {id, role, content, created_at}.

        契约 17: save_message
        - id 缺省 → UUID; role 缺省 → 'assistant'; content 缺省 → ''
        - tool_calls 缺省 → '[]'
        - is_read 默认 1 (True); is_background/is_streaming/is_context 默认 0 (False)
        - bool → int (True→1, False→0)
        - images/metadata: dict|list → JSON 序列化（修复 E8: 补全 images 保存）
        """
        agent_id = attrs["agent_id"]
        msg_id = attrs.get("id") or str(uuid.uuid4())
        role = attrs.get("role", "assistant")
        content = attrs.get("content", "")
        thinking = attrs.get("thinking")
        tool_calls = attrs.get("tool_calls", "[]")
        tool_call_id = attrs.get("tool_call_id")
        is_streaming = 1 if attrs.get("is_streaming", False) else 0
        is_background = 1 if attrs.get("is_background", False) else 0
        is_read = 1 if attrs.get("is_read", True) else 0
        is_context = 1 if attrs.get("is_context", False) else 0
        team_from = attrs.get("team_from_agent_id")
        team_to = attrs.get("team_to_agent_id")

        # images/metadata: accept dict|list (JSON serialize) or string (as-is)
        images = attrs.get("images")
        if images is not None and isinstance(images, (dict, list)):
            images = json.dumps(images, ensure_ascii=False)
        metadata = attrs.get("metadata")
        if metadata is not None and isinstance(metadata, dict):
            metadata = json.dumps(metadata, ensure_ascii=False)

        now_ms = attrs.get("created_at") or int(time.time() * 1000)

        await project_db.execute(
            agent_id,
            "INSERT INTO chat_messages (id, agent_id, role, content, thinking, "
            "tool_calls, tool_call_id, is_streaming, is_background, is_read, "
            "is_context, team_from_agent_id, team_to_agent_id, images, metadata, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [msg_id, agent_id, role, content, thinking, tool_calls, tool_call_id,
             is_streaming, is_background, is_read, is_context, team_from, team_to,
             images, metadata, now_ms])
        log.info("chat_message_saved", agent_id=agent_id, role=role,
                 msg_id=msg_id, preview=content[:80] if content else "")
        return {"id": msg_id, "role": role, "content": content, "created_at": now_ms}

    async def update_message(self, agent_id: str, msg_id: str, attrs: dict) -> bool:
        """Update an existing message's content/is_read/is_streaming/tool_calls/thinking.

        契约 17: update_message — 仅更新非 None 字段。Returns True if a row was affected.
        """
        fields: list[str] = []
        params: list = []
        for key in ("content", "thinking", "tool_calls", "tool_call_id",
                    "is_read", "is_streaming", "is_context", "is_background"):
            if key in attrs and attrs[key] is not None:
                val = attrs[key]
                if key in ("is_read", "is_streaming", "is_context", "is_background"):
                    val = 1 if val else 0
                fields.append(f"{key} = ?")
                params.append(val)
        if not fields:
            return False
        params.extend([agent_id, msg_id])

        conn = await project_db.get_project_db_for_agent(agent_id)
        if conn is None:
            return False
        cursor = await conn.execute(
            f"UPDATE chat_messages SET {', '.join(fields)} "
            f"WHERE agent_id = ? AND id = ?", params)
        await conn.commit()
        ok = cursor.rowcount > 0
        await cursor.close()
        return ok

    async def get_messages(
        self, agent_id: str, limit: int = 200, offset: int = 0
    ) -> list[dict]:
        """Get recent messages for an agent (chronological order). Default limit 200.

        契约 17: DESC + reverse → 正序返回。异常返回 []（fail-empty）。
        R7 fix: 支持 offset 分页（DESC 结果上跳过 offset 条再取 limit 条）。
        """
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT id, agent_id, role, content, thinking, tool_calls, "
                "tool_call_id, is_streaming, is_background, is_read, is_context, "
                "team_from_agent_id, team_to_agent_id, images, metadata, created_at "
                "FROM chat_messages WHERE agent_id = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [agent_id, limit, offset])
            return [self._row_to_msg(r) for r in reversed(rows)]
        except Exception as e:
            log.warning("get_messages_failed", agent_id=agent_id, error=str(e))
            return []

    async def get_history(self, agent_id: str, limit: int = 200) -> list[dict]:
        """Alias for get_messages."""
        return await self.get_messages(agent_id, limit)

    async def update_streaming_messages_done(self, agent_id: str) -> None:
        """Mark all streaming messages for an agent as done (is_streaming=0).

        契约 17: 用于 safety_timeout / :DOWN handler，防止崩溃后僵尸流式消息。
        """
        await project_db.execute(
            agent_id,
            "UPDATE chat_messages SET is_streaming = 0 "
            "WHERE agent_id = ? AND is_streaming = 1",
            [agent_id])

    async def mark_as_read(self, agent_id: str, msg_ids: list[str]) -> int:
        """Mark messages as read by ID list. Returns count marked.

        契约 17: 空列表返回 0 不发 SQL。异常返回 0。
        """
        if not msg_ids:
            return 0
        try:
            placeholders = ", ".join(["?"] * len(msg_ids))
            await project_db.execute(
                agent_id,
                f"UPDATE chat_messages SET is_read = 1 "
                f"WHERE agent_id = ? AND id IN ({placeholders})",
                [agent_id] + msg_ids)
            return len(msg_ids)
        except Exception as e:
            log.warning("mark_as_read_failed", agent_id=agent_id, error=str(e))
            return 0

    async def get_unread_background(self, agent_id: str) -> list[dict]:
        """Get unread background messages (oldest first).

        契约 17: is_background=1 AND is_read=0, ORDER BY created_at ASC。
        异常返回 []（fail-empty）。
        """
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT id, agent_id, role, content, thinking, tool_calls, "
                "tool_call_id, is_streaming, is_background, is_read, is_context, "
                "team_from_agent_id, team_to_agent_id, images, metadata, created_at "
                "FROM chat_messages WHERE agent_id = ? AND is_background = 1 "
                "AND is_read = 0 ORDER BY created_at ASC",
                [agent_id])
            return [self._row_to_msg(r) for r in rows]
        except Exception as e:
            log.warning("get_unread_background_failed", agent_id=agent_id,
                        error=str(e))
            return []

    async def has_unanswered_user_messages(self, agent_id: str) -> bool:
        """Check if there are unanswered user messages.

        契约 17: 存在前台 user 消息，其后（含同时刻 created_at >=）无前台 assistant 消息响应。
        忽略 is_background=1 的消息。异常返回 False（fail-safe，不误触发）。
        """
        try:
            row = await project_db.query_one(
                agent_id,
                "SELECT EXISTS("
                "  SELECT 1 FROM chat_messages m1"
                "  WHERE m1.agent_id = ? AND m1.role = 'user'"
                "    AND m1.is_background = 0"
                "    AND NOT EXISTS("
                "      SELECT 1 FROM chat_messages m2"
                "      WHERE m2.agent_id = m1.agent_id"
                "        AND m2.role = 'assistant'"
                "        AND m2.is_background = 0"
                "        AND m2.created_at >= m1.created_at"
                "    )"
                ") AS has_unanswered",
                [agent_id])
            return bool(row and row["has_unanswered"])
        except Exception as e:
            log.warning("has_unanswered_check_failed", agent_id=agent_id,
                        error=str(e))
            return False

    async def clear_stuck_streaming(self) -> None:
        """Clear all stuck streaming messages across all projects.

        契约 17: 启动时遍历所有 project DB，清除 is_streaming=1 的僵尸消息。
        单个 project 失败仅 warning，不中断整体。整体异常 rescue 返回。
        """
        try:
            rows = await meta_db.query("SELECT id, workspace_path FROM projects")
            for row in rows:
                workspace = row["workspace_path"]
                if not workspace:
                    continue
                try:
                    conn = await project_db.ensure_project_db(workspace)
                    await conn.execute(
                        "UPDATE chat_messages SET is_streaming = 0 "
                        "WHERE is_streaming = 1")
                    await conn.commit()
                except Exception as e:
                    log.warning("clear_stuck_streaming_project_failed",
                                project_id=row["id"], error=str(e))
            log.info("clear_stuck_streaming_done", project_count=len(rows))
        except Exception as e:
            log.warning("clear_stuck_streaming_failed", error=str(e))

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _row_to_msg(row) -> dict:
        d = dict(row)
        d["is_streaming"] = bool(d.get("is_streaming"))
        d["is_background"] = bool(d.get("is_background"))
        d["is_read"] = bool(d.get("is_read"))
        d["is_context"] = bool(d.get("is_context"))
        return d
