"""A1 批2：短 id 判等死锁修复（dashed 12-char slice 可解析 + 写库守门）。

报告依据：docs/platform-issue-research/MERGED-2026-09-28-workbuddy-研究报告合集.md §2-A1。

病根链（已核实）：模型截短 UUID → 平台 `_slice_id`（``str(uuid)[:12]`` =
``"xxxxxxxx-xxx"``，带横线）把短码放进可回传字段二次分发 → 判等侧
``_HEX_REF_RE`` 不含横线 ⇒ 带横线短前缀在 ``canonical_task_id`` fail-open
返原值、跳过 DB 解析 ⇒ 严格相等判否 ⇒ 凭证门死锁（S14 5 条 / D76 3 条
历史行即此形态）。

修法钉死四件事：
1. 判等侧容错：先剥横线再 fullmatch（阳性对照：dashed 切片 vs 完整 id = True）；
2. 写库守门：``require_canonical_task_id`` fail-closed，解析失败拒绝并附处方；
3. attest 工具入口归一化 + 回执回显完整 id；
4. platform_state 可回传字段回传完整 id（签发侧不再铸造短码）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.org import OrgService
from hiveweave.services.platform_state import build_platform_state
from hiveweave.tools.task_tools import AttestDocReviewParams, attest_doc_review_tool

FULL_UUID = "1a2b3c4d-5e6f-7890-abcd-ef1234567890"
# 平台旧 _slice_id 铸造的 12 字符形态：str(uuid)[:12] = "xxxxxxxx-xxx"。
DASHED_SLICE = FULL_UUID[:12]  # "1a2b3c4d-5e6" — 带横线，旧判等侧解析不开
NODASH_SLICE = DASHED_SLICE.replace("-", "")  # "1a2b3c4d5e6"
CANON = FULL_UUID.lower().replace("-", "")


# ── 1. 判等侧容错（阳性对照，报告钦定）──────────────────────────────


@pytest.mark.asyncio
async def test_task_ids_equal_four_states_positive_control():
    """阳性对照：dashed 12 字符切片修复后应判等 True（四态钉死）。

    修复前：``_HEX_REF_RE.fullmatch('1a2b3c4d-5e6')`` 不含横线 ⇒ fail-open
    返原值 ⇒ ``:393`` 严格相等判否 ⇒ 凭证门死锁（S14/D76 现场形态）。
    """
    from hiveweave.services.attestation import _task_ids_equal

    with patch("hiveweave.services.task.TaskService") as TS:
        TS.return_value.require_task_id = AsyncMock(return_value=FULL_UUID)
        # ① 带横线短前缀（本次修复的形态）
        assert await _task_ids_equal("proj", DASHED_SLICE, FULL_UUID) is True
        # ② 不带横线短前缀（既有能力，防回归）
        assert await _task_ids_equal("proj", NODASH_SLICE, FULL_UUID) is True
        # ③ 完整 id 两种横线形态互判等
        assert await _task_ids_equal("proj", FULL_UUID, CANON) is True
        # ④ 垃圾串：与完整 id 判否；与自身判等（合成 id 语义不误伤）
        assert await _task_ids_equal("proj", "task-1", FULL_UUID) is False
        assert await _task_ids_equal("proj", "task-1", "task-1") is True


@pytest.mark.asyncio
async def test_canonical_task_id_dash_strip_tolerance():
    """dashed 切片走 DB 解析；解析失败仍 fail-open 返原值（判等侧永不抛）。"""
    from hiveweave.services.attestation import canonical_task_id

    with patch("hiveweave.services.task.TaskService") as TS:
        TS.return_value.require_task_id = AsyncMock(return_value=FULL_UUID)
        assert await canonical_task_id("proj", DASHED_SLICE) == CANON

        TS.return_value.require_task_id = AsyncMock(
            side_effect=ValueError("Task not found")
        )
        assert await canonical_task_id("proj", DASHED_SLICE) == DASHED_SLICE


@pytest.mark.asyncio
async def test_canonical_task_id_non_uuid_refs_stay_raw():
    """非 UUID 形态剥横线后非 hex ⇒ 原样返回，绝不被剥横线损坏（既有语义）。"""
    from hiveweave.services.attestation import canonical_task_id

    assert await canonical_task_id("proj", "task-1") == "task-1"
    assert await canonical_task_id("proj", "t-invalidate-3") == "t-invalidate-3"
    assert await canonical_task_id("proj", None) is None
    assert await canonical_task_id("proj", "  ") is None
    assert await canonical_task_id("proj", FULL_UUID) == CANON


@pytest.mark.asyncio
async def test_get_accepts_dashed_attestation_id_prefix(tmp_path):
    """同族修复：attestation id 的 dashed 短前缀也能前缀命中（原样返 None）。"""
    import hiveweave.services.attestation as att_module
    from hiveweave.services.attestation import attestation_service

    project_id = "short-id-get-proj"
    agent_id = "exec-get-1"
    ws = str(tmp_path)

    async def fake_ws(pid: str):
        return ws if pid == project_id else None

    att_module._migrated.clear()
    with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
        att_id = await attestation_service.create(
            project_id, agent_id=agent_id, kind="test_run",
            command_or_url="pytest", stdout="ok", exit_code=0,
        )
        row = await attestation_service.get(project_id, att_id[:12])
        assert row is not None and row["id"] == att_id


# ── 2. 写库守门 require_canonical_task_id ────────────────────────────


@pytest.mark.asyncio
async def test_require_canonical_task_id_gate():
    """写库守门：完整 id 归一；dashed 切片解析成功归一、失败拒绝附处方；
    合成 id 原样放行。"""
    from hiveweave.services.attestation import require_canonical_task_id

    assert await require_canonical_task_id("proj", None) is None
    assert await require_canonical_task_id("proj", "  ") is None
    assert await require_canonical_task_id("proj", FULL_UUID) == CANON
    # 合成 id：非 UUID 形态原样放行（legacy 语义）
    assert await require_canonical_task_id("proj", "task-1") == "task-1"

    with patch("hiveweave.services.task.TaskService") as TS:
        TS.return_value.require_task_id = AsyncMock(return_value=FULL_UUID)
        assert await require_canonical_task_id("proj", DASHED_SLICE) == CANON

        TS.return_value.require_task_id = AsyncMock(
            side_effect=ValueError("Task not found: 1a2b3c4d-5e6")
        )
        with pytest.raises(ValueError) as ei:
            await require_canonical_task_id("proj", DASHED_SLICE)
        msg = str(ei.value)
        # 处方：完整 id 从台账复制、勿截断、用完整 id 重试
        assert "FULL id" in msg
        assert "do not truncate" in msg
        assert "retry with the full id" in msg
        assert DASHED_SLICE in msg


# ── 3. attest 写库归一化（端到端，真项目库）──────────────────────────


@pytest.mark.asyncio
async def test_attestation_create_normalizes_dashed_slice(tmp_path):
    """dashed 12 字符切片落库前归一成 canonical 完整 id（历史死锁形态）。"""
    import hiveweave.services.attestation as att_module
    from hiveweave.services.attestation import attestation_service

    project_id = "short-id-gate-proj"
    agent_id = "exec-sig-1"
    ws = str(tmp_path)

    async def fake_ws(pid: str):
        return ws if pid == project_id else None

    task_module._migrated.clear()
    att_module._migrated.clear()
    with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
        svc = task_module.TaskService()
        tid = await svc.create_task(
            project_id=project_id, title="T", description="d",
            creator_id=agent_id,
        )
        assert tid == tid.strip() and "-" in tid  # full UUID with dashes
        att_id = await attestation_service.create(
            project_id, agent_id=agent_id, kind="test_run",
            task_id=tid[:12],  # 平台旧切片形态，带横线
            command_or_url="pytest", stdout="ok", exit_code=0,
        )
        rec = await attestation_service.get(project_id, att_id)
        assert rec is not None
        assert rec["task_id"] == tid.lower().replace("-", "")


@pytest.mark.asyncio
async def test_attestation_create_rejects_unresolvable_dashed_slice(tmp_path):
    """解析失败的 dashed 切片 → fail-closed 拒绝写入，回执附处方。"""
    import hiveweave.services.attestation as att_module
    from hiveweave.services.attestation import attestation_service

    project_id = "short-id-reject-proj"
    agent_id = "exec-sir-1"
    ws = str(tmp_path)

    async def fake_ws(pid: str):
        return ws if pid == project_id else None

    task_module._migrated.clear()
    att_module._migrated.clear()
    with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
        svc = task_module.TaskService()
        tid_a = await svc.create_task(
            project_id=project_id, title="A", description="d",
            creator_id=agent_id,
        )
        # 形态合法（dashed 切片）但前缀不命中任何任务 → 拒绝 + 处方
        ghost = "deadbeef-999"
        with pytest.raises(ValueError) as ei:
            await attestation_service.create(
                project_id, agent_id=agent_id, kind="test_run",
                task_id=ghost,
                command_or_url="pytest", stdout="ok", exit_code=0,
            )
        assert "retry with the full id" in str(ei.value)
        # 真实任务的短前缀（8 字符，唯一）照常归一入库
        att_id = await attestation_service.create(
            project_id, agent_id=agent_id, kind="test_run",
            task_id=tid_a[:8],
            command_or_url="pytest", stdout="ok", exit_code=0,
        )
        rec = await attestation_service.get(project_id, att_id)
        assert rec is not None
        assert rec["task_id"] == tid_a.lower().replace("-", "")


# ── 4. attest 工具入口：拒绝处方 + 回执回显完整 id ───────────────────


@pytest.mark.asyncio
async def test_attest_doc_review_rejects_unresolvable_task_ref(tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("# doc\n", encoding="utf-8")
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(tmp_path)),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch("hiveweave.services.task.TaskService") as TS,
    ):
        Org.return_value.get_agent = AsyncMock(return_value={})
        TS.return_value.require_task_id = AsyncMock(
            side_effect=ValueError("Task not found: 1a2b3c4d-5e6")
        )
        result = await attest_doc_review_tool(
            AttestDocReviewParams(
                task_id=DASHED_SLICE, files=[{"path": "a.md"}],
            ),
            agent_id="a1",
            workspace=str(tmp_path),
        )
    assert result.success is False
    err = result.error or ""
    assert "retry with the full id" in err
    assert "do not truncate" in err


@pytest.mark.asyncio
async def test_attest_doc_review_normalizes_and_echoes_full_id(tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("# doc\n", encoding="utf-8")
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(tmp_path)),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch("hiveweave.services.task.TaskService") as TS,
        patch(
            "hiveweave.services.attestation.create_doc_review",
            AsyncMock(
                return_value=("att-1", {"files": [{"path": "a.md"}]}),
            ),
        ) as create,
    ):
        Org.return_value.get_agent = AsyncMock(return_value={})
        TS.return_value.require_task_id = AsyncMock(return_value=FULL_UUID)
        result = await attest_doc_review_tool(
            AttestDocReviewParams(
                task_id=DASHED_SLICE, files=[{"path": "a.md"}],
            ),
            agent_id="a1",
            workspace=str(tmp_path),
        )
    assert result.success is True
    # 落库参数已是 canonical 完整 id；回执回显完整 id 供模型复制
    assert create.await_args.kwargs["task_id"] == CANON
    assert f"task_id={CANON}" in (result.output or "")


# ── 5. 签发侧：platform_state 可回传字段回完整 id ────────────────────


@pytest.fixture
async def ps_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = str(Path(tmpdir).resolve())
        pid = "short-id-ps-proj"

        async def fake_ws(p: str):
            return ws if p == pid else None

        task_module._migrated.clear()
        with (
            patch("hiveweave.db.meta.get_project_workspace", fake_ws),
            patch(
                "hiveweave.agents.supervisor.agent_manager.get_agent",
                return_value=None,
            ),
        ):
            yield {"project_id": pid, "workspace": ws}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_platform_state_ledger_rows_carry_full_ids(ps_env):
    """ledger.mine / ledger.scope 的 task id、parent_task_id、depends_on
    必须是完整 id——签发侧不再铸造 12 字符短码。"""
    pid = ps_env["project_id"]
    viewer_id = "sig-ceo-0001"
    worker_id = "sig-worker-01"
    ws = ps_env["workspace"]
    org = OrgService()
    await org.create_agent(
        {
            "id": viewer_id, "project_id": pid, "name": "审",
            "role": "ceo", "permission_type": "coordinator",
            "status": "active", "short_id": "S001", "workspace_path": ws,
        },
        bootstrap=True,
    )
    await org.create_agent(
        {
            "id": worker_id, "project_id": pid, "name": "干",
            "role": "engineer", "permission_type": "executor",
            "parent_id": viewer_id, "status": "active", "short_id": "S002",
            "workspace_path": ws,
        },
        bootstrap=True,
    )
    svc = task_module.TaskService()
    parent_tid = await svc.create_task(
        project_id=pid, title="P", description="d",
        creator_id=viewer_id, assignee_id=viewer_id,
    )
    child_tid = await svc.create_task(
        project_id=pid, title="C", description="d",
        creator_id=viewer_id, assignee_id=worker_id,
        parent_task_id=parent_tid,
        depends_on=[parent_tid],
    )

    snap = await build_platform_state(agent_id=viewer_id, project_id=pid)
    ledger = snap["ledger"]

    # mine：完整 id（修复前是 [:12] 切片）
    mine_ids = {r["id"] for r in ledger["mine"]}
    assert parent_tid in mine_ids
    assert DASHED_SLICE not in mine_ids

    # scope（CEO 全项目可见）：id / parent_task_id / depends_on 全为完整 id
    scope_by_id = {r["id"]: r for r in ledger["scope"]}
    assert child_tid in scope_by_id
    child_row = scope_by_id[child_tid]
    assert child_row["parent_task_id"] == parent_tid
    assert child_row["depends_on"] == [parent_tid]

    # LLM 文本渲染同样携带完整 id
    from hiveweave.services.platform_state import format_platform_state

    text = format_platform_state(snap)
    assert child_tid in text
