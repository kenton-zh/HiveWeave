"""批 2（I3+I4）：两处 dispatch 兜底统一盖戳的守卫测试。

I3（P0-2 同一异常两本账）+ I4（L9-1 兜底出口不盖戳）—— 一次改动，一组守卫。

验收映射（fixplan 批 2）：
① 同一异常经 legacy executor（``ToolExecutor.execute`` 的 ``_dispatch`` 兜底）
  与 pipeline（``execute_registered_tool`` 的 ``execute_fn`` 兜底）两路，
  ``fact`` / ``runner_failed`` / ``executed`` / ``blocked`` 取值相同；
② 「兜底分支返回必带 blocked 或 fact」的守卫（本文件 TestFallbackGuard）；
③ platform-side 沙箱拒绝归 ``runner_failed``，**不再**落
  ``fact_position`` 未分类样本桶（未分类桶只该装"真没判据"的新形态）。

上游对照：
- DSH ``docs/defensive-patterns.md:7-9``（HEAD 477b4f420）—— Report
  orthogonal outcomes independently; never nest one flag's report inside
  another's branch, or a caller reads a cut-short run as a clean success.
- DSH ``docs/testing.md:40`` —— guard 只有能被 regression 打红才算守卫
  （阳性对照在施工轮执行并留档：把 executor 兜底改回裸 ``self._error``
  ⇒ 本文件 ``test_both_fallbacks_reference_shared_helper`` 与
  ``test_same_exception_two_executors_same_facts`` 同步转红）。
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError


# ── 基座 ──────────────────────────────────────────────────────────────


class _Allow:
    async def evaluate_detailed(self, agent_id, tool_name, args):
        return ("allow", None)


def _platform_side_exc() -> SandboxUnavailableError:
    """真实事故形态：构造点亲笔声明 platform_side + spawn 打的 executed=False。"""
    e = SandboxUnavailableError(
        "seal read-back failed: .git/config", platform_side=True
    )
    e.executed = False  # spawn_agent_command 的 F5 戳（动态属性）
    return e


def _wrapped_bug_exc() -> SandboxUnavailableError:
    """真代码 bug 被容器包裹（异常链里无任何平台侧亲笔签名）。"""
    exc = SandboxUnavailableError("spawn_confined failed")
    # 等价 `raise ... from ...`（__cause__ 赋值隐式置 __suppress_context__）
    exc.__cause__ = TypeError("'NoneType' object has no attribute 'spawn'")
    return exc


_EXC_FACTORIES = {
    "platform_side": _platform_side_exc,
    "wrapped_bug": _wrapped_bug_exc,
    "value_error": lambda: ValueError("bad input"),
}


def _fact_axes(r: dict) -> tuple:
    """验收①的四条轴：run_steps 三列 + 权威 fact。"""
    return (
        r.get("fact"),
        r.get("runner_failed"),
        r.get("executed"),
        r.get("blocked"),
    )


# ── 验收①：同一异常两执行器同账 ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_name", sorted(_EXC_FACTORIES))
async def test_same_exception_two_executors_same_facts(exc_name, tmp_path):
    exc = _EXC_FACTORIES[exc_name]()

    # legacy 路径：未注册工具 run_tests → executor._dispatch 兜底
    from hiveweave.tools.executor import ToolExecutor

    with patch.object(
        ToolExecutor, "_dispatch", new=AsyncMock(side_effect=exc)
    ):
        executor = ToolExecutor(_Allow(), object())
        legacy = await executor.execute(
            "a1", "run_tests", {"filePaths": ["a.py"]}, str(tmp_path)
        )

    # pipeline 路径：注册表内 bash 工具 execute_fn 抛同一异常
    from hiveweave.tools.base import _TOOL_REGISTRY
    from hiveweave.tools.pipeline import execute_registered_tool

    bash_def = _TOOL_REGISTRY["bash"]
    with patch.object(bash_def, "execute_fn", new=AsyncMock(side_effect=exc)):
        piped = await execute_registered_tool(
            "bash", {"command": "ls"}, "a1", str(tmp_path), _Allow(), None
        )

    assert legacy.get("success") is False
    assert piped is not None and piped.get("success") is False
    # 核心断言：同一异常，两本账必须一致（I3 卡片的 NULL/1 分裂不得复发）
    assert _fact_axes(legacy) == _fact_axes(piped)


@pytest.mark.asyncio
async def test_platform_side_sandbox_rejection_is_runner_failed(tmp_path):
    """platform-side 沙箱异常 ⇒ blocked + runner_failed + executed=False。"""
    from hiveweave.tools.executor import ToolExecutor

    with patch.object(
        ToolExecutor, "_dispatch", new=AsyncMock(side_effect=_platform_side_exc())
    ):
        executor = ToolExecutor(_Allow(), object())
        result = await executor.execute(
            "a1", "run_tests", {"filePaths": ["a.py"]}, str(tmp_path)
        )

    assert result["blocked"] is True
    assert result["fact"] == "runner_failed"
    assert result["runner_failed"] is True
    assert result["executed"] is False


# ── 验收③：平台侧拒绝不再进未分类桶 ─────────────────────────────────


@pytest.mark.asyncio
async def test_platform_side_rejection_not_in_unclassified_bucket(tmp_path):
    from hiveweave.tools.executor import ToolExecutor

    with patch.object(
        ToolExecutor, "_dispatch", new=AsyncMock(side_effect=_platform_side_exc())
    ):
        executor = ToolExecutor(_Allow(), object())
        result = await executor.execute(
            "a1", "run_tests", {"filePaths": ["a.py"]}, str(tmp_path)
        )

    # 平台侧拒绝不得进「真未分类」桶：无样本，或有也只是声明支路的
    # 确定性降采样（带 kind，F2 设计内）——真未分类样本不带 kind 键。
    _sample = result.get("unclassified_sample")
    assert not _sample or _sample.get("kind") == "declared_sampled"


@pytest.mark.asyncio
async def test_wrapped_bug_not_claimed_platform(tmp_path):
    """容器包裹的真 bug ⇒ outcome_unknown（「没有结果」），不替 agent 卸责。"""
    from hiveweave.tools.executor import ToolExecutor

    with patch.object(
        ToolExecutor, "_dispatch", new=AsyncMock(side_effect=_wrapped_bug_exc())
    ):
        executor = ToolExecutor(_Allow(), object())
        result = await executor.execute(
            "a1", "run_tests", {"filePaths": ["a.py"]}, str(tmp_path)
        )

    assert result["blocked"] is True
    assert result["fact"] == "outcome_unknown"
    # 显式判定"非 runner 失败"（False = 已判定），与 NULL（未判定）不同形
    assert result["runner_failed"] is False


# ── 验收②：兜底必带 blocked 或 fact ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_name", sorted(_EXC_FACTORIES))
async def test_fallback_result_carries_blocked_or_fact(exc_name, tmp_path):
    from hiveweave.tools.executor import ToolExecutor
    from hiveweave.tools.base import _TOOL_REGISTRY
    from hiveweave.tools.pipeline import execute_registered_tool

    exc = _EXC_FACTORIES[exc_name]()

    with patch.object(
        ToolExecutor, "_dispatch", new=AsyncMock(side_effect=exc)
    ):
        executor = ToolExecutor(_Allow(), object())
        legacy = await executor.execute(
            "a1", "run_tests", {"filePaths": ["a.py"]}, str(tmp_path)
        )
    with patch.object(
        _TOOL_REGISTRY["bash"], "execute_fn", new=AsyncMock(side_effect=exc)
    ):
        piped = await execute_registered_tool(
            "bash", {"command": "ls"}, "a1", str(tmp_path), _Allow(), None
        )

    for label, r in (("legacy", legacy), ("pipeline", piped)):
        assert r.get("blocked") or r.get("fact"), (
            f"{label} 兜底返回无事实位（L9-1 复发）：keys={sorted(r)}"
        )


# ── python_script 同族出口对齐（I3 卡片的另一本账）──────────────────


@pytest.mark.asyncio
async def test_python_script_sandbox_exit_aligned(tmp_path):
    """python_script 沙箱出口 = bash 同形状（blocked/fact/executed 三轴一致）。"""
    from hiveweave.tools.python_script import (
        PythonScriptParams,
        python_script_execute,
    )

    exc = _platform_side_exc()
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value="p1"),
        ),
        patch(
            "hiveweave.services.acl_sandbox.entry.spawn_agent_command",
            new=AsyncMock(side_effect=exc),
        ),
    ):
        result = await python_script_execute(
            PythonScriptParams(script="print(1)"), "a1", str(tmp_path)
        )
        # 工具函数契约是 ToolResult；run_steps 消费的是漏斗展开后的 dict
        if not isinstance(result, dict):
            result = result.to_dict()

    # 与 executor/pipeline 兜底（同异常）三轴一致
    assert result["blocked"] is True
    assert result["fact"] == "runner_failed"
    assert result["runner_failed"] is True
    assert result["executed"] is False

    # 与 bash.py 同族出口（SandboxUnavailableError.to_tool_dict）形状对齐
    bash_shaped = exc.to_tool_dict(platform_side=True, executed=False)
    for key in ("blocked", "fact", "runner_failed", "executed"):
        assert result.get(key) == bash_shaped.get(key), key


@pytest.mark.asyncio
async def test_python_script_wrapped_bug_not_claimed_platform(tmp_path):
    from hiveweave.tools.python_script import (
        PythonScriptParams,
        python_script_execute,
    )

    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value="p1"),
        ),
        patch(
            "hiveweave.services.acl_sandbox.entry.spawn_agent_command",
            new=AsyncMock(side_effect=_wrapped_bug_exc()),
        ),
    ):
        result = await python_script_execute(
            PythonScriptParams(script="print(1)"), "a1", str(tmp_path)
        )
        if not isinstance(result, dict):
            result = result.to_dict()

    assert result["blocked"] is True
    assert result["fact"] == "outcome_unknown"
    assert result["runner_failed"] is False


# ── 结构守卫：两处兜底必须共用唯一实现 ───────────────────────────────


def test_both_fallbacks_reference_shared_helper():
    """防复发：两处兜底必须引用 dispatch_failure_result，裸返回模式不得回潮。

    阳性对照（施工轮已执行并转红）：把 executor 兜底改回
    ``return self._error(f"Error: {type(exc).__name__}: {exc}")`` ⇒ 本测试
    与 test_same_exception_two_executors_same_facts[platform_side] 同时转红。
    """
    import hiveweave.tools.executor as executor_mod
    import hiveweave.tools.pipeline as pipeline_mod

    executor_src = inspect.getsource(executor_mod)
    pipeline_src = inspect.getsource(pipeline_mod)

    assert "dispatch_failure_result" in executor_src, (
        "executor 兜底必须走唯一实现 dispatch_failure_result（I3+I4）"
    )
    assert "dispatch_failure_result" in pipeline_src, (
        "pipeline 兜底必须走唯一实现 dispatch_failure_result（I3+I4）"
    )
    # 旧撒谎形态不得回潮
    assert 'return self._error(f"Error: {type(exc).__name__}' not in executor_src
    assert 'ToolResult.err(f"Error: {type(exc).__name__}' not in pipeline_src


def test_helper_routes_through_funnel():
    """共享助手必须经过唯一漏斗收口（E23 分母 + 派生键展开）。"""
    from hiveweave.tools.fact_positions import dispatch_failure_result

    src = inspect.getsource(dispatch_failure_result)
    assert "finalize_tool_result(" in src


def test_dispatch_failure_result_unit():
    """纯函数级三态：platform-side / 包裹 bug / 通用异常。"""
    from hiveweave.tools.fact_positions import dispatch_failure_result

    r1 = dispatch_failure_result("t", _platform_side_exc())
    assert (r1["fact"], r1["runner_failed"], r1["executed"], r1["blocked"]) == (
        "runner_failed", True, False, True,
    )
    # 平台侧拒绝不得产「真未分类」样本（位缺失+文本未命中）。声明支路的
    # 确定性降采样（kind="declared_sampled"，1/20 哈希命中）是 F2 设计内的
    # 通道活性观测 —— 同 event_type 但带 kind 字段，以此区分（审计 P2-1）。
    _s1 = r1.get("unclassified_sample")
    assert not _s1 or _s1.get("kind") == "declared_sampled"

    r2 = dispatch_failure_result("t", _wrapped_bug_exc())
    assert (r2["fact"], r2["blocked"]) == ("outcome_unknown", True)
    assert r2["runner_failed"] is False

    r3 = dispatch_failure_result("t", ValueError("bad input"))
    # 不猜：交漏斗归因阶梯，fail-soft 兜底 outcome_unknown + 样本
    assert r3["fact"] == "outcome_unknown"
    assert r3["runner_failed"] is False
    assert r3.get("unclassified_sample"), "通用异常应产未分类样本（观测通道）"
