"""TEST_DSH_70 门禁家族第一批修复回归（P0-1 / P0-2 / P0-3 / P1-2）。

权威定案：docs/platform-issue-research/platform-issue-report-TEST_DSH_70-2026-09-25-merged.html §6。

- P0-1  coverage 门：``{"item":[…]}`` 单键解包（b）、三态回执（a）、
        凭证↔条目绑定诊断（c）、多键 dict 禁止 values() 展平（b 反例）。
- P0-2  doom-loop：submit_task 限额对齐 gate_reject 维度（12），门禁连拒
        的合法重试不再被 doom 误杀（run 9910ce3d 实证）。
- P0-3  VERIFY 空 acceptance_criteria：建单硬门（或显式批准事实位）+
        关单/approve 前 criteria 门 + 删「空清单可绕过」降档披露文案。
- P1-2  子门消费 waiver 对齐主门（不对形状错生效）+ ≥3 连拒自动
        blocked+external + wakeAt='' 归一 None。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from hiveweave.db import project as project_db
from hiveweave.llm.streamer.doom_loop import (
    GATE_REJECT_STALL_LIMIT,
    doom_loop_limit,
)
from hiveweave.llm.streamer.tool_exec import ToolExecMixin
from hiveweave.services import attestation as att_module
from hiveweave.services import task as task_module
from hiveweave.services.attestation import attestation_service, create_waiver
from hiveweave.services.tasks.acceptance import (
    ATTESTATION_IDS_FIELD,
    COVERAGE_EVIDENCE_KEY,
    assess_acceptance_coverage_verified,
    format_acceptance_coverage_error,
    format_acceptance_coverage_verdict,
    parse_acceptance_coverage,
    parse_acceptance_coverage_ex,
    uncovered_acceptance_items_verified,
)
from hiveweave.services.tasks.gate_reject_guard import (
    GATE_REJECT_ESCALATE_AFTER,
    escalate_if_gate_reject_loop,
    gate_rejection_count,
    reset_for_tests as reset_guard,
)
from hiveweave.services.tasks.verify_criteria_gate import (
    has_empty_criteria_approval,
    record_empty_criteria_approval,
    verify_creation_criteria_error,
)
from hiveweave.services.task import TaskService
from hiveweave.tools.tasks.lifecycle import (
    UpdateTaskStatusParams,
    _parse_wake_at_ms,
)

PROJECT_ID = "dsh70-proj"
COORD = "dsh70-coord"
EXEC = "dsh70-exec"

CRITERIA = [
    {"id": "c1", "text": "导出 CSV 功能可用"},
    {"id": "c2", "text": "边界情况有测试覆盖"},
]
_C1_LABEL = "条目c1: 导出 CSV 功能可用"
_C2_LABEL = "条目c2: 边界情况有测试覆盖"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        att_module._migrated.clear()
        task_module._migrated.clear()
        reset_guard()
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        reset_guard()


async def _mk_verify_task(env, criteria=CRITERIA) -> str:
    ts = TaskService()
    return await ts.create_task(
        env["project_id"],
        "VERIFY: 导出",
        "d",
        creator_id=COORD,
        assignee_id=EXEC,
        acceptance_criteria=criteria,
        kind="verify",
        dedup_policy="allow",
    )


async def _test_run(env, task_id: str, *, exit_code: int = 0) -> str:
    return await attestation_service.create(
        env["project_id"],
        agent_id=EXEC,
        kind="test_run",
        task_id=task_id,
        command_or_url="uv run pytest tests/test_export.py -q",
        exit_code=exit_code,
        stdout="54 passed",
    )


async def _gaps(env, task_id, evidence, criteria=CRITERIA):
    return await uncovered_acceptance_items_verified(
        env["project_id"],
        task_id,
        criteria,
        evidence,
        expected_agent_id=EXEC,
    )


async def _assess(env, task_id, evidence, criteria=CRITERIA):
    return await assess_acceptance_coverage_verified(
        env["project_id"],
        task_id,
        criteria,
        evidence,
        expected_agent_id=EXEC,
    )


# ────────────────────────────────────────────────────────────
# P0-1b：ids_raw / 单键声明解包
# ────────────────────────────────────────────────────────────


def test_single_key_item_shape_unwraps():
    """``{"item":[…]}`` 单键声明现在能被解包采用（71 轮 35/35 全中的形状）。"""
    claims = parse_acceptance_coverage(
        {COVERAGE_EVIDENCE_KEY: {"1": {"item": ["att-1", "att-2"]}}}
    )
    assert claims == {
        "1": {"attestation_ids": ["att-1", "att-2"], "not_applicable_reason": ""}
    }
    # 单键 + 裸串值同样解包（与顶层裸串 id 归一口径一致）
    claims2 = parse_acceptance_coverage(
        {COVERAGE_EVIDENCE_KEY: {"1": {"item": "att-1"}}}
    )
    assert claims2["1"]["attestation_ids"] == ["att-1"]
    # list 形态里的单键声明（id 结构键不参与「未知键」计数）
    claims3 = parse_acceptance_coverage(
        {COVERAGE_EVIDENCE_KEY: [{"id": "c1", "item": ["att-1"]}]}
    )
    assert claims3["c1"]["attestation_ids"] == ["att-1"]


@pytest.mark.asyncio
async def test_single_key_unwrap_end_to_end_covers(env):
    """正例：真实 test_run 凭证 + `{"item":[…]}` 声明 ⇒ 覆盖通过（修前全拒）。"""
    tid = await _mk_verify_task(env)
    a1 = await _test_run(env, tid)
    gaps = await _gaps(
        env, tid, {COVERAGE_EVIDENCE_KEY: {"c1": {"item": [a1]},
                                           "c2": {"item": [a1]}}}
    )
    assert gaps == []


def test_multi_key_dict_not_flattened_becomes_shape_error():
    """反例：多键 dict 声明**禁止 values() 展平**（歧义）→ 形状错 + 日志。"""
    with capture_logs() as logs:
        claims, errors = parse_acceptance_coverage_ex(
            {COVERAGE_EVIDENCE_KEY: {"1": {"item": ["a"], "why": "b"}}}
        )
    assert claims == {}  # fail-closed：不进覆盖
    assert len(errors) == 1
    assert "'item'" in errors[0].detail and "'why'" in errors[0].detail
    # 解析失败必须有日志，不许静默 None（P0-1b 铁律）
    assert any(
        x.get("event") == "acceptance_coverage_shape_error" for x in logs
    ), logs


# ────────────────────────────────────────────────────────────
# P0-1a / P0-1c：三态回执 + 凭证↔条目绑定诊断
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_undeclared_and_unverified_distinct_receipts(env):
    """未声明 / 未核验两种态回执措辞不同，且未核验态点名凭证↔条目绑定。"""
    tid = await _mk_verify_task(env)

    # 未声明：完全没有 coverage 声明
    a = await _assess(env, tid, {"summary": "done"})
    assert a.undeclared == [_C1_LABEL, _C2_LABEL]
    assert not a.shape_errors and not a.unverified
    msg = format_acceptance_coverage_verdict(a)
    assert "未声明" in msg and "没有任何" in msg
    # 平铺兼容出口逐字保持旧行为（条目原序、纯标签）
    assert await _gaps(env, tid, {"summary": "done"}) == [_C1_LABEL, _C2_LABEL]

    # 未核验：声明合法但凭证对不上 —— 回执含「未核验」态 + 逐条绑定诊断
    evidence = {
        COVERAGE_EVIDENCE_KEY: {
            "c1": {ATTESTATION_IDS_FIELD: ["ghost-id-1"]},
        }
    }
    a2 = await _assess(env, tid, evidence)
    assert a2.unverified == [_C1_LABEL]  # c2 未声明，c1 声明了但没核验过
    assert a2.credential_notes["c1"], a2.credential_notes
    assert any("ghost-id-1" in n for n in a2.credential_notes["c1"])
    msg2 = format_acceptance_coverage_verdict(a2)
    assert "未核验" in msg2 and "ghost-id-1" in msg2 and "条目c1" in msg2


@pytest.mark.asyncio
async def test_shape_error_receipt_names_state_and_summary(env):
    """形状错态回执：点出「形状错」+ 回显收到的原始形态摘要。"""
    tid = await _mk_verify_task(env)
    evidence = {
        COVERAGE_EVIDENCE_KEY: {"c1": {"item": ["a1"], "note": "x"}}
    }
    a = await _assess(env, tid, evidence)
    assert len(a.shape_errors) == 1 and a.shape_errors[0].item_id == "c1"
    msg = format_acceptance_coverage_verdict(a)
    assert "形状错" in msg
    assert "条目c1" in msg
    assert "'item'" in msg and "'note'" in msg  # 形态摘要回显
    # 形状错条目**不**落进未声明态（声明存在，只是解析失败）
    assert a.undeclared == [_C2_LABEL]


def test_flat_shape_error_line_inline():
    """平铺兼容出口：形状错行内联在该条目处，含「形状错」与形态摘要。"""
    criteria = ["甲", "乙"]
    evidence = {COVERAGE_EVIDENCE_KEY: {"1": {"a": 1, "b": 2}}}

    async def _run():
        return await uncovered_acceptance_items_verified(
            "p", None, criteria, evidence
        )

    import asyncio

    gaps = asyncio.run(_run())
    assert len(gaps) == 2
    assert "形状错" in gaps[0] and "条目1" in gaps[0]
    assert gaps[1].startswith("条目2")


# ────────────────────────────────────────────────────────────
# P0-2：doom-loop 不再误杀被门逼出的 submit_task 合法重试
# ────────────────────────────────────────────────────────────


def test_submit_task_doom_limit_aligned_with_gate_reject():
    assert doom_loop_limit("submit_task") == GATE_REJECT_STALL_LIMIT == 12


def test_gate_reject_retries_no_longer_doom_killed():
    """同参连拒 11 次不触顶（修前 3 次即被 PermanentError 杀）；基线不变。"""
    tracker: dict = {"last_key": None, "count": 0}
    call = {"name": "submit_task", "arguments": '{"taskId":"t"}', "id": "c"}
    eleven = [dict(call, id=f"c{i}") for i in range(11)]
    assert ToolExecMixin._detect_doom_loop(eleven, tracker) is None
    # 第 12 次同参触顶（与 gate_reject 维度同口收口）
    twelve = eleven + [dict(call, id="c11")]
    assert ToolExecMixin._detect_doom_loop(twelve, tracker) == "submit_task"
    # 基线不回归：普通副作用工具仍是 3
    bt: dict = {"last_key": None, "count": 0}
    bash3 = [{"name": "bash", "arguments": '{"command":"x"}', "id": f"b{i}"}
             for i in range(3)]
    assert ToolExecMixin._detect_doom_loop(bash3, bt) == "bash"


# ────────────────────────────────────────────────────────────
# P0-3：VERIFY 空 acceptance_criteria 建单 / 关单 / 文案
# ────────────────────────────────────────────────────────────


def test_verify_creation_empty_criteria_gate():
    err = verify_creation_criteria_error(criteria=None, allow_empty=False)
    assert err and "acceptance_criteria is empty" in err
    assert "acceptanceCriteria" in err  # 处方指向正路
    # 显式批准位放行（批准事实另行落审计事件）
    assert verify_creation_criteria_error(criteria=None, allow_empty=True) is None
    assert verify_creation_criteria_error(criteria=CRITERIA, allow_empty=False) is None
    assert verify_creation_criteria_error(criteria=[], allow_empty=False) is not None
    assert verify_creation_criteria_error(criteria="[]", allow_empty=False) is not None


def test_creation_and_review_gates_are_wired():
    """门必须接进 create/dispatch 两条 mint 路与 review approve / close。"""
    root = Path(__file__).resolve().parents[1] / "src" / "hiveweave"
    create_src = (root / "tools/tasks/create.py").read_text(encoding="utf-8")
    dispatch_src = (root / "tools/tasks/dispatch.py").read_text(encoding="utf-8")
    review_src = (root / "services/tasks/review.py").read_text(encoding="utf-8")
    close_src = (root / "services/tasks/close.py").read_text(encoding="utf-8")
    assert create_src.count("verify_creation_criteria_error(") >= 1
    assert dispatch_src.count("verify_creation_criteria_error(") >= 1
    assert "assert_verify_criteria_or_approval" in review_src
    assert "assert_verify_criteria_or_approval" in close_src


def test_allow_empty_criteria_exposed_in_llm_schemas():
    """TOOL_PARAM_SCHEMAS 必须暴露 allowEmptyCriteria（acceptanceCoverage 同款
    接线陷阱：不暴露 ⇒ LLM 传参被判 Unknown，批准位实际不可达）。"""
    import hiveweave.tools  # noqa: F401 — populate @tool registry

    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS, validate_tool_args

    for tool_name in ("create_task", "dispatch_task"):
        props = TOOL_PARAM_SCHEMAS[tool_name]["properties"]
        assert "allowEmptyCriteria" in props, tool_name
        assert "allow_empty_criteria" in props["allowEmptyCriteria"]["aliases"]
    normalized, err = validate_tool_args(
        "create_task",
        {
            "title": "T",
            "description": "d",
            "submitGate": "unit",
            "milestoneVerify": True,
            "allowEmptyCriteria": True,
        },
    )
    assert err is None, err
    assert normalized["allowEmptyCriteria"] is True


@pytest.mark.asyncio
async def test_empty_criteria_verify_cannot_close_or_approve(env):
    """反例 e2e：空清单 VERIFY 过不了 close / approve（第 6 张空单的路封死）。"""
    ts = TaskService()
    tid = await _mk_verify_task(env, criteria=None)
    assert await has_empty_criteria_approval(env["project_id"], tid) is False
    with pytest.raises(ValueError, match="acceptance_criteria is empty"):
        await ts.close_task(env["project_id"], tid)

    # approve 路径（reviewing → approved）同样被拦
    tid2 = await _mk_verify_task(env, criteria=None)
    await ts.claim_task(env["project_id"], tid2, EXEC, bypass_verify_serialize=True)
    await ts.start_task(env["project_id"], tid2)
    await ts.submit_task(
        env["project_id"], tid2, {"summary": "s", "verdict": "PASS"}
    )
    await ts.start_review(env["project_id"], tid2)
    with pytest.raises(ValueError, match="acceptance_criteria is empty"):
        await ts.review_task(env["project_id"], tid2, "approve")


@pytest.mark.asyncio
async def test_empty_criteria_verify_close_allowed_with_approval_fact(env):
    """正例：显式批准事实行落库后，空清单 VERIFY 可关单（且事实可查）。"""
    ts = TaskService()
    tid = await _mk_verify_task(env, criteria=None)
    await record_empty_criteria_approval(
        env["project_id"], tid, COORD, title="VERIFY: 导出"
    )
    assert await has_empty_criteria_approval(env["project_id"], tid) is True
    await ts.close_task(env["project_id"], tid)  # 不再抛
    task = await ts.get_task(env["project_id"], tid)
    assert task["status"] == "closed"


@pytest.mark.asyncio
async def test_nonempty_criteria_verify_closes_normally(env):
    """正例护栏：带清单的 VERIFY 关单不受影响（门只拦空清单）。"""
    ts = TaskService()
    tid = await _mk_verify_task(env)
    await ts.close_task(env["project_id"], tid)
    task = await ts.get_task(env["project_id"], tid)
    assert task["status"] == "closed"


def test_bypass_recipe_removed_from_receipt():
    """「空 criteria 可绕过」的降档配方必须从拒收回执中删除（文本判据铁律）。"""
    msg = format_acceptance_coverage_error([_C1_LABEL])
    assert "为空的任务不受此门影响" not in msg
    assert "acceptance_criteria 为空" not in msg
    # 处方本体仍完整（旧文案测试的判据继续成立）
    assert "acceptanceCoverage" in msg
    assert "test_run" in msg
    assert "waive_attestation" in msg


# ────────────────────────────────────────────────────────────
# P1-2：子门消费 waiver 对齐主门 / ≥N 连拒自动 blocked+external / wakeAt 归一
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_waiver_covers_undeclared_and_unverified_items(env):
    """正例：有效平台 waiver 行兜住未声明/未核验条目（对齐主门短路语义）。"""
    tid = await _mk_verify_task(env)
    await create_waiver(
        env["project_id"], task_id=tid, waived_by=COORD, reason="tool_failure 兜底"
    )
    # 未声明（完全没挂 coverage）——修前 CEO 的 waiver 在子门全无效
    assert await _gaps(env, tid, {"summary": "done"}) == []
    # 未核验（挂了假凭证 id）——waiver 同样接手
    gaps = await _gaps(
        env, tid, {COVERAGE_EVIDENCE_KEY: {"c1": {ATTESTATION_IDS_FIELD: ["ghost"]}}}
    )
    assert gaps == []


@pytest.mark.asyncio
async def test_waiver_does_not_mask_shape_errors(env):
    """反例：豁免**不对形状错生效** —— agent 的输入病必须自己修。"""
    tid = await _mk_verify_task(env)
    await create_waiver(
        env["project_id"], task_id=tid, waived_by=COORD, reason="兜底"
    )
    gaps = await _gaps(
        env, tid, {COVERAGE_EVIDENCE_KEY: {"c1": {"item": ["x"], "note": "y"}}}
    )
    assert gaps, "waiver 不得掩盖形状错"
    assert any("形状错" in g for g in gaps)


@pytest.mark.asyncio
async def test_repeated_gate_rejects_auto_block_external(env):
    """连拒 ≥3 ⇒ 自动 blocked(wait_kind=external)；不足阈值不动。"""
    ts = TaskService()
    tid = await ts.create_task(
        env["project_id"], "实现导出", "d", creator_id=COORD, assignee_id=EXEC,
        dedup_policy="allow",
    )
    await ts.start_task(env["project_id"], tid)  # claimed → running（assign=claim）
    task = {"id": tid, "assignee_id": EXEC}

    note1 = await escalate_if_gate_reject_loop(
        env["project_id"], task, error_text="拒1"
    )
    assert note1 == ""
    note2 = await escalate_if_gate_reject_loop(
        env["project_id"], task, error_text="拒2"
    )
    assert note2 == ""
    cur = await ts.get_task(env["project_id"], tid)
    assert cur["status"] == "running"  # 阈值前不动状态

    note3 = await escalate_if_gate_reject_loop(
        env["project_id"], task, error_text="拒3"
    )
    assert GATE_REJECT_ESCALATE_AFTER == 3
    assert "GATE REJECT ESCALATION" in note3
    cur = await ts.get_task(env["project_id"], tid)
    assert cur["status"] == "blocked"
    assert cur["wait_kind"] == "external"
    assert "GATE REJECT LOOP" in (cur.get("blocked_reason") or "")
    # 触发后计数清零（新一轮从 0 起）
    assert gate_rejection_count(env["project_id"], tid) == 0


@pytest.mark.asyncio
async def test_gate_reject_count_resets_on_submit_success(env):
    """提交成功 ⇒ 连拒计数清零（只有「连续」被拒才累计）。"""
    ts = TaskService()
    tid = await ts.create_task(
        env["project_id"], "实现导出", "d", creator_id=COORD, assignee_id=EXEC,
        dedup_policy="allow",
    )
    await ts.start_task(env["project_id"], tid)
    from hiveweave.services.tasks.gate_reject_guard import (
        register_gate_rejection,
    )

    register_gate_rejection(env["project_id"], tid, "拒1")
    register_gate_rejection(env["project_id"], tid, "拒2")
    assert gate_rejection_count(env["project_id"], tid) == 2
    await ts.submit_task(env["project_id"], tid, {"summary": "ok"})
    assert gate_rejection_count(env["project_id"], tid) == 0


def test_wake_at_empty_string_normalizes_to_none():
    """``wakeAt=''`` 归一 None（空串不是合法时间，不该硬拒）。"""
    p = UpdateTaskStatusParams.model_validate(
        {"taskId": "t", "status": "blocked", "wakeAt": ""}
    )
    assert p.wake_at is None
    p = UpdateTaskStatusParams.model_validate(
        {"taskId": "t", "status": "blocked", "wakeAt": "   "}
    )
    assert p.wake_at is None
    # 非空值不吞（含可解析值原样保留，工具层 _parse_wake_at_ms 兜解析）
    p = UpdateTaskStatusParams.model_validate(
        {"taskId": "t", "status": "blocked", "wakeAt": "2026-10-01T00:00:00Z"}
    )
    assert p.wake_at == "2026-10-01T00:00:00Z"
    assert _parse_wake_at_ms(p.wake_at) is not None
    assert _parse_wake_at_ms("") is None  # 归一后不会走到工具层的硬拒分支
