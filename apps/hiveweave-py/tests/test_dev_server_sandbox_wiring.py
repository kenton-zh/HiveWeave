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


# ══════════════════════════════════════════════════════════════════
# F3（2026-09-17）：执行面戳必须活到**工具出口**，不能死在类型转换处
# ══════════════════════════════════════════════════════════════════
#
# 缺口实证：58/59/60/61 四个项目共 4863 行 run_steps，`enforcement`
# **零落库**；而同批、同 SQL、同 `record_step_end` 的 `git_hardened` 在 61
# 有 86 条。差别只在上游键名：`git_hardened` 登记进了
# `bash._SHELL_FACT_FLAG_KEYS`，`enforcement*` 4 键**两份清单都没登记**。
#
# 更隐蔽的一层：`start_dev_server_tool` 的每条出口都**新构造 ToolResult**，
# 而 `spawn_agent_command` 的戳挂在 `routed.result` 上 —— 不显式搬运即在类型
# 转换处消失（bash.py 那边因为出口返回裸 dict + 统一漏斗，戳能随 extra 走，
# 所以「同一个缺口在两条路上长得不一样」）。
#
# 回滚探针：把任一出口的 `**_stamp` 删掉即转红。


class _LiveJob:
    """受限 job 替身：`is_exited()` False ⇒ 进程活着（不进 early-exit 回执）。"""

    pid = 4242

    def terminate(self) -> None:
        pass

    def is_exited(self) -> bool:
        return False


def _confined_job_result() -> dict:
    """受限分支的成功结果（带 entry 盖的戳）。"""
    return {
        "job": _LiveJob(),
        "stdout": "", "stderr": "", "exit_code": 0, "timed_out": False,
        "error": None, "long_running": True,
        "enforcement": "confined",
        "enforcement_level": "partial",
        "enforcement_reason": R_CONFINED,
    }


class _FakeWriter:
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _FakeReader:
    pass


def _fake_open_connection(port):
    """让健康检查认为端口在监听（否则走 no-LISTEN 的 err 出口）。

    ⚠ 这是**必要**的打桩，不是为了方便：`start_dev_server` 的成功出口只
    在「进程活着 + 端口可连」之后才到达 —— 不打这个桩就永远只测到
    `_early_exit_receipt` / no-LISTEN 那两条**失败**路径，而它们恰好是
    另一组出口（本次实测：不打桩时 `success=False`、`port=None`，
    阳性对照因此不转红 ⇒ 测试是假绿）。
    """
    async def _open(host, p):
        return _FakeReader(), _FakeWriter()
    return _open


@pytest.mark.asyncio
async def test_start_dev_server_ok_export_carries_enforcement_stamp(tmp_path):
    """★ F3：`start_dev_server` **成功出口**必须带 `enforcement*`。

    不带 ⇒ `streaming.py` 取到 None ⇒ `run_steps.enforcement` 恒 NULL，
    把「适用且已判定」说成「不适用」。

    回滚探针：删掉成功出口的 `**_stamp` 即转红（本测试已用阳性对照验过）。
    """
    confined = AsyncMock(return_value=_confined_job_result())
    native = MagicMock()
    patchers = _patch_common(confined, native)
    for p in patchers:
        p.start()
    try:
        with _decide(R_CONFINED), \
             patch("hiveweave.services.acl_sandbox.integration.build_confined_argv",
                   side_effect=lambda c: ["cmd.exe", "/c", c]), \
             patch("hiveweave.tools.dev_server_tools.register",
                   new=MagicMock(return_value=MagicMock(
                       to_dict=MagicMock(return_value={})))), \
             patch("hiveweave.tools.dev_server_tools.lookup_by_project",
                   new=MagicMock(return_value=[])), \
             patch("asyncio.open_connection",
                   new=_fake_open_connection(3100)):
            result = await start_dev_server_tool(
                StartDevServerParams(
                    command="npm run dev -- --port 3100 --strictPort",
                    preferred_port=3100,
                ),
                "agent-1", str(tmp_path),
            )
    finally:
        for p in reversed(patchers):
            p.stop()

    d = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    assert d.get("success") is True, (
        f"★ 本测试必须落在**成功出口**，实测 success={d.get('success')}、"
        f"error={d.get('error')!r} —— 落错出口则它什么都不守（假绿来源）"
    )
    assert d.get("enforcement") == "confined", (
        "★ F3：成功出口丢了执行面戳 —— spawn 说 confined，回执却说不出"
        f"；实测键：{sorted(k for k in d if 'enforc' in k)}"
    )
    assert d.get("enforcement_level") == "partial", d
    assert d.get("enforcement_reason") == R_CONFINED, d


@pytest.mark.asyncio
async def test_start_dev_server_err_export_carries_enforcement_stamp(tmp_path):
    """★ F3：**失败**出口同样要带戳（观测位不随成败消失）。

    「这次到底有没有沙箱」在失败时**更需要**被看见 —— 而失败出口恰恰是最
    容易在重构里丢字段的地方（本仓在 `_shell_tool_result` 的成功分支上
    已经栽过一次：`fact_flags` 整个丢掉，见其 docstring）。
    """
    confined = AsyncMock(return_value=_confined_job_result())
    native = MagicMock()
    patchers = _patch_common(confined, native)
    for p in patchers:
        p.start()
    try:
        with _decide(R_CONFINED), \
             patch("hiveweave.services.acl_sandbox.integration.build_confined_argv",
                   side_effect=lambda c: ["cmd.exe", "/c", c]), \
             patch("hiveweave.tools.dev_server_tools.register",
                   side_effect=RuntimeError("registry down")):
            result = await start_dev_server_tool(
                StartDevServerParams(
                    command="npm run dev -- --port 3100 --strictPort",
                    preferred_port=3100,
                ),
                "agent-1", str(tmp_path),
            )
    finally:
        for p in reversed(patchers):
            p.stop()

    d = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    assert d.get("success") is False, d
    assert d.get("enforcement") == "confined", (
        "★ F3：失败出口丢了执行面戳（观测位不随成败消失）"
    )


@pytest.mark.asyncio
async def test_native_branch_receipt_carries_enforcement_stamp(tmp_path):
    """★ M1（2026-09-17 审计必修）：**原生分支**的回执也必须带 `enforcement*`。

    ## 这里原本是 F3 的一条**结构性**漏洞（不是"既有事实"）

    F3 的第一版提取是 `if isinstance(routed.result, dict): _enforcement_stamp(...)`。
    它只在**受限侧**成立 —— `routed.result` 的形状逐分支不同：
      · 受限侧：含 `job` 的 dict（戳在里面）；
      · 原生侧：`(proc, err, meta)` **三元组**，而 `entry._with_stamp`
        （entry.py:160-169）对非 dict **原样返回**。
    ⇒ 按 dict 判定 = **原生分支永远拿不到戳**，该出口照旧说不出「这次有没有
    沙箱」。修法是改从 `RoutedSpawn` 自身取：`_stamp = routed.stamp()` ——
    戳挂在 `decision` 上（entry.py:72），与 result 的形状无关。

    ⚠ 本测试**必须落在原生分支**且**必须**走「原生 spawn 成功、进程活着、
    端口可连」这条路 —— 否则它与 `**_stamp` 是否正确注入无关（落错出口
    就是假绿，本仓已实测过两次）。

    回滚探针：把 `_stamp = routed.stamp()` 改回 `if isinstance(routed.result,
    dict): _enforcement_stamp(routed.result)` 即转红（**本轮实测转红**）。
    """
    # 原生 spawn 成功：返回 (proc, err, meta) 三元组，err=None ⇒ 不进 err 出口
    class _NativeProc:
        pid = 9911

        def poll(self) -> int | None:
            return None  # 活着 ⇒ 不进 _early_exit_receipt

        def terminate(self) -> None:
            pass

    native = MagicMock(return_value=(_NativeProc(), None, {"command": "npm run dev"}))
    confined = AsyncMock()
    patchers = _patch_common(confined, native)
    for p in patchers:
        p.start()
    try:
        with _decide(R_NATIVE_CONFIG_OFF), \
             patch("hiveweave.tools.dev_server_tools.register",
                   new=MagicMock(return_value=MagicMock(
                       to_dict=MagicMock(return_value={})))), \
             patch("hiveweave.tools.dev_server_tools.lookup_by_project",
                   new=MagicMock(return_value=[])), \
             patch("asyncio.open_connection", new=_fake_open_connection(3100)):
            result = await start_dev_server_tool(
                StartDevServerParams(
                    command="npm run dev -- --port 3100 --strictPort",
                    preferred_port=3100,
                ),
                "agent-1", str(tmp_path),
            )
    finally:
        for p in reversed(patchers):
            p.stop()

    d = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    assert d.get("success") is True, (
        f"★ 本测试必须落在**原生成功出口**；实测 success={d.get('success')}、"
        f"error={d.get('error')!r} —— 落错出口则它对 M1 视而不见（假绿）"
    )
    assert d.get("enforcement") == "native", (
        "★ M1：原生分支的回执丢了执行面戳 —— 「这次没有沙箱」必须被**看见**，"
        "否则一个漏接的工具与一个正确接线但沙箱关的平台在数据里长得一模一样"
        f"；实测键：{sorted(k for k in d if 'enforc' in k)}"
    )
    assert d.get("enforcement_level") == "none", d
    assert d.get("enforcement_reason") == R_NATIVE_CONFIG_OFF, d
    assert "enforcement_boundary" not in d, (
        "原生侧不该有边界标记 —— 「没有边界」本身就是要被看见的事实，"
        "补一个假边界会让下游读成「有限制」（policy.stamp 的设计意图）"
    )


@pytest.mark.asyncio
async def test_native_branch_err_receipt_carries_enforcement_stamp(tmp_path):
    """★ M1：原生 spawn **失败**（三元组 err 非空）时同样要带戳。

    单列一条是因为它走的是另一条出口（`:440` 附近的 `spawn_err or proc is
    None`），与上一条的成功出口不共路径。

    回滚探针：同上（改回 dict 判定即转红）。
    """
    native = MagicMock(return_value=(None, "spawn boom", {}))
    confined = AsyncMock()
    patchers = _patch_common(confined, native)
    for p in patchers:
        p.start()
    try:
        with _decide(R_NATIVE_CONFIG_OFF):
            result = await start_dev_server_tool(
                StartDevServerParams(
                    command="npm run dev -- --port 3100 --strictPort",
                    preferred_port=3100,
                ),
                "agent-1", str(tmp_path),
            )
    finally:
        for p in reversed(patchers):
            p.stop()

    d = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    assert d.get("success") is False, d
    assert "spawn boom" in (d.get("error") or ""), d.get("error")
    assert d.get("enforcement") == "native", (
        "★ M1：原生分支的**失败**出口也丢了戳 —— 观测位不随成败消失"
    )


def test_early_exit_receipt_carries_enforcement_stamp(tmp_path):
    """★ F3：健康窗口内秒退的两条回执（ok 与 err）都要带戳。

    这两条出口的语义里，进程**确实被 spawn 出来过**（只是很快退了）⇒
    「有没有沙箱」这条观测是**适用且已判定**的，不能留 NULL。
    """
    from hiveweave.tools.dev_server_tools import _early_exit_receipt

    stamp = {"enforcement": "confined", "enforcement_level": "partial",
             "enforcement_reason": R_CONFINED}
    log_path = tmp_path / "dev.log"
    log_path.write_text("boom\n", encoding="utf-8")

    ok_d = _early_exit_receipt(0, "cmd", log_path, stamp).to_dict()
    assert ok_d["success"] is True
    assert ok_d.get("enforcement") == "confined", ok_d

    err_d = _early_exit_receipt(1, "cmd", log_path, stamp).to_dict()
    assert err_d["success"] is False
    assert err_d.get("enforcement") == "confined", err_d
    # 不传 stamp 时保持既有行为（缺键不补默认值 —— NULL ≠ 说了否）
    none_d = _early_exit_receipt(0, "cmd", log_path).to_dict()
    assert "enforcement" not in none_d, (
        "缺键不补默认值：把「没这条信息」写成 native 就是 NULL 说谎"
    )


@pytest.mark.asyncio
async def test_shell_fact_flag_keys_carry_enforcement_for_shell_paths(tmp_path):
    """★ F3：shell 路的 `_ff` 过滤**必须**放行 `enforcement*`。

    这是 F3 的主丢失点：八处出口都正确调用了 `_enforcement_stamp(result)`，
    而 `_shell_tool_impl` / `run_command_tool` 用 `_SHELL_FACT_FLAG_KEYS`
    做过滤时把 4 个键全滤掉 ⇒ 回执里没有该键。
    """
    from hiveweave.tools import bash as bash_mod
    from hiveweave.services.acl_sandbox.policy import SPAWN_STAMP_KEYS

    assert set(SPAWN_STAMP_KEYS) <= set(bash_mod._SHELL_FACT_FLAG_KEYS), (
        "spawn 面戳必须整体在 shell 白名单里 —— 否则在这层被过滤掉，"
        "run_steps.enforcement 恒 NULL（实证 4863 行零落库）"
    )


@pytest.mark.asyncio
async def test_start_dev_server_no_listen_export_carries_enforcement_stamp(tmp_path):
    """★ F3：`no non-reserved LISTEN port was observed` 出口也要带戳。

    ⚠ 为什么单列一条而不是并进上一测试：这条出口**只在端口连不上时**到达。
    上一测试打了 `open_connection` 的桩 ⇒ 永远走成功出口，对这条**视而不见**
    —— 本次实测过这个陷阱：不打桩时上一测试的 `success` 是 False，
    阳性对照因此不转红（假绿）。两条出口各要一条测试。

    回滚探针：删掉该出口的 `**_stamp` 即转红（本轮已用阳性对照验过）。
    """
    confined = AsyncMock(return_value=_confined_job_result())
    native = MagicMock()
    patchers = _patch_common(confined, native)
    for p in patchers:
        p.start()
    try:
        with _decide(R_CONFINED), \
             patch("hiveweave.services.acl_sandbox.integration.build_confined_argv",
                   side_effect=lambda c: ["cmd.exe", "/c", c]), \
             patch("asyncio.open_connection",
                   side_effect=OSError("refused")):
            result = await start_dev_server_tool(
                StartDevServerParams(
                    command="npm run dev -- --port 3100 --strictPort",
                    preferred_port=3100,
                ),
                "agent-1", str(tmp_path),
            )
    finally:
        for p in reversed(patchers):
            p.stop()

    d = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    assert d.get("success") is False, f"应落在 no-LISTEN 出口；实测 {d}"
    assert "no non-reserved LISTEN" in (d.get("error") or ""), d.get("error")
    assert d.get("enforcement") == "confined", (
        "★ F3：no-LISTEN 出口丢了执行面戳 —— 进程被 spawn 过了，"
        f"「有没有沙箱」这条观测是适用的；实测键："
        f"{sorted(k for k in d if 'enforc' in k)}"
    )
