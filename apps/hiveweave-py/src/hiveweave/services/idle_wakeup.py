"""I10 idle wakeup — run 完成收口点的「下一个该谁动」评估与定向唤醒。

病灶（s3-clone_13 实测，PLATFORM-ISSUES §十五 I10/P1-6）：09:08:14 最后
一个 run 正常完成后，组织空转 **14.9 分钟** —— 期间唯一兜底唤醒源是
silence_watchdog（阈值即系统最小空转粒度）；task_event 投递
``inbox.wake=0``（通知型不打断），两条 [TASK CLOSED] 躺在收件箱里无人看。

修法（fixplan §四 I10 方法①②落在本模块，③在 game_time）：
① run 正常完成收口点（``agents/completion.py::handle_completion`` 末尾）
  评估「下一个该谁动」：存在**唯一可推进者** ⇒ 直接唤醒；
② ``task_event_relay`` 投递目标是唯一可推进者 ⇒ ``inbox.wake=1``
  （此前硬编码只认 ``task.blocked``）。

**判据只认状态，不读文案**（fixplan 三铁律之一）：
- tasks 非终态：``TERMINAL_STATUSES``（``services/tasks/constants.py``，
  ADR-001 R1 唯一终态常量）；
- agent_waits 未满足：``cleared_at IS NULL`` 且未过期 ⇒ **合法停泊**，
  停泊者不是唤醒候选（commit_turn 声明的等待不许被调度器打破）；
- 各 agent 活跃度：``status='active'``、非 processing（在干活的不是
  「下一个该谁动」的对象）、``last_active_at`` 产出推进重置唤醒预算。

**防循环**（↗ 上游移植：DSH ``packages/jobs/tool-jobs/src/index.ts:59-64``
``completionDelivery`` 默认 ``'wakeup'`` + ``maxConsecutiveWakes``）：
1. 第一道闸：**无待办 ⇒ 恒不唤醒** —— 没有任何「义务人」时本模块不产出
   任何触发（组织收工态靠看门狗「无事可做」长档兜底，不靠本模块空转）；
2. 第二道闸：同 agent **连唤上限**（``IDLE_WAKE_MAX_CONSECUTIVE``，默认
   3）—— 达上限后不再唤，直到 ``last_active_at`` 晚于上次唤醒时刻或预算
   窗口过期。
   ⚠ 批 9 审计 P2-1 如实标注：``last_active_at`` 的语义是「完成了任意一个
   run」（成功/失败一视同仁，挂点在 completion/recovery 共同汇点）——
   即被唤者**空转一轮也会重置自己的预算**，「唤醒不产出 ⇒ 停唤」只对
   trigger 在 run 启动前蒸发（no-context / meeting hold / archived）的
   形态成立。空转 run 循环由第一道闸（出口门 ok 判据 + waiting 停泊先落
   库 + 唯一性 None）间接兜住 + 窗口过期上界（每 agent 每小时 ≤3 次空唤）
   封顶；若未来出口门结构变化，需补 per-(project,agent) 最小间隔冷却。
"""

from __future__ import annotations

import os
import time

import structlog

log = structlog.get_logger(__name__)

# 连唤上限（DSH maxConsecutiveWakes 同型）。同一 agent 连续 N 次「被唤醒
# 但零产出」后停唤 —— 防无限唤醒循环；看门狗（分档阈值）仍是最终兜底。
IDLE_WAKE_MAX_CONSECUTIVE = int(
    os.environ.get("HIVEWEAVE_IDLE_WAKE_MAX_CONSECUTIVE", "3") or "3"
)
# 连唤预算窗口：超过此时长未再唤 ⇒ 计数作废（防陈旧计数永久关闭唤醒）。
IDLE_WAKE_BUDGET_WINDOW_MS = int(
    os.environ.get("HIVEWEAVE_IDLE_WAKE_BUDGET_WINDOW_MS", "3600000") or "3600000"
)
# relay 批量路径用的唯一可推进者短 TTL 缓存（一批事件只算一次）。
SOLE_ADVANCER_CACHE_TTL_S = 15.0

# (project_id) → (monotonic_ts, sole | None)
_sole_cache: dict[str, tuple[float, dict | None]] = {}
# (project_id, agent_id) → {"count": int, "last_wake_ms": int}
_wake_budget: dict[tuple[str, str], dict] = {}


def reset_wake_budget(project_id: str, agent_id: str | None = None) -> None:
    """清唤醒预算（测试 / 手工恢复用）。agent_id 为 None 清全项目。"""
    if agent_id is None:
        for key in [k for k in _wake_budget if k[0] == project_id]:
            _wake_budget.pop(key, None)
    else:
        _wake_budget.pop((project_id, agent_id), None)


def clear_sole_advancer_cache() -> None:
    """清唯一可推进者缓存（测试 / 强制重算用）。"""
    _sole_cache.clear()


async def compute_sole_advancer(
    project_id: str,
    *,
    exclude_agent_ids: frozenset[str] | set[str] = frozenset(),
) -> dict | None:
    """状态判据评估「下一个该谁动」。

    返回唯一可推进者 ``{"agent_id", "name", "role", "obligations",
    "ask_senders"}``，不唯一 / 不存在 / 项目下班 ⇒ None。

    候选资格（全部状态判据，查询异常按**有债**处理 —— 与
    ``has_open_work`` 的 fail-closed 同向；对「唯一性」判定而言
    fail-closed 只会让候选变多 ⇒ 更保守（不唤），不引入新静默面）：
    - ``status='active'``；
    - 不在 ``exclude_agent_ids``（刚收口的 run 主人由它自己的出口链
      负责，不参与「下一个该谁动」）；
    - 非 processing（在干活的不是唤醒对象）；
    - 无未过期 wait contract（合法停泊者不唤醒）；
    - 有开放义务：闭式 ``get_open_work_obligations``（ADR-001 §1 唯一
      判定源）非空，或未解除回复契约（outstanding ask）非空。
    """
    from hiveweave.db import meta as meta_db
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.task import TaskService, _query
    from hiveweave.services.wait_contract import wait_contract_service

    # 项目下班（is_started=0）不唤醒 —— 收口评估不是唤醒的豁免通道。
    try:
        proj = await meta_db.query_one(
            "SELECT is_started FROM projects WHERE id = ?", [project_id]
        )
        if not proj or not dict(proj).get("is_started"):
            return None
    except Exception as e:
        log.debug("sole_advancer_project_check_failed",
                  project_id=project_id, error=str(e))
        return None

    try:
        agents_raw = await _query(
            project_id,
            "SELECT id, name, role, status, last_active_at FROM agents "
            "WHERE status = 'active'",
            [],
        )
    except Exception as e:
        log.debug("sole_advancer_agents_query_failed",
                  project_id=project_id, error=str(e))
        return None
    agents = [dict(a) for a in agents_raw]
    if not agents:
        return None

    # 合法停泊（wait contract 未过期）不参与候选 —— wait 是 commit_turn
    # 声明的等待，调度器不得打破（否则等 timer / 等人的 agent 被反复空醒）。
    now_ms = int(time.time() * 1000)
    parked: set[str] = set()
    try:
        for w in await wait_contract_service.list_all_active(project_id) or []:
            exp = w.get("expiresAt")
            if exp is None or int(exp) > now_ms:
                aid = w.get("agentId") or ""
                if aid:
                    parked.add(aid)
    except Exception as e:
        # 停泊集读不到 ⇒ 不引入误停泊（fail-open 到义务判定；
        #义务判定本身 fail-closed，整体仍保守）。
        log.debug("sole_advancer_waits_query_failed",
                  project_id=project_id, error=str(e))

    # 在干活的不是「下一个该谁动」的对象。
    try:
        from hiveweave.agents.supervisor import agent_manager

        processing = {
            aid for aid, pid in agent_manager.list_processing()
            if pid == project_id
        }
    except Exception:
        processing = set()

    task_svc = TaskService()
    inbox_svc = InboxService()
    candidates: list[dict] = []
    for a in agents:
        aid = a.get("id") or ""
        if not aid or aid in exclude_agent_ids or aid in processing:
            continue
        if aid in parked:
            continue
        obligations: list = []
        try:
            obligations = (
                await task_svc.get_open_work_obligations(project_id, aid)
                or []
            )
        except Exception as e:
            log.debug("sole_advancer_obligations_failed",
                      agent_id=aid, error=str(e))
            obligations = [{"id": "_fail_closed"}]  # fail-closed：按有债
        ask_senders: set = set()
        if not obligations:
            try:
                ask_senders = (
                    await inbox_svc.get_outstanding_ask_senders(aid) or set()
                )
            except Exception as e:
                log.debug("sole_advancer_asks_failed",
                          agent_id=aid, error=str(e))
                ask_senders = {"_fail_closed"}  # fail-closed：按有债
        if obligations or ask_senders:
            candidates.append({
                "agent_id": aid,
                "name": a.get("name") or "",
                "role": a.get("role") or "",
                "obligations": obligations,
                "ask_senders": ask_senders,
            })
        if len(candidates) > 1:
            return None  # 不唯一，提前收场（省后续 per-agent 查询）

    return candidates[0] if len(candidates) == 1 else None


async def sole_advancer_cached(
    project_id: str, *, ttl_s: float = SOLE_ADVANCER_CACHE_TTL_S
) -> dict | None:
    """带短 TTL 缓存的 ``compute_sole_advancer``（relay 批量路径用）。"""
    now = time.monotonic()
    hit = _sole_cache.get(project_id)
    if hit is not None and (now - hit[0]) < ttl_s:
        return hit[1]
    sole = await compute_sole_advancer(project_id)
    _sole_cache[project_id] = (now, sole)
    return sole


async def _wake_budget_allows(
    project_id: str, agent_id: str, now_ms: int
) -> bool:
    """连唤预算（DSH ``maxConsecutiveWakes`` 同型）。

    同一 agent 连唤达上限后拒发，除非 ``last_active_at`` 已推进过上次
    唤醒时刻（真实产出 ⇒ 计数重置）或预算窗口过期。
    """
    from hiveweave.services.task import _query

    key = (project_id, agent_id)
    entry = _wake_budget.get(key)
    if entry and now_ms - int(entry["last_wake_ms"]) > IDLE_WAKE_BUDGET_WINDOW_MS:
        entry = None  # 窗口过期，计数作废
    if entry and int(entry["count"]) >= IDLE_WAKE_MAX_CONSECUTIVE:
        last_active = 0
        try:
            rows = await _query(
                project_id,
                "SELECT last_active_at FROM agents WHERE id = ?",
                [agent_id],
            )
            if rows:
                last_active = int(rows[0]["last_active_at"] or 0)
        except Exception as e:
            log.debug("wake_budget_last_active_failed",
                      agent_id=agent_id, error=str(e))
        if last_active <= int(entry["last_wake_ms"]):
            log.info(
                "idle_wake_budget_exhausted",
                project_id=project_id, agent_id=agent_id,
                consecutive_wakes=int(entry["count"]),
            )
            return False
        entry = None  # 真实产出推进 ⇒ 预算重置
    if entry is None:
        entry = {"count": 0, "last_wake_ms": 0}
    entry["count"] = int(entry["count"]) + 1
    entry["last_wake_ms"] = now_ms
    _wake_budget[key] = entry
    return True


async def maybe_wake_sole_advancer(
    project_id: str,
    *,
    exclude_agent_ids: frozenset[str] | set[str] = frozenset(),
    source: str = "run_completion",
) -> str | None:
    """评估并在存在唯一可推进者时直接唤醒；返回被唤醒的 agent_id。

    唤醒走既有 trigger 通道（coordinator 用 ``trigger_coordinator``，
    其余 ``trigger_subordinate``）；``force=True`` 与看门狗穿透唤醒同据
    —— 义务证据来自账本状态而非收件箱，未读守卫不得吞掉它。
    两道防循环闸（无待办不唤 / 连唤上限）见模块 docstring。
    """
    sole = await compute_sole_advancer(
        project_id, exclude_agent_ids=exclude_agent_ids
    )
    if not sole:
        return None
    aid = sole["agent_id"]
    now_ms = int(time.time() * 1000)
    if not await _wake_budget_allows(project_id, aid, now_ms):
        return None

    from hiveweave.agents.trigger import (
        is_coordinator,
        trigger_coordinator,
        trigger_subordinate,
    )

    if is_coordinator(sole.get("role")):
        await trigger_coordinator(aid, force=True)
    else:
        await trigger_subordinate(aid, force=True)
    log.info(
        "idle_wake_sole_advancer",
        project_id=project_id,
        agent_id=aid,
        role=sole.get("role") or "",
        source=source,
        obligations=len(sole.get("obligations") or []),
        ask_senders=len(sole.get("ask_senders") or ()),
    )
    return aid
