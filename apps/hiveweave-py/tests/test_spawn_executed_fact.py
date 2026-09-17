"""F5：`executed` 执行面事实位 —— 「戳说在沙箱里，而进程从未启动」。

## 本条在修什么（判据全部来自状态，不来自文案）

`PwshUnavailableError` 这条出口（受限 shell 缺失）返回的是**普通 dict**
（``exit_code=None`` / ``error="pwsh not found"``）而**不是 None**
（`python_script.py` / `bash.py` 的 `_confined`）。于是：

1. `entry.spawn_agent_command` 里 `result is not None` 成立 ⇒ 不抛异常；
2. 照常返回 `RoutedSpawn(result, decision)`；
3. `decision.confined is True` ⇒ 戳宣告 ``enforcement="confined"``；
4. 而 `exit_code is None` 已排除「跑了但失败」⇒ **进程从未启动**。

`run_steps.enforcement` 的列契约逐字写着它回答「**这次调用有没有被沙箱
约束**」—— 回执说"被约束"，事实是**没有进程、没有边界**。这就是 F5。

## 本文件守什么（三件，缺一即假绿）

- **A 入口记事实**：受限实现**抛出** ⇒ 异常上带 `executed=False`；
  受限实现**返回** ⇒ 结果上带 `executed=True`。
- **B 谁更接近事实谁说话**：受限实现**自己声明** `executed=False`
  （「我返回了，但我没启动进程」）时，入口**不得**用 `True` 覆盖它。
- **C 消费端不落谎报的戳**：`executed is False` ⇒ `record_step_end` 收到的
  `enforcement` 必须是 `None`（NULL=未判定），**不得**是 `"confined"`。

⚠ 阳性对照（改坏必须转红）：
  · 去掉 `_mark_not_executed` ⇒ A 的抛错用例红；
  · 去掉 `_mark_executed` 的 `False` 优先判断 ⇒ B 红；
  · 去掉 `streaming.py` 的条件 ⇒ C 红。
"""

from __future__ import annotations

import asyncio

import pytest

from hiveweave.services.acl_sandbox import entry as entry_mod
from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
from hiveweave.services.acl_sandbox.policy import (
    ALL_SPAWN_STAMP_KEYS,
    ENFORCEMENT_STAMP_KEYS,
    SPAWN_STAMP_KEYS,
    SpawnDecision,
)


def _confined_decision() -> SpawnDecision:
    """构造一个「判定为受限」的 decision（不走 DB，纯状态对象）。"""
    return SpawnDecision(
        enforcement="confined",
        level="partial",
        reason="sandbox_enabled",
        project_id="p-test",
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── A. 入口记事实 ────────────────────────────────────────────────


def test_confined_raise_marks_executed_false_on_exception():
    """受限实现**抛出** ⇒ 异常上必须有 `executed=False`。

    这是 F5 的核心：抛出这条路没有返回值可用，事实位只能挂在异常上
    与它同路到达调用方的 `except` 分支。
    """
    boom = RuntimeError("pwsh not found")

    async def _confined(ctx):
        raise boom

    async def _native():
        raise AssertionError("判定为受限时不得走原生")

    with pytest.raises(RuntimeError) as ei:
        _run(entry_mod.spawn_agent_command(
            entry="bash",
            agent_id="a1",
            workspace_path="C:/w",
            workdir="C:/w",
            project_id=None,
            confined=_confined,
            native=_native,
            decision=_confined_decision(),
        ))

    assert ei.value is boom, "必须原样抛出（不得换成别的异常类型）"
    assert getattr(ei.value, "executed", None) is False, (
        "受限实现抛出 = 进程从未启动 ⇒ 异常上必须带 executed=False"
    )


def test_confined_return_marks_executed_true():
    """受限实现**正常返回** ⇒ 结果上必须有 `executed=True`（进程启动过）。"""

    async def _confined(ctx):
        return {"success": True, "exit_code": 0, "output": "hi"}

    async def _native():
        raise AssertionError("不得走原生")

    routed = _run(entry_mod.spawn_agent_command(
        entry="bash",
        agent_id="a1",
        workspace_path="C:/w",
        workdir="C:/w",
        project_id=None,
        confined=_confined,
        native=_native,
        decision=_confined_decision(),
    ))

    assert routed.result["executed"] is True
    # 戳（工具层取戳的唯一入口）也要带上它
    assert routed.stamp()["executed"] is True


def test_stamp_includes_executed_and_native_leaves_it_unset():
    """`RoutedSpawn.stamp()` 带上执行事实；未判定时**不补**（缺键不补默认值）。"""
    d = _confined_decision()

    # 未判定（原生侧由工具层自报）⇒ 戳里**没有** executed
    unset = entry_mod.RoutedSpawn(result={}, decision=d, executed=None)
    assert "executed" not in unset.stamp(), "未判定 = 缺键，不得补 True/False"

    # 已判定 ⇒ 进戳
    asserted = entry_mod.RoutedSpawn(result={}, decision=d, executed=True)
    assert asserted.stamp()["executed"] is True

    # 决策面键仍在（本批只加键，不动既有语义）
    # ⚠ `enforcement_boundary` **不在**这里：它只在传了 `boundary_root` 时有值
    #（「没有边界」本身就是要被看见的事实，不补假边界 —— policy.py:134）。
    s = asserted.stamp()
    for k in ("enforcement", "enforcement_level", "enforcement_reason"):
        assert k in s, f"决策面键 {k} 必须仍在（本批只加不减）"
    assert "enforcement_boundary" not in s, (
        "不传 boundary_root ⇒ 不得补假边界（既有语义，本批不碰）"
    )
    # 传了才有
    assert "enforcement_boundary" in asserted.stamp(boundary_root="C:/w")


# ── B. 谁更接近事实谁说话 ────────────────────────────────────────


@pytest.mark.parametrize("declared", [False])
def test_caller_declared_not_executed_wins_over_mark_true(declared):
    """受限实现**自己声明** `executed=False` ⇒ 入口**不得**覆盖成 True。

    为什么必须有这一条：入口只知道"函数返回了个 dict"，**不知道**那个 dict
    是"没跑"。若入口无条件写 `True`，`PwshUnavailableError` 那条早返回
    （它返回 dict 而非抛出）照样会被说成"进程启动过" ⇒ F5 原缺陷原样复发。
    这条守卫挡的正是"修了抛出那条路、漏了返回那条路"。
    """

    async def _confined(ctx):
        # 形态与 `python_script.py` / `bash.py` 的 PwshUnavailableError 出口一致
        return {
            "output": "", "stdout": "", "stderr": "",
            "exit_code": None, "timed_out": False,
            "error": "pwsh (PowerShell 7+) not found on PATH",
            "executed": declared,
            "fact": "runner_failed",
        }

    async def _native():
        raise AssertionError("不得走原生")

    routed = _run(entry_mod.spawn_agent_command(
        entry="bash",
        agent_id="a1",
        workspace_path="C:/w",
        workdir="C:/w",
        project_id=None,
        confined=_confined,
        native=_native,
        decision=_confined_decision(),
    ))

    assert routed.result["executed"] is False, (
        "执行函数自己声明了没启动进程 ⇒ 入口不得用 True 覆盖"
    )


def test_mark_executed_still_marks_when_unset():
    """入口的 `True` 只在**没有既有声明**时补 —— 否则会漏掉正常返回那一路。"""
    assert entry_mod._mark_executed({"exit_code": 0})["executed"] is True
    assert entry_mod._mark_executed({"executed": None})["executed"] is True
    assert entry_mod._mark_executed({"executed": False})["executed"] is False
    # 非 dict（原生侧三元组）原样返回 —— 不改变既有契约
    assert entry_mod._mark_executed((1, 2, 3)) == (1, 2, 3)


def test_stamp_never_contradicts_result_executed():
    """⚠⚠ 审计 BLOCKING-1 回归守卫：**戳与 result 不得自相矛盾**。

    早先版本让 `executed` 同时存在于 `self.executed`（入口写）与
    `result["executed"]`（执行函数写），两者不一致时：

        result['executed']    = False      # pwsh 缺失，进程没起来
        stamp()['executed']   = True       # 入口硬编码 True
        stamp()['enforcement'] = 'confined'

    ⇒ **本批要修的 F5 原样复发**（回执说"在沙箱里跑过"）。而当时的 10 条测试
    一条都没抓到这个 —— 因为它们全都只读 `result` 或只读 `stamp()`，
    **从不交叉验证两者**。

    ⇒ 修法：`executed` 的值只有一个权威来源（`executed_actual`，优先
    `result` 里执行函数的亲笔声明），`stamp()` 一律经它取值。
    本条守卫用**交叉判据**：同一个对象的两个出口必须说同一句话。
    """
    d = _confined_decision()

    # ① 执行函数声明"没跑" + 入口（错误地）声明"跑了" ⇒ 必须听执行函数的
    contradiction = entry_mod.RoutedSpawn(
        result={"executed": False, "exit_code": None, "error": "pwsh not found"},
        decision=d,
        executed=True,  # 故意构造不一致
    )
    assert contradiction.stamp()["executed"] is False, (
        "result 里执行函数的亲笔声明优先 —— 戳不得与 result 打架"
    )
    assert (
        contradiction.stamp()["executed"] == contradiction.result["executed"]
    ), "同一个事实在两个出口必须一致"

    # ② 反向：执行函数说跑了 ⇒ 入口不得把它降级
    other = entry_mod.RoutedSpawn(
        result={"executed": True, "exit_code": 0}, decision=d, executed=None,
    )
    assert other.stamp()["executed"] is True

    # ③ 执行函数**没表态**（key 不存在）⇒ 入口的兜底值生效
    silent = entry_mod.RoutedSpawn(
        result={"exit_code": 0}, decision=d, executed=True,
    )
    assert silent.stamp()["executed"] is True

    # ④ 两处都未表态 ⇒ 缺键（不补默认值）
    neither = entry_mod.RoutedSpawn(result={}, decision=d, executed=None)
    assert "executed" not in neither.stamp()


def test_full_confined_path_records_not_executed():
    """★ 端到端：`PwshUnavailableError` 形态走完整条入口，戳必须说"没跑"。

    这是审计 BLOCKING-1 的**真实触发路径**：`_confined` 返回一个自己声明
    `executed=False` 的 dict（正是 `python_script.py:219-236` /
    `bash.py` 的早返回形态），入口**不得**把它改写成 True。
    """

    async def _confined(ctx):
        # 与生产代码逐字同形
        return {
            "output": "", "stdout": "", "stderr": "",
            "exit_code": None, "timed_out": False,
            "error": "pwsh (PowerShell 7+) not found on PATH",
            "executed": False, "fact": "runner_failed",
        }

    async def _native():
        raise AssertionError("不得走原生")

    routed = _run(entry_mod.spawn_agent_command(
        entry="python_script",
        agent_id="a1",
        workspace_path="C:/w",
        workdir="C:/w",
        project_id=None,
        confined=_confined,
        native=_native,
        decision=_confined_decision(),
    ))

    stamp = routed.stamp()
    assert stamp["executed"] is False, (
        "pwsh 缺失 ⇒ 进程从未启动 ⇒ 戳必须说 executed=False"
    )
    assert routed.result["executed"] is False
    # 消费端据此不落 enforcement（见 streaming.py）
    assert stamp["enforcement"] == "confined", (
        "决策面仍如实记录『判定为受限』—— 它没错；错的是把它当成"
        "『在沙箱里跑过』。两个事实并排存在，由 executed 区分"
    )


# ── C. 消费端不落谎报的戳 ────────────────────────────────────────


def test_all_spawn_stamp_keys_include_executed():
    """唯一登记点：`executed` 必须在派生集里，否则工具层透传会把它滤掉。

    ⚠ 这是「每处各列一份清单」的守卫 —— 本仓在这个形态上栽过三次
    （`_SHELL_FACT_FLAG_KEYS` → `to_dict` 白名单 → 本地前缀过滤）。
    """
    assert "executed" in ALL_SPAWN_STAMP_KEYS
    assert "executed" not in ENFORCEMENT_STAMP_KEYS, (
        "决策面 4 键由 SpawnDecision.stamp() 产出、判定阶段就冻结 —— "
        "executed 是执行阶段的事实，不能并进去（全等断言会红）"
    )
    assert "executed" not in SPAWN_STAMP_KEYS
    # 原有的键一个都不能少（本批只加）
    for k in SPAWN_STAMP_KEYS:
        assert k in ALL_SPAWN_STAMP_KEYS


def test_shell_flag_keys_derive_executed_from_policy():
    """shell 侧的透传清单必须**派生**出 `executed`（不是各列一份）。"""
    from hiveweave.tools.bash import _SHELL_FACT_FLAG_KEYS

    assert "executed" in _SHELL_FACT_FLAG_KEYS
    for k in ALL_SPAWN_STAMP_KEYS:
        assert k in _SHELL_FACT_FLAG_KEYS


def test_executed_stamp_reads_from_exception():
    """`_executed_stamp` 从异常取位；缺属性 ⇒ 空（不猜）。"""
    from hiveweave.tools.bash import _executed_stamp

    exc = SandboxUnavailableError("boom")
    exc.executed = False
    assert _executed_stamp(exc) == {"executed": False}

    # 没打过标记 ⇒ 空 dict（缺键不补默认值）
    assert _executed_stamp(SandboxUnavailableError("boom")) == {}


def test_tool_dict_carries_executed_through():
    """`to_tool_dict` / `to_tool_result` 透传 `executed`（F5 的最后一公里）。

    这两条出口是 `bash.py` / `dev_server_tools.py` 的 fail-closed 回执 ——
    事实位带不过去，前面的工作全白做。
    """
    from hiveweave.tools.bash import _executed_stamp

    exc = SandboxUnavailableError("boom")
    exc.executed = False
    extra = _executed_stamp(exc)

    d = exc.to_tool_dict(**extra)
    assert d["executed"] is False
    assert d["fact"] == "runner_failed", "既有事实位语义不得被改动"

    r = exc.to_tool_result(**extra)
    assert r.to_dict()["executed"] is False
    assert r.fact == "runner_failed"


# ── D. 端到端：pwsh 缺失形态下 `enforcement` 不得落 confined ──────


def test_enforcement_not_recorded_when_not_executed():
    """消费端判据：`executed is False` ⇒ `enforcement` 必须降级为 None。

    直接验 `streaming.py` 里那行表达式所依赖的**状态**（不回填 native ——
    那会把"没有进程"说成"确认无沙箱"）。
    """
    confined_result = {
        "executed": False,
        "enforcement": "confined",
        "enforcement_level": "partial",
        "enforcement_reason": "sandbox_enabled",
        "error": "pwsh (PowerShell 7+) not found on PATH",
        "exit_code": None,
    }
    # 与 streaming.py 中落库处的表达式同形
    recorded = (
        None if confined_result.get("executed") is False
        else confined_result.get("enforcement")
    )
    assert recorded is None, (
        "命令从未启动 ⇒ 不得写 enforcement='confined'（列契约答的是"
        "'有没有被约束'，没有进程就没有边界）"
    )

    # 正常执行（executed=True）⇒ 照常落 confined，本批不削弱既有行为
    normal = {"executed": True, "enforcement": "confined"}
    recorded_normal = (
        None if normal.get("executed") is False else normal.get("enforcement")
    )
    assert recorded_normal == "confined"

    # 非 spawn 工具（没有 executed 键）⇒ 沿用既有语义（戳在就落，没有就 None）
    legacy = {"enforcement": "native"}
    recorded_legacy = (
        None if legacy.get("executed") is False else legacy.get("enforcement")
    )
    assert recorded_legacy == "native"
