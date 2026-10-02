"""T18 — SYSTEM AUDIT 卡两个集成 bug 修复回归（S16 三张卡全卡死的根因）。

钉住的契约：

bug1（reviewer 错配）：
  旧默认把卡（creator=被审 submitter）的 reviewer 钉成 creator——被审
  executor 无 ``review_task`` 工具 ⇒ 卡死 submitted。修复后卡的 reviewer
  必须是**有 review 权限的角色**（源任务 review obligation owner 链：
  源任务 reviewer → 源任务 creator → qa_lead → 卡 assignee 的 parent），
  且绝不能是被审 executor、也不能是卡的 assignee（review_task 禁自审）。

bug2（凭证不回写）：
  审计者把代审凭证挂在了**卡**上（taskId=卡 id）时，卡被 approve 关闭后
  源任务的 submit 门纹丝不动。修复后卡 approve 收口把卡上代审产生的
  code_audit 凭证按 ``authorId=submitter`` 既有绑定语义回写源任务 ⇒
  源任务重提过门。幂等：源任务上已有同指纹凭证（正确代审路径落的或
  早前已回写的）⇒ 不重复。

fixture/DB 模式沿用 tests/test_t1_audit_dispatch.py（临时 per-project DB +
meta 路由 patch + 真实 agents 表行 + git 全打桩）。

批3（2026-09-29）：SUBMIT_AUDIT_GATE 默认 off（逐提交审计门已拆）。本文件
的铸造前置（``_mint_card``）与历史卡收口在 fixture 里显式 flag=on / 直接调
audit_dispatch（ mint 短路只挡新铸造，卡判别/回写不受 flag 影响）。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.config import settings
from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services import task as task_module
from hiveweave.services.attestation import attestation_service
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.audit_dispatch import (
    is_system_audit_card,
    rebind_audit_attestations_on_approve,
    resolve_card_reviewer,
)
from hiveweave.tools.tasks.submit import SubmitTaskParams, submit_task_tool

PROJECT_ID = "test-t18-audit-card-fixes"
COORD_ID = "t18-coordinator"
EXEC_ID = "t18-executor"
QA_LEAD_ID = "t18-qa-lead"
CEO_ID = "t18-ceo"
ALL_AGENTS = (COORD_ID, EXEC_ID, QA_LEAD_ID, CEO_ID)


@pytest.fixture
async def env():
    """临时 per-project DB + meta 路由 patch（同 test_t1 模式）。

    组织：coordinator（源任务 creator，即源任务 review obligation owner）
    + executor（被审 submitter）+ qa_lead（审计卡执行者）。
    """
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
             patch.object(settings, "submit_audit_gate", "on"):
            _env = {"workspace_path": workspace_path}
            await _insert_agent(
                _env, COORD_ID, "架构师",
                role="coordinator", permission_type="coordinator", parent_id=None,
            )
            await _insert_agent(
                _env, EXEC_ID, "霜叶",
                role="engineer", permission_type="executor", parent_id=COORD_ID,
            )
            await _insert_agent(
                _env, QA_LEAD_ID, "验真",
                role="qa_lead", permission_type="coordinator", parent_id=None,
            )
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

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
    permission_type: str = "executor", parent_id: str | None = None,
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


async def _create_source_task(env, *, assignee_id: str = EXEC_ID) -> str:
    """建 code_audit policy 的源任务并 claim+start（creator=COORD=审查 owner）。"""
    svc = TaskService()
    tid = await svc.create_task(
        project_id=PROJECT_ID, title="实现导出功能", description="d",
        creator_id=COORD_ID, tags=["code_audit"],
    )
    if assignee_id:
        await svc.claim_task(PROJECT_ID, tid, assignee_id)
        await svc.start_task(PROJECT_ID, tid)
    return tid


async def _git_fail_all(args, cwd, timeout: float = 30.0, project_root=None):
    return False, ""


async def _submit_tool(env, params: SubmitTaskParams, caller: str = EXEC_ID):
    async def fake_get_project_id(aid: str):
        return PROJECT_ID if aid in ALL_AGENTS else None

    with patch("hiveweave.tools.helpers.get_project_id", fake_get_project_id), \
         patch("hiveweave.agents.trigger.trigger_coordinator", AsyncMock()), \
         patch("hiveweave.agents.trigger.trigger_subordinate", AsyncMock()), \
         patch("hiveweave.services.git_worktree._git", _git_fail_all):
        return await submit_task_tool(params, caller, env["workspace_path"])


async def _mint_card(env, tid: str) -> str:
    """审计门触发铸造卡，返回卡 id（submit 被拒是铸造的前置）。"""
    result = await _submit_tool(
        env, SubmitTaskParams(taskId=tid, summary="big diff", testsPassed=True)
    )
    assert result.success is False
    receipt = result.to_dict()
    assert receipt["audit_dispatched"] is True
    return str(receipt["audit_task_id"])


async def _card_to_reviewing(env, card_id: str) -> None:
    """审计者（qa_lead）start + submit 审计卡 ⇒ 卡进入待审。"""
    svc = TaskService()
    await svc.start_task(PROJECT_ID, card_id)
    await svc.submit_task(
        PROJECT_ID, card_id, {"summary": "audit verdict=PASS"}
    )


async def _attestation_count(env, task_id: str, agent_id: str) -> int:
    conn = await ensure_project_db(env["workspace_path"])
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM tool_attestations "
        "WHERE project_id = ? AND task_id = ? AND agent_id = ? "
        "AND kind = 'code_audit'",
        [PROJECT_ID, str(task_id).replace("-", "").lower(), agent_id],
    )
    row = await cur.fetchone()
    await cur.close()
    return int(row["n"])


# ── bug1：卡的 reviewer 是有 review 权限的角色，不是被审 executor ──


class TestCardReviewerAssignment:
    async def test_card_reviewer_is_source_owner_not_auditee(self, env):
        """端到端：卡 submit ⇒ reviewer 钉源任务 owner（COORD），非被审 executor。"""
        tid = await _create_source_task(env)
        card_id = await _mint_card(env, tid)
        await _card_to_reviewing(env, card_id)

        svc = TaskService()
        card = await svc.get_task(PROJECT_ID, card_id)
        assert card["status"] == "submitted"
        assert card["reviewer_id"] == COORD_ID  # 源任务 creator（持 REVIEW）
        assert card["reviewer_id"] != EXEC_ID  # 绝不是被审对象（无 review_task）

    async def test_card_reviewer_survives_resubmit(self, env):
        """rework 后卡重提：重解析仍给出有 review 权限的角色（不回退 executor）。"""
        tid = await _create_source_task(env)
        card_id = await _mint_card(env, tid)
        svc = TaskService()
        await _card_to_reviewing(env, card_id)
        await svc.start_review(PROJECT_ID, card_id, reviewer_id=COORD_ID)
        await svc.review_task(
            PROJECT_ID, card_id, "rework",
            feedback="补审计明细 src/audit.py",
            reviewer_id=COORD_ID,
        )
        assert (await svc.get_task(PROJECT_ID, card_id))["status"] == "running"
        await svc.submit_task(
            PROJECT_ID, card_id, {"summary": "audit verdict=PASS v2"}
        )
        card = await svc.get_task(PROJECT_ID, card_id)
        assert card["reviewer_id"] == COORD_ID
        assert card["reviewer_id"] != EXEC_ID

    async def test_resolve_chain_prefers_source_reviewer_then_creator(self, env):
        """链单测：源任务已钉 reviewer 优先；否则 creator；被审 executor 永不入选。"""
        svc = TaskService()
        tid = await _create_source_task(env)

        rev, chain = await resolve_card_reviewer(
            PROJECT_ID, card_assignee_id=QA_LEAD_ID,
            submitter_id=EXEC_ID, source_task_id=tid,
        )
        assert chain == "source_creator"
        assert rev is not None and rev["id"] == COORD_ID

        # 源任务钉了 reviewer ⇒ 优先取已钉的
        from hiveweave.services.tasks.db import _execute

        now = int(time.time() * 1000)
        await _execute(
            PROJECT_ID, "UPDATE tasks SET reviewer_id = ?, updated_at = ? "
            "WHERE id = ?", [QA_LEAD_ID, now, tid],
        )
        rev2, chain2 = await resolve_card_reviewer(
            PROJECT_ID, card_assignee_id=COORD_ID,
            submitter_id=EXEC_ID, source_task_id=tid,
        )
        assert chain2 == "source_reviewer"
        assert rev2 is not None and rev2["id"] == QA_LEAD_ID

    async def test_resolve_chain_falls_to_qa_lead_when_creator_is_assignee(self, env):
        """源任务 creator 恰为卡的 assignee（禁自审）⇒ 跳过，落到 qa_lead。"""
        svc = TaskService()
        tid = await svc.create_task(
            project_id=PROJECT_ID, title="t", description="d",
            creator_id=COORD_ID, tags=["code_audit"],
        )
        rev, chain = await resolve_card_reviewer(
            PROJECT_ID, card_assignee_id=COORD_ID,
            submitter_id=EXEC_ID, source_task_id=tid,
        )
        assert chain == "qa_lead"
        assert rev is not None and rev["id"] == QA_LEAD_ID

    async def test_resolve_chain_archived_source_creator_skipped(self, env):
        """源任务 creator 已归档 ⇒ 跳过（不能把卡派给死人审）。"""
        await _insert_agent(
            env, "t18-ghost", "幽灵", role="coordinator",
            permission_type="coordinator", parent_id=None, status="archived",
        )
        svc = TaskService()
        tid = await svc.create_task(
            project_id=PROJECT_ID, title="t", description="d",
            creator_id="t18-ghost", tags=["code_audit"],
        )
        # 卡的 assignee 是 executor 形态（回退链把卡派给了非 qa_lead）⇒
        # qa_lead 可作为补充审查者入选。
        rev, chain = await resolve_card_reviewer(
            PROJECT_ID, card_assignee_id=EXEC_ID,
            submitter_id=QA_LEAD_ID, source_task_id=tid,
        )
        assert chain == "qa_lead"
        assert rev is not None and rev["id"] == QA_LEAD_ID

    async def test_resolve_chain_last_resort_assignee_parent(self, env):
        """源任务 creator/qa_lead 都恰为卡 assignee（禁自审双双排除）⇒
        保底落 assignee 的 parent（qa_lead 上级通常为 CEO）。"""
        # 给 qa_lead 挂一个 CEO 上级（生产形态：qa_lead 归 CEO 管）
        conn = await ensure_project_db(env["workspace_path"])
        await conn.execute(
            "INSERT INTO agents (id, project_id, name, role, parent_id, "
            "status, permission_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, 'active', 'coordinator', ?, ?)",
            [CEO_ID, PROJECT_ID, "厂长", "ceo", int(time.time() * 1000),
             int(time.time() * 1000)],
        )
        await conn.execute(
            "UPDATE agents SET parent_id = ? WHERE id = ?",
            [CEO_ID, QA_LEAD_ID],
        )
        await conn.commit()
        svc = TaskService()
        tid = await svc.create_task(
            project_id=PROJECT_ID, title="t", description="d",
            creator_id=QA_LEAD_ID, tags=["code_audit"],
        )
        rev, chain = await resolve_card_reviewer(
            PROJECT_ID, card_assignee_id=QA_LEAD_ID,
            submitter_id=EXEC_ID, source_task_id=tid,
        )
        assert chain == "assignee_parent"
        assert rev is not None and rev["id"] == CEO_ID

    def test_is_system_audit_card_predicate(self):
        assert is_system_audit_card(
            {"title": "SYSTEM AUDIT: x", "tags": "[]"}
        )
        assert is_system_audit_card(
            {"title": "随便", "tags": json.dumps(["system-audit"])}
        )
        assert is_system_audit_card(
            {"title": "随便", "tags": ["system-audit"]}
        )
        # 标题系统前缀优先于坏 tags（前缀即系统标记）
        assert is_system_audit_card(
            {"title": "SYSTEM AUDIT: x", "tags": "bad-json"}
        )
        assert not is_system_audit_card({"title": "普通任务", "tags": "[]"})
        assert not is_system_audit_card({"title": "普通任务", "tags": "bad-json"})
        assert not is_system_audit_card(None)


# ── bug2：卡 approve ⇒ 凭证回写源任务 ⇒ 源任务重提过门 ────────


class TestApproveRebindsAttestation:
    async def test_approve_rebinds_card_attestation_and_opens_gate(self, env):
        """端到端：凭证挂在卡上（S16 现场）⇒ approve ⇒ 源任务查得凭证 ⇒ 重提过门。"""
        tid = await _create_source_task(env)
        card_id = await _mint_card(env, tid)

        # 审计者把 PASS 凭证错挂在卡上（taskId=卡 id，authorId=submitter）
        att_id = await attestation_service.create(
            PROJECT_ID,
            agent_id=EXEC_ID,
            kind="code_audit",
            task_id=card_id,
            exit_code=0,
            workspace=r"C:\fake\wt",
            stdout="VERDICT: PASS",
        )
        assert await _attestation_count(env, tid, EXEC_ID) == 0  # 源任务上还没有

        await _card_to_reviewing(env, card_id)
        svc = TaskService()
        await svc.start_review(PROJECT_ID, card_id, reviewer_id=COORD_ID)
        await svc.review_task(
            PROJECT_ID, card_id, "approve", feedback="audit ok",
            reviewer_id=COORD_ID,
        )
        assert (await svc.get_task(PROJECT_ID, card_id))["status"] == "approved"

        # ① 源任务可查得 code_audit 凭证（authorId=submitter）
        ids = await attestation_service.find_recent_for_agent(
            PROJECT_ID, agent_id=EXEC_ID, task_id=tid, kinds=["code_audit"],
        )
        assert ids, "approve 后源任务必须查得回写的 code_audit 凭证"
        row = await attestation_service.get(PROJECT_ID, ids[0])
        assert row is not None and row["agent_id"] == EXEC_ID
        assert row["exit_code"] == 0

        # ② 源任务重提过门
        second = await _submit_tool(
            env, SubmitTaskParams(
                taskId=tid, summary="big diff", testsPassed=True,
            )
        )
        assert second.success is True, second.error
        assert (await svc.get_task(PROJECT_ID, tid))["status"] == "submitted"

    async def test_rebind_skips_issues_and_foreign_agent_attestations(self, env):
        """ISSUES（exit≠0）与审计者自己的凭证都不回写——只有可开门的代审 PASS。"""
        tid = await _create_source_task(env)
        card_id = await _mint_card(env, tid)

        await attestation_service.create(
            PROJECT_ID, agent_id=EXEC_ID, kind="code_audit", task_id=card_id,
            exit_code=1, workspace=r"C:\fake\wt", stdout="VERDICT: ISSUES",
        )
        await attestation_service.create(
            PROJECT_ID, agent_id=QA_LEAD_ID, kind="code_audit", task_id=card_id,
            exit_code=0, workspace=r"C:\fake\wt", stdout="VERDICT: PASS",
        )
        card = await TaskService().get_task(PROJECT_ID, card_id)
        rebound = await rebind_audit_attestations_on_approve(PROJECT_ID, card)
        assert rebound == []
        assert await _attestation_count(env, tid, EXEC_ID) == 0


# ── 幂等：同凭证不重复 ────────────────────────────────────────


class TestRebindIdempotency:
    async def test_second_rebind_is_noop(self, env):
        """approve 后再跑回写（重复收口）⇒ 零新增。"""
        tid = await _create_source_task(env)
        card_id = await _mint_card(env, tid)
        await attestation_service.create(
            PROJECT_ID, agent_id=EXEC_ID, kind="code_audit", task_id=card_id,
            exit_code=0, workspace=r"C:\fake\wt", stdout="VERDICT: PASS",
        )
        await _card_to_reviewing(env, card_id)
        svc = TaskService()
        await svc.start_review(PROJECT_ID, card_id, reviewer_id=COORD_ID)
        await svc.review_task(
            PROJECT_ID, card_id, "approve", feedback="ok", reviewer_id=COORD_ID,
        )
        n_after_approve = await _attestation_count(env, tid, EXEC_ID)
        assert n_after_approve == 1

        card = await svc.get_task(PROJECT_ID, card_id)
        rebound = await rebind_audit_attestations_on_approve(PROJECT_ID, card)
        assert rebound == []
        assert await _attestation_count(env, tid, EXEC_ID) == n_after_approve

    async def test_existing_delegated_attestation_not_duplicated(self, env):
        """正确代审路径已在源任务落了同指纹凭证 ⇒ approve 回写不重复。"""
        tid = await _create_source_task(env)
        card_id = await _mint_card(env, tid)
        # 正确路径：凭证直接绑（源任务， submitter），与卡上那条同 stdout 指纹
        await attestation_service.create(
            PROJECT_ID, agent_id=EXEC_ID, kind="code_audit", task_id=tid,
            exit_code=0, workspace=r"C:\fake\wt", stdout="VERDICT: PASS",
        )
        # 卡上也有一条（审计者重复挂错）
        await attestation_service.create(
            PROJECT_ID, agent_id=EXEC_ID, kind="code_audit", task_id=card_id,
            exit_code=0, workspace=r"C:\fake\wt", stdout="VERDICT: PASS",
        )
        await _card_to_reviewing(env, card_id)
        svc = TaskService()
        await svc.start_review(PROJECT_ID, card_id, reviewer_id=COORD_ID)
        await svc.review_task(
            PROJECT_ID, card_id, "approve", feedback="ok", reviewer_id=COORD_ID,
        )
        assert await _attestation_count(env, tid, EXEC_ID) == 1  # 无重复
