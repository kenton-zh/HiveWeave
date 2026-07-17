"""Inbox service — inter-agent message delivery with wake policy (P0).

- Progress/ACK: upsert + wake=0 (no LLM trigger)
- Command/ask/task_transition: wake=1
- Idempotency keys prevent duplicate progress spam
"""

from __future__ import annotations

import time
import uuid

import structlog

from hiveweave.db import project as project_db
from hiveweave.services.wake_policy import (
    classify_message,
    make_idempotency_key,
    should_wake,
)

log = structlog.get_logger(__name__)

_migrated: set[str] = set()

_MISSING_COLUMNS = [
    ("priority", "TEXT DEFAULT 'normal'"),
    ("task_id", "TEXT"),
    ("wake", "INTEGER DEFAULT 1"),
    ("idempotency_key", "TEXT"),
    # delivered: 是否进入过对话上下文。与 read/wake 正交——
    # progress 类消息 wake=0（不触发 LLM）但必须能在下一次自然触发时
    # 作为 background updates 捎带进上下文，否则证据类消息永久静默丢失。
    # 存量行默认 1（视为已交付，避免迁移后历史消息倒灌）。
    ("delivered", "INTEGER DEFAULT 1"),
]


async def _ensure_schema(agent_id: str) -> None:
    """Add missing columns to inbox table (idempotent)."""
    if agent_id in _migrated:
        return
    for col_name, col_def in _MISSING_COLUMNS:
        try:
            await project_db.execute(
                agent_id,
                f"ALTER TABLE inbox ADD COLUMN {col_name} {col_def}",
            )
        except Exception:
            pass
    # Unique-ish index for idempotency (best-effort)
    try:
        await project_db.execute(
            agent_id,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_idempotency "
            "ON inbox(to_agent_id, idempotency_key) "
            "WHERE idempotency_key IS NOT NULL AND idempotency_key != ''",
        )
    except Exception:
        pass
    _migrated.add(agent_id)


class InboxService:
    """Agent inbox — message delivery with priority, wake flag, read tracking."""

    async def send_message(
        self,
        from_agent_id: str,
        to_agent_id: str,
        message: str,
        message_type: str = "normal",
        priority: str = "normal",
        expect_report: bool = False,
        task_id: str | None = None,
        *,
        wake: bool | None = None,
        idempotency_key: str | None = None,
        recipient_disposition: str | None = None,
    ) -> dict:
        """Send a message. Returns dict including ``should_wake``."""
        try:
            from hiveweave.db import meta as meta_db

            dest = await meta_db.get_agent_by_id(to_agent_id)
            if dest and (dest.get("status") or "") == "archived":
                raise ValueError(
                    f"Cannot message archived agent {to_agent_id[:12]} "
                    f"({dest.get('name', '?')})"
                )
        except ValueError:
            raise
        except Exception as e:
            log.warning("inbox_archived_check_failed", error=str(e))

        category = classify_message(
            message=message,
            message_type=message_type,
            expect_report=expect_report,
            from_agent_id=from_agent_id,
            priority=priority,
            task_id=task_id,
        )
        if wake is None:
            active_waits = None
            from_name = None
            from_short = None
            try:
                from hiveweave.db import meta as meta_db
                from hiveweave.services.wait_contract import wait_contract_service

                pid = await meta_db.get_agent_project_id(to_agent_id)
                if pid:
                    active_waits = await wait_contract_service.list_active(
                        pid, to_agent_id
                    )
                # Resolve sender 花名/short_id — Wait Contract refs use names
                try:
                    sender = await meta_db.get_agent_by_id(from_agent_id)
                    if sender:
                        from_name = sender.get("name")
                        from_short = sender.get("short_id")
                except Exception:
                    pass
                if not from_name:
                    try:
                        row = await project_db.query_one(
                            to_agent_id,
                            "SELECT name, short_id FROM agents WHERE id = ?",
                            [from_agent_id],
                        )
                        if row:
                            from_name = row["name"] if "name" in row.keys() else None
                            from_short = (
                                row["short_id"] if "short_id" in row.keys() else None
                            )
                    except Exception:
                        pass
            except Exception as e:
                log.debug("inbox_wait_lookup_failed", error=str(e))
            wake_flag = should_wake(
                category,
                disposition=recipient_disposition,
                from_agent_id=from_agent_id,
                from_agent_name=from_name,
                from_short_id=from_short,
                active_waits=active_waits or None,
            )
        else:
            wake_flag = bool(wake)

        key = idempotency_key or make_idempotency_key(
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            category=category,
            message=message,
            task_id=task_id,
        )

        await _ensure_schema(to_agent_id)

        # Progress: supersede prior unread progress from same sender
        # 同时 delivered=1：被取代的旧进度不应再作为 background 回灌
        if category == "progress":
            try:
                await project_db.execute(
                    to_agent_id,
                    "UPDATE inbox SET read = 1, delivered = 1 WHERE to_agent_id = ? "
                    "AND read = 0 "
                    "AND from_agent_id = ? AND COALESCE(wake, 1) = 0",
                    [to_agent_id, from_agent_id],
                )
            except Exception as e:
                log.debug("progress_supersede_failed", error=str(e))

        # Idempotent insert: if key exists, return existing / skip wake
        try:
            existing = await project_db.query_one(
                to_agent_id,
                "SELECT id, read, wake FROM inbox "
                "WHERE to_agent_id = ? AND idempotency_key = ? LIMIT 1",
                [to_agent_id, key],
            )
            if existing:
                log.info(
                    "inbox_deduped",
                    to_agent_id=to_agent_id,
                    category=category,
                    key=key[:12],
                )
                try:
                    from hiveweave.services.telemetry import telemetry

                    telemetry.inbox_deduped(to_agent_id, category)
                except Exception:
                    pass
                return {
                    "id": existing["id"],
                    "from_agent_id": from_agent_id,
                    "to_agent_id": to_agent_id,
                    "message": message,
                    "message_type": message_type,
                    "priority": priority,
                    "expect_report": expect_report,
                    # Row 对象无 .get（此前这里抛异常 → 落入 INSERT 撞 UNIQUE
                    # → 返回未落库的幻影 id；修复后正确返回已存在行的 id）
                    "read": bool(existing["read"]),
                    "created_at": int(time.time() * 1000),
                    "task_id": task_id,
                    "should_wake": False,
                    "category": category,
                    "deduped": True,
                }
        except Exception as e:
            log.debug("inbox_idempotency_lookup_failed", error=str(e))

        msg_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        expect = 1 if expect_report else 0
        wake_int = 1 if wake_flag else 0
        # Non-waking progress: insert as already-read so watcher ignores
        read_int = 0 if wake_flag else 1
        # delivered 语义（BUGFIX 消息静默丢失）：
        # wake=1 的消息走 read=0 正常通道进上下文，无需 delivered 追踪（=1）。
        # wake=0 的 progress/ACK 不唤醒 LLM，但**必须**在下一次自然触发时
        # 作为 background updates 捎带进上下文（delivered=0 → 待捎带）。
        # 此前 wake=0 直接 read=1 了事，导致验证通过/交付完成的证据类消息
        # 永远不会进入接收方上下文（CEO 看不到下属的成果证据）。
        delivered_int = 1 if wake_flag else 0

        try:
            await project_db.execute(
                to_agent_id,
                "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
                "created_at, message_type, expect_report, priority, task_id, "
                "wake, idempotency_key, delivered) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    msg_id,
                    from_agent_id,
                    to_agent_id,
                    message,
                    read_int,
                    now_ms,
                    message_type,
                    expect,
                    priority,
                    task_id,
                    wake_int,
                    key,
                    delivered_int,
                ],
            )
        except Exception as e:
            # Unique conflict → treat as dedupe
            if "UNIQUE" in str(e).upper() or "unique" in str(e).lower():
                log.info("inbox_deduped_race", key=key[:12])
                return {
                    "id": msg_id,
                    "from_agent_id": from_agent_id,
                    "to_agent_id": to_agent_id,
                    "message": message,
                    "message_type": message_type,
                    "priority": priority,
                    "expect_report": expect_report,
                    "read": True,
                    "created_at": now_ms,
                    "task_id": task_id,
                    "should_wake": False,
                    "category": category,
                    "deduped": True,
                }
            raise

        log.info(
            "inbox_sent",
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            message_type=message_type,
            priority=priority,
            preview=message[:80],
            task_id=task_id,
            category=category,
            wake=wake_flag,
        )

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
                    "wake": wake_flag,
                    "category": category,
                },
            )
        except Exception as e:
            log.debug("inbox_event_push_failed", error=str(e))

        return {
            "id": msg_id,
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "message": message,
            "message_type": message_type,
            "priority": priority,
            "expect_report": expect_report,
            "read": bool(read_int),
            "created_at": now_ms,
            "task_id": task_id,
            "should_wake": wake_flag,
            "category": category,
            "deduped": False,
        }

    async def get_pending_messages(self, agent_id: str) -> list[dict]:
        """Unread messages that may wake the agent (wake=1 or legacy NULL)."""
        await _ensure_schema(agent_id)
        rows = await project_db.query(
            agent_id,
            "SELECT id, from_agent_id, to_agent_id, message, read, created_at, "
            "message_type, expect_report, priority, task_id, "
            "COALESCE(wake, 1) AS wake "
            "FROM inbox "
            "WHERE to_agent_id = ? AND read = 0 AND COALESCE(wake, 1) = 1 "
            "ORDER BY created_at ASC LIMIT 50",
            [agent_id],
        )
        return [self._row_to_msg(r) for r in rows]

    async def get_undelivered_background(
        self, agent_id: str, limit: int = 20
    ) -> list[dict]:
        """wake=0 且尚未进过任何对话上下文的消息（progress/ACK 类）。

        这些消息不触发 LLM（wake=0），但必须在 agent 因其他原因自然醒来时
        捎带进上下文（作为 background updates），否则交付证据类信息永久丢失。
        """
        await _ensure_schema(agent_id)
        rows = await project_db.query(
            agent_id,
            "SELECT id, from_agent_id, to_agent_id, message, read, created_at, "
            "message_type, expect_report, priority, task_id, "
            "COALESCE(wake, 1) AS wake "
            "FROM inbox "
            "WHERE to_agent_id = ? AND COALESCE(wake, 1) = 0 "
            "AND COALESCE(delivered, 1) = 0 "
            "ORDER BY created_at ASC LIMIT ?",
            [agent_id, limit],
        )
        return [self._row_to_msg(r) for r in rows]

    async def mark_read_by_ids(self, agent_id: str, message_ids: list[str]) -> None:
        """标记消息已读 + 已交付（进过上下文）。

        delivered 与 read 同步置 1：凡是进入过对话上下文的消息（无论 wake 通道
        还是 background 捎带）都视为已交付，不再重复捎带。
        """
        if not message_ids:
            return
        await _ensure_schema(agent_id)
        placeholders = ", ".join(["?"] * len(message_ids))
        await project_db.execute(
            agent_id,
            f"UPDATE inbox SET read = 1, delivered = 1 "
            f"WHERE to_agent_id = ? AND id IN ({placeholders})",
            [agent_id] + message_ids,
        )

    async def mark_all_read(self, agent_id: str) -> None:
        await _ensure_schema(agent_id)
        await project_db.execute(
            agent_id,
            "UPDATE inbox SET read = 1, delivered = 1 "
            "WHERE to_agent_id = ? AND read = 0",
            [agent_id],
        )

    async def supersede_watchdog_messages(
        self, agent_id: str, prefixes: list[str] | None = None
    ) -> int:
        await _ensure_schema(agent_id)
        prefixes = prefixes or (
            "[TASK WATCHDOG]",
            "[WATCHDOG]",
            "[POST-MERGE VERIFY]",
        )
        clauses = " OR ".join(["message LIKE ?" for _ in prefixes])
        params = [agent_id] + [f"{p}%" for p in prefixes]
        try:
            await project_db.execute(
                agent_id,
                f"UPDATE inbox SET read = 1, delivered = 1 "
                f"WHERE to_agent_id = ? AND read = 0 "
                f"AND ({clauses})",
                params,
            )
            return 1
        except Exception as e:
            log.warning(
                "supersede_watchdog_failed", agent_id=agent_id, error=str(e)
            )
            return 0

    async def get_unread_count(self, agent_id: str) -> int:
        await _ensure_schema(agent_id)
        row = await project_db.query_one(
            agent_id,
            "SELECT COUNT(*) AS cnt FROM inbox WHERE to_agent_id = ? AND read = 0",
            [agent_id],
        )
        return row["cnt"] if row else 0

    @staticmethod
    def _row_to_msg(r) -> dict:
        return {
            "id": r["id"],
            "from_agent_id": r["from_agent_id"],
            "to_agent_id": r["to_agent_id"],
            "message": r["message"],
            "read": bool(r["read"]),
            "created_at": r["created_at"],
            "message_type": r["message_type"] if "message_type" in r.keys() else "normal",
            "expect_report": bool(r["expect_report"]) if "expect_report" in r.keys() else False,
            "priority": r["priority"] if "priority" in r.keys() else "normal",
            "task_id": r["task_id"] if "task_id" in r.keys() else None,
            "wake": bool(r["wake"]) if "wake" in r.keys() else True,
        }
