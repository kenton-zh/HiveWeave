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
    make_idempotency_key,
    should_wake,
)

log = structlog.get_logger(__name__)

_migrated: set[str] = set()

# Give-up ACK / deactivate-park must NOT swallow these — review & escalation
# obligations live in the inbox wake path (BUGFIX: TEST7 429→ACK killed reviews).
ACK_SPARE_MESSAGE_TYPES: frozenset[str] = frozenset({"escalation", "ask"})
ACK_SPARE_PREFIXES: tuple[str, ...] = (
    "[TASK SUBMITTED]",
    "[ESCALATION]",
    "[REWORK REQUESTED]",
    "[LEDGER REVIEW]",
    "[MERGE PENDING]",
    "[MERGE PROXY]",
    "[PEER_REVIEW_DEADLOCK]",
    "[POST-MERGE VERIFY]",
)
PARK_EXEMPT_MESSAGE_TYPES: frozenset[str] = frozenset({"escalation", "ask"})


def should_spare_from_give_up_ack(msg: dict | None) -> bool:
    """True → keep unread/wake on consecutive-error give-up (do not mark_read)."""
    if not msg:
        return False
    if msg.get("expect_report"):
        return True
    mt = (msg.get("message_type") or "").lower()
    if mt in ACK_SPARE_MESSAGE_TYPES:
        return True
    text = (msg.get("message") or "").lstrip()
    return any(text.startswith(p) for p in ACK_SPARE_PREFIXES)


def should_exempt_from_park(msg: dict | None) -> bool:
    """True → deactivate/activate park must leave this wake=1 message alone."""
    if not msg:
        return False
    mt = (msg.get("message_type") or "").lower()
    if mt in PARK_EXEMPT_MESSAGE_TYPES:
        return True
    if msg.get("expect_report"):
        return True
    text = (msg.get("message") or "").lstrip()
    return any(text.startswith(p) for p in ACK_SPARE_PREFIXES)

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
    # parked: deactivate 时把 wake=1 未读压住，activate 时合并成一条 briefing
    ("parked", "INTEGER DEFAULT 0"),
    # triage: batch assignment + persisted wake category for digests
    ("triage_batch_id", "TEXT"),
    ("wake_category", "TEXT"),
    # delivery_state: 正式交付状态机 pending → delivered → acked
    # 存量行按 delivered/read 推导默认值
    ("delivery_state", "TEXT DEFAULT 'delivered'"),
    # reply_contract_id: 消息要求回复时的合约 ID（expect_report=1 时生成）
    ("reply_contract_id", "TEXT"),
    # reply_to: 回复消息引用的原始合约 ID
    ("reply_to", "TEXT"),
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
        reply_to: str | None = None,
    ) -> dict:
        """Send a message. Returns dict including ``should_wake``.

        Args:
            reply_to: reply_contract_id of the original message being replied to.
                When set, closes the reply contract — collect_unreplied_asks
                checks for matching reply_to instead of heuristic recipient scan.
        """
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

        # No category taxonomy — wake always (unless caller passes wake=False).
        category = "message"
        wake_flag = bool(wake) if wake is not None else should_wake()

        # TEST13 P2-2: open task_id + explicit wake=False → force wake.
        # NL "forward" with wake=0 left executors idle forever.
        force_wake_note = ""
        if task_id and wake is False:
            try:
                from hiveweave.services.task import TaskService

                # Resolve project via recipient
                from hiveweave.db import meta as meta_db

                dest_row = await meta_db.get_agent_by_id(to_agent_id)
                pid = (dest_row or {}).get("project_id")
                open_task = False
                if pid:
                    t = await TaskService().get_task(pid, str(task_id))
                    if t and not t.get("is_archived"):
                        st = (t.get("status") or "").lower()
                        if st not in ("closed", "cancelled"):
                            open_task = True
                            assignee = t.get("assignee_id")
                            if assignee and str(assignee) != str(to_agent_id):
                                force_wake_note = (
                                    "warning: recipient is not task assignee — "
                                    "they have no obligation; use reassign_task"
                                )
                if open_task:
                    wake_flag = True
                    log.info(
                        "inbox_force_wake_open_task",
                        task_id=str(task_id)[:8],
                        to_agent_id=to_agent_id[:8],
                        note=force_wake_note or "wake forced",
                    )
            except Exception as e:
                log.warning("inbox_force_wake_check_failed", error=str(e))

        key = idempotency_key or make_idempotency_key(
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            message=message,
            task_id=task_id,
        )

        await _ensure_schema(to_agent_id)
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

        # ── Auto-close reply contract（TEST10 修复）────────────────────
        # 背景: LLM 常忽略 how_to_reply 提示、不显式传 reply_to 就直接
        # send_message 回复对方，导致 reply_to 全为 NULL、合约永远关闭不了，
        # commit_turn 被 UNREPLIED_ASKS gate 反复死锁。
        # 这里在发送方未显式传 reply_to 时，自动关联「收件人 → 发送方」方向
        # 最老一条未关闭合约。系统消息（task/task_event/alarm）不参与，
        # 避免 [TASK APPROVED] 之类的通知误关合约。
        if (
            reply_to is None
            and from_agent_id
            and to_agent_id != from_agent_id
            and message_type in ("normal", "ask", "notify")
        ):
            try:
                open_contracts = await project_db.query(
                    from_agent_id,
                    "SELECT reply_contract_id FROM inbox "
                    "WHERE from_agent_id = ? AND to_agent_id = ? "
                    "AND reply_contract_id IS NOT NULL "
                    "ORDER BY created_at ASC",
                    [to_agent_id, from_agent_id],
                )
                if open_contracts:
                    closed_rows = await project_db.query(
                        to_agent_id,
                        "SELECT DISTINCT reply_to FROM inbox "
                        "WHERE from_agent_id = ? AND to_agent_id = ? "
                        "AND reply_to IS NOT NULL",
                        [from_agent_id, to_agent_id],
                    )
                    closed_set = {
                        r["reply_to"] for r in closed_rows if r["reply_to"]
                    }
                    for c in open_contracts:
                        cid = c["reply_contract_id"]
                        if cid and cid not in closed_set:
                            reply_to = cid
                            log.info(
                                "inbox_auto_close_reply_contract",
                                from_agent_id=from_agent_id,
                                to_agent_id=to_agent_id,
                                contract=cid[:8],
                            )
                            break
            except Exception as e:
                log.debug("inbox_auto_close_contract_failed", error=str(e))

        # 根因修复: ask-reply 链断裂 — auto-close 匹配到 reply_to（或 LLM
        # 显式传 reply_to）说明本消息是对先前 ask 的回复。回复不应再产生
        # 新的 reply 义务，否则 A→ask→B→ask→A→ask→B... 无限循环
        # （TEST11 doom loop 根因）。
        # 必须在 expect/contract_id 计算之前执行，否则 DB 仍写入
        # expect_report=1 + 新合约 ID，降级形同虚设。
        # 结构化判定（基于 reply_contract 匹配），不扫描文案。
        if reply_to is not None and (
            expect_report or (message_type or "").lower() == "ask"
        ):
            expect_report = False
        # Also strip message_type=ask — reply_required is expect_report-only;
        # leaving mt=ask would confuse logs/UI even though gates ignore it.
            if (message_type or "").lower() == "ask":
                message_type = "notify"
            log.info(
                "ask_chain_downgraded",
                from_agent_id=from_agent_id,
                to_agent_id=to_agent_id,
                reply_to=reply_to[:8],
                message_type=message_type,
            )

        # 在降级之后计算 DB 写入值，确保降级生效
        expect = 1 if expect_report else 0
        wake_int = 1 if wake_flag else 0
        # Product default: messages wake (wake_flag True → unread pending).
        # Explicit wake=False still parks as background (delivered=0).
        read_int = 0 if wake_flag else 1
        delivered_int = 1 if wake_flag else 0
        # Generate reply_contract_id only when this message requires a reply
        # （降级后 expect_report=False → 不生成新合约，打破循环）
        contract_id = str(uuid.uuid4()) if expect_report else None

        try:
            await project_db.execute(
                to_agent_id,
                "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
                "created_at, message_type, expect_report, priority, task_id, "
                "wake, idempotency_key, delivered, wake_category, "
                "reply_contract_id, reply_to) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    category,
                    contract_id,
                    reply_to,
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
            "reply_contract_id": contract_id,
            **({"warning": force_wake_note} if force_wake_note else {}),
        }

    async def get_pending_messages(self, agent_id: str) -> list[dict]:
        """Unread messages that may wake the agent (wake=1 or legacy NULL)."""
        await _ensure_schema(agent_id)
        rows = await project_db.query(
            agent_id,
            "SELECT id, from_agent_id, to_agent_id, message, read, created_at, "
            "message_type, expect_report, priority, task_id, "
            "COALESCE(wake, 1) AS wake, wake_category, triage_batch_id, "
            "reply_contract_id "
            "FROM inbox "
            "WHERE to_agent_id = ? AND read = 0 AND COALESCE(wake, 1) = 1 "
            "ORDER BY created_at ASC LIMIT 80",
            [agent_id],
        )
        return [self._row_to_msg(r) for r in rows]

    async def count_pending_and_background(self, agent_id: str) -> tuple[int, int]:
        """Total wake=1 unread and undelivered background (no LIMIT)."""
        await _ensure_schema(agent_id)
        pending = 0
        background = 0
        try:
            row = await project_db.query_one(
                agent_id,
                "SELECT COUNT(*) AS c FROM inbox "
                "WHERE to_agent_id = ? AND read = 0 AND COALESCE(wake, 1) = 1",
                [agent_id],
            )
            pending = int(row["c"]) if row else 0
            row2 = await project_db.query_one(
                agent_id,
                "SELECT COUNT(*) AS c FROM inbox "
                "WHERE to_agent_id = ? AND COALESCE(wake, 1) = 0 "
                "AND COALESCE(delivered, 1) = 0",
                [agent_id],
            )
            background = int(row2["c"]) if row2 else 0
        except Exception as e:
            log.debug("inbox_count_failed", error=str(e))
        return pending, background

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
            "COALESCE(wake, 1) AS wake, wake_category, triage_batch_id "
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
            f"UPDATE inbox SET read = 1, delivered = 1, wake = 0 "
            f"WHERE to_agent_id = ? AND id IN ({placeholders})",
            [agent_id] + message_ids,
        )
        # Consume triage batch(es) covering these messages
        try:
            from hiveweave.services.inbox_triage import inbox_triage_service

            rows = await project_db.query(
                agent_id,
                f"SELECT DISTINCT triage_batch_id FROM inbox "
                f"WHERE id IN ({placeholders}) AND triage_batch_id IS NOT NULL",
                message_ids,
            )
            for r in rows or []:
                bid = r["triage_batch_id"] if "triage_batch_id" in r.keys() else None
                if bid:
                    await inbox_triage_service.mark_consumed(agent_id, bid)
        except Exception as e:
            log.debug("inbox_triage_consume_on_read_failed", error=str(e))

    async def mark_all_read(self, agent_id: str) -> None:
        await _ensure_schema(agent_id)
        await project_db.execute(
            agent_id,
            "UPDATE inbox SET read = 1, delivered = 1, wake = 0 "
            "WHERE to_agent_id = ? AND read = 0",
            [agent_id],
        )

    async def supersede_watchdog_messages(
        self,
        agent_id: str,
        prefixes: list[str] | tuple[str, ...] | None = None,
        *,
        contains: str | None = None,
    ) -> int:
        await _ensure_schema(agent_id)
        if prefixes is None:
            prefixes = [
                "[TASK WATCHDOG]",
                "[WATCHDOG]",
                "[POST-MERGE VERIFY]",
            ]
        clauses = " OR ".join(["message LIKE ?" for _ in prefixes])
        params: list = [agent_id] + [f"{p}%" for p in prefixes]
        extra = ""
        if contains:
            extra = " AND message LIKE ?"
            params.append(f"%{contains}%")
        try:
            await project_db.execute(
                agent_id,
                f"UPDATE inbox SET read = 1, delivered = 1 "
                f"WHERE to_agent_id = ? AND read = 0 "
                f"AND ({clauses}){extra}",
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

    async def get_messages_by_ids(
        self, agent_id: str, message_ids: list[str]
    ) -> list[dict]:
        """Fetch inbox rows by id (any read/wake state)."""
        ids = [m for m in (message_ids or []) if m]
        if not ids:
            return []
        await _ensure_schema(agent_id)
        placeholders = ", ".join(["?"] * len(ids))
        rows = await project_db.query(
            agent_id,
            "SELECT id, from_agent_id, to_agent_id, message, read, created_at, "
            "message_type, expect_report, priority, task_id, "
            "COALESCE(wake, 1) AS wake, COALESCE(parked, 0) AS parked, "
            "wake_category, triage_batch_id "
            f"FROM inbox WHERE to_agent_id = ? AND id IN ({placeholders})",
            [agent_id, *ids],
        )
        return [self._row_to_msg(r) for r in rows]

    async def partition_give_up_ack(
        self, agent_id: str, message_ids: list[str]
    ) -> tuple[list[str], list[str]]:
        """Split ids into (ack_now, spare_review_critical)."""
        ids = [m for m in (message_ids or []) if m]
        if not ids:
            return [], []
        rows = await self.get_messages_by_ids(agent_id, ids)
        by_id = {m["id"]: m for m in rows if m.get("id")}
        to_ack: list[str] = []
        to_spare: list[str] = []
        for mid in ids:
            if should_spare_from_give_up_ack(by_id.get(mid)):
                to_spare.append(mid)
            else:
                to_ack.append(mid)
        return to_ack, to_spare

    async def ensure_wake(self, agent_id: str, message_ids: list[str]) -> int:
        """Force wake=1, parked=0 on unread ids (keep review-critical alive)."""
        ids = [m for m in (message_ids or []) if m]
        if not ids:
            return 0
        await _ensure_schema(agent_id)
        placeholders = ", ".join(["?"] * len(ids))
        try:
            conn = await project_db.get_project_db_for_agent(agent_id)
            cursor = await conn.execute(
                f"UPDATE inbox SET wake = 1, parked = 0 "
                f"WHERE to_agent_id = ? AND read = 0 "
                f"AND id IN ({placeholders})",
                [agent_id, *ids],
            )
            await conn.commit()
            n = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            await cursor.close()
            return int(n)
        except Exception as e:
            log.warning("inbox_ensure_wake_failed", agent_id=agent_id, error=str(e))
            return 0

    async def park_pending_wakes(self, agent_id: str) -> int:
        """Deactivate: demote wake=1 unread → wake=0 + parked=1 (no LLM stampede).

        Escalation / ask / TASK SUBMITTED / LEDGER REVIEW stay wake=1 so
        leadership and review obligations are not silenced by off-duty park.
        """
        await _ensure_schema(agent_id)
        try:
            pending = await project_db.query(
                agent_id,
                "SELECT id, message, message_type, expect_report FROM inbox "
                "WHERE to_agent_id = ? AND read = 0 AND COALESCE(wake, 1) = 1",
                [agent_id],
            )
            park_ids: list[str] = []
            for r in pending or []:
                msg = {
                    "id": r["id"],
                    "message": r["message"] if "message" in r.keys() else "",
                    "message_type": (
                        r["message_type"] if "message_type" in r.keys() else "normal"
                    ),
                    "expect_report": (
                        bool(r["expect_report"])
                        if "expect_report" in r.keys()
                        else False
                    ),
                }
                if should_exempt_from_park(msg):
                    continue
                if msg.get("id"):
                    park_ids.append(msg["id"])
            if not park_ids:
                return 0
            placeholders = ", ".join(["?"] * len(park_ids))
            conn = await project_db.get_project_db_for_agent(agent_id)
            cursor = await conn.execute(
                f"UPDATE inbox SET wake = 0, parked = 1 "
                f"WHERE to_agent_id = ? AND id IN ({placeholders})",
                [agent_id, *park_ids],
            )
            await conn.commit()
            n = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            await cursor.close()
            if n:
                log.info("inbox_parked", agent_id=agent_id, count=n)
            return int(n)
        except Exception as e:
            log.warning("inbox_park_failed", agent_id=agent_id, error=str(e))
            return 0

    async def demote_wake(
        self,
        agent_id: str,
        msg_ids: list[str],
        *,
        reason: str = "",
    ) -> int:
        """Set wake=0 for unread ids without ACK (keep read=0).

        Used when trigger/chat skips (e.g. complete gate) so the 5s watcher
        stops spinning, while leaving messages available for a later real wake.
        """
        ids = [m for m in (msg_ids or []) if m]
        if not ids:
            return 0
        await _ensure_schema(agent_id)
        try:
            placeholders = ", ".join("?" * len(ids))
            conn = await project_db.get_project_db_for_agent(agent_id)
            cursor = await conn.execute(
                f"UPDATE inbox SET wake = 0 "
                f"WHERE to_agent_id = ? AND read = 0 "
                f"AND COALESCE(wake, 1) = 1 AND id IN ({placeholders})",
                [agent_id, *ids],
            )
            await conn.commit()
            n = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            await cursor.close()
            if n:
                log.info(
                    "inbox_wake_demoted",
                    agent_id=agent_id,
                    count=n,
                    reason=reason or "",
                )
            return int(n)
        except Exception as e:
            log.warning(
                "inbox_demote_wake_failed",
                agent_id=agent_id,
                error=str(e),
            )
            return 0

    async def get_outstanding_ask_recipients(
        self, from_agent_id: str
    ) -> set[str]:
        """Recipients of unanswered expect_report/ask messages from this agent.

        Used by WAIT_WITHOUT_ASK (TEST11 #1a): waiting on X is legal if an
        unanswered ask to X already exists (even if sent in a prior turn).
        An ask is outstanding when its reply_contract_id has not been closed
        via any reply_to, or (fallback) the message is still unread.
        """
        await _ensure_schema(from_agent_id)
        try:
            rows = await project_db.query(
                from_agent_id,
                "SELECT to_agent_id, reply_contract_id, read FROM inbox "
                "WHERE from_agent_id = ? AND expect_report = 1 "
                "ORDER BY created_at DESC LIMIT 100",
                [from_agent_id],
            )
            if not rows:
                return set()
            contracts = [
                r["reply_contract_id"]
                for r in rows
                if r.get("reply_contract_id")
            ]
            closed: set[str] = set()
            if contracts:
                # Find which contracts have been replied to (any agent)
                placeholders = ",".join("?" * len(contracts))
                closed_rows = await project_db.query(
                    from_agent_id,
                    f"SELECT DISTINCT reply_to FROM inbox "
                    f"WHERE reply_to IN ({placeholders})",
                    list(contracts),
                )
                closed = {
                    r["reply_to"] for r in closed_rows if r.get("reply_to")
                }
            out: set[str] = set()
            for r in rows:
                to_id = r.get("to_agent_id")
                if not to_id:
                    continue
                cid = r.get("reply_contract_id")
                if cid and cid in closed:
                    continue
                if cid and cid not in closed:
                    out.add(to_id)
                elif not cid and not r.get("read"):
                    # Legacy ask without contract: unread counts as outstanding
                    out.add(to_id)
            return out
        except Exception as e:
            log.debug("inbox_outstanding_asks_failed", error=str(e))
            return set()

    async def get_sent_recipients_since(
        self, agent_id: str, since_ms: int
    ) -> set[str]:
        """Recipients (to_agent_id) this agent successfully messaged since ``since_ms``.

        reply_required 硬门用：inbox 落库即 send_message/message 工具成功
        送达的 DB 证据（工具调用本身可能失败，以落库为准）。
        """
        await _ensure_schema(agent_id)
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT DISTINCT to_agent_id FROM inbox "
                "WHERE from_agent_id = ? AND created_at >= ?",
                [agent_id, int(since_ms)],
            )
            return {r["to_agent_id"] for r in rows if r.get("to_agent_id")}
        except Exception as e:
            log.debug("inbox_sent_since_failed", error=str(e))
            return set()

    async def get_replied_contracts_since(
        self, agent_id: str, since_ms: int
    ) -> set[str]:
        """Reply contract IDs that this agent has closed since ``since_ms``.

        Returns the set of reply_to values from messages sent by this agent.
        Used by collect_unreplied_asks to deterministically check if a
        reply contract has been fulfilled.
        """
        await _ensure_schema(agent_id)
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT DISTINCT reply_to FROM inbox "
                "WHERE from_agent_id = ? AND created_at >= ? "
                "AND reply_to IS NOT NULL",
                [agent_id, int(since_ms)],
            )
            return {r["reply_to"] for r in rows if r.get("reply_to")}
        except Exception as e:
            log.debug("inbox_replied_contracts_failed", error=str(e))
            return set()

    async def get_pending_ids_since(
        self, agent_id: str, since_ms: int
    ) -> list[str]:
        """Unread wake=1 message ids created at/after ``since_ms`` (mid-turn)."""
        await _ensure_schema(agent_id)
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT id FROM inbox "
                "WHERE to_agent_id = ? AND read = 0 "
                "AND COALESCE(wake, 1) = 1 AND created_at >= ? "
                "ORDER BY created_at ASC",
                [agent_id, int(since_ms)],
            )
            return [r["id"] for r in rows if r.get("id")]
        except Exception as e:
            log.debug("inbox_pending_since_failed", error=str(e))
            return []

    async def list_parked_messages(self, agent_id: str, limit: int = 50) -> list[dict]:
        await _ensure_schema(agent_id)
        rows = await project_db.query(
            agent_id,
            "SELECT id, from_agent_id, to_agent_id, message, read, created_at, "
            "message_type, expect_report, priority, task_id, "
            "COALESCE(wake, 0) AS wake, COALESCE(parked, 0) AS parked "
            "FROM inbox "
            "WHERE to_agent_id = ? AND COALESCE(parked, 0) = 1 AND read = 0 "
            "ORDER BY created_at ASC LIMIT ?",
            [agent_id, limit],
        )
        return [self._row_to_msg(r) for r in rows]

    async def deliver_parked_briefing(self, agent_id: str) -> tuple[int, bool]:
        """Activate: coalesce parked inbox into one wake message; clear parked rows.

        Returns (parked_cleared_count, briefing_sent).
        """
        parked = await self.list_parked_messages(agent_id)
        if not parked:
            return 0, False

        # Resolve sender names for a short digest
        lines: list[str] = []
        for m in parked[:12]:
            preview = (m.get("message") or "").replace("\n", " ").strip()[:100]
            lines.append(f"- from={m.get('from_agent_id', '?')[:8]}…: {preview}")
        extra = len(parked) - len(lines)
        if extra > 0:
            lines.append(f"- …另有 {extra} 条已折叠")

        briefing = (
            f"[RESUME AFTER OFF-DUTY] 下班期间积压 {len(parked)} 条待处理消息"
            f"（已合并，避免复工踩踏）。请按优先级处理：\n"
            + "\n".join(lines)
        )

        ids = [m["id"] for m in parked if m.get("id")]
        await self.mark_read_by_ids(agent_id, ids)
        # Clear parked flag even if already marked read
        try:
            await project_db.execute(
                agent_id,
                "UPDATE inbox SET parked = 0 WHERE to_agent_id = ? AND COALESCE(parked, 0) = 1",
                [agent_id],
            )
        except Exception as e:
            log.warning("clear_parked_flag_failed", agent_id=agent_id, error=str(e))

        # One coalesced wake from system — wakes the agent once
        await self.send_message(
            from_agent_id="system",
            to_agent_id=agent_id,
            message=briefing,
            message_type="resume_briefing",
            priority="normal",
            expect_report=False,
            wake=True,
        )
        log.info(
            "resume_briefing_sent",
            agent_id=agent_id,
            parked_cleared=len(ids),
        )
        return len(ids), True

    @staticmethod
    def _row_to_msg(r) -> dict:
        keys = r.keys() if hasattr(r, "keys") else []
        return {
            "id": r["id"],
            "from_agent_id": r["from_agent_id"],
            "to_agent_id": r["to_agent_id"],
            "message": r["message"],
            "read": bool(r["read"]),
            "created_at": r["created_at"],
            "message_type": r["message_type"] if "message_type" in keys else "normal",
            "expect_report": bool(r["expect_report"]) if "expect_report" in keys else False,
            "priority": r["priority"] if "priority" in keys else "normal",
            "task_id": r["task_id"] if "task_id" in keys else None,
            "wake": bool(r["wake"]) if "wake" in keys else True,
            "parked": bool(r["parked"]) if "parked" in keys else False,
            "wake_category": r["wake_category"] if "wake_category" in keys else None,
            "triage_batch_id": (
                r["triage_batch_id"] if "triage_batch_id" in keys else None
            ),
        }
