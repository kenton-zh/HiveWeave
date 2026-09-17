"""F3 / M2（2026-09-17 审计必修）：`python_script` 的执行面戳**透传**行为测试。

## 为什么单开一个文件

F3 的三处修法里，`python_script` 的戳透传**零测试覆盖** —— 审计用探针实测：
把 `python_script.py` 重建 dict 那一步的 `**_enforcement_stamp(result)` 整行删掉，
既有 65 条测试**全绿**。也就是说那处修改当时既没被守住、也没被证明有效。

结构性守卫（`test_sandbox_single_entry.py::test_spawn_callers_transport_the_
enforcement_stamp`）只判「有没有搬运动作」，**不判搬没搬到**。本文件补的是
**行为**：打进一个带戳的受限结果，断言戳真的出现在 `ToolResult.to_dict()` 上
—— 那是 `streaming.py:336` 读 `run_steps.enforcement` 的入口。

## 覆盖的三条出口

| 出口 | 位置 | 本文件用例 |
|---|---|---|
| `long_running` 拒绝 | `python_script.py` 重建 dict **之前** return | `test_long_running_exit_carries_stamp` |
| `error` 非空 | 重建后、第一分支 | `test_error_exit_carries_stamp` |
| `timed_out` | 重建后、第二分支 | `test_timeout_exit_carries_stamp` |
| `exit_code == 0` 成功 | 重建后、末段 | `test_success_exit_carries_stamp` |

⚠ **加固面键（`git_hardened`）单独断言**：M3 修掉的正是「本地
`startswith("enforcement")` 前缀过滤把 `git_hardened` 静默排除」。若只断言
`enforcement`，那条缺陷**照样能通过** —— 这两个键分属两类（决策层产出 /
env 构造点产出，见 `policy.SPAWN_STAMP_KEYS` 的注释），必须分别钉。

## 回滚探针（本文件全部用例）

- 删掉重建 dict 里的 `**_enforcement_stamp(result)` ⇒ 成功/失败/超时三条转红；
- 删掉 `_stamp = _enforcement_stamp(result)` ⇒ 全部转红；
- 把 `_stamp` 换成 `startswith("enforcement")` 前缀过滤 ⇒
  **仅** `git_hardened` 断言转红（这正是 M3 的形态）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.acl_sandbox.policy import R_CONFINED, make_decision
from hiveweave.tools import python_script as ps

_DECISION_SEAM = "hiveweave.services.acl_sandbox.policy.resolve_spawn_decision"


def _confined_result(**overrides) -> dict:
    """受限分支的结果（带入口盖的全套戳 + 加固面键）。

    键名不硬编码猜测：`enforcement*` 与 `git_hardened` 都来自
    `policy.SPAWN_STAMP_KEYS` 的语义（决策层 4 键 + env 构造点 1 键）。
    """
    base = {
        "output": "",
        "stdout": "hi\n",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "error": None,
        "enforcement": "confined",
        "enforcement_level": "partial",
        "enforcement_reason": R_CONFINED,
        "enforcement_boundary": r"C:\ws\agent-1",
        "git_hardened": True,
    }
    base.update(overrides)
    return base


def _run(result: dict, tmp_path):
    """在受限判定下跑 `python_script_execute`，返回 ToolResult。"""
    with (
        patch(
            _DECISION_SEAM,
            new=AsyncMock(return_value=make_decision(R_CONFINED)),
        ),
        patch(
            "hiveweave.services.acl_sandbox.service.spawn_confined",
            new=AsyncMock(return_value=result),
        ),
        patch(
            "hiveweave.services.acl_sandbox.integration.build_confined_argv",
            new=MagicMock(return_value=["pwsh", "-c", "python x.py"]),
        ),
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value="p1"),
        ),
    ):
        return None  # 由调用方 await（保持 async 上下文在用例内）


async def _run_async(result: dict, tmp_path):
    with (
        patch(
            _DECISION_SEAM,
            new=AsyncMock(return_value=make_decision(R_CONFINED)),
        ),
        patch(
            "hiveweave.services.acl_sandbox.service.spawn_confined",
            new=AsyncMock(return_value=result),
        ),
        patch(
            "hiveweave.services.acl_sandbox.integration.build_confined_argv",
            new=MagicMock(return_value=["pwsh", "-c", "python x.py"]),
        ),
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value="p1"),
        ),
    ):
        return await ps.python_script_execute(
            ps.PythonScriptParams(script="print('hi')"),
            "agent-1",
            str(tmp_path),
        )


def _assert_stamp_carried(d: dict, context: str):
    """三条断言共用：执行面戳 + 加固面戳都要在（M3 的形态）。"""
    assert d.get("enforcement") == "confined", (
        f"★ {context}：执行面戳丢了 —— `streaming.py:336` 取到 None ⇒ "
        f"`run_steps.enforcement` 恒 NULL（实证 4863 行零落库）；"
        f"实测键：{sorted(k for k in d if 'enforc' in k)}"
    )
    assert d.get("enforcement_level") == "partial", (context, d)
    assert d.get("enforcement_reason") == R_CONFINED, (context, d)
    assert d.get("enforcement_boundary") == r"C:\ws\agent-1", (
        f"★ {context}：受限侧必须有边界标记（「被关在哪里」）"
    )
    assert d.get("git_hardened") is True, (
        f"★ M3：加固面键 `git_hardened` 丢了 —— 它不含 'enforcement' 前缀，"
        f"所以**本地前缀过滤会静默把它排除**。这正是 M3 修掉的形态："
        f"键名必须由 `policy.SPAWN_STAMP_KEYS` 单点登记；实测键：{sorted(d)}"
    )


@pytest.mark.asyncio
async def test_success_exit_carries_stamp(tmp_path):
    """★ M2：成功出口（`exit_code == 0`）必须带全套戳。

    回滚探针：删掉重建 dict 里的 `**_enforcement_stamp(result)` 即转红。
    """
    r = await _run_async(_confined_result(), tmp_path)
    assert r.success is True, r.error
    _assert_stamp_carried(r.to_dict(), "成功出口")


@pytest.mark.asyncio
async def test_error_exit_carries_stamp(tmp_path):
    """★ M2：`error` 非空出口要带戳（失败时的观测**更**需要被看见）。

    回滚探针：同上。
    """
    r = await _run_async(
        _confined_result(error="boom", exit_code=1, stdout=""), tmp_path
    )
    assert r.success is False and "boom" in (r.error or "")
    _assert_stamp_carried(r.to_dict(), "error 出口")


@pytest.mark.asyncio
async def test_timeout_exit_carries_stamp(tmp_path):
    """★ M2：超时出口要带戳（且与 `timeout_kind` 共存不互相覆盖）。

    这条同时守住一个易踩的坑：`timeout_kind` / `timeout_ms` 是**显式**
    关键字，`**_stamp` 是展开 —— 若两者键名撞车会 TypeError。戳的 5 个键
    与超时键不相交，这里用实际调用把这条不变式钉住。
    """
    r = await _run_async(
        _confined_result(timed_out=True, exit_code=None, stdout=""), tmp_path
    )
    assert r.success is False
    d = r.to_dict()
    assert d.get("timeout_kind") == "command", d
    _assert_stamp_carried(d, "超时出口")


@pytest.mark.asyncio
async def test_nonzero_exit_carries_stamp(tmp_path):
    """★ M2：非零退出（末段 err 出口）要带戳。"""
    r = await _run_async(
        _confined_result(exit_code=2, stdout="", stderr="bad thing"), tmp_path
    )
    assert r.success is False
    assert "exited with code 2" in (r.error or "")
    _assert_stamp_carried(r.to_dict(), "非零退出出口")


@pytest.mark.asyncio
async def test_long_running_exit_carries_stamp(tmp_path):
    """★ M2：`long_running` 拒绝出口要带戳（它在**重建 dict 之前** return）。

    这是本文件里唯一一条「不经重建 dict」的出口 —— 正因如此，它需要**单独**
    的提取动作（`_enforcement_stamp(result)` 直接展开），也最容易被漏掉。
    """
    r = await _run_async(_confined_result(long_running=True), tmp_path)
    assert r.success is False
    assert "background unsupported" in (r.error or "")
    d = r.to_dict()
    assert d.get("enforcement") == "confined", (
        "★ 该出口在重建 dict 之前 return —— 它自带一份提取，最容易漏"
    )
    assert d.get("git_hardened") is True, (
        "★ M3：这条出口同样不能用本地前缀过滤（git_hardened 会被排除）"
    )


@pytest.mark.asyncio
async def test_absent_stamp_keys_are_not_filled_with_defaults(tmp_path):
    """缺键**不补默认值** —— NULL ≠ 说了否。

    原生分支（或受限实现未盖戳）不该被写成 `enforcement="native"`：那把
    「没这条信息」说成了「确定了是原生」，正是本仓「NULL 说谎」的形态。
    """
    bare = {
        "output": "", "stdout": "hi\n", "stderr": "",
        "exit_code": 0, "timed_out": False, "error": None,
    }
    r = await _run_async(bare, tmp_path)
    assert r.success is True, r.error
    d = r.to_dict()
    assert "enforcement" not in d, (
        f"缺键不补默认值：上游没盖戳时不许臆断成 native；实测 {sorted(d)}"
    )
    assert "git_hardened" not in d, d
