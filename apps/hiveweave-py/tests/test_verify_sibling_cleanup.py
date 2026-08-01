"""TEST19 regression: sibling VERIFY cleanup must not kill real work.

TEST19 实测：归零 approve 模块A/B 时，_close_sibling_verify_tasks 把同
parent 下 tags 含 verify 的普通验证实施任务（模块C claimed / 模块D running）
当重复清扫 → 验证工作被系统重建 2 轮。修复后只清「真重复」：
- 必须 title 以 VERIFY: 开头（系统 spawn 的 VERIFY 子任务）
- 标题归一化后与收口 VERIFY 相同
- 仅 created/claimed 等未开始状态才自动归档
"""

from __future__ import annotations

from hiveweave.services.task import TaskService
from hiveweave.services.tasks.verify import VerifyMixin

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


def test_verify_title_key_normalizes():
    assert VerifyMixin._verify_title_key(
        "VERIFY: 项9 演练（归零 CEO 配合 waiver）"
    ) == "项9 演练"
    assert VerifyMixin._verify_title_key(
        "VERIFY: 项9 演练 (rebuild)"
    ) == "项9 演练"
    assert VerifyMixin._verify_title_key("VERIFY:  项9 演练  ") == "项9 演练"
    assert VerifyMixin._verify_title_key("验证·模块C：skill 市场") == "验证·模块C：skill 市场"


async def _setup(parent_title: str, verify_ok_title: str, pid: str, ts) -> str:
    """parent + 一个已收口的 VERIFY（except_id），返回 parent_id。"""
    parent_id = await ts.create_task(
        pid, parent_title, "d", creator_id=COORD, assignee_id=EXEC
    )
    verify_ok = await ts.create_task(
        pid,
        verify_ok_title,
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        parent_task_id=parent_id,
        tags=["verify"],
    )
    await ts.close_task(pid, verify_ok)
    return parent_id


async def test_cleanup_spares_plain_verify_tagged_impl_tasks(task_env):
    """tags=verify 的普通实施任务（无 VERIFY: 前缀）绝不能被清扫。

    TEST19：模块C「验证·模块C：…」claimed / 模块D「验证·模块D：…」running
    在 approve 模块A/B 时被误杀 → 此回归断言两者完好。
    """
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await _setup("Root", "VERIFY: Root", pid, ts)

    impl_c = await ts.create_task(
        pid, "验证·模块C：skill 市场", "d",
        creator_id=COORD, assignee_id=EXEC, parent_task_id=parent_id,
        tags=["verify"],
    )
    impl_d = await ts.create_task(
        pid, "验证·模块D：提示词落地", "d",
        creator_id=COORD, assignee_id=EXEC, parent_task_id=parent_id,
        tags=["verify"],
    )
    await ts.claim_task(pid, impl_c, EXEC)
    await ts.claim_task(pid, impl_d, EXEC)
    await ts.start_task(pid, impl_d)

    verify_ok = await ts.create_task(
        pid, "VERIFY: Root", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    await ts.close_task(pid, verify_ok)
    await ts._close_sibling_verify_tasks(pid, parent_id, except_id=verify_ok)

    for tid, expected in ((impl_c, "claimed"), (impl_d, "running")):
        t = await ts.get_task(pid, tid)
        assert t["status"] == expected, f"impl task {tid[:8]} was cleaned up!"
        assert not t.get("is_archived")


async def test_cleanup_spares_different_verify_target(task_env):
    """VERIFY: 前缀但归一化标题不同的兄弟不动（不是同一验证目标）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await _setup("Root", "VERIFY: Root", pid, ts)

    other = await ts.create_task(
        pid, "VERIFY: Other Module", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    await ts.claim_task(pid, other, EXEC)
    verify_ok = await ts.create_task(
        pid, "VERIFY: Root", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    await ts.close_task(pid, verify_ok)
    await ts._close_sibling_verify_tasks(pid, parent_id, except_id=verify_ok)
    t = await ts.get_task(pid, other)
    assert t["status"] == "claimed"
    assert not t.get("is_archived")


async def test_cleanup_spares_in_flight_duplicate(task_env):
    """同目标的重复 VERIFY 若 running/submitted 等执行中 → 跳过，留协调者裁决。"""
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await _setup("Root", "VERIFY: Root", pid, ts)

    dup_running = await ts.create_task(
        pid, "VERIFY: Root", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    await ts.claim_task(pid, dup_running, EXEC)
    await ts.start_task(pid, dup_running)
    verify_ok = await ts.create_task(
        pid, "VERIFY: Root", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    await ts.close_task(pid, verify_ok)
    await ts._close_sibling_verify_tasks(pid, parent_id, except_id=verify_ok)
    t = await ts.get_task(pid, dup_running)
    assert t["status"] == "running"
    assert not t.get("is_archived")


async def test_cleanup_archives_inactive_true_duplicate(task_env):
    """同目标、VERIFY: 前缀、created/claimed 未开始的真重复 → 归档。"""
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await _setup("Root", "VERIFY: Root", pid, ts)

    dup = await ts.create_task(
        pid, "VERIFY: Root", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    verify_ok = await ts.create_task(
        pid, "VERIFY: Root", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    await ts.close_task(pid, verify_ok)
    await ts._close_sibling_verify_tasks(pid, parent_id, except_id=verify_ok)
    t = await ts.get_task(pid, dup)
    assert t.get("is_archived")
    assert t["status"] == "cancelled"


async def test_cleanup_archives_numbered_duplicate(task_env):
    """真实重复常带编号后缀：VERIFY: X（1）/ X（2）归一相同 → 未开始的归档。"""
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await _setup("Root", "VERIFY: Root", pid, ts)

    dup = await ts.create_task(
        pid, "VERIFY: Root（1）", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    verify_ok = await ts.create_task(
        pid, "VERIFY: Root（2）", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    await ts.close_task(pid, verify_ok)
    await ts._close_sibling_verify_tasks(pid, parent_id, except_id=verify_ok)
    t = await ts.get_task(pid, dup)
    assert t.get("is_archived")


async def test_cleanup_spares_rework_duplicate(task_env):
    """rework 是活跃返工态（→running），同目标重复也跳过，不归档。"""
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await _setup("Root", "VERIFY: Root", pid, ts)

    dup = await ts.create_task(
        pid, "VERIFY: Root", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    await ts.claim_task(pid, dup, EXEC)
    await ts.start_task(pid, dup)
    await ts.submit_task(pid, dup, evidence={"tests_passed": True, "test_output": "ok"})
    await ts.start_review(pid, dup)
    await ts.review_task(pid, dup, "rework")
    verify_ok = await ts.create_task(
        pid, "VERIFY: Root", "verify", creator_id=COORD,
        assignee_id=EXEC, parent_task_id=parent_id, tags=["verify"],
    )
    await ts.close_task(pid, verify_ok)
    await ts._close_sibling_verify_tasks(pid, parent_id, except_id=verify_ok)
    # review rework 是瞬态（reviewing → rework → running）
    t = await ts.get_task(pid, dup)
    assert t["status"] == "running"
    assert not t.get("is_archived")
