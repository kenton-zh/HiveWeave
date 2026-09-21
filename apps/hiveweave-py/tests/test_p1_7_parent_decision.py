"""P1-7 ①：派单新建任务时的 parent **只认显式**，不再自动认父。

病灶（§10.3 真根因 B）：`_infer_parent_task_id` 会在未显式给 parent 时，把
**派发者当前唯一在跑的「别人派给我」的任务**当成新任务的父 ⇒ 任何派单都被挂到
它下面（父子不是拆解出来的，是被猜出来的）；该函数零测试，也无法区分
「我一边在跑 X、一边被要求单独立一个 Y」。

判据（AC2）：无 `parentTaskId` 新建 ⇒ `parent_task_id` 为 NULL。
"""

from __future__ import annotations

import inspect

import pytest

from hiveweave.services.dispatch import DispatchService


@pytest.mark.parametrize(
    "explicit,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("task-abc", "task-abc"),
        ("  task-abc  ", "task-abc"),
    ],
)
def test_parent_decision_only_honors_explicit(explicit, expected):
    d = DispatchService.__new__(DispatchService)  # 只测决策点，不建依赖
    assert d._parent_for_new_dispatch(explicit) == expected


def test_decision_point_does_not_call_the_infer_helper():
    """反回归（结构判据）：决策点里**不得**再出现自动推断调用。"""
    src = inspect.getsource(DispatchService._parent_for_new_dispatch)
    assert "_infer_parent_task_id(" not in src, (
        "决策点又调回自动认父 ⇒ 派单会被误挂到当前在跑的任务下"
    )
    # 旧函数仍在（供调用方显式复用），只是不再默认调用
    assert hasattr(DispatchService, "_infer_parent_task_id")


def test_no_production_call_sites_left():
    """反回归：`_infer_parent_task_id` 只应有**定义**、不应再有调用点。

    （本条是 `test_p1_7_parent_decision` 的核心：改的是「谁在调它」。）
    """
    import hiveweave.services.dispatch as mod

    src = inspect.getsource(mod)
    calls = src.count("_infer_parent_task_id(")
    assert calls == 1, (
        f"仍有 {calls - 1} 处生产调用点 —— 自动认父没被真正摘掉"
    )
