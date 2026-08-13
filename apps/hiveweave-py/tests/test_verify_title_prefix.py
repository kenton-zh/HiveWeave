"""H1 regression: VERIFY 判定必须覆盖括号包裹/全角冒号标题形态。

slack-clone_03 实测：任务标题「【VERIFY: M4 后端消息与互动 API】验收对象：…」
（VERIFY: 前有「【」）逃过 startswith("VERIFY:") 判定 →
claim.py 的 _verify_serialize_lock 检查整体被跳过、verify_spawn.py 的
_in_flight_verify_task 不把该任务计入锁持有者 → 锁双向穿透，双 VERIFY
in-flight 重叠。修复：所有判定收口到 is_verify_title（锚定 ^[【[]?\\s*VERIFY[:：]）。
"""

from __future__ import annotations

import pytest

from hiveweave.services.task import TaskService
from hiveweave.services.tasks.verify import VerifyMixin, is_verify_title
from hiveweave.tools.tasks.verify_spawn import _in_flight_verify_task

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


def test_is_verify_title_accepts_all_system_forms():
    for t in (
        "VERIFY: x",
        "【VERIFY: x】",
        "[VERIFY: x]",
        "VERIFY：x",
        "【VERIFY：x】",
        "[VERIFY：x]",
        "VERIFY : x",
        "【 VERIFY: x】",
        "VERIFY: M4 后端消息与互动 API",
        "【VERIFY: M4 后端消息与互动 API】验收对象：互动面板",
    ):
        assert is_verify_title(t), t
        assert VerifyMixin._is_verify_task({"title": t}), t


def test_is_verify_title_rejects_ordinary_titles():
    for t in (
        "收到 VERIFY 通知",
        "关于 VERIFY 的讨论",
        "验证·模块C：skill 市场",
        "verify: x",  # 大小写敏感（与原 startswith 行为一致）
        "【VERIFY 通知】",  # 无冒号
        "xxVERIFY: y",  # 未锚定行首
        "【任务】VERIFY: y",
        "",
        None,
    ):
        assert not is_verify_title(t), t
        assert not VerifyMixin._is_verify_task({"title": t}), t


def test_verify_title_key_normalizes_bracket_forms():
    assert (
        VerifyMixin._verify_title_key("【VERIFY: 项9 演练】") == "项9 演练"
    )
    assert (
        VerifyMixin._verify_title_key("VERIFY: 项9 演练") == "项9 演练"
    )
    assert (
        VerifyMixin._verify_title_key(
            "【VERIFY: 项9 演练】验收对象：归零 CEO 配合"
        )
        == "项9 演练 验收对象：归零 CEO 配合"
    )
    assert VerifyMixin._verify_title_key("[VERIFY: 项9 演练]") == "项9 演练"
    assert VerifyMixin._verify_title_key("验证·模块C：skill 市场") == "验证·模块C：skill 市场"


async def _make_verify(pid, ts, title):
    """创建 created 态 VERIFY（带 assignee，可直接 claim）。"""
    return await ts.create_task(
        pid,
        title,
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        tags=["verify", "mandatory"],
        source="system",
    )


@pytest.mark.asyncio
async def test_bracket_verify_holder_blocks_plain_verify_claim(task_env):
    """括号 VERIFY in-flight 时必须挡住第二个（plain）VERIFY 的 claim。

    Pre-fix：_in_flight_verify_task 不认【VERIFY: …】→ 无 blocker → 双 in-flight。
    """
    ts = TaskService()
    pid = task_env["project_id"]
    va = await _make_verify(pid, ts, "【VERIFY: M4 后端消息与互动 API】验收对象：互动面板")
    await ts.claim_task(pid, va, EXEC)  # 无其它 in-flight → 正常 claim

    vb = await _make_verify(pid, ts, "VERIFY: M4")
    with pytest.raises(ValueError, match="VERIFY"):
        await ts.claim_task(pid, vb, EXEC)
    assert (await ts.get_task(pid, vb))["status"] == "created"


@pytest.mark.asyncio
async def test_plain_verify_holder_blocks_bracket_verify_claim(task_env):
    """plain VERIFY in-flight 时必须挡住括号 VERIFY 的 claim。

    Pre-fix：claim 路径对【VERIFY: …】判定 False → _verify_serialize_lock
    检查整体被跳过 → 双 in-flight。
    """
    ts = TaskService()
    pid = task_env["project_id"]
    va = await _make_verify(pid, ts, "VERIFY: M4")
    await ts.claim_task(pid, va, EXEC)

    vb = await _make_verify(pid, ts, "【VERIFY: M4】验收对象：互动面板")
    with pytest.raises(ValueError, match="VERIFY"):
        await ts.claim_task(pid, vb, EXEC)
    assert (await ts.get_task(pid, vb))["status"] == "created"


@pytest.mark.asyncio
async def test_in_flight_verify_detects_bracket_holder(task_env):
    """_in_flight_verify_task 必须把括号标题的 claimed VERIFY 计入锁持有者。"""
    ts = TaskService()
    pid = task_env["project_id"]
    va = await _make_verify(pid, ts, "【VERIFY: M4 后端消息与互动 API】验收对象：互动面板")
    await ts.claim_task(pid, va, EXEC)

    blocker = await _in_flight_verify_task(pid)
    assert blocker is not None
    assert blocker["id"] == va


@pytest.mark.asyncio
async def test_fullwidth_colon_verify_holder_blocks_claim(task_env):
    """全角冒号 VERIFY：x 同样计入判定（双向一致）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    va = await _make_verify(pid, ts, "VERIFY：M4 后端消息与互动 API")
    await ts.claim_task(pid, va, EXEC)

    vb = await _make_verify(pid, ts, "VERIFY: M5")
    with pytest.raises(ValueError, match="VERIFY"):
        await ts.claim_task(pid, vb, EXEC)
    assert (await ts.get_task(pid, vb))["status"] == "created"


@pytest.mark.asyncio
async def test_forge_gate_rejects_bracket_and_lowercase_forms(task_env):
    """伪造门（agent source）必须拒绝括号/小写/全角冒号 VERIFY 标题。

    H1 收口：修复前 `【VERIFY: x】` 可穿透伪造门且带全权 VERIFY 语义。
    """
    ts = TaskService()
    pid = task_env["project_id"]
    for t in (
        "VERIFY: x",
        "【VERIFY: x】",
        "[VERIFY: x]",
        "VERIFY：x",
        "verify: x",
        "【verify: x】",
    ):
        with pytest.raises(ValueError, match="VERIFY"):
            await ts.create_task(
                pid, t, "desc", creator_id=COORD, assignee_id=EXEC, source="agent"
            )
    # 普通标题不受影响
    tid = await ts.create_task(
        pid, "Verify dup", "desc", creator_id=COORD, assignee_id=EXEC,
        source="agent",
    )
    assert tid
