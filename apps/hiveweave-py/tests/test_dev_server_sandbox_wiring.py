"""#1：`start_dev_server` 必须把受限 agent 的 spawn 送进沙箱（验收①+中间验收②）。

## 缺口的实证（TEST_DSH_56）

叶子 A066 用 `start_dev_server(command="python -c ...")` **成功写出项目之外**
（回执 `code=0`、`Test-Path` True）；**同一越界动作经 pwsh 被 ACL 拒绝** —— 干净对照。
根因：沙箱路由是**每个工具自己的约定**（`acl_sandbox_active()` 散在 `bash.py` 里），
本工具从来没 import 过 `acl_sandbox`。

## 本文件钉三件事

1. **沙箱开 ⇒ 走 `spawn_confined(entry="dev_server", long_running=True)`**，
   且 **`spawn_project_process` 一次都不被调用**（后者＝平台身份无沙箱）；
2. **沙箱不可用 ⇒ fail-closed**：干净拒绝，**绝不静默降级为原生 spawn**
   （「以为在沙箱里、其实在沙箱外」是本条最坏的形态）；
3. **反面对照**：判定为原生 ⇒ 回落原生路径（确认没有把正常路径改死）。

⚠ 不断言"越界命令真的被拒" —— 那要真沙箱（Windows ACL/CreateProcessAsUser），
属真机实调；本文件钉的是**路由**（"命令走哪条 spawn"），路由错了后面全错。

⚠ #1 治本后**控制面换了**：路由由 `entry.spawn_agent_command` 经
`policy.resolve_spawn_decision` 决定 —— 所以本文件 patch 的是那个判定函数，
而**不是** `integration.acl_sandbox_active`（后者已退化为布尔视图，工具侧
不再读它；继续 patch 它会让本文件静默失去控制力 = 假绿）。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from hiveweave.services.acl_sandbox.policy import (
    R_CONFINED,
    R_NATIVE_CONFIG_OFF,
    make_decision,
)
from hiveweave.tools.dev_server_tools import (
    StartDevServerParams,
    start_dev_server_tool,
)

_DECISION_SEAM = "hiveweave.services.acl_sandbox.policy.resolve_spawn_decision"


def _decide(reason: str):
    """判定接缝的替身（#1 治本后的唯一控制面）。"""
    return patch(_DECISION_SEAM, new=AsyncMock(return_value=make_decision(reason)))


class _Job:
    pid = 4242

    def terminate(self) -> None:
        pass


def _patch_common(spawn_confined: AsyncMock, native: MagicMock):
    """把 spawn 之前的所有前置打桩（返回 patcher 列表）。"""
    return [
        patch("hiveweave.tools.dev_server_tools.get_project_id",
              new=AsyncMock(return_value="proj-1")),
        patch("hiveweave.tools.dev_server_tools._agent_active_verify_task",
              new=AsyncMock(return_value=None)),
        patch("hiveweave.tools.dev_server_tools.prune_dead_processes",
              new=MagicMock(return_value=None)),
        patch("hiveweave.tools.dev_server_tools.lookup_by_port",
              new=MagicMock(return_value=[])),
        patch("hiveweave.tools.dev_server_tools.stop_process_by_port",
              new=MagicMock(return_value=None)),
        patch("hiveweave.tools.dev_server_tools.allocate_project_port",
              new=MagicMock(return_value=3100)),
        patch("hiveweave.services.eval_seal.is_eval_sealed",
              new=MagicMock(return_value=False)),
        patch("hiveweave.services.eval_seal.sealed_bash_deny_for_workspace",
              new=MagicMock(return_value=None)),
        patch("hiveweave.services.acl_sandbox.service.spawn_confined",
              new=spawn_confined),
        patch("hiveweave.services.acl_sandbox.integration.resolve_project_root",
              new=AsyncMock(return_value=r"C:\fake\proj")),
        patch("hiveweave.tools.dev_server_tools.spawn_project_process",
              new=native),
    ]


@pytest.mark.asyncio
async def test_sandbox_on_routes_to_spawn_confined(tmp_path):
    """★ 验收①：沙箱开 ⇒ 走 `spawn_confined(entry="dev_server")`，原生 spawn 零调用。"""
    confined = AsyncMock(return_value={"job": _Job()})
    native = MagicMock()
    patchers = _patch_common(confined, native)
    for p in patchers:
        p.start()
    try:
        with _decide(R_CONFINED), \
             patch("hiveweave.services.acl_sandbox.integration.build_confined_argv",
                   side_effect=lambda c: ["cmd.exe", "/c", c]):
            await start_dev_server_tool(
                StartDevServerParams(command="npm run dev -- --port 3100 --strictPort", preferred_port=3100),
                "agent-1",
                str(tmp_path),
            )
    finally:
        for p in reversed(patchers):
            p.stop()

    assert confined.await_count == 1, (
        "★ 沙箱开启时必须走 spawn_confined —— 否则 start_dev_server 是"
        "「以平台身份、无沙箱执行任意 command」的缺口（#1 实证形态）"
    )
    kwargs = confined.await_args.kwargs
    assert kwargs.get("entry") == "dev_server", "entry 必须标 dev_server（对齐 bash）"
    assert kwargs.get("long_running") is True, "dev server 是长驻进程"
    assert kwargs.get("agent_id") == "agent-1"
    assert native.call_count == 0, (
        f"★ 沙箱开启时**绝不能**调 spawn_project_process（它会无沙箱 spawn）；"
        f"实际调用参数：{native.call_args}"
    )


@pytest.mark.asyncio
async def test_sandbox_unavailable_is_fail_closed(tmp_path):
    """★ 中间验收②（计划 §三 #1 修法第 2 段）：沙箱**不可用** ⇒ 拒绝，不降级。"""
    from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError

    confined = AsyncMock(side_effect=SandboxUnavailableError("sandbox down"))
    native = MagicMock()
    patchers = _patch_common(confined, native)
    for p in patchers:
        p.start()
    try:
        with _decide(R_CONFINED), \
             patch("hiveweave.services.acl_sandbox.integration.build_confined_argv",
                   side_effect=lambda c: ["cmd.exe", "/c", c]):
            result = await start_dev_server_tool(
                StartDevServerParams(command="npm run dev -- --port 3100 --strictPort", preferred_port=3100), "agent-1", str(tmp_path)
            )
    finally:
        for p in reversed(patchers):
            p.stop()

    d = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    assert d.get("success") is False, "沙箱不可用必须**拒绝**"
    assert native.call_count == 0, (
        "★ fail-closed：沙箱不可用时**绝不**静默降级为原生 spawn —— "
        "「以为在沙箱里、其实在沙箱外」比明确拒绝糟得多"
    )


@pytest.mark.asyncio
async def test_sandbox_off_falls_back_to_native(tmp_path):
    """反面对照：判定为**原生** ⇒ 回落原生路径（确认没把正常路径改死）。"""
    confined = AsyncMock()
    native = MagicMock(return_value=(None, "boom", {}))
    patchers = _patch_common(confined, native)
    for p in patchers:
        p.start()
    try:
        with _decide(R_NATIVE_CONFIG_OFF):
            await start_dev_server_tool(
                StartDevServerParams(command="npm run dev -- --port 3100 --strictPort", preferred_port=3100), "agent-1", str(tmp_path)
            )
    finally:
        for p in reversed(patchers):
            p.stop()

    assert confined.await_count == 0, "原生判定时不该走受限入口"
    assert native.call_count == 1, "原生判定 ⇒ 回落原生路径（既有行为不变）"


@pytest.mark.asyncio
async def test_native_decision_stamps_the_result(tmp_path):
    """★ #1 治本：**原生分支也要盖戳** —— 「这次没有沙箱」必须被看见。

    只在受限侧盖戳时，原生就是那个静默的默认值：一个**漏接**的工具与一个
    正确接线但沙箱关的平台，在数据里长得一模一样（#1 的真实形态是
    `start_dev_server` 从未 import 过 sandbox 而照样跑）。

    回滚探针：删掉 `entry._with_stamp` 的原生分支即转红。
    """
    from hiveweave.services.acl_sandbox.entry import spawn_agent_command

    async def _native_impl():
        return {"stdout": "x", "exit_code": 0}

    with _decide(R_NATIVE_CONFIG_OFF):
        routed = await spawn_agent_command(
            entry="dev_server", agent_id="a", workspace_path=str(tmp_path),
            workdir=str(tmp_path), project_id="proj-1",
            confined=AsyncMock(),
            native=_native_impl,
        )
    assert routed.native is True
    assert routed.result["enforcement"] == "native"
    assert routed.result["enforcement_reason"] == R_NATIVE_CONFIG_OFF
    assert routed.result["enforcement_level"] == "none"
    assert "enforcement_boundary" not in routed.result, (
        "原生侧不该有边界标记 —— 「没有边界」本身就是要被看见的事实"
    )


@pytest.mark.asyncio
async def test_confined_decision_never_silently_downgrades(tmp_path):
    """★ 入口的最后一道不变式：判定说受限、受限实现却"没结果" ⇒ **fail-closed**。

    #1 最坏的形态是「以为在沙箱里、其实在沙箱外」；静默按原生再跑一遍正是
    它的实现方式。判定与执行不一致时只能抛错。

    回滚探针：把 `spawn_agent_command` 里的 raise 改成 `return await native()` 即转红。
    """
    from hiveweave.services.acl_sandbox.entry import spawn_agent_command
    from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError

    native = AsyncMock(return_value={"stdout": "unconfined!", "exit_code": 0})
    with _decide(R_CONFINED):
        with pytest.raises(SandboxUnavailableError):
            await spawn_agent_command(
                entry="dev_server", agent_id="a", workspace_path=str(tmp_path),
                workdir=str(tmp_path), project_id="proj-1",
                confined=AsyncMock(return_value=None),
                native=native,
            )
    assert native.await_count == 0, (
        "★ 判定为受限时**绝不能**回落原生 —— 那正是「以为在沙箱里」"
    )
