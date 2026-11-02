"""跨进程 crash-loop 守卫（施工单 2026-11-03 §4，上游对表 pi durable）。

病灶：``Agent._consecutive_errors`` 是进程内存属性 —— 某条 LLM 请求若
反复让进程死亡，每次重启都能经 ``find_interrupted_run`` + 恢复简报继续
续跑同一条 run（重启即归零），平台层没有任何持久化的止损位。

机制（三件套，全部 best-effort）：
- **在飞标记**：``http_stream`` 在主 run 的 LLM 请求链进入前
  ``RunLedger.mark_inflight``、``finally`` ``clear_inflight`` —— 只有
  「进程死在请求中间」才留下标记（工具执行中 / 请求间隙的正常重启不计数）。
- **启动 sweep 记账**（``run_ledger._sweep_running_rows_locked`` 的
  ``account_crashes=True`` 路径）：带标记的 running 行 ⇒
  ``agents.crash_streak += 1``；streak ≥ ``HIVEWEAVE_CRASH_LOOP_MAX``
  （默认 3）⇒ 该 run 标 ``failed``（不再自动续跑）并在 streak **恰好
  等于** MAX 时升级上级一次；无标记 ⇒ 现状 interrupted，不计数。
- **Agent 侧门禁**：加载时 ``crash_streak ≥ MAX`` ⇒
  ``_arm_resume_suppressed()``（现有 30min latch，user/task wake 仍可
  穿透手动恢复）；``complete_run`` 成功 ⇒ streak 清零。

升级为什么走队列：startup sweep 先于 ``agent_router.rebuild()`` 与
``start_project_agents``（main.py lifespan 顺序）—— 服务层按 agent_id
路由在 sweep 时刻不可靠（``_insert_placeholder_usage_for_swept_runs``
同款教训），故 sweep 锁内只落库收集，``flush_pending_escalations()``
在路由重建 + agent 启动后统一发送（main.py 调用）。
"""

from __future__ import annotations

import os

import structlog

log = structlog.get_logger(__name__)

CRASH_LOOP_MAX_DEFAULT = 3
"""连续「LLM 请求在飞时进程死亡」的容忍次数。"""

CRASH_LOOP_REASON_TEMPLATE = (
    "crash_loop: {streak} consecutive process deaths during in-flight "
    "LLM request"
)
"""crash-loop run 行的 ``error_reason`` 文案（含 streak，落 ``failed`` 行）。"""


def crash_loop_max() -> int:
    """升级阈值（env ``HIVEWEAVE_CRASH_LOOP_MAX``；非法/非正值回退默认）。"""
    raw = (os.environ.get("HIVEWEAVE_CRASH_LOOP_MAX") or "").strip()
    if not raw:
        return CRASH_LOOP_MAX_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return CRASH_LOOP_MAX_DEFAULT
    return value if value > 0 else CRASH_LOOP_MAX_DEFAULT


def crash_loop_guard_enabled() -> bool:
    """总开关（env ``HIVEWEAVE_CRASH_LOOP_GUARD=0`` 关闭；误判逃生门）。"""
    return (os.environ.get("HIVEWEAVE_CRASH_LOOP_GUARD") or "").strip() != "0"


# ── 升级队列（sweep 收集 → 路由重建后统一发送）──────────────────────

#: ``{"agent_id", "streak", "run_id", "workspace_path"}``；有界防膨胀。
_PENDING_ESCALATIONS: list[dict] = []
_PENDING_ESCALATIONS_MAX = 64


def queue_escalations(items: list[dict]) -> None:
    """sweep 锁外入队（只收集，不发送 —— 路由此刻不可靠）。"""
    overflow = len(_PENDING_ESCALATIONS) + len(items) - _PENDING_ESCALATIONS_MAX
    if overflow > 0:
        del _PENDING_ESCALATIONS[:overflow]
    _PENDING_ESCALATIONS.extend(items)


def pending_escalations() -> list[dict]:
    """只读快照（测试/观测用）。"""
    return list(_PENDING_ESCALATIONS)


def reset_pending_escalations() -> None:
    """清空队列（测试隔离用）。"""
    _PENDING_ESCALATIONS.clear()


async def flush_pending_escalations() -> int:
    """统一发送积累的升级（main.py 在 agent_router 重建 + agent 启动后调）。

    每条 best-effort：失败只记日志并丢弃（与
    ``recovery.escalate_turn_interruption`` 同纪律 —— 升级是提示性附加物，
    重试链路不值得为其引入状态机）。返回成功发送条数。
    """
    if not _PENDING_ESCALATIONS:
        return 0
    batch = _PENDING_ESCALATIONS[:]
    _PENDING_ESCALATIONS.clear()
    sent = 0
    for item in batch:
        ok = await _escalate_one(
            item["agent_id"], item["streak"], item.get("run_id") or ""
        )
        sent += 1 if ok else 0
    return sent


async def _escalate_one(agent_id: str, streak: int, last_run_id: str) -> bool:
    """对单个 agent 发升级：上级查询 + urgent inbox + 触发上级。"""
    try:
        from hiveweave.services.org import OrgService

        superior = await OrgService().get_superior(agent_id)
        if not superior:
            log.warning(
                "crash_loop_escalate_no_superior",
                agent_id=agent_id, crash_streak=streak,
            )
            return False
        from hiveweave.services.inbox import InboxService

        org = OrgService()
        agent_row = await org.get_agent(agent_id)
        agent_name = (agent_row or {}).get("name") or agent_id
        await InboxService().send_message(
            from_agent_id=agent_id,
            to_agent_id=superior["id"],
            message=(
                f"[ESCALATION] Subordinate {agent_name} stopped auto-resuming "
                f"after {streak} consecutive process deaths during an "
                f"in-flight LLM request (last run: {last_run_id or 'n/a'}). "
                f"The crashed runs were marked failed; user/task wake can "
                f"still resume them manually. Please check on them and any "
                f"submitted tasks."
            ),
            message_type="escalation",
            priority="urgent",
            wake=True,
            trusted_platform=True,
        )
        from hiveweave.agents.trigger import trigger_coordinator

        await trigger_coordinator(superior["id"])
        log.warning(
            "crash_loop_escalated",
            agent_id=agent_id,
            superior_id=superior["id"],
            crash_streak=streak,
            last_run_id=last_run_id,
        )
        return True
    except Exception as e:  # noqa: BLE001 — best-effort，绝不阻断启动
        log.error(
            "crash_loop_escalate_failed", agent_id=agent_id, error=str(e)
        )
        return False
