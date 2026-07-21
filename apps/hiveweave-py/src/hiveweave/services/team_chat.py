"""Team chat service — multi-agent group chat.

契约 18: TeamChatService
- 复用 chat_messages 表（role='team'），独立 team_chat_dedupe 去重表
- 1 分钟窗口内 (from, to, content) 三元组重复则丢弃（返回 'duplicate'）
- record_message: 去重后写 chat_messages + dedupe_key
- get_history: DESC + reverse → 正序（oldest first）

team_chat_dedupe 表 schema 已存在于 per-project DB，无需迁移。
"""

import hashlib
import time
import uuid

import structlog

from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

# 契约 18: 去重窗口 60_000 ms（1 分钟）
_DEDUPE_WINDOW_MS = 60_000


class TeamChatService:
    """Multi-agent team chat with dedup.

    所有操作路由到 per-project DB（通过 agent_id）。
    消息存储在 chat_messages 表（role='team'），用 team_from/to_agent_id 区分。
    """

    async def record_message(self, agent_id: str, from_agent_id: str,
                             to_agent_id: str, content: str,
                             opts: dict | None = None) -> str:
        """Record a team chat message with dedup.

        契约 18: record_message
        - dedupe_key = MD5("{from}:{to}:{content}") hex lowercase
        - 1 分钟窗口内重复 → 返回 'duplicate'，不写 chat_messages
        - 非重复 → 写 chat_messages + dedupe_key，返回 'ok'
        - save 失败 → 返回 'error'
        - save_dedupe_key 失败 → 静默（仍返回 'ok'，dedupe 是优化非必需）

        Returns: 'ok' | 'duplicate' | 'error'
        """
        dedupe_key = hashlib.md5(
            f"{from_agent_id}:{to_agent_id}:{content}".encode()
        ).hexdigest()
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - _DEDUPE_WINDOW_MS

        # Check dedup (fail-open: exception → not duplicate,宁可重复不丢消息)
        if await self._is_duplicate(agent_id, dedupe_key, cutoff):
            log.info("team_chat_dedup", agent_id=agent_id,
                     from_agent_id=from_agent_id, to_agent_id=to_agent_id)
            return "duplicate"

        # Save message to chat_messages
        msg_id = str(uuid.uuid4())
        try:
            await project_db.execute(
                agent_id,
                "INSERT INTO chat_messages (id, agent_id, role, content, "
                "is_background, is_read, is_streaming, team_from_agent_id, "
                "team_to_agent_id, created_at) "
                "VALUES (?, ?, 'team', ?, 0, 0, 0, ?, ?, ?)",
                [msg_id, agent_id, content, from_agent_id, to_agent_id, now_ms])
        except Exception as e:
            log.error("team_chat_save_failed", agent_id=agent_id, error=str(e))
            return "error"

        # Save dedupe key (failure is silent — dedupe is optimization, not required)
        try:
            dedupe_id = str(uuid.uuid4())
            await project_db.execute(
                agent_id,
                "INSERT INTO team_chat_dedupe (id, agent_id, dedupe_key, created_at) "
                "VALUES (?, ?, ?, ?)",
                [dedupe_id, agent_id, dedupe_key, now_ms])
        except Exception as e:
            log.warning("team_chat_dedupe_save_failed", agent_id=agent_id,
                        error=str(e))

        return "ok"

    async def check_and_mark(self, agent_id: str, from_agent_id: str,
                             to_agent_id: str, content: str) -> bool:
        """原子化"检查+登记"去重（供不写 chat_messages 的调用方使用）。

        背景（P2 三连发）：trigger digest 经 ChatMessageService.save_message
        直写 chat_messages，绕过 record_message 的去重，导致同一 digest
        在同一秒内向同一 agent 落库 3 次。调用方（如 agents/trigger.py）
        在写消息前调用本方法：返回 True 表示窗口内重复，应跳过写库；
        返回 False 表示已登记 dedupe_key，可正常写库。

        - dedupe_key 与 record_message 同规则：MD5("{from}:{to}:{content}")
        - fail-open：任何异常返回 False（宁可重复不丢消息）

        Returns: True = duplicate（跳过写库）; False = newly marked（可写库）
        """
        dedupe_key = hashlib.md5(
            f"{from_agent_id}:{to_agent_id}:{content}".encode()
        ).hexdigest()
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - _DEDUPE_WINDOW_MS
        try:
            if await self._is_duplicate(agent_id, dedupe_key, cutoff):
                log.info("team_chat_dedup_mark", agent_id=agent_id,
                         from_agent_id=from_agent_id, to_agent_id=to_agent_id)
                return True
            dedupe_id = str(uuid.uuid4())
            await project_db.execute(
                agent_id,
                "INSERT INTO team_chat_dedupe (id, agent_id, dedupe_key, created_at) "
                "VALUES (?, ?, ?, ?)",
                [dedupe_id, agent_id, dedupe_key, now_ms])
            return False
        except Exception as e:
            log.warning("team_chat_check_mark_failed", agent_id=agent_id,
                        error=str(e))
            return False

    async def get_history(self, agent_id: str, limit: int = 50) -> list[dict]:
        """Get team chat history (chronological order). Default limit 50.

        契约 18: get_history
        - WHERE role='team' AND agent_id=?
        - ORDER BY created_at DESC LIMIT ? → reverse → 正序（oldest first）
        - 返回 [{id, agent_id, content, from_agent_id, to_agent_id, created_at}]
        - 异常返回 []
        """
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT id, agent_id, content, team_from_agent_id, "
                "team_to_agent_id, created_at "
                "FROM chat_messages WHERE role = 'team' AND agent_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                [agent_id, limit])
            return [self._row_to_msg(r) for r in reversed(rows)]
        except Exception as e:
            log.warning("team_chat_history_failed", agent_id=agent_id,
                        error=str(e))
            return []

    async def _is_duplicate(self, agent_id: str, dedupe_key: str,
                            cutoff: int) -> bool:
        """Check if a dedupe_key exists within the window.

        契约 18: is_duplicate? — 异常返回 False（fail-open，宁可重复不丢消息）。
        """
        try:
            row = await project_db.query_one(
                agent_id,
                "SELECT id FROM team_chat_dedupe "
                "WHERE agent_id = ? AND dedupe_key = ? AND created_at > ? LIMIT 1",
                [agent_id, dedupe_key, cutoff])
            return row is not None
        except Exception as e:
            log.warning("team_chat_dedup_check_failed", agent_id=agent_id,
                        error=str(e))
            return False

    @staticmethod
    def _row_to_msg(row) -> dict:
        """Convert DB row to message dict with friendly key names."""
        d = dict(row)
        d["from_agent_id"] = d.pop("team_from_agent_id", None)
        d["to_agent_id"] = d.pop("team_to_agent_id", None)
        return d
