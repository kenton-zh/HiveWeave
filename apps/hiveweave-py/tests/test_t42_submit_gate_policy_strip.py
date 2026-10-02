"""T42 — 任务级 submitGate 政策的 code_audit kind 结构性剥离（第二评估路径收口）。

背景（S19 实测）：批3 拆门（``SUBMIT_AUDIT_GATE=off`` 默认）只剥了 submit
preflight 的默认门路径（>20 行触发）；任务级 submitGate 政策（coordinator
派单设 ``submitGate=code_audit+unit`` ⇒ policy ``code_audit_unit``）在
**approve/HTTP 门**仍严格要 code_audit 凭证 —— 而 flag off 下 executor 结构
上产不出 code_audit 凭证（request_code_audit 撞上游 540s；平台也不再铸
soft-fail/impossible 事实行）⇒ submit 凭 test_run 过了、approve 卡死，
S16 教训原样复现。

修法（单一事实源）：``code_audit.strip_code_audit_kind_for_gate`` ——
submit preflight / review approve 门 / api HTTP 门 / check_task_attestations
四个评估点 + 三个展示面（required_evidence / ``evidence=`` 标签 /
[SUBMIT CONTRACT]）全部共用同一判据。剥离语义：code_audit 从 required
kinds 移除 ⇒ test_run 等其余凭证即可过门；剥离后为空 ⇒ 空集（软策略既有
语义：HTTP 门直接放行）。flag=on 原样还原（旧行为逐字节）。

钉住的契约：
1. off + code_audit_unit + 只有 test_run 凭证 ⇒ 过门（核心断言：submit 工具
   端到端 + check_task_attestations + HTTP 门三入口）；
2. off + 零凭证 / 只有 code_audit 凭证 ⇒ 仍拒（test_run 等其它 kind 照常
   要求，剥离不开整扇门）；
3. on + 同场景 ⇒ 仍要 code_audit（S19 原始拒绝签名逐字复现）；
4. 任务描述不再钉审计通知（``append_code_audit_notice`` 短路端到端）+
   create/dispatch 的 submitGate 枚举不再教 code_audit 系列；
5. 展示面与门禁同源（label / submit_expectations / required_evidence）。

fixture/DB 模式沿用 tests/test_submit_audit_gate_flag.py（临时 per-project
DB + meta 路由 patch + 真实 agents 表行 + git 全打桩）。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.config import settings, submit_audit_gate_enabled
from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services import attestation as att_module
from hiveweave.services import task as task_module
from hiveweave.services.attestation import (
    attestation_service,
    check_task_attestations,
    policy_required_kinds_label,
)
from hiveweave.services.code_audit import (
    append_code_audit_notice,
    drop_code_audit_kind_if_soft,
    strip_code_audit_kind_for_gate,
)
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.policy import submit_expectations
from hiveweave.tools.tasks.submit import SubmitTaskParams, submit_task_tool

PROJECT_ID = "test-t42-gate-policy-strip"
COORD_ID = "t42-coordinator"
EXEC_ID = "t42-executor"
ALL_AGENTS = (COORD_ID, EXEC_ID)


@pytest.fixture
async def env():
    """临时 per-project DB + meta 路由 patch（同 test_submit_audit_gate_flag）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in ALL_AGENTS else None

        task_module._migrated.clear()
        att_module._migrated.clear()

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace), \
             patch("hiveweave.db.meta.get_agent_project_id",
                   fake_get_agent_project_id), \
             patch.object(settings, "submit_audit_gate", "off"):
            _env = {"workspace_path": workspace_path}
            await _insert_agent(_env, COORD_ID, "架构师", role="coordinator")
            await _insert_agent(_env, EXEC_ID, "工匠", parent_id=COORD_ID)
            yield _env

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        for aid in ALL_AGENTS:
            project_db._agent_cache.pop(aid, None)


async def _insert_agent(
    env, agent_id: str, name: str, *, role: str = "executor",
    permission_type: str = "executor", parent_id: str | None = None,
) -> None:
    conn = await ensure_project_db(env["workspace_path"])
    ts = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agents (id, project_id, name, role, parent_id, status, "
        "permission_type, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [agent_id, PROJECT_ID, name, role, parent_id, "active",
         permission_type, ts, ts],
    )
    await conn.commit()


async def _create_audit_unit_task(env) -> str:
    """coordinator 派单 submitGate=code_audit+unit 的任务形态（running）。"""
    svc = TaskService()
    tid = await svc.create_task(
        project_id=PROJECT_ID,
        title="实现导出功能",
        description="d",
        creator_id=COORD_ID,
        tags=["code_audit", "unit"],
        policy_id="code_audit_unit",
    )
    await svc.claim_task(PROJECT_ID, tid, EXEC_ID)
    await svc.start_task(PROJECT_ID, tid)
    return tid


async def _hang_test_run(env, task_id: str) -> str:
    """executor 真跑过测试的凭证（exit=0 + stdout_hash，绑定本任务）。"""
    return await attestation_service.create(
        PROJECT_ID,
        agent_id=EXEC_ID,
        kind="test_run",
        task_id=task_id,
        command_or_url="uv run pytest tests/ -q",
        exit_code=0,
        stdout="1 passed",
    )


async def _hang_code_audit(env, task_id: str) -> str:
    return await attestation_service.create(
        PROJECT_ID,
        agent_id=EXEC_ID,
        kind="code_audit",
        task_id=task_id,
        command_or_url="[verdict=PASS]",
        exit_code=0,
        stdout="audit ok",
    )


async def _submit_tool(env, params: SubmitTaskParams, caller: str = EXEC_ID):
    async def fake_get_project_id(aid: str):
        return PROJECT_ID if aid in ALL_AGENTS else None

    async def _git_fail_all(args, cwd, timeout: float = 30.0,
                            project_root=None):
        return False, ""

    with patch("hiveweave.tools.helpers.get_project_id", fake_get_project_id), \
         patch("hiveweave.agents.trigger.trigger_coordinator", AsyncMock()), \
         patch("hiveweave.agents.trigger.trigger_subordinate", AsyncMock()), \
         patch("hiveweave.services.git_worktree._git", _git_fail_all):
        return await submit_task_tool(params, caller, env["workspace_path"])


def _task_row(task_id: str, *, policy_id: str = "code_audit_unit") -> dict:
    return {
        "id": task_id,
        "title": "实现导出功能",
        "description": "d",
        "tags": ["code_audit", "unit"],
        "evidence": {},
        "policy_id": policy_id,
        "assignee_id": EXEC_ID,
    }


# ── 1. 单一事实源 helper：剥离判据只有一份 ─────────────────────


class TestStripHelper:
    def test_off_strips_code_audit_keeps_other_kinds(self):
        with patch.object(settings, "submit_audit_gate", "off"):
            assert strip_code_audit_kind_for_gate(
                frozenset({"code_audit", "test_run"})
            ) == frozenset({"test_run"})
            # 剥离后为空 ⇒ 空集（软策略既有语义）
            assert strip_code_audit_kind_for_gate(
                frozenset({"code_audit"})
            ) == frozenset()
            # 无该 kind / 软策略（None）原样
            assert strip_code_audit_kind_for_gate(
                frozenset({"test_run", "browse_e2e"})
            ) == frozenset({"test_run", "browse_e2e"})
            assert strip_code_audit_kind_for_gate(None) is None

    def test_on_is_identity(self):
        """红线：flag=on 旧行为逐字节还原（原样返回，不动集合）。"""
        needed = frozenset({"code_audit", "test_run"})
        with patch.object(settings, "submit_audit_gate", "on"):
            assert strip_code_audit_kind_for_gate(needed) == needed
            assert strip_code_audit_kind_for_gate(None) is None

    def test_predicate_reads_live_settings(self):
        with patch.object(settings, "submit_audit_gate", " ON "):
            assert submit_audit_gate_enabled() is True
        with patch.object(settings, "submit_audit_gate", "off"):
            assert submit_audit_gate_enabled() is False


# ── 2. 核心断言：off + code_audit_unit + 只有 test_run ⇒ 过门 ──


class TestGateOffPassesWithTestRunOnly:
    async def test_check_task_attestations_passes(self, env):
        tid = await _create_audit_unit_task(env)
        aid = await _hang_test_run(env, tid)
        task = await TaskService().get_task(PROJECT_ID, tid)
        task["evidence"] = {"attestation_ids": [aid]}

        # 显式 id 入参（HTTP/UI 路径）与 evidence.attestation_ids（approve
        # 消费面）两形态都过门
        assert await check_task_attestations(PROJECT_ID, task, [aid]) is None
        assert await check_task_attestations(PROJECT_ID, task, None) is None

    async def test_http_gate_passes(self, env):
        from hiveweave.api.tasks import _gate_attestation_for_task

        tid = await _create_audit_unit_task(env)
        aid = await _hang_test_run(env, tid)
        task = await TaskService().get_task(PROJECT_ID, tid)
        task["evidence"] = {"attestation_ids": [aid]}

        # 不抛 HTTPException 即过门
        await _gate_attestation_for_task(PROJECT_ID, task, task["evidence"])

    async def test_submit_tool_end_to_end_passes(self, env):
        """S19 场景端到端：submitGate=code_audit+unit 任务凭 test_run 提交成功。"""
        tid = await _create_audit_unit_task(env)
        aid = await _hang_test_run(env, tid)

        result = await _submit_tool(
            env,
            SubmitTaskParams(
                taskId=tid,
                summary="done",
                testsPassed=True,
                attestationIds=[aid],
            ),
        )

        assert result.success is True, result.error
        task = await TaskService().get_task(PROJECT_ID, tid)
        assert task["status"] == "submitted"

    async def test_funnel_still_fail_closed_without_fact_rows(self, env):
        """drop_code_audit_kind_if_soft 本体契约不动：无事实行仍不剔除
        （T42 剥离做在调用点共用判据，funnel 的 F1 fail-closed 语义保留）。"""
        needed, dropped = await drop_code_audit_kind_if_soft(
            frozenset({"code_audit", "test_run"}),
            PROJECT_ID,
            task_id="no-such-task",
        )
        assert dropped is False
        assert needed == frozenset({"code_audit", "test_run"})


# ── 3. off + 零凭证 / 只有 code_audit ⇒ 仍拒（其它 kind 照常）──


class TestGateOffStillRequiresOtherKinds:
    async def test_zero_attestations_still_rejected(self, env):
        tid = await _create_audit_unit_task(env)
        task = await TaskService().get_task(PROJECT_ID, tid)

        err = await check_task_attestations(PROJECT_ID, task, None)

        assert err is not None
        # 拒因是缺 test_run，不是重新要 code_audit
        assert "test_run" in (err or "").lower() or "No attestation_ids" in err

    async def test_code_audit_alone_does_not_unlock(self, env):
        """剥离语义是「减去 code_audit」不是「开门」：只挂 code_audit 凭证
        仍因缺 test_run 被拒。"""
        tid = await _create_audit_unit_task(env)
        aid = await _hang_code_audit(env, tid)
        task = await TaskService().get_task(PROJECT_ID, tid)

        err = await check_task_attestations(PROJECT_ID, task, [aid])

        assert err is not None
        assert "test_run" in (err or "").lower()


# ── 4. on（显式置位）：旧行为逐字节还原 ────────────────────────


class TestGateOnRestoresOldBehavior:
    async def test_check_task_attestations_requires_code_audit(self, env):
        """同场景 flag=on ⇒ 仍要 code_audit —— S19 原始拒绝签名复现。"""
        tid = await _create_audit_unit_task(env)
        aid = await _hang_test_run(env, tid)
        task = await TaskService().get_task(PROJECT_ID, tid)

        with patch.object(settings, "submit_audit_gate", "on"):
            err = await check_task_attestations(PROJECT_ID, task, [aid])

        assert err is not None
        assert "attestation gate failed (code_audit_unit)" in (err or "")
        assert "Missing required attestation kind(s) ['code_audit']" in (err or "")
        assert "got ['test_run']" in (err or "")

    async def test_http_gate_rejects_without_code_audit(self, env):
        from fastapi import HTTPException

        from hiveweave.api.tasks import _gate_attestation_for_task

        tid = await _create_audit_unit_task(env)
        aid = await _hang_test_run(env, tid)
        task = await TaskService().get_task(PROJECT_ID, tid)
        task["evidence"] = {"attestation_ids": [aid]}

        with patch.object(settings, "submit_audit_gate", "on"), \
             pytest.raises(HTTPException) as ei:
            await _gate_attestation_for_task(
                PROJECT_ID, task, task["evidence"]
            )

        assert ei.value.status_code == 400
        assert "code_audit" in str(ei.value.detail)

    async def test_submit_tool_rejects_when_on(self, env):
        tid = await _create_audit_unit_task(env)
        aid = await _hang_test_run(env, tid)

        with patch.object(settings, "submit_audit_gate", "on"):
            result = await _submit_tool(
                env,
                SubmitTaskParams(
                    taskId=tid,
                    summary="done",
                    testsPassed=True,
                    attestationIds=[aid],
                ),
            )

        assert result.success is False
        assert "code_audit" in (result.error or "")
        task = await TaskService().get_task(PROJECT_ID, tid)
        assert task["status"] == "running"  # 旧门拒绝语义原样


# ── 5. 教学残留：描述按 flag 装配 + 审计通知短路 ───────────────


class TestTeachingResidueCleanup:
    def test_append_code_audit_notice_short_circuit_off(self):
        """off：任务卡不钉审计通知（原样返回，幂等）。"""
        with patch.object(settings, "submit_audit_gate", "off"):
            assert append_code_audit_notice("做 X") == "做 X"
            assert append_code_audit_notice("") == ""

    def test_append_code_audit_notice_pins_when_on(self):
        from hiveweave.services.code_audit import CODE_AUDIT_POLICY

        with patch.object(settings, "submit_audit_gate", "on"):
            pinned = append_code_audit_notice("做 X")
            assert CODE_AUDIT_POLICY in pinned
            # 幂等：重复钉不重复追加
            assert append_code_audit_notice(pinned) == pinned

    def test_submit_gate_enum_assembly_default_off(self):
        """导入期（默认 off）装配：create/dispatch 的 submitGate 枚举与
        字段 schema 不再教 code_audit 系列。"""
        from hiveweave.tools.tasks import create as create_mod
        from hiveweave.tools.tasks import dispatch as dispatch_mod

        assert "code_audit" not in create_mod._SUBMIT_GATE_ENUM_FULL
        assert "code_audit" not in create_mod._SUBMIT_GATE_ENUM_SHORT
        assert "code_audit" not in dispatch_mod._SUBMIT_GATE_ENUM_FULL
        field_desc = create_mod.CreateTaskParams.model_fields[
            "submit_gate"
        ].description or ""
        assert "code_audit" not in field_desc
        assert "docs | unit | module_visual" in field_desc

    def test_on_assembly_restores_enum(self):
        """纯函数语义：flag=on 时枚举还原（此处直接按判据装配验证）。"""
        from hiveweave.tools.tasks import create as create_mod

        expected_full = (
            "docs | unit | module_visual"
            " | code_audit | code_audit+module_visual | code_audit+unit"
        )
        with patch.object(settings, "submit_audit_gate", "on"):
            # 模块级常量导入期已定稿；判据本身按 settings 现读，
            # on 状态下 create/description 的装配表达式等价于全枚举。
            assert submit_audit_gate_enabled() is True
            assert (
                "docs | unit | module_visual"
                + (
                    " | code_audit | code_audit+module_visual | code_audit+unit"
                    if submit_audit_gate_enabled()
                    else ""
                )
            ) == expected_full
            # on 时 append 通知恢复钉（见上），枚举恢复在重启后生效（同
            # build_submit_task_description 的导入期口径）。
            assert create_mod is not None


# ── 6. 展示面与门禁同源 ────────────────────────────────────────


class TestDisplaySingleSource:
    def test_label_strips_code_audit_when_off(self):
        assert policy_required_kinds_label("code_audit_unit") == "test_run"
        assert policy_required_kinds_label("code_audit") == ""
        assert policy_required_kinds_label("code_audit_visual") == "browse_e2e"

    def test_label_keeps_full_kinds_when_on(self):
        with patch.object(settings, "submit_audit_gate", "on"):
            assert (
                policy_required_kinds_label("code_audit_unit")
                == "code_audit,test_run"
            )

    def test_submit_expectations_strips_code_audit_when_off(self):
        exp = submit_expectations({"id": "t", "policy_id": "code_audit_unit",
                                   "tags": []})
        assert exp["attestation_kinds"] == ["test_run"]
        assert exp["policy_unknown"] is False
        # 纯审计 policy 剥离后为空 ⇒ 软策略形态（不再教 request_code_audit）
        exp2 = submit_expectations({"id": "t", "policy_id": "code_audit",
                                    "tags": []})
        assert exp2["attestation_kinds"] is None
        assert exp2["policy_unknown"] is False

    def test_submit_expectations_full_when_on(self):
        with patch.object(settings, "submit_audit_gate", "on"):
            exp = submit_expectations(
                {"id": "t", "policy_id": "code_audit_unit", "tags": []}
            )
            assert exp["attestation_kinds"] == ["code_audit", "test_run"]

    async def test_platform_state_required_evidence_strips(self, env):
        from hiveweave.services.platform_state import _policy_required_evidence

        tid = await _create_audit_unit_task(env)
        task = await TaskService().get_task(PROJECT_ID, tid)
        assert _policy_required_evidence(task) == ["test_run"]

        with patch.object(settings, "submit_audit_gate", "on"):
            assert _policy_required_evidence(task) == [
                "code_audit",
                "test_run",
            ]
