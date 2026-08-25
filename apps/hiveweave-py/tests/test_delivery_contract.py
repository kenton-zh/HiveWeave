"""Delivery contract — 普通代码任务的轻量 slice 契约(收敛规格 §10 验收)。

规格: docs/spec/delivery-contract.md
覆盖:
- 注入端:dispatch 给写树 assignee 默认生成 dc 契约(VERIFY/非写树跳过、
  已有契约不覆盖)
- 校验端:submit preflight 单一 check(delivery_contract_incomplete)
  —— 回执缺失 / 测试凭证机器验证 / N/A 声明 / contractWaived / 存量兼容
细胞测试:build_default_contract / delivery_contract_missing / test_evidence_*
"""

from __future__ import annotations

import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services import delivery_contract as dcv
from hiveweave.services.task import TaskService
from hiveweave.services.task_contract import validate_contract
from hiveweave.services.attestation import attestation_service
from hiveweave.tools.tasks.submit import (
    SubmitTaskParams,
    _submit_preflight,
)

PROJECT_ID = "dc-proj"
COORD = "coord-dc"
EXEC = "exec-dc"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _dc_contract(task_id: str = "abc12345") -> dict:
    return dcv.build_default_contract(task_id)


async def _mk_task(
    env, *, title: str = "signup module", contract=None, assignee: str | None = EXEC
):
    ts = TaskService()
    tid = await ts.create_task(
        env["project_id"],
        title,
        "impl signup",
        creator_id=COORD,
        assignee_id=assignee,
        policy_id="coordinator_review",
        contract_json=contract,
    )
    task = await ts.get_task(env["project_id"], tid)
    return ts, tid, task


async def _preflight(env, task, params_overrides: dict | None = None):
    base = {
        "task_id": task["id"],
        "summary": "impl done",
        "tests_passed": True,
    }
    if params_overrides:
        base.update(params_overrides)
    params = SubmitTaskParams(**base)
    with patch("hiveweave.services.worktree_review.agent_worktree_path",
               AsyncMock(return_value=None)):
        return await _submit_preflight(
            env["project_id"], EXEC, task["id"], task, params
        )


# ── 细胞测试: 契约形状 / 回执判定 / 测试证据解析 ─────────────

def test_build_default_contract_shape():
    c = _dc_contract()
    assert validate_contract(c) is None  # schema 合法
    assert c["slice_type"] == "delivery_contract"
    assert c.get("inputs") == []  # 无 inputs → 不触发 ready-gate
    assert c.get("slice_status") == "ready"
    kinds = [a["type"] for a in c["acceptance"]]
    assert kinds == ["manual_review", "manual_review"]  # 机器验收 deferred


def test_delivery_contract_missing():
    # 无回执 → 两字段全缺
    assert set(dcv.delivery_contract_missing({})) == {"summary", "test"}
    # 占位符视为缺失
    assert "summary" in dcv.delivery_contract_missing(
        {"delivery_contract": {"summary": "<待填>", "test": "x"}}
    )
    # 合法回执 → 通过
    assert dcv.delivery_contract_missing(
        {"delivery_contract": {"summary": "done", "test": "test_run:1"}}
    ) == []


def test_test_evidence_parsing():
    assert dcv.test_evidence_is_na("N/A — 无测试基建")
    assert dcv.test_evidence_is_na("n/a- 无测试基建")
    assert not dcv.test_evidence_is_na("test_run:abc")
    assert dcv.test_evidence_reason("N/A — 无测试基建").startswith("无测试")
    assert dcv.test_evidence_reason("N/A") == ""  # 裸 N/A 无原因
    assert dcv.parse_test_evidence_attestation_id("test_run:abc123") == "abc123"
    assert dcv.parse_test_evidence_attestation_id("abc123") is None  # 裸 id 不收
    assert dcv.parse_test_evidence_attestation_id("N/A — 无测试基建") is None
    assert dcv.parse_test_evidence_attestation_id("") is None


# ── 注入端 ─────────────────────────────────────────────────

def _dispatch_env_mocks(env, *, writes: bool, verify_title: str | None = None):
    """dispatch_task 周边最小 mock:写树资格可按用例切换(AsyncExitStack 多模块)。"""
    ws = env["workspace"]
    stack = AsyncExitStack()
    mocks = [
        patch("hiveweave.services.org_span.validate_dispatch_span",
              AsyncMock(return_value=None)),
        patch("hiveweave.services.org_span.validate_ceo_dispatch_target",
              AsyncMock(return_value=None)),
        patch("hiveweave.services.org_span.validate_executor_assignee",
              AsyncMock(return_value=None)),
        patch("hiveweave.services.git_worktree.agent_gets_write_worktree",
              lambda a: writes),
        patch("hiveweave.services.git_worktree.ensure_executor_worktree",
              AsyncMock(return_value={"success": True, "path": ws,
                                      "short_id": "EXEC1"})),
        patch("hiveweave.services.git_worktree.worktree_commits_behind_main",
              AsyncMock(return_value=0)),
        patch("hiveweave.services.org.OrgService.resolve_agent",
              AsyncMock(return_value={"short_id": "EXEC1",
                                      "permission_type": "executor"})),
        patch("hiveweave.services.inbox.InboxService.send_message",
              AsyncMock(return_value={"success": True})),
        patch("hiveweave.services.handoff.HandoffService.create_handoff",
              AsyncMock(return_value="handoff-1")),
        patch("hiveweave.services.obligation.ObligationLedger.create",
              AsyncMock(return_value=None)),
        patch("hiveweave.services.code_audit.append_code_audit_notice",
              lambda d: d + "\n[CODE AUDIT POLICY] x"),
    ]
    for m in mocks:
        stack.enter_context(m)
    return stack


@pytest.mark.asyncio
async def test_dispatch_injects_dc_for_writer(env):
    from hiveweave.services.dispatch import DispatchService

    async with _dispatch_env_mocks(env, writes=True):
        ds = DispatchService()
        out = await ds.dispatch_task(
            env["project_id"], COORD, EXEC, "impl signup module"
        )
    assert out["success"] is True
    task = await TaskService().get_task(env["project_id"], out["task_id"])
    c = dcv.parse_delivery_contract(task)
    assert c is not None
    assert c["slice_type"] == "delivery_contract"


@pytest.mark.asyncio
async def test_dispatch_skips_non_writer(env):
    from hiveweave.services.dispatch import DispatchService

    # 非写树 assignee(CEO/HR):不生成契约
    async with _dispatch_env_mocks(env, writes=False):
        ds = DispatchService()
        # resolve_agent 走非写树分支,仍返回 dict
        out = await ds.dispatch_task(
            env["project_id"], COORD, EXEC, "impl signup module"
        )
    task = await TaskService().get_task(env["project_id"], out["task_id"])
    assert dcv.parse_delivery_contract(task) is None


@pytest.mark.asyncio
async def test_dispatch_does_not_overwrite_existing_contract(env):
    from hiveweave.services.dispatch import DispatchService

    ts, tid, _ = await _mk_task(env)
    custom = {
        "id": "custom-slice",
        "slice_status": "ready",
        "inputs": [],
        "deliverables": [],
        "acceptance": [{"id": "AC1", "type": "manual_review", "note": "custom"}],
    }
    await ts._persist_contract_json(env["project_id"], tid, custom)

    async with _dispatch_env_mocks(env, writes=True):
        ds = DispatchService()
        out = await ds.dispatch_task(
            env["project_id"], COORD, EXEC, "impl signup module",
            existing_task_id=tid,
        )
    task = await TaskService().get_task(env["project_id"], out["task_id"])
    assert task["contract_json"].get("id") == "custom-slice"  # 未被覆盖


# ── 校验端(preflight) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_preflight_empty_receipt_rejected(env):
    _, tid, task = await _mk_task(env, contract=_dc_contract("x"))
    res = await _preflight(env, task)  # 无 delivery_contract 回执
    codes = {i["code"] for i in res["issues"]}
    assert "delivery_contract_incomplete" in codes


@pytest.mark.asyncio
async def test_preflight_placeholder_receipt_rejected(env):
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    res = await _preflight(
        env, task, {"delivery_contract": {"summary": "<待填>", "test": "x"}}
    )
    assert "delivery_contract_incomplete" in {
        i["code"] for i in res["issues"]
    }


@pytest.mark.asyncio
async def test_preflight_real_test_run_token_passes(env):
    _, tid, task = await _mk_task(env, contract=_dc_contract("tid12345"))
    att_id = await attestation_service.create(
        env["project_id"], agent_id=EXEC, kind="test_run",
        task_id=tid, command_or_url="pytest -q", exit_code=0, stdout="ok",
    )
    res = await _preflight(
        env, task, {"delivery_contract": {"summary": "done",
                                          "test": f"test_run:{att_id}"}}
    )
    assert "delivery_contract_incomplete" not in {
        i["code"] for i in res["issues"]
    }


@pytest.mark.asyncio
async def test_preflight_fake_test_run_token_rejected(env):
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    # 编造 id —— 平台不可验证 → issue
    res = await _preflight(
        env, task, {"delivery_contract": {"summary": "done",
                                          "test": "test_run:does-not-exist"}}
    )
    assert "delivery_contract_incomplete" in {
        i["code"] for i in res["issues"]
    }


@pytest.mark.asyncio
async def test_preflight_na_reason_passes(env):
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    res = await _preflight(
        env, task, {"delivery_contract": {"summary": "done",
                                          "test": "N/A — 仓库无测试基建"}}
    )
    assert "delivery_contract_incomplete" not in {
        i["code"] for i in res["issues"]
    }
    # N/A 缺原因 → issue
    res2 = await _preflight(
        env, task, {"delivery_contract": {"summary": "done", "test": "N/A"}}
    )
    assert "delivery_contract_incomplete" in {
        i["code"] for i in res2["issues"]
    }


@pytest.mark.asyncio
async def test_preflight_contract_waived_skips(env):
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    res = await _preflight(
        env, task, {"delivery_contract": None, "contract_waived": True}
    )
    assert "delivery_contract_incomplete" not in {
        i["code"] for i in res["issues"]
    }


@pytest.mark.asyncio
async def test_preflight_legacy_task_no_contract_unaffected(env):
    # 存量任务:无 contract_json → preflight 不受 dc 检查影响
    _, tid, task = await _mk_task(env, contract=None)
    res = await _preflight(env, task)
    assert "delivery_contract_incomplete" not in {
        i["code"] for i in res["issues"]
    }


@pytest.mark.asyncio
async def test_preflight_non_dc_slice_skipped(env):
    # 协调者自建的非 delivery 类型切片契约,无回执也不产生 dc issue
    # (parse_delivery_contract 只认 slice_type=delivery_contract)。
    custom = {
        "id": "custom-slice",
        "slice_type": "slice",
        "slice_status": "ready",
        "inputs": [],
        "deliverables": [],
        "acceptance": [{"id": "AC1", "type": "manual_review", "note": "custom"}],
    }
    _, tid, task = await _mk_task(env, contract=custom)
    res = await _preflight(env, task)  # 无 delivery_contract 回执
    assert "delivery_contract_incomplete" not in {
        i["code"] for i in res["issues"]
    }


@pytest.mark.asyncio
async def test_preflight_waiver_short_circuit(env):
    # 协调者显式 waiver 后,回执缺失不再拦截(wlaiver 短路,规格 §5 出口 2)
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    with patch("hiveweave.services.attestation.has_valid_waiver",
               AsyncMock(return_value=True)):
        res = await _preflight(env, task)  # 无回执,但有 waiver
    assert "delivery_contract_incomplete" not in {
        i["code"] for i in res["issues"]
    }


@pytest.mark.asyncio
async def test_preflight_na_but_successful_test_run_exists_rejected(env):
    # R1 回执一致性:任务存在成功 test_run 凭证,executor 却写 N/A → 拦
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    await attestation_service.create(
        env["project_id"], agent_id=EXEC, kind="test_run",
        task_id=tid, command_or_url="pytest -q", exit_code=0, stdout="ok",
    )
    res = await _preflight(
        env, task, {"delivery_contract": {"summary": "done",
                                          "test": "N/A — 没跑测试"}}
    )
    assert "delivery_contract_inconsistent" in {
        i["code"] for i in res["issues"]
    }


@pytest.mark.asyncio
async def test_preflight_na_without_test_run_still_passes(env):
    # R1 互补:任务确无成功 test_run 凭证时,N/A(带原因)仍放行
    _, tid, task = await _mk_task(env, contract=_dc_contract())
    res = await _preflight(
        env, task, {"delivery_contract": {"summary": "done",
                                          "test": "N/A — 仓库无测试基建"}}
    )
    assert "delivery_contract_inconsistent" not in {
        i["code"] for i in res["issues"]
    }
    assert "delivery_contract_incomplete" not in {
        i["code"] for i in res["issues"]
    }