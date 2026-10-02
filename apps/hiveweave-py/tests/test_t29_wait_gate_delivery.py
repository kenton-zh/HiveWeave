"""T29：WAIT_WITHOUT_ASK 按实际送达判定 + [TASK ADVANCE] 上报出口（S16 回归）.

S16 事故锚点（2026-09，原编号 T23/T29）:
  - 门禁判据缺口 ①：30 分钟送达证据查询嵌在 ``if turn_started_ms:``
    内 —— current_job 缺 started_at 时整份送达证据被丢成空集，
    send_message 确认送达仍被 WAIT_WITHOUT_ASK 误拒（×4）。
  - 门禁判据缺口 ②：送达证据只按「pending 发件人恰好出现」补花名，
    不补 short_id、不覆盖一般收件人 —— 模型按回执可见形态（短号/花名）
    写 waiting_on ref 时，inbox 落库的 UUID 证据匹配不上 ⇒ 真送达被拒。
  - [TASK ADVANCE] 对卡死任务只有「自己 running」一条路，无上报出口。

修复面（本文件钉死）:
  - agents/completion.py::gather_exit_gate_facts —— 30 分钟窗口无条件查；
    送达证据按 id+花名+short_id 三形态富化（口径对齐 commit_turn 预检的
    enrich_id_set）。
  - hooks/handlers/task_advance.py::build_task_advance_hint —— 含
    dispatch 拆分 / send_message 向上级求助的出口文案。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.agents.completion import gather_exit_gate_facts
from hiveweave.hooks.handlers.task_advance import build_task_advance_hint
from hiveweave.services.turn_exit import evaluate_turn_exit
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)

UUID_MUYUN = "5f0e3c2a-1111-4222-8333-444455556666"


def _fake_agent() -> AsyncMock:
    agent = AsyncMock()
    agent.id = "asker-agent"
    agent.project_id = "proj-t29"
    agent.pending_inbox_msg_ids = []
    agent.current_job = {}  # ⚠ 缺 started_at —— 缺口①的触发形态
    agent._task_ids_advanced_this_turn = lambda tc: set()
    agent._task_ids_gate_resolved_this_turn = lambda tc: set()
    inbox = AsyncMock()
    inbox.get_pending_messages = AsyncMock(return_value=[])
    inbox.get_sent_recipients_since = AsyncMock(return_value={UUID_MUYUN})
    inbox.get_outstanding_ask_recipients = AsyncMock(return_value=set())
    inbox.get_replied_contracts_since = AsyncMock(return_value=set())
    agent._inbox = inbox
    return agent


@pytest.mark.asyncio
async def test_delivery_evidence_survives_missing_started_at():
    """缺口①：current_job 缺 started_at ⇒ 30 分钟送达证据仍要查。"""
    agent = _fake_agent()

    async def fake_meta_agent(aid: str):
        if aid == UUID_MUYUN:
            return {"id": UUID_MUYUN, "name": "牧云", "short_id": "A149",
                    "status": "active"}
        return None

    with patch("hiveweave.db.meta.get_agent_by_id", fake_meta_agent):
        facts = await gather_exit_gate_facts(agent, [])
    assert agent._inbox.get_sent_recipients_since.await_count >= 1, (
        "30 分钟送达证据查询被 started_at 门吞掉（缺口①复现）"
    )
    # 缺口②：UUID 证据必须富化出花名 + 短号（模型按回执形态写 ref）
    assert UUID_MUYUN in facts.messaged_refs
    assert "牧云" in facts.messaged_refs
    assert "A149" in facts.messaged_refs


@pytest.mark.asyncio
async def test_delivered_message_satisfies_wait_gate_by_alias():
    """端到端口径：真送达（inbox 落库）后，waiting_on 用短号/花名引用
    对方 ⇒ 门必须放行（不再 WAIT_WITHOUT_ASK）。"""
    agent = _fake_agent()

    async def fake_meta_agent(aid: str):
        if aid == UUID_MUYUN:
            return {"id": UUID_MUYUN, "name": "牧云", "short_id": "A149",
                    "status": "active"}
        return None

    with patch("hiveweave.db.meta.get_agent_by_id", fake_meta_agent):
        facts = await gather_exit_gate_facts(agent, [])
    ctx = facts.to_context()
    try:
        for ref in ("A149", "牧云", UUID_MUYUN):
            set_pending_turn_result(
                agent.id,
                {
                    "phase": "waiting",
                    "summary": "等牧云对二选一决策的回复",
                    "waiting_on": [{"kind": "agent", "ref": ref}],
                },
            )
            decision = evaluate_turn_exit(ctx)
            assert "WAIT_WITHOUT_ASK" not in decision.violations, (
                f"ref={ref} 真送达仍被拒：{decision.violations}"
            )
    finally:
        clear_pending_turn_result(agent.id)


def test_task_advance_hint_has_escalation_exit():
    """T29-②：[TASK ADVANCE] 必须带上报出口（dispatch 拆分 / 向上级求助）。"""
    hint = build_task_advance_hint([
        {"id": "task-1", "title": "卡死的任务", "status": "claimed",
         "role_hint": "assignee", "progress": 0},
    ])
    assert "[TASK ADVANCE]" in hint
    assert "dispatch" in hint
    assert "send_message" in hint
    assert "向上级" in hint
    assert "阻塞点" in hint
