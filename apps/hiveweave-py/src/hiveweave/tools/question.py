"""question tool — agent asks the user a question and waits for an answer.

契约 02: 工具执行器 — question 子模块
- 持久化 question 到 per-project DB (questions 表)，**带 expires_at**（I9）
- 落库同时落一条 ``agent_waits(kind='user')``（I9 ①：门铃两套并一套）
- 通过 in-memory asyncio.Future 阻塞等待用户回答（QUESTION_TIMEOUT_S，轮内钟）
- **双触发（I9）**：
    触发 A（时间）：pending ≥ QUESTION_UNATTENDED_TIMEOUT_S 无人应答 ⇒
      按 options[0]（推荐项）裁决继续，question 标 timed_out + 广播可见事件；
    触发 B（生命周期）：项目下班/停止 ⇒ pending 立即按默认项裁决并写进
      当轮交接摘要（接线 services/project_lifecycle.py::stop_project_cleanly）。
  两条触发都落事实位 timed_out_at / resolved_by='timeout'|'lifecycle_stop'。
- **诚实语义（I9）**：轮内超时/被掐**不再 fake success** —— question 保持
  pending（用户晚到答案仍可写），工具如实返回失败并说明门铃在途。
- 前端通过 API 控制器调用 resolve_question() 提交答案
- streamer 对 question 使用更长工具超时（>_QUESTION_TOOL_TIMEOUT_S），
  避免 120s 通用工具超时抢先取消本 Future
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.config import settings
from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

# 轮内阻塞钟：agent 的 turn 最多为这个问题停 180s。⚠ streamer 的
# _QUESTION_TOOL_TIMEOUT_S=200（llm/streamer/constants.py）必须长于它 ——
# 本值**不是**无人值守阈值，勿混用（I9 落点订正：报告曾误引 streamer 注释）。
QUESTION_TIMEOUT_S = 180


def _unattended_timeout_s() -> int:
    """触发 A 阈值（秒）：pending 超此时长无人应答 ⇒ 按推荐项裁决。"""
    return int(settings.question_unattended_timeout_s)


def _wait_cap_s() -> int:
    """等待硬上限（秒）：复用平台既有 user-wait 档（3600s）。"""
    return int(settings.question_wait_cap_s)


def validate_question_thresholds(unattended_s: int, cap_s: int) -> None:
    """「默认 + 硬顶」自检（照抄上游 DSH tool-jobs/src/index.ts:202：
    默认值 > 上限直接抛，配测试覆盖）。本模块导入期调用 ⇒ 配置错误
    fail-loud，而不是让门铃静默失准。"""
    if unattended_s <= 0 or cap_s <= 0:
        raise ValueError(
            f"question thresholds must be positive, got "
            f"unattended={unattended_s}s cap={cap_s}s"
        )
    if unattended_s > cap_s:
        raise ValueError(
            f"question_unattended_timeout_s ({unattended_s}s) exceeds "
            f"question_wait_cap_s ({cap_s}s) — unattended threshold must "
            f"not exceed the hard wait cap (DSH tool-jobs :202 self-check)"
        )


validate_question_thresholds(_unattended_timeout_s(), _wait_cap_s())

# In-memory pending questions: question_id -> asyncio.Future
_pending: dict[str, asyncio.Future[str]] = {}

# I9: 未决 question 的落点账（question_id -> (project_id, agent_id)）——
# 供晚到答案/裁决的清扫路径定位 per-project DB。仅存未收口者，量有界。
_question_projects: dict[str, tuple[str, str]] = {}

# I9: 在途无人值守裁决任务（question_id -> Task）——答案先到则撤销。
_adjudicators: dict[str, asyncio.Task] = {}

# 晚到答案的兜底清扫任务（强引用防 GC，done 即弃）
_cleanup_tasks: set[asyncio.Task] = set()

# 去重窗口：同一 agent 在此时间内有 pending question 则不允许再提问
DEDUP_WINDOW_MS = 30 * 60 * 1000  # 30 分钟

RESOLVED_BY_TIMEOUT = "timeout"
RESOLVED_BY_LIFECYCLE_STOP = "lifecycle_stop"
_ADJUDICABLE_STATUSES = ("pending", "cancelled")


class QuestionTimeout(Exception):
    """Raised when a question times out waiting for an answer."""


def _default_option_text(options: Any) -> str | None:
    """options[0]（推荐项）的人类可读文本；无可用项返回 None。"""
    if not options:
        return None
    try:
        first = options[0]
    except (IndexError, TypeError):
        return None
    if isinstance(first, dict):
        for key in ("label", "text", "value", "option"):
            val = first.get(key)
            if val is not None and str(val).strip():
                return str(val)
        return None
    text = str(first).strip()
    return text or None


def _parse_options_json(raw: Any) -> list[Any] | None:
    if not raw:
        return None
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _cancel_adjudicator(question_id: str, *, include_current: bool = True) -> None:
    task = _adjudicators.pop(question_id, None)
    if task is not None and not task.done():
        if not include_current and task is asyncio.current_task():
            # 裁决任务自身走到这里（触发 A 收口路径）：cancel 自己会在下个
            # await 点掐断收口余下步骤（同生 wait 解除等）——跳过。
            return
        task.cancel()


async def _clear_sibling_wait(project_id: str, agent_id: str, question_id: str) -> None:
    """同生共死（I9 ④）：question 收口 ⇒ 同生的 agent_waits 行一并解除。"""
    try:
        from hiveweave.services.wait_contract import wait_contract_service

        await wait_contract_service.clear_waits_matching_ref(
            project_id, agent_id, question_id
        )
    except Exception as exc:  # noqa: BLE001 — 解除失败退化为 TTL 兜底
        log.warning(
            "question_sibling_wait_clear_failed",
            question_id=question_id,
            error=str(exc),
        )


# ── 裁决唯一出口（触发 A / 触发 B 共用）───────────────────────────


async def adjudicate_question(
    question_id: str,
    project_id: str,
    *,
    reason: str,
    default_text: str | None = None,
    send_inbox: bool = False,
    trigger_agent: bool = False,
) -> dict[str, Any] | None:
    """裁决一道仍未收口的 question（I9 触发 A/B 唯一出口）。

    按 options[0]（推荐项，无则平台兜底文案）裁决；落事实位
    ``timed_out_at`` / ``resolved_by``；广播可见事件 + chat 面板留痕
    （**不许静默**）；解除同生 agent_waits。``send_inbox`` 再发一条
    wake=1 inbox（触发 A 现场通知；触发 B/恢复补票的这条会在 activate
    pre-park 时并进复工 briefing=交接摘要）。``trigger_agent`` 额外唤醒
    agent 继续（仅触发 A——project 在跑时）。

    Returns 裁决摘要 dict；question 不存在/已收口/被并发抢先裁决 → None。
    """
    if reason not in (RESOLVED_BY_TIMEOUT, RESOLVED_BY_LIFECYCLE_STOP):
        raise ValueError(f"unknown adjudication reason: {reason!r}")
    if not project_id:
        return None
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "question.adjudicate_no_db", question_id=question_id, error=str(exc)
        )
        return None
    cur = await conn.execute(
        "SELECT id, agent_id, question, options, status, created_at "
        "FROM questions WHERE id = ?",
        [question_id],
    )
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return None
    rowd = dict(row)
    if (rowd.get("status") or "") not in _ADJUDICABLE_STATUSES:
        return None
    agent_id = str(rowd.get("agent_id") or "")

    options = _parse_options_json(rowd.get("options"))
    if default_text is None:
        default_text = _default_option_text(options)
    chosen = default_text or "（无推荐项——视为未获用户输入）"
    now_ms = int(time.time() * 1000)

    # rowcount 守卫：与用户回答 / 另一触发并发时只赢一方（同 disk-guard
    # 后写者竞态纪律：0 行 = 抢先者已收口，不重复裁决）。
    cur = await conn.execute(
        "UPDATE questions SET status = 'timed_out', answer = ?, "
        "timed_out_at = ?, resolved_by = ? "
        "WHERE id = ? AND status IN ('pending', 'cancelled')",
        [chosen, now_ms, reason, question_id],
    )
    updated = cur.rowcount
    await conn.commit()
    await cur.close()
    if not updated:
        return None

    _question_projects.pop(question_id, None)
    _cancel_adjudicator(question_id, include_current=False)
    await _clear_sibling_wait(project_id, agent_id, question_id)

    preview = str(rowd.get("question") or "")[:160]
    reason_label = (
        "无人值守超时" if reason == RESOLVED_BY_TIMEOUT else "项目下班/停止"
    )
    log.info(
        "question.adjudicated",
        question_id=question_id,
        agent_id=agent_id,
        project_id=project_id,
        reason=reason,
        chosen=chosen[:80],
    )

    # 可见出口 1：chat 面板留痕（提问当时就在这里问的，裁决也回这里）
    try:
        from hiveweave.services.chat_message import ChatMessageService

        await ChatMessageService().save_message({
            "agent_id": agent_id,
            "role": "assistant",
            "content": (
                f"[QUESTION AUTO-RESOLVED] 提问「{preview}」{reason_label}，"
                f"已按推荐项裁决继续：{chosen}（resolved_by={reason}）。"
                "用户此后仍可补答，但该轮决定已按上述默认项生效。"
            ),
            "is_streaming": False,
            "is_background": False,
            "is_read": True,
            "metadata": {
                "source": "platform",
                "kind": "question_adjudicated",
                "question_id": question_id,
                "resolved_by": reason,
            },
        })
    except Exception as exc:  # noqa: BLE001 — 留痕失败不影响裁决
        log.warning("question.adjudicate_chat_failed", error=str(exc))

    # 可见出口 2：实时广播（前端 pending 面板/OrgTree 即时刷新）
    try:
        from hiveweave.realtime.event_bus import status_event_bus

        event: dict[str, Any] = {
            "type": "question_adjudicated",
            "agentId": agent_id,
            "projectId": project_id,
            "questionId": question_id,
            "question": preview,
            "resolvedBy": reason,
            "chosenDefault": chosen,
        }
        await status_event_bus.publish("lobby", event)
        await status_event_bus.publish(f"project:{project_id}", event)
    except Exception as exc:  # noqa: BLE001 — 广播失败不影响裁决
        log.warning("question.adjudicate_push_failed", error=str(exc))

    if send_inbox:
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().send_message(
                from_agent_id="system",
                to_agent_id=agent_id,
                message=(
                    f"[QUESTION AUTO-RESOLVED] 提问「{preview}」{reason_label}，"
                    f"已按推荐项裁决：{chosen}（resolved_by={reason}）。"
                    "如需改判，向用户说明后继续。"
                ),
                message_type="question_timeout",
                wake=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("question.adjudicate_inbox_failed", error=str(exc))
    if trigger_agent:
        try:
            from hiveweave.agents.trigger import trigger_subordinate

            await trigger_subordinate(agent_id)
        except Exception as exc:  # noqa: BLE001 — 唤醒失败退化为看门狗兜底
            log.warning(
                "question.adjudicate_trigger_failed",
                question_id=question_id,
                error=str(exc),
            )
    return {
        "questionId": question_id,
        "agentId": agent_id,
        "projectId": project_id,
        "reason": reason,
        "chosen": chosen,
    }


async def adjudicate_project_questions(
    project_id: str, *, reason: str = RESOLVED_BY_LIFECYCLE_STOP
) -> list[dict[str, Any]]:
    """触发 B（I9）：项目下班/停止 ⇒ 该项目所有未收口 question 立即裁决。

    覆盖 ``pending`` 与 ``cancelled``（后者 = 在途 turn 被下班 cancel 的
    ——I9 案发形态：提问 115s 后就下班，轮内钟 180s 都没等到）。收口结果
    经 send_inbox 落 wake=1 inbox，activate 的 pre-park 会将其并进复工
    briefing（=「写进当轮交接摘要」）。由
    services/project_lifecycle.py::stop_project_cleanly 在 agent 全部
    cancel 之后调用（此后不再有会回答 question 的在跑写方）。
    """
    summary = await _adjudicate_where(
        project_id,
        where=(
            "status IN ('pending', 'cancelled')"
        ),
        reason=reason,
        send_inbox=True,
        trigger_agent=False,
    )
    if summary:
        log.info(
            "question.lifecycle_adjudicated",
            project_id=project_id,
            count=len(summary),
        )
    return summary


async def adjudicate_expired_questions(project_id: str) -> list[dict[str, Any]]:
    """触发 A 补票（I9）：expires_at 已过仍 pending 的（停机期间进程重启
    错过内存裁决任务）→ 按 resolved_by='timeout' 补裁决。由 activate 的
    deliver_resume_briefings 在 pre-park **之前**调用，裁决 inbox 并进
    复工 briefing。"""
    cutoff = int(time.time() * 1000)
    return await _adjudicate_where(
        project_id,
        where=(
            "status = 'pending' AND expires_at IS NOT NULL AND expires_at <= ?"
        ),
        params=[cutoff],
        reason=RESOLVED_BY_TIMEOUT,
        send_inbox=True,
        trigger_agent=False,
    )


async def _adjudicate_where(
    project_id: str,
    *,
    where: str,
    reason: str,
    send_inbox: bool,
    trigger_agent: bool,
    params: list[Any] | None = None,
) -> list[dict[str, Any]]:
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("question.adjudicate_scan_no_db", error=str(exc))
        return []
    sql = (
        "SELECT id FROM questions WHERE project_id = ? AND " + where
    )
    cur = await conn.execute(sql, [project_id, *(params or [])])
    rows = await cur.fetchall()
    await cur.close()
    summary: list[dict[str, Any]] = []
    for r in rows:
        result = await adjudicate_question(
            str(r["id"]),
            project_id,
            reason=reason,
            send_inbox=send_inbox,
            trigger_agent=trigger_agent,
        )
        if result is not None:
            summary.append(result)
    return summary


# ── 触发 A：无人值守裁决任务（进程内钟）───────────────────────────


def _spawn_unattended_adjudicator(
    question_id: str, project_id: str, delay_s: float | None = None
) -> None:
    if delay_s is None:
        delay_s = float(_unattended_timeout_s())
    task = asyncio.create_task(
        _unattended_adjudicator(question_id, project_id, delay_s)
    )
    _adjudicators[question_id] = task


async def _unattended_adjudicator(
    question_id: str, project_id: str, delay_s: float
) -> None:
    try:
        await asyncio.sleep(delay_s)
        await adjudicate_question(
            question_id,
            project_id,
            reason=RESOLVED_BY_TIMEOUT,
            send_inbox=True,
            trigger_agent=True,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — 裁决失败留痕，不炸后台任务
        log.warning(
            "question.unattended_adjudicator_failed",
            question_id=question_id,
            error=str(exc),
        )
    finally:
        _adjudicators.pop(question_id, None)


# ── 晚到答案的兜底清扫（future 已不在：turn 已在轮内钟处返回）───────


def _schedule_orphan_cleanup(question_id: str) -> None:
    _cancel_adjudicator(question_id)
    if question_id not in _question_projects:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_orphan_cleanup(question_id))
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)


async def _orphan_cleanup(question_id: str) -> None:
    loc = _question_projects.pop(question_id, None)
    if loc is None:
        return
    project_id, agent_id = loc
    await _clear_sibling_wait(project_id, agent_id, question_id)


async def execute_question(
    agent_id: str,
    question: str,
    options: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Ask the user a question and block until answered or in-turn timeout.

    Returns {success, output, error} where output is the user's answer.
    Waits up to ``QUESTION_TIMEOUT_S`` (180s, 轮内钟); streamer must use a
    longer per-tool timeout so it does not cancel this wait at 120s.

    I9：落库带 expires_at（= 无人值守阈值）并同生一条
    agent_waits(kind='user')；轮内超时/被掐**如实返回失败**（question
    保持 pending，门铃在途：600s 无人应答按推荐项裁决，用户晚到仍可答）。
    """
    if not question or not question.strip():
        return {"success": False, "output": "",
                "error": "Error: question is required"}

    project_id = await meta_db.get_agent_project_id(agent_id) or ""
    now_ms = int(time.time() * 1000)

    # 去重检查：同一 agent 在 30 分钟内有 pending question 则不允许再提问
    try:
        rows = await project_db.query(
            agent_id,
            "SELECT id, question FROM questions "
            "WHERE agent_id = ? AND status = 'pending' AND created_at > ? "
            "ORDER BY created_at DESC LIMIT 1",
            [agent_id, now_ms - DEDUP_WINDOW_MS],
        )
        if rows:
            existing_q = rows[0]
            return {
                "success": True,
                "output": (f"已有待回答的问题（已跳过重复提问）。"
                           f"请等待用户回答上一个问题后再提问。"
                           f"上一个问题: {existing_q['question'][:100]}"),
                "error": None,
            }
    except Exception as exc:
        log.warning("question.dedup_check_failed", error=str(exc))

    question_id = str(uuid.uuid4())

    # Persist question to per-project DB —— I9：expires_at = 无人值守阈值，
    # 是触发 A / 恢复补票的唯一判据（此前该表对「等到什么时候」零概念）。
    unattended_s = _unattended_timeout_s()
    cap_s = _wait_cap_s()
    expires_at_ms = now_ms + min(unattended_s, cap_s) * 1000
    options_json = json.dumps(options, ensure_ascii=False) if options else None
    try:
        await project_db.execute(
            agent_id,
            """INSERT INTO questions
               (id, agent_id, project_id, question, options, status,
                created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            [question_id, agent_id, project_id, question, options_json,
             now_ms, expires_at_ms],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("question.persist_failed", error=str(exc))

    # I9 ①：门铃两套并一套 —— 落库同时落一条 agent_waits(kind='user')。
    # 此前这条路存在（TTL=3600s）却只被用过 1 次；question 的等待从此
    # 是「有界的等待」（ref=question_id，解除与 question 同生共死）。
    try:
        from hiveweave.services.wait_contract import record_question_user_wait

        await record_question_user_wait(
            project_id,
            agent_id,
            ref=question_id,
            expires_at_ms=expires_at_ms,
            note=f"question: {question[:120]}",
            now_ms=now_ms,
        )
    except Exception as exc:  # noqa: BLE001 — 门铃登记缺席退化为旧行为
        log.warning("question.wait_record_failed", error=str(exc))

    # 实时推送 question_asked 事件 — 前端监听后立即弹窗，无需等 5s 轮询
    try:
        from hiveweave.realtime.event_bus import status_event_bus
        await status_event_bus.publish_question_asked(
            agent_id=agent_id,
            project_id=project_id,
            question_id=question_id,
            question=question,
            options=options,
        )
    except Exception as exc:
        log.warning("question.push_failed", error=str(exc))

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
        # fixplan #8：这是**第三条**用户可见出口（前两条 = message_user /
        # send_message(to=用户)）。不挂徽章 ⇒ 把完工结论塞进 question 就能
        # 跳过徽章，前两条白堵。徽章走**同一个** helper ⇒ 三条出口状态必然一致。
        from hiveweave.tools.misc_tools import delivery_badge_metadata

        _badge = await delivery_badge_metadata(agent_id) or {}
        await chat_msg.save_message({
            "agent_id": agent_id,
            "role": "assistant",
            "content": f"[QUESTION] {question}{options_text}",
            "is_streaming": False,
            "is_background": False,
            "is_read": True,
            "metadata": {"source": "agent_to_user", "kind": "question", **_badge},
        })
    except Exception as exc:
        log.warning("question.chat_message_failed", error=str(exc))

    # Create a Future for the answer
    loop = asyncio.get_event_loop()
    future: asyncio.Future[str] = loop.create_future()
    _pending[question_id] = future
    if project_id:
        _question_projects[question_id] = (project_id, agent_id)
        # I9 触发 A：无人值守裁决钟（进程内）。答案先到 ⇒ resolve_question
        # 撤销；进程重启丢钟 ⇒ 恢复时 adjudicate_expired_questions 按
        # expires_at 补票。
        _spawn_unattended_adjudicator(question_id, project_id)

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

        # I9 ④ 同生共死：question 已答 ⇒ 同生 agent_waits 一并解除 +
        # 撤销未决裁决（resolve_question 侧已撤，这里幂等兜底）。
        _question_projects.pop(question_id, None)
        _cancel_adjudicator(question_id)
        await _clear_sibling_wait(project_id, agent_id, question_id)

        # BUG-036: Save answer as user message in chat so the conversation is visible
        try:
            await chat_msg.save_message({
                "agent_id": agent_id,
                "role": "user",
                "content": answer,
                "is_streaming": False,
                "is_background": False,
                "is_read": True,
                "metadata": {"source": "user", "kind": "question_answer"},
            })
        except Exception as exc:
            log.warning("question.answer_chat_failed", error=str(exc))

        return {"success": True, "output": f"User answered: {answer}",
                "error": None}

    except asyncio.TimeoutError:
        # I9：轮内钟到点**如实失败**——不再 fake success、不再把 question
        # 标成终态。question 保持 pending：用户晚到答案仍可写；触发 A
        # （600s 无人应答按推荐项裁决）/触发 B（下班收尾）负责收口。
        return {
            "success": False,
            "output": "",
            "error": (
                f"Error: no user answer within {QUESTION_TIMEOUT_S}s "
                "(in-turn wait). The question STAYS PENDING — the user may "
                "still answer it later. Do NOT treat it as answered. If you "
                "cannot wait, proceed without the user's input and say so "
                "explicitly; if it stays unanswered, it will be "
                f"auto-adjudicated to the recommended option after "
                f"{unattended_s}s."
            ),
        }
    except asyncio.CancelledError:
        # streamer 的 TOOL_EXECUTION_TIMEOUT_S / 下班 cancel 可能在轮内钟
        # 之前取消本 task。I9：不再写 'cancelled' 终态也不再 fake success
        # —— question 保持 pending，由触发 A/B/晚到答案收口。
        log.info("question.cancelled", question_id=question_id, agent_id=agent_id)
        return {
            "success": False,
            "output": "",
            "error": (
                "Error: question wait was cancelled (tool/turn teardown). "
                "The question STAYS PENDING — no user input was received. "
                "Do NOT treat it as answered."
            ),
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
        # I9：晚到答案（轮内钟已到、future 已收）——DB 侧由 API 直接写，
        # 这里负责门铃退场：撤销未决裁决 + 解除同生 wait（否则 600s 空鸣）。
        _schedule_orphan_cleanup(question_id)
        return False
    future.set_result(answer)
    # I9：答案已到，撤销无人值守裁决钟（answered 分支再做同生 wait 解除）。
    _cancel_adjudicator(question_id)
    log.info("question.resolved", question_id=question_id,
             answer_preview=answer[:120])
    return True


def drain_expired_questions() -> list[str]:
    """Cancel and remove expired questions (cleanup, best-effort)."""
    expired: list[str] = []
    for qid, future in list(_pending.items()):
        if future.done():
            expired.append(qid)
    for qid in expired:
        _pending.pop(qid, None)
    return expired


# ── Pydantic models + @tool registration (Phase 2 migration) ──────

from typing import Optional

from pydantic import BaseModel, Field, ConfigDict

from .base import tool
from .result import ToolResult


class QuestionParams(BaseModel):
    """Parameters for question tool."""
    model_config = ConfigDict(populate_by_name=True)

    question: str = Field(
        description=(
            "The question to ask the user. Blocking: this call waits up to "
            "~180s for an answer. If the same agent already has a pending "
            "question within the last 30 minutes, this ask is skipped and the "
            "call returns immediately. On in-turn timeout the call returns "
            "FAILURE and the question stays pending (the user may still "
            "answer; if unattended it is auto-adjudicated to your first "
            "recommended option after ~600s, or when the project stops). "
            "Never treat a failed question as answered."
        ),
        json_schema_extra={"aliases": ["prompt", "text"]},
    )
    options: Optional[list[Any]] = Field(
        default=None,
        description=(
            "Optional list of choices. Each item can be a string or an "
            "object with 'label'/'text' keys. The FIRST item is the "
            "recommended default and is auto-selected if the question times "
            "out unattended or the project stops — put the recommendation "
            "first and state its rationale."
        ),
    )


@tool(
    "question",
    "Ask the user a question and wait for their answer. Use when you need clarification or a decision from the user.",
    requires_workspace=False,
    security_level="standard",
)
async def question_tool(params: QuestionParams, agent_id: str, workspace: str) -> ToolResult:
    """Ask the user a question and block until answered."""
    result = await execute_question(
        agent_id=agent_id,
        question=params.question,
        options=params.options,
    )
    if result.get("success"):
        return ToolResult.ok(result["output"])
    return ToolResult.err(result.get("error", "Unknown error"))
