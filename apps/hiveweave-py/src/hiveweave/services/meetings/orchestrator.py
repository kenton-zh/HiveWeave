"""会议编排器 — 集合等待、fan-out、超时、回岗（docs/spec/team-meeting.md）。

铁律：
- ``start_meeting`` 先登记 hold、再 ``asyncio.create_task`` 编排器，工具
  协程**立刻返回**（在工具协程里等全员 IDLE = 自死锁）。
- 编排器是 MeetingTurnRunner 的唯一调用方；会务不走 ``enqueue_wake``。
- 集合**永不** ``cancel()`` 在飞工作；safety/下班/用户 Stop 结束工作回合
  后落回 hold，不算会议弃权。
- 主持 180s 无 continue/conclude → 重唤一次 → 仍无 → abort（不代写结论）。
- 崩溃靠 ``recover_meetings`` 泵（lifespan / activate / game_time tick）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from hiveweave.realtime.event_bus import status_event_bus
from hiveweave.services.meetings import hold, prompts
from hiveweave.services.meetings.runner import (
    MEETING_SPEECH_TIMEOUT_S,
    RunnerFn,
    run_meeting_turn,
)
from hiveweave.services.meetings.service import (
    ABSTAIN_DISMISSED,
    ABSTAIN_ERROR,
    ABSTAIN_RECOVERED,
    ABSTAIN_UNAVAILABLE,
    ACTIVE_STATUSES,
    MAX_ROUNDS,
    MeetingConflict,
    MeetingError,
    meeting_service as svc,
)

log = structlog.get_logger(__name__)

_ASSEMBLY_POLL_S = 0.5
#: 集合死等上限（规格未给数字；超时 abort 释放锁，决策点见报告）
_ASSEMBLY_TIMEOUT_S = 900.0
_MAX_PARALLEL_SPEECHES = 8
_CHAIR_REWAKE_ATTEMPTS = 1

# meeting_id -> orchestrate task（防泵重复 re-arm）
_RUNNING: dict[str, asyncio.Task] = {}

# 每会议事件序号（WS meeting_updated at-least-once + seq 幂等）
_EVENT_SEQ: dict[str, int] = {}

# 测试注入的 runner_fn（None = 真实 MeetingTurnRunner）
_runner_fn: RunnerFn | None = None


def set_runner_fn(fn: RunnerFn | None) -> None:
    global _runner_fn
    _runner_fn = fn


def running_task(meeting_id: str) -> asyncio.Task | None:
    return _RUNNING.get(meeting_id)


# ── 事件 ─────────────────────────────────────────────────────


async def emit_meeting_event(
    project_id: str, meeting: dict[str, Any], **extra: Any
) -> None:
    """meeting_updated：至少一次每状态迁移，payload 带 seq（幂等合并用）。

    直接 bus.publish（lobby + project 频道）——**禁**走
    ``publish_stream_event``，否则会进 agent 主聊/活动流。
    """
    mid = str(meeting.get("id") or "")
    _EVENT_SEQ[mid] = _EVENT_SEQ.get(mid, 0) + 1
    event: dict[str, Any] = {
        "type": "meeting_updated",
        "meetingId": mid,
        "projectId": project_id,
        "status": meeting.get("status"),
        "topicIndex": meeting.get("topic_index"),
        "roundIndex": meeting.get("round_index"),
        "title": meeting.get("title") or "",
        "seq": _EVENT_SEQ[mid],
    }
    event.update(extra)
    try:
        await status_event_bus.publish("lobby", event)
        await status_event_bus.publish(f"project:{project_id}", event)
    except Exception as e:
        log.debug("meeting_event_publish_failed", error=str(e))


# ── 发起 ─────────────────────────────────────────────────────


async def start_meeting(
    project_id: str,
    chair_id: str,
    title: str,
    topics: list[str],
    participant_ids: list[str],
) -> dict[str, Any]:
    """登记会议行 + hold，立刻返回（编排器在独立 task 里等集合）。"""
    meeting = await svc.create_meeting(
        project_id, chair_id, title, topics, participant_ids
    )
    # 先登记 hold（卡口即刻生效），再 create_task 编排器 —— 顺序不可换。
    await hold.apply_hold(
        project_id,
        meeting["id"],
        list(meeting["participants"] or []),
        started_at_ms=meeting.get("hold_started_at"),
    )
    _RUNNING[meeting["id"]] = asyncio.create_task(
        _orchestrate(project_id, meeting["id"]),
        name=f"meeting-orchestrate-{meeting['id'][:8]}",
    )
    await emit_meeting_event(project_id, meeting)
    log.info(
        "meeting_started",
        project_id=project_id,
        meeting_id=meeting["id"][:12],
        chair_id=chair_id,
        topics=len(topics or []),
        participants=len(meeting["participants"] or []),
    )
    return meeting


# ── 编排主循环 ───────────────────────────────────────────────


async def _orchestrate(
    project_id: str, meeting_id: str, *, recovering: bool = False
) -> None:
    try:
        meeting = await svc.get_meeting(project_id, meeting_id)
        if meeting is None or meeting["status"] not in ACTIVE_STATUSES:
            return
        if meeting["status"] == "assembling":
            alive = await _alive_roster(project_id, meeting["participants"])
            if len(alive) < 2:
                await abort_meeting(project_id, meeting_id, "roster_lt2")
                return
            ok = await _wait_all_idle(project_id, alive)
            if not ok:
                await abort_meeting(project_id, meeting_id, "assembly_timeout")
                return
            meeting = await _begin_collecting(project_id, meeting_id)
            await emit_meeting_event(project_id, meeting)
        while True:
            meeting = await svc.get_meeting(project_id, meeting_id)
            if meeting is None or meeting["status"] not in ACTIVE_STATUSES:
                return
            if meeting["status"] == "collecting":
                await _run_round(
                    project_id, meeting, recovering=recovering
                )
                recovering = False
                meeting = await svc.set_status(
                    project_id, meeting_id, "facilitating"
                )
                await emit_meeting_event(project_id, meeting)
            if meeting["status"] == "facilitating":
                decision = await _facilitate(project_id, meeting_id)
                if decision is None:
                    return  # aborted inside (chair timeout)
                meeting = await svc.get_meeting(project_id, meeting_id)
                if meeting is None or meeting["status"] != "facilitating":
                    return
                chair = meeting["chair_id"]
                if decision.get("action") == "continue":
                    meeting = await svc.continue_round(
                        project_id,
                        meeting_id,
                        chair,
                        str(decision.get("direction") or ""),
                    )
                else:
                    meeting = await svc.conclude_topic(
                        project_id,
                        meeting_id,
                        chair,
                        str(decision.get("result") or ""),
                    )
                await emit_meeting_event(project_id, meeting)
                if meeting.get("concluded_now"):
                    await _deliver_and_return(project_id, meeting_id)
                    return
    except asyncio.CancelledError:
        raise
    except MeetingError as e:
        log.warning("meeting_orchestrate_meeting_error",
                    meeting_id=meeting_id[:12], error=str(e))
        await _safe_abort(project_id, meeting_id, "internal")
    except Exception as e:
        log.error("meeting_orchestrate_error",
                  meeting_id=meeting_id[:12], error=str(e), exc_info=True)
        await _safe_abort(project_id, meeting_id, "internal")
    finally:
        _RUNNING.pop(meeting_id, None)


async def _safe_abort(project_id: str, meeting_id: str, reason: str) -> None:
    try:
        await abort_meeting(project_id, meeting_id, reason)
    except Exception as e:
        log.error("meeting_safe_abort_failed",
                  meeting_id=meeting_id[:12], error=str(e))


async def _begin_collecting(
    project_id: str, meeting_id: str
) -> dict[str, Any]:
    """assembling → collecting（首个议题从第 1 轮开始）。"""
    return await svc.begin_collecting(project_id, meeting_id)


async def _alive_roster(
    project_id: str, participants: list[str]
) -> list[str]:
    """仍在名册且未归档的人（agent 行缺失按存活处理 —— dismiss 钩子负责显式移除）。"""
    from hiveweave.services.org import OrgService

    org = OrgService()
    alive: list[str] = []
    for aid in participants or []:
        try:
            row = await org.get_agent(aid)
        except Exception:
            row = None
        if row is not None and (row.get("status") or "") in (
            "archived",
            "dismissed",
        ):
            continue
        alive.append(aid)
    return alive


async def _wait_all_idle(
    project_id: str, participants: list[str]
) -> bool:
    """等每位参会者 IDLE 且无在飞 offturn 子代理。永不 cancel()。"""
    from hiveweave.agents.supervisor import agent_manager
    from hiveweave.services.offturn import has_live_jobs_for_agent

    deadline = time.monotonic() + _ASSEMBLY_TIMEOUT_S
    while True:
        meeting = await svc.get_active_meeting(project_id)
        if meeting is None:
            return False  # 已被外部 abort
        pending = False
        for aid in participants:
            agent = agent_manager.get_agent(aid)
            if agent is not None:
                status = getattr(agent.status, "value", str(agent.status))
                if status == "processing":
                    pending = True
                    continue
            if has_live_jobs_for_agent(aid):
                pending = True
        if not pending:
            return True
        if time.monotonic() > deadline:
            log.warning("meeting_assembly_timeout",
                        project_id=project_id)
            return False
        await asyncio.sleep(_ASSEMBLY_POLL_S)


# ── 发言 fan-out ─────────────────────────────────────────────


async def _run_round(
    project_id: str, meeting: dict[str, Any], *, recovering: bool = False
) -> None:
    """一轮盲评并行发言（≤8 并发）。恢复模式把未发言者补成弃权。"""
    meeting_id = meeting["id"]
    topic_index = int(meeting["topic_index"])
    round_index = int(meeting["round_index"])
    roster = await _alive_roster(project_id, meeting["participants"])
    direction = await _latest_direction(
        project_id, meeting_id, topic_index, round_index
    )
    concluded = list(meeting["topic_results"] or [])

    for start in range(0, len(roster), _MAX_PARALLEL_SPEECHES):
        batch = roster[start : start + _MAX_PARALLEL_SPEECHES]
        results = await asyncio.gather(
            *(
                _participant_turn(
                    project_id,
                    meeting,
                    aid,
                    topic_index=topic_index,
                    round_index=round_index,
                    direction=direction,
                    concluded=concluded,
                    recovering=recovering,
                )
                for aid in batch
            ),
            return_exceptions=True,
        )
        for aid, res in zip(batch, results):
            if isinstance(res, Exception):
                log.warning("meeting_participant_turn_failed",
                            agent_id=aid, error=str(res))
                await _record_abstain_if_missing(
                    project_id, meeting, aid, topic_index, round_index,
                    f"turn error: {res}",
                    abstain_reason=ABSTAIN_ERROR,
                )


async def _latest_direction(
    project_id: str, meeting_id: str, topic_index: int, round_index: int
) -> str | None:
    if round_index < 2:
        return None
    rows = await svc.get_utterances(
        project_id,
        meeting_id,
        topic_index=topic_index,
        round_index=round_index - 1,
        roles=("direction",),
    )
    return str(rows[-1]["content"]) if rows else None


def _resolve_agent(aid: str) -> Any:
    from hiveweave.agents.supervisor import agent_manager

    return agent_manager.get_agent(aid)


async def _record_abstain_if_missing(
    project_id: str,
    meeting: dict[str, Any],
    agent_id: str,
    topic_index: int,
    round_index: int,
    reason: str,
    abstain_reason: str = "",
) -> None:
    try:
        if await svc.has_utterance(
            project_id,
            meeting["id"],
            topic_index=topic_index,
            round_index=round_index,
            agent_id=agent_id,
            roles=("speech", "abstain"),
        ):
            return
        await svc.record_utterance(
            project_id,
            meeting["id"],
            topic_index=topic_index,
            round_index=round_index,
            agent_id=agent_id,
            role="abstain",
            content=reason,
            abstain_reason=abstain_reason,
            _force=True,
        )
        await emit_meeting_event(project_id, meeting)
    except MeetingError as e:
        log.debug("meeting_abstain_record_skipped", error=str(e))


async def _participant_turn(
    project_id: str,
    meeting: dict[str, Any],
    agent_id: str,
    *,
    topic_index: int,
    round_index: int,
    direction: str | None,
    concluded: list[dict[str, Any]],
    recovering: bool,
) -> None:
    if recovering:
        # 泵恢复：collecting 中途重启 → 未发言者直接补弃权（规格 §泵）
        # ⚠ 结构化原因：本人**从未表态**（不是「听了没意见」），且加轮救不回
        # —— 渲染时必须与真弃权分开，见 ABSTAIN_WRITTEN_BY_PLATFORM。
        await _record_abstain_if_missing(
            project_id, meeting, agent_id, topic_index, round_index,
            "recovered after restart — recorded as abstain",
            abstain_reason=ABSTAIN_RECOVERED,
        )
        return
    if await svc.has_utterance(
        project_id,
        meeting["id"],
        topic_index=topic_index,
        round_index=round_index,
        agent_id=agent_id,
        roles=("speech", "abstain"),
    ):
        return  # 幂等：本轮已发言/已弃权
    agent = _resolve_agent(agent_id)
    name = ""
    if agent is not None:
        name = str((getattr(agent, "config", None) or {}).get("name") or "")
    if agent is None and _runner_fn is None:
        await _record_abstain_if_missing(
            project_id, meeting, agent_id, topic_index, round_index,
            "no live agent instance",
            abstain_reason=ABSTAIN_UNAVAILABLE,
        )
        return
    briefing = prompts.participant_briefing(
        meeting_title=str(meeting.get("title") or ""),
        topic_title=str(
            (meeting.get("topics") or [])[topic_index]
            if topic_index < len(meeting.get("topics") or [])
            else ""
        ),
        round_index=round_index,
        max_rounds=MAX_ROUNDS,
        direction=direction,
        concluded_results=concluded,
        attendee_name=name,
    )
    outcome = await _invoke_runner(
        agent,
        tool_profile="participant",
        briefing=briefing,
    )
    if outcome.get("action") == "speak":
        try:
            await svc.record_utterance(
                project_id,
                meeting["id"],
                topic_index=topic_index,
                round_index=round_index,
                agent_id=agent_id,
                role="speech",
                content=str(outcome.get("content") or ""),
            )
            await emit_meeting_event(
                project_id, meeting, lastSpeech=str(outcome.get("content") or "")[:500]
            )
            return
        except MeetingError as e:
            log.warning("meeting_speech_rejected",
                        agent_id=agent_id, error=str(e))
            return
    await _record_abstain_if_missing(
        project_id, meeting, agent_id, topic_index, round_index,
        str(outcome.get("content") or "abstain"),
        # ⭐ 结构化原因：runner 判定的「预算切断/超时/异常/真弃权」经此落库。
        # 主持人简报据此把「未完成」与「弃权」分开渲染（不再一样）。
        abstain_reason=str(outcome.get("abstain_reason") or ""),
    )


# ── 主持回合 ─────────────────────────────────────────────────


async def _facilitate(
    project_id: str, meeting_id: str
) -> dict[str, Any] | None:
    """主持决策回合。180s 无 continue/conclude → 重唤一次 → abort。"""
    meeting = await svc.get_meeting(project_id, meeting_id)
    if meeting is None:
        return None
    chair = meeting["chair_id"]
    topic_index = int(meeting["topic_index"])
    round_index = int(meeting["round_index"])
    allow_continue = round_index < MAX_ROUNDS
    speeches = await svc.get_utterances(
        project_id,
        meeting_id,
        topic_index=topic_index,
        round_index=round_index,
        roles=("speech", "abstain"),
    )
    chair_agent = _resolve_agent(chair)
    briefing = prompts.chair_briefing(
        meeting_title=str(meeting.get("title") or ""),
        topic_title=str(
            (meeting.get("topics") or [])[topic_index]
            if topic_index < len(meeting.get("topics") or [])
            else ""
        ),
        round_index=round_index,
        max_rounds=MAX_ROUNDS,
        speeches=speeches,
        concluded_results=list(meeting["topic_results"] or []),
        allow_continue=allow_continue,
    )
    for attempt in range(1 + _CHAIR_REWAKE_ATTEMPTS):
        outcome = await _invoke_runner(
            chair_agent,
            tool_profile="chair",
            briefing=briefing,
            allow_continue=allow_continue,
        )
        action = str(outcome.get("action") or "")
        if action == "continue" and allow_continue:
            return outcome
        if action == "conclude":
            return outcome
        if attempt < _CHAIR_REWAKE_ATTEMPTS:
            log.warning("meeting_chair_rewake",
                        meeting_id=meeting_id[:12], attempt=attempt + 1)
    log.warning("meeting_chair_timeout_abort", meeting_id=meeting_id[:12])
    await abort_meeting(project_id, meeting_id, "chair_timeout")
    return None


async def _invoke_runner(
    agent: Any,
    *,
    tool_profile: str,
    briefing: str,
    allow_continue: bool = True,
) -> dict[str, Any]:
    fn = _runner_fn or run_meeting_turn
    return await fn(
        agent,
        tool_profile=tool_profile,
        briefing=briefing,
        allow_continue=allow_continue,
        timeout_s=MEETING_SPEECH_TIMEOUT_S,
    )


# ── abort / 回岗 ─────────────────────────────────────────────


async def abort_meeting(
    project_id: str, meeting_id: str, reason: str
) -> bool:
    """abort：不代写结论；解 hold + [MEETING ABORTED] + 一次 trigger 回岗。"""
    meeting = await svc.get_meeting(project_id, meeting_id)
    if meeting is None or meeting["status"] not in ACTIVE_STATUSES:
        return False
    try:
        meeting = await svc.set_status(project_id, meeting_id, "aborted")
    except MeetingError as e:
        log.warning("meeting_abort_transition_failed", error=str(e))
        return False
    await _return_to_desk(
        project_id, meeting, abort_reason=prompts.aborted_body(
            reason, str(meeting.get("title") or "")
        ),
        abort_reason_key=reason,
    )
    await emit_meeting_event(project_id, meeting, abortReason=reason)
    log.info("meeting_aborted", meeting_id=meeting_id[:12], reason=reason)
    return True


async def abort_active_meetings_for_project(
    project_id: str, reason: str = "off_duty"
) -> int:
    rows = await svc.list_meetings(project_id, limit=200)
    n = 0
    for m in rows:
        if m.get("status") in ACTIVE_STATUSES:
            if await abort_meeting(project_id, str(m["id"]), reason):
                n += 1
    return n


async def _deliver_and_return(project_id: str, meeting_id: str) -> dict:
    """concluded → 投递 RESULT + 回岗协议（幂等，泵按 meeting id 重试）。"""
    meeting = await svc.get_meeting(project_id, meeting_id)
    if meeting is None:
        return {}
    await _return_to_desk(project_id, meeting)
    await svc.mark_delivered(project_id, meeting_id)
    meeting = await svc.get_meeting(project_id, meeting_id) or meeting
    await emit_meeting_event(project_id, meeting)
    log.info("meeting_delivered", meeting_id=meeting_id[:12])
    return {"delivered": True}


async def _return_to_desk(
    project_id: str,
    meeting: dict[str, Any],
    *,
    abort_reason: str | None = None,
    abort_reason_key: str | None = None,
) -> None:
    """回岗协议（规格 §散会后回到岗位，每人独立、可幂等）。

    1. 插 [MEETING RESULT] / [MEETING ABORTED]（wake=True、非 ask、
       idempotency_key 幂等）；2. unpark 原行（禁走 deliver_resume_briefings
       —— 会 mark_read 丢 ask 合同）；3. wait 补时；4. 清 hold；
    5. last_active_at=now；6. 一次普通 trigger 消化。
    """
    from hiveweave.services.inbox import InboxService
    from hiveweave.agents.trigger import (
        trigger_coordinator,
        trigger_subordinate,
    )
    from hiveweave.services.policy import infer_role_family

    inbox = InboxService()
    roster = await _alive_roster(project_id, meeting["participants"])
    if abort_reason is not None:
        tag = prompts.MEETING_ABORTED_TAG
        body = abort_reason
        idem_prefix = "meeting-abort"
    else:
        tag = prompts.MEETING_RESULT_TAG
        body = prompts.result_body(list(meeting["topic_results"] or []))
        idem_prefix = "meeting-result"
    text = f"{tag}\n{body}"
    for aid in roster:
        try:
            await inbox.send_message(
                from_agent_id="system",
                to_agent_id=aid,
                message=text,
                message_type="system",
                wake=True,
                expect_report=False,
                idempotency_key=f"{idem_prefix}-{meeting['id']}-{aid}",
                trusted_platform=True,
            )
        except Exception as e:
            log.warning("meeting_result_insert_failed",
                        agent_id=aid, error=str(e))
    for aid in roster:
        await hold.release_hold(aid)
    await hold.refresh_last_active(project_id, roster)
    for aid in roster:
        try:
            unread = await inbox.get_unread_count(aid)
        except Exception:
            unread = 1
        if unread <= 0:
            continue
        try:
            row = None
            try:
                from hiveweave.services.org import OrgService

                row = await OrgService().get_agent(aid)
            except Exception:
                row = None
            if row is not None and infer_role_family(row) == "coordinator":
                await trigger_coordinator(aid)
            else:
                await trigger_subordinate(aid)
        except Exception as e:
            log.warning("meeting_return_trigger_failed",
                        agent_id=aid, error=str(e))


# ── 泵（lifespan / activate / game_time tick）────────────────


async def recover_meetings(project_id: str) -> dict[str, int]:
    """恢复泵：恢复 hold、补弃权、重唤主持、重试 RESULT 投递（幂等）。"""
    from hiveweave.services.project_lifecycle import project_known_off_duty

    stats = {"resumed": 0, "delivered": 0, "aborted": 0}
    try:
        if await project_known_off_duty(project_id):
            stats["aborted"] = await abort_active_meetings_for_project(
                project_id, "off_duty"
            )
            return stats
        rows = await svc.recover_candidates(project_id)
    except Exception as e:
        log.debug("meeting_recover_scan_failed", error=str(e))
        return stats
    for m in rows:
        meeting_id = str(m["id"])
        try:
            if m.get("status") == "concluded":
                if (m.get("delivery_state") or "") == "pending":
                    await _deliver_and_return(project_id, meeting_id)
                    stats["delivered"] += 1
                continue
            await hold.apply_hold(
                project_id,
                meeting_id,
                list(m.get("participants") or []),
                started_at_ms=m.get("hold_started_at"),
            )
            task = _RUNNING.get(meeting_id)
            if task is not None and not task.done():
                continue
            recovering = m.get("status") in ("collecting", "facilitating")
            _RUNNING[meeting_id] = asyncio.create_task(
                _orchestrate(project_id, meeting_id, recovering=recovering),
                name=f"meeting-recover-{meeting_id[:8]}",
            )
            stats["resumed"] += 1
        except MeetingConflict as e:
            log.warning("meeting_recover_conflict", error=str(e))
        except Exception as e:
            log.warning("meeting_recover_row_failed",
                        meeting_id=meeting_id[:12], error=str(e))
    if any(stats.values()):
        log.info("meeting_recover_done", project_id=project_id, **stats)
    return stats


# ── dismiss 钩子 ─────────────────────────────────────────────


async def handle_agent_dismissed(project_id: str, agent_id: str) -> None:
    """主席 dismiss → abort；参会者 dismiss → 弃权 + 移出名册；活人 <2 → abort。"""
    try:
        rows = await svc.list_meetings(project_id, limit=200)
    except Exception as e:
        log.debug("meeting_dismiss_scan_failed", error=str(e))
        return
    for m in rows:
        if m.get("status") not in ACTIVE_STATUSES:
            continue
        meeting_id = str(m["id"])
        if str(m.get("chair_id")) == str(agent_id):
            await abort_meeting(project_id, meeting_id, "chair_dismissed")
            continue
        if agent_id in (m.get("participants") or []):
            try:
                await svc.record_utterance(
                    project_id,
                    meeting_id,
                    topic_index=int(m["topic_index"]),
                    round_index=int(m["round_index"]),
                    agent_id=agent_id,
                    role="abstain",
                    content="dismissed during meeting",
                    # ⚠ 结构化原因：本人**从未表态**（被移出名册），不是「听了
                    # 没意见」。空串会把它渲染成真弃权 —— 正是本修复根除的错。
                    abstain_reason=ABSTAIN_DISMISSED,
                    _force=True,
                )
            except MeetingError:
                pass
            try:
                updated = await svc.remove_participant(
                    project_id, meeting_id, agent_id
                )
                alive = await _alive_roster(
                    project_id,
                    list((updated or {}).get("participants") or []),
                )
                if len(alive) < 2:
                    await abort_meeting(project_id, meeting_id, "roster_lt2")
            except Exception as e:
                log.warning("meeting_dismiss_roster_update_failed",
                            meeting_id=meeting_id[:12], error=str(e))
    hold.clear_hold_silent(agent_id)
