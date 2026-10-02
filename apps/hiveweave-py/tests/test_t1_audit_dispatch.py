"""T1 审查/审计派单化 — submit 门触发的 SYSTEM AUDIT 铸造链路回归。

钉住的契约（模块 ``services/tasks/audit_dispatch.py`` + ``tools/tasks/submit.py``
接线 + ``tools/code_audit.py`` authorId 代审参数）：

1. 端到端：审计门（policy 要求 code_audit 且未满足）触发 ⇒ submit 仍被拒，
   但平台铸造 SYSTEM AUDIT 任务（assignee=qa_lead、creator=submitter、
   source=system、policy=coordinator_review、evidence.source_task_id 指回源任务），
   拒收回执带 ``[AUDIT TASK DISPATCHED]`` 区块 + waiting_on 处方 + 派单通知。
2. 幂等：同一 (submitter, 源任务) 重提 ⇒ 复用进行中的审计任务，不重铸；
   审计任务 submitted（结论已交付）后不再命中 ⇒ 允许铸新。
3. 回退链：qa_lead → submitter 的 parent coordinator → 显式
   ``no_reviewer_available``（不静默吞）；qa_lead 恰为 submitter / 已归档时跳过。
4. ≤ 审计门槛已满足（有效 code_audit 凭证可自动附上）⇒ 不铸任务直过。
5. 代审：审查池成员以 ``authorId=<submitter>`` 调 request_code_audit ⇒
   审计跑在**被审者**的 worktree 上、凭证绑**被审者**与其任务；普通成员
   带authorId 被拒（runner_failed）。
6. 单轨红线：submit 主路径**零同步审计 LLM 调用**（run_code_audit 不被触达）。
7. 凭证落地开门：审计者落 PASS 凭证后源任务重提 ⇒ find_recent_for_agent
   自动附凭证 ⇒ 门开（源任务 submitted，且未再铸新审计任务）。

fixture/DB 模式沿用 tests/test_batch2_a4_a6（临时 per-project DB + meta 路由
patch）与 tests/test_dsh77（真实 agents 表行）；git 全部打桩保证封闭。

批3（2026-09-29）：SUBMIT_AUDIT_GATE 默认 off（逐提交审计门已拆）。本文件
测的是「门触发铸造」**机制**——fixture 显式置 flag=on 运行；机制保留可回切，
默认路径（off）不铸造，行为见 tests/test_submit_audit_gate_flag.py。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.config import settings
from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services import task as task_module
from hiveweave.services.attestation import attestation_service
from hiveweave.services.task import TaskService
from hiveweave.tools.code_audit import RequestCodeAuditParams, request_code_audit_tool
from hiveweave.tools.tasks.submit import SubmitTaskParams, submit_task_tool

PROJECT_ID = "test-t1-audit-dispatch"
COORD_ID = "t1-coordinator"
EXEC_ID = "t1-executor"
QA_LEAD_ID = "t1-qa-lead"
LONER_ID = "t1-loner"  # 单兵形态：无 qa_lead、无 parent coordinator 的 submitter
AUDIT_PREFIX = "SYSTEM AUDIT: "
ALL_AGENTS = (COORD_ID, EXEC_ID, QA_LEAD_ID, LONER_ID)


@pytest.fixture
async def env():
    """临时 per-project DB + meta 路由 patch（同 test_batch2 / test_dsh77 模式）。

    默认插入 coordinator + executor；qa_lead 由需要审查池的用例显式插入
    （回退链用例需要「无 qa_lead」的空池形态）。fake ``get_agent_by_id``
    直接读同一张 per-project agents 表 —— 与 ``get_agent_by_role`` 单一事实源。
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
            # 默认组织：coordinator + executor（submitter）。qa_lead 由需要
            # 审查池的用例显式插入（回退链用例需要「无 qa_lead」空池形态）。
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


# ── Helpers ─────────────────────────────────────────────────


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


async def _insert_qa_lead(env, *, status: str = "active") -> None:
    """按生产形态插入 qa_lead（api/projects.py 建项种子：role=qa_lead,
    permission_type=coordinator）。"""
    await _insert_agent(
        env, QA_LEAD_ID, "验真", role="qa_lead",
        permission_type="coordinator", status=status,
    )


async def _create_source_task(
    env, *, assignee_id: str = EXEC_ID, tags: list[str] | None = None,
    title: str = "实现导出功能",
) -> str:
    """建 code_audit policy 的源任务并 claim+start（submit 前置状态）。"""
    svc = TaskService()
    tid = await svc.create_task(
        project_id=PROJECT_ID, title=title, description="d",
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
    """submit_task_tool 全封闭执行：meta 路由 + 唤醒链打桩 + git 打桩。"""

    async def fake_get_project_id(aid: str):
        return PROJECT_ID if aid in ALL_AGENTS else None

    with patch("hiveweave.tools.helpers.get_project_id", fake_get_project_id), \
         patch("hiveweave.agents.trigger.trigger_coordinator", AsyncMock()), \
         patch("hiveweave.agents.trigger.trigger_subordinate", AsyncMock()), \
         patch("hiveweave.services.git_worktree._git", _git_fail_all):
        return await submit_task_tool(params, caller, env["workspace_path"])


async def _audit_task_rows(env) -> list[dict]:
    conn = await ensure_project_db(env["workspace_path"])
    cur = await conn.execute(
        "SELECT id, title, status, assignee_id, creator_id, source, policy_id, "
        "evidence FROM tasks WHERE title LIKE ? ORDER BY created_at ASC",
        [AUDIT_PREFIX + "%"],
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _audit_inbox_count(env, agent_id: str = QA_LEAD_ID) -> int:
    row = await project_db.query_one(
        agent_id,
        "SELECT COUNT(*) AS n FROM inbox WHERE to_agent_id = ? "
        "AND message LIKE '%[SYSTEM AUDIT]%'",
        [agent_id],
    )
    return int(row["n"])


def _mint_params(tid: str) -> SubmitTaskParams:
    return SubmitTaskParams(taskId=tid, summary="big diff landed", testsPassed=True)


# ── 1+2. 端到端：铸造 + 回执 + 幂等重提 ──────────────────────


class TestGateMintE2E:
    async def test_gate_reject_mints_audit_task_with_receipt(self, env):
        """>审计门 submit：任务被拒 + 铸 SYSTEM AUDIT 任务 + 回执带派单区块。"""
        await _insert_qa_lead(env)
        tid = await _create_source_task(env)

        result = await _submit_tool(env, _mint_params(tid))

        assert result.success is False
        receipt = result.to_dict()
        assert receipt["audit_dispatched"] is True
        assert receipt["audit_task_id"]
        assert receipt["audit_reviewer_id"] == QA_LEAD_ID
        minted_id = receipt["audit_task_id"]

        rows = await _audit_task_rows(env)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == minted_id
        assert row["assignee_id"] == QA_LEAD_ID
        assert row["creator_id"] == EXEC_ID  # creator=submitter ⇒ 唤醒链成立
        assert (row["source"] or "") == "system"
        assert row["status"] == "claimed"  # assign=claim，审计者直接可 start
        assert (row["policy_id"] or "") == "coordinator_review"  # 防递归门
        ev = json.loads(row["evidence"])
        assert ev["source_task_id"] == tid
        assert ev["submitter_id"] == EXEC_ID
        assert ev["dispatched_by"] == "submit_gate"

        text = result.error or ""
        assert "[AUDIT TASK DISPATCHED]" in text
        assert minted_id in text
        # 结构化收尾处方（非 prose）——按模式分流（T16 三期）：两种模式都
        # 必须带 task 引用；auto 用 wait_on(refs=…)、legacy 用 waiting_on。
        assert '"kind": "task"' in text
        assert ("wait_on(refs=" in text) or ("waiting_on" in text)
        # 派单通知落审计者收件箱
        assert await _audit_inbox_count(env) == 1

        # submit 被拒 ≠ 状态变化：源任务仍 running，等凭证落地后重提
        task = await TaskService().get_task(PROJECT_ID, tid)
        assert task["status"] == "running"

    async def test_resubmit_reuses_open_audit_task(self, env):
        """幂等：同 (submitter, 源任务) 重提 ⇒ 复用进行中审计任务，不重铸。"""
        await _insert_qa_lead(env)
        tid = await _create_source_task(env)

        first = await _submit_tool(env, _mint_params(tid))
        assert first.success is False
        minted_id = first.to_dict()["audit_task_id"]

        second = await _submit_tool(env, _mint_params(tid))
        assert second.success is False
        receipt = second.to_dict()
        assert receipt["audit_dispatched"] is True
        assert receipt["audit_task_id"] == minted_id
        assert "复用进行中任务" in (second.error or "")
        assert len(await _audit_task_rows(env)) == 1  # 没有第二条
        # 复用路径不重发派单通知
        assert await _audit_inbox_count(env) == 1

    async def test_submitted_audit_task_no_longer_blocks_remint(self, env):
        """已 submitted（结论已交付）的审计任务不算 open ⇒ find_open 返回 None。

        （ISSUES 返工场景下重提源任务要审**新** diff，必须允许铸新任务。）
        """
        from hiveweave.services.tasks.audit_dispatch import find_open_audit_task

        await _insert_qa_lead(env)
        tid = await _create_source_task(env)
        first = await _submit_tool(env, _mint_params(tid))
        audit_id = first.to_dict()["audit_task_id"]

        found = await find_open_audit_task(PROJECT_ID, tid, EXEC_ID)
        assert found is not None and found["id"] == audit_id

        svc = TaskService()
        await svc.start_task(PROJECT_ID, audit_id)
        await svc.submit_task(
            PROJECT_ID, audit_id, {"summary": "audit verdict=ISSUES"}
        )
        found_after = await find_open_audit_task(PROJECT_ID, tid, EXEC_ID)
        assert found_after is None


# ── 3. 回退链 ───────────────────────────────────────────────


class TestReviewerFallbackChain:
    async def test_qa_lead_first(self, env):
        await _insert_qa_lead(env)
        from hiveweave.services.tasks.audit_dispatch import resolve_reviewer

        reviewer, chain = await resolve_reviewer(PROJECT_ID, EXEC_ID)
        assert chain == "qa_lead"
        assert reviewer is not None and reviewer["id"] == QA_LEAD_ID

    async def test_no_qa_lead_falls_back_to_parent_coordinator(self, env):
        """无 qa_lead ⇒ 回退 submitter 的 parent coordinator（中层在即可）。"""
        from hiveweave.services.tasks.audit_dispatch import resolve_reviewer

        reviewer, chain = await resolve_reviewer(PROJECT_ID, EXEC_ID)
        assert chain == "parent_coordinator"
        assert reviewer is not None and reviewer["id"] == COORD_ID

    async def test_qa_lead_is_submitter_self_excluded(self, env):
        """submitter 恰为 qa_lead（不自我审计）⇒ 跳过，回退 parent。"""
        await _insert_qa_lead(env)
        from hiveweave.services.tasks.audit_dispatch import resolve_reviewer

        reviewer, chain = await resolve_reviewer(PROJECT_ID, QA_LEAD_ID)
        assert chain == "parent_coordinator"
        assert reviewer is not None and reviewer["id"] == COORD_ID

    async def test_archived_qa_lead_skipped(self, env):
        await _insert_qa_lead(env, status="archived")
        from hiveweave.services.tasks.audit_dispatch import resolve_reviewer

        reviewer, chain = await resolve_reviewer(PROJECT_ID, EXEC_ID)
        assert chain == "parent_coordinator"
        assert reviewer is not None and reviewer["id"] == COORD_ID

    async def test_empty_chain_explicit_no_reviewer(self, env):
        """单兵 78 形态（无 qa_lead、submitter 无 parent）⇒ 显式 no_reviewer。"""
        from hiveweave.services.tasks.audit_dispatch import resolve_reviewer

        await _insert_agent(
            env, LONER_ID, "独狼", role="executor",
            permission_type="executor", parent_id=None,
        )
        reviewer, chain = await resolve_reviewer(PROJECT_ID, LONER_ID)
        assert reviewer is None
        assert chain == "no_reviewer_available"


class TestNoReviewerReceipt:
    async def test_no_reviewer_receipt_names_gap_and_mints_nothing(self, env):
        """走空 ⇒ 回执显式点名（不静默吞），且不铸造任何审计任务。"""
        await _insert_agent(
            env, LONER_ID, "独狼", role="executor",
            permission_type="executor", parent_id=None,
        )
        tid = await _create_source_task(env, assignee_id=LONER_ID)

        result = await _submit_tool(
            env, _mint_params(tid), caller=LONER_ID
        )

        assert result.success is False
        receipt = result.to_dict()
        assert not receipt.get("audit_dispatched")
        assert receipt.get("audit_task_id") is None
        text = result.error or ""
        assert "[AUDIT DISPATCH FAILED]" in text
        assert "waive_attestation" in text  # 原豁免出口保持可见
        assert await _audit_task_rows(env) == []
        task = await TaskService().get_task(PROJECT_ID, tid)
        assert task["status"] == "running"


# ── 回执区块纯函数契约 ───────────────────────────────────────


class TestDispatchReceiptBlock:
    def test_dispatched_block_contains_waiting_on_prescription(self):
        """收尾处方按模式分流（T16 三期）：auto ⇒ wait_on（禁 commit_turn
        处方）；legacy ⇒ commit_turn(waiting, waiting_on=[task 引用])。"""
        from hiveweave.config import settings as _settings
        from hiveweave.services.tasks.audit_dispatch import (
            format_dispatch_receipt_block,
        )

        mint = {
            "dispatched": True, "reused": False, "task_id": "audit-1",
            "title": "SYSTEM AUDIT: x", "reviewer_id": "r1",
            "reviewer_name": "验真",
        }
        original = _settings.turn_exit_mode
        try:
            for mode in ("auto", "legacy"):
                _settings.turn_exit_mode = mode
                block = format_dispatch_receipt_block(mint, "src-1")
                assert "[AUDIT TASK DISPATCHED]" in block
                assert "audit-1" in block and "src-1" in block
                assert "验真" in block
                if mode == "auto":
                    assert (
                        'wait_on(refs=[{"kind": "task", "ref": "audit-1"}])'
                        in block
                    )
                    assert "commit_turn(phase" not in block
                else:
                    assert (
                        'waiting_on=[{"kind": "task", "ref": "audit-1"}]'
                        in block
                    )
        finally:
            _settings.turn_exit_mode = original

    def test_reused_block_marks_reuse(self):
        from hiveweave.services.tasks.audit_dispatch import (
            format_dispatch_receipt_block,
        )

        block = format_dispatch_receipt_block(
            {"dispatched": True, "reused": True, "task_id": "audit-1",
             "title": "t", "reviewer_id": "r1", "reviewer_name": "验真"},
            "src-1",
        )
        assert "复用进行中任务" in block

    def test_no_reviewer_block_explicit(self):
        from hiveweave.services.tasks.audit_dispatch import (
            format_dispatch_receipt_block,
        )

        block = format_dispatch_receipt_block(
            {"dispatched": False, "reason": "no_reviewer_available"}, "src-1"
        )
        assert "[AUDIT DISPATCH FAILED]" in block
        assert "waive_attestation" in block

    def test_error_block_carries_detail(self):
        from hiveweave.services.tasks.audit_dispatch import (
            format_dispatch_receipt_block,
        )

        block = format_dispatch_receipt_block(
            {"dispatched": False, "reason": "error", "detail": "boom"}, "src-1"
        )
        assert "[AUDIT DISPATCH FAILED]" in block
        assert "boom" in block

    def test_empty_mint_is_empty_string(self):
        from hiveweave.services.tasks.audit_dispatch import (
            format_dispatch_receipt_block,
        )

        assert format_dispatch_receipt_block({}, "src-1") == ""
        assert format_dispatch_receipt_block(None, "src-1") == ""


# ── 4. 门槛已满足（≤ 审计要求）⇒ 不铸直过 ────────────────────


class TestGateSatisfiedNoMint:
    async def test_valid_audit_attestation_passes_without_mint(self, env):
        """code_audit 凭证在手（可自动附上）⇒ 门开，直过，零铸造。"""
        await _insert_qa_lead(env)
        tid = await _create_source_task(env)
        await attestation_service.create(
            PROJECT_ID,
            agent_id=EXEC_ID,
            kind="code_audit",
            task_id=tid,
            exit_code=0,
            workspace=str(env["workspace_path"]),
            stdout="VERDICT: PASS",
        )

        result = await _submit_tool(
            env, SubmitTaskParams(
                taskId=tid, summary="small change", testsPassed=True,
            )
        )

        assert result.success is True, result.error
        assert await _audit_task_rows(env) == []
        task = await TaskService().get_task(PROJECT_ID, tid)
        assert task["status"] == "submitted"


# ── 5. 代审（authorId 参数路径）──────────────────────────────


def _audit_git_stub(head_sha: str = "a" * 40):
    """空 diff 桩：main 可解析、各 diff 全空 ⇒ run_code_audit 走 auto-PASS。"""
    table = {
        ("rev-parse", "--verify", "refs/heads/main"): (True, ""),
        ("diff", "main...HEAD"): (True, ""),
        ("diff", "HEAD"): (True, ""),
        ("ls-files", "--others", "--exclude-standard"): (True, ""),
        ("rev-parse", "HEAD"): (True, head_sha),
    }

    async def fake_git(args, cwd, timeout: float = 30.0, project_root=None):
        return table.get(tuple(args), (False, ""))

    return fake_git


class TestOnBehalfAudit:
    async def test_reviewer_on_behalf_binds_submitter(self, env):
        """qa_lead 以 authorId=submitter 代审：审计跑在被审者 worktree 上，
        凭证绑被审者 + 源任务（submit 门的既有判定原样消费）。"""
        await _insert_qa_lead(env)
        wt_mock = AsyncMock(return_value=r"C:\fake\wt")
        att_svc = MagicMock()
        att_svc.create = AsyncMock(return_value="att-onbehalf-1")
        prior_mock = AsyncMock(return_value=None)

        async def fake_get_project_id(aid: str):
            return PROJECT_ID if aid in ALL_AGENTS else None

        with patch("hiveweave.tools.helpers.get_project_id",
                   fake_get_project_id), \
             patch("hiveweave.services.worktree_review.agent_worktree_path",
                   wt_mock), \
             patch("hiveweave.services.git_worktree._git", _audit_git_stub()), \
             patch("hiveweave.services.attestation.attestation_service",
                   att_svc), \
             patch("hiveweave.services.attestation.find_latest_attestation_by_kind",
                   prior_mock):
            result = await request_code_audit_tool(
                RequestCodeAuditParams(taskId="task-src-1", authorId=EXEC_ID),
                QA_LEAD_ID,
                env["workspace_path"],
            )

        assert result.success is True, result.error
        assert "PASS" in (result.output or "")
        assert "att-onbehalf-1" in (result.output or "")
        # 审计的是**被审者**的 worktree（不是审计者自己的）
        assert wt_mock.await_args_list[0].args[0] == EXEC_ID
        kw = att_svc.create.await_args.kwargs
        assert kw["agent_id"] == EXEC_ID  # 凭证绑 submitter
        assert kw["task_id"] == "task-src-1"  # 绑源任务
        assert kw["kind"] == "code_audit"
        assert kw["exit_code"] == 0

    async def test_plain_executor_cannot_use_author_id(self, env):
        """普通成员带 authorId ⇒ runner_failed（代审仅审查池开放）。"""
        await _insert_qa_lead(env)

        async def fake_get_project_id(aid: str):
            return PROJECT_ID if aid in ALL_AGENTS else None

        with patch("hiveweave.tools.helpers.get_project_id",
                   fake_get_project_id):
            result = await request_code_audit_tool(
                RequestCodeAuditParams(taskId="t1", authorId=COORD_ID),
                EXEC_ID,
                env["workspace_path"],
            )

        assert result.success is False
        assert result.fact == "runner_failed"
        assert "审查池" in (result.error or "")

    async def test_unknown_author_id_rejected(self, env):
        await _insert_qa_lead(env)

        async def fake_get_project_id(aid: str):
            return PROJECT_ID if aid in ALL_AGENTS else None

        with patch("hiveweave.tools.helpers.get_project_id",
                   fake_get_project_id):
            result = await request_code_audit_tool(
                RequestCodeAuditParams(taskId="t1", authorId="t1-ghost"),
                QA_LEAD_ID,
                env["workspace_path"],
            )

        assert result.success is False
        assert result.fact == "runner_failed"
        assert "authorId not found" in (result.error or "")


# ── 6. 单轨红线：submit 主路径零同步审计 ─────────────────────


class TestNoSyncAuditOnSubmitPath:
    async def test_submit_gate_dispatches_instead_of_sync_audit(self, env):
        """审计门触发时 submit 主路径**绝不**调审计 LLM 入口（单轨钦定）。"""
        from hiveweave.services.code_audit import run_code_audit

        await _insert_qa_lead(env)
        tid = await _create_source_task(env)
        run_mock = AsyncMock(wraps=None)

        async def fake_get_project_id(aid: str):
            return PROJECT_ID if aid in ALL_AGENTS else None

        with patch("hiveweave.tools.helpers.get_project_id",
                   fake_get_project_id), \
             patch("hiveweave.agents.trigger.trigger_coordinator", AsyncMock()), \
             patch("hiveweave.agents.trigger.trigger_subordinate", AsyncMock()), \
             patch("hiveweave.services.git_worktree._git", _git_fail_all), \
             patch("hiveweave.services.code_audit.run_code_audit", run_mock):
            result = await submit_task_tool(
                _mint_params(tid), EXEC_ID, env["workspace_path"]
            )

        assert result.success is False
        assert result.to_dict()["audit_dispatched"] is True
        run_mock.assert_not_awaited()


# ── 7. 凭证落地开门 ─────────────────────────────────────────


class TestAttestationLandingOpensGate:
    async def test_pass_attestation_on_resubmit_opens_gate(self, env):
        """审计任务 submitted（带 PASS 凭证）⇒ 源任务重提自动附凭证 ⇒ 门开。

        凭证由代审落库：绑 submitter + 源任务（kind=code_audit, exit 0）。
        重提不带 attestationIds —— 走 find_recent_for_agent 自动附上。
        """
        await _insert_qa_lead(env)
        tid = await _create_source_task(env)
        svc = TaskService()

        first = await _submit_tool(env, _mint_params(tid))
        assert first.success is False
        audit_id = first.to_dict()["audit_task_id"]

        # 审计者代审落凭证（绑定 submitter + 源任务）
        att_id = await attestation_service.create(
            PROJECT_ID,
            agent_id=EXEC_ID,
            kind="code_audit",
            task_id=tid,
            exit_code=0,
            workspace=r"C:\fake\wt",
            stdout="VERDICT: PASS",
        )
        # 审计任务走普通状态机收口：start → submit（唤醒链由工具层负责）
        await svc.start_task(PROJECT_ID, audit_id)
        await svc.submit_task(
            PROJECT_ID, audit_id,
            {"summary": "audit verdict=PASS attestation=" + att_id},
        )

        second = await _submit_tool(
            env, SubmitTaskParams(
                taskId=tid, summary="big diff landed", testsPassed=True,
            )
        )
        assert second.success is True, second.error
        task = await svc.get_task(PROJECT_ID, tid)
        assert task["status"] == "submitted"
        # 门开走的是凭证路径，不再铸第二条审计任务
        assert len(await _audit_task_rows(env)) == 1
