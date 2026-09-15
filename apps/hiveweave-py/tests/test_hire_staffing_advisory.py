"""件6（dsh42 实证）：hire_agent 回执的待派活维度软提示。

招 6 executor 只有 4 个可做任务位，2 人 Reserve 待命 30min，CEO 点名
「职责重叠」。staffing_advisory（org_invariants，非阻塞）在 open 待派活
任务 < 在编执行者时提示确认扩编成本；任务查询失败 fail-open 跳过。
"""

from __future__ import annotations

from hiveweave.services.org_invariants import staffing_advisory


def _exec(i: int) -> dict:
    return {
        "id": f"exec-{i}",
        "permission_type": "executor",
        "status": "active",
    }


def _task(
    status: str,
    assignee: str = "",
    title: str = "Feature",
    kind: str | None = None,
) -> dict:
    return {
        "id": title + status + assignee,
        "status": status,
        "assignee_id": assignee,
        "title": title,
        "kind": kind,  # #11：VERIFY 判定读 kind，不再读标题
        "is_archived": 0,
    }


def test_open_below_headcount_returns_advisory():
    """open < 在编 → 出提示（含 N < M 数字与 Reserve 成本字样）。"""
    agents = [_exec(i) for i in range(3)]
    tasks = [_task("created"), _task("blocked")]
    note = staffing_advisory(agents=agents, tasks=tasks)
    assert note is not None
    assert "待派活任务 2 < 在编执行者 3" in note
    assert "Reserve 待命成本" in note


def test_open_at_or_above_headcount_no_advisory():
    """open ≥ 在编 → 不出提示（blocked 无主才计入 open）。"""
    agents = [_exec(i) for i in range(2)]
    # 2 open（未 claim created + 无主 blocked）= 2 在编 → 不提示
    tasks2 = [_task("created"), _task("blocked")]
    assert staffing_advisory(agents=agents, tasks=tasks2) is None
    # 3 open（created + created + running 已派不算）≥ 2 在编
    tasks3 = [
        _task("created"),
        _task("created"),
        _task("running", "someone"),
    ]
    assert staffing_advisory(agents=agents, tasks=tasks3) is None


def test_blocked_with_assignee_not_counted_as_open():
    """P2-3（审计）：blocked 已有 assignee 是有主停靠位，不计可派活存量。

    2 在编，仅 1 个真 open（无主 created）→ 提示为 1 < 2（有主 blocked
    不虚增存量）。
    """
    agents = [_exec(i) for i in range(2)]
    tasks = [
        _task("created"),
        _task("blocked", assignee="exec-0"),
        _task("blocked", assignee="exec-1"),
    ]
    note = staffing_advisory(agents=agents, tasks=tasks)
    assert note is not None
    assert "待派活任务 1 < 在编执行者 2" in note


def test_task_query_failure_skips_advisory_fail_open():
    """tasks=None（查询失败 fail-open）→ 跳过提示，不抛。"""
    agents = [_exec(i) for i in range(3)]
    assert staffing_advisory(agents=agents, tasks=None) is None


def test_verify_tasks_not_counted_as_executor_work():
    """VERIFY 是 QA 岗活：不计入 open，也不伪装成「有活可派」——
    2 个 VERIFY 活 vs 2 在编 executor → open=0，提示为 0 < 2。"""
    agents = [_exec(i) for i in range(2)]
    tasks = [
        _task("created", title="VERIFY: UI A", kind="verify"),
        _task("created", title="VERIFY: UI B", kind="verify"),
    ]
    note = staffing_advisory(agents=agents, tasks=tasks)
    assert note is not None
    assert "待派活任务 0 < 在编执行者 2" in note
