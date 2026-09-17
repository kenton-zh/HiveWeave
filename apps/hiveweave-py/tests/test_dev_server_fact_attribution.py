"""F5 补充：`start_dev_server` 的 `except Exception` 出口 —— **归因不得吞掉真 bug**。

## 本文件在守什么（2026-09-17 第二轮审计必修）

`dev_server_tools.start_dev_server_tool` 的 `except Exception` 出口要补 `fact`
（这条路上"命令从未启动"，与 `SandboxUnavailableError.to_tool_result` 的 L6
定档同源）。首版判据是 ``if _executed_stamp(e):`` —— **恒为真**：

  `service.py:1105-1107` 把**一切**意外异常（含 `TypeError` / `AttributeError`
  这类真代码 bug）都包成 ``SandboxUnavailableError(...) from e``；该异常冒到
  `entry.py:189-196` 后被 `_mark_not_executed` 打上 ``executed=False``
  ⇒ `_executed_stamp(e)` **恒非空** ⇒ 条件恒真。

于是注释里承诺的「不把真代码 bug 判成平台故障」**没有落地**（审计原话：
「注释把已发生的事写成了未来风险」）—— 根因是**用 `executed` 去判 `fact`**，
两个正交事实混进一个字段（F5 那一族的老病）。

## 为什么本文件必须**端到端**驱动工具

只测 `_is_platform_side` 本身是**假绿**：把调用点改回 ``if _ex_stamp:``，
helper 的单元测试照样全绿（本轮已实测：5 passed, EXIT=0）。判据必须落在
**调用点**（工具返回的 `fact`）才守得住。

⚠ 本文件的三条**必须**各走各的出口（落错出口即假绿，本仓已实测过两次）：
  · 平台侧（`api_name` 非空 / pwsh 缺失）⇒ `fact="runner_failed"`；
  · 真代码 bug（`TypeError` 被包）⇒ **不得**是 `runner_failed`；
  · `executed` 事实位在两种情形下都要活到回执。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
from hiveweave.services.acl_sandbox.policy import R_CONFINED, make_decision
from hiveweave.tools.dev_server_tools import (
    StartDevServerParams,
    start_dev_server_tool,
)

_DECISION_SEAM = "hiveweave.services.acl_sandbox.policy.resolve_spawn_decision"

_CMD = "npm run dev -- --port 3100 --strictPort"


def _decide():
    return patch(_DECISION_SEAM, new=AsyncMock(return_value=make_decision(R_CONFINED)))


def _patch_common(spawn_confined, native):
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


async def _run(confined, native, tmp_path):
    """驱动工具并返回回执 dict（全部走受限分支 ⇒ 落 `except Exception` 出口）。"""
    patchers = _patch_common(confined, native)
    for p in patchers:
        p.start()
    try:
        with _decide(), \
             patch("hiveweave.services.acl_sandbox.integration.build_confined_argv",
                   side_effect=lambda c: ["cmd.exe", "/c", c]):
            result = await start_dev_server_tool(
                StartDevServerParams(command=_CMD, preferred_port=3100),
                "agent-1", str(tmp_path),
            )
    finally:
        for p in reversed(patchers):
            p.stop()
    return result.to_dict() if hasattr(result, "to_dict") else dict(result)


@pytest.mark.asyncio
async def test_code_bug_must_not_be_attributed_to_platform(tmp_path):
    """⭐ 真代码 bug（`TypeError` 被 `SandboxUnavailableError` 包住）⇒ **不得**
    落 `fact="runner_failed"`。

    回滚探针（**实测转红**）：把 `dev_server_tools.py` 的
    `if _is_platform_side(e):` 改回 `if _ex_stamp:`（恒真）即转红 ——
    agent 会收到「不是你的问题」而放弃自查真 bug。
    """
    bug = TypeError("unsupported operand type(s) for +: 'int' and 'str'")
    wrapped = SandboxUnavailableError(f"ACL sandbox execution failed: {bug}")
    wrapped.__cause__ = bug

    d = await _run(AsyncMock(side_effect=wrapped), MagicMock(), tmp_path)

    assert d.get("success") is False, d
    assert d.get("fact") != "runner_failed", (
        "★ 真代码 bug 被判成了平台侧归因 —— 承诺的「不把真 bug 判成平台故障」"
        f"没有落地（死代码恒真）；实测 fact={d.get('fact')!r}"
    )


@pytest.mark.asyncio
async def test_win32_failure_is_attributed_to_platform(tmp_path):
    """平台侧（`api_name` 非空 = 真调了 Win32 API）⇒ `fact="runner_failed"`。

    与上一条**成对**：只守"不误判"会把条件改成恒假（另一头的假绿）——
    平台故障必须仍能被归因，否则 agent 会去自查一个不存在的 bug。
    """
    exc = SandboxUnavailableError(
        "SetNamedSecurityInfo failed", api_name="SetNamedSecurityInfo", win32_code=5
    )
    d = await _run(AsyncMock(side_effect=exc), MagicMock(), tmp_path)

    assert d.get("success") is False, d
    assert d.get("fact") == "runner_failed", (
        f"平台侧故障没被归因（真实原因：Win32 API 失败）；实测 fact={d.get('fact')!r}"
    )


@pytest.mark.asyncio
async def test_pwsh_missing_is_attributed_to_platform(tmp_path):
    """受限 shell 缺失（`PwshUnavailableError` 在异常链里）⇒ `runner_failed`。"""
    from hiveweave.services.acl_sandbox.integration import PwshUnavailableError

    exc = SandboxUnavailableError("ACL sandbox execution failed")
    exc.__cause__ = PwshUnavailableError("pwsh not found on PATH")

    d = await _run(AsyncMock(side_effect=exc), MagicMock(), tmp_path)

    assert d.get("success") is False, d
    assert d.get("fact") == "runner_failed", (
        f"pwsh 缺失（平台侧）没被归因；实测 fact={d.get('fact')!r}"
    )


@pytest.mark.asyncio
async def test_executed_fact_survives_on_this_exit(tmp_path):
    """⭐ 两个正交事实都要活到回执：`executed=False` + 成因 `fact`。

    这条出口是 F5 的**第三处同族落点**（另两处在 `bash.py` / `python_script.py`）
    —— 审计探针 J2 实测此处原先两个事实位**全丢**
    （``{'success': False, 'error': '…', 'blocked': False}``、
    ``executed=None`` / ``fact=None``）。
    """
    exc = SandboxUnavailableError(
        "SetNamedSecurityInfo failed", api_name="SetNamedSecurityInfo", win32_code=5
    )
    d = await _run(AsyncMock(side_effect=exc), MagicMock(), tmp_path)

    assert d.get("executed") is False, (
        "★ `executed=False` 没活到回执 —— 「命令从未启动」这个事实丢了，"
        f"下游只剩「被拒绝了」这个结论；实测 executed={d.get('executed')!r}"
    )
