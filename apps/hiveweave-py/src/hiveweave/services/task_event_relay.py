"""Task event relay — reads task_events outbox and ensures inbox notifications.

The relay is a safety net: task.py already sends inbox messages directly
in most code paths. This relay reads the authoritative task_events table
and fills in any gaps — ensuring no notification is lost even if the
direct path fails or a new state transition is added without a matching
send_message call.

Runs on each game_time tick. Idempotent: uses event_id in the
idempotency_key to prevent duplicate messages across relay runs.
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

        processed = 0
        for ev in events:
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
            event_type, task_id, payload, title=task.get("title") or ""
        )

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
        self, event_type: str, task_id: str, payload: dict, *, title: str = ""
    ) -> str:
        """Build inbox message text for the event.

        Title comes from the task row (``_get_task``), not the event payload —
        transition events store ``{}`` payload and archived events store only
        the archive meta (no title).
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
        return messages.get(event_type, f"[{event_type}] task {short_id}")

    async def _get_task(self, project_id: str, task_id: str) -> dict | None:
        """Fetch task from per-project DB."""
        try:
            from hiveweave.services.task import _query

            rows = await _query(
                project_id,
                "SELECT assignee_id, creator_id, title FROM tasks WHERE id = ?",
                [task_id],
            )
            if rows:
                r = rows[0]
                return {
                    "assignee_id": r["assignee_id"] if "assignee_id" in r.keys() else None,
                    "creator_id": r["creator_id"] if "creator_id" in r.keys() else None,
                    "title": r["title"] if "title" in r.keys() else "",
                }
        except Exception as e:
            log.debug("relay_get_task_failed", task_id=task_id[:12], error=str(e))
        return None


# Singleton
task_event_relay = TaskEventRelay()
