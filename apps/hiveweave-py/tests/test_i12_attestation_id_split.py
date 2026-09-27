"""I12(P2-2) 批 4 差集：凭证 id 与 task id 分列 + 链路参数校验面核实.

两个验收面（fixplan §四 I12）：
1. 方法② —— ``check_attestation_reuse_binding`` 的 different-agent/different-task
   拒绝文案必须把「凭证 id」与「task id」写成**具名、独立可 grep**的值
   （旧文案 ``"Attestation task_id mismatch: <id>"`` 把凭证 id 塞进唯一
   ``<id>`` 槽，读起来像 task id）。
2. 方法①核实 —— attestation 链路上的**参数**校验（submit_task /
   waive_attestation 均经 ``@tool`` 注册走统一 pipeline ``validate_detailed``）
   在批 E 改造后已给逐条 instancePath（方括号形态进 ``invalidArgs``、点号
   形态进 ``error`` 文本），且不做 expected-vs-provided 值 diff。attestation
   服务自身（verify_ids / binding 矩阵）是语义门禁，无 pydantic 参数校验面
   —— 方法①对它没有适用面，本文件只钉住工具参数侧。
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.attestation import (
    AttestationService,
    check_attestation_reuse_binding,
)
from hiveweave.tools.base import get_tool_def

TASK_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TASK_A_STRIPPED = TASK_A.replace("-", "")
TASK_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
AGENT_1 = "11111111-1111-4111-8111-111111111111"
AGENT_2 = "22222222-2222-4222-8222-222222222222"
ATT_ID = "407ec944-1111-4111-8111-111111111111"


def _row(**kwargs) -> dict:
    import time

    now = int(time.time() * 1000)
    base = {
        "id": ATT_ID,
        "agent_id": AGENT_2,
        "task_id": TASK_A_STRIPPED,
        "kind": "test_run",
        "exit_code": 0,
        "stdout_hash": "deadbeefdeadbeef",
        "commit_hash": "",
        "created_at": now,
        "expires_at": now + 24 * 60 * 60 * 1000,
    }
    base.update(kwargs)
    return base


@pytest.mark.asyncio
async def test_mismatch_names_attestation_id_and_both_task_ids():
    """不同 agent + 不同 task ⇒ 文案三值具名：attestation_id / 两个 task id。"""
    ok, err = await check_attestation_reuse_binding(
        "proj",
        _row(),
        expected_task_id=TASK_B,
        expected_agent_id=AGENT_1,
    )
    assert ok is False
    # 凭证 id 独立具名（不再塞进 "mismatch:" 后的歧义 <id> 槽）
    assert f"attestation_id={ATT_ID}" in err
    assert "mismatch: attestation_id=" in err
    # 凭证绑定的 task 与本次期望的 task 各自具名
    assert f"attestation_task_id={TASK_A_STRIPPED}" in err
    assert f"task_id={TASK_B}" in err


@pytest.mark.asyncio
async def test_mismatch_ids_independently_greppable_and_distinct():
    """两个 id 可被独立正则提取，且凭证 id ≠ task id（分列而非同一槽）。"""
    _, err = await check_attestation_reuse_binding(
        "proj",
        _row(),
        expected_task_id=TASK_B,
        expected_agent_id=AGENT_1,
    )
    m_aid = re.search(r"attestation_id=(\S+)", err)
    m_att_task = re.search(r"attestation_task_id=(\S+)", err)
    # 负向后顾排除 attestation_task_id= / attestation_id= 的子串误命中
    m_task = re.search(r"(?<![a-z_])task_id=(\S+)", err)
    assert m_aid and m_att_task and m_task
    assert m_aid.group(1) == ATT_ID
    assert m_att_task.group(1) == TASK_A_STRIPPED
    assert m_task.group(1) == TASK_B
    assert m_aid.group(1) != m_task.group(1)


@pytest.mark.asyncio
async def test_verify_ids_submit_chain_receipt_carries_named_ids():
    """submit 链路（verify_ids → check）回执透出同样分列的两个 id。"""
    svc = AttestationService()
    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch.object(svc, "get", new_callable=AsyncMock, return_value=_row()),
    ):
        ok, err = await svc.verify_ids(
            "proj",
            [ATT_ID],
            expected_agent_id=None,  # 不约束 agent ⇒ 走 binding 的 task 分列分支
            task_id=TASK_B,
        )
    assert ok is False
    assert f"attestation_id={ATT_ID}" in err
    assert f"task_id={TASK_B}" in err


@pytest.mark.asyncio
async def test_legit_bindings_still_pass():
    """合法路径照常通过（改文案不改判定语义）。"""
    # 不同 agent + 同 task ⇒ 允许（P2-4 task 级 pooling）
    ok, err = await check_attestation_reuse_binding(
        "proj",
        _row(agent_id=AGENT_2),
        expected_task_id=TASK_A,
        expected_agent_id=AGENT_1,
    )
    assert ok, err
    # 同 agent + 不同 task + 无 commit_hash ⇒ 允许（复用矩阵）
    ok2, err2 = await check_attestation_reuse_binding(
        "proj",
        _row(agent_id=AGENT_1, commit_hash=""),
        expected_task_id=TASK_B,
        expected_agent_id=AGENT_1,
    )
    assert ok2, err2


def test_attestation_chain_tools_are_pipeline_validated():
    """方法①核实：链上两个工具都经 @tool 注册 ⇒ 参数校验走批 E 统一漏斗。"""
    assert get_tool_def("submit_task") is not None
    assert get_tool_def("waive_attestation") is not None


def test_submit_task_nested_param_error_gives_instance_path():
    """嵌套/数组项参数错误 ⇒ 逐条 instancePath（方括号 + 点号双形态）。"""
    td = get_tool_def("submit_task")
    params, error, violations = td.validate_detailed(
        {"summary": "x", "failuresAcknowledged": ["oops"]}
    )
    assert params is None and error
    assert any(
        v["path"].endswith("[0]") and v.get("message") for v in violations
    )
    # 点号形态进 error 文本（与方括号形态双写，批 E P2-1 口径）
    assert re.search(r"failures_acknowledged\.0", error)
    # 不做 expected-vs-provided 值 diff：调用方提供的坏值不被回显
    assert "oops" not in error


def test_waive_attestation_valid_params_pass_validation():
    """合法参数照常通过（validation 面），违规清单为空。"""
    td = get_tool_def("waive_attestation")
    params, error, violations = td.validate_detailed(
        {
            "taskId": TASK_B,
            "reason": "audit LLM transient outage",
            "reasonKind": "tool_failure",
            "evidenceAttestationId": ATT_ID,
        }
    )
    assert params is not None
    assert error is None
    assert violations == []
