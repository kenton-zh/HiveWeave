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
from unittest.mock import patch

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
