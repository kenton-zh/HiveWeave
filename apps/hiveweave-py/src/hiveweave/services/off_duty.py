"""Off-duty (project not started) auto-reply for user-facing chat.

When ``projects.is_started = 0`` (下班), agents are stopped. User messages
must still get a polite UI reply instead of silent drop / "Agent not running".
"""

from __future__ import annotations

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.chat_message import ChatMessageService

log = structlog.get_logger(__name__)

OFF_DUTY_REPLY = "我已下班，上班后再聊 😊"


async def is_project_off_duty(project_id: str | None) -> bool:
    """True when project missing or ``is_started`` is falsy."""
    if not project_id:
        return True
    row = await meta_db.query_one(
        "SELECT is_started FROM projects WHERE id = ?",
        [project_id],
    )
    if not row:
        return True
    return not bool(dict(row).get("is_started"))


async def is_agent_off_duty(agent_id: str) -> bool:
    """Resolve agent's project and check off-duty."""
    project_id = await meta_db.get_agent_project_id(agent_id)
    return await is_project_off_duty(project_id)


async def send_off_duty_auto_reply(agent_id: str) -> dict:
    """Persist assistant off-duty reply and push stream events for live UI.

    Returns the saved chat_messages row dict ``{id, role, content, created_at}``.
    """
    chat = ChatMessageService()
    saved = await chat.save_message(
        {
            "agent_id": agent_id,
            "role": "assistant",
            "content": OFF_DUTY_REPLY,
            "is_streaming": False,
            "is_background": False,
            "is_read": True,
        }
    )

    try:
        from hiveweave.realtime.event_bus import status_event_bus

        # message_id first so UI has a stable assistant row before chunks
        await status_event_bus.publish(
            f"agent:{agent_id}",
            {
                "type": "message_id",
                "id": saved["id"],
                "agentId": agent_id,
                "role": "assistant",
                "content": OFF_DUTY_REPLY,
            },
        )
        await status_event_bus.publish_stream_event(
            agent_id,
            {
                "type": "text_delta",
                "content": OFF_DUTY_REPLY,
                "agentId": agent_id,
            },
        )
        await status_event_bus.publish_stream_event(
            agent_id,
            {
                "type": "done",
                "content": OFF_DUTY_REPLY,
                "agentId": agent_id,
            },
        )
    except Exception as e:
        log.warning(
            "off_duty_broadcast_failed",
            agent_id=agent_id,
            error=str(e),
        )

    log.info("off_duty_auto_reply", agent_id=agent_id, msg_id=saved["id"])
    return saved
