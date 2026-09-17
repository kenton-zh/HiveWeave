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

# F 组（落库面）复用 0-3 那套现成夹具：`task_env` 建项目库 + workspace，
# `ledger_env` 额外把 agent→workspace 塞进 project_db 缓存（`record_step_start`
# 走 `get_project_db_for_agent`，不塞会拿 "agent not registered in Meta DB"）。
# ⚠ 复用而不是自己造：那套夹具的注释已论证"走缓存是设计上的合法路径"，
#   且 `EXEC` 与 `record_step_end` 的真实签名同源。
# ⚠ `ledger_env` **依赖 `task_env`** —— 两者都要 import 进本模块命名空间，
#   只 import 后者会报 "fixture 'task_env' not found"（本轮实测）。
from tests.test_git_hardening_consumer import ledger_env  # noqa: F401
from tests.test_idle_architecture_p0 import EXEC, task_env  # noqa: F401


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


# ════════════════════════════════════════════════════════════════════
# E 组（2026-09-17 审计必修后新增）：三条**同一批里漏掉的落点**
#
# 上一轮审计用独立探针抓到：本批只修了「pwsh 缺失 ⇒ 返回普通 dict」这一条
# 主路径，而**同族还有三处**，且其中一处的谎报**连日志都没有**。以下每条
# 对应一个审计发现，判据都要求"缺陷回来它必须转红"。
# ════════════════════════════════════════════════════════════════════


def test_bash_prep_err_early_return_declares_not_executed():
    """E1（审计 BLOCKING）：`bash.py` 的 `prep_err` 早返回也必须声明 False。

    ⚠ 这条是**本批第二处 F5 落点**，且比第一处更隐蔽 —— 上一轮把它归成
    "同族遗留、未进范围"，实测它原样复发：

        result['executed'] = True
        stamp() = {'enforcement': 'confined', …, 'executed': True}

    `entry._mark_executed` 按「函数返回了 ⇒ 启动过」补 `True`，而命令
    **根本没启动**；又因为值是 `True`——不是 `False`——`streaming.py` 的
    fail-loud 告警（条件 `is False`）**也不触发** ⇒ **谎报且无声**。

    判据用 AST **从源码取常量**而不是 import 后读行为：本函数在真实
    `_bash` 闭包内、依赖整套 `prepared`/`ctx`，直接跑成本过高；而这里要钉住的
    恰恰是「**这个 return 语句里有没有那个键**」这条字面事实。
    阳性对照：把 `"executed": False,` 从该 dict 里删掉 ⇒ 本用例转红。
    """
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "src" / "hiveweave" / "tools" / "bash.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 定位：`if prep_err:` 分支里那个 `return finalize_fact_dict({...})`
    hits: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        cond = ast.unparse(node.test).strip()
        if cond != "prep_err":
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if ast.unparse(sub.func).strip() != "finalize_fact_dict":
                continue
            for arg in sub.args:
                if isinstance(arg, ast.Dict):
                    hits.append({
                        k.value: ast.unparse(v)
                        for k, v in zip(arg.keys, arg.values)
                        if isinstance(k, ast.Constant)
                    })

    assert hits, (
        "没找到 `if prep_err:` 里的 finalize_fact_dict 返回 —— 结构变了，"
        "本守卫已失效（**不是**缺陷消失），请对照 bash.py 更新定位"
    )
    for d in hits:
        assert "executed" in d, (
            f"prep_err 早返回缺 `executed` ⇒ `_mark_executed` 会补 True ⇒ "
            f"戳宣告「在沙箱里」而命令从未启动；且 `is False` 的 fail-loud "
            f"不触发 ⇒ 谎报无声。实际键：{sorted(d)}"
        )
        assert d["executed"] == "False", (
            f"prep_err 早返回的 executed 必须是 False（命令从未启动），"
            f"实际 {d['executed']!r}"
        )
        # ⚠ 与 fact 正交：两类成因（runner_failed / bad_args）都不该被改写
        assert "fact" in d and d["fact"] == "_prep_fact", (
            f"executed 是执行事实、fact 是成分类别，二者正交 —— "
            f"不得因补 executed 而改 fact（实际 {d.get('fact')!r}）"
        )


@pytest.mark.asyncio
async def test_entry_result_none_branch_marks_not_executed():
    """E2（审计 LOW）：`entry.py` 的「返回 None」fail-closed 分支同样打标。

    该分支与「受限实现抛出」**完全同档**：判定说 confined、执行面没起来。
    原先直接 `raise SandboxUnavailableError(...)` 不经 `_mark_not_executed`
    ⇒ 调用方 `_executed_stamp(e)` 取不到属性返回 `{}` ⇒ 这条出口第三次回到
    "沉默的默认值"（审计 B3 的实证形态）。
    """
    from hiveweave.services.acl_sandbox.entry import (
        spawn_agent_command as _spawn,
    )
    from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
    from hiveweave.tools.bash import _executed_stamp

    async def _native():
        return {}

    async def _confined_none(ctx):
        return None  # 「判定说 confined、实现返回空」的 fail-closed 路

    with pytest.raises(SandboxUnavailableError) as ei:
        await _spawn(
            entry="probe",
            agent_id="a1",
            workspace_path="C:/w",
            workdir="C:/w",
            project_id=None,
            native=_native,
            confined=_confined_none,
            decision=_confined_decision(),
        )

    assert _executed_stamp(ei.value) == {"executed": False}, (
        "「受限实现返回 None」这条 fail-closed 出口也必须带上 executed=False —— "
        "否则它与「非 spawn 工具」在数据里同形，下游分不出「没跑」与「不适用」"
    )
    # 既有契约一字不动：类型与文案都不许被这次改动碰到
    assert "returned no result" in str(ei.value)


# ════════════════════════════════════════════════════════════════════
# F 组：**落库面** —— 审计核心质疑 ④ 的守卫
#
# 上一版只把「executed is False ⇒ enforcement 落 NULL」写进 streaming，
# 却**没把 executed 本身落库** ⇒ 三种语义完全不同的情况在数据里同形：
#   ① executed=False 判定成立但没跑 ← 要捞的正是这个
#   ② 非 spawn 工具（write_file）—— 不适用
#   ③ executed=None 未判定
# ⇒ 「宣告了沙箱却根本没跑」在数据上**不可查** ——
# **这正是 F5 的病本身**（两个正交事实挤进一个字段）。
# ════════════════════════════════════════════════════════════════════


def test_run_steps_has_executed_column_and_migration():
    """列 + 迁移**都**要有：只改 CREATE ⇒ 新库有列、老库永远没有。"""
    from hiveweave.db.schema import PROJECT_DB_COLUMN_CHECKS, PROJECT_DB_TABLES

    assert "executed" in PROJECT_DB_COLUMN_CHECKS["run_steps"], (
        "executed 未登记进启动自检 ⇒ 迁移断裂不会被 fail-loud 抓住，"
        "「宣告了沙箱却没跑」会再次静默退化成 NULL"
    )
    assert any(
        s.strip().endswith("ALTER TABLE run_steps ADD COLUMN executed INTEGER")
        for s in PROJECT_DB_TABLES
    ), "迁移未登记 —— 已有项目库永远不会长出这一列"

    # ⚠ F5 反回归：CREATE 里的该列**不得带 DEFAULT**（本仓已栽过：
    # SQLite 会给存量行回填 DEFAULT 值 ⇒ 方向写反，见 fixqueue「新列的迁移
    # 形态本身就是判据」）。`executed` 的语义是"未知=NULL"，不是"默认没跑"。
    # ⚠ 定位必须**精确匹配表名**：其他语句的注释里也出现过 "run_steps"
    #（如 project_meta 的注释）⇒ 用子串 `next()` 会抓错条目（本轮首跑即栽）。
    create = next(
        s for s in PROJECT_DB_TABLES
        if "CREATE TABLE IF NOT EXISTS run_steps (" in s
    )
    line = next(
        (ln.strip() for ln in create.splitlines() if ln.strip().startswith("executed")),
        None,
    )
    assert line == "executed INTEGER", (
        f"executed 列定义必须是无 DEFAULT 的 `executed INTEGER`（实际 {line!r}）"
        f" —— 带 DEFAULT 会让存量行被回填成假事实"
    )


@pytest.mark.asyncio
async def test_record_step_end_writes_executed_and_none_keeps_old_value(ledger_env):
    """写入语义：True→1、False→**0**、None→不覆盖（未判定不冒充否）。

    ⚠ 这条是 ④ 的**核心守卫**：`False → 0` 是本列存在的全部理由。若把
    `False` 也写成 NULL，那「判定成立但没跑」与「非 spawn 工具」又同形了。
    """
    from hiveweave.db import project as project_db
    from hiveweave.services.run_ledger import RunLedger

    ledger = RunLedger()
    conn = await project_db.ensure_project_db(ledger_env["workspace"])

    async def _mk() -> str:
        sid = await ledger.record_step_start(
            agent_id=EXEC, run_id="run-f5", step_index=0,
            step_type="tool_call", tool_name="bash",
        )
        assert sid, "record_step_start returned no id"
        return sid

    async def _val(sid: str):
        cur = await conn.execute(
            "SELECT executed FROM run_steps WHERE id = ?", [sid]
        )
        row = await cur.fetchone()
        return None if row is None else row[0]

    # ① executed=True → 1（跑了）
    sid1 = await _mk()
    await ledger.record_step_end(
        agent_id=EXEC, step_id=sid1, status="completed", executed=True,
    )
    assert await _val(sid1) == 1

    # ② executed=False → **0**（"确认没启动" —— 与 NULL 是两回事）
    sid2 = await _mk()
    await ledger.record_step_end(
        agent_id=EXEC, step_id=sid2, status="blocked", executed=False,
    )
    assert await _val(sid2) == 0, (
        "False 必须落成 0 —— 若落成 NULL，它与「非 spawn 工具」同形，"
        "「宣告了沙箱却根本没跑」永远捞不出来（F5 的病原样留在数据里）"
    )

    # ③ None → 不覆盖既有 1（未判定不得冒充否）
    await ledger.record_step_end(
        agent_id=EXEC, step_id=sid1, status="completed", executed=None,
    )
    assert await _val(sid1) == 1, "None 覆盖了既有值 —— 未判定被写成了否"

    # ④ 非 spawn 类步骤不写 ⇒ NULL（不适用；与 0 必须可区分）
    sid3 = await _mk()
    await ledger.record_step_end(agent_id=EXEC, step_id=sid3, status="completed")
    assert await _val(sid3) is None, "非 spawn 步骤被写入了默认值"

    # ⑤ **正交性**：同一次调用里 enforcement 与 executed 各答各的
    sid4 = await _mk()
    await ledger.record_step_end(
        agent_id=EXEC, step_id=sid4, status="blocked",
        enforcement=None,      # 判定成立但没跑 ⇒ 不落 confined
        executed=False,        # ← 这个才是"发生了什么"
    )
    cur = await conn.execute(
        "SELECT enforcement, executed FROM run_steps WHERE id = ?", [sid4]
    )
    row = await cur.fetchone()
    assert row[0] is None and row[1] == 0, (
        f"两列必须各答一个问题（enforcement=打算走哪条路 / executed=有没有启动），"
        f"实际 enforcement={row[0]!r} executed={row[1]!r}"
    )

