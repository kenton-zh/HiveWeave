"""Project on/off-duty lifecycle — park inbox, stop agents cleanly, resume briefings.

Deactivate must not leave watchers polling or a wake stampede on next activate.
"""

from __future__ import annotations

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.inbox import InboxService

log = structlog.get_logger(__name__)

OFF_DUTY_CANCEL_REASON = "off_duty"
OFF_DUTY_STREAM_CONTENT = "[项目已下班，本轮进度已保存]"


async def _project_agent_ids(project_id: str) -> list[str]:
    """Union of router IDs + in-memory manager IDs + DB active agents."""
    ids: set[str] = set()
    try:
        from hiveweave.services.agent_router import agent_router

        ids.update(agent_router.get_project_agent_ids(project_id) or [])
    except Exception as e:
        log.warning("lifecycle_router_ids_failed", project_id=project_id, error=str(e))
    try:
        from hiveweave.agents.supervisor import agent_manager

        for aid, agent in list(agent_manager._agents.items()):
            if getattr(agent, "project_id", None) == project_id:
                ids.add(aid)
    except Exception as e:
        log.warning("lifecycle_manager_ids_failed", project_id=project_id, error=str(e))
    # Always union DB active agents — router/manager can be partial
    try:
        from hiveweave.db import project as project_db

        conn = await project_db.get_project_db_by_project_id(project_id)
        cursor = await conn.execute(
            "SELECT id FROM agents WHERE status = 'active'"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        ids.update(r[0] if not hasattr(r, "keys") else r["id"] for r in rows)
    except Exception as e:
        log.warning("lifecycle_db_ids_failed", project_id=project_id, error=str(e))
    return sorted(ids)


async def park_project_inbox(project_id: str, agent_ids: list[str] | None = None) -> int:
    """Park wake=1 unread inbox so deactivate does not leave a wake stampede.

    Returns number of messages parked.
    """
    ids = agent_ids if agent_ids is not None else await _project_agent_ids(project_id)
    inbox = InboxService()
    total = 0
    for aid in ids:
        try:
            total += await inbox.park_pending_wakes(aid)
        except Exception as e:
            log.warning("park_inbox_agent_failed", agent_id=aid, error=str(e))
    log.info("park_project_inbox_done", project_id=project_id, parked=total, agents=len(ids))
    return total


async def close_running_runs(
    project_id: str,
    *,
    close_reason: str = "startup_sweep: stale running run from prior process",
) -> int:
    """I8 (批7)：收尾该项目 workspace 内所有 ``status='running'`` 的 agent_runs。

    病灶：``agent.cancel``（off_duty 下班）不写 run ledger —— cancel 只掐
    asyncio task，run 行永远停在 running（s3-clone_13 实测 8 个 run 30 分钟
    后仍 ``status='running'``、``ended_at`` 全 NULL）。修复 = 复用统一判定源
    ``sweep_stale_agent_runs``（running→interrupted + 孤儿 run_step 按
    started 事实位分流），**不新写清扫**；启动 sweep（main.py lifespan）、
    停止收尾（stop_project_cleanly）、恢复回收（activate 路径）三处同源。

    ``close_reason``（批 7 审计 P2-2）：落进 agent_runs.error_reason ——
    调用方必须给可区分来路的文案（off_duty_close / activate_reclaim），
    恢复 Trail 不能把本进程主动关闭误读成「进程死亡残留」。

    Returns number of runs reaped；任何失败 best-effort 返回 0（下班/上班
    流程不能因收尾失败而中断）。
    """
    try:
        from hiveweave.services.run_ledger import sweep_stale_agent_runs

        workspace_path = await meta_db.get_project_workspace(project_id)
        if not workspace_path:
            return 0
        return await sweep_stale_agent_runs(
            workspace_path, close_reason=close_reason)
    except Exception as e:
        log.warning(
            "close_running_runs_failed",
            project_id=project_id,
            error=str(e),
        )
        return 0


async def stop_project_cleanly(project_id: str) -> dict:
    """Stop every in-memory agent for the project (manager ∪ router), off-duty cancel."""
    from hiveweave.agents.supervisor import agent_manager

    ids = await _project_agent_ids(project_id)
    result = {
        "stopped": 0,
        "errors": 0,
        "agent_ids": ids,
        "leftover_cleared": 0,
        "offturn_reaped": 0,
        "runs_interrupted": 0,
    }

    # Reap before cancel so finishing jobs cannot wake=1 after agents die.
    try:
        from hiveweave.services.offturn import reap_offturn_for_project

        result["offturn_reaped"] = await reap_offturn_for_project(project_id)
    except Exception as e:
        log.warning(
            "stop_project_offturn_reap_failed",
            project_id=project_id,
            error=str(e),
        )

    stopped = 0
    errors = 0
    for aid in ids:
        try:
            agent = agent_manager.get_agent(aid)
            if agent is not None:
                await agent.cancel(reason=OFF_DUTY_CANCEL_REASON)
                # Ensure removed from registry even if cancel left it
                agent_manager._agents.pop(aid, None)
                stopped += 1
            else:
                # Still try stop_agent for symmetry / logging
                await agent_manager.stop_agent(aid)
        except Exception as e:
            errors += 1
            log.warning(
                "stop_project_agent_failed",
                project_id=project_id,
                agent_id=aid,
                error=str(e),
            )
            agent_manager._agents.pop(aid, None)

    # Final sweep — anything still keyed to this project_id
    leftover = [
        aid
        for aid, ag in list(agent_manager._agents.items())
        if getattr(ag, "project_id", None) == project_id
    ]
    for aid in leftover:
        try:
            await agent_manager.stop_agent(aid)
            stopped += 1
        except Exception:
            agent_manager._agents.pop(aid, None)
            errors += 1

    result["stopped"] = stopped
    result["errors"] = errors
    result["leftover_cleared"] = len(leftover)

    # TEST6 evening P2-6: kill main-checkout + all registered project processes
    try:
        from hiveweave.services.process_registry import stop_processes_for_project

        proc = stop_processes_for_project(project_id)
        result["processes"] = proc
    except Exception as e:
        log.warning(
            "stop_project_processes_failed",
            project_id=project_id,
            error=str(e),
        )
        result["processes"] = {"error": str(e)}

    # I8 (批7)：run 收尾必须排在 agent 全部 cancel **之后** —— cancel 会
    # await 已掐的 llm task，此后不再有写 run 行的在跑写方，sweep 才不会与
    # 完成路径竞态。cancel 路径本身不写 ledger（I8 根因），由这里的统一
    # sweep 兜底：running→interrupted + 孤儿 run_step 分流。
    result["runs_interrupted"] = await close_running_runs(
        project_id,
        close_reason="off_duty_close: run closed by project stop (agent cancelled)",
    )

    # I9（批 8 触发 B）：下班/停止时所有未收口 question（pending + 在途被
    # cancel 的）立即按默认项裁决（resolved_by='lifecycle_stop'）+ 落
    # wake=1 inbox —— activate 的 pre-park 会把它并进复工 briefing（交接
    # 摘要）。与上面 run 收尾同位：排在 agent 全部 cancel 之后，此后不再
    # 有会回答/裁决 question 的在跑写方。
    try:
        from hiveweave.tools.question import adjudicate_project_questions

        adjudicated = await adjudicate_project_questions(
            project_id, reason="lifecycle_stop"
        )
        result["questions_adjudicated"] = len(adjudicated)
    except Exception as e:
        log.warning(
            "stop_project_question_adjudicate_failed",
            project_id=project_id,
            error=str(e),
        )
        result["questions_adjudicated"] = 0

    log.info("stop_project_cleanly_done", project_id=project_id, **result)
    return result


async def deliver_resume_briefings(project_id: str) -> dict:
    """On activate: park any leftover wake=1, then one coalesced briefing per agent.

    Parking here covers projects deactivated before the park feature existed
    (or deactivate that failed mid-way) so activate never stampede-wakes.
    """
    ids = await _project_agent_ids(project_id)
    # I9（批 8 触发 A 补票）：停机期间 expires_at 已过仍 pending 的 question
    # （进程重启丢了内存裁决钟）在此按 resolved_by='timeout' 补裁决。排在
    # pre-park **之前** —— 裁决产出的 wake=1 inbox 会被 park 进当轮复工
    # briefing（交接摘要），提问 agent 复工即知默认项裁决结果。
    try:
        from hiveweave.tools.question import adjudicate_expired_questions

        await adjudicate_expired_questions(project_id)
    except Exception as e:
        log.warning(
            "resume_question_catchup_failed",
            project_id=project_id,
            error=str(e),
        )
    # Safety net: coalesce leftover wake=1 unread even if deactivate skipped park
    pre_parked = await park_project_inbox(project_id, ids)
    inbox = InboxService()
    briefed = 0
    cleared = 0
    for aid in ids:
        try:
            n_cleared, sent = await inbox.deliver_parked_briefing(aid)
            cleared += n_cleared
            if sent:
                briefed += 1
        except Exception as e:
            log.warning(
                "resume_briefing_failed",
                project_id=project_id,
                agent_id=aid,
                error=str(e),
            )
    result = {
        "briefed": briefed,
        "parked_cleared": cleared,
        "pre_parked": pre_parked,
        "agents": len(ids),
    }
    log.info("deliver_resume_briefings_done", project_id=project_id, **result)
    return result


async def project_is_started(project_id: str | None) -> bool:
    if not project_id:
        return False
    row = await meta_db.query_one(
        "SELECT is_started FROM projects WHERE id = ?", [project_id]
    )
    if not row:
        return False
    return bool(dict(row).get("is_started"))


async def project_known_off_duty(project_id: str | None) -> bool:
    """True only when we positively know is_started=0.

    Missing project / DB errors → False (fail-open) so inbox watchers in
    tests or transient routing gaps keep polling instead of going silent.
    """
    if not project_id:
        return False
    try:
        row = await meta_db.query_one(
            "SELECT is_started FROM projects WHERE id = ?", [project_id]
        )
    except Exception:
        return False
    if not row:
        return False
    return not bool(dict(row).get("is_started"))
