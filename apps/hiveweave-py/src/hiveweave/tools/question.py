"""question tool — agent asks the user a question and waits for an answer.

契约 02: 工具执行器 — question 子模块
- 持久化 question 到 per-project DB (questions 表)
- 通过 in-memory asyncio.Future 阻塞等待用户回答（120s 超时）
- 超时返回友好提示（不阻塞 agent 流程）
- 前端通过 API 控制器调用 resolve_question() 提交答案
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

QUESTION_TIMEOUT_S = 120

# In-memory pending questions: question_id -> asyncio.Future
_pending: dict[str, asyncio.Future[str]] = {}


class QuestionTimeout(Exception):
    """Raised when a question times out waiting for an answer."""


async def execute_question(
    agent_id: str,
    question: str,
    options: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Ask the user a question and block until answered or 120s timeout.

    Returns {success, output, error} where output is the user's answer.
    """
    if not question or not question.strip():
        return {"success": False, "output": "",
                "error": "Error: question is required"}

    project_id = await meta_db.get_agent_project_id(agent_id) or ""
    question_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)

    # Persist question to per-project DB
    try:
        await project_db.execute(
            agent_id,
            """INSERT INTO questions
               (id, agent_id, project_id, question, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            [question_id, agent_id, project_id, question, now_ms],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("question.persist_failed", error=str(exc))

    # BUG-036: Also save as a chat_message so the question appears in ChatPanel.
    # Previously only saved to questions table — user couldn't see it in chat.
    from hiveweave.services.chat_message import ChatMessageService
    chat_msg = ChatMessageService()
    options_text = ""
    if options:
        opts: list[str] = []
        for o in options[:6]:
            if isinstance(o, dict):
                opts.append(f"- {o.get('label', o.get('text', str(o)))}")
            else:
                opts.append(f"- {o}")
        options_text = "\n\n选项:\n" + "\n".join(opts)
    try:
        await chat_msg.save_message({
            "agent_id": agent_id,
            "role": "assistant",
            "content": f"[QUESTION] {question}{options_text}",
            "is_streaming": False,
            "is_background": False,
            "is_read": True,
        })
    except Exception as exc:
        log.warning("question.chat_message_failed", error=str(exc))

    # Create a Future for the answer
    loop = asyncio.get_event_loop()
    future: asyncio.Future[str] = loop.create_future()
    _pending[question_id] = future

    log.info("question.asked", question_id=question_id,
             agent_id=agent_id, preview=question[:120])

    try:
        answer = await asyncio.wait_for(future, timeout=QUESTION_TIMEOUT_S)
        answered_at = int(time.time() * 1000)
        try:
            await project_db.execute(
                agent_id,
                """UPDATE questions
                   SET status = 'answered', answer = ?, answered_at = ?
                   WHERE id = ?""",
                [answer, answered_at, question_id],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("question.update_failed", error=str(exc))

        # BUG-036: Save answer as user message in chat so the conversation is visible
        try:
            await chat_msg.save_message({
                "agent_id": agent_id,
                "role": "user",
                "content": answer,
                "is_streaming": False,
                "is_background": False,
                "is_read": True,
            })
        except Exception as exc:
            log.warning("question.answer_chat_failed", error=str(exc))

        return {"success": True, "output": f"User answered: {answer}",
                "error": None}

    except asyncio.TimeoutError:
        # Mark as timed out in DB
        try:
            await project_db.execute(
                agent_id,
                "UPDATE questions SET status = 'timeout' WHERE id = ?",
                [question_id],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("question.timeout_update_failed", error=str(exc))

        return {
            "success": True,  # not an error — agent continues
            "output": (f"Question timed out ({QUESTION_TIMEOUT_S}s). "
                       "Proceeding without user input."),
            "error": None,
        }
    finally:
        _pending.pop(question_id, None)


def resolve_question(question_id: str, answer: str) -> bool:
    """Resolve a pending question with the user's answer.

    Called by the API controller when the user submits an answer.
    Returns True if the question was found and resolved.
    """
    future = _pending.get(question_id)
    if future is None or future.done():
        return False
    future.set_result(answer)
    log.info("question.resolved", question_id=question_id,
             answer_preview=answer[:120])
    return True


def get_pending_questions() -> list[dict[str, Any]]:
    """Return all currently-pending question IDs (for diagnostics/polling)."""
    return [{"question_id": qid} for qid in list(_pending.keys())]


def drain_expired_questions() -> list[str]:
    """Cancel and remove expired questions (cleanup, best-effort)."""
    expired: list[str] = []
    for qid, future in list(_pending.items()):
        if future.done():
            expired.append(qid)
    for qid in expired:
        _pending.pop(qid, None)
    return expired
