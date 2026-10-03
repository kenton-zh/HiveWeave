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


# ── P1-12（批22，2026-10-03）：user_pings 人类侧 ping 台账 ─────────────
# 病灶：user_pings 全仓零 INSERT（读方 GET /api/user-pings 消费一张永远为空
# 的表）——无人值守 question 超时自动裁决后，用户在 ping 台账上看不到任何
# 痕迹。注意：批8 的 agent_waits(kind='user') 门铃是**活的**（等待机制），
# 本台账是**人类侧可见性**，两者不重复。
#: ping 台账状态值（user_pings.status 列）。
PING_STATUS_PENDING = "pending"
PING_STATUS_ANSWERED = "answered"
PING_STATUS_TIMED_OUT = "timed_out"
PING_STATUS_READJUDICATED = "readjudicated"


async def _update_user_ping_status(
    project_id: str | None, question_id: str, status: str
) -> None:
    """question 收口 ⇒ 同步 user_pings 台账状态（best-effort，绝不影响收口）。"""
    if not project_id or not question_id:
        return
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
        cur = await conn.execute(
            "UPDATE user_pings SET status = ? WHERE question_id = ?",
            [status, question_id],
        )
        await conn.commit()
        await cur.close()
    except Exception as exc:  # noqa: BLE001 — 台账同步是旁支
        log.warning(
            "question.user_ping_update_failed",
            question_id=question_id[:8],
            status=status,
            error=str(exc),
        )


async def _insert_user_ping(
    project_id: str,
    agent_id: str,
    question_id: str,
    *,
    question: str,
    options: list | None,
    now_ms: int,
) -> None:
    """question 创建（无人值守场景）⇒ INSERT user_pings 一行（P1-12 写方）。

    message = 问题文本 + 选项摘要（label 优先）；status=pending。best-effort。
    """
    if not project_id:
        return
    labels: list[str] = []
    for o in (options or [])[:6]:
        if isinstance(o, dict):
            labels.append(str(o.get("label") or o.get("text") or o))
        else:
            labels.append(str(o))
    opts_summary = f" 选项: {' / '.join(labels)}" if labels else ""
    try:
        await project_db.execute(
            agent_id,
            """INSERT INTO user_pings
               (id, project_id, from_agent_id, message, is_read, created_at,
                question_id, status)
               VALUES (?, ?, ?, ?, 0, ?, ?, 'pending')""",
            [
                str(uuid.uuid4()),
                project_id,
                agent_id,
                f"{(question or '').strip()[:400]}{opts_summary[:300]}",
                now_ms,
                question_id,
            ],
        )
    except Exception as exc:  # noqa: BLE001 — 台账写入是旁支
        log.warning(
            "question.user_ping_insert_failed",
            question_id=question_id[:8],
            error=str(exc),
        )


async def deliver_question_readjudication(
    project_id: str,
    agent_id: str,
    question_id: str,
    *,
    question_text: str,
    previous_choice: str,
    new_answer: str,
    changed: bool,
) -> None:
    """迟到人类裁决的投递 + 台账（P1-12 b，由 API answer 端点调用）。

    user_pings 置 ``readjudicated``；答案与原自动裁决**不同**时再投递
    ``[QUESTION RE-ADJUDICATED]`` 平台通知（inbox wake=1 + trigger 唤醒 +
    chat 面板留痕）——同答案重复提交不重投（幂等，由调用方以 ``changed``
    判定传入）。各出口独立 best-effort。
    """
    await _update_user_ping_status(project_id, question_id, PING_STATUS_READJUDICATED)
    preview = (question_text or "").strip()[:160]
    new_text = (new_answer or "").strip()[:200]
    prev_text = (previous_choice or "").strip()[:200]
    if changed:
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().send_message(
                from_agent_id="system",
                to_agent_id=agent_id,
                message=(
                    f"[QUESTION RE-ADJUDICATED] 你此前的提问「{preview}」原被"
                    f"无人值守自动裁决为「{prev_text}」；用户现已迟到改判为："
                    f"{new_text}。原自动裁决被推翻 —— 请按新答案继续；若此前"
                    "按默认项执行的步骤与新答案冲突，先说明影响再返工。"
                ),
                message_type="question_readjudicated",
                priority="urgent",
                wake=True,
            )
        except Exception as exc:  # noqa: BLE001 — 单出口失败不拖累其余
            log.warning(
                "question.readjudicate_inbox_failed",
                question_id=question_id[:8],
                error=str(exc),
            )
        try:
            from hiveweave.agents.trigger import trigger_subordinate

            await trigger_subordinate(agent_id)
        except Exception as exc:  # noqa: BLE001 — 唤醒失败退化为看门狗
            log.warning(
                "question.readjudicate_trigger_failed",
                question_id=question_id[:8],
                error=str(exc),
            )
    # chat 面板留痕（含幂等同答案路径 —— 用户改判动作本身值得可见）。
    try:
        from hiveweave.services.chat_message import ChatMessageService

        await ChatMessageService().save_message({
            "agent_id": agent_id,
            "role": "assistant",
            "content": (
                f"[QUESTION RE-ADJUDICATED] 提问「{preview}」用户迟到改判："
                f"{new_text}（原自动裁决：{prev_text}，已推翻，按新答案继续）。"
                if changed
                else (
                    f"[QUESTION RE-ADJUDICATED] 提问「{preview}」用户确认了"
                    f"原自动裁决：{prev_text}（迟到裁决落账，无需改道）。"
                )
            ),
            "is_streaming": False,
            "is_background": False,
            "is_read": True,
            "metadata": {
                "source": "platform",
                "kind": "question_readjudicated",
                "question_id": question_id,
                "changed": changed,
            },
        })
    except Exception as exc:  # noqa: BLE001 — 留痕失败不影响改判
        log.warning(
            "question.readjudicate_chat_failed",
            question_id=question_id[:8],
            error=str(exc),
        )
    log.info(
        "question.readjudicated",
        project_id=project_id,
        agent_id=agent_id,
        question_id=question_id[:8],
        changed=changed,
    )


async def wake_question_waiters(
    project_id: str,
    question_id: str,
    *,
    answer: str | None = None,
    resolved_by: str | None = None,
    chosen: str | None = None,
    trigger: bool = True,
    skip_trigger_agent_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """T39：question 收口（用户答复 / 自动裁决）⇒ 唤醒
    ``wait_on(kind='user', ref=<question_id>)`` 的等待者。

    与 :func:`_clear_sibling_wait`（只清提问者自己的同生 wait）互补：
    wait_on 的挂起者可以是**另一个 agent** 或**提问者的后续回合**（典型
    场景：提问 180s 轮内超时 → wait_on 挂起 → 用户晚到答复 —— 此时
    future 已不在，提问者全靠本函数唤醒）。幂等：rowcount 守卫
    （cleared_at IS NULL）—— 已清过的行不重复通知，二次调用 no-op。

    ``skip_trigger_agent_ids``：不触发 trigger 的 agent（如正被 future
    解阻塞的提问者；行照清、信照发）。

    Returns 被唤醒（实际清到等待行）的 agent id 列表。
    """
    qid = (question_id or "").strip()
    if not project_id or not qid:
        return []
    from hiveweave.services.wait_contract import wait_contract_service

    try:
        active = await wait_contract_service.list_all_active(project_id)
    except Exception as exc:  # noqa: BLE001 — 扫描失败退化为 TTL 兜底
        log.warning(
            "question.waiter_scan_failed",
            question_id=qid[:8],
            error=str(exc),
        )
        return []
    waiters: list[str] = []
    for w in active:
        if str(w.get("kind") or "").lower() != "user":
            continue
        if str(w.get("ref") or "").strip() != qid:
            continue
        aid = str(w.get("agentId") or w.get("agent_id") or "")
        if aid and aid not in waiters:
            waiters.append(aid)
    if not waiters:
        return []

    cleared: list[str] = []
    for aid in waiters:
        try:
            n = await wait_contract_service.clear_waits_matching_ref(
                project_id, aid, qid
            )
        except Exception as exc:  # noqa: BLE001 — 单个失败不拖累其余
            log.warning(
                "question.waiter_clear_failed", agent_id=aid, error=str(exc)
            )
            continue
        if n:
            cleared.append(aid)
    if not cleared:
        return []

    qshort = qid[:8]
    if resolved_by:
        reason_label = (
            "无人值守超时"
            if resolved_by == RESOLVED_BY_TIMEOUT
            else "项目下班/停止"
        )
        body = (
            f"[QUESTION AUTO-RESOLVED] 你 wait_on 等待的问题"
            f"（question_id={qshort}…）已{reason_label}按推荐项自动裁决："
            f"{str(chosen or '')[:200]}。不会再有用户答复。Resume work."
        )
    else:
        body = (
            f"[QUESTION ANSWERED] 你 wait_on 等待的用户答复已到"
            f"（question_id={qshort}…）：{str(answer or '')[:400]}。"
            "Resume work with the user's answer."
        )
    try:
        from hiveweave.services.inbox import InboxService

        inbox = InboxService()
        for aid in cleared:
            try:
                await inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=aid,
                    message=body,
                    message_type="system",
                    priority="urgent",
                    wake=True,
                )
            except Exception as exc:  # noqa: BLE001 — 单发失败不拖累其余
                log.warning(
                    "question.waiter_notify_failed",
                    agent_id=aid,
                    error=str(exc),
                )
    except Exception as exc:  # noqa: BLE001 — inbox 不可用退化为仅清等待
        log.warning("question.waiter_inbox_unavailable", error=str(exc))
    if trigger:
        from hiveweave.agents.trigger import trigger_subordinate

        for aid in cleared:
            if aid in skip_trigger_agent_ids:
                continue
            try:
                await trigger_subordinate(aid)
            except Exception as exc:  # noqa: BLE001 — 触发失败退化为看门狗
                log.warning(
                    "question.waiter_trigger_failed",
                    agent_id=aid,
                    error=str(exc),
                )
    log.info(
        "question_waiters_woken",
        project_id=project_id,
        question_id=qshort,
        woken=len(cleared),
        resolved_by=resolved_by or "user_answer",
    )
    return cleared


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
    # P1-12：ping 台账同步 —— 自动裁决（超时/下班）对人类侧可见。
    await _update_user_ping_status(project_id, question_id, PING_STATUS_TIMED_OUT)
    await _clear_sibling_wait(project_id, agent_id, question_id)
    # T39：wait_on(kind=user) 等待者随裁决一并解锁 —— 不会再有用户答复，
    # 等待只能空等到 TTL（与任务终态短路同判据）。提问者的唤醒由本函数
    # 既有出口（send_inbox / trigger_agent）负责，这里跳过。
    try:
        await wake_question_waiters(
            project_id,
            question_id,
            resolved_by=reason,
            chosen=chosen,
            trigger=trigger_agent,
            skip_trigger_agent_ids=frozenset({agent_id}),
        )
    except Exception as exc:  # noqa: BLE001 — 唤醒失败退化为 TTL 兜底
        log.warning("question.adjudicate_waiter_wake_failed", error=str(exc))

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


def _schedule_orphan_cleanup(question_id: str, *, answer: str | None = None) -> None:
    _cancel_adjudicator(question_id)
    if question_id not in _question_projects:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_orphan_cleanup(question_id, answer=answer))
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)


async def _orphan_cleanup(question_id: str, *, answer: str | None = None) -> None:
    loc = _question_projects.pop(question_id, None)
    if loc is None:
        return
    project_id, agent_id = loc
    # T39：先唤醒 wait_on(kind=user, ref=本question) 的等待者（**含提问者
    # 本人** —— 轮内超时后 wait_on 挂起、晚到答案的唯一唤醒口），再兜底清
    # 同生 wait。顺序不可反：sibling clear 会连提问者的 wait_on 行一起清，
    # 先清会让唤醒扫描扑空。
    try:
        await wake_question_waiters(project_id, question_id, answer=answer)
    except Exception as exc:  # noqa: BLE001 — 唤醒失败退化为 TTL 兜底
        log.warning("question.orphan_waiter_wake_failed", error=str(exc))
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

    # P1-12（批22）：user_pings 写方立项 —— 人类侧 ping 台账一行（pending）。
    # 此前该表读方在、写方全仓为零；无人值守场景的可见性由此补齐（轮内钟
    # 到点/被掐都不撤行 —— 台账本就该记录「这道题还没人管」）。
    await _insert_user_ping(
        project_id,
        agent_id,
        question_id,
        question=question,
        options=options,
        now_ms=now_ms,
    )

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
        # P1-12：ping 台账同步收口（answer 端点路径已写，此处幂等兜底）。
        await _update_user_ping_status(
            project_id, question_id, PING_STATUS_ANSWERED
        )
        await _clear_sibling_wait(project_id, agent_id, question_id)
        # T39：wait_on(kind=user, ref=本question) 的其他等待者唤醒。
        # 提问者此刻正被 future 解阻塞（在跑），跳过其 trigger ——
        # 行照清（同生语义），信照发可作留痕。
        try:
            await wake_question_waiters(
                project_id,
                question_id,
                answer=answer,
                skip_trigger_agent_ids=frozenset({agent_id}),
            )
        except Exception as exc:  # noqa: BLE001 — 唤醒失败退化为 TTL 兜底
            log.warning("question.waiter_wake_failed", error=str(exc))

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
        # T39：回执带 question_id + wait_on(user) 处方 —— 这是超时后
        # 继续合法等答复的唯一入口（带 id 才挂得上）。
        return {
            "success": False,
            "output": "",
            "question_id": question_id,
            "error": (
                f"Error: no user answer within {QUESTION_TIMEOUT_S}s "
                "(in-turn wait). The question STAYS PENDING — the user may "
                "still answer it later. Do NOT treat it as answered. To keep "
                "legally waiting for this answer, call "
                'wait_on(refs=[{"kind": "user", "ref": "' + question_id + '"}])'
                " then stop outputting; you will be woken when the answer "
                "lands. If you cannot wait, proceed without the user's input "
                "and say so explicitly; if it stays unanswered, it will be "
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
            "question_id": question_id,
            "error": (
                "Error: question wait was cancelled (tool/turn teardown). "
                "The question STAYS PENDING — no user input was received. "
                "Do NOT treat it as answered. wait_on(kind=user, ref="
                f"\"{question_id}\") can keep the wait legal if you stop here."
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
        # 这里负责门铃退场：撤销未决裁决 + 解除同生 wait（否则 600s 空鸣）
        # + T39 唤醒 wait_on(kind=user) 等待者。
        _schedule_orphan_cleanup(question_id, answer=answer)
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


# B6 契约单源化（缺口 19）：工具最终描述的唯一 owner 是本常量 —— @tool
# 注册处与 executor.py 的 TOOL_PARAM_SCHEMAS 手写条目都引用它，不再各写一份。
QUESTION_TOOL_DESCRIPTION = (
    "Ask the human a question and block up to ~180s for their "
    "answer; optional options (each a string or {label, text}). "
    "On timeout/cancel the call FAILS and the question stays "
    "PENDING (the receipt carries the question_id — to keep "
    "legally waiting for the answer, call wait_on(kind=user, "
    "ref=<question_id>) then stop; if unattended it "
    "is auto-adjudicated to your first option after ~600s, or when "
    "the project stops) — NEVER treat a failed question as "
    "answered. If you already have one pending question "
    "unanswered, a new call is skipped (30-min dedup) — wait for "
    "the prior answer. This is the ONLY channel that delivers a "
    "question to the user; assistant text is not delivered."
)


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
        # 别名是手写层（TOOL_PARAM_SCHEMAS）曾接受的并集——registered 管线的
        # 别名解析只认 pydantic 这张表，回填才不丢既有可达面（B6 红线）。
        json_schema_extra={"aliases": ["message", "content", "query", "prompt", "text"]},
    )
    options: Optional[list[Any]] = Field(
        default=None,
        description=(
            "Optional list of choices (up to 6 shown). Each item can be a "
            "string or an object with 'label'/'text' keys. The FIRST item is "
            "the recommended default and is auto-selected if the question "
            "times out unattended or the project stops — put the "
            "recommendation first and state its rationale."
        ),
        json_schema_extra={"aliases": ["choices"]},
    )


@tool(
    "question",
    QUESTION_TOOL_DESCRIPTION,
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
    extra = (
        {"question_id": result["question_id"]}
        if result.get("question_id")
        else {}
    )
    return ToolResult.err(result.get("error", "Unknown error"), **extra)
