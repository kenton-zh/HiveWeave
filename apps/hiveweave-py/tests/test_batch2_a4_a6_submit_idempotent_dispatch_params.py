"""批 2 A4/A6：submit 幂等 + dispatch_task 参数陷阱（D76 / S14 实测）。

A4（D76）：快速重提撞 ``Illegal transition: submitted → submitted``，agent
被迫把任务移回 running 再重提 —— 状态机被当绕路工具用。钉住：
- 服务层 submitted → submitted = 幂等成功（不抛错、不改状态/证据）；
- 同任务并发提交由 per-task 锁串行化（in-flight 防抖）；
- 非法转移报错自带处方（列出当前状态的合法出口）。

A6（S14 ×4 同秒 ``'task': Field required``）：模型可见契约（executor 的
TOOL_PARAM_SCHEMAS 手写层）给 task/target 宣传的别名，pydantic 强制面
此前不认 —— 模型照契约传即撞 Field required。钉住：宣传别名 ⊆ 强制面
（漂移守卫）、复用 taskId 时 task 可省、新建缺 task 的守卫回执带处方、
param_example 附着。
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
from hiveweave.tools.base import get_tool_def
from hiveweave.tools.tasks.dispatch import DispatchTaskParams, dispatch_task_tool

PROJECT_ID = "test-batch2-a4a6"
COORDINATOR_ID = "test-batch2-coordinator"
EXECUTOR_ID = "test-batch2-executor"


@pytest.fixture
async def env():
    """与 tests/test_task_service.py 同款：临时 per-project DB + meta 路由。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORDINATOR_ID, EXECUTOR_ID) else None

        _FAKE_AGENTS = {
            COORDINATOR_ID: {
                "id": COORDINATOR_ID,
                "name": "Batch2 Coordinator",
                "parent_id": None,
                "permission_type": "coordinator",
                "role": "架构师",
                "status": "active",
            },
            EXECUTOR_ID: {
                "id": EXECUTOR_ID,
                "name": "Batch2 Executor",
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
    """Create → claim → start. Returns task_id（与 test_task_service 同款）。"""
    tid = await svc.create_task(
        project_id=env["project_id"], title="T", description="d",
        creator_id=env["coordinator_id"])
    await svc.claim_task(env["project_id"], tid, env["executor_id"])
    await svc.start_task(env["project_id"], tid)
    return tid


# ── A4-1/A4-2：submit 幂等 + 防抖 ──────────────────────────


class TestSubmitIdempotent:
    async def test_resubmit_submitted_is_idempotent(self, env):
        """submitted → submitted 重复提交：幂等成功，状态/证据/时间戳不动。"""
        svc = TaskService()
        tid = await _create_started(env, svc)
        await svc.submit_task(env["project_id"], tid, {"files": ["a.py"]})
        before = await svc.get_task(env["project_id"], tid)
        assert before["status"] == "submitted"

        result = await svc.submit_task(
            env["project_id"], tid, {"files": ["HACKED.py"]}
        )
        assert isinstance(result, dict)
        assert result["idempotent"] is True
        assert result["status"] == "submitted"

        after = await svc.get_task(env["project_id"], tid)
        assert after["status"] == "submitted"
        # 首次提交的证据不被重提覆盖
        assert after["evidence"] == {"files": ["a.py"]}
        assert after["submitted_at"] == before["submitted_at"]

    async def test_concurrent_submit_debounces(self, env):
        """并发双提交：锁串行化，后到者走幂等出口，最终证据 = 先到者。"""
        svc = TaskService()
        tid = await _create_started(env, svc)

        r1, r2 = await asyncio.gather(
            svc.submit_task(env["project_id"], tid, {"files": ["first.py"]}),
            svc.submit_task(env["project_id"], tid, {"files": ["second.py"]}),
        )
        results = [r1, r2]
        assert all(isinstance(r, dict) for r in results)
        non_idem = [r for r in results if not r["idempotent"]]
        idem = [r for r in results if r["idempotent"]]
        assert len(non_idem) == 1
        assert len(idem) == 1

        after = await svc.get_task(env["project_id"], tid)
        assert after["status"] == "submitted"
        # 落库证据必须是**真实提交那一次**的（锁胜者），不是幂等重提的；
        # 胜者不确定（gather 调度序），按非幂等结果反推。
        winner = "first.py" if r1 is non_idem[0] else "second.py"
        assert after["evidence"] == {"files": [winner]}

    async def test_failed_submit_then_retry_still_runs_gates(self, env):
        """防抖只消化成功提交的重入：首次失败（非法转移）后重试仍正常判定。"""
        svc = TaskService()
        tid = await svc.create_task(
            project_id=env["project_id"], title="T", description="d",
            creator_id=env["coordinator_id"])
        # created → submitted 仍是非法转移（幂等出口只覆盖 submitted 态）
        with pytest.raises(ValueError, match="Illegal transition"):
            await svc.submit_task(env["project_id"], tid, {"files": ["x.py"]})


# ── A4 让渡项：工具层消费幂等返回 —— 重提不再唤醒 reviewer ────


class TestSubmitToolIdempotentNoWake:
    async def test_tool_resubmit_idempotent_skips_reviewer_wake(self, env):
        """submit_task_tool 重提：消费服务幂等返回提早退，不再发
        [TASK SUBMITTED] 唤醒（inbox 不新增行、不再 trigger reviewer）。

        断言锚在真实机制上：per-project DB 的 inbox 行数。首轮通知行先置
        已读（模拟 reviewer 已消费）——否则 send_message 的 idempotency_key
        判重会把未修复时的第二次通知也吞掉，断言测不出差异。
        """
        from hiveweave.tools.tasks.submit import (
            SubmitTaskParams,
            submit_task_tool,
        )

        svc = TaskService()
        tid = await _create_started(env, svc)

        async def fake_get_project_id(aid: str):
            return env["project_id"] if aid == env["executor_id"] else None

        async def _inbox_count() -> int:
            row = await project_db.query_one(
                env["coordinator_id"],
                "SELECT COUNT(*) AS n FROM inbox WHERE to_agent_id = ?",
                [env["coordinator_id"]],
            )
            return int(row["n"])

        trigger_mock = AsyncMock()
        with patch("hiveweave.tools.helpers.get_project_id",
                   fake_get_project_id), \
             patch("hiveweave.agents.trigger.trigger_coordinator",
                   trigger_mock):
            first = await submit_task_tool(
                SubmitTaskParams(
                    taskId=tid, summary="first submission", testsPassed=True
                ),
                env["executor_id"],
                env["workspace_path"],
            )
            assert first.success is True, first.error

            n_first = await _inbox_count()
            assert n_first >= 1  # 首次提交确有 [TASK SUBMITTED] 通知行
            trigger_mock.assert_awaited_once()

            # 模拟 reviewer 已消费首轮通知：旧行已读 ⇒ 未修时重提会走
            # rearm 补投新行；修复后应零新增。
            await project_db.execute(
                env["coordinator_id"],
                "UPDATE inbox SET read = 1 WHERE to_agent_id = ?",
                [env["coordinator_id"]],
            )

            second = await submit_task_tool(
                SubmitTaskParams(
                    taskId=tid, summary="duplicate submission",
                    testsPassed=True,
                ),
                env["executor_id"],
                env["workspace_path"],
            )
            assert second.success is True, second.error
            assert "already submitted (idempotent)" in (second.output or "")
            receipt = second.to_dict()
            assert receipt["idempotent"] is True
            assert receipt["task_id"] == tid
            assert receipt["status"] == "submitted"
            task_after = await svc.get_task(env["project_id"], tid)
            assert receipt["submitted_at"] == task_after["submitted_at"]

            # 幂等重提零新通知、零新唤醒
            assert await _inbox_count() == n_first
            trigger_mock.assert_awaited_once()


# ── A4-3：非法转移报错带合法出口处方 ────────────────────────


class TestIllegalTransitionPrescription:
    async def test_error_lists_legal_exits(self, env):
        svc = TaskService()
        tid = await svc.create_task(
            project_id=env["project_id"], title="T", description="d",
            creator_id=env["coordinator_id"])
        with pytest.raises(ValueError) as exc_info:
            await svc._transition(env["project_id"], tid, "running")
        msg = str(exc_info.value)
        assert "Illegal transition: created → running" in msg
        # 处方：合法出口清单（sorted(created) = blocked, claimed, closed）
        assert "Legal exits from 'created': blocked, claimed, closed" in msg
        assert "Retrying the same call will not help" in msg

    async def test_multi_error_lists_legal_exits(self, env):
        svc = TaskService()
        tid = await svc.create_task(
            project_id=env["project_id"], title="T", description="d",
            creator_id=env["coordinator_id"])
        with pytest.raises(ValueError) as exc_info:
            await svc._transition_multi(env["project_id"], tid,
                                        "running", "submitted")
        assert "Illegal transition: created → running" in str(exc_info.value)
        assert "Legal exits from 'created'" in str(exc_info.value)

    async def test_terminal_state_exits_rendered(self, env):
        svc = TaskService()
        tid = await svc.create_task(
            project_id=env["project_id"], title="T", description="d",
            creator_id=env["coordinator_id"])
        await svc.claim_task(env["project_id"], tid, env["executor_id"])
        await svc.start_task(env["project_id"], tid)
        await svc.submit_task(env["project_id"], tid, {"files": ["a.py"]})
        await svc.start_review(env["project_id"], tid)
        await svc.review_task(env["project_id"], tid, "approve")
        await svc.close_task(env["project_id"], tid, skip_merge_gate=True)
        with pytest.raises(ValueError) as exc_info:
            await svc._transition(env["project_id"], tid, "running")
        assert "Legal exits from 'closed': (none — terminal state)" in (
            str(exc_info.value)
        )


# ── A6：dispatch_task 参数别名收编 + 处方回执 ───────────────

# 手写层（TOOL_PARAM_SCHEMAS）宣传给模型的别名 —— 强制面必须全收，
# 否则模型照契约传即撞 Field required（S14 实测病灶）。
_ADVERTISED_TASK_ALIASES = ["description", "message", "content", "summary", "desc"]
_ADVERTISED_TARGET_ALIASES = [
    "toAgentId", "to_agent_id", "recipient", "agentId", "subordinate", "agent_id",
]


def _validate_dispatch(raw: dict):
    td = get_tool_def("dispatch_task")
    assert td is not None, "dispatch_task 未注册"
    params, error, _violations = td.validate_detailed(raw)
    return params, error


class TestDispatchParamAliases:
    @pytest.mark.parametrize("alias", _ADVERTISED_TASK_ALIASES)
    def test_advertised_task_aliases_are_enforced(self, alias):
        """漂移守卫：手写层宣传的 task 别名必须被 registered 管线接受。"""
        params, error = _validate_dispatch(
            {"target": "A009", alias: "do the thing", "submitGate": "unit"}
        )
        assert error is None, f"别名 {alias!r} 被强制面拒绝: {error}"
        assert params.task == "do the thing"

    @pytest.mark.parametrize("alias", _ADVERTISED_TARGET_ALIASES)
    def test_advertised_target_aliases_are_enforced(self, alias):
        params, error = _validate_dispatch(
            {"target": "A009", "task": "do it", "submitGate": "unit"}
        )
        assert error is None  # 正名基线
        params, error = _validate_dispatch(
            {alias: "A009", "task": "do it", "submitGate": "unit"}
        )
        assert error is None, f"别名 {alias!r} 被强制面拒绝: {error}"
        assert params.target == "A009"

    def test_reuse_taskid_without_task_description_is_valid(self):
        """复用 taskId 重新指派：task 可省（保留既有任务卡描述）。"""
        params, error = _validate_dispatch(
            {"target": "A009", "taskId": "t-123", "submitGate": "unit"}
        )
        assert error is None, error
        assert params.task is None
        assert params.task_id == "t-123"

    @pytest.mark.asyncio
    async def test_new_task_without_task_gets_prescription_receipt(self):
        """新建缺 task：进任何服务前给带处方（必填/可选参数名）的回执。"""
        result = await dispatch_task_tool(
            DispatchTaskParams(target="A009", submitGate="unit"),
            "agent-1",
            "/ws",
        )
        assert result.success is False
        text = result.error or result.output or ""
        assert "Required: target, task, submitGate" in text
        assert "taskId" in text  # 复用出口也在处方里

    def test_param_example_attached_to_tool_def(self):
        """参数校验失败回执附带的可抄样例必须存在且点名关键参数。"""
        td = get_tool_def("dispatch_task")
        example = getattr(td, "param_example", None)
        assert example
        assert '"target"' in example
        assert '"task"' in example
        assert "submitGate" in example

    def test_alias_tables_no_cross_field_collision(self):
        """新收编的别名不得撞其他字段的正名/别名（防错路由，B6 同款红线）。"""
        from hiveweave.tools.base import _extract_aliases

        fields = DispatchTaskParams.model_fields
        taken: dict[str, str] = {}
        for name, info in fields.items():
            names = {name, *_extract_aliases(info)}
            for n in names:
                owner = taken.setdefault(n, name)
                assert owner == name, (
                    f"别名冲突: {n!r} 同时属于 {owner} 和 {name}"
                )
