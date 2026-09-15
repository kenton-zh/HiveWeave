"""fixplan #8 新契约：CEO 交付状态是**计算出来的状态位**，不是措辞判断。

旧形态（本文件原名 E4 补）：``message_user`` 出口用 8 个中英词子串猜
「这是不是完工断言」，命中且账本不干净就拦 —— #8（``daa3995``）已把它
**整个下线**：文本判据换措辞/换语言即绕过，且把触发条件写在工具描述上
等于随附绕过配方。

新契约（本文件锁定的行为）：

- ``message_user`` **零文本判断、永不因措辞拦截**；真实交付状态挂
  ``chat_messages.metadata``（徽章）由用户侧渲染 ⇒ 谎报一眼可辨；
- 「交付完成」的唯一写者是 ``mark_delivery_complete``：**无参数**（agent
  无法用措辞/理由影响判定），内部跑三条**状态查询**（未解决 FAIL 终验 /
  approved 未 closed / 本人未读人工消息）；不通过 ⇒ 写
  ``delivery_state='blocked'`` + 政策码快照并拒绝；
- 账本干净时才写 ``delivery_state='complete'``。
"""

from __future__ import annotations

import json
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.tools.misc_tools import (
    DELIVERY_STATE_BLOCKED,
    DELIVERY_STATE_COMPLETE,
    DELIVERY_STATE_UNMARKED,
    MarkDeliveryCompleteParams,
    MessageUserParams,
    POLICY_INBOX_UNREAD_HUMAN,
    POLICY_LEDGER_APPROVED_OPEN,
    POLICY_LEDGER_FAIL_VERDICT,
    mark_delivery_complete_tool,
    message_user_tool,
)

CEO_ID = "ceo-exit-uuid"
PROJECT_ID = "test-ceo-exit-assertion"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.clear()

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _ceo_row():
    return {"id": CEO_ID, "role": "ceo", "permission_type": "coordinator"}


async def _insert_task(ws, task_id, *, status, evidence=None):
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, creator_id, assignee_id,"
        " status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [task_id, PROJECT_ID, "t", CEO_ID, CEO_ID, status, now, now],
    )
    if evidence is not None:
        await conn.execute(
            "UPDATE tasks SET evidence = ? WHERE id = ?",
            [json.dumps(evidence), task_id],
        )
    await conn.commit()


def _identity_patch(role: str = "ceo"):
    """交付三件套共用的最小 mock 集：身份解析 + 角色族判定。

    ⚠ patch ``hiveweave.tools.misc_tools.get_project_id``（``from .helpers
    import`` 是导入期名字绑定，patch 原模块属性管不到 misc_tools 里的引用
    —— 旧用例 patch 错了位置还能绿，靠的是「解析失败 ⇒ fail-open」）。
    """
    return [
        patch(
            "hiveweave.tools.misc_tools.get_project_id",
            new=AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.services.org.OrgService.get_agent",
            new=AsyncMock(return_value=_ceo_row()),
        ),
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value=role,
        ),
    ]


def _message_patch(role: str = "ceo"):
    """message_user 通路的 mock 集（save_message 由 ``_send`` 注入以便断言）。"""
    return _identity_patch(role) + [
        patch(
            "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
            new=AsyncMock(),
        ),
    ]


async def _send(
    message: str, ws: str, *, role: str = "ceo", unread: int = 0
):
    """发一条 message_user；``unread > 0`` 先插入真人未读 inbox 行。

    返回 ``(result, save_mock)`` —— 徽章断言读 save_message 收到的 payload。
    from=system 的行不计入人工未读（TEST_DSH_32 P3 后直查 inbox 表）。
    """
    if unread > 0:
        conn = await project_db.ensure_project_db(ws)
        now = int(time.time() * 1000)
        for i in range(unread):
            await conn.execute(
                "INSERT INTO inbox (id, from_agent_id, to_agent_id, message,"
                " read, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                [f"unread-{i}", "peer-agent-x", CEO_ID, "pending reply", now],
            )
        await conn.commit()
    save_mock = AsyncMock()
    with ExitStack() as stack:
        for cm in _message_patch(role=role):
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "hiveweave.services.chat_message.ChatMessageService.save_message",
                new=save_mock,
            )
        )
        result = await message_user_tool(
            MessageUserParams(message=message), CEO_ID, ws
        )
    return result, save_mock


async def _mark(ws: str, *, role: str = "ceo"):
    with ExitStack() as stack:
        for cm in _identity_patch(role=role):
            stack.enter_context(cm)
        return await mark_delivery_complete_tool(
            MarkDeliveryCompleteParams(), CEO_ID, ws
        )


async def _delivery_state(ws: str) -> str | None:
    conn = await project_db.ensure_project_db(ws)
    cur = await conn.execute(
        "SELECT delivery_state FROM project_meta WHERE project_id = ?",
        [PROJECT_ID],
    )
    row = await cur.fetchone()
    await cur.close()
    return row["delivery_state"] if row else None


async def _delivery_policy_codes(ws: str) -> list[str]:
    """读 blocked 快照里的政策码（设计契约 = 可断言的是 code，不是文案）。"""
    conn = await project_db.ensure_project_db(ws)
    cur = await conn.execute(
        "SELECT delivery_snapshot FROM project_meta WHERE project_id = ?",
        [PROJECT_ID],
    )
    row = await cur.fetchone()
    await cur.close()
    if not row or not row["delivery_snapshot"]:
        return []
    snap = json.loads(row["delivery_snapshot"])
    return list(snap.get("policy_codes") or [])


# ── 消息出口：零文本判断 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completion_wording_never_blocks_and_state_stays_unmarked(env):
    """账本脏 + 完工措辞 ⇒ 消息**不拦**（文本门已下线），徽章如实显示
    「未标记完工」并附当前阻塞项 —— 谎报由徽章揭穿，不由措辞门假装不存在。"""
    await _insert_task(
        env["workspace_path"],
        "t-fail",
        status="running",
        evidence={"verdict": "FAIL", "blocking_issues": ["/_admin 404"]},
    )
    await _insert_task(env["workspace_path"], "t-apr", status="approved")

    result, save = await _send(
        "项目已全部完成，可以交付使用", env["workspace_path"]
    )
    assert result.success is True, result.error
    meta = save.call_args.args[0]["metadata"]
    assert meta["delivery_state"] == DELIVERY_STATE_UNMARKED
    codes = {b["code"] for b in meta["delivery_blockers"]}
    assert POLICY_LEDGER_FAIL_VERDICT in codes
    assert POLICY_LEDGER_APPROVED_OPEN in codes


@pytest.mark.asyncio
async def test_non_ceo_message_has_no_badge_and_never_blocked(env):
    """非 CEO 不带徽章、也永不被拦（徽章语义 = CEO 交付声明是否被核验）。"""
    await _insert_task(
        env["workspace_path"],
        "t-fail",
        status="running",
        evidence={"verdict": "FAIL", "blocking_issues": ["x"]},
    )
    result, save = await _send(
        "模块已全部完成", env["workspace_path"], role="coordinator"
    )
    assert result.success is True, result.error
    payload = save.call_args.args[0]
    assert not (payload.get("metadata") or {}).get("delivery_state")


@pytest.mark.asyncio
async def test_ceo_progress_update_not_blocked(env):
    """普通进度汇报照常发送（无论账本状态）。"""
    await _insert_task(
        env["workspace_path"],
        "t-fail",
        status="running",
        evidence={"verdict": "FAIL", "blocking_issues": ["x"]},
    )
    result, _ = await _send(
        "当前进度：3/5 模块已合入 MAIN", env["workspace_path"]
    )
    assert result.success is True, result.error


# ── mark_delivery_complete：唯一写者，判定只读状态 ─────────────────


@pytest.mark.asyncio
async def test_mark_delivery_complete_refused_on_dirty_ledger(env):
    """未解决 FAIL 终验 + approved 未 closed ⇒ 拒绝并写 blocked 状态位。"""
    await _insert_task(
        env["workspace_path"],
        "t-fail",
        status="running",
        evidence={"verdict": "FAIL", "blocking_issues": ["/_admin 404"]},
    )
    await _insert_task(env["workspace_path"], "t-apr", status="approved")

    result = await _mark(env["workspace_path"])
    assert result.success is False
    assert (result.error or "").strip()  # 拒绝必须带理由（给 CEO 读）
    codes = await _delivery_policy_codes(env["workspace_path"])
    assert POLICY_LEDGER_FAIL_VERDICT in codes
    assert POLICY_LEDGER_APPROVED_OPEN in codes
    assert await _delivery_state(env["workspace_path"]) == DELIVERY_STATE_BLOCKED


@pytest.mark.asyncio
async def test_mark_delivery_complete_refused_on_unread_inbox(env):
    """仅本人未读人工消息也足以拒绝标记。"""
    conn = await project_db.ensure_project_db(env["workspace_path"])
    now = int(time.time() * 1000)
    for i in range(3):
        await conn.execute(
            "INSERT INTO inbox (id, from_agent_id, to_agent_id, message,"
            " read, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            [f"unread-{i}", "peer-agent-x", CEO_ID, "pending reply", now],
        )
    await conn.commit()

    result = await _mark(env["workspace_path"])
    assert result.success is False
    codes = await _delivery_policy_codes(env["workspace_path"])
    assert POLICY_INBOX_UNREAD_HUMAN in codes
    assert await _delivery_state(env["workspace_path"]) == DELIVERY_STATE_BLOCKED


@pytest.mark.asyncio
async def test_system_unread_does_not_block_mark_delivery_complete(env):
    """TEST_DSH_32 P3 延续：from=system 的未读副本（平台投递回执）不算
    人工未读 —— 不阻塞标记。"""
    conn = await project_db.ensure_project_db(env["workspace_path"])
    now = int(time.time() * 1000)
    for i in range(2):
        await conn.execute(
            "INSERT INTO inbox (id, from_agent_id, to_agent_id, message,"
            " read, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            [f"sys-{i}", "system", CEO_ID, "[BASH FAILED] delivery", now],
        )
    await conn.commit()

    result = await _mark(env["workspace_path"])
    assert result.success is True, result.error
    assert await _delivery_state(env["workspace_path"]) == DELIVERY_STATE_COMPLETE


@pytest.mark.asyncio
async def test_mark_delivery_complete_succeeds_on_clean_ledger_and_badge_flips(
    env,
):
    """账本干净 ⇒ 写 complete；此后消息徽章从 unmarked 翻转为 complete。"""
    before, save_before = await _send("在吗", env["workspace_path"])
    assert before.success is True
    assert (
        save_before.call_args.args[0]["metadata"]["delivery_state"]
        == DELIVERY_STATE_UNMARKED
    )

    result = await _mark(env["workspace_path"])
    assert result.success is True, result.error
    assert await _delivery_state(env["workspace_path"]) == DELIVERY_STATE_COMPLETE

    after, save_after = await _send("交付完成", env["workspace_path"])
    assert after.success is True
    meta = save_after.call_args.args[0]["metadata"]
    assert meta["delivery_state"] == DELIVERY_STATE_COMPLETE
    assert meta.get("delivery_at")


@pytest.mark.asyncio
async def test_mark_delivery_complete_ceo_only(env):
    """仅 CEO 可标记（coordinator ⇒ 拒绝，状态位不动）。"""
    result = await _mark(env["workspace_path"], role="coordinator")
    assert result.success is False
    assert await _delivery_state(env["workspace_path"]) is None


def test_e4_fourth_query_present_source_guard():
    """E4 变异替代：源码级存在性守卫——删第四查询本测试即红。

    E4 验收提到「变异测试（删第四查询 → 测试红）」；变异操作不适合放进
    常规单测运行，此处以源码断言等价防回归（turn exit 的 FAIL 终验查询
    与 `_delivery_blockers` 的第一 blockers 判据同源同语义）。
    """
    import hiveweave.services.turn_exit as te

    src = Path(te.__file__).read_text(encoding="utf-8")
    assert "upper(json_extract(evidence, '$.verdict')) = 'FAIL'" in src
