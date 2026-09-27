"""deliver_user_message — 用户消息统一投递入口（spec §4.1 对话桥协议）。

三面一管道（spec §1）：网页 chat / 飞书（P1+）/ 桌面悬浮球是"面"，不是
另一套事实源。所有用户→agent 的自然语言消息都经本入口投递：

    存 chat_messages（metadata.source 区分来源 web|ball|feishu）
      → agent 忙 → InboxService 排队（from="用户"，trusted_platform）
      → agent 闲 → agent.chat()（BUG-036 JSON 信封）

off-duty / 项目未启动的自动回复因共用入口而在三端自动一致。

抽取自 ``realtime/phoenix_adapter.py:_handle_chat_push``（原网页端逻辑），
语义逐条对齐；phoenix 与悬浮球通道均调用本函数。

Result contract（结构化结果，调用方自行映射到各自通道协议）::

    {
        "ok": bool,                 # 消息已投递（含排队/off-duty 场景）
        "outcome": str,             # started|queued|off_duty|paused|not_found|insert_failed
        "user_message_id": str|None,
        "assistant_message_id": str|None,   # off-duty 自动回复时非空
        "assistant_content": str|None,      # off-duty 自动回复文本
        "error": str|None,
    }
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable

import structlog

from hiveweave.services.inbox import InboxService

log = structlog.get_logger(__name__)

#: 用户消息信封（BUG-036）：chat() 收 JSON 结构化消息，落库存纯文本
_USER_ENVELOPE_FROM = "用户"


async def deliver_user_message(
    agent_id: str,
    content: str,
    source: str = "web",
    *,
    images: list[str] | None = None,
    on_user_saved: Callable[[dict], Awaitable[None]] | None = None,
) -> dict:
    """投递一条用户消息到 agent（三端共用入口，spec §4.1）。

    Args:
        agent_id: 目标 agent UUID。
        content: 用户消息纯文本（落库内容；chat() 另行包信封）。
        source: 来源面标识，写入 ``chat_messages.metadata.source``
            （``web`` | ``ball`` | ``feishu``）。
        images: 可选 data URL 图片列表（仅 idle 直聊路径喂模型；
            busy 排队/off-duty 路径与原网页端行为一致，不透传）。
        on_user_saved: 可选回调——用户消息落库后、``agent.chat()`` 启动前
            调用。phoenix 通道用它先发 ``message_id`` ack（时序契约：
            ack 必须早于流式 delta），悬浮球等无 ack 通道不传。

    Returns:
        结构化结果 dict（见模块 docstring）。
    """
    # 审计 H2：调用时导入（勿提模块级）——monkey-patch 窗口内首次导入会把
    # 该用例的 fake 固化进模块全局，后续用例全部打在旧 fake 上。
    from hiveweave.agents.supervisor import agent_manager
    from hiveweave.services.chat_message import ChatMessageService

    chat_service = ChatMessageService()

    async def _save_user_message(*, is_read: bool = True) -> dict | None:
        try:
            saved = await chat_service.save_message(
                {
                    "agent_id": agent_id,
                    "role": "user",
                    "content": content,
                    "is_streaming": False,
                    "is_read": is_read,
                    "images": images,
                    "metadata": {"source": source},
                }
            )
        except Exception as e:
            log.warning(
                "deliver_user_message_save_failed",
                agent_id=agent_id,
                source=source,
                error=str(e),
            )
            return None
        if on_user_saved is not None:
            try:
                await on_user_saved(saved)
            except Exception as e:
                log.warning(
                    "deliver_user_message_ack_failed",
                    agent_id=agent_id,
                    error=str(e),
                )
        return saved

    # ── agent 不在运行 ────────────────────────────────────────
    agent = agent_manager.get_agent(agent_id)
    if agent is None:
        from hiveweave.services.off_duty import is_agent_off_duty, send_off_duty_auto_reply

        if await is_agent_off_duty(agent_id):
            saved_off = await _save_user_message()
            try:
                asst = await send_off_duty_auto_reply(agent_id)
            except Exception as e:
                log.warning(
                    "deliver_user_message_off_duty_reply_failed",
                    agent_id=agent_id,
                    error=str(e),
                )
                return {
                    "ok": False,
                    "outcome": "off_duty",
                    "user_message_id": (saved_off or {}).get("id"),
                    "assistant_message_id": None,
                    "assistant_content": None,
                    "error": "off-duty auto-reply failed",
                }
            return {
                "ok": True,
                "outcome": "off_duty",
                "user_message_id": (saved_off or {}).get("id"),
                "assistant_message_id": asst["id"],
                "assistant_content": asst["content"],
                "error": None,
            }
        return {
            "ok": False,
            "outcome": "not_found",
            "user_message_id": None,
            "assistant_message_id": None,
            "assistant_content": None,
            "error": "Agent not running",
        }

    # ── agent 忙 → 插话窗口之外的语义是排队（BUG-036：不丢消息）────
    if agent.status.value == "processing":
        saved_busy = await _save_user_message()
        # Store PLAIN text only: from_agent_id + message_type already identify
        # the sender (pre-wrapping caused double-encoded trigger digests).
        await InboxService().send_message(
            from_agent_id=_USER_ENVELOPE_FROM,
            to_agent_id=agent_id,
            message=content,
            message_type="user_message",
            priority="normal",
            trusted_platform=True,
        )
        return {
            "ok": True,
            "outcome": "queued",
            "user_message_id": (saved_busy or {}).get("id"),
            "assistant_message_id": None,
            "assistant_content": None,
            "error": None,
        }

    # ── agent 闲 → 直聊 ──────────────────────────────────────
    saved = await _save_user_message()

    # 防御性回调补丁（与原 phoenix 路径一致）：agent 缺流式回调时补挂，
    # 否则本轮回复不会广播到总线，前端收不到任何流事件。
    if getattr(agent, "_on_stream_event", None) is None:
        from hiveweave.realtime.event_bus import create_agent_callbacks

        project_id = getattr(agent, "project_id", "") or ""
        on_status, on_stream = create_agent_callbacks(agent_id, project_id)
        agent._on_status_change = on_status
        agent._on_stream_event = on_stream
        log.info("deliver_patch_agent_callbacks", agent_id=agent_id)

    user_msg = json.dumps(
        {"from": _USER_ENVELOPE_FROM, "content": content}, ensure_ascii=False
    )
    # 用户发图喂 LLM：data URL → 内部格式，经 opts 穿到本轮 user 消息与
    # conversation 用户 turn（chat_messages 已存原图供 UI，此处只喂模型）。
    from hiveweave.services.vision import parse_user_images

    user_images = parse_user_images(images) if images else []
    # I15(P2-5)：三端共用入口显式报 source="chat"（surface 细分 web|ball|feishu
    # 仍记 chat_messages.metadata.source；缺省 ⇒ unknown+告警）。
    chat_opts: dict = {"source": "chat"}
    if user_images:
        chat_opts["images"] = user_images
    result = await agent.chat(user_msg, chat_opts)

    if result.get("error") == "busy":
        return {
            "ok": False,
            "outcome": "busy",
            "user_message_id": (saved or {}).get("id"),
            "assistant_message_id": None,
            "assistant_content": None,
            "error": "Agent is busy",
        }
    if result.get("error") == "paused":
        return {
            "ok": False,
            "outcome": "paused",
            "user_message_id": (saved or {}).get("id"),
            "assistant_message_id": None,
            "assistant_content": None,
            "error": "System is paused",
        }
    if result.get("error") == "project_not_started":
        from hiveweave.services.off_duty import send_off_duty_auto_reply

        # 用户消息已落库；只补下班自动回复
        try:
            asst = await send_off_duty_auto_reply(agent_id)
        except Exception as e:
            log.warning(
                "deliver_user_message_off_duty_reply_failed",
                agent_id=agent_id,
                error=str(e),
            )
            return {
                "ok": False,
                "outcome": "off_duty",
                "user_message_id": (saved or {}).get("id"),
                "assistant_message_id": None,
                "assistant_content": None,
                "error": "off-duty auto-reply failed",
            }
        return {
            "ok": True,
            "outcome": "off_duty",
            "user_message_id": (saved or {}).get("id"),
            "assistant_message_id": asst["id"],
            "assistant_content": asst["content"],
            "error": None,
        }

    ok = bool(result.get("ok") or result.get("queued") or result.get("held"))
    return {
        "ok": ok,
        "outcome": "started",
        "user_message_id": (saved or {}).get("id"),
        "assistant_message_id": None,
        "assistant_content": None,
        "error": None if ok else "Failed to trigger chat",
    }
