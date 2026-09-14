"""审计 epic（42 轮双项目报告 P0 四条边）回归测试。

覆盖：
- P0-1 任务 A：audit_cache 新列（top_issues / source_attestation_id）+
  缓存命中回放原结论（回执不再「ISSUES / 0 行 / 0 问题」自相矛盾）。
- P0-2 任务 B：审计 prompt 注入任务验收标准节 + finding 级申诉（appeal_notes）。
- P1-4 任务 C：llm_failed 自动入队（退避 2^n*60s±jitter）→ 重跑成功 →
  收件箱通知；diff 变化作废；attempts 耗尽 → exhausted。
- P0-3 任务 D：waiver 按原因分流（tool_failure 不失审批权 + 审批自动路由；
  quality 连坐回归）。
- 任务 E：identity / executor / coordinator prompt 与工具回执文案
  「llm_failed 教 waive」→「平台自动排队重试，等待即可」。

DB 环境 fixture 照 test_attestation_waiver.py（tempdir per-project DB +
patch meta.get_project_workspace）。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services import audit_retry as retry_module
from hiveweave.services.attestation import (
    attestation_service,
    create_waiver,
    get_valid_waiver,
)

PROJECT_ID = "audit-epic-project"
AGENT_ID = "epic-executor"
HEAD_SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"

DIFF_TEXT = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        att_module._migrated.clear()
        retry_module._retry_migrated.clear()

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            yield {"workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _fake_git():
    """Async fake for ``hiveweave.services.git_worktree._git``：固定非空 diff。"""

    async def fake_git(args: list[str], cwd: str, timeout: float = 30.0):
        table = {
            ("rev-parse", "--verify", "refs/heads/main"): (True, ""),
            ("diff", "main...HEAD"): (True, DIFF_TEXT),
            ("diff", "HEAD"): (True, ""),
            ("ls-files", "--others", "--exclude-standard"): (True, ""),
            ("rev-parse", "HEAD"): (True, HEAD_SHA),
        }
        return table.get(tuple(args), (False, ""))

    return fake_git


def _run_audit_patches():
    """run_code_audit 的 worktree / git / 报告文件打桩（返回未进入的 ctx）。"""
    return (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_fake_git()),
        patch(
            "hiveweave.tools.executor.ToolExecutor._save_tool_output_file",
            return_value=r"C:\fake\report.txt",
        ),
    )


async def _fetch_retry_row(conn, agent_id: str) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM audit_retry WHERE agent_id = ?", [agent_id]
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


# ── 任务 A：缓存命中回放原结论 ───────────────────────────────────


@pytest.mark.asyncio
async def test_audit_cache_store_persists_new_columns(env):
    """audit_cache 新列 top_issues / source_attestation_id 落库可读回。"""
    await attestation_service.audit_cache_store(
        PROJECT_ID,
        agent_id=AGENT_ID,
        diff_hash="abcd1234abcd1234",
        verdict="ISSUES",
        exit_code=1,
        attestation_id="att-src-1",
        top_issues=["x.py:12 [high] crash", "y.py:3 [low] nit"],
        source_attestation_id="att-src-1",
    )
    row = await attestation_service.audit_cache_lookup(
        PROJECT_ID, agent_id=AGENT_ID, diff_hash="abcd1234abcd1234"
    )
    assert row is not None
    assert row["verdict"] == "ISSUES"
    assert row["exit_code"] == 1
    assert json.loads(row["top_issues"]) == [
        "x.py:12 [high] crash", "y.py:3 [low] nit",
    ]
    assert row["source_attestation_id"] == "att-src-1"


@pytest.mark.asyncio
async def test_cached_hit_replays_original_verdict(env):
    """缓存命中回放 issues/top_issues/原凭证 id——回执不再 0 行/0 问题。"""
    from hiveweave.services.code_audit import (
        record_change,
        reset_ledger,
        run_code_audit,
    )
    from hiveweave.tools.code_audit import _format_verdict

    llm_text = (
        "VERDICT: ISSUES\n"
        "- x.py:1 [high] admin token hardcoded but spec requires it\n"
        "- y.py:9 [low] style\n"
    )

    reset_ledger(AGENT_ID)
    record_change(AGENT_ID, 30)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        first = await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-a",
            call_llm=AsyncMock(return_value=llm_text),
        )
    assert first["audited"] is True
    assert first["verdict"] == "ISSUES"
    assert first["issues_count"] == 2
    src_att = first["attestation_id"]
    reset_ledger(AGENT_ID)

    # 第二次：同 diff → 缓存命中（不烧 LLM）
    record_change(AGENT_ID, 12)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        second = await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-a",
            call_llm=AsyncMock(return_value="SHOULD NOT BE CALLED"),
        )
    reset_ledger(AGENT_ID)

    assert second["audited"] is True
    assert second["verdict"] == "ISSUES"
    # P0-1 核心：不再 0/0
    assert second["issues_count"] == 2
    assert second["top_issues"][0].startswith("x.py:1")
    assert second["lines_audited"] == 12
    assert second["cached_from_attestation_id"] == src_att
    assert second["attestation_id"] != src_att  # 仍发新凭证

    # 工具回执：缓存复用语义 + 原凭证 id + 问题清单齐全
    receipt = _format_verdict(second).output or ""
    assert "缓存复用自凭证" in receipt
    assert src_att in receipt
    assert "重审无意义" in receipt
    assert "问题数: 2" in receipt
    assert "审计行数: 12" in receipt


@pytest.mark.asyncio
async def test_cached_hit_pass_replays_lines_and_message(env):
    """缓存命中 PASS：行数回放台账值，message 透出到回执（不再被丢弃）。"""
    from hiveweave.services.code_audit import (
        record_change,
        reset_ledger,
        run_code_audit,
    )
    from hiveweave.tools.code_audit import _format_verdict

    reset_ledger(AGENT_ID)
    record_change(AGENT_ID, 22)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        first = await run_code_audit(
            PROJECT_ID, AGENT_ID,
            call_llm=AsyncMock(return_value="VERDICT: PASS\n"),
        )
    assert first["verdict"] == "PASS"
    reset_ledger(AGENT_ID)

    record_change(AGENT_ID, 7)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        second = await run_code_audit(
            PROJECT_ID, AGENT_ID,
            call_llm=AsyncMock(return_value="SHOULD NOT BE CALLED"),
        )
    reset_ledger(AGENT_ID)

    assert second["verdict"] == "PASS"
    assert second["lines_audited"] == 7
    receipt = _format_verdict(second).output or ""
    assert "审计行数: 7" in receipt
    assert second["message"][:11] in receipt  # 缓存命中的 message 透出


# ── 任务 B：prompt 验收标准 + finding 级申诉 ─────────────────────


def test_build_audit_prompt_sections():
    from hiveweave.services.code_audit import build_audit_prompt

    system, user = build_audit_prompt(
        "DIFF_BODY", "task-9",
        acceptance_criteria=["默认 admin token 为规格要求", "响应 < 200ms"],
        appeal_notes="规格 3.2 节明确要求默认 admin token（原文附后）",
    )
    assert "acceptance criteria" in system
    assert "任务验收标准" in user
    assert "不是缺陷" in user
    assert "默认 admin token 为规格要求" in user
    assert "作者申诉" in user
    assert "不因申诉自动放行" in user
    assert "规格 3.2 节" in user
    assert "DIFF_BODY" in user
    assert "task-9" in user

    # 取不到就跳过该节
    _system2, user2 = build_audit_prompt("D", None, None, None)
    assert "任务验收标准" not in user2
    assert "作者申诉" not in user2


@pytest.mark.asyncio
async def test_load_task_acceptance_criteria_fail_open(env):
    from hiveweave.services.code_audit import load_task_acceptance_criteria

    assert await load_task_acceptance_criteria(PROJECT_ID, None) is None
    assert await load_task_acceptance_criteria(
        PROJECT_ID, "nonexistent-task"
    ) is None


@pytest.mark.asyncio
async def test_run_code_audit_injects_criteria_and_appeal(env):
    """run_code_audit 把验收标准 + appeal_notes 注入审计 prompt。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    captured: dict = {}

    async def fake_criteria(pid, tid):
        captured["criteria_task"] = tid
        return ["规格 3.2：默认 admin token"]

    async def spy_call(system, user):
        captured["system"] = system
        captured["user"] = user
        return "VERDICT: PASS\n"

    reset_ledger(AGENT_ID)
    p_wt, p_git, p_save = _run_audit_patches()
    with (
        p_wt,
        p_git,
        p_save,
        patch(
            "hiveweave.services.code_audit.load_task_acceptance_criteria",
            new=fake_criteria,
        ),
    ):
        result = await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-b",
            call_llm=spy_call,
            appeal_notes="规格要求默认 token，非缺陷",
        )
    reset_ledger(AGENT_ID)

    assert result["audited"] is True
    assert captured["criteria_task"] == "task-b"
    assert "规格 3.2：默认 admin token" in captured["user"]
    assert "任务验收标准" in captured["user"]
    assert "作者申诉" in captured["user"]
    assert "规格要求默认 token，非缺陷" in captured["user"]
    assert "不因申诉自动放行" in captured["user"]


# ── 任务 C：失败入队 → 退避 → 重跑成功 → 通知；diff 变化作废 ──────


@pytest.mark.asyncio
async def test_llm_failed_enqueues_retry_with_backoff(env):
    """llm_failed → 入队 pending（attempts=1, 指数退避）；重复失败 attempts+1。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    reset_ledger(AGENT_ID)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        result = await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-c",
            call_llm=AsyncMock(side_effect=RuntimeError("HTTP 503 upstream")),
        )
    assert result["audited"] is False
    assert result["reason"] == "llm_failed"
    assert result["retry_queued"] is True
    assert result["retry_attempts"] == 1
    assert result["retry_exhausted"] is False

    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    row = await _fetch_retry_row(conn, AGENT_ID)
    assert row is not None
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["task_id"] == "task-c"
    req = json.loads(row["request_json"])
    assert req["project_id"] == PROJECT_ID
    assert req["agent_id"] == AGENT_ID
    assert req["task_id"] == "task-c"
    assert row["next_retry_at"] > int(time.time() * 1000)

    # 第二次失败（同 agent+diff）→ attempts+1 退避
    reset_ledger(AGENT_ID)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        result2 = await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-c",
            call_llm=AsyncMock(side_effect=RuntimeError("HTTP 503 again")),
        )
    assert result2["retry_attempts"] == 2
    row2 = await _fetch_retry_row(conn, AGENT_ID)
    assert int(row2["attempts"]) == 2


@pytest.mark.asyncio
async def test_retry_loop_rerun_success_notifies(env):
    """队列重跑成功 → status=done + 收件箱通知（凭证/结论/wake=1/幂等键）。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    reset_ledger(AGENT_ID)
    # 第一次真实失败 → 入队（diff_hash 由真实 collect 计算）
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-d",
            call_llm=AsyncMock(side_effect=RuntimeError("upstream 500")),
        )
    reset_ledger(AGENT_ID)

    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    row = await _fetch_retry_row(conn, AGENT_ID)
    assert row is not None

    send_mock = AsyncMock(return_value={"should_wake": True})
    oneshot_mock = AsyncMock(return_value="VERDICT: PASS\n")
    peer_cfg = {"id": "m-peer", "model_id": "peer-model-x"}
    p_wt2, p_git2, p_save2 = _run_audit_patches()
    p_peer = patch(
        "hiveweave.services.code_audit.resolve_peer_audit_model",
        new_callable=AsyncMock,
        return_value=(peer_cfg, "peer"),
    )
    p_oneshot = patch(
        "hiveweave.services.audit_retry._resolve_oneshot_callback",
        new_callable=AsyncMock,
        return_value=oneshot_mock,
    )
    with (
        p_wt2,
        p_git2,
        p_save2,
        p_peer,
        p_oneshot,
        patch("hiveweave.services.inbox.InboxService") as IS,
    ):
        IS.return_value.send_message = send_mock
        await retry_module.audit_retry_loop._process_row(PROJECT_ID, row)

    cur = await conn.execute(
        "SELECT status FROM audit_retry WHERE id = ?", [row["id"]]
    )
    status_row = await cur.fetchone()
    await cur.close()
    assert status_row["status"] == "done"

    send_mock.assert_awaited_once()
    kw = send_mock.await_args.kwargs
    assert kw["to_agent_id"] == AGENT_ID
    assert kw["wake"] is True
    assert kw["trusted_platform"] is True
    assert kw["idempotency_key"] == f"audit_retry:{row['id']}:1"
    assert "审计重试成功" in kw["message"]
    assert "PASS" in kw["message"]
    assert "可继续提交" in kw["message"]


@pytest.mark.asyncio
async def test_retry_loop_discards_when_diff_changed(env):
    """重跑前 diff_hash 变化 → status=discarded + 通知「作废，请重新审计」。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    reset_ledger(AGENT_ID)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-e",
            call_llm=AsyncMock(side_effect=RuntimeError("upstream 500")),
        )
    reset_ledger(AGENT_ID)

    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    row = await _fetch_retry_row(conn, AGENT_ID)
    assert row is not None

    # diff 变了：git 这次返回不同内容
    async def changed_git(args, cwd, timeout=30.0):
        table = {
            ("rev-parse", "--verify", "refs/heads/main"): (True, ""),
            ("diff", "main...HEAD"): (
                True, "--- a/new.py\n+++ b/new.py\n+totally new",
            ),
            ("diff", "HEAD"): (True, ""),
            ("ls-files", "--others", "--exclude-standard"): (True, ""),
            ("rev-parse", "HEAD"): (True, HEAD_SHA),
        }
        return table.get(tuple(args), (False, ""))

    send_mock = AsyncMock(return_value={"should_wake": True})
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=changed_git),
        patch("hiveweave.services.inbox.InboxService") as IS,
    ):
        IS.return_value.send_message = send_mock
        await retry_module.audit_retry_loop._process_row(PROJECT_ID, row)

    cur = await conn.execute(
        "SELECT status FROM audit_retry WHERE id = ?", [row["id"]]
    )
    status_row = await cur.fetchone()
    await cur.close()
    assert status_row["status"] == "discarded"

    kw = send_mock.await_args.kwargs
    assert "作废" in kw["message"]
    assert "重新" in kw["message"]
    assert kw["idempotency_key"] == f"audit_retry:{row['id']}:discarded"


@pytest.mark.asyncio
async def test_enqueue_exhausts_after_max_attempts(env):
    """连续失败达 MAX_ATTEMPTS → exhausted（不再 pending，等人工决策）。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    notify_mock = AsyncMock(return_value={"should_wake": True})
    last = None
    p_wt, p_git, p_save = _run_audit_patches()
    with (
        p_wt,
        p_git,
        p_save,
        patch("hiveweave.services.inbox.InboxService") as IS,
    ):
        IS.return_value.send_message = notify_mock
        for _ in range(retry_module.MAX_ATTEMPTS):
            reset_ledger(AGENT_ID)
            last = await run_code_audit(
                PROJECT_ID, AGENT_ID, task_id="task-f",
                call_llm=AsyncMock(side_effect=RuntimeError("upstream 503")),
            )
        reset_ledger(AGENT_ID)

    assert last["retry_exhausted"] is True
    assert last["retry_attempts"] == retry_module.MAX_ATTEMPTS

    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    row = await _fetch_retry_row(conn, AGENT_ID)
    assert row is not None
    assert row["status"] == "exhausted"
    assert int(row["attempts"]) == retry_module.MAX_ATTEMPTS

    # 耗尽通知：指路 waive（真实人工决策）
    exhaust_calls = [
        c for c in notify_mock.await_args_list
        if "audit_retry:" in str(c.kwargs.get("idempotency_key", ""))
    ]
    assert exhaust_calls, "exhaustion must notify the agent"
    assert "waive_attestation" in exhaust_calls[-1].kwargs["message"]
    assert "5 次仍失败" in exhaust_calls[-1].kwargs["message"]


@pytest.mark.asyncio
async def test_enqueue_stays_legacy_contract_without_db(monkeypatch):
    """无项目 DB（workspace 不存在）→ 入队失败，llm_failed 回执保持原契约。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    async def no_workspace(pid: str):
        return None

    monkeypatch.setattr(
        "hiveweave.db.meta.get_project_workspace", no_workspace
    )
    reset_ledger(AGENT_ID)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        result = await run_code_audit(
            "no-such-project", AGENT_ID,
            call_llm=AsyncMock(side_effect=RuntimeError("upstream down")),
        )
    reset_ledger(AGENT_ID)
    # 与 41+08 时代的契约完全一致（无 retry 附加键）
    assert result == {
        "audited": False,
        "reason": "llm_failed",
        "audit_upstream_unavailable": True,
    }


# ── 任务 D：waive 按原因分流 + 审批自动路由 ──────────────────────


def _waive_patches(task: dict, holders):
    """waive_attestation_tool 的公共打桩（TaskService / Org / 名单）。"""
    ts = MagicMock()
    ts.get_task = AsyncMock(return_value=task)
    ts._is_verify_task = MagicMock(return_value=False)
    org = MagicMock()
    org.get_agent = AsyncMock(
        side_effect=lambda aid: {
            "coord-1": {"id": "coord-1", "name": "统筹", "short_id": "c1",
                        "role": "后端负责人"},
            "qa-1": {"id": "qa-1", "name": "鹿鸣", "short_id": "q1",
                     "role": "测试工程师"},
        }.get(aid, {"id": aid, "name": str(aid)[:8], "short_id": "?"})
    )
    list_mock = AsyncMock(return_value=list(holders))
    sole_mock = AsyncMock(return_value=False)
    return ts, org, list_mock, sole_mock


@pytest.mark.asyncio
async def test_waive_tool_failure_keeps_approval_and_routes(env):
    """tool_failure 豁免：waiver 落 kind；QA 优先路由通知；发起人保留审批权。"""
    from hiveweave.tools.task_tools import (
        WaiveAttestationParams,
        waive_attestation_tool,
    )

    task = {
        "id": "t-tf-1",
        "title": "实现导出模块",
        "tags": [],
        "evidence": {},
        "assignee_id": None,
    }
    ts, org, list_mock, sole_mock = _waive_patches(task, ["coord-1", "qa-1"])
    send_mock = AsyncMock(return_value={"should_wake": True})

    with (
        patch("hiveweave.tools.helpers.get_project_id",
              AsyncMock(return_value=PROJECT_ID)),
        patch("hiveweave.services.task.TaskService", return_value=ts),
        patch("hiveweave.services.org.OrgService", return_value=org),
        patch("hiveweave.services.unblock_soft.list_review_capable_agent_ids",
              list_mock),
        patch("hiveweave.services.unblock_soft.is_small_team_sole_reviewer",
              sole_mock),
        patch("hiveweave.services.policy.infer_role_family",
              return_value="ceo"),
        patch("hiveweave.services.inbox.InboxService") as IS,
    ):
        IS.return_value.send_message = send_mock
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-tf-1",
                reason="审计 LLM 上游连续 llm_failed，自动化通道暂不可用",
                reasonKind="tool_failure",
            ),
            agent_id="coord-1",
            workspace=env["workspace_path"],
        )

    assert result.success is True, (result.output or "") + (result.error or "")
    text = result.output or ""
    assert "kind=tool_failure" in text
    assert "不影响你的审批权" in text
    # 审计 P2 修复：路由播报只在 route_note 一处，不再重复两遍
    assert "审批已请求转给" not in text
    assert text.count("审批请求已自动转给") == 1
    assert "鹿鸣" in text

    # 审批路由通知：QA 优先（qa-1 的 role=测试工程师）
    route_calls = [
        c for c in send_mock.await_args_list
        if str(c.kwargs.get("idempotency_key", "")).startswith("waive_route:")
    ]
    assert len(route_calls) == 1
    kw = route_calls[0].kwargs
    assert kw["to_agent_id"] == "qa-1"
    assert kw["wake"] is True
    assert kw["trusted_platform"] is True
    assert kw["idempotency_key"].startswith("waive_route:t-tf-1:")
    assert "已豁免" in kw["message"] and "请你审批" in kw["message"]

    # tool_failure 不把发起人踢出审批名单
    excl = list_mock.await_args.kwargs["exclude_ids"]
    assert "coord-1" not in excl

    # waiver 行落了 kind
    wr = await get_valid_waiver(PROJECT_ID, "t-tf-1")
    assert wr is not None
    assert wr["waiver_kind"] == "tool_failure"


@pytest.mark.asyncio
async def test_waive_quality_regression_third_party_isolation(env):
    """quality（默认）豁免：waived_by + assignee 都被排除（连坐回归）。"""
    from hiveweave.tools.task_tools import (
        WaiveAttestationParams,
        waive_attestation_tool,
    )

    task = {
        "id": "t-q-1",
        "title": "实现导入模块",
        "tags": [],
        "evidence": {},
        "assignee_id": "exec-9",
    }
    ts, org, list_mock, sole_mock = _waive_patches(task, ["qa-1"])
    send_mock = AsyncMock(return_value={"should_wake": True})

    with (
        patch("hiveweave.tools.helpers.get_project_id",
              AsyncMock(return_value=PROJECT_ID)),
        patch("hiveweave.services.task.TaskService", return_value=ts),
        patch("hiveweave.services.org.OrgService", return_value=org),
        patch("hiveweave.services.unblock_soft.list_review_capable_agent_ids",
              list_mock),
        patch("hiveweave.services.unblock_soft.is_small_team_sole_reviewer",
              sole_mock),
        patch("hiveweave.services.policy.infer_role_family",
              return_value="ceo"),
        patch("hiveweave.services.inbox.InboxService") as IS,
    ):
        IS.return_value.send_message = send_mock
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-q-1",
                reason="CLI 任务无 UI 可 browse，以 bash 验证日志替代验证",
                # reasonKind 缺省 = quality
            ),
            agent_id="coord-1",
            workspace=env["workspace_path"],
        )

    assert result.success is True
    text = result.output or ""
    assert "kind=quality" in text
    assert "失审批权" in text

    excl = list_mock.await_args.kwargs["exclude_ids"]
    assert "coord-1" in excl      # waived_by 连坐
    assert "exec-9" in excl       # assignee 永不可自批
    # 路由仍会发生（qa-1 可批）
    route_calls = [
        c for c in send_mock.await_args_list
        if str(c.kwargs.get("idempotency_key", "")).startswith("waive_route:")
    ]
    assert len(route_calls) == 1
    assert route_calls[0].kwargs["to_agent_id"] == "qa-1"

    wr = await get_valid_waiver(PROJECT_ID, "t-q-1")
    assert wr is not None
    assert wr["waiver_kind"] == "quality"


@pytest.mark.asyncio
async def test_create_waiver_kind_normalization(env):
    """create_waiver kind 归一化：未知值收紧为 quality；get_valid_waiver 回读。"""
    await create_waiver(
        PROJECT_ID, task_id="t-k-1", waived_by="coord-1",
        reason="kind normalization check tool failure path here",
        kind="tool_failure",
    )
    wr = await get_valid_waiver(PROJECT_ID, "t-k-1")
    assert wr["waiver_kind"] == "tool_failure"

    await create_waiver(
        PROJECT_ID, task_id="t-k-2", waived_by="coord-1",
        reason="kind normalization default quality path check here",
    )
    wr2 = await get_valid_waiver(PROJECT_ID, "t-k-2")
    assert wr2["waiver_kind"] == "quality"

    await create_waiver(
        PROJECT_ID, task_id="t-k-3", waived_by="coord-1",
        reason="kind normalization unknown value tightened here ok",
        kind="weird-value",
    )
    wr3 = await get_valid_waiver(PROJECT_ID, "t-k-3")
    assert wr3["waiver_kind"] == "quality"


# ── 任务 E：prompt / 回执文案 ────────────────────────────────────


def _norm(text: str) -> str:
    from hiveweave.prompts.identity import _normalize_cjk_punct

    return _normalize_cjk_punct(text)


def test_identity_prompt_auto_retry_wording():
    from hiveweave.prompts.identity import build_identity_prompt

    text = _norm(
        build_identity_prompt("开发工程师", "executor", "", name="阿蓝")
    )
    assert "平台会自动排队重试并回填通知，等待即可" in text
    assert "多次自动重试仍失败时才考虑 waive" in text
    # 旧口径必须消失
    assert "仍失败就请协调者 waive" not in text


def test_executor_prompt_auto_retry_wording():
    from hiveweave.prompts.executor import build_executor_script

    for role in ("开发工程师", "测试工程师"):
        text = _norm(build_executor_script(role, "阿蓝"))
        assert "平台会自动排队后台重试" in text, role
        assert "无需申请豁免" in text, role
        assert "多次自动重试（5 次）仍失败" in text, role
        # 旧口径：教 agent 在 llm_failed 后主动请上级 waive —— 必须消失
        assert "请他对这条任务" not in text, role


def test_coordinator_prompt_auto_retry_wording():
    from hiveweave.prompts.coordinator import build_coordinator_script

    text = _norm(build_coordinator_script("后端负责人", "阿蓝"))
    assert "无需申请豁免" in text
    assert "reasonKind=tool_failure" in text
    assert "自动排队后台重试" in text


@pytest.mark.asyncio
async def test_tool_receipt_retry_queue_wording():
    """llm_failed + 已入队 → 回执是重试队列口径；耗尽 → waive 出口。"""
    from hiveweave.tools.code_audit import (
        RequestCodeAuditParams,
        request_code_audit_tool,
    )

    run_mock = AsyncMock(
        return_value={
            "audited": False,
            "reason": "llm_failed",
            "audit_upstream_unavailable": True,
            "retry_queued": True,
            "retry_attempts": 2,
            "retry_exhausted": False,
        }
    )
    with (
        patch("hiveweave.tools.helpers.get_project_id",
              AsyncMock(return_value=PROJECT_ID)),
        patch("hiveweave.services.code_audit.run_code_audit", new=run_mock),
    ):
        result = await request_code_audit_tool(
            RequestCodeAuditParams(task_id="t-1"), AGENT_ID, r"C:\fake\wt",
        )
    # TEST_DSH_55 P0 三态：已受理·等待结果 ⇒ 不报成功；诊断走 error
    # （判定未放宽：由「必为 ok」收紧为「必为 accepted_pending 且信号齐全」）
    assert result.success is False
    assert result.fact == "outcome_unknown"
    assert result.blocked is True
    assert result.extra["audit_state"] == "accepted_pending"
    assert result.extra["wait_for_notice"] is True
    assert result.extra["action_required"] is False
    text = result.error or ""
    assert "已排队自动重试" in text
    assert "第 2 次" in text
    assert "无需申请豁免" in text

    run_mock2 = AsyncMock(
        return_value={
            "audited": False,
            "reason": "llm_failed",
            "audit_upstream_unavailable": True,
            "retry_queued": True,
            "retry_attempts": 5,
            "retry_exhausted": True,
        }
    )
    with (
        patch("hiveweave.tools.helpers.get_project_id",
              AsyncMock(return_value=PROJECT_ID)),
        patch("hiveweave.services.code_audit.run_code_audit", new=run_mock2),
    ):
        result2 = await request_code_audit_tool(
            RequestCodeAuditParams(task_id="t-1"), AGENT_ID, r"C:\fake\wt",
        )
    # 重试耗尽 = 平台不再接管 ⇒ 终局失败（不是「已受理」）
    assert result2.success is False
    assert result2.extra["audit_state"] == "failed"
    assert result2.extra["action_required"] is True
    assert result2.blocked is False
    text2 = result2.error or ""
    assert "已达上限" in text2
    assert "waive_attestation" in text2


@pytest.mark.asyncio
async def test_tool_receipt_appeal_notes_passthrough():
    """appealNotes 参数透传到 run_code_audit(appeal_notes=...)。"""
    from hiveweave.tools.code_audit import (
        RequestCodeAuditParams,
        request_code_audit_tool,
    )

    run_mock = AsyncMock(
        return_value={"audited": True, "verdict": "PASS",
                      "lines_audited": 3, "attestation_id": "att-9"}
    )
    with (
        patch("hiveweave.tools.helpers.get_project_id",
              AsyncMock(return_value=PROJECT_ID)),
        patch("hiveweave.services.code_audit.run_code_audit", new=run_mock),
    ):
        result = await request_code_audit_tool(
            RequestCodeAuditParams(
                task_id="t-1",
                appealNotes="规格 3.2 要求默认 admin token",
            ),
            AGENT_ID, r"C:\fake\wt",
        )
    assert result.success is True
    assert run_mock.await_args.kwargs["appeal_notes"] == (
        "规格 3.2 要求默认 admin token"
    )


# ══ 审计代理复审修复（P0-1 + P1-1..4 + P2 ①②③）═════════════════════


# ── P0-1：no_lawful_approver kind-aware ──────────────────────────


@pytest.mark.asyncio
async def test_no_lawful_approver_tool_failure_keeps_issuer(env):
    """tool_failure waiver：waived_by 保留审批权 → 不构成审批死锁。"""
    from hiveweave.services.unblock_soft import no_lawful_approver

    async def fake_list(project_id, *, exclude_ids=None):
        excl = exclude_ids or set()
        return [] if "coord-1" in excl else ["coord-1"]

    sole_mock = AsyncMock(return_value=False)
    with (
        patch("hiveweave.services.unblock_soft.list_review_capable_agent_ids",
              fake_list),
        patch("hiveweave.services.unblock_soft.is_small_team_sole_reviewer",
              sole_mock),
    ):
        tool_failure = await no_lawful_approver(
            PROJECT_ID,
            {"id": "t-nla-tf"},
            waiver_row={"agent_id": "coord-1", "waiver_kind": "tool_failure"},
        )
        assert tool_failure is None
        sole_mock.assert_not_awaited()  # tool_failure 短路，不走 sole 豁免

        # quality（显式）：同一形状 → 死锁诊断（sole 被打 False 模拟非唯一 holder 豁免失效）
        quality = await no_lawful_approver(
            PROJECT_ID,
            {"id": "t-nla-q"},
            waiver_row={"agent_id": "coord-1", "waiver_kind": "quality"},
        )
        assert quality is not None
        assert "waiver issuer cannot approve" in quality

        # 存量行（waiver_kind 缺失）→ quality 语义，不放宽
        legacy = await no_lawful_approver(
            PROJECT_ID,
            {"id": "t-nla-legacy"},
            waiver_row={"agent_id": "coord-1"},
        )
        assert legacy is not None


@pytest.mark.asyncio
async def test_no_lawful_approver_reads_kind_from_active_waiver(env):
    """未传 waiver_row 时从 DB active waiver 读 kind（真实落库链路）。"""
    from hiveweave.services.unblock_soft import no_lawful_approver

    await create_waiver(
        PROJECT_ID, task_id="t-nla-db-tf", waived_by="coord-1",
        reason="no lawful approver kind from db tool failure case",
        kind="tool_failure",
    )
    await create_waiver(
        PROJECT_ID, task_id="t-nla-db-q", waived_by="coord-1",
        reason="no lawful approver kind from db quality case here",
    )

    async def fake_list(project_id, *, exclude_ids=None):
        excl = exclude_ids or set()
        return [] if "coord-1" in excl else ["coord-1"]

    with (
        patch("hiveweave.services.unblock_soft.list_review_capable_agent_ids",
              fake_list),
        patch("hiveweave.services.unblock_soft.is_small_team_sole_reviewer",
              AsyncMock(return_value=False)),
    ):
        assert await no_lawful_approver(
            PROJECT_ID, {"id": "t-nla-db-tf"}
        ) is None
        deadlock = await no_lawful_approver(PROJECT_ID, {"id": "t-nla-db-q"})
        assert deadlock is not None
        assert "waiver issuer cannot approve" in deadlock


# ── P1-1：路由候选恒排除 assignee ────────────────────────────────


@pytest.mark.asyncio
async def test_route_never_wakes_assignee_even_when_qa(env):
    """assignee 是唯一 QA 时路由也不得 wake 承办人本人（自审硬门必拒）。"""
    from hiveweave.tools.task_tools import (
        WaiveAttestationParams,
        waive_attestation_tool,
    )

    task = {
        "id": "t-route-1",
        "title": "实现路由模块",
        "tags": [],
        "evidence": {},
        "assignee_id": "exec-9",
    }
    ts = MagicMock()
    ts.get_task = AsyncMock(return_value=task)
    ts._is_verify_task = MagicMock(return_value=False)
    org = MagicMock()
    org.get_agent = AsyncMock(side_effect=lambda aid: {
        "coord-1": {"id": "coord-1", "name": "统筹", "short_id": "c1",
                    "role": "后端负责人"},
        "coord-2": {"id": "coord-2", "name": "坐标", "short_id": "c2",
                    "role": "前端负责人"},
        "exec-9": {"id": "exec-9", "name": "承包", "short_id": "e9",
                   "role": "测试工程师"},  # 承办人本人是 QA
    }.get(aid, {"id": aid, "name": str(aid)[:8], "short_id": "?"}))

    async def fake_list(project_id, *, exclude_ids=None):
        excl = exclude_ids or set()
        return [h for h in ("exec-9", "coord-2", "qa-1") if h not in excl]

    send_mock = AsyncMock(return_value={"should_wake": True})
    with (
        patch("hiveweave.tools.helpers.get_project_id",
              AsyncMock(return_value=PROJECT_ID)),
        patch("hiveweave.services.task.TaskService", return_value=ts),
        patch("hiveweave.services.org.OrgService", return_value=org),
        patch("hiveweave.services.policy.infer_role_family",
              return_value="ceo"),
        patch("hiveweave.services.unblock_soft.list_review_capable_agent_ids",
              fake_list),
        patch("hiveweave.services.unblock_soft.is_small_team_sole_reviewer",
              AsyncMock(return_value=False)),
        patch("hiveweave.services.inbox.InboxService") as IS,
    ):
        IS.return_value.send_message = send_mock
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-route-1",
                reason="审计 LLM 上游暂态失败，工具失败类豁免该任务门禁",
                reasonKind="tool_failure",
            ),
            agent_id="coord-1",
            workspace=env["workspace_path"],
        )

    assert result.success is True
    route_calls = [
        c for c in send_mock.await_args_list
        if str(c.kwargs.get("idempotency_key", "")).startswith("waive_route:")
    ]
    assert len(route_calls) == 1
    # 路由到 coord-2（排除 assignee 后的 QA 优先者），绝不 wake 承办人 exec-9
    assert route_calls[0].kwargs["to_agent_id"] == "coord-2"
    # exec-9 可能收到 waive 工具固有的 assignee 通知（合法），但绝不能收到
    # 审批路由请求（自审硬门必拒）
    assert all(
        c.kwargs["to_agent_id"] != "exec-9"
        for c in send_mock.await_args_list
        if str(c.kwargs.get("idempotency_key", "")).startswith("waive_route:")
    )


# ── P1-2：存量缓存 NULL 明细回执 ─────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_cache_row_null_issues_gets_upgrade_note(env):
    """存量缓存行 top_issues=NULL → ISSUES 回执不再出现「问题数: 0」矛盾。"""
    from hiveweave.services.code_audit import (
        record_change,
        reset_ledger,
        run_code_audit,
    )
    from hiveweave.tools.code_audit import _format_verdict

    llm_text = "VERDICT: ISSUES\n- x.py:1 [high] boom\n"
    reset_ledger(AGENT_ID)
    record_change(AGENT_ID, 30)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        first = await run_code_audit(
            PROJECT_ID, AGENT_ID,
            call_llm=AsyncMock(return_value=llm_text),
        )
    assert first["verdict"] == "ISSUES"
    reset_ledger(AGENT_ID)

    # 模拟升级前写入的存量行：明细列为 NULL
    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    cur = await conn.execute(
        "UPDATE audit_cache SET top_issues = NULL, source_attestation_id = NULL "
        "WHERE agent_id = ?",
        [AGENT_ID],
    )
    await conn.commit()
    await cur.close()

    record_change(AGENT_ID, 5)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        second = await run_code_audit(
            PROJECT_ID, AGENT_ID,
            call_llm=AsyncMock(return_value="SHOULD NOT BE CALLED"),
        )
    reset_ledger(AGENT_ID)

    assert second["verdict"] == "ISSUES"
    assert second["issues_count"] == 0
    receipt = _format_verdict(second).output or ""
    assert "存量缓存无问题明细" in receipt
    assert "diff 变化后重审" in receipt
    assert "问题数: 0" not in receipt
    # 新鲜审计（非缓存命中）ISSUES 无明细时仍诚实显示 0
    fresh = _format_verdict({
        "verdict": "ISSUES", "issues_count": 0, "top_issues": [],
        "lines_audited": 1,
    }).output or ""
    assert "问题数: 0" in fresh
    assert "存量缓存" not in fresh


# ── P1-3：列迁移只吞 duplicate-column，其余错误不落 _migrated ─────


@pytest.mark.asyncio
async def test_column_migration_retries_after_non_duplicate_error():
    """ALTER 因锁/IO 失败 → 不标记迁移（下次重试），且 ensure_schema 不抛。"""
    import sqlite3 as _sqlite3

    async def locked_exec(pid, sql, params=None):
        if "ADD COLUMN" in sql:
            raise _sqlite3.OperationalError("database is locked")
        return None

    with patch("hiveweave.services.attestation.execute_by_project",
               new=locked_exec):
        att_module._migrated.clear()
        # 标记键 = (workspace, 连接世代)，不再是裸 project_id —— 断言必须走
        # 同一个 key 函数，否则键形状一变就变成静默失真的假绿。
        from hiveweave.db import project as project_db

        key = await project_db.schema_marker_key_for_project("mig-proj-locked")
        # 不得抛异常
        await attestation_service.ensure_schema("mig-proj-locked")
        assert key not in att_module._migrated


@pytest.mark.asyncio
async def test_column_migration_duplicate_is_idempotent():
    """duplicate column → 幂等吞掉并正常标记迁移完成。"""
    import sqlite3 as _sqlite3

    async def dup_exec(pid, sql, params=None):
        if "ADD COLUMN" in sql:
            raise _sqlite3.OperationalError(
                "duplicate column name: top_issues"
            )
        return None

    with patch("hiveweave.services.attestation.execute_by_project",
               new=dup_exec):
        att_module._migrated.clear()
        from hiveweave.db import project as project_db

        key = await project_db.schema_marker_key_for_project("mig-proj-dup")
        await attestation_service.ensure_schema("mig-proj-dup")
        assert key in att_module._migrated


# ── P1-4：appeal_notes 贯穿入队与重跑 ────────────────────────────


@pytest.mark.asyncio
async def test_appeal_notes_flows_through_retry_queue(env):
    """llm_failed 入队携带 appeal_notes → request_json 落库 → 重跑透传。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    appeal = "规格 3.2 要求默认 admin token，非缺陷"
    reset_ledger(AGENT_ID)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-appeal",
            call_llm=AsyncMock(side_effect=RuntimeError("upstream 503")),
            appeal_notes=appeal,
        )
    reset_ledger(AGENT_ID)

    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    row = await _fetch_retry_row(conn, AGENT_ID)
    assert row is not None
    req = json.loads(row["request_json"])
    assert req["appeal_notes"] == appeal

    # 重跑：透传给 run_code_audit
    rerun_mock = AsyncMock(
        return_value={"audited": True, "verdict": "PASS",
                      "lines_audited": 0, "attestation_id": "att-r"}
    )
    p_wt, p_git, p_save = _run_audit_patches()
    with (
        p_wt,
        p_git,
        p_save,
        patch("hiveweave.services.code_audit.run_code_audit", new=rerun_mock),
    ):
        await retry_module.audit_retry_loop._process_row(PROJECT_ID, row)
    assert rerun_mock.await_args.kwargs["appeal_notes"] == appeal


@pytest.mark.asyncio
async def test_retry_without_appeal_notes_passes_none(env):
    """入队时无申诉 → 重跑 appeal_notes=None（不传垃圾值）。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    reset_ledger(AGENT_ID)
    p_wt, p_git, p_save = _run_audit_patches()
    with p_wt, p_git, p_save:
        await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-noappeal",
            call_llm=AsyncMock(side_effect=RuntimeError("upstream 503")),
        )
    reset_ledger(AGENT_ID)

    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    row = await _fetch_retry_row(conn, AGENT_ID)
    rerun_mock = AsyncMock(
        return_value={"audited": True, "verdict": "PASS",
                      "lines_audited": 0, "attestation_id": "att-r2"}
    )
    p_wt, p_git, p_save = _run_audit_patches()
    with (
        p_wt,
        p_git,
        p_save,
        patch("hiveweave.services.code_audit.run_code_audit", new=rerun_mock),
    ):
        await retry_module.audit_retry_loop._process_row(PROJECT_ID, row)
    assert rerun_mock.await_args.kwargs["appeal_notes"] is None


# ── P2-②：耗尽后人工重试 = 新一轮重试序列 ────────────────────────


@pytest.mark.asyncio
async def test_manual_retry_after_exhaustion_starts_new_sequence(env):
    """exhausted 后再失败 → 新插行 resequence=True（不再是「第 1 次」歧义）。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    notify_mock = AsyncMock(return_value={"should_wake": True})
    last = None
    p_wt, p_git, p_save = _run_audit_patches()
    with (
        p_wt,
        p_git,
        p_save,
        patch("hiveweave.services.inbox.InboxService") as IS,
    ):
        IS.return_value.send_message = notify_mock
        for _ in range(retry_module.MAX_ATTEMPTS):
            reset_ledger(AGENT_ID)
            last = await run_code_audit(
                PROJECT_ID, AGENT_ID, task_id="task-reseq",
                call_llm=AsyncMock(side_effect=RuntimeError("upstream 503")),
            )
        assert last["retry_exhausted"] is True

        # 耗尽后人工重试仍失败 → 新一轮序列
        reset_ledger(AGENT_ID)
        last = await run_code_audit(
            PROJECT_ID, AGENT_ID, task_id="task-reseq",
            call_llm=AsyncMock(side_effect=RuntimeError("upstream 503")),
        )
    reset_ledger(AGENT_ID)

    assert last["retry_queued"] is True
    assert last["retry_resequence"] is True
    assert last["retry_attempts"] == 1
    assert last["retry_exhausted"] is False

    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    cur = await conn.execute(
        "SELECT status, attempts FROM audit_retry "
        "WHERE agent_id = ? AND status = 'pending'",
        [AGENT_ID],
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    assert int(row["attempts"]) == 1


@pytest.mark.asyncio
async def test_resequence_receipt_wording():
    """resequence 回执点明「新一轮重试序列」，与耗尽文案不打架。"""
    from hiveweave.tools.code_audit import (
        RequestCodeAuditParams,
        request_code_audit_tool,
    )

    run_mock = AsyncMock(
        return_value={
            "audited": False,
            "reason": "llm_failed",
            "audit_upstream_unavailable": True,
            "retry_queued": True,
            "retry_attempts": 1,
            "retry_exhausted": False,
            "retry_resequence": True,
        }
    )
    with (
        patch("hiveweave.tools.helpers.get_project_id",
              AsyncMock(return_value=PROJECT_ID)),
        patch("hiveweave.services.code_audit.run_code_audit", new=run_mock),
    ):
        result = await request_code_audit_tool(
            RequestCodeAuditParams(task_id="t-1"), AGENT_ID, r"C:\fake\wt",
        )
    # TEST_DSH_55 P0 三态：耗尽后人工重试 = 新一轮已受理 ⇒ accepted_pending
    assert result.success is False
    assert result.extra["audit_state"] == "accepted_pending"
    assert result.extra["wait_for_notice"] is True
    text = result.error or ""
    assert "新一轮重试序列" in text
    assert "第 1 次" in text


# ── P2-①：幂等拒绝措辞按 kind 分流 ───────────────────────────────


@pytest.mark.asyncio
async def test_idempotent_reject_wording_tool_failure(env):
    """tool_failure 幂等拒绝：不再硬编码「quality-class cannot approve」。"""
    from hiveweave.tools.task_tools import (
        WaiveAttestationParams,
        waive_attestation_tool,
    )

    task = {
        "id": "t-idem-tf",
        "title": "实现幂等模块",
        "tags": [],
        "evidence": {},
        "assignee_id": None,
    }
    ts, org, list_mock, sole_mock = _waive_patches(task, ["qa-1"])
    send_mock = AsyncMock(return_value={"should_wake": True})
    with (
        patch("hiveweave.tools.helpers.get_project_id",
              AsyncMock(return_value=PROJECT_ID)),
        patch("hiveweave.services.task.TaskService", return_value=ts),
        patch("hiveweave.services.org.OrgService", return_value=org),
        patch("hiveweave.services.policy.infer_role_family",
              return_value="ceo"),
        patch("hiveweave.services.unblock_soft.list_review_capable_agent_ids",
              list_mock),
        patch("hiveweave.services.unblock_soft.is_small_team_sole_reviewer",
              sole_mock),
        patch("hiveweave.services.inbox.InboxService") as IS,
    ):
        IS.return_value.send_message = send_mock
        params = dict(
            taskId="t-idem-tf",
            reason="审计 LLM 上游连续失败，工具失败类豁免该任务门禁检查",
            reasonKind="tool_failure",
        )
        first = await waive_attestation_tool(
            WaiveAttestationParams(**params),
            agent_id="coord-1",
            workspace=env["workspace_path"],
        )
        assert first.success is True
        second = await waive_attestation_tool(
            WaiveAttestationParams(**params),
            agent_id="coord-1",
            workspace=env["workspace_path"],
        )
    assert second.success is False
    text = (second.output or "") + (second.error or "")
    assert "kind=tool_failure" in text
    assert "does NOT take away your approval right" in text
    assert "quality-class waived_by cannot approve" not in text


@pytest.mark.asyncio
async def test_idempotent_reject_wording_quality(env):
    """quality 幂等拒绝：维持第三方隔离措辞。"""
    from hiveweave.tools.task_tools import (
        WaiveAttestationParams,
        waive_attestation_tool,
    )

    task = {
        "id": "t-idem-q",
        "title": "实现幂等质量模块",
        "tags": [],
        "evidence": {},
        "assignee_id": None,
    }
    ts, org, list_mock, sole_mock = _waive_patches(task, ["qa-1"])
    send_mock = AsyncMock(return_value={"should_wake": True})
    with (
        patch("hiveweave.tools.helpers.get_project_id",
              AsyncMock(return_value=PROJECT_ID)),
        patch("hiveweave.services.task.TaskService", return_value=ts),
        patch("hiveweave.services.org.OrgService", return_value=org),
        patch("hiveweave.services.policy.infer_role_family",
              return_value="ceo"),
        patch("hiveweave.services.unblock_soft.list_review_capable_agent_ids",
              list_mock),
        patch("hiveweave.services.unblock_soft.is_small_team_sole_reviewer",
              sole_mock),
        patch("hiveweave.services.inbox.InboxService") as IS,
    ):
        IS.return_value.send_message = send_mock
        first = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-idem-q",
                reason="CLI 任务无 UI 可 browse，以 bash 验证日志替代验证",
            ),
            agent_id="coord-1",
            workspace=env["workspace_path"],
        )
        assert first.success is True
        second = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-idem-q",
                reason="CLI 任务无 UI 可 browse，以 bash 验证日志替代验证",
            ),
            agent_id="coord-1",
            workspace=env["workspace_path"],
        )
    assert second.success is False
    text = (second.output or "") + (second.error or "")
    assert "kind=quality" in text
    assert "quality-class waived_by cannot approve" in text


@pytest.mark.asyncio
async def test_scan_once_tolerates_row_typed_meta_rows(env):
    """scan_once 对 meta 库 sqlite3.Row 行不再 .get 崩溃（TEST_BATCH_43 活体回归）。

    活体项目每 68s 报 audit_retry_loop_error: 'sqlite3.Row' object has no
    attribute 'get' —— scan_once 此前对 meta 行做 (p or {}).get("id")，
    Row 无 .get → 整轮扫描抛异常，重试队列永不工作。
    """
    import sqlite3 as _sq

    from hiveweave.services.audit_retry import AuditRetryLoop

    mem = _sq.connect(":memory:")
    mem.row_factory = _sq.Row
    mem.execute("CREATE TABLE projects (id TEXT)")
    mem.execute("INSERT INTO projects VALUES (?)", (PROJECT_ID,))
    row = mem.execute("SELECT id FROM projects").fetchone()
    assert isinstance(row, _sq.Row)

    with (
        patch(
            "hiveweave.services.audit_retry.meta_db.query",
            new_callable=AsyncMock,
            return_value=[row],
        ),
        patch.object(
            AuditRetryLoop,
            "_scan_project",
            new_callable=AsyncMock,
            return_value=0,
        ) as sp,
    ):
        loop = AuditRetryLoop()
        processed = await loop.scan_once()

    assert sp.await_count == 1
    assert sp.await_args.args[0] == PROJECT_ID
    assert processed == 0
