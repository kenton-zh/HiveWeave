"""T16 一期：auto 回合出口模式（HIVEWEAVE_TURN_EXIT_MODE=auto）的程序化收口。

终局形态（MERGED-2026-09-28 研究报告 §3.1 T16 行，用户 09-30 钦定）：
commit_turn 从模型工具面下架 —— 模型停止调用工具（=自认干完/无事可做）时
平台程序化自动收口。本模块承载 auto 语义的三块：

1. ``build_auto_exit_gate_hint`` —— tool_loop ``exit_gate_check`` 接缝的
   auto 实现。复用既有义务结算判定（``gather_exit_gate_facts`` +
   ``evaluate_turn_exit``，本体零改动，只换调用时机为平台发起）：
   - 未结算 ⇒ 注入 hint 循环继续（hint 次数策略：第 1-2 次常规 hint；
     第 3 次起升级（notice 给上级 + 「列出阻塞点」最小指令）；
     第 5 次强制挂起 blocked —— 替换 legacy ``_TURN_GATE_MAX=1``）。
   - 全部结算 ⇒ 返回 None（loop 正常退出，handle_completion 落平台 commit）。
2. ``ensure_platform_turn_result`` —— 平台生成 commit：summary=末段
   assistant 文本（≤200 字符，空则最后工具结果摘要），phase 平台推断
   （wait 契约=waiting，否则 done_slice），落既有 pending TurnResult 结构。
3. 升级通知（``notify_superior_gate_escalation``）—— 3 连停 notice 给上级。

legacy 模式完全不进入本模块（``agents/agent.py`` 的接缝按模式分发）。
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

# 补步 hint 次数策略（T16 钦定，替换 legacy _TURN_GATE_MAX=1 语义）：
#   自然停 #1、#2 → 常规 hint；
#   自然停 #3、#4 → 升级：notice 给上级 + 「列出阻塞点」最小指令；
#   自然停 #5     → 强制挂起 blocked + 通知 creator/CEO（不再注入 hint）。
AUTO_GATE_HINT_MAX = 4
AUTO_GATE_ESCALATE_FROM = 3

# 平台合成 summary 的长度上限（与 build_run_result_summary 同口径）。
_PLATFORM_SUMMARY_MAX = 200

#: auto 版违规 → 动作映射。刻意不复用 turn_exit.GATE_ACTIONS —— 那份处方
#: 面向 commit_turn（auto 已下架），照抄会让 hint 指挥模型调不存在的工具。
_AUTO_VIOLATION_ACTIONS: dict[str, str] = {
    "MISSING_COMMIT_TURN": "直接停止输出即可（平台会自动收口）",
    "INVALID_TURN_RESULT": "直接停止输出即可（平台会自动收口）",
    "WAITING_ON_REQUIRED": (
        'wait_on(refs=[{"kind": "task|agent|fact|user", "ref": "<id>"}]) 登记等待后停止'
    ),
    "BLOCKED_WAITING_ON_REQUIRED": (
        'wait_on(refs=[{"kind": "task|agent|fact|user", "ref": "<id>"}]) 登记阻塞后停止'
    ),
    "UNREPLIED_ASKS": "用 send_message/ask_agent 回复对方（带 replyTo）",
    "WAIT_WITHOUT_ASK": (
        "先 ask_agent/send_message 对方；或改 wait_on 挂任务/事实"
    ),
    "HIRE_UNREPORTED": "向招人请求方 send_message/notify_agent 汇报结果",
    "OPEN_TASKS_UNDECLARED": "推进任务，或 wait_on 合法挂起；不要假装完成",
    "ASSIGNEE_MUST_SUBMIT": (
        "自己在执行 ⇒ submit_task(taskId)；已派发下属 ⇒ "
        'wait_on(refs=[{"kind": "task", "ref": "<子任务id>"}]) 后停止'
    ),
    "REVIEWER_MUST_START_REVIEW": (
        "review_task(taskId) 开始审查；确需等待则 wait_on 该任务后停止"
    ),
    "REVIEWER_MUST_FINISH_REVIEW": "review_task(taskId, decision=...) 完成审查",
    "CREATOR_MUST_REVIEW": "review_task(taskId) 完成审查",
    "CREATOR_MUST_MERGE": "git_worktree_merge(branchName=...) 合并",
    "UNCOMMITTED_WORKTREE": "git_worktree_checkpoint 提交工作树后停止",
    "CEO_PROJECT_PENDING": (
        "推进：审查 submitted / 收口 VERIFY / 给待命叶子派活；"
        "确需等待则 wait_on 声明等待对象"
    ),
}


def _truncate_tail(text: str, max_chars: int = _PLATFORM_SUMMARY_MAX) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_chars:
        return text
    return "…" + text[-(max_chars - 1):]


def synthesize_commit_summary(
    content: str, tool_turn_messages: list | None = None
) -> str:
    """平台 commit 的 summary：末段 assistant 文本；空则最后工具结果摘要。

    末段口径复用 ``build_run_result_summary``（取最后一条非平台尾注旁白，
    尾部截断保留末尾语义）；纯平台尾注/无文本时回退**最后一条 tool 结果**
    摘要（同为 ≤200 字符）。仍为空返回空串 —— 调用方负责非空兜底
    （TurnResult.summary 有 min_length=1 硬校验）。
    """
    from hiveweave.agents.completion import build_run_result_summary

    summary = build_run_result_summary(content or "")
    if summary:
        return _truncate_tail(summary)
    for m in reversed(tool_turn_messages or []):
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        text = str(m.get("content") or "").strip()
        if text:
            return _truncate_tail(text)
    return ""


async def infer_platform_phase(agent: Any) -> tuple[str, list[dict[str, Any]]]:
    """phase 平台推断：wait 契约（agent_waits 活跃行）⇒ waiting；否则 done_slice。

    waiting_on 从 wait 契约行还原（kind/ref/note），使既有
    ``handle_completion`` 的 P1 持久化路径（phase=waiting + waiting_on ⇒
    ``replace_waits``）与唤醒链原样工作。
    """
    from hiveweave.services.wait_contract import wait_contract_service

    try:
        waits = await wait_contract_service.list_active(
            agent.project_id, agent.id
        )
    except Exception as e:
        log.debug(
            "t16_platform_phase_waits_lookup_failed",
            agent_id=getattr(agent, "id", "?"),
            error=str(e),
        )
        waits = []
    waiting_on: list[dict[str, Any]] = []
    for w in waits or []:
        ref = str(w.get("ref") or "").strip()
        if not ref:
            continue  # 空 ref 行不可唤醒，不进合成 TurnResult
        waiting_on.append(
            {
                "kind": str(w.get("kind") or "external"),
                "ref": ref,
                **({"note": str(w["note"])} if w.get("note") else {}),
            }
        )
    if waiting_on:
        return "waiting", waiting_on
    return "done_slice", []


async def ensure_platform_turn_result(
    agent: Any,
    content: str = "",
    tool_turn_messages: list | None = None,
    *,
    refine_summary: bool = False,
) -> dict[str, Any] | None:
    """平台生成 commit（幂等）：pending TurnResult 为空时按平台推断合成。

    - 模型已提交（hint 补步轮中幻觉调了 commit_turn 等）⇒ 不覆盖，返回 None。
    - ``refine_summary=True``（handle_completion 收口侧，拿得到最终文本）时，
      对**平台合成**的 pending 结果刷新 summary —— 门禁接缝侧只拿得到
      tool history，先用占位合成，收口侧以最终 assistant 文本为准。
    - 模型合成的 summary 为空 ⇒ "(no narration)" 兜底（schema 硬校验非空）。
    """
    from hiveweave.services.turn_result import TURN_RESULT_SCHEMA_VERSION
    from hiveweave.services.turn_session import (
        get_pending_turn_result,
        set_pending_turn_result,
    )

    existing = get_pending_turn_result(agent.id)
    if existing is not None:
        if not (
            refine_summary
            and isinstance(existing.get("extensions"), dict)
            and existing["extensions"].get("platform_commit")
        ):
            return None
        # 平台合成的结果由平台全权刷新 —— 不只 summary：模型在合成之后
        # （如被 hint 打回后的补步轮里）合法 wait_on 挂起时，wait 契约
        # 已登记，phase 必须随之重推断（done_slice → waiting），否则会把
        # 合法挂起误判成 done_slice 违规继续打回。
        refined = synthesize_commit_summary(content, tool_turn_messages)
        new_phase, new_waits = await infer_platform_phase(agent)
        changed = False
        if refined and refined != existing.get("summary"):
            existing["summary"] = refined
            changed = True
        if new_phase != existing.get("phase") or new_waits != existing.get(
            "waiting_on"
        ):
            existing["phase"] = new_phase
            existing["waiting_on"] = new_waits
            changed = True
        if changed:
            set_pending_turn_result(agent.id, existing)
            return existing
        return None

    phase, waiting_on = await infer_platform_phase(agent)
    summary = synthesize_commit_summary(content, tool_turn_messages)
    payload: dict[str, Any] = {
        "schema_version": TURN_RESULT_SCHEMA_VERSION,
        "phase": phase,
        "summary": summary or "(no narration)",
        "waiting_on": waiting_on,
        "result": {},
        "extensions": {"platform_commit": True},
    }
    set_pending_turn_result(agent.id, payload)
    log.info(
        "t16_platform_commit_synth",
        agent_id=agent.id,
        phase=phase,
        summary_len=len(payload["summary"]),
        waits=len(waiting_on),
    )
    return payload


def _build_auto_exit_hint(decision: Any, facts: Any) -> str:
    """常规 hint（停止 #1、#2）：违规清单 + auto 处方（无 commit_turn）。"""
    lines = [
        "[TURN EXIT] 平台判定：现在停止还不能收口 —— 有未结算义务。",
        "逐项处理下方义务；全部结算后**直接停止输出**（平台会自动生成收口）。",
        "合法等待他人/任务/事实 ⇒ wait_on(refs=[{\"kind\": \"task|agent|fact|user\","
        ' "ref": "<id>"}]) 后停止。不要尝试 commit_turn（本模式已下架）。',
    ]
    for v in decision.violations:
        lines.append(
            f"GATE={v} ACTION="
            f"{_AUTO_VIOLATION_ACTIONS.get(v, '处理后直接停止')}"
        )
    if "UNREPLIED_ASKS" in decision.violations and facts.unreplied_asks:
        for m in facts.unreplied_asks[:8]:
            name = m.get("from_name") or (m.get("from_agent_id") or "?")[:8]
            cid = (m.get("reply_contract_id") or "")[:12]
            lines.append(f"  ❌ {name}: replyTo={cid or '?'}")
    return "\n".join(lines)


def build_auto_block_hint(violations: list[str]) -> str:
    """auto 停泊/强挂通知的义务处方（qoder 上下文审计补漏）。

    病灶：handle_completion 的 TURN EXIT PARKED / GATE UNRESOLVED 通知在
    auto 下也直接嵌 ``exit_decision.hint`` —— 那是 legacy 门禁的
    commit_turn 处方墙（turn_exit.build_gate_repair_hint），auto 工具面
    根本没有该工具。本函数用同一份 ``_AUTO_VIOLATION_ACTIONS`` 处方表
    生成 auto 版通知体，杜绝陈旧教学进上下文。
    """
    lines = [
        "[TURN EXIT] 平台判定：当前不能收口——有未结算义务。",
        "逐项处理下方义务；全部结算后**直接停止输出**"
        "（平台自动收口；commit_turn 本模式已下架，不要尝试）。",
        "确需挂起：wait_on(refs=[{\"kind\": \"task|agent|fact|user\", "
        "\"ref\": \"<id>\"}]) 登记后停止。",
    ]
    for v in violations[:8]:
        lines.append(
            f"GATE={v} ACTION={_AUTO_VIOLATION_ACTIONS.get(v, '处理后直接停止')}"
        )
    return "\n".join(lines)


def _build_minimal_blocking_instruction(violations: list[str]) -> str:
    """升级 hint（停止 #3 起）：最小指令 —— 只列阻塞点，不许继续干活。"""
    return (
        "[TURN EXIT ESCALATED] 多次补步后仍未收口。不要再做任何其他工作。"
        "只回一段文字：逐条列出当前阻塞点（GATE 名称 + 涉及的 ref/id）"
        "以及各自缺什么；能立即结算的（一条回复/一次 submit）现在就做，"
        "然后停止输出。若平台再次判定未结算，将把你挂起为 blocked 并"
        "通知上级。当前未结算: "
        + ", ".join(violations[:6])
        + "。"
    )


async def notify_superior_gate_escalation(
    agent: Any, violations: list[str]
) -> None:
    """3 连停升级 notice 给上级（creator）。best-effort，不阻塞补步。

    上级缺失（CEO 无 parent）只留日志 —— CEO 的 5 连停强挂通知由
    recovery.escalate_gate_exhaustion 落 work_log/红框可见。
    """
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.inbox import InboxService

        me = await meta_db.get_agent_by_id(agent.id)
        parent_id = (me or {}).get("parent_id")
        name = ((me or {}).get("name") or agent.id[:8]) if me else agent.id[:8]
        if not parent_id:
            log.info(
                "t16_gate_escalation_no_parent",
                agent_id=agent.id,
                violations=violations[:6],
            )
            return
        await InboxService().send_message(
            "system",
            parent_id,
            (
                f"[TURN EXIT ESCALATION] 下属 {name} 在同一 run 内多次自然停止"
                f"仍无法收口（未结算: {', '.join(violations[:6])}）。"
                "请介入协调（派单/回复/解除阻塞）。"
            ),
            message_type="escalation",
            priority="urgent",
            wake=True,
        )
        from hiveweave.agents.trigger import trigger_subordinate

        await trigger_subordinate(parent_id)
        log.warning(
            "t16_gate_escalated_to_superior",
            agent_id=agent.id,
            parent_id=parent_id,
            violations=violations[:6],
        )
    except Exception as e:
        log.warning(
            "t16_gate_escalation_failed",
            agent_id=getattr(agent, "id", "?"),
            error=str(e),
        )


async def build_auto_exit_gate_hint(agent: Any, tool_calls: list) -> str | None:
    """auto 模式自然停出口判定（``tool_loop.exit_gate_check`` 接缝的 auto 实现）。

    返回 hint ⇒ 注入循环继续；返回 None ⇒ loop 正常退出
    （全部结算 ⇒ handle_completion 落平台 commit；5 连停 ⇒ 强制挂起标记，
    handle_completion 收口为 blocked 并通知 creator/CEO）。

    纯读 + 补步计数（``agent._turn_gate_count`` 复用 legacy 计数器，
    chat() 每 run 复位口径一致）；义务/门禁判定本体不改 —— 只是调用
    时机从模型发起（commit_turn）变为平台发起（自然停止）。
    """
    from hiveweave.agents.completion import gather_exit_gate_facts
    from hiveweave.services.turn_exit import evaluate_turn_exit
    from hiveweave.services.turn_session import set_force_exit_block

    # 平台先合成（幂等）：让 evaluate_turn_exit 在「平台推断 phase」上判定，
    # 而不是判 MISSING_COMMIT_TURN。合成失败按 None 继续（此时会命中
    # MISSING_COMMIT_TURN 修复项，hint 引导直接停止 —— 保守不炸）。
    # refine_summary=True 且本接缝无最终文本 ⇒ summary 不被空值冲掉，
    # 但 wait 契约变化（模型补步轮里合法 wait_on）会让 phase 随之重推断。
    try:
        await ensure_platform_turn_result(agent, refine_summary=True)
    except Exception as e:
        log.warning(
            "t16_auto_gate_synth_failed",
            agent_id=getattr(agent, "id", "?"),
            error=str(e),
        )

    try:
        facts = await gather_exit_gate_facts(agent, tool_calls)
    except Exception as e:
        log.warning(
            "t16_auto_gate_facts_failed",
            agent_id=getattr(agent, "id", "?"),
            error=str(e),
        )
        return None
    decision = evaluate_turn_exit(facts.to_context(), emit_telemetry=False)
    if decision.ok or not decision.should_repair:
        # 全部结算（或 park 类账实不符 ⇒ 交 handle_completion 权威停泊）
        return None

    count = int(getattr(agent, "_turn_gate_count", 0) or 0)
    if count >= AUTO_GATE_HINT_MAX:
        # 第 5 次自然停：不再 hint —— 标记强制挂起，loop 退出后由
        # handle_completion 收口 blocked + 通知 creator/CEO。
        set_force_exit_block(
            agent.id, {"violations": list(decision.violations)}
        )
        log.warning(
            "t16_auto_gate_force_block",
            agent_id=agent.id,
            violations=decision.violations,
            stops=count,
        )
        return None

    agent._turn_gate_count = count + 1
    if agent._turn_gate_count >= AUTO_GATE_ESCALATE_FROM:
        await notify_superior_gate_escalation(
            agent, list(decision.violations)
        )
        return _build_minimal_blocking_instruction(
            list(decision.violations)
        )
    try:
        from hiveweave.services.telemetry import telemetry

        telemetry.turn_exit_gate(
            agent.id,
            list(decision.violations),
            "supplement",
            gate_round=agent._turn_gate_count,
        )
    except Exception as e:  # noqa: BLE001 — 遥测 best-effort，hint 必须照常返回
        log.debug("turn_exit_gate_telemetry_failed", agent_id=agent.id, error=str(e))
    return _build_auto_exit_hint(decision, facts)
