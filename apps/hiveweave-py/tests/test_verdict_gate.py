"""E1/E2 复盘验收：终验 evidence verdict 强制 schema + FAIL 强制 rework 路由。

E1: submit 对 VERIFY/milestoneVerify 任务硬校验 verdict∈{PASS,FAIL}，
    FAIL 必须带非空 blocking_issues，缺失/非法 → ValueError（transition 前）。
E2: approve 时若 verdict==FAIL → 强制走 rework（reviewing→rework→running），
    并复用既有 rework 收尾 + 把 blocking_issues 附进 assignee 通知；
    PASS / 无 verdict（存量）按原行为放行。
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


async def _mk_verify_running(ts: TaskService, pid: str) -> str:
    """建一条已 running 的终验（VERIFY）任务（assign=claim 后 start）。"""
    verify_id = await ts.create_task(
        pid,
        "VERIFY: UI",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        tags=["verify", "mandatory"],
        source="system",
    )
    # 单测会在同一 project DB 里并行建多条 VERIFY 行——绕过单飞串行化锁
    # （平台运行时由 _nudge_one_verify_task 持锁 claim，测试不走该路径）。
    await ts.claim_task(pid, verify_id, EXEC, bypass_verify_serialize=True)
    await ts.start_task(pid, verify_id)
    assert (await ts.get_task(pid, verify_id))["status"] == "running"
    return verify_id


# ── E1 submit verdict schema ────────────────────────────────


@pytest.mark.asyncio
async def test_verify_submit_rejects_missing_verdict(task_env):
    """E1-①无 verdict → 硬拒，错误文案点名缺 verdict。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running(ts, pid)
    with pytest.raises(ValueError) as ei:
        await ts.submit_task(pid, vid, evidence={"tests_passed": True})
    assert "verdict" in str(ei.value)
    # transition 之前拦截 → 状态未被推进
    assert (await ts.get_task(pid, vid))["status"] == "running"


@pytest.mark.asyncio
async def test_verify_submit_rejects_fail_without_blocking_issues(task_env):
    """E1-②verdict=FAIL 但 blocking_issues 缺失/为空 → 硬拒，点名 blocking_issues。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running(ts, pid)
    with pytest.raises(ValueError) as ei:
        await ts.submit_task(pid, vid, evidence={"verdict": "FAIL"})
    assert "blocking_issues" in str(ei.value)

    with pytest.raises(ValueError) as ei2:
        await ts.submit_task(pid, vid, evidence={"verdict": "FAIL", "blocking_issues": []})
    assert "blocking_issues" in str(ei2.value)


@pytest.mark.asyncio
async def test_verify_submit_rejects_invalid_verdict(task_env):
    """补充：verdict 取值非法（非 PASS/FAIL）同样被拒并点名。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running(ts, pid)
    with pytest.raises(ValueError) as ei:
        await ts.submit_task(pid, vid, evidence={"verdict": "PENDING"})
    assert "verdict" in str(ei.value)


@pytest.mark.asyncio
async def test_verify_submit_rejects_non_dict_evidence(task_env):
    """E1-审计：evidence 非 dict（str/None）→ 硬拒。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running(ts, pid)
    with pytest.raises(ValueError) as ei:
        await ts.submit_task(pid, vid, evidence="not-a-dict")  # type: ignore[arg-type]
    assert "dict" in str(ei.value)
    assert (await ts.get_task(pid, vid))["status"] == "running"
    with pytest.raises(ValueError):
        await ts.submit_task(pid, vid, evidence=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_verify_submit_accepts_lowercase_verdict(task_env):
    """E1-审计归一：verdict 大小写不敏感（fail/pass 归一后判定）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running(ts, pid)
    await ts.submit_task(pid, vid, evidence={"verdict": "pass", "done": True})
    assert (await ts.get_task(pid, vid))["status"] == "submitted"

    vid2 = await _mk_verify_running(ts, pid)
    await ts.submit_task(
        pid, vid2, evidence={"verdict": "FAIL", "blocking_issues": ["f"]}
    )
    assert (await ts.get_task(pid, vid2))["status"] == "submitted"


# ── G1 审计：工具层 verdict 通道（submit_task_tool → evidence）───


@pytest.mark.asyncio
async def test_submit_tool_carries_verdict_into_evidence(task_env):
    """submit_task_tool 带 verdict/blockingIssues → service 收到含 verdict 的 evidence。"""
    from hiveweave.tools.task_tools import SubmitTaskParams, submit_task_tool

    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running(ts, pid)

    from pathlib import Path

    (Path(task_env["workspace"]) / "a.py").write_text("x = 1\n")

    captured: dict = {}

    async def _capture_submit(self, p, tid, evidence):
        captured["evidence"] = dict(evidence)

    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=pid),
        ),
        patch(
            "hiveweave.db.meta.get_agent_project_id",
            new=AsyncMock(return_value=pid),
        ),
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            return_value=frozenset(),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.find_recent_for_agent",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.verify_ids",
            new=AsyncMock(return_value=(True, "")),
        ),
        patch(
            "hiveweave.services.task.TaskService.submit_task",
            new=_capture_submit,
        ),
    ):
        params = SubmitTaskParams(
            taskId=vid,
            summary="终验完成",
            testsPassed=True,
            verdict="FAIL",
            blockingIssues=["/_admin 全线 404"],
            filesChanged=["a.py"],
        )
        result = await submit_task_tool(params, EXEC, task_env["workspace"])

    assert result.success is True, result.error
    assert captured["evidence"].get("verdict") == "FAIL"
    assert captured["evidence"].get("blocking_issues") == ["/_admin 全线 404"]


@pytest.mark.asyncio
async def test_verify_submit_accepts_valid_verdict(task_env):
    """E1-③合法 evidence 通过：PASS（无 blocking_issues）与 FAIL（带清单）均可提交。"""
    ts = TaskService()
    pid = task_env["project_id"]

    vid_pass = await _mk_verify_running(ts, pid)
    await ts.submit_task(pid, vid_pass, evidence={"verdict": "PASS", "tests_passed": True})
    assert (await ts.get_task(pid, vid_pass))["status"] == "submitted"

    vid_fail = await _mk_verify_running(ts, pid)
    await ts.submit_task(
        pid,
        vid_fail,
        evidence={"verdict": "FAIL", "blocking_issues": ["UI 未按线框图渲染"]},
    )
    assert (await ts.get_task(pid, vid_fail))["status"] == "submitted"


@pytest.mark.asyncio
async def test_non_verify_submit_not_validated(task_env):
    """非终验任务：无 verdict 也按现状放行（不校验）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(pid, tid, evidence={"summary": "done"})
    assert (await ts.get_task(pid, tid))["status"] == "submitted"


# ── E2 FAIL 强制 rework 路由 ────────────────────────────────


@pytest.mark.asyncio
async def test_fail_verdict_approve_routes_rework_and_notifies(task_env):
    """E2-①verdict=FAIL 的 approve → 状态转 rework→running + assignee 通知含 blocking_issues。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running(ts, pid)
    issues = ["UI 渲染不符合线框图", "空态未处理"]
    await ts.submit_task(
        pid, vid, evidence={"verdict": "FAIL", "blocking_issues": issues}
    )
    await ts.start_review(pid, vid)

    append = AsyncMock()
    with patch("hiveweave.services.work_log.WorkLogService.append_log", append):
        await ts.review_task(pid, vid, "approve", feedback="验收不合格，请返工")

    after = await ts.get_task(pid, vid)
    # reviewing → rework → running（复用 rework 状态机，非 close/approved）
    assert after["status"] == "running"
    assert after["status"] not in ("approved", "closed")

    # reason_code=verdict_fail_rework 落地到 task_events
    evs = await task_module._query(
        pid,
        "SELECT event_type, to_status, payload FROM task_events "
        "WHERE task_id = ? ORDER BY created_at DESC",
        [vid],
    )
    assert any("verdict_fail_rework" in (r["payload"] or "") for r in evs)

    # assignee 收到通知且带 blocking_issues 内容
    summaries = [c.kwargs.get("summary") or "" for c in append.await_args_list]
    assert any("blocking_issues" in s and issues[0] in s for s in summaries), summaries


@pytest.mark.asyncio
async def test_pass_verdict_approve_normal_approve_close(task_env):
    """E2-②verdict=PASS 的 approve → 正常 approved，终验任务走自有 close。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running(ts, pid)
    await ts.submit_task(pid, vid, evidence={"verdict": "PASS", "tests_passed": True})
    await ts.start_review(pid, vid)
    await ts.review_task(pid, vid, "approve")
    after = await ts.get_task(pid, vid)
    assert after["status"] in ("approved", "closed")
    assert after["status"] != "running"


@pytest.mark.asyncio
async def test_legacy_no_verdict_approve_normal(task_env):
    """E2-③存量数据（无 verdict）→ 不触发路由，按现状正常 approve。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running(ts, pid)
    # 模拟已处于 reviewing 的存量终验行（无 verdict 字段，绕过 E1 submit 门）
    now_ms = int(time.time() * 1000)
    await task_module._execute(
        pid,
        "UPDATE tasks SET status = 'reviewing', evidence = ?, updated_at = ? "
        "WHERE id = ?",
        [json.dumps({"tests_passed": True}), now_ms, vid],
    )
    await ts.review_task(pid, vid, "approve")
    after = await ts.get_task(pid, vid)
    assert after["status"] in ("approved", "closed")
    assert after["status"] != "running"


# ── E5 断流收口纪律（submit 侧）：降级中提交 FAIL 终验被拒 ──────────


async def _mk_verify_running_for(ts: TaskService, pid: str, agent: str) -> str:
    """同 _mk_verify_running，但 assignee 可指定（E5 降级判定按 assignee）。"""
    verify_id = await ts.create_task(
        pid,
        "VERIFY: UI",
        "verify",
        creator_id=COORD,
        assignee_id=agent,
        tags=["verify", "mandatory"],
        source="system",
    )
    await ts.claim_task(pid, verify_id, agent, bypass_verify_serialize=True)
    await ts.start_task(pid, verify_id)
    assert (await ts.get_task(pid, verify_id))["status"] == "running"
    return verify_id


@pytest.mark.asyncio
async def test_fail_submit_rejected_when_assignee_degraded(task_env):
    """E5-④降级中提交 verdict=FAIL → 硬拒（禁止就地收口），状态不推进。"""
    from hiveweave.agents.recovery import clear_degraded, mark_degraded

    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running_for(ts, pid, EXEC)
    mark_degraded(EXEC)
    try:
        with pytest.raises(ValueError) as ei:
            await ts.submit_task(
                pid,
                vid,
                evidence={"verdict": "FAIL", "blocking_issues": ["/_admin 404"]},
            )
        assert "degraded" in str(ei.value)
    finally:
        clear_degraded(EXEC)
    # transition 之前拦截 → 状态未被推进
    assert (await ts.get_task(pid, vid))["status"] == "running"


@pytest.mark.asyncio
async def test_pass_submit_allowed_when_assignee_degraded(task_env):
    """E5-⑤降级中提交 verdict=PASS（真实续跑完成）→ 不受 E5 影响。"""
    from hiveweave.agents.recovery import clear_degraded, mark_degraded

    ts = TaskService()
    pid = task_env["project_id"]
    vid = await _mk_verify_running_for(ts, pid, EXEC)
    mark_degraded(EXEC)
    try:
        await ts.submit_task(
            pid, vid, evidence={"verdict": "PASS", "tests_passed": True}
        )
    finally:
        clear_degraded(EXEC)
    assert (await ts.get_task(pid, vid))["status"] == "submitted"