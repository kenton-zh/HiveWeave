"""声明式凭证通道 — bash/run_command testEvidence=true 无条件落 test_run。

根因(TEST_DSH_31 Robin 死锁 1h): is_test_command 正则只认 test_/verify_/
check_ 前缀, agent 的 validate-suite.mjs 每次全绿却永不落凭证, 命名暗号
对 agent 不可见。药方: 意图声明式通道 declared_test=True 绕过正则,
正则保留为自动检测便利项(存量行为零回归)。防伪不靠文件名(伪造
verify_fake.py 照样过正则), 靠凭证记录的 command+stdout 交 reviewer 审。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.tools.bash import BashParams, RunCommandParams
from hiveweave.tools.bash import _issue_test_run_attestation


@pytest.fixture
def attest_env(tmp_path, monkeypatch):
    """mock 凭证落库 + 任务绑定解析(resolved=None 走非 VERIFY 路径)。"""
    create_mock = AsyncMock(return_value="att-123")
    with patch(
        "hiveweave.services.attestation.attestation_service.create",
        new=create_mock,
    ), patch(
        "hiveweave.tools.bash._resolve_test_attestation_task_id",
        new=AsyncMock(return_value=(None, "")),
    ):
        yield create_mock, str(tmp_path)


# ── _issue_test_run_attestation 门条件 ─────────────────────


@pytest.mark.asyncio
async def test_declared_test_forces_attestation_on_nonmatching_name(
    attest_env,
) -> None:
    """validate-suite.mjs(不命中正则) + declared_test=True → 落凭证。"""
    create_mock, ws = attest_env
    note = await _issue_test_run_attestation(
        project_id="proj", agent_id="agent-1",
        command="node validate-suite.mjs",
        workspace=ws, stdout="33 checks passed", exit_code=0,
        task_id=None, declared_test=True,
    )
    assert create_mock.call_count == 1
    kwargs = create_mock.call_args.kwargs
    assert kwargs.get("kind") == "test_run"
    assert kwargs.get("exit_code") == 0
    assert "validate-suite.mjs" in str(kwargs.get("command_or_url", ""))
    assert note  # 有回执片段


@pytest.mark.asyncio
async def test_undeclared_nonmatching_name_still_skipped(attest_env) -> None:
    """不声明 + 不命中正则 → 不落(存量自动检测行为保留)。"""
    create_mock, ws = attest_env
    note = await _issue_test_run_attestation(
        project_id="proj", agent_id="agent-1",
        command="node validate-suite.mjs",
        workspace=ws, stdout="ok", exit_code=0,
        task_id=None, declared_test=False,
    )
    assert note == ""
    assert create_mock.call_count == 0


@pytest.mark.asyncio
async def test_regex_autodetect_still_works_without_declaration(
    attest_env,
) -> None:
    """不声明 + 命中正则(verify_ 前缀) → 照旧落(零回归)。"""
    create_mock, ws = attest_env
    await _issue_test_run_attestation(
        project_id="proj", agent_id="agent-1",
        command="node verify_suite.mjs",
        workspace=ws, stdout="ok", exit_code=0,
        task_id=None, declared_test=False,
    )
    assert create_mock.call_count == 1


@pytest.mark.asyncio
async def test_declared_test_records_failure_too(attest_env) -> None:
    """声明通道下 exit≠0 也落凭证(失败测试照记, TEST6 P0-3 语义)。"""
    create_mock, ws = attest_env
    await _issue_test_run_attestation(
        project_id="proj", agent_id="agent-1",
        command="node validate-suite.mjs",
        workspace=ws, stdout="1 check failed", exit_code=1,
        task_id=None, declared_test=True,
    )
    assert create_mock.call_count == 1
    kwargs = create_mock.call_args.kwargs
    assert kwargs.get("exit_code") == 1


# ── 参数模型: 字符串宽容转换(LLM 传 "true") ────────────────


def test_bash_params_coerces_string_true() -> None:
    p = BashParams(command="echo hi", testEvidence="true")
    assert p.test_evidence is True
    p2 = BashParams(command="echo hi", test_evidence=1)
    assert p2.test_evidence is True


def test_bash_params_default_false() -> None:
    p = BashParams(command="echo hi")
    assert p.test_evidence is False


def test_run_command_params_coerces_string_true() -> None:
    p = RunCommandParams(command="echo hi", testEvidence="yes")
    assert p.test_evidence is True
    p2 = RunCommandParams(command="echo hi")
    assert p2.test_evidence is False


def test_bash_params_alias_accepted() -> None:
    """camelCase alias testEvidence 与 populate_by_name 两条路都通。"""
    p = BashParams.model_validate(
        {"command": "echo hi", "testEvidence": True}
    )
    assert p.test_evidence is True


# ── 调用点接线(审计 P2-1): params.test_evidence → declared_test ──


@pytest.mark.asyncio
async def test_run_command_tool_wires_test_evidence(tmp_path) -> None:
    """run_command_tool 端到端: testEvidence=true 的自定义脚本名落凭证。

    锁调用点接线——若 params.test_evidence 未传入 declared_test,
    本测试必红(函数级测试测不到)。
    """
    create_mock = AsyncMock(return_value="att-456")
    with patch(
        "hiveweave.tools.helpers.get_project_id",
        new=AsyncMock(return_value="proj"),
    ), patch(
        "hiveweave.services.process_registry.prepare_spawn_command",
        return_value=("node validate-suite.mjs", None, None, None),
    ), patch(
        "hiveweave.tools.bash.execute_run_command",
        new=AsyncMock(return_value={
            "success": True, "output": "33 checks passed", "exit_code": 0,
        }),
    ), patch(
        "hiveweave.services.attestation.attestation_service.create",
        new=create_mock,
    ), patch(
        "hiveweave.tools.bash._resolve_test_attestation_task_id",
        new=AsyncMock(return_value=(None, "")),
    ):
        from hiveweave.tools.bash import run_command_tool

        result = await run_command_tool(
            RunCommandParams(
                command="node validate-suite.mjs", testEvidence=True
            ),
            "agent-1", str(tmp_path),
        )
    assert result.success is True, result.error
    assert create_mock.call_count == 1
    kwargs = create_mock.call_args.kwargs
    assert kwargs.get("kind") == "test_run"
    assert kwargs.get("exit_code") == 0
    assert "validate-suite.mjs" in str(kwargs.get("command_or_url", ""))


@pytest.mark.asyncio
async def test_bash_tool_wires_test_evidence(tmp_path) -> None:
    """bash_tool 端到端: testEvidence=true 同样落凭证(bash 主路径)。"""
    create_mock = AsyncMock(return_value="att-789")
    with patch(
        "hiveweave.tools.helpers.get_project_id",
        new=AsyncMock(return_value="proj"),
    ), patch(
        "hiveweave.services.process_registry.prepare_spawn_command",
        return_value=("node validate-suite.mjs", None, None, None),
    ), patch(
        "hiveweave.tools.bash.execute_bash",
        new=AsyncMock(return_value={
            "success": True, "output": "ok", "exit_code": 0,
        }),
    ), patch(
        "hiveweave.services.attestation.attestation_service.create",
        new=create_mock,
    ), patch(
        "hiveweave.tools.bash._resolve_test_attestation_task_id",
        new=AsyncMock(return_value=(None, "")),
    ):
        from hiveweave.tools.bash import bash_tool

        result = await bash_tool(
            BashParams(
                command="node validate-suite.mjs", testEvidence=True
            ),
            "agent-1", str(tmp_path),
        )
    assert result.success is True, result.error
    assert create_mock.call_count == 1
