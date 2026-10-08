"""批27-1 N1：submit 幂等出口「证据感知」（A+B 方案）。

根因（d90 实录）：A4-1 幂等分支把 submitted 重提一律当「证据未变」吞掉，
入参 evidence（含新 commit）物理不落库 ⇒ QA 反馈后修码重提时新 commit
无声丢失 ⇒ approve 证据门核旧 files_changed ⇒ 自动推翻 reviewer 批准。

钉住（指纹 = commit + files_changed + attestation id 集，只对入参已有
字段判等）：
- 服务层：submitted + 指纹不同 ⇒ 证据更新（状态保持 submitted）+ reviewer
  收恰好一次 [TASK RESUBMITTED]；指纹相同 ⇒ 幂等成功（A4 旧契约不动）。
- 工具层：同指纹 + 显式带证据字段 ⇒ err + 处方（无效重提不再以 ok 掩盖）；
  同指纹 + 裸重提（双发/防抖）⇒ 幂等 ok；指纹不同 ⇒ 透传证据更新路径。
- 证据刷新后，approve 证据门核得新 files_changed（不再推翻批准）。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import hiveweave.services.task as task_module
from hiveweave.db import project as project_db
from hiveweave.services.task import TaskService

PROJECT_ID = "test-b27-n1"
COORDINATOR_ID = "test-b27-coordinator"
EXECUTOR_ID = "test-b27-executor"


@pytest.fixture
async def env():
    """临时 per-project DB + meta 路由（与 test_batch2_a4_a6 同款）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORDINATOR_ID, EXECUTOR_ID) else None

        _FAKE_AGENTS = {
            COORDINATOR_ID: {
                "id": COORDINATOR_ID,
                "name": "B27 Coordinator",
                "parent_id": None,
                "permission_type": "coordinator",
                "role": "架构师",
                "status": "active",
            },
            EXECUTOR_ID: {
                "id": EXECUTOR_ID,
                "name": "B27 Executor",
                "parent_id": COORDINATOR_ID,
                "permission_type": "executor",
                "role": "engineer",
                "status": "active",
            },
        }

        async def fake_get_agent_by_id(aid: str):
            return _FAKE_AGENTS.get(aid)

        task_module._migrated.clear()
        project_db._agent_cache.pop(COORDINATOR_ID, None)
        project_db._agent_cache.pop(EXECUTOR_ID, None)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace), \
             patch("hiveweave.db.meta.get_agent_project_id",
                   fake_get_agent_project_id), \
             patch("hiveweave.db.meta.get_agent_by_id",
                   fake_get_agent_by_id):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
                "coordinator_id": COORDINATOR_ID,
                "executor_id": EXECUTOR_ID,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(COORDINATOR_ID, None)
        project_db._agent_cache.pop(EXECUTOR_ID, None)


async def _create_started(env, svc: TaskService) -> str:
    tid = await svc.create_task(
        project_id=env["project_id"], title="T", description="d",
        creator_id=env["coordinator_id"])
    await svc.claim_task(env["project_id"], tid, env["executor_id"])
    await svc.start_task(env["project_id"], tid)
    return tid


async def _inbox_count(env, pattern: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM inbox WHERE to_agent_id = ?"
    args: list = [env["coordinator_id"]]
    if pattern:
        sql += " AND message LIKE ?"
        args.append(pattern)
    row = await project_db.query_one(env["coordinator_id"], sql, args)
    return int(row["n"])


# ── A：服务层证据更新路径 ─────────────────────────────────────


class TestServiceEvidenceResubmit:
    @pytest.mark.asyncio
    async def test_resubmit_new_evidence_updates_and_notifies_reviewer_once(
        self, env,
    ):
        """① submitted + 指纹不同 ⇒ 证据更新 + reviewer 通知恰好一次 +
        状态仍 submitted；同指纹再提 ⇒ 幂等防抖不再通知。"""
        svc = TaskService()
        pid = env["project_id"]
        tid = await _create_started(env, svc)
        await svc.submit_task(
            pid, tid, {"files_changed": ["a.py"], "commit": "aaa"})
        before = await svc.get_task(pid, tid)
        assert before["status"] == "submitted"

        n0 = await _inbox_count(env)
        result = await svc.submit_task(
            pid, tid, {"files_changed": ["a.py", "b.py"], "commit": "bbb"})
        assert isinstance(result, dict)
        assert result["resubmitted"] is True
        assert result["idempotent"] is True
        assert result["status"] == "submitted"

        after = await svc.get_task(pid, tid)
        assert after["status"] == "submitted"  # 证据刷新不是状态迁移
        assert after["evidence"]["files_changed"] == ["a.py", "b.py"]
        assert after["evidence"]["commit"] == "bbb"
        assert after["submitted_at"] >= (before["submitted_at"] or 0)
        # reviewer 通知恰好一次（[TASK RESUBMITTED]，非重复 [TASK SUBMITTED]）
        assert await _inbox_count(env) == n0 + 1
        assert await _inbox_count(env, "%[TASK RESUBMITTED]%") == 1
        assert await _inbox_count(env, "%[TASK SUBMITTED]%") == 0

        # 同指纹再提 ⇒ 幂等防抖，证据与通知都不动（同指纹只发一次）
        again = await svc.submit_task(
            pid, tid, {"files_changed": ["a.py", "b.py"], "commit": "bbb"})
        assert again["idempotent"] is True
        assert not again.get("resubmitted")
        after2 = await svc.get_task(pid, tid)
        assert after2["evidence"] == after["evidence"]
        assert await _inbox_count(env) == n0 + 1

    @pytest.mark.asyncio
    async def test_resubmit_same_fingerprint_idempotent(self, env):
        """② submitted + 同指纹（显式同 commit/files）⇒ 幂等成功，证据/
        时间戳不动，无新通知（A4 契约在显式指纹字段上同样成立）。"""
        svc = TaskService()
        pid = env["project_id"]
        tid = await _create_started(env, svc)
        await svc.submit_task(
            pid, tid, {"files_changed": ["a.py"], "commit": "aaa"})
        before = await svc.get_task(pid, tid)

        n0 = await _inbox_count(env)
        result = await svc.submit_task(
            pid, tid, {"files_changed": ["a.py"], "commit": "aaa"})
        assert isinstance(result, dict)
        assert result["idempotent"] is True
        assert not result.get("resubmitted")

        after = await svc.get_task(pid, tid)
        assert after["evidence"] == before["evidence"]
        assert after["submitted_at"] == before["submitted_at"]
        assert await _inbox_count(env) == n0

    @pytest.mark.asyncio
    async def test_concurrent_submit_same_fingerprint_debounces(self, env):
        """③ A4 场景（快速双发，指纹字段相同）：锁串行化 ⇒ 恰一次真实提交，
        后到者幂等，最终证据 = 先到者。"""
        svc = TaskService()
        pid = env["project_id"]
        tid = await _create_started(env, svc)
        ev = {"files_changed": ["same.py"], "commit": "c1"}

        r1, r2 = await asyncio.gather(
            svc.submit_task(pid, tid, dict(ev)),
            svc.submit_task(pid, tid, dict(ev)),
        )
        results = [r1, r2]
        non_idem = [r for r in results if not r["idempotent"]]
        idem = [r for r in results if r["idempotent"]]
        assert len(non_idem) == 1
        assert len(idem) == 1
        assert not idem[0].get("resubmitted")

        after = await svc.get_task(pid, tid)
        assert after["status"] == "submitted"
        assert after["evidence"]["files_changed"] == ["same.py"]
        assert after["evidence"]["commit"] == "c1"

    @pytest.mark.asyncio
    async def test_resubmit_missing_field_not_misjudged_as_changed(self, env):
        """入参未带的指纹字段视为未变：不带 files_changed 的重提不得把库内
        清单抹成空（工具层 files_changed 常靠 preflight 自动回填，缺席 ≠
        清空）。"""
        svc = TaskService()
        pid = env["project_id"]
        tid = await _create_started(env, svc)
        await svc.submit_task(
            pid, tid, {"files_changed": ["a.py"], "commit": "aaa"})

        result = await svc.submit_task(pid, tid, {"commit": "aaa"})
        assert result["idempotent"] is True
        assert not result.get("resubmitted")
        after = await svc.get_task(pid, tid)
        assert after["evidence"]["files_changed"] == ["a.py"]


# ── B：工具层 submitted 前置分流 ─────────────────────────────


class TestToolSubmittedFingerprintGate:
    @pytest.mark.asyncio
    async def test_explicit_no_new_evidence_gets_prescription_err(self, env):
        """④ 无效重提（显式带同 commit，无任何新证据）⇒ err + 处方文案；
        证据与通知都不动。"""
        from hiveweave.tools.tasks.submit import (
            SubmitTaskParams,
            submit_task_tool,
        )

        svc = TaskService()
        pid = env["project_id"]
        tid = await _create_started(env, svc)
        await svc.submit_task(
            pid, tid, {"files_changed": ["a.py"], "commit": "aaa"})
        before = await svc.get_task(pid, tid)

        async def fake_get_project_id(aid: str):
            return pid if aid == env["executor_id"] else None

        with patch("hiveweave.tools.helpers.get_project_id",
                   fake_get_project_id), \
             patch("hiveweave.agents.trigger.trigger_coordinator",
                   AsyncMock()):
            result = await submit_task_tool(
                SubmitTaskParams(
                    taskId=tid, summary="no-op resubmit",
                    testsPassed=True, commit="aaa",
                ),
                env["executor_id"],
                env["workspace_path"],
            )

        assert result.success is False
        text = result.error or result.output or ""
        assert "无新证据" in text
        assert "账本 commit=aaa = 你的 commit=aaa" in text
        assert "处方" in text
        assert "无需动作" in text
        # 无效重提零副作用
        after = await svc.get_task(pid, tid)
        assert after["evidence"] == before["evidence"]
        assert after["submitted_at"] == before["submitted_at"]
        assert await _inbox_count(env) == 0

    @pytest.mark.asyncio
    async def test_tool_resubmit_new_evidence_updates_and_notifies(self, env):
        """⑤ 工具层 d90 主链路：修码重提（新 commit + 新 files_changed）⇒
        透传证据更新路径，回执如实说 resubmitted；不再补发
        [TASK SUBMITTED]（reviewer 只收一次 RESUBMITTED）。"""
        from hiveweave.tools.tasks.submit import (
            SubmitTaskParams,
            submit_task_tool,
        )

        svc = TaskService()
        pid = env["project_id"]
        tid = await _create_started(env, svc)
        await svc.submit_task(
            pid, tid, {"files_changed": ["a.py"], "commit": "aaa"})
        # 新交付物真实存在于工作区（submit 侧存在性门 + 证据门都以磁盘为准）
        (Path(env["workspace_path"]) / "b.py").write_text(
            "fixed", encoding="utf-8")

        async def fake_get_project_id(aid: str):
            return pid if aid == env["executor_id"] else None

        with patch("hiveweave.tools.helpers.get_project_id",
                   fake_get_project_id), \
             patch("hiveweave.agents.trigger.trigger_coordinator",
                   AsyncMock()):
            result = await submit_task_tool(
                SubmitTaskParams(
                    taskId=tid, summary="fixed per QA feedback",
                    testsPassed=True, filesChanged=["b.py"], commit="bbb",
                ),
                env["executor_id"],
                env["workspace_path"],
            )

        assert result.success is True, result.error
        assert "resubmitted" in (result.output or "")
        after = await svc.get_task(pid, tid)
        assert after["status"] == "submitted"
        assert after["evidence"]["files_changed"] == ["b.py"]
        assert after["evidence"]["commit"] == "bbb"
        # reviewer 通知恰好一次：只有 RESUBMITTED，无第二次 [TASK SUBMITTED]
        assert await _inbox_count(env, "%[TASK RESUBMITTED]%") == 1
        assert await _inbox_count(env, "%[TASK SUBMITTED]%") == 0

    @pytest.mark.asyncio
    async def test_tool_bare_duplicate_still_idempotent_ok(self, env):
        """B 反向护栏：裸重提（不显式带证据字段，双发/防抖场景）维持幂等
        ok（A4 让渡项回执原样）。"""
        from hiveweave.tools.tasks.submit import (
            SubmitTaskParams,
            submit_task_tool,
        )

        svc = TaskService()
        pid = env["project_id"]
        tid = await _create_started(env, svc)
        await svc.submit_task(
            pid, tid, {"files_changed": ["a.py"], "commit": "aaa"})

        async def fake_get_project_id(aid: str):
            return pid if aid == env["executor_id"] else None

        with patch("hiveweave.tools.helpers.get_project_id",
                   fake_get_project_id), \
             patch("hiveweave.agents.trigger.trigger_coordinator",
                   AsyncMock()):
            result = await submit_task_tool(
                SubmitTaskParams(
                    taskId=tid, summary="duplicate poke", testsPassed=True,
                ),
                env["executor_id"],
                env["workspace_path"],
            )

        assert result.success is True, result.error
        assert "already submitted (idempotent)" in (result.output or "")
        receipt = result.to_dict()
        assert receipt["idempotent"] is True
        assert not receipt.get("resubmitted")
        assert await _inbox_count(env) == 0


# ── ⑤：证据刷新后 approve 证据门核新 files_changed ───────────


class TestEvidenceGateSeesRefreshedFiles:
    @pytest.mark.asyncio
    async def test_check_evidence_verifiable_passes_after_refresh(self, env):
        """证据门（check_evidence_verifiable）读 task.evidence.files_changed：
        刷新后核新清单通过——d90「核旧清单推翻批准」不再复现。"""
        from hiveweave.services.worktree_review import check_evidence_verifiable

        svc = TaskService()
        pid = env["project_id"]
        tid = await _create_started(env, svc)
        await svc.submit_task(pid, tid, {"files_changed": ["old.py"]})

        refreshed_path = Path(env["workspace_path"]) / "new_fix.py"
        refreshed_path.write_text("fix", encoding="utf-8")
        result = await svc.submit_task(
            pid, tid, {"files_changed": ["new_fix.py"], "commit": "ccc"})
        assert result["resubmitted"] is True

        refreshed = await svc.get_task(pid, tid)
        assert refreshed["evidence"]["files_changed"] == ["new_fix.py"]
        deny = await check_evidence_verifiable(
            pid, refreshed, refreshed["evidence"]
        )
        assert deny is None
        # 反证：旧清单里的文件不存在，门会拒——证明门确实在核刷新后的清单
        stale = dict(refreshed)
        stale["evidence"] = dict(refreshed["evidence"])
        stale["evidence"]["files_changed"] = ["old.py"]
        deny_stale = await check_evidence_verifiable(
            pid, stale, stale["evidence"]
        )
        assert deny_stale is not None
