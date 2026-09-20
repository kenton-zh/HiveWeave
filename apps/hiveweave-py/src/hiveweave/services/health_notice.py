"""平台健康提示的**独立投递通道**（批次 4 附项，2026-09-11）。

## 为什么必须独立（病因）

三类提示此前全部 `+=` 拼进 `result["error"]`：

| 提示 | 原拼接点 |
| --- | --- |
| `[SELF REPEAT #N]` | `tools/executor.py` `_f10_result_hooks` |
| `[REPEAT REJECTION #N]` | `services/rejection_memory.py` → `tasks/submit.py` / `tools/patch.py` / `tools/turn_tools.py` |
| `[shared fix]` | `services/failure_signature.py::known_signature_hint` → 同上 |

两个后果：

1. **工具回执对「工具返回了什么」撒谎** —— `error` 字段本该是工具的真实
   报错。DSH 归档设计笔记 `2026-07-08-repeat-tool-guard.md:58` 明确否决这种
   做法（"it makes the logged tool/result lie about what the tool returned"）。
   日志里看到的 error 已经不再是错误本身，取证时**无法区分**「这是真错误」
   与「这是平台附加的提醒」。
2. **提醒被习得性跳读** —— 提示与真错误同格同色，模型对 error 字段的阅读
   是"扫一眼找 RETRY 标记"，附加段落越长越被略过。这正是 R7 恶化项
   （50:17 / 51:11）的机制：**广播有了，但没人读**。

## 怎么独立（判据三问，见 fixplan §5 的 code-review 门槛）

① **绑定的是操作身份还是"工具/时机"？** —— 本模块只做**投递**，身份由
   调用方决定（`[SELF REPEAT]`/`[REPEAT REJECTION]` 绑 agent+签名；
   组织级升级绑 `distinct_hitters` 集合）。投递层不重新发明身份。
② **投递在独立通道还是寄生在别人输出里？** —— 走 inbox 的
   **platform-reserved** 消息类型（`PLATFORM_NOTICE_MESSAGE_TYPE`），
   发送侧强制 `trusted_platform=True`。模型在**自己的下一轮**看到它，
   与工具回执物理分离：回执干净、提示可归属、可从 inbox 表重放。
③ **越界时静默、阻塞、还是梯度升级？** —— **不阻塞**。投递失败只记日志
   （`best-effort`），绝不影响工具执行；升级节奏由调用方的梯度决定
   （`[3,5,8]`，越顶静默）。

## 为什么落到 inbox 而不是"往上下文塞一段"

inbox 消息是**已落库、有 id、可从 DB 重放**的实体；往上下文塞字符串则是
瞬时状态，重启即失、无法审计。三问 ② 要的是"可从日志重放"，而 inbox 表
本身就是那份日志。

## ⚠️ 已知边界

`send_message` 的平台保留身份由 `wake_policy.is_platform_reserved_inbox_identity`
判定，所以**非 inbox 路径**（例如未来的 direct-message 通道）不受此保护 ——
新增通道时必须把判定接进该通道的写入侧，否则保留类型形同虚设。
"""

from __future__ import annotations

import time
from collections import deque

import structlog

log = structlog.get_logger(__name__)

#: 提示种类标记 —— 写进消息正文前缀，供模型与取证脚本按类型分流。
KIND_SELF_REPEAT = "SELF_REPEAT"
KIND_REPEAT_REJECTION = "REPEAT_REJECTION"
KIND_ORG_ESCALATION = "ORG_ESCALATION"

#: 单条提示长度上限（inbox 正文不该被提示撑爆；超出截断并留痕）。
_MAX_BODY_CHARS = 2000


# ── 上游死亡通知去重（TEST_DSH_63 批3 组4，2026-09-19）────────────────
# 病：一次 30 秒上游抖动窗连杀 6+ run，零通知 —— 但反过来「每次死亡都
# 告诉 CEO」也会在抖动窗里刷屏。药：进程内滑动窗（300s）内第 1 次死亡只
# 记数，≥2 次 → 给 CEO 发**一条**汇总；窗口内只发一条。
# 与 wait_contract 的 durable 重醒不同，这里是**观测面**：进程内即忘
# （重启清零可接受——通知不是恢复机制，重醒才是）。


async def deliver_notice(
    agent_id: str,
    text: str,
    *,
    kind: str,
    project_id: str | None = None,
    wake: bool = True,
) -> bool:
    """把平台提示投递到该 agent 的**独立 inbox 通道**。

    ``text`` 是提示正文（不含前缀）。返回是否投递成功 —— 调用方**不应**
    因 False 改变任何行为（best-effort，见模块 docstring 三问 ③）。

    ``wake=True``（默认）让提示能启动一次 LLM turn：提示的价值在于被读到，
    静默落库等于没发。但**调用方在 turn 内**（工具执行中）投递时可能希望
    只落库、由当前 turn 的下一轮自然看到 —— 那种场景传 ``wake=False``。
    """
    body = (text or "").strip()
    if not body:
        return False
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "…（已截断）"
    message = f"[{kind}] {body}"
    try:
        from hiveweave.services.inbox import InboxService
        from hiveweave.services.wake_policy import PLATFORM_NOTICE_MESSAGE_TYPE

        await InboxService().send_message(
            from_agent_id="system",
            to_agent_id=agent_id,
            message=message,
            message_type=PLATFORM_NOTICE_MESSAGE_TYPE,
            wake=wake,
            trusted_platform=True,
        )
        log.info(
            "health_notice.delivered",
            agent_id=(agent_id or "")[:12],
            kind=kind,
            project_id=project_id,
            chars=len(body),
        )
        return True
    except Exception as e:  # noqa: BLE001 — 投递 best-effort，绝不影响工具执行
        log.warning(
            "health_notice.delivery_failed",
            agent_id=(agent_id or "")[:12],
            kind=kind,
            error=str(e),
        )
        return False


def combine_pending_text(*parts: str | None) -> str:
    """把多条提示正文合并成一条（多类提示同一轮命中时只发一条，防刷屏）。

    空/None 段被丢弃；全空返回 ""（调用方据此跳过投递）。合并顺序即传入
    顺序 —— 调用方按"越紧急越靠前"排列。
    """
    kept = [(p or "").strip() for p in parts]
    kept = [p for p in kept if p]
    return "\n\n".join(kept)


# ── 上游死亡窗口去重 + 重醒耗尽升级（组4）────────────────────────────

UPSTREAM_DEATH_WINDOW_S = 300
"""死亡滑动窗：窗内第 1 次只记数，≥2 次 → 一条汇总；窗口发过即不再发。"""

_UPSTREAM_EXHAUSTED_DEDUP_S = 30 * 60
"""重醒耗尽升级的进程内去重（每 agent 30min 至多一条；不受死亡窗限制）。"""

# project_id → [(monotonic, agent_id, run_id), ...]
_upstream_death_events: dict[str, deque[tuple[float, str, str]]] = {}
# project_id → 上次窗内汇总发送时刻（monotonic）
_upstream_death_notice_sent: dict[str, float] = {}
# "project_id:agent_id" → 上次耗尽升级发送时刻（monotonic）
_upstream_exhausted_notice_sent: dict[str, float] = {}


def reset_upstream_notice_state_for_tests() -> None:
    """清空进程内去重状态（仅供测试）。"""
    _upstream_death_events.clear()
    _upstream_death_notice_sent.clear()
    _upstream_exhausted_notice_sent.clear()


async def _project_ceo_id(project_id: str | None) -> str | None:
    """项目 CEO（root）agent id。role='ceo' 优先，无父者兜底；fail-open None。"""
    if not project_id:
        return None
    try:
        from hiveweave.services.org import OrgService

        agents = await OrgService().list_agents(project_id) or []
    except Exception as e:  # noqa: BLE001 — 通知 best-effort
        log.debug("health_notice.ceo_lookup_failed", error=str(e))
        return None
    for a in agents:
        if str(a.get("role") or "").lower() == "ceo" and a.get("id"):
            return str(a["id"])
    for a in agents:
        if not a.get("parent_id") and a.get("id"):
            return str(a["id"])
    return None


async def notify_upstream_deaths(
    project_id: str | None,
    agent_id: str,
    run_id: str = "",
) -> bool:
    """上游死亡进滑动窗；窗内 ≥2 次且未发过 → 给 CEO 发**一条**汇总。

    返回是否实际发送（调用方不应因 False 改变行为）。第 1 次死亡只记数，
    不打扰任何人 —— 单次抖动自愈是常态（durable 重醒在 wait_contract 侧
    负责），刷屏只会训练 CEO 跳读。
    """
    key = project_id or "?"
    now = time.monotonic()
    dq = _upstream_death_events.setdefault(key, deque())
    dq.append((now, str(agent_id or ""), str(run_id or "")[:8]))
    while dq and now - dq[0][0] > UPSTREAM_DEATH_WINDOW_S:
        dq.popleft()
    if len(dq) < 2:
        return False
    if now - _upstream_death_notice_sent.get(key, float("-inf")) < UPSTREAM_DEATH_WINDOW_S:
        return False  # 本窗口已发过一条 —— 只记数
    seen: set[str] = set()
    agents: list[str] = []
    for _ts, aid, _rid in dq:
        if aid and aid not in seen:
            seen.add(aid)
            agents.append(aid)
    ceo = await _project_ceo_id(project_id)
    if not ceo:
        log.warning(
            "health_notice.upstream_death_no_ceo",
            project_id=project_id,
            deaths=len(dq),
        )
        return False
    text = (
        f"上游抖动窗内检测到 {len(dq)} 次 run 死亡"
        f"（agent：{', '.join(a[:12] for a in agents) or '未知'}）。"
        "各 agent 已按退避（60s/180s/600s，封顶 3 次）排定 durable 自动重醒，"
        "任务与上下文不变；本窗口内只汇总此一条。"
        f"最近 run：{(run_id or '')[:8] or 'n/a'}。"
    )
    # 先占窗再投递：并发死亡（DSH_63 正是 30s 窗连杀多个 run）在 deliver
    # await 期间再进本口时，必须看到「已发」而不是把汇总再发一遍；
    # 投递失败则回滚占位，让窗口内的下一次死亡重试。
    _upstream_death_notice_sent[key] = now
    sent = await deliver_notice(
        ceo, text, kind=KIND_ORG_ESCALATION, project_id=project_id, wake=True
    )
    if sent:
        dq.clear()  # 本窗收口 —— 下一次抖动从新窗起算
        log.warning(
            "health_notice.upstream_deaths_summarized",
            project_id=project_id,
            deaths_in_window=len(agents),
            agents=[a[:12] for a in agents],
        )
    else:
        _upstream_death_notice_sent.pop(key, None)
    return sent


async def notify_upstream_recovery_exhausted(
    project_id: str | None,
    agent_id: str,
    attempts: int = 3,
) -> bool:
    """自动重醒耗尽仍死 → 停止重醒，交人工（**不受**死亡窗限制，单独发）。

    进程内每 (project, agent) 30min 至多一条 —— 耗尽后的每一次死亡都会
    重进本口，不去重会给 CEO 刷近重复消息。
    """
    key = f"{project_id or '?'}:{agent_id or '?'}"
    now = time.monotonic()
    if now - _upstream_exhausted_notice_sent.get(key, float("-inf")) < (
        _UPSTREAM_EXHAUSTED_DEDUP_S
    ):
        return False
    ceo = await _project_ceo_id(project_id)
    if not ceo:
        log.warning(
            "health_notice.upstream_exhausted_no_ceo",
            project_id=project_id,
            agent_id=(agent_id or "")[:12],
        )
        return False
    text = (
        f"agent {str(agent_id or '')[:12]} 的上游死亡自动重醒已达上限"
        f"（{attempts} 次：60s/180s/600s 均未能恢复），已停止自动重醒。"
        "任务仍 claimed、上下文完整，需人工介入：检查模型区域可用性 / "
        "更换模型或密钥后手动唤醒该 agent。"
    )
    # 先占去重位再投递（同 notify_upstream_deaths 的并发占位理由）；
    # 失败回滚，让下一次耗尽重试。
    _upstream_exhausted_notice_sent[key] = now
    sent = await deliver_notice(
        ceo, text, kind=KIND_ORG_ESCALATION, project_id=project_id, wake=True
    )
    if sent:
        log.warning(
            "health_notice.upstream_recovery_exhausted",
            project_id=project_id,
            agent_id=(agent_id or "")[:12],
            attempts=attempts,
        )
    else:
        _upstream_exhausted_notice_sent.pop(key, None)
    return sent
