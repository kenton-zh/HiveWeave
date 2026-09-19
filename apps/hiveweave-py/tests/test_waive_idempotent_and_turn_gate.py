"""L8 家族修复回归：waive_attestation 幂等豁免 + CEO_PROJECT_PENDING 窄豁免。

批2（TEST_DSH_63）：CEO 已对任务落 waiver 后重复 waive（同任务同 kind 且
未过期）曾被判非法（"already has an unexpired waiver"），把发起人的善后
通道卡死。与 update_task_status running→running（TEST_DSH_62）同病同修法：
同 kind 重复 waive → ToolResult.ok no-op 回执（outcome=already_waived）；
kind 不同（tool_failure ↔ quality 改变审批权归属）→ 维持拒绝。

批4（TEST_DSH_63 ×7 复撞）：VERIFY 持串行锁、其余任务全 closed 时，CEO
done_slice 被 CEO_PROJECT_PENDING 的「派活待命叶子」子条款卡死——确实无
活可派，义务物理不可能完成。窄豁免：当且仅当 blocker 集只剩「待命叶子
未派活」且不存在可派任务（无 created 未认领任务）时该子条款不成立；
submitted/verifying/FAIL 等其他子条款照旧阻断，不整体拆门。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services import turn_exit as turn_exit_module
from hiveweave.services.turn_exit import (
    ceo_project_pending_obligations,
    pop_idle_leaf_exempt_note,
    pre_check_exit_gates,
)

PROJECT_ID = "test-waive-idem-project"
CEO_ID = "ceo-idem-0001"
LEAF_ID = "leaf-idem-0001"


# ── 批2：waive_attestation 幂等豁免（mock 层，与 test_dogfood_p2_fixes
#    同款打桩风格，不触真实 DB）───────────────────────────────────


def _waive_patches(
    *,
    existing_waiver: dict | None,
    prior_count: int = 0,
    task: dict | None = None,
    family: str = "ceo",
):
    """Common patch stack for waive_attestation_tool unit calls."""
    task = task or {
        "id": "t-waive-idem",
        "title": "fix tooling",
        "tags": [],
        "assignee_id": "exec1",
        "evidence": {"verdict": "PASS"},
    }
    return (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch("hiveweave.services.task.TaskService"),
        patch("hiveweave.services.org.OrgService"),
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value=family,
        ),
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value=existing_waiver),
        ),
        patch(
            "hiveweave.services.attestation.count_waivers",
            AsyncMock(return_value=prior_count),
        ),
        patch(
            "hiveweave.tools.tasks.waive._format_post_waive_approve_tip",
            AsyncMock(return_value=""),
        ),
    )


async def _call_waive(reason_kind: str, reason: str):
    from hiveweave.tools.task_tools import (
        WaiveAttestationParams,
        waive_attestation_tool,
    )

    return await waive_attestation_tool(
        WaiveAttestationParams(
            taskId="t-waive-idem",
            reason=reason,
            reasonKind=reason_kind,
            evidenceAttestationId="",
        ),
        agent_id=CEO_ID,
        workspace="/tmp",
    )


_NOW_MS = int(time.time() * 1000)


@pytest.mark.asyncio
async def test_waive_repeat_same_kind_is_noop_ok():
    """同任务同 kind 重复 waive → no-op 成功回执 + outcome=already_waived。

    prior_count=2（已到终身上限）也在位：幂等检查必须先于 cap——no-op
    不新增 waiver 行，重试不应被 cap 卡死。reason 换一套完全不同的说法
    仍 no-op（不做模糊 reason 判断，仅同 kind 即 no-op）。
    """
    from hiveweave.services.attestation import MAX_WAIVERS_PER_TASK

    existing = {
        "id": "waiver-uuid-0001",
        "agent_id": CEO_ID,
        "waiver_kind": "tool_failure",
        "expires_at": _NOW_MS + 3_600_000,
    }
    patches = _waive_patches(
        existing_waiver=existing,
        prior_count=MAX_WAIVERS_PER_TASK,
    )
    with patch(
        "hiveweave.services.attestation.create_waiver",
        AsyncMock(return_value="should-not-create"),
    ) as create, patches[0], patches[1] as TS, patches[2] as Org, patches[3], patches[4], patches[5], patches[6]:
        TS.return_value.get_task = AsyncMock(
            return_value={
                "id": "t-waive-idem",
                "title": "fix tooling",
                "tags": [],
                "assignee_id": "exec1",
                "evidence": {"verdict": "PASS"},
            }
        )
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": CEO_ID, "role": "ceo"}
        )
        result = await _call_waive(
            "tool_failure",
            "审计 LLM 暂态不可用，以人工复核记录替代（与首次豁免理由措辞"
            "完全不同的重试）",
        )
    assert result.success is True
    out = result.output or ""
    assert "outcome=already_waived" in out
    assert "already waived (no-op)" in out
    assert "kind=tool_failure" in out
    assert "无需重复豁免" in out
    # 既有 waiver 的有效期要回执可见
    assert "剩" in out or "无过期时间" in out
    # no-op 不得新增 waiver 行
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_waive_repeat_different_kind_still_rejected():
    """kind 不同 = 实质语义变化（审批权归属），维持拒绝不 no-op。"""
    existing = {
        "id": "waiver-uuid-0002",
        "agent_id": CEO_ID,
        "waiver_kind": "tool_failure",
        "expires_at": _NOW_MS + 3_600_000,
    }
    patches = _waive_patches(existing_waiver=existing, prior_count=1)
    with patches[0], patches[1] as TS, patches[2] as Org, patches[3], patches[4], patches[5], patches[6]:
        TS.return_value.get_task = AsyncMock(
            return_value={
                "id": "t-waive-idem",
                "title": "fix tooling",
                "tags": [],
                "assignee_id": "exec1",
                "evidence": {"verdict": "PASS"},
            }
        )
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": CEO_ID, "role": "ceo"}
        )
        result = await _call_waive(
            "quality",
            "改口称质量类豁免——kind 变化会改变谁能审批，不允许绕过",
        )
    assert result.success is False
    text = (result.output or "") + (result.error or "")
    assert "already has an unexpired waiver" in text
    assert "kind=tool_failure" in text


@pytest.mark.asyncio
async def test_waive_first_time_unchanged():
    """无既有 waiver 的首次 waive → 原成功行为不变。"""
    patches = _waive_patches(existing_waiver=None, prior_count=0)
    with patch(
        "hiveweave.services.attestation.create_waiver",
        AsyncMock(return_value="waiver-new-1"),
    ) as create, patches[0], patches[1] as TS, patches[2] as Org, patches[3], patches[4], patches[5], patches[6], patch(
        "hiveweave.services.inbox.InboxService"
    ) as Inbox:
        TS.return_value.get_task = AsyncMock(
            return_value={
                "id": "t-waive-idem",
                "title": "fix tooling",
                "tags": [],
                "assignee_id": "exec1",
                "evidence": {"verdict": "PASS"},
            }
        )
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": CEO_ID, "role": "ceo"}
        )
        Inbox.return_value.send_message = AsyncMock()
        result = await _call_waive(
            "tool_failure",
            "审计 LLM 暂态不可用，首次豁免该任务的 code_audit 凭证",
        )
    assert result.success is True
    out = result.output or ""
    assert "Attestation waived" in out
    assert "outcome=already_waived" not in out
    create.assert_awaited()


# ── 批4：CEO_PROJECT_PENDING 窄豁免（DB 层，与
#    test_ceo_project_pending_gate.py 同款 fixture）────────────────


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.clear()
        turn_exit_module._idle_leaf_exempt_notes.clear()

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"workspace_path": workspace_path}

        turn_exit_module._idle_leaf_exempt_notes.clear()
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _ceo_row():
    return {
        "id": CEO_ID,
        "role": "ceo",
        "permission_type": "coordinator",
        "status": "active",
    }


async def _insert_idle_leaf(ws: str) -> None:
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status,"
        " permission_type, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            LEAF_ID,
            "A130",
            PROJECT_ID,
            "Vera",
            "后端工程师",
            "active",
            "executor",
            now - 20 * 60 * 1000,  # 超过 10 分钟宽限期
            now,
        ],
    )
    await conn.commit()


async def _insert_task(
    ws: str,
    task_id: str,
    *,
    status: str,
    assignee_id: str | None = None,
) -> None:
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, creator_id, assignee_id,"
        " status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [task_id, PROJECT_ID, "t", "someone-else", assignee_id, status, now, now],
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_gate_idle_leaf_only_no_dispatchable_work_exempted(env):
    """只剩 idle-leaf blocker 且无可派任务 → 放行 + pending_idle_leaf_exempted。"""
    ws = env["workspace_path"]
    await _insert_idle_leaf(ws)
    await _insert_task(ws, "t-done", status="closed")  # 其余任务全 closed
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert pending == []
    note = pop_idle_leaf_exempt_note(CEO_ID)
    assert "pending_idle_leaf_exempted" in note
    assert "无活可派" in note

    # 同步预检同口径：done_slice 不再被 CEO_PROJECT_PENDING 拒
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_outstanding_ask_senders",
        new=AsyncMock(return_value=set()),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_sent_recipients_since",
        new=AsyncMock(return_value=set()),
    ):
        violations = await pre_check_exit_gates(
            CEO_ID, PROJECT_ID, phase="done_slice"
        )
    assert "CEO_PROJECT_PENDING" not in violations


@pytest.mark.asyncio
async def test_gate_idle_leaf_with_dispatchable_task_still_blocks(env):
    """存在 created 未认领任务 → 有活可派，idle-leaf 子条款照旧成立。"""
    ws = env["workspace_path"]
    await _insert_idle_leaf(ws)
    await _insert_task(ws, "t-open", status="created")  # 可派
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert any("待命叶子" in p for p in pending)
    assert pop_idle_leaf_exempt_note(CEO_ID) == ""


@pytest.mark.asyncio
async def test_gate_submitted_clause_still_blocks_with_idle_leaf(env):
    """有 submitted 待审 → 其他子条款照旧阻断，不许整体拆门。"""
    ws = env["workspace_path"]
    await _insert_idle_leaf(ws)
    await _insert_task(ws, "t-sub", status="submitted")
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert any("submitted" in p for p in pending)
    assert any("待命叶子" in p for p in pending)
    assert pop_idle_leaf_exempt_note(CEO_ID) == ""

    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_outstanding_ask_senders",
        new=AsyncMock(return_value=set()),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_sent_recipients_since",
        new=AsyncMock(return_value=set()),
    ):
        violations = await pre_check_exit_gates(
            CEO_ID, PROJECT_ID, phase="done_slice"
        )
    assert "CEO_PROJECT_PENDING" in violations
    from hiveweave.services.turn_exit import pop_ceo_project_pending_details

    details = pop_ceo_project_pending_details(CEO_ID)
    assert any("submitted" in d for d in details)


@pytest.mark.asyncio
async def test_gate_verifying_clause_still_blocks_with_idle_leaf(env):
    """verifying（verify 串行锁占用中）+ 待命叶子 → verifying 照旧阻断。"""
    ws = env["workspace_path"]
    await _insert_idle_leaf(ws)
    await _insert_task(ws, "t-vfy", status="verifying")
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(return_value=_ceo_row()),
    ):
        pending = await ceo_project_pending_obligations(PROJECT_ID, CEO_ID)
    assert any("verifying" in p for p in pending)
    assert pop_idle_leaf_exempt_note(CEO_ID) == ""


@pytest.mark.asyncio
async def test_commit_turn_receipt_carries_exempt_token():
    """豁免放行时 commit_turn 成功回执带稳定 token（turn_tools 接线）。"""
    from hiveweave.services.turn_session import (
        clear_pending_turn_result,
    )
    from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

    agent = "ceo-exempt-receipt"
    turn_exit_module._idle_leaf_exempt_notes[agent] = (
        "无活可派，叶子待命不计义务 (pending_idle_leaf_exempted)"
    )
    params = CommitTurnParams(phase="done_slice", summary="all closed, no work")
    try:
        with patch(
            "hiveweave.db.meta.get_agent_project_id",
            new_callable=AsyncMock,
            return_value="proj-exempt",
        ), patch(
            "hiveweave.services.turn_exit.pre_check_exit_gates",
            new_callable=AsyncMock,
            return_value=[],
        ):
            r = await commit_turn_tool(params, agent, ".")
    finally:
        clear_pending_turn_result(agent)

    assert r.success is True
    out = r.output or ""
    assert "pending_idle_leaf_exempted" in out
    assert "无活可派" in out
    # 回执消费后不残留
    assert pop_idle_leaf_exempt_note(agent) == ""
