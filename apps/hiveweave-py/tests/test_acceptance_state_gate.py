"""#14 验收门状态判据：``acceptance_coverage`` 的 id 声明必须**锚在平台凭证**上。

修前的判据是文本（抄条目原文即"覆盖"、换措辞即"未覆盖"、裸 ``N/A: 理由`` 即豁免）。
本文件把四条验收用例钉在与措辞无关的状态判据上：

① 抄条目原文进 evidence ⇒ 不覆盖；② 换措辞 + 声明 id + **真 test_run 凭证** ⇒ 覆盖；
③ 裸 ``N/A—随便`` ⇒ 不放行（须平台 waiver 行）；④ 阳性对照：全部声明 id 且有凭证 ⇒ 通过。

⚠ 关键一条（计划硬要求）：**声明了 id 但凭证不存在/不属于本任务/kind 不对 ⇒ 不覆盖**
—— 否则 id 声明只是"自述字段"替代文本，等于没修。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services import task as task_module
from hiveweave.services.attestation import attestation_service, create_waiver
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.acceptance import (
    ATTESTATION_IDS_FIELD,
    COVERAGE_EVIDENCE_KEY,
    NOT_APPLICABLE_REASON_FIELD,
    acceptance_coverage_kinds,
    format_acceptance_coverage_error,
    parse_acceptance_plan,
    uncovered_acceptance_items,
    uncovered_acceptance_items_verified,
)

PROJECT_ID = "acc-state-proj"
COORD = "acc-coord"
EXEC = "acc-exec"

# 带显式 id 的条目（dict 形态）+ 无 id 的条目（退化按 1-based 序号）。
CRITERIA = [
    {"id": "c1", "text": "导出 CSV 功能可用", "required": True},
    {"id": "c2", "text": "边界情况有测试覆盖", "required": True},
]


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        att_module._migrated.clear()
        task_module._migrated.clear()
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _mk_task(env, criteria=CRITERIA) -> str:
    ts = TaskService()
    return await ts.create_task(
        env["project_id"],
        "VERIFY: 导出",
        "d",
        creator_id=COORD,
        assignee_id=EXEC,
        acceptance_criteria=criteria,
        # ⚠ P1-7② 之后 `create_task` 有查重门（命中相似 open 任务会**复用**既有 id）。
        # 本文件的用例**刻意**建多条同名任务来验"凭证/豁免是否绑任务"
        # ⇒ 显式声明 `allow`（这正是该出口存在的理由；不声明会把两条用例变成单任务）。
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


# ── 形状：条目 id ──────────────────────────────────────────────


def test_plan_ids_explicit_and_positional():
    plan = parse_acceptance_plan(CRITERIA)
    assert [i.id for i in plan] == ["c1", "c2"]
    # 纯文本清单 → 1-based 序号（与既有「条目N」文案一致）
    plan2 = parse_acceptance_plan(["甲", "乙"])
    assert [i.id for i in plan2] == ["1", "2"]
    # JSON 字符串形态（DB 列直取）
    assert [i.id for i in parse_acceptance_plan('["甲","乙"]')] == ["1", "2"]
    assert parse_acceptance_plan(None) == []


# ── ① 文本不再是判据 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_copied_criteria_text_does_not_cover(env):
    """抄条目原文进 summary ⇒ 两条都未覆盖（修前的假通过）。"""
    tid = await _mk_task(env)
    gaps = await _gaps(
        env, tid,
        {"summary": "导出 CSV 功能可用；边界情况有测试覆盖（照抄原文）"},
    )
    assert len(gaps) == 2, gaps
    assert "条目c1" in gaps[0] and "条目c2" in gaps[1]


@pytest.mark.asyncio
async def test_index_and_bare_na_text_do_not_cover(env):
    """旧的「编号引用 / 裸 N/A: 理由」文本形态 ⇒ 不再覆盖（无需 waiver 的旧出口已死）。"""
    tid = await _mk_task(env)
    for summary in (
        "条目1 实测通过；条目2 N/A: 本环境无边界数据集",
        "item 1 verified; item 2 N/A — no dataset",
        "第1条 通过；第2条 N/A：无数据",
    ):
        gaps = await _gaps(env, tid, {"summary": summary})
        assert len(gaps) == 2, (summary, gaps)


# ── ② / ④ 声明 id + 真凭证 ⇒ 覆盖（阳性对照） ────────────────


@pytest.mark.asyncio
async def test_declared_ids_with_real_credentials_cover(env):
    """换措辞无所谓：只要逐条声明 id 且各锚一条本任务 test_run 凭证 ⇒ 通过。"""
    tid = await _mk_task(env)
    a1 = await _test_run(env, tid)
    a2 = await _test_run(env, tid)
    gaps = await _gaps(
        env, tid,
        {
            "summary": "we exported CSV and covered the edge cases",  # 措辞完全无关
            COVERAGE_EVIDENCE_KEY: {
                "c1": {ATTESTATION_IDS_FIELD: [a1]},
                "c2": {ATTESTATION_IDS_FIELD: [a2]},
            },
        },
    )
    assert gaps == []


@pytest.mark.asyncio
async def test_layer2_payload_list_form_covers(env):
    """list 形态声明（[{"id":…, "attestationIds":…}]）等价可用。"""
    tid = await _mk_task(env)
    a1 = await _test_run(env, tid)
    gaps = await _gaps(
        env, tid,
        {
            "summary": "done",
            COVERAGE_EVIDENCE_KEY: [
                {"id": "c1", ATTESTATION_IDS_FIELD: [a1]},
                {"id": "c2", ATTESTATION_IDS_FIELD: [a1]},
            ],
        },
    )
    assert gaps == []


# ── ⚠ 硬要求：声明必须锚在可核验凭证上 ───────────────────────


@pytest.mark.asyncio
async def test_declared_id_without_credential_does_not_cover(env):
    """声明了 id 但库里的凭证不存在 ⇒ 不覆盖（否则只是"自述字段"）。"""
    tid = await _mk_task(env)
    gaps = await _gaps(
        env, tid,
        {
            "summary": "done",
            COVERAGE_EVIDENCE_KEY: {
                "c1": {ATTESTATION_IDS_FIELD: ["00000000-0000-0000-0000-000000000000"]},
                "c2": {ATTESTATION_IDS_FIELD: ["does-not-exist"]},
            },
        },
    )
    assert len(gaps) == 2, gaps


@pytest.mark.asyncio
async def test_credential_from_other_task_does_not_cover(env):
    """凭证存在但不属于本任务 ⇒ 不覆盖（task 绑定）。"""
    tid = await _mk_task(env)
    other = await _mk_task(env)
    a_other = await _test_run(env, other)
    gaps = await _gaps(
        env, tid,
        {
            "summary": "done",
            COVERAGE_EVIDENCE_KEY: {
                "c1": {ATTESTATION_IDS_FIELD: [a_other]},
                "c2": {ATTESTATION_IDS_FIELD: [a_other]},
            },
        },
    )
    assert len(gaps) == 2, gaps


@pytest.mark.asyncio
async def test_wrong_kind_credential_does_not_cover(env):
    """凭证 kind 不是 test_run（如 browse_e2e）⇒ 不覆盖（kind 绑定）。"""
    tid = await _mk_task(env)
    aid = await attestation_service.create(
        env["project_id"],
        agent_id=EXEC,
        kind="browse_e2e",
        task_id=tid,
        command_or_url="browse http://localhost:3000",
        exit_code=0,
        stdout="ok",
    )
    gaps = await _gaps(
        env, tid,
        {
            "summary": "done",
            COVERAGE_EVIDENCE_KEY: {
                "c1": {ATTESTATION_IDS_FIELD: [aid]},
                "c2": {ATTESTATION_IDS_FIELD: [aid]},
            },
        },
    )
    assert len(gaps) == 2, gaps


@pytest.mark.asyncio
async def test_failed_test_run_credential_does_not_cover(env):
    """exit_code != 0 的 test_run ⇒ 不覆盖（失败运行不解锁）。"""
    tid = await _mk_task(env)
    aid = await _test_run(env, tid, exit_code=1)
    gaps = await _gaps(
        env, tid,
        {
            "summary": "done",
            COVERAGE_EVIDENCE_KEY: {
                "c1": {ATTESTATION_IDS_FIELD: [aid]},
                "c2": {ATTESTATION_IDS_FIELD: [aid]},
            },
        },
    )
    assert len(gaps) == 2, gaps


# ── ③ 不适用条目：权威是平台 waiver 行 ───────────────────────


@pytest.mark.asyncio
async def test_not_applicable_without_platform_waiver_does_not_cover(env):
    """自述 not_applicable_reason（含任意理由）⇒ 不放行（修前的 N/A 自由出口）。"""
    tid = await _mk_task(env)
    gaps = await _gaps(
        env, tid,
        {
            "summary": "done",
            COVERAGE_EVIDENCE_KEY: {
                "c1": {NOT_APPLICABLE_REASON_FIELD: "无所谓随便写"},
                "c2": {NOT_APPLICABLE_REASON_FIELD: "本环境无边界数据集"},
            },
        },
    )
    assert len(gaps) == 2, gaps


@pytest.mark.asyncio
async def test_not_applicable_with_platform_waiver_covers(env):
    """平台 waiver 行（coordinator 签发）批准不适用 ⇒ 覆盖。"""
    tid = await _mk_task(env)
    await create_waiver(
        env["project_id"],
        task_id=tid,
        waived_by=COORD,
        reason="本环境无边界数据集，无测试基建",
    )
    gaps = await _gaps(
        env, tid,
        {
            "summary": "done",
            COVERAGE_EVIDENCE_KEY: {
                "c1": {NOT_APPLICABLE_REASON_FIELD: "无所谓随便写"},
                "c2": {NOT_APPLICABLE_REASON_FIELD: "本环境无边界数据集"},
            },
        },
    )
    assert gaps == []


@pytest.mark.asyncio
async def test_waiver_row_is_task_scoped(env):
    """另一个任务的 waiver 行不豁免本任务（状态判据是任务级绑定）。"""
    tid = await _mk_task(env)
    other = await _mk_task(env)
    await create_waiver(
        env["project_id"], task_id=other, waived_by=COORD, reason="别的任务豁免"
    )
    gaps = await _gaps(
        env, tid,
        {
            "summary": "done",
            COVERAGE_EVIDENCE_KEY: {
                "c1": {NOT_APPLICABLE_REASON_FIELD: "不适用"},
                "c2": {NOT_APPLICABLE_REASON_FIELD: "不适用"},
            },
        },
    )
    assert len(gaps) == 2, gaps


# ── 无效声明 / 同步形态 fail-closed ──────────────────────────


@pytest.mark.asyncio
async def test_verdict_is_language_invariant(env):
    """判据与语言无关：同一结构 + 中/法/西理由文本 ⇒ 结论逐字相同（#14 反措辞）。"""
    tid = await _mk_task(env)
    a1 = await _test_run(env, tid)
    results = []
    for reason in (
        "本环境无边界数据集",
        "aucun jeu de données de bord disponible",
        "no hay datos de borde disponibles",
        "「不做判断」中性写法",
        # 变体含「照抄条目原文」——文本判据会把它当成 c2 已覆盖（对照用）
        "边界情况有测试覆盖",
    ):
        gaps = await _gaps(
            env, tid,
            {
                "summary": reason,
                COVERAGE_EVIDENCE_KEY: {
                    "c1": {ATTESTATION_IDS_FIELD: [a1]},
                    "c2": {NOT_APPLICABLE_REASON_FIELD: reason},
                },
            },
        )
        results.append(gaps)
    # 无 waiver：c2 一律未覆盖，且缺口文案与语言无关
    assert all(r == ["条目c2: 边界情况有测试覆盖"] for r in results), results


@pytest.mark.asyncio
async def test_unknown_id_declaration_is_named(env):
    """id 由平台侧确定：声明不存在的 id ⇒ 点名无效声明（不是静默放行）。"""
    tid = await _mk_task(env)
    a1 = await _test_run(env, tid)
    gaps = await _gaps(
        env, tid,
        {
            "summary": "done",
            COVERAGE_EVIDENCE_KEY: {
                "c1": {ATTESTATION_IDS_FIELD: [a1]},
                "c2": {ATTESTATION_IDS_FIELD: [a1]},
                "条目9": {ATTESTATION_IDS_FIELD: [a1]},
            },
        },
    )
    # c1/c2 已覆盖，但无效声明必须显式点名
    assert len(gaps) == 1 and "无效覆盖声明" in gaps[0], gaps


def test_sync_form_fails_closed_without_verification():
    """同步形态不做核验 ⇒ 未传 verified_ids 时一律判未覆盖（fail-closed），

    并**自诊断点名接线故障**（否则生产上会表现为"所有条目都没写清"的误导）。
    """
    ev = {
        "summary": "done",
        COVERAGE_EVIDENCE_KEY: {"c1": {ATTESTATION_IDS_FIELD: ["x"]}},
    }
    one = CRITERIA[:1]
    gaps = uncovered_acceptance_items(one, ev)
    assert len(gaps) == 2, gaps
    assert gaps[0].startswith("条目c1"), gaps
    assert "未接线" in gaps[1], gaps
    # 传了核验结果 ⇒ 覆盖，且不再有接线诊断
    assert uncovered_acceptance_items(one, ev, verified_ids={"x"}) == []


def test_error_message_names_ids_and_prescribes_state_path():
    msg = format_acceptance_coverage_error(["条目c1: 导出 CSV 功能可用"])
    # 2026-09-19 接线修复：处方指向 submit_task 的 acceptanceCoverage 参数
    assert "acceptanceCoverage" in msg
    assert "test_run" in msg
    assert "waive_attestation" in msg
    assert "N/A" in msg  # 说明裸 N/A 不再放行（处方仍点名它）


# ── kinds：按任务 policy 取（#14 的另一半 = 真交付被误拒） ──────


@pytest.mark.asyncio
async def test_coverage_kinds_follow_task_policy():
    """policy 明确要求执行类 kind ⇒ 用它；soft/未知 ⇒ 全部执行类 kind + 留痕。"""
    from structlog.testing import capture_logs

    assert await acceptance_coverage_kinds({"policy_id": "generic_tests"}) == (
        "test_run",
    )
    assert await acceptance_coverage_kinds({"policy_id": "docs_only"}) == (
        "doc_review",
    )
    assert await acceptance_coverage_kinds({"policy_id": "ui_browser_e2e"}) == (
        "browse_e2e",
    )
    soft = await acceptance_coverage_kinds({"policy_id": "coordinator_review"})
    assert set(soft) == {"test_run", "browse_e2e", "visual_check", "doc_review"}
    # fail-loud：soft 用 info、不可用/未知用 warning —— 都不静默
    with capture_logs() as logs:
        await acceptance_coverage_kinds({"policy_id": "coordinator_review"})
    assert any(
        str(x.get("event")) == "acceptance_coverage_policy_soft_all_execution_kinds"
        for x in logs
    ), logs
    with capture_logs() as logs2:
        await acceptance_coverage_kinds({"policy_id": "code_audit"})  # 非执行类
    assert any(
        str(x.get("event"))
        == "acceptance_coverage_policy_kinds_not_execution_evidence"
        for x in logs2
    ), logs2


@pytest.mark.asyncio
async def test_browse_e2e_covers_when_kinds_allow(env):
    """UI 类条目能被 browse_e2e 凭证覆盖（kinds 放行 browse_e2e）⇒ 不误拒。"""
    tid = await _mk_task(env, criteria=[{"id": "ui", "text": "登录页可点通"}])
    aid = await attestation_service.create(
        env["project_id"], agent_id=EXEC, kind="browse_e2e", task_id=tid,
        command_or_url="browse http://localhost:3000", exit_code=0, stdout="ok",
    )
    gaps = await uncovered_acceptance_items_verified(
        env["project_id"], tid,
        [{"id": "ui", "text": "登录页可点通"}],
        {"summary": "done",
         COVERAGE_EVIDENCE_KEY: {"ui": {ATTESTATION_IDS_FIELD: [aid]}}},
        expected_agent_id=EXEC,
        kinds=("browse_e2e",),
    )
    assert gaps == []
    # 同一份 evidence：kinds 只认 test_run ⇒ 该条未覆盖（kinds 真的被判据消费）
    gaps2 = await uncovered_acceptance_items_verified(
        env["project_id"], tid,
        [{"id": "ui", "text": "登录页可点通"}],
        {"summary": "done",
         COVERAGE_EVIDENCE_KEY: {"ui": {ATTESTATION_IDS_FIELD: [aid]}}},
        expected_agent_id=EXEC,
        kinds=("test_run",),
    )
    assert len(gaps2) == 1 and "条目ui" in gaps2[0], gaps2
