"""批3 — SUBMIT_AUDIT_GATE 灰度：submit 路径逐提交 code_audit 门拆/回切。

用户钦定（2026-09-29）：五次实跑证明逐提交 LLM 审计高成本近零信号（S16 退化
审计冻结全组织 3.5h；82/78 无审计照样 20/20 交付），里程碑 QA 验收两次抓到
真缺陷 ⇒ **拆除逐提交审计门，QA 终局负责审计**。

钉住的契约：

1. off（默认）：>20 行 diff 的 code_audit policy 任务 submit **直接过门**
   —— 无审计要求 issue、无 SYSTEM AUDIT 卡铸造、回执不提审计要求。
2. off 时任务级门原样保留：code_audit+unit（policy code_audit_unit）缺
   test_run 凭证仍被拒，但回执处方不再是 request_code_audit。
3. on（fixture 显式置位）：旧门回切 —— submit 被拒 + 铸 SYSTEM AUDIT 卡
   （机制回归，完整链路见 test_t1_audit_dispatch.py）。
4. 铸造入口防御短路：flag off 时 ``mint_audit_task`` 直接
   ``{"dispatched": False, "reason": "submit_audit_gate_off"}``，零落库；
   ``format_dispatch_receipt_block`` 对该 reason 渲染空串（回执不漏审计散文）。
5. 工具描述契约随 flag 装配：off 无 ``CODE AUDIT GATE`` 条款；on 有。
   ``submit_audit_gate_enabled`` 判据容忍大小写/空白，非法值按 off。
6. 成功回执的审计软提醒（账本超阈）在 off 时静默；账本重置保留。

fixture/DB 模式沿用 tests/test_t1_audit_dispatch.py（临时 per-project DB +
meta 路由 patch + 真实 agents 表行 + git 全打桩）。
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
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.audit_dispatch import (
    format_dispatch_receipt_block,
    mint_audit_task,
)
from hiveweave.tools.tasks.submit import (
    SUBMIT_TASK_TOOL_DESCRIPTION,
    SubmitTaskParams,
    build_submit_task_description,
    submit_task_tool,
)

PROJECT_ID = "test-submit-audit-gate-flag"
COORD_ID = "gate-coordinator"
EXEC_ID = "gate-executor"
QA_LEAD_ID = "gate-qa-lead"
ALL_AGENTS = (COORD_ID, EXEC_ID, QA_LEAD_ID)
AUDIT_PREFIX = "SYSTEM AUDIT: "


@pytest.fixture
async def env():
    """临时 per-project DB + meta 路由 patch（同 test_t1 模式，flag 显式 off）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in ALL_AGENTS else None

        async def fake_get_agent_by_id(aid: str):
            try:
                conn = await ensure_project_db(workspace_path)
                cur = await conn.execute(
                    "SELECT * FROM agents WHERE id = ?", [str(aid)]
                )
                row = await cur.fetchone()
                await cur.close()
                return dict(row) if row else None
            except Exception:
                return None

        task_module._migrated.clear()
        from hiveweave.services.code_audit import reset_ledger
        from hiveweave.services.tasks.gate_reject_guard import reset_for_tests

        reset_for_tests()
        for aid in ALL_AGENTS:
            reset_ledger(aid)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace), \
             patch("hiveweave.db.meta.get_agent_project_id",
                   fake_get_agent_project_id), \
             patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id), \
             patch.object(settings, "submit_audit_gate", "off"):
            _env = {"workspace_path": workspace_path}
            await _insert_agent(
                _env, COORD_ID, "架构师",
                role="coordinator", permission_type="coordinator",
                parent_id=None,
            )
            await _insert_agent(
                _env, EXEC_ID, "工匠",
                role="engineer", permission_type="executor",
                parent_id=COORD_ID,
            )
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        for aid in ALL_AGENTS:
            project_db._agent_cache.pop(aid, None)
        from hiveweave.services.code_audit import reset_ledger
        from hiveweave.services.tasks.gate_reject_guard import reset_for_tests

        for aid in ALL_AGENTS:
            reset_ledger(aid)
        reset_for_tests()


# ── Helpers（同 test_t1 模式）────────────────────────────────


async def _insert_agent(
    env, agent_id: str, name: str, *, role: str = "executor",
    permission_type: str = "executor", parent_id: str | None = COORD_ID,
    status: str = "active",
) -> None:
    conn = await ensure_project_db(env["workspace_path"])
    ts = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agents (id, project_id, name, role, parent_id, status, "
        "permission_type, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [agent_id, PROJECT_ID, name, role, parent_id, status, permission_type,
         ts, ts],
    )
    await conn.commit()


async def _insert_qa_lead(env) -> None:
    await _insert_agent(
        env, QA_LEAD_ID, "验真", role="qa_lead",
        permission_type="coordinator", parent_id=None,
    )


async def _create_source_task(
    env, *, assignee_id: str = EXEC_ID, tags: list[str] | None = None,
) -> str:
    """建 code_audit policy 的源任务并 claim+start（>20 行 diff 形态）。"""
    svc = TaskService()
    tid = await svc.create_task(
        project_id=PROJECT_ID, title="实现导出功能", description="d",
        creator_id=COORD_ID, tags=list(tags or ["code_audit"]),
    )
    if assignee_id:
        await svc.claim_task(PROJECT_ID, tid, assignee_id)
        await svc.start_task(PROJECT_ID, tid)
    return tid


async def _git_fail_all(args, cwd, timeout: float = 30.0, project_root=None):
    """封闭桩：worktree 定位本就返回 None，任何 git 探测一律失败。"""
    return False, ""


async def _submit_tool(env, params: SubmitTaskParams, caller: str = EXEC_ID):
    async def fake_get_project_id(aid: str):
        return PROJECT_ID if aid in ALL_AGENTS else None

    with patch("hiveweave.tools.helpers.get_project_id", fake_get_project_id), \
         patch("hiveweave.agents.trigger.trigger_coordinator", AsyncMock()), \
         patch("hiveweave.agents.trigger.trigger_subordinate", AsyncMock()), \
         patch("hiveweave.services.git_worktree._git", _git_fail_all):
        return await submit_task_tool(params, caller, env["workspace_path"])


def _mint_params(tid: str) -> SubmitTaskParams:
    return SubmitTaskParams(taskId=tid, summary="big diff landed", testsPassed=True)


async def _audit_task_rows(env) -> list[dict]:
    conn = await ensure_project_db(env["workspace_path"])
    cur = await conn.execute(
        "SELECT id, title, status FROM tasks WHERE title LIKE ?",
        [AUDIT_PREFIX + "%"],
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


# ── 1. off（默认）：>20 行 diff submit 直过，零铸造 ───────────


class TestGateOffDirectPass:
    async def test_big_diff_submit_passes_without_audit(self, env):
        """code_audit policy + 无审计凭证 ⇒ 过门直 submitted（旧门必拒）。"""
        tid = await _create_source_task(env)

        result = await _submit_tool(env, _mint_params(tid))

        assert result.success is True, result.error
        out = result.output or ""
        # 回执不再提审计要求（旧软提醒一并静默）
        assert "request_code_audit" not in out
        assert "AUDIT" not in out.upper()
        # 零铸造
        assert await _audit_task_rows(env) == []
        task = await TaskService().get_task(PROJECT_ID, tid)
        assert task["status"] == "submitted"

    async def test_resubmit_still_idempotent_after_gate_removal(self, env):
        """拆门不伤批2 幂等出口：submitted 状态重提仍走幂等成功。"""
        tid = await _create_source_task(env)
        first = await _submit_tool(env, _mint_params(tid))
        assert first.success is True

        second = await _submit_tool(env, _mint_params(tid))
        assert second.success is True
        assert second.to_dict().get("idempotent") is True
        assert await _audit_task_rows(env) == []

    async def test_off_suppresses_over_threshold_reminder(self, env):
        """off 时账本超阈的软提醒一并静默；账本重置保留（提交成功即清账）。"""
        from hiveweave.services.code_audit import (
            get_unaudited_lines,
            record_change,
        )

        record_change(EXEC_ID, 25)
        assert get_unaudited_lines(EXEC_ID) == 25
        tid = await _create_source_task(env)

        result = await _submit_tool(env, _mint_params(tid))

        assert result.success is True, result.error
        assert "CODE AUDIT REMINDER" not in (result.output or "")
        assert get_unaudited_lines(EXEC_ID) == 0  # reset_ledger 保留


# ── 2. off：unit/visual 等任务级门原样保留 ────────────────────


class TestGateOffKeepsTaskGates:
    async def test_unit_gate_still_requires_test_run(self, env):
        """code_audit+unit 缺 test_run 凭证 ⇒ 仍拒；处方不再指 request_code_audit。"""
        tid = await _create_source_task(env, tags=["code_audit", "unit"])

        result = await _submit_tool(env, _mint_params(tid))

        assert result.success is False
        text = result.error or ""
        assert "test_run" in text  # 任务级 kind 门保留
        assert "request_code_audit" not in text  # 审计处方消失
        assert "[AUDIT TASK DISPATCHED]" not in text  # 不铸不派
        assert await _audit_task_rows(env) == []
        task = await TaskService().get_task(PROJECT_ID, tid)
        assert task["status"] == "running"  # 仍被任务级门拒在门外


# ── 3. on（fixture 显式置位）：旧门回切，铸造机制回归 ─────────


class TestGateOnMintRestored:
    async def test_flag_on_restores_mint_rejection(self, env):
        await _insert_qa_lead(env)
        tid = await _create_source_task(env)

        with patch.object(settings, "submit_audit_gate", "on"):
            result = await _submit_tool(env, _mint_params(tid))

        assert result.success is False
        receipt = result.to_dict()
        assert receipt["audit_dispatched"] is True
        assert receipt["audit_reviewer_id"] == QA_LEAD_ID
        rows = await _audit_task_rows(env)
        assert len(rows) == 1
        assert "[AUDIT TASK DISPATCHED]" in (result.error or "")
        # 源任务仍 running（旧门拒绝语义原样）
        task = await TaskService().get_task(PROJECT_ID, tid)
        assert task["status"] == "running"


# ── 4. 铸造入口防御短路（off）────────────────────────────────


class TestMintEntryShortCircuit:
    async def test_mint_direct_call_is_noop_when_off(self, env):
        """绕过 submit 工具层直接调铸造 ⇒ off 模式零落库 not-dispatched。"""
        svc = TaskService()
        tid = await svc.create_task(
            project_id=PROJECT_ID, title="源任务", description="d",
            creator_id=COORD_ID, tags=["code_audit"],
        )
        task = await svc.get_task(PROJECT_ID, tid)

        mint = await mint_audit_task(PROJECT_ID, task, EXEC_ID)

        assert mint == {"dispatched": False, "reason": "submit_audit_gate_off"}
        assert await _audit_task_rows(env) == []

    def test_gate_off_reason_renders_empty_receipt_block(self):
        assert format_dispatch_receipt_block(
            {"dispatched": False, "reason": "submit_audit_gate_off"}, "src-1"
        ) == ""


# ── 5. flag 判据 + 工具描述契约装配 ──────────────────────────


class TestFlagSemantics:
    def test_enabled_predicate_variants(self):
        with patch.object(settings, "submit_audit_gate", "on"):
            assert submit_audit_gate_enabled() is True
        with patch.object(settings, "submit_audit_gate", " ON "):
            assert submit_audit_gate_enabled() is True
        with patch.object(settings, "submit_audit_gate", "off"):
            assert submit_audit_gate_enabled() is False
        with patch.object(settings, "submit_audit_gate", ""):
            assert submit_audit_gate_enabled() is False
        with patch.object(settings, "submit_audit_gate", "garbage"):
            assert submit_audit_gate_enabled() is False  # 非法值按 off

    def test_description_off_has_no_audit_gate_clause(self):
        # 描述在导入期按默认 off 装配：不含旧门条款，也不漏审计散文
        assert "CODE AUDIT GATE" not in SUBMIT_TASK_TOOL_DESCRIPTION
        assert "SYSTEM AUDIT" not in SUBMIT_TASK_TOOL_DESCRIPTION
        # 任务级契约仍在
        assert "merge_conflict_with_main" in SUBMIT_TASK_TOOL_DESCRIPTION
        assert "Only the assignee can submit" in SUBMIT_TASK_TOOL_DESCRIPTION
        # on 装配（纯函数）恢复旧门全文
        on_desc = build_submit_task_description(audit_gate=True)
        assert "CODE AUDIT GATE" in on_desc
        assert "SYSTEM AUDIT" in on_desc
        assert "waive_attestation" in on_desc
        # 两态共享同一 base（B6 单源），仅审计条款不同
        off_desc = build_submit_task_description(audit_gate=False)
        assert SUBMIT_TASK_TOOL_DESCRIPTION == off_desc  # 导入期=off 装配
        assert on_desc != off_desc
        head = "Submit for review (running → submitted)."
        tail = "Only the assignee can submit."
        assert on_desc.startswith(head) and off_desc.startswith(head)
        assert on_desc.endswith(tail) and off_desc.endswith(tail)
