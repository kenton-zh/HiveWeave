"""start_dev_server 的 `env` 参数：agent 唯一能用的项目级环境变量通道。

背景（s3-clone_06 M5）：工具描述曾教 agent 用 `VAR=x cmd` 前缀 —— 那是 bash
语法，spawn 走 cmd.exe（Popen shell=True），100% 失败（实测 00:42 / 11:58 /
12:27 三次同款失败）；而 `.hiveweave/env.sh` 在沙箱 Windows 宿主上根本不会
被 source（`_source_env_sh` 仅在沙箱关闭时调用）。霁岚为此空转 3 小时。

契约：`env` 传给 spawn_project_process 并落到子进程；不传时不带 env kwarg
（旧调用方/测试桩兼容）。
"""

from __future__ import annotations

import pytest

from hiveweave.tools.dev_server_tools import (
    StartDevServerParams,
    start_dev_server_tool,
)
from tests.test_dev_server_cwd_precheck import (
    TEST_AGENT,
    _FakeAsyncio,
    _FakeProc,
)

# #1 治本（2026-09-14）：本文件的用例与**沙箱路由无关**（测 cwd 校验 /
# 端口 / 注册表 / alarm 脚本安全校验），而 spawn 现在必经唯一判定点 ——
# pytest 的 tmp workspace 不满足受限令牌前置条件 ⇒ 显式声明走原生执行面，
# 保持这些用例的被测语义（改造前它们隐式就在原生路径上）。
pytestmark = pytest.mark.usefixtures("native_sandbox_plane")


async def _run_start(monkeypatch, tmp_path, params: StartDevServerParams):
    """复用 cwd_precheck 测试的桩件（项目 id / spawn / 健康检查秒过）。"""
    seen: dict = {}

    async def fake_get_project_id(agent_id: str) -> str | None:
        return "proj-env-test"

    def fake_spawn(cmd, **kwargs):
        seen.update(kwargs)
        seen["command"] = cmd
        return _FakeProc(), None, {"command": cmd}

    monkeypatch.setattr(
        "hiveweave.tools.dev_server_tools.get_project_id",
        fake_get_project_id,
    )
    monkeypatch.setattr(
        "hiveweave.tools.dev_server_tools.spawn_project_process", fake_spawn
    )
    monkeypatch.setattr(
        "hiveweave.tools.dev_server_tools.asyncio", _FakeAsyncio()
    )
    result = await start_dev_server_tool(
        params, agent_id=TEST_AGENT, workspace=str(tmp_path)
    )
    return result, seen


async def test_env_param_reaches_spawn(monkeypatch, tmp_path):
    params = StartDevServerParams.model_validate(
        {
            "command": "python -m uvicorn app.main:app --host 0.0.0.0 --port {port}",
            "preferredPort": 8042,
            "env": {"HALYARD_DATA_DIR": str(tmp_path / "data")},
        }
    )
    result, seen = await _run_start(monkeypatch, tmp_path, params)

    assert result.success, result.error
    assert seen.get("env") == {"HALYARD_DATA_DIR": str(tmp_path / "data")}


async def test_no_env_kwarg_when_not_provided(monkeypatch, tmp_path):
    """不传 env 时不带该 kwarg —— 保持对旧调用方/测试桩的兼容。"""
    params = StartDevServerParams.model_validate({"preferredPort": 8043})
    result, seen = await _run_start(monkeypatch, tmp_path, params)

    assert result.success, result.error
    assert "env" not in seen
