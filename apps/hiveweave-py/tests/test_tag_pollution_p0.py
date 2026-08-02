"""TEST19 regression: tags=verify pollution must not drive VERIFY paths.

TEST19 实测：磐石给普通模块验证任务打 tags=["verify", ...]，平台
_is_verify_task 双通道判定把普通任务当 VERIFY 系统任务 → 自动归档
(P0-1) / 隔离门拒绝 (P0-2) / 强制 main 跑测试 (P1-3) 全部误伤。

修复后两条规则：
1. _is_verify_task（及所有独立判定点）只认系统 VERIFY: 前缀。
2. create_task 入口剥离 agent/user 提交的保留 tag
   （verify/mandatory/post-merge），source="system" 豁免。
"""

from __future__ import annotations

import json

import pytest

from hiveweave.services.git_worktree.reconcile import _row_is_verify_task
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.verify import VerifyMixin

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


def _stored_tags(t: dict) -> list:
    raw = t.get("tags")
    if raw is None:
        return []
    return json.loads(raw) if isinstance(raw, str) else raw


# ── 规则 1：判定只认前缀 ──────────────────────────────────────────


async def test_is_verify_task_prefix_only(task_env):
    """tags=verify 无前缀 → 普通任务；VERIFY: 前缀 → 系统 VERIFY。"""
    ts = TaskService()
    pid = task_env["project_id"]
    plain = await ts.create_task(
        pid, "验证·模块A：skill 市场", "d",
        creator_id=COORD, assignee_id=EXEC, tags=["verify", "mandatory"],
    )
    t = await ts.get_task(pid, plain)
    assert VerifyMixin._is_verify_task(t) is False

    sys_verify = await ts.create_task(
        pid, "VERIFY: 模块A", "v",
        creator_id=COORD, assignee_id=EXEC, tags=["verify"],
        source="system",
    )
    t2 = await ts.get_task(pid, sys_verify)
    assert VerifyMixin._is_verify_task(t2) is True

    legacy = {"title": "旧任务", "tags": ["verify"]}
    assert VerifyMixin._is_verify_task(legacy) is False


async def test_is_verify_task_prefix_only_preserves_system(task_env):
    """系统 spawn 形态（前缀 + tags 双命中）仍为 True。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "VERIFY: Root", "verify", creator_id=COORD,
        assignee_id=EXEC, tags=["verify", "mandatory", "post-merge"],
        source="system",
    )
    t = await ts.get_task(pid, tid)
    assert VerifyMixin._is_verify_task(t) is True


async def test_row_is_verify_task_prefix_only(task_env):
    """git_worktree reconcile 判定（DB row 直读）同样只认前缀。"""
    ts = TaskService()
    pid = task_env["project_id"]
    plain = await ts.create_task(
        pid, "验证·模块B：提示词", "d",
        creator_id=COORD, assignee_id=EXEC, tags=["verify"],
    )
    sys_v = await ts.create_task(
        pid, "VERIFY: 模块B", "v",
        creator_id=COORD, assignee_id=EXEC, tags=["verify"],
        source="system",
    )
    for tid, expected in ((plain, False), (sys_v, True)):
        t = await ts.get_task(pid, tid)
        assert _row_is_verify_task(t["title"], t["tags"]) is expected


# ── 规则 2：create_task 剥离保留 tag ─────────────────────────────


async def test_create_task_strips_reserved_tags(task_env):
    """agent 提交 verify/mandatory/post-merge → 落库前剥离。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "验证·模块C：skill 市场", "d",
        creator_id=COORD, assignee_id=EXEC,
        tags=["verify", "mandatory", "post-merge", "ui"],
    )
    t = await ts.get_task(pid, tid)
    stored = _stored_tags(t)
    assert "verify" not in stored
    assert "mandatory" not in stored
    assert "post-merge" not in stored
    assert "ui" in stored  # 非保留 tag 原样保留
    # 剥离后普通任务立即认领（不被当 VERIFY 卡在 created）
    assert t["status"] == "claimed"
    assert VerifyMixin._is_verify_task(t) is False


async def test_create_task_strips_all_reserved_tags_to_null(task_env):
    """仅保留 tag → 全部剥离 → tags 落库为 NULL（与 crud 惯例一致）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "验证·模块E：数据", "d",
        creator_id=COORD, assignee_id=EXEC,
        tags=["verify", "post-merge"],
    )
    t = await ts.get_task(pid, tid)
    assert t["tags"] is None
    assert t["status"] == "claimed"


async def test_create_task_keeps_reserved_tags_for_system(task_env):
    """source="system"（verify_spawn 路径）保留保留 tag。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "VERIFY: 模块D", "v",
        creator_id=COORD, assignee_id=EXEC,
        tags=["verify", "mandatory", "post-merge"],
        source="system",
    )
    t = await ts.get_task(pid, tid)
    stored = _stored_tags(t)
    assert "verify" in stored
    assert "mandatory" in stored
    assert "post-merge" in stored
    assert VerifyMixin._is_verify_task(t) is True


async def test_create_task_keeps_plain_tags(task_env):
    """非保留 tag（docs_only/ui/自定义）不受影响。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "写文档", "d", creator_id=COORD, assignee_id=EXEC,
        tags=["docs_only", "explore"],
    )
    t = await ts.get_task(pid, tid)
    stored = _stored_tags(t)
    assert stored == ["docs_only", "explore"]


async def test_create_task_rejects_forged_verify_title(task_env):
    """agent 不得伪造 VERIFY: 标题（系统 spawn 专属通道）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    with pytest.raises(ValueError, match="VERIFY:"):
        await ts.create_task(
            pid, "VERIFY: forged", "d",
            creator_id=COORD, assignee_id=EXEC,
        )


async def test_update_task_strips_reserved_tags_and_rejects_verify_title(task_env):
    """PATCH 同样剥保留 tag；不可把 title 改成 VERIFY:。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "普通任务", "d", creator_id=COORD, assignee_id=EXEC,
        tags=["ui"],
    )
    await ts.update_task(pid, tid, tags=["verify", "ui", "mandatory"])
    t = await ts.get_task(pid, tid)
    stored = _stored_tags(t)
    assert "verify" not in stored
    assert "mandatory" not in stored
    assert "ui" in stored

    with pytest.raises(ValueError, match="VERIFY:"):
        await ts.update_task(pid, tid, title="VERIFY: sneaky")
