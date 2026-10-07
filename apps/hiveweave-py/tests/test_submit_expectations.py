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
    """下发块的 dc 形态必须指向新契约（#14 后 N/A 文本出口已死）。

    原断言 ``assert "N/A—" in block`` 是**对旧契约的固化**：当时交付契约的
    ``test`` 字段接受 ``N/A—<原因>`` 自由文本作为放行出口。``delivery_contract``
    改成按 ``evidence_kind`` 状态判定后，裸写 N/A 文本**一律判缺**，
    旧断言等于把一条已死的路锁进回归测试 —— 它绿着，但教的是死路。
    """
    block = format_submit_expectations(
        _task(contract={"slice_type": "delivery_contract", "id": "dc-t1"})
    )
    assert "'summary'" in block
    assert "'test'" in block
    assert "test_run:" in block          # 唯一合法形态：本任务 test_run 凭证 id
    # 旧出口（N/A 文本即放行）不得再被下发块推荐
    assert "N/A—" not in block and "| N/A" not in block
    # 新出路必须写明：状态字段 + 平台 waiver，而不是另一份"写 XX 就放行"的词表
    assert "evidence_kind" in block
    assert "not_applicable" in block
    assert "waive_attestation" in block
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


# ── 批26 修复1：可照抄回执模板（submit 首过率前置化）────────────────


def test_format_renders_copy_paste_template_for_dc_tasks():
    """有 delivery 契约 ⇒ 下发块必须带**可照抄**的回执模板。

    第五轮 submit 首过失败主拒因 = 「Delivery contract 回执未填齐」
    74+64 次；模板前置到派单/认领时刻（本块随 P1-7 钉进任务卡与 claim
    回显），不再等 submit 被拒才见处方。
    """
    block = format_submit_expectations(
        _task(contract={"slice_type": "delivery_contract", "id": "dc-t1"})
    )
    assert "可照抄回执模板" in block
    # 模板与契约段合并在同一个 [SUBMIT CONTRACT] 块内（不另起段落，
    # dispatch/claim 侧的整块去重 `block not in description` 仍有效）
    assert block.count("[SUBMIT CONTRACT]") == 1
    # summary / test 两个槽位都在模板里（REQUIRED_REPLY_FIELDS 全覆盖）
    from hiveweave.services.tasks.policy import (
        _DC_TEMPLATE_SUMMARY,
        _DC_TEMPLATE_TEST,
    )

    assert _DC_TEMPLATE_SUMMARY in block
    assert _DC_TEMPLATE_TEST in block
    assert "test_run:" in _DC_TEMPLATE_TEST


def test_template_values_pass_placeholder_self_check():
    """模板值不得落在占位前缀上 —— 照抄模板不能被占位门二次拒收。

    旧 _DC_FIELD_SHAPE 的 `<…>` 形态本身就是占位前缀（is_placeholder_text
    判 True），照抄必被 submit 占位门打回 —— 本测试锁死「模板值自查过
    占位门」这条契约（与 delivery_contract._PLACEHOLDER_LEADS 同源）。
    """
    from hiveweave.services.delivery_contract import is_placeholder_text
    from hiveweave.services.tasks.policy import (
        _DC_TEMPLATE_SUMMARY,
        _DC_TEMPLATE_TEST,
    )

    assert is_placeholder_text(_DC_TEMPLATE_SUMMARY) is False, (
        "summary 模板值落在占位前缀上 ⇒ 照抄即被二次拒收"
    )
    assert is_placeholder_text(_DC_TEMPLATE_TEST) is False, (
        "test 模板值落在占位前缀上 ⇒ 照抄即被二次拒收"
    )


def test_template_absent_without_delivery_contract():
    """无 dc 契约（纯 kind 门 / 软策略）⇒ 不渲染模板（没得填的东西）。"""
    block = format_submit_expectations(_task(policy_id="generic_tests"))
    assert "可照抄回执模板" not in block
    assert format_submit_expectations(_task()) == ""


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
