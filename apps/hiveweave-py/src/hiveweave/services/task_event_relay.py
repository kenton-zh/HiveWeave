"""Task event relay — reads task_events outbox and ensures inbox notifications.

The relay is a safety net: task.py already sends inbox messages directly
in most code paths. This relay reads the authoritative task_events table
and fills in any gaps — ensuring no notification is lost even if the
direct path fails or a new state transition is added without a matching
send_message call.

Runs on each game_time tick. Idempotent: uses event_id in the
idempotency_key to prevent duplicate messages across relay runs.

收敛（slack-clone_01 P2-4，治 coordinator 25+ 双写洪泛）：
1. **直推去重**：submitted/approved/rework 三类，工具层已有 wake=True 直推
   （submit.py/review.py/verify_merge.py）。relay 发送前按
   (task_id, recipient, 协议前缀, 事件时间窗) 查 inbox，已有直推则跳过；
   查不到（REST 逃生门等无直推路径）仍由 relay 兜底。
2. **同批合并**：同一任务同批事件里，claimed/running 中间态 FYI 若被更晚
   事件取代（如 submit 自动 claim→start→submit 一串），只留最新事件发声。
   事件本身照常落 task_events（timeline 数据源），只收敛通知。
"""

from __future__ import annotations

import json
import time

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services.inbox import InboxService

log = structlog.get_logger(__name__)

# Relay tick interval (game_time ticks). Runs every ~30s (6 ticks * 5s).
RELAY_TICK_INTERVAL = 6

# 工具层有 wake=True 直推的事件 → 直推消息的协议前缀（平台常量）。
# relay 对这三类只做兜底：直推已送达则跳过。
_DIRECT_COVERED_PREFIX = {
    "task.submitted": "[TASK SUBMITTED]",
    "task.approved": "[TASK APPROVED]",
    "task.rework": "[REWORK REQUESTED]",
}

# 直推与事件落库的时间差容忍（直推在事件之后毫秒级；留窗口防时钟偏斜）
_DIRECT_DEDUPE_SKEW_MS = 60_000

# 审计 F9：去重窗口下界——直推在事件落库之后发送（submit.py/review.py 同一
# 工具调用内），故本轮直推 created_at 必 >= 本轮事件 ev_ts。下界只留极小
# 容差容忍同轮内"直推落在事件落库毫秒前"的抖动，绝不向前扫 60s——否则
# rework 后 60s 内再 submit 会命中**上一轮**残留下的 [TASK SUBMITTED] 直推，
# 静默吞掉本轮"待审查"通知（任务卡 submitted 无人审查的有机 stall）。
_DIRECT_DEDUPE_BACKWARD_MS = 5_000

# 同批内可被更晚事件取代的中间态 FYI
_SUPERSEDABLE_FYI = frozenset({"task.claimed", "task.running"})


class TaskEventRelay:
    """Reads undelivered task_events and creates inbox messages."""

    async def process_pending(self, project_id: str) -> int:
        """Process undelivered task events for a project.

        Returns: number of events processed.
        """
        from hiveweave.services.task import TaskEventService

        svc = TaskEventService()
        events = await svc.get_undelivered(project_id, limit=50)
        if not events:
            return 0

        # P2-4 同批合并：claimed/running 中间态被同任务更晚事件取代时跳过
        # （如 submit 自动 claim→start→submit 一串只让最终态发声）。
        last_idx: dict[str, int] = {}
        for idx, ev in enumerate(events):
            last_idx[ev.get("task_id") or ""] = idx
        sendable = [
            ev
            for idx, ev in enumerate(events)
            if (ev.get("event_type") or "") not in _SUPERSEDABLE_FYI
            or last_idx.get(ev.get("task_id") or "", idx) == idx
        ]

        processed = 0
        for ev in sendable:
            try:
                await self._process_one(project_id, ev)
                processed += 1
            except Exception as e:
                log.warning(
                    "task_event_relay_failed",
                    event_id=ev.get("id"),
                    event_type=ev.get("event_type"),
                    error=str(e),
                )

        # Mark all as delivered (even failed ones — avoid infinite retry)
        event_ids = [e["id"] for e in events if "id" in e]
        if event_ids:
            await svc.mark_delivered(project_id, event_ids)

        if processed:
            log.info(
                "task_event_relay_batch",
                project_id=project_id,
                processed=processed,
                total=len(events),
            )
        return processed

    async def _process_one(self, project_id: str, event: dict) -> None:
        """Process a single task event — determine recipients and send inbox."""
        event_type = event.get("event_type") or ""
        task_id = event.get("task_id") or ""
        actor_id = event.get("actor_id")
        payload_str = event.get("payload") or "{}"
        try:
            payload = json.loads(payload_str) if isinstance(payload_str, str) else dict(payload_str)
        except (json.JSONDecodeError, TypeError):
            payload = {}

        # Fetch task once — recipients + message title both need it
        task = await self._get_task(project_id, task_id)
        if not task:
            return

        # archive_task 同步推送带 reason_code 的恢复指引（同 idempotency key）。
        # 若 relay 在同步推送前抢跑，短 FYI 会占坑、详指引被幂等丢掉。
        # 因此 task.archived 只由 close.py 直推，relay 跳过。
        if event_type == "task.archived":
            return

        # Determine recipients based on event type
        recipients = await self._determine_recipients(
            project_id, event_type, task_id, actor_id, payload, task=task
        )
        if not recipients:
            return

        # Build message content (title from task row — event payload is "{}"
        # for transition events, so falling back to payload would be empty)
        message = self._build_message(
            event_type, task_id, payload, title=task.get("title") or "",
            evidence=task.get("evidence"),
        )

        # P2-4 直推去重：工具层已 wake=True 直推的事件（submit/review 工具
        # 路径），relay 不再重复 FYI；查不到直推（REST 逃生门等）照常兜底。
        direct_prefix = _DIRECT_COVERED_PREFIX.get(event_type)
        if direct_prefix:
            try:
                ev_ts = int(event.get("created_at") or 0)
            except (TypeError, ValueError):
                ev_ts = 0
            recipients = [
                r
                for r in recipients
                if not await self._direct_already_sent(
                    project_id, r, task_id, direct_prefix, ev_ts
                )
            ]
            if not recipients:
                return

        # Send to each recipient (idempotent via event-based key)
        inbox = InboxService()
        event_id = event.get("id", "")
        for recipient_id in recipients:
            idem_key = f"task_event:{event_id}:{recipient_id}"
            try:
                await inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=recipient_id,
                    message=message,
                    message_type="task_event",
                    priority="normal",
                    task_id=task_id,
                    idempotency_key=idem_key,
                    wake=False,  # relay messages are FYI by default
                )
            except Exception as e:
                log.debug(
                    "task_event_relay_send_skipped",
                    event_type=event_type,
                    recipient=recipient_id[:12],
                    error=str(e),
                )

    async def _determine_recipients(
        self,
        project_id: str,
        event_type: str,
        task_id: str,
        actor_id: str | None,
        payload: dict,
        task: dict | None = None,
    ) -> list[str]:
        """Determine who should be notified for this event.

        Rules:
        - task.submitted → creator (must review)
        - task.approved → assignee (work accepted)
        - task.rework → assignee (needs rework)
        - task.closed → creator + assignee
        - task.archived → assignee + creator
        - task.claimed / task.running → creator (work started)
        - task.blocked → creator (may need to unblock)
        - task.verifying → creator (verification began)
        - task.created → no relay (dispatch already delivers the description)
        """
        recipients: list[str] = []

        # Fetch task to get creator_id + assignee_id
        if task is None:
            task = await self._get_task(project_id, task_id)
        if not task:
            return []

        assignee = task.get("assignee_id")
        creator = task.get("creator_id")

        if event_type == "task.submitted":
            # Creator needs to review (unless self-assigned)
            if creator and creator != assignee:
                recipients.append(creator)
        elif event_type == "task.approved":
            # Assignee's work was approved
            if assignee:
                recipients.append(assignee)
        elif event_type == "task.rework":
            # Assignee needs to rework
            if assignee:
                recipients.append(assignee)
        elif event_type == "task.closed":
            # Both parties should know
            if creator:
                recipients.append(creator)
            if assignee and assignee not in recipients:
                recipients.append(assignee)
        elif event_type == "task.archived":
            if assignee:
                recipients.append(assignee)
            if creator and creator not in recipients:
                recipients.append(creator)
        elif event_type == "task.claimed":
            # Creator knows work started (direct path was missing)
            if creator and creator != assignee:
                recipients.append(creator)
        elif event_type == "task.running":
            if creator and creator != assignee:
                recipients.append(creator)
        elif event_type == "task.blocked":
            # Creator may need to unblock (deps / waiting contracts)
            if creator:
                recipients.append(creator)
        elif event_type == "task.verifying":
            # Creator knows verification began
            if creator:
                recipients.append(creator)

        # Don't notify the actor themselves
        if actor_id and actor_id in recipients:
            recipients.remove(actor_id)

        return recipients

    def _build_message(
        self, event_type: str, task_id: str, payload: dict, *, title: str = "",
        evidence: object = None,
    ) -> str:
        """Build inbox message text for the event.

        Title comes from the task row (``_get_task``), not the event payload —
        transition events store ``{}`` payload and archived events store only
        the archive meta (no title).

        TEST_DSH_32 P2（反馈通路）：task.rework 兜底通知附 review_feedback
        全文（evidence 里一等可达），REST 逃生门路径不再只给一句「去看反馈」
        却无处可看。
        """
        title = (title or payload.get("title") or "")[:80]
        short_id = task_id[:8]

        messages = {
            "task.submitted": f"[TASK SUBMITTED] {title} ({short_id}) is ready for review.",
            "task.approved": f"[TASK APPROVED] {title} ({short_id}) has been approved.",
            "task.rework": f"[REWORK REQUESTED] {title} ({short_id}) needs rework. Check review feedback.",
            "task.closed": f"[TASK CLOSED] {title} ({short_id}) is closed.",
            "task.archived": (
                f"[TASK ARCHIVED] {title} ({short_id}) was archived. "
                "恢复指引：若工作仍需继续，用 create_task 重新创建并注明原任务 "
                f"{short_id} 与归档原因。"
            ),
            "task.claimed": f"[TASK CLAIMED] {title} ({short_id}) was claimed.",
            "task.running": f"[TASK RUNNING] {title} ({short_id}) is now in progress.",
            "task.blocked": (
                f"[TASK BLOCKED] {title} ({short_id}) is blocked — check "
                "dependencies / waiting contracts to unblock."
            ),
            "task.verifying": f"[TASK VERIFYING] {title} ({short_id}) verification started.",
        }
        msg = messages.get(event_type, f"[{event_type}] task {short_id}")
        if event_type == "task.rework":
            ev = evidence
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except (json.JSONDecodeError, TypeError):
                    ev = None
            fb = (ev or {}).get("review_feedback") if isinstance(ev, dict) else None
            if fb:
                msg += f" Feedback: {fb}"
        return msg

    async def _direct_already_sent(
        self,
        project_id: str,
        recipient_id: str,
        task_id: str,
        prefix: str,
        event_created_at: int,
    ) -> bool:
        """True when a direct-path notification already covers this event.

        直推在事件落库之后发送（submit.py/review.py 同一工具调用内），
        故本轮直推 created_at 必 >= 本轮事件 ev_ts。去重窗口绑定本轮事件：
        ``created_at >= ev_ts - BACKWARD``（容忍同轮毫秒级抖动）且
        ``created_at <= ev_ts + SKEW``（容忍直推落库稍晚）。绝不向前扫 60s
        ——否则 rework 后快速再 submit 会命中上一轮残留直推，静默吞通知。
        前缀是平台协议常量（代码发出的英文常量），非自然语言意图猜测。
        """
        try:
            from hiveweave.services.task import _query

            rows = await _query(
                project_id,
                "SELECT 1 FROM inbox WHERE to_agent_id = ? AND task_id = ? "
                "AND substr(message, 1, ?) = ? "
                "AND created_at >= ? AND created_at <= ? LIMIT 1",
                [
                    recipient_id,
                    task_id,
                    len(prefix),
                    prefix,
                    max(0, event_created_at - _DIRECT_DEDUPE_BACKWARD_MS),
                    event_created_at + _DIRECT_DEDUPE_SKEW_MS,
                ],
            )
            return bool(rows)
        except Exception as e:
            # 查询失败时保守放行（兜底语义优先：宁重勿丢）
            log.debug("relay_direct_dedupe_check_failed", error=str(e))
            return False

    async def _get_task(self, project_id: str, task_id: str) -> dict | None:
        """Fetch task from per-project DB."""
        try:
            from hiveweave.services.task import _query

            rows = await _query(
                project_id,
                "SELECT assignee_id, creator_id, title, evidence FROM tasks WHERE id = ?",
                [task_id],
            )
            if rows:
                r = rows[0]
                return {
                    "assignee_id": r["assignee_id"] if "assignee_id" in r.keys() else None,
                    "creator_id": r["creator_id"] if "creator_id" in r.keys() else None,
                    "title": r["title"] if "title" in r.keys() else "",
                    "evidence": r["evidence"] if "evidence" in r.keys() else None,
                }
        except Exception as e:
            log.debug("relay_get_task_failed", task_id=task_id[:12], error=str(e))
        return None


# Singleton
task_event_relay = TaskEventRelay()
