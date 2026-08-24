"""Attestation waiver + CLI 测试命令识别 — BUGFIX #2。

回归场景（井字棋实测事故）：
- executor 跑 `python tictactoe.py` / `python verify_ai.py` 验证 CLI 程序
- 旧 is_test_command 只认 pytest/npm test 等测试运行器 → 不签发 attestation
- submit_task 硬拒，CEO charter 口头豁免工具层不认 → 全部任务卡死

修复：
1. is_test_command 覆盖 unittest / test_*.py / verify_*.py / check_* 脚本
2. waive_attestation 正式豁免通道（coordinator 落库、可审计、24h 过期）
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services.attestation import (
    attestation_service,
    check_task_attestations,
    create_waiver,
    has_valid_waiver,
    is_test_command,
)

PROJECT_ID = "test-waiver-project"
COORD_ID = "test-coordinator"
EXEC_ID = "test-executor"


# ── is_test_command 覆盖 ────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        "python verify_ai.py",
        "python tests/verify_ai.py",
        "python test_tictactoe.py",
        "python tictactoe_test.py",
        "python -m unittest discover -s tests",
        "python -m pytest tests/",
        "uv run pytest tests/",
        "node verify_logic.mjs",
        "bash check_build.sh",
        "pytest tests/ -q",
        "npm test",
    ],
)
def test_is_test_command_accepts_cli_verify_scripts(cmd):
    assert is_test_command(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "python tictactoe.py",          # 运行主程序 ≠ 验证脚本
        "python -c 'print(1)'",
        "ls -la",
        "cat README.md",
        "python contest_submission.py",  # 含 test 子串但不是测试语义（词边界）
    ],
)
def test_is_test_command_rejects_non_test_commands(cmd):
    assert is_test_command(cmd) is False


# ── waiver 通道 ─────────────────────────────────────────────


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        att_module._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            yield {"workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _generic_task(task_id: str) -> dict:
    """generic_tests policy 的任务（井字棋实现任务形态）。"""
    return {
        "id": task_id,
        "title": "实现井字棋 CLI",
        "description": "python 单文件 minimax",
        "tags": [],
        "evidence": {},
    }


@pytest.mark.asyncio
async def test_waiver_still_records_but_gate_is_open(env):
    """Attestation hard-gate removed — submit never blocked; waiver still audit-logs."""
    task = _generic_task("task-cli-1")

    deny = await check_task_attestations(PROJECT_ID, task, None)
    assert deny is None

    wid = await create_waiver(
        PROJECT_ID, task_id=task["id"], waived_by=COORD_ID,
        reason="纯 CLI 任务无 UI 可 browse，以 bash 验证日志替代",
    )
    assert wid
    assert await has_valid_waiver(PROJECT_ID, task["id"]) is True

    ok = await check_task_attestations(PROJECT_ID, task, None)
    assert ok is None

    # Other tasks also open (no scripted gate)
    deny2 = await check_task_attestations(
        PROJECT_ID, _generic_task("task-other"), None
    )
    assert deny2 is None


@pytest.mark.asyncio
async def test_waiver_requires_reason(env):
    with pytest.raises(ValueError, match="reason"):
        await create_waiver(
            PROJECT_ID, task_id="t1", waived_by=COORD_ID, reason="  "
        )


@pytest.mark.asyncio
async def test_waiver_expires(env):
    # 创建一个已过期的 waiver（ttl=-1ms）
    await create_waiver(
        PROJECT_ID, task_id="t-exp", waived_by=COORD_ID,
        reason="expired waiver", ttl_ms=-1,
    )
    await attestation_service.ensure_schema(PROJECT_ID)
    # 刚创建就过期
    assert await has_valid_waiver(PROJECT_ID, "t-exp") is False


# ── E3 waiver 治理：verdict=FAIL 的结论不可豁免（复盘致命链一）──────


@pytest.mark.asyncio
async def test_waive_fail_verdict_rejected(env):
    """E3-①verdict=FAIL 的任务 waive → 硬拒，文案点名结论不可豁免 + rework。"""
    from hiveweave.tools.task_tools import (
        WaiveAttestationParams,
        waive_attestation_tool,
    )

    task = {
        "id": "t-verify-fail",
        "title": "VERIFY: feature",
        "tags": ["verify", "mandatory"],
        "assignee_id": "qa1",
        "evidence": {
            "verdict": "FAIL",
            "blocking_issues": ["/_admin 全线 404"],
        },
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="ceo",
        ),
        patch(
            "hiveweave.services.attestation.count_waivers",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": "ceo1", "role": "CEO"}
        )
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-verify-fail",
                reason="终验 FAIL 但用户催交付，申请豁免放行该终验",
            ),
            agent_id="ceo1",
            workspace=env["workspace_path"],
        )
    assert result.success is False
    text = (result.output or "") + (result.error or "")
    assert "结论不合格不可豁免" in text
    assert "rework" in text


@pytest.mark.asyncio
async def test_waive_non_fail_verdict_unaffected(env):
    """E3-②PASS / 无 verdict 的任务不触发 E3 硬拒（其他闸门照常）。"""
    from hiveweave.tools.task_tools import (
        WaiveAttestationParams,
        waive_attestation_tool,
    )

    task = {
        "id": "t-verify-pass",
        "title": "VERIFY: feature",
        "tags": ["verify", "mandatory"],
        "assignee_id": "qa1",
        "evidence": {"verdict": "PASS", "tests_passed": True},
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="ceo",
        ),
        patch(
            "hiveweave.services.attestation.count_waivers",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": "ceo1", "role": "CEO"}
        )
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-verify-pass",
                reason="无 UI 模块走 bash 验证日志替代，豁免该终验凭证",
            ),
            agent_id="ceo1",
            workspace=env["workspace_path"],
        )
    text = (result.output or "") + (result.error or "")
    assert "结论不合格不可豁免" not in text


# ── E5 断流收口纪律：降级 + open VERIFY → 禁就地 waiver 收口 ─────────


@pytest.mark.asyncio
async def test_waive_degraded_with_open_verify_rejected(env):
    """E5-①断流降级中且名下有未闭环 VERIFY → waive 被拒，指引续跑/升级。"""
    from hiveweave.agents.recovery import clear_degraded, mark_degraded
    from hiveweave.tools.task_tools import (
        WaiveAttestationParams,
        waive_attestation_tool,
    )

    task = {
        "id": "t-verify-e5",
        "title": "VERIFY: feature",
        "tags": ["verify", "mandatory"],
        "assignee_id": "qa1",
        "evidence": {"verdict": "PASS", "tests_passed": True},
    }
    open_verify = {
        "id": "t-verify-open",
        "title": "VERIFY: other",
        "tags": ["verify"],
        "assignee_id": "qa1",
    }
    mark_degraded("qa1")  # 模拟刚被打断（SSL EOF / stall）的降级态
    try:
        with (
            patch(
                "hiveweave.tools.helpers.get_project_id",
                AsyncMock(return_value=PROJECT_ID),
            ),
            patch("hiveweave.services.task.TaskService") as TS,
            patch("hiveweave.services.org.OrgService") as Org,
            patch(
                "hiveweave.services.policy.infer_role_family",
                return_value="ceo",
            ),
            patch(
                "hiveweave.services.attestation.count_waivers",
                AsyncMock(return_value=0),
            ),
            patch(
                "hiveweave.services.attestation.has_valid_waiver",
                AsyncMock(return_value=False),
            ),
        ):
            TS.return_value.get_task = AsyncMock(return_value=task)
            TS.return_value.get_open_work_obligations = AsyncMock(
                return_value=[open_verify]
            )
            TS.return_value._is_verify_task = MagicMock(return_value=True)
            Org.return_value.get_agent = AsyncMock(
                return_value={"id": "ceo1", "role": "CEO"}
            )
            result = await waive_attestation_tool(
                WaiveAttestationParams(
                    taskId="t-verify-e5",
                    reason="该终验已通过并附机器验证记录，豁免其余凭证要求以便收口归档",
                ),
                agent_id="qa1",
                workspace=env["workspace_path"],
            )
    finally:
        clear_degraded("qa1")

    assert result.success is False
    text = (result.output or "") + (result.error or "")
    assert "断流" in text and "禁止就地 waiver 收口" in text


@pytest.mark.asyncio
async def test_waive_normal_not_blocked_by_e5(env):
    """E5-②非降级（断流已恢复）→ E5 不拦，waive 走其他闸门。"""
    from hiveweave.tools.task_tools import (
        WaiveAttestationParams,
        waive_attestation_tool,
    )

    task = {
        "id": "t-verify-e5b",
        "title": "VERIFY: feature",
        "tags": ["verify", "mandatory"],
        "assignee_id": "qa1",
        "evidence": {"verdict": "PASS", "tests_passed": True},
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="ceo",
        ),
        patch(
            "hiveweave.services.attestation.count_waivers",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value.get_open_work_obligations = AsyncMock(
            return_value=[{"id": "t-open", "title": "普通任务"}]
        )
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": "ceo1", "role": "CEO"}
        )
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-verify-e5b",
                reason="该终验已通过并附机器验证记录，豁免其余凭证要求以便收口归档",
            ),
            agent_id="qa1",
            workspace=env["workspace_path"],
        )
    text = (result.output or "") + (result.error or "")
    assert "禁止就地 waiver 收口" not in text


# ── P0-2: rework 时 invalidate valid waiver ─────────────────
# TEST18 死锁根因：rework 不清 waiver，waived_by 第三人隔离 24h 内
# 不可恢复。修复后 rework 立即退役 active waiver，新 submit/review
# 周期从干净状态开始，但 lifetime count 保留（MAX_WAIVERS_PER_TASK cap）。


@pytest.mark.asyncio
async def test_invalidate_valid_waivers_retires_active(env):
    """invalidate_valid_waivers 把 active waiver 的 expires_at 设为 now。"""
    from hiveweave.services.attestation import (
        count_waivers,
        get_valid_waiver,
        invalidate_valid_waivers,
    )

    tid = "t-invalidate-1"
    await create_waiver(
        PROJECT_ID, task_id=tid, waived_by=COORD_ID,
        reason="active waiver to be retired on rework",
    )
    assert await has_valid_waiver(PROJECT_ID, tid) is True
    prior_count = await count_waivers(PROJECT_ID, tid)
    assert prior_count == 1

    retired = await invalidate_valid_waivers(PROJECT_ID, tid)
    assert retired == 1

    # Active waiver gone — approve path no longer sees waived_by isolation
    assert await has_valid_waiver(PROJECT_ID, tid) is False
    assert await get_valid_waiver(PROJECT_ID, tid) is None
    # Lifetime count preserved (row kept for audit, MAX_WAIVERS_PER_TASK cap)
    assert await count_waivers(PROJECT_ID, tid) == prior_count


@pytest.mark.asyncio
async def test_invalidate_valid_waivers_idempotent(env):
    """重复调用安全 — 无 active waiver 时返回 0，不报错。"""
    from hiveweave.services.attestation import invalidate_valid_waivers

    tid = "t-invalidate-2"
    # No waiver exists
    assert await invalidate_valid_waivers(PROJECT_ID, tid) == 0
    # After creating + retiring once, second call returns 0
    await create_waiver(
        PROJECT_ID, task_id=tid, waived_by=COORD_ID,
        reason="waiver for idempotency test",
    )
    assert await invalidate_valid_waivers(PROJECT_ID, tid) == 1
    assert await invalidate_valid_waivers(PROJECT_ID, tid) == 0


@pytest.mark.asyncio
async def test_invalidate_valid_waivers_handles_empty_task_id(env):
    """空 task_id 早返回 0，不查 DB。"""
    from hiveweave.services.attestation import invalidate_valid_waivers

    assert await invalidate_valid_waivers(PROJECT_ID, "") == 0
    assert await invalidate_valid_waivers(PROJECT_ID, None) == 0


@pytest.mark.asyncio
async def test_invalidate_preserves_expired_waivers_audit_rows(env):
    """退役的 waiver 行仍保留（UPDATE expires_at，非 DELETE）。"""
    from hiveweave.services.attestation import (
        WAIVER_KIND,
        _conn as _att_conn,
        invalidate_valid_waivers,
    )

    tid = "t-invalidate-3"
    await create_waiver(
        PROJECT_ID, task_id=tid, waived_by=COORD_ID,
        reason="audit row preservation test",
    )
    await invalidate_valid_waivers(PROJECT_ID, tid)

    # Direct SQL: row still exists, now expired
    conn = await _att_conn(PROJECT_ID)
    cur = await conn.execute(
        "SELECT COUNT(*) AS c, expires_at FROM tool_attestations "
        "WHERE project_id = ? AND task_id = ? AND kind = ?",
        [PROJECT_ID, tid, WAIVER_KIND],
    )
    row = await cur.fetchone()
    await cur.close()
    assert int(row["c"]) == 1
    # expires_at is set (not NULL) and <= now
    import time as _time
    assert row["expires_at"] is not None
    assert int(row["expires_at"]) <= int(_time.time() * 1000)

