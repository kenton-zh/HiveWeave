"""P1-7 (TEST_DSH_33): 收口期望前置下发。

现场：submit_task 13 次调用失败 6 次（46.2%），全部是「撞了才知道规则」——
deliveryContract 缺 summary/test（3 次）、attestation kind 不匹配（2 次）。
期望在 submit 侧本来就能算出来，缺的只是**提前**告诉 assignee。

契约（services/tasks/policy.py）：
- 推导复用 submit 侧同一份（ledger_policy_id → required_attestation_kinds、
  parse_delivery_contract → REQUIRED_REPLY_FIELDS），不新造判定
- 无任何硬性期望 → 空串（调用方据此不注入）
- 未知 policy fail-close 成哨兵 kind —— 哨兵**不得**当下发（会教 agent
  去找一个不存在的凭证），改为提示重设合法 gate
"""

from __future__ import annotations

import json

import pytest

from hiveweave.services.tasks.policy import (
    format_submit_expectations,
    submit_expectations,
)


def _task(policy_id: str | None = None, contract: dict | None = None) -> dict:
    t: dict = {"id": "t-1", "title": "实现 M-A 模块", "policy_id": policy_id}
    if contract is not None:
        t["contract_json"] = json.dumps(contract)
    return t


# ── 推导（机器可读形态）────────────────────────────────────


def test_expectations_none_task():
    exp = submit_expectations(None)
    assert exp == {
        "policy_id": None,
        "attestation_kinds": None,
        "delivery_contract_fields": [],
        "policy_unknown": False,
    }


def test_expectations_known_policy_with_kinds():
    exp = submit_expectations(_task(policy_id="generic_tests"))
    assert exp["attestation_kinds"] == ["test_run"]
    assert exp["policy_unknown"] is False
    assert exp["delivery_contract_fields"] == []


def test_expectations_soft_policy_has_no_kinds():
    """coordinator_review = 软策略（None）——不是未知，也不是强制。"""
    exp = submit_expectations(_task(policy_id="coordinator_review"))
    assert exp["attestation_kinds"] is None
    assert exp["policy_unknown"] is False


def test_expectations_unknown_policy_fail_close_without_sentinel():
    exp = submit_expectations(_task(policy_id="bogus_policy"))
    assert exp["policy_unknown"] is True
    # 哨兵 kind 不出现在下发面（教 agent 找不存在的凭证 = 二次浪费）
    assert exp["attestation_kinds"] is None


def test_expectations_with_delivery_contract_fields():
    exp = submit_expectations(
        _task(contract={"slice_type": "delivery_contract", "id": "dc-t1"})
    )
    assert exp["delivery_contract_fields"] == ["summary", "test"]


def test_expectations_non_delivery_contract_ignored():
    """协调者自建的其它 slice 契约不属于 preflight 干预范围。"""
    exp = submit_expectations(
        _task(contract={"slice_type": "feature_slice", "id": "fs-1"})
    )
    assert exp["delivery_contract_fields"] == []


def test_expectations_policy_from_tags_when_unset():
    """policy_id 空 → 从结构化 tags 推导（resolve_task_policy 只认
    tags，HARD RULE: 不从自由文本 title/description 猜意图）。"""
    exp = submit_expectations(
        {"title": "随便什么标题", "policy_id": "", "tags": ["ui_browser_e2e"]}
    )
    assert exp["policy_id"] == "ui_browser_e2e"
    assert exp["attestation_kinds"] == ["browse_e2e"]
    assert exp["policy_unknown"] is False


def test_expectations_policy_falls_back_to_coordinator_review():
    """无 tags 无 policy_id → 兜底软策略 coordinator_review（非未知）。"""
    exp = submit_expectations({"title": "实现 M-A 模块", "policy_id": ""})
    assert exp["policy_id"] == "coordinator_review"
    assert exp["attestation_kinds"] is None
    assert exp["policy_unknown"] is False


# ── 渲染（下发的人话块）────────────────────────────────────


def test_format_empty_when_no_hard_expectations():
    assert format_submit_expectations(_task(policy_id="coordinator_review")) == ""
    assert format_submit_expectations(None) == ""


def test_format_renders_kinds_with_tool_guidance():
    block = format_submit_expectations(_task(policy_id="ui_browser_e2e"))
    assert "[SUBMIT CONTRACT]" in block
    assert "browse_e2e" in block
    # 取证方式必须带上（browse 工具）——不是只丢一个 kind 名
    assert "browse(" in block
    # 兜底出路：正式豁免而非硬试
    assert "waive_attestation" in block


def test_format_renders_delivery_contract_shapes():
    block = format_submit_expectations(
        _task(contract={"slice_type": "delivery_contract", "id": "dc-t1"})
    )
    assert "'summary'" in block
    assert "'test'" in block
    assert "test_run:" in block          # 合法形态 1
    assert "N/A—" in block               # 合法形态 2
    assert "contractWaived" in block


def test_format_unknown_policy_redirects_to_gate_reset():
    block = format_submit_expectations(_task(policy_id="bogus_policy"))
    assert "[SUBMIT CONTRACT]" in block
    assert "bogus_policy" in block
    # 指路重设 gate，而非让 agent 去撞 fail-close 门
    assert "dispatch_task" in block
    assert "_unknown_policy" not in block


def test_format_kinds_and_contract_combined():
    """kind 门与 dc 门同时存在时两个块都要渲染。"""
    block = format_submit_expectations(
        _task(
            policy_id="generic_tests",
            contract={"slice_type": "delivery_contract", "id": "dc-t1"},
        )
    )
    assert "attestation kind" in block
    assert "deliveryContract" in block


# ── 注入点集成（期望必须真的随 claim 下发）──────────────────


@pytest.mark.asyncio
async def test_claim_task_echoes_expectations_block(
    monkeypatch: pytest.MonkeyPatch,
):
    """claim_task 工具回显期望块——删掉 lifecycle.py 里的注入调用不能逃过
    测试（否则 assignee 又回到「submit 被拒才知道规则」）。"""

    async def fake_project_id(agent_id):
        return "proj-p1"

    class FakeTaskService:
        async def claim_task(self, project_id, task_id, agent_id):
            return None

        async def get_task(self, project_id, task_id):
            return _task(policy_id="generic_tests")

    monkeypatch.setattr(
        "hiveweave.tools.helpers.get_project_id", fake_project_id
    )
    monkeypatch.setattr(
        "hiveweave.services.task.TaskService", FakeTaskService
    )
    from hiveweave.tools.tasks.lifecycle import (
        ClaimTaskParams,
        claim_task_tool,
    )

    result = await claim_task_tool(
        ClaimTaskParams(taskId="t-1"), "agent-p1", ""
    )
    assert result.success
    assert "[SUBMIT CONTRACT]" in result.output
    assert "test_run" in result.output
    assert "waive_attestation" in result.output
