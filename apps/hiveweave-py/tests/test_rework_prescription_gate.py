"""处方门禁下沉回归（fixlist #3 + #8，2026-09-11）。

DSH 判据 ``packages/AGENTS.md:14``「**Enforce a decision in the operation
that makes it**」：门禁必须住在做那个决定的操作里；facade / wrapper /
listener 顺序都不算 enforcement，因为旁路调用方能绕开。

旧实现把处方检查放在 tool 层的 ``decision == "rework"`` 分支上，于是
``decision='approve'`` 被证据闸强制转 rework 的路径**整条绕开它** ——
fixlist #3 实测净状态恰是门禁要防的「无处方的返修」。

本文件锁两条性质：

    A. ``_force_rework`` 是两条返修路径的唯一汇聚点；判定发生在**状态变更
       之前**，缺处方即抛 ``ReworkPrescriptionAbsent`` 且任务停在原状态。
    B. 处方可以是**结构化**的（``prescription_kind`` 闭合词表）—— 让写不出
       文件路径的 QA/VERIFY 类返修有正规通道（fixlist #8 的误伤）。
"""

from __future__ import annotations

import pytest

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401
from hiveweave.services.worktree_review import (
    REWORK_PRESCRIPTION_KINDS,
    rework_feedback_missing_prescription,
    rework_prescription_problem,
)


# ── 判据矩阵（纯函数）──────────────────────────────────────────


@pytest.mark.parametrize(
    "feedback,kind,expected",
    [
        # 空 feedback
        (None, None, "feedback_empty"),
        ("   ", None, "feedback_empty"),
        # 有 feedback 但无任何处方形式
        ("fix it", None, "feedback_without_prescription"),
        ("needs rework", None, "feedback_without_prescription"),
        # 旧判据覆盖的两条正路（回归保护：不许收窄）
        ("change src/main.py line 3", None, None),
        ("please rerun tests/test_main.py", None, None),
        ("filesChanged=[a/b.py]", None, None),
        # 新增的结构化正路：写不出路径的返修类型
        ("补 test_run 凭证", "missing-evidence", None),
        ("状态没对上", "state-mismatch", None),
        ("违反验收条款 3", "clause-violation", None),
        ("参数非法", "param-invalid", None),
        ("改文件", "path-change", None),
        # 未知类别要被挡（闭合词表口径）
        ("补凭证", "bogus-kind", "unknown_prescription_kind:bogus-kind"),
        # 但 kind 写错**不该否决**一个本来合格的返修（审计 L1）：三条正路是
        # 「或」，未登记的 kind 只是拿不到第 1 条，仍回落看 feedback。
        ("change src/main.py line 3", "bogus-kind", None),
        ("change src/main.py line 3", "PathChange", None),
        # 空白 kind 视为未声明 → 回落到文本判据
        ("fix it", "   ", "feedback_without_prescription"),
    ],
)
def test_prescription_problem_matrix(feedback, kind, expected):
    assert rework_prescription_problem(feedback, kind) == expected


def test_all_documented_kinds_are_accepted():
    """词表里的每个类别都必须真的被接受（防止词表与判据漂移）。"""
    for kind in REWORK_PRESCRIPTION_KINDS:
        assert rework_prescription_problem("whatever", kind) is None


def test_legacy_bool_wrapper_stays_equivalent():
    """旧布尔壳是向后兼容接口，语义必须与新函数一致。"""
    for feedback, want_missing in [
        (None, True),
        ("fix it", True),
        ("change src/main.py", False),
        ("filesChanged=[x]", False),
    ]:
        assert rework_feedback_missing_prescription(feedback) is want_missing
        assert (
            rework_prescription_problem(feedback) is not None
        ) is want_missing


# ── service 层：#3 绕过路径 ────────────────────────────────────


async def _mk_reviewing(task_env, evidence: dict | None = None):
    """建任务 → claim → start → submit → reviewing，返回 (ts, pid, tid)。

    ``evidence`` 走**公开路径**（submit_task）注入，而不是直写 SQL —— 这样
    测试自己就证明了输入状态可达（审计 L7）。
    """
    from hiveweave.services.task import TaskService

    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        project_id=pid,
        title="prescription gate",
        description="d",
        creator_id="creator-1",
        assignee_id="assignee-1",
    )
    await ts.claim_task(pid, tid, "assignee-1")
    await ts.start_task(pid, tid)
    await ts.submit_task(pid, tid, evidence=evidence or {"summary": "done"})
    await ts.start_review(pid, tid, reviewer_id="reviewer-1")
    return ts, pid, tid


@pytest.mark.asyncio
async def test_approve_forced_rework_without_evidence_is_rejected(task_env):
    """#3 核心：approve 被证据闸强制转 rework，却拿不出证据条目 → 拒。

    这条正是旧实现在 tool 层绕不过去、而在 service 层必须挡住的路径：
    ``verdict == "FAIL"`` 的触发条件**并不要求** blocking_issues 非空，
    所以「系统返修却没有处方」是真实可达状态。
    """
    from hiveweave.services.tasks.review import ReworkPrescriptionAbsent

    ts, pid, tid = await _mk_reviewing(task_env, evidence={"verdict": "FAIL"})  # 无 blocking_issues

    with pytest.raises(ReworkPrescriptionAbsent) as exc:
        await ts.review_task(pid, tid, "approve")

    assert exc.value.problem == "system_rework_without_evidence"
    # 判定在状态变更之前 —— 任务必须留在 reviewing
    assert (await ts.get_task(pid, tid))["status"] == "reviewing"


@pytest.mark.asyncio
async def test_approve_forced_rework_with_evidence_passes(task_env):
    """对照：系统返修**有**证据条目（=处方）时照常强制返修。"""
    ts, pid, tid = await _mk_reviewing(
        task_env,
        evidence={"verdict": "FAIL", "blocking_issues": ["src/a.py 缺 X 分支"]},
    )

    await ts.review_task(pid, tid, "approve")

    assert (await ts.get_task(pid, tid))["status"] == "running"


@pytest.mark.asyncio
async def test_explicit_rework_without_prescription_rejected_at_service(task_env):
    """显式 rework 无处方：service 层同样拒（tool 层只是早拒，不是唯一门）。"""
    from hiveweave.services.tasks.review import ReworkPrescriptionAbsent

    ts, pid, tid = await _mk_reviewing(task_env)

    with pytest.raises(ReworkPrescriptionAbsent) as exc:
        await ts.review_task(pid, tid, "rework", feedback="fix it")

    assert exc.value.problem == "feedback_without_prescription"
    assert (await ts.get_task(pid, tid))["status"] == "reviewing"


@pytest.mark.asyncio
async def test_explicit_rework_with_kind_passes(task_env):
    """#8：写不出路径的返修（补证据类）用结构化类别即可通过。"""
    ts, pid, tid = await _mk_reviewing(task_env)

    await ts.review_task(
        pid, tid, "rework",
        feedback="请补 test_run 凭证后重交",
        prescription_kind="missing-evidence",
    )

    assert (await ts.get_task(pid, tid))["status"] == "running"


@pytest.mark.asyncio
async def test_explicit_rework_unknown_kind_rejected(task_env):
    """闭合词表：未登记的类别不是处方。"""
    from hiveweave.services.tasks.review import ReworkPrescriptionAbsent

    ts, pid, tid = await _mk_reviewing(task_env)

    with pytest.raises(ReworkPrescriptionAbsent) as exc:
        await ts.review_task(
            pid, tid, "rework", feedback="补凭证",
            prescription_kind="whatever",
        )

    assert exc.value.problem == "unknown_prescription_kind:whatever"
    assert (await ts.get_task(pid, tid))["status"] == "reviewing"


@pytest.mark.asyncio
async def test_explicit_rework_with_path_still_passes(task_env):
    """回归保护：既有正路（写文件路径）不许被新门禁挡掉。"""
    ts, pid, tid = await _mk_reviewing(task_env)

    await ts.review_task(
        pid, tid, "rework", feedback="src/main.py:3 逻辑反了"
    )

    assert (await ts.get_task(pid, tid))["status"] == "running"


# ── tool 层：结构化字段接线 ────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_accepts_prescription_kind(task_env, monkeypatch):
    """工具层把 prescriptionKind 一路传到 service（字段接线回归）。

    新工具字段三件套（本项目坑）：@tool 注册 + 参数 schema + pydantic
    canonical camelCase alias —— 缺 alias 会让 LLM 照 schema 首调必失败。
    """
    from hiveweave.services import task as task_mod
    from hiveweave.tools.tasks.review import review_task_tool

    ts, pid, tid = await _mk_reviewing(task_env)
    captured: dict = {}

    orig = task_mod.TaskService.review_task

    async def _spy(self, project_id, task_id, decision, feedback=None,
                   reviewer_id=None, **kw):
        captured["decision"] = decision
        captured["kind"] = kw.get("prescription_kind")
        return await orig(
            self, project_id, task_id, decision, feedback,
            reviewer_id=reviewer_id, **kw
        )

    monkeypatch.setattr(task_mod.TaskService, "review_task", _spy)

    async def _fake_pid(agent_id: str):
        return pid

    monkeypatch.setattr("hiveweave.tools.helpers.get_project_id", _fake_pid)

    async def _fake_agent_project(agent_id: str):
        return (
            pid if agent_id in ("assignee-1", "reviewer-1", "creator-1")
            else None
        )

    monkeypatch.setattr(
        "hiveweave.db.meta.get_agent_project_id", _fake_agent_project
    )

    from hiveweave.tools.tasks.review import ReviewTaskParams

    # camelCase alias 必须能解析（LLM 照 schema 调用的真实形状）
    params = ReviewTaskParams(
        taskId=tid, decision="rework",
        feedback="补 test_run 凭证", prescriptionKind="missing-evidence",
    )
    assert params.prescription_kind == "missing-evidence"

    # M1 回归（审计发现）：字段必须出现在**喂给 LLM 的那份 schema** 里。
    # 手写 `TOOL_PARAM_SCHEMAS` 优先于 pydantic 反射 —— 只补 pydantic 层
    # 会让模型根本不知道能传这个参数，被拒一次才知道，等于又加一轮往返。
    from hiveweave.tools.executor import get_tool_schema_for_llm

    llm_schema = get_tool_schema_for_llm("review_task")
    props = llm_schema.get("properties") or {}
    assert "prescriptionKind" in props, (
        "prescriptionKind 必须登记进 TOOL_PARAM_SCHEMAS，否则 LLM 看不到"
    )
    assert "missing-evidence" in (props["prescriptionKind"].get("enum") or [])

    result = await review_task_tool(
        params, agent_id="reviewer-1", workspace=task_env["workspace"],
    )

    assert result.success is True, result.error
    assert captured["kind"] == "missing-evidence"
    assert (await ts.get_task(pid, tid))["status"] == "running"
