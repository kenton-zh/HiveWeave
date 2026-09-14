"""request_code_audit 三态信号契约（TEST_DSH_55 P0：假成功修复）。

## 缺陷（已取证，TEST_DSH_55）

``request_code_audit`` 被调用 34 次，其中 11 次（32%）结果文本是
「审计未执行: llm_failed. 审计上游失败，平台已排队自动重试（第 1 次）…」，
但 ``run_steps.status`` **34/34 全是 'completed'**。后果：agent 读到「成功」
却拿不到凭证（``attestation``），于是重发 11 次。文案里写着「无需循环重试」
说不服 —— 它看到的**信号**是成功。

## 本文件钉住的判据

一次调用的终局有三态，且三态必须能被**结构化信号**区分（不是文案）：
  ready（有凭证）/ accepted_pending（已受理·等待结果）/ failed。
以及最要紧的一条：``audited=False`` 的任何终局都**不得**报成成功。

借判据不借实现（deepseek-harness ``SubagentResult`` 三条）：
  ① 非 completed 的终止原因 ⇒ 不得当作成功上报；
  ② 诊断信息与产出分离（走 ``error`` + 结构化字段，不混进 ``output``）；
  ③ 未知终止原因按失败处理（fail-closed）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.tools.code_audit import (
    RequestCodeAuditParams,
    request_code_audit_tool,
)

AGENT_ID = "agent-signal"
PROJECT_ID = "proj-signal"


async def _call_tool(service_result: dict):
    """跑一次工具壳，``run_code_audit`` 的返回由 ``service_result`` 决定。"""
    run_mock = AsyncMock(return_value=service_result)
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch("hiveweave.services.code_audit.run_code_audit", new=run_mock),
    ):
        return await request_code_audit_tool(
            RequestCodeAuditParams(task_id="t-1"), AGENT_ID, r"C:\fake\wt"
        )


def _ledger_status(result_dict: dict) -> str:
    """复刻 ``agents/streaming.py:323`` 的落库派生规则（逐字）。

    ``status="completed" if result.get("success") else "failed"``
    —— 这就是缺陷的传导路径：``success=True`` ⇒ run_steps 'completed'。
    """
    return "completed" if result_dict.get("success") else "failed"


# ── ① 缺陷本体：已受理·等待结果不得报成功 ───────────────────────


@pytest.mark.asyncio
async def test_accepted_pending_is_not_reported_as_success():
    """★ 阳性对照锚点：11/34 的那条（llm_failed 已入队）不得是 success。"""
    result = await _call_tool(
        {
            "audited": False,
            "reason": "llm_failed",
            "audit_upstream_unavailable": True,
            "retry_queued": True,
            "retry_attempts": 1,
            "retry_exhausted": False,
        }
    )

    assert result.success is False, (
        "已受理·等待结果被报成成功 —— 这正是 TEST_DSH_55 的缺陷"
        "（agent 拿不到凭证却收到「成功」信号 ⇒ 重发）"
    )
    assert result.extra["audit_state"] == "accepted_pending"


@pytest.mark.asyncio
async def test_accepted_pending_ledger_status_is_not_completed():
    """★ 平台侧信号：run_steps.status 不得再是 'completed'。"""
    result = await _call_tool(
        {
            "audited": False,
            "reason": "llm_failed",
            "audit_upstream_unavailable": True,
            "retry_queued": True,
            "retry_attempts": 1,
            "retry_exhausted": False,
        }
    )

    assert _ledger_status(result.to_dict()) == "failed"


# ── ② 第三态靠结构化信号，不靠文案 ─────────────────────────────


@pytest.mark.asyncio
async def test_accepted_pending_signals_wait_do_not_resend():
    """第三态必须自证「等通知，不要重发」：blocked + fact + 结果契约字段。

    判据来源（不抄字段名，只借判据）：
      - ①非成功：``success is False``
      - ②诊断与产出分离：诊断在 ``error``，``output`` 为空
      - ③fail-closed：fact 显式落在四格词表内（此处 ``outcome_unknown``
        = 「结果未知 · 平台已记录 · 不许盲目重试」）
    """
    result = await _call_tool(
        {
            "audited": False,
            "reason": "llm_failed",
            "audit_upstream_unavailable": True,
            "retry_queued": True,
            "retry_attempts": 2,
            "retry_exhausted": False,
        }
    )

    d = result.to_dict()
    # ③ fail-closed：fact 落在词表内，且与 blocked 的合法组合
    assert d["fact"] == "outcome_unknown"
    assert d["blocked"] is True
    # 「平台已接管」的结构化信号（门禁/UI/消费者据此判「该等不该重发」）
    assert d["audit_state"] == "accepted_pending"
    assert d["wait_for_notice"] is True
    assert d["action_required"] is False
    assert d["retry_attempts"] == 2
    # ② 诊断与产出分离：产出为空，诊断走 error
    assert d["output"] == ""
    assert "已排队自动重试" in (d["error"] or "")
    assert "无需循环重试" in (d["error"] or "")


@pytest.mark.asyncio
async def test_failed_attempted_but_not_queued_is_failed_state():
    """未入队（平台未接管）的 llm_failed ⇒ failed 态，与第三态结构可分。"""
    result = await _call_tool({"audited": False, "reason": "llm_failed"})

    d = result.to_dict()
    assert d["success"] is False
    assert d["audit_state"] == "failed"
    assert d["action_required"] is True
    assert d["wait_for_notice"] is False
    assert d["blocked"] is False
    assert d["fact"] == "runner_failed"  # 从未执行 ⇒ 平台前提缺失
    assert _ledger_status(d) == "failed"


@pytest.mark.asyncio
async def test_three_states_are_pairwise_distinguishable():
    """三态两两结构可分（同轴可读）——证明「第三态」不是文案幻觉。"""
    ready = await _call_tool(
        {
            "audited": True,
            "verdict": "PASS",
            "lines_audited": 3,
            "attestation_id": "att-1",
        }
    )
    pending = await _call_tool(
        {
            "audited": False,
            "reason": "llm_failed",
            "retry_queued": True,
            "retry_attempts": 1,
            "retry_exhausted": False,
        }
    )
    failed = await _call_tool({"audited": False, "reason": "no_worktree"})

    signatures = [
        (r.success, r.to_dict().get("fact"), r.to_dict().get("blocked"),
         r.to_dict().get("audit_state"))
        for r in (ready, pending, failed)
    ]
    assert signatures == [
        (True, None, False, "ready"),
        (False, "outcome_unknown", True, "accepted_pending"),
        (False, "runner_failed", False, "failed"),
    ]


# ── ③ 全类收口：任何未审计终局都不得报成功（fail-closed）───────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service_result",
    [
        {"audited": False, "reason": "no_worktree"},
        {"audited": False, "reason": "no_callback"},
        {"audited": False, "reason": "no_model"},
        {"audited": False, "reason": "llm_failed"},
        {"audited": False, "reason": "error"},
        # 重试耗尽：平台不再接管 ⇒ 终局失败（不是「已受理」）
        {
            "audited": False,
            "reason": "llm_failed",
            "retry_queued": True,
            "retry_attempts": 5,
            "retry_exhausted": True,
        },
    ],
)
async def test_no_unaudited_outcome_is_ever_success(service_result):
    result = await _call_tool(service_result)

    d = result.to_dict()
    assert d["success"] is False
    assert _ledger_status(d) == "failed"
    # 收口处必须能自己答出「我属于四格中的哪一格」
    assert d["fact"] in ("runner_failed", "command_failed", "bad_args", "outcome_unknown")
    assert d["audit_state"] == "failed"


@pytest.mark.asyncio
async def test_internal_error_reason_uses_outcome_unknown_not_runner_failed():
    """兜底 except 触发点在执行之后 ⇒ 不得标 runner_failed（会诱发副作用双发）。"""
    result = await _call_tool({"audited": False, "reason": "error"})

    d = result.to_dict()
    assert d["fact"] == "outcome_unknown"
    assert d["runner_failed"] is False
    assert d["command_failed"] is False


# ── ④ 与平台收口函数（fact_positions）相容：不被兜底覆盖 ────────


@pytest.mark.asyncio
async def test_finalize_tool_result_keeps_declared_fact():
    """经唯一收口 ``finalize_tool_result`` 后 fact 不被兜底改写。

    ``fact_positions.assert_fact_complete`` 对「失败且无 fact」会兜底成
    ``outcome_unknown`` 并记 ERROR 日志。此处 failed 态声明的是
    ``runner_failed``：若本工具的 fact 丢失，收口会把它改成
    ``outcome_unknown`` ⇒ 这条断言转红。
    """
    from hiveweave.tools.fact_positions import finalize_tool_result

    result = await _call_tool({"audited": False, "reason": "no_callback"})
    out = finalize_tool_result("request_code_audit", result)

    assert out["success"] is False
    assert out["fact"] == "runner_failed"
    assert out["audit_state"] == "failed"
    assert out["blocked"] is False


@pytest.mark.asyncio
async def test_finalize_tool_result_keeps_accepted_pending_blocked():
    """第三态经收口后 blocked / fact 组合仍合法（blocked 需要平台侧事实位）。"""
    from hiveweave.tools.fact_positions import finalize_tool_result

    result = await _call_tool(
        {
            "audited": False,
            "reason": "llm_failed",
            "retry_queued": True,
            "retry_attempts": 1,
            "retry_exhausted": False,
        }
    )
    out = finalize_tool_result("request_code_audit", result)

    assert out["blocked"] is True
    assert out["fact"] == "outcome_unknown"
    assert out["audit_state"] == "accepted_pending"
