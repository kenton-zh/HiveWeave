"""门禁智能化包（2026-09-05 四件套）回归测试。

覆盖：
- 任务1 submit 聚合预检一次报全（attestation 门 + files_changed_empty +
  verdict_gate / acceptance_coverage 并进同一回执，不再逐轮撞门）
- 任务2 回执全文 UUID（拒绝/回执文案输出完整 36 位 id）
- 任务3 review/submit race 事实位（提交于 X 秒前；46 轮 #9 去掉时长承诺）
- 任务4 verdict_claim_check：blockingIssues 文件级主张机械复核
  verified / refuted / skipped 三例 + 只附事实位不拒绝
- 任务5 doc_review 分档（全文档放行 / 缺失拒 / 代码任务不进分档）+
  reviewer filesChanged 替代 evidence.files_changed
- 任务6 acceptance_criteria 覆盖门（缺条拒；#14 后**文本一律不算覆盖**——
  原文照抄/编号/裸 N/A 全拒，结构化 id + 凭证判据见
  tests/test_acceptance_state_gate.py）
"""
from __future__ import annotations

import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.acceptance import (
    format_acceptance_coverage_error,
    parse_acceptance_items,
    uncovered_acceptance_items,
)
from hiveweave.services.tasks.verdict_claim_check import (
    format_claim_check_lines,
    parse_file_claims,
    run_verdict_claim_check,
)
from hiveweave.services.attestation import create_waiver
from hiveweave.services.tasks.verify import VERIFY_KIND
from hiveweave.services.worktree_review import (
    _all_doc_paths,
    _has_parent_segment,
    _is_doc_path,
    review_worktree_gate,
)
from hiveweave.tools.tasks.review import (
    ReviewTaskParams,
    _race_fact_bit,
    review_task_tool,
)
from hiveweave.tools.tasks.submit import SubmitTaskParams, submit_task_tool

PROJECT_ID = "test-gate-intelligence"
COORD_ID = "coord-gi"
EXEC_ID = "exec-gi"


# ── env：DB workspace + 两个假 agent（无 worktree）─────────────────


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())
        # executor 的可测量 worktree（干净 git 仓库）——files_changed_empty
        # 只在 worktree/MAIN 可定位时判定
        wt_path = Path(workspace_path) / "wt-e901"
        wt_path.mkdir()
        import subprocess

        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(wt_path)], check=True
        )
        subprocess.run(
            ["git", "-C", str(wt_path), "config", "user.email", "t@t"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(wt_path), "config", "user.name", "t"],
            check=True,
        )

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORD_ID, EXEC_ID) else None

        _FAKE_AGENTS = {
            COORD_ID: {
                "id": COORD_ID,
                "name": "协调员",
                "short_id": "C901",
                "parent_id": None,
                "permission_type": "coordinator",
                "role": "架构师",
                "status": "active",
            },
            EXEC_ID: {
                "id": EXEC_ID,
                "name": "执行者",
                "short_id": "E901",
                "parent_id": COORD_ID,
                "permission_type": "executor",
                "role": "engineer",
                "status": "active",
                "workspace_path": str(wt_path),
            },
        }

        async def fake_get_agent_by_id(aid: str):
            return _FAKE_AGENTS.get(aid)

        att_module._migrated.clear()
        task_module._migrated.clear()
        project_db._agent_cache.pop(COORD_ID, None)
        project_db._agent_cache.pop(EXEC_ID, None)

        with (
            patch(
                "hiveweave.db.meta.get_project_workspace",
                fake_get_project_workspace,
            ),
            patch(
                "hiveweave.db.meta.get_agent_project_id",
                fake_get_agent_project_id,
            ),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
        ):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
                "coordinator_id": COORD_ID,
                "executor_id": EXEC_ID,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(COORD_ID, None)
        project_db._agent_cache.pop(EXEC_ID, None)


async def _mk_running_task(
    env, svc, *, title="Feature", tags=None, kind=None, source=None, **kw
):
    """新建并跑到 running。

    #11：VERIFY 判定来源是 ``kind``，**不是**标题前缀 —— 这里不再从
    ``title.startswith("VERIFY")`` 反推 source / 串行化旁路（那正是被删掉的
    文本判据；写在测试里会掩盖生产里"标题伪造 VERIFY"的真实路径）。
    需要 VERIFY 语义的调用方显式传 ``kind=VERIFY_KIND``。
    """
    is_verify = kind == VERIFY_KIND
    tid = await svc.create_task(
        env["project_id"], title, "d",
        creator_id=env["coordinator_id"],
        assignee_id=env["executor_id"],
        tags=tags or ["generic_tests"],
        source=source or ("system" if is_verify else "agent"),
        kind=kind,
        **kw,
    )
    # 单测同库并行多条 VERIFY —— 绕过单飞串行化锁（同 test_verdict_gate）
    await svc.claim_task(
        env["project_id"], tid, env["executor_id"],
        bypass_verify_serialize=is_verify,
    )
    await svc.start_task(env["project_id"], tid)
    return tid


def _base_patches(verify_ids_result=(False, "no matching attestation")):
    return [
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            return_value=frozenset({"bash_test"}),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.find_recent_for_agent",
            new=AsyncMock(return_value=[]),
        ),
    ]


async def _run_submit(env, params, submit_mock, verify_ids_result):
    async def _fake_verify_ids(project_id, ids, **kwargs):
        return verify_ids_result

    with ExitStack() as stack:
        for cm in _base_patches():
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.attestation_service.verify_ids",
                new=AsyncMock(side_effect=_fake_verify_ids),
            )
        )
        stack.enter_context(
            patch(
                "hiveweave.services.task.TaskService.submit_task", submit_mock
            )
        )
        return await submit_task_tool(params, EXEC_ID, env["workspace_path"])


def _params(task_id, **kw):
    base = dict(
        task_id=task_id,
        summary="done",
        tests_passed=True,
        dry_run=False,
    )
    base.update(kw)
    return SubmitTaskParams(**base)


# ── 任务1：聚合预检一次报全 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_preflight_reports_all_issues_at_once(env):
    """attestation 门失败 + files_changed 空 → 一条回执同时列出，不逐个撞。"""
    svc = TaskService()
    tid = await _mk_running_task(env, svc)
    submit_mock = AsyncMock()

    dry = await _run_submit(env, _params(tid, dry_run=True), submit_mock,
                            (False, "no matching attestation for kind bash_test"))
    assert dry.success is True, dry.error
    codes = {i["code"] for i in (dry.extra.get("missing") or [])}
    assert {"attestation", "files_changed_empty"} <= codes, codes
    submit_mock.assert_not_awaited()

    real = await _run_submit(env, _params(tid), submit_mock,
                             (False, "no matching attestation for kind bash_test"))
    assert real.success is False
    err = real.error or ""
    assert "attestation" in err, err
    assert "files_changed_empty" in err, err
    assert "[additional blockers]" in err, err
    submit_mock.assert_not_awaited()
    assert (await svc.get_task(env["project_id"], tid))["status"] == "running"


@pytest.mark.asyncio
async def test_submit_preflight_aggregates_verdict_and_checklist(env):
    """VERIFY 缺 verdict 且验收清单未覆盖 → dry_run 一次列出两类问题。"""
    svc = TaskService()
    tid = await _mk_running_task(
        env, svc,
        title="VERIFY: 里程碑",
        tags=["verify"],
        acceptance_criteria=["导出 CSV 功能可用", "边界情况有测试覆盖"],
        kind=VERIFY_KIND,
    )
    submit_mock = AsyncMock()

    async def _fake_verify_ids(project_id, ids, **kwargs):
        return True, ""

    with ExitStack() as stack:
        for cm in _base_patches():
            stack.enter_context(cm)
        # VERIFY 走空 needed 集合（跳过 attestation 门），隔离 verdict/清单门
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.required_attestation_kinds",
                return_value=frozenset(),
            )
        )
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.attestation_service.verify_ids",
                new=AsyncMock(side_effect=_fake_verify_ids),
            )
        )
        stack.enter_context(
            patch("hiveweave.services.task.TaskService.submit_task", submit_mock)
        )
        result = await submit_task_tool(
            SubmitTaskParams(task_id=tid, summary="done", tests_passed=True,
                             dry_run=True),
            EXEC_ID, env["workspace_path"],
        )

    assert result.success is True, result.error
    codes = {i["code"] for i in (result.extra.get("missing") or [])}
    assert "verdict_gate" in codes, codes
    assert "acceptance_coverage" in codes, codes
    submit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_task_not_blocked_by_files_changed_empty(env):
    """VERIFY 任务无 files_changed 不算问题（交付物是凭证/verdict）。"""
    svc = TaskService()
    tid = await _mk_running_task(env, svc, title="VERIFY: x", tags=["verify"], kind=VERIFY_KIND)

    async def _fake_verify_ids(project_id, ids, **kwargs):
        return True, ""

    with ExitStack() as stack:
        for cm in _base_patches():
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.required_attestation_kinds",
                return_value=frozenset(),
            )
        )
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.attestation_service.verify_ids",
                new=AsyncMock(side_effect=_fake_verify_ids),
            )
        )
        submit_mock = AsyncMock()
        stack.enter_context(
            patch("hiveweave.services.task.TaskService.submit_task", submit_mock)
        )
        result = await submit_task_tool(
            SubmitTaskParams(task_id=tid, summary="done", tests_passed=True,
                             dry_run=True),
            EXEC_ID, env["workspace_path"],
        )
    codes = {i["code"] for i in (result.extra.get("missing") or [])}
    assert "files_changed_empty" not in codes, codes


# ── 任务2：回执全文 UUID ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archived_receipt_carries_full_uuid(env):
    """归档任务拒绝文案输出完整 36 位任务 id（可直接复制）。"""
    svc = TaskService()
    tid = await _mk_running_task(env, svc)
    await svc.archive_task(
        env["project_id"], tid, archived_by=env["coordinator_id"],
        reason="obsolete",
    )
    submit_mock = AsyncMock()
    result = await _run_submit(env, _params(tid), submit_mock, (True, ""))
    assert result.success is False
    assert tid in (result.error or ""), result.error
    assert len(tid) == 36
    submit_mock.assert_not_awaited()


# ── 任务3：race 事实位 ──────────────────────────────────────────────


def test_race_fact_bit_seconds():
    # 46 轮 #9：模板写死 "~5s 可审" 与实测 378s 自相矛盾 → 改为只报实测
    # 年龄，不做时长承诺（处方仍留在主文案 WAIT a few seconds）。
    task = {"submitted_at": int(time.time() * 1000) - 12_000}
    bit = _race_fact_bit(task)
    assert "12s ago" in bit, bit
    assert "~5s" not in bit, bit
    assert "submitted" in bit


def test_race_fact_bit_without_timestamp():
    bit = _race_fact_bit({})
    assert "submitted_at" in bit, bit


# ── 任务4：verdict_claim_check ──────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_check_verified_refuted_skipped(env):
    """三例：主张成立（未找到）/ 被驳回（存在）/ 无法读取跳过。"""
    ws = Path(env["workspace_path"])
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "auth.py").write_text(
        "def login():\n    return 'ok'\n", encoding="utf-8"
    )
    (ws / "src" / "user.py").write_text(
        "def login_user(name):\n    return name\n", encoding="utf-8"
    )

    task = {"id": "t-claim", "title": "VERIFY: claim", "assignee_id": None}
    evidence = {
        "verdict": "FAIL",
        "blocking_issues": [
            "src/auth.py 缺少 token_refresh 处理",        # → verified
            "src/user.py 没有 login_user 函数",           # → refuted
            "src/ghost.py missing module_init",           # → skipped（读不到）
            "页面整体交互坏掉了，无具体文件指向",           # → 解析不了，跳过
        ],
    }
    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        new=AsyncMock(return_value=str(ws)),
    ):
        results = await run_verdict_claim_check(PROJECT_ID, task, evidence)

    by_sym = {r["symbol"]: r["result"] for r in results}
    assert by_sym.get("token_refresh") == "verified", results
    assert by_sym.get("login_user") == "refuted", results
    assert by_sym.get("module_init") == "skipped", results
    # 事实位文案
    lines = format_claim_check_lines(results)
    assert any(l.startswith('claim_check: "') for l in lines)
    assert any("→ refuted" in l for l in lines)
    assert any("→ verified" in l for l in lines)
    assert any("→ skipped" in l for l in lines)


def test_claim_check_parse_patterns():
    """中英典型句式都能解析出 path+symbol。"""
    cases = [
        ("api/auth.py is missing refresh_session", "api/auth.py", "refresh_session"),
        ("refresh_session missing in api/auth.py", "api/auth.py", "refresh_session"),
        ("api/auth.py does not define refresh_session", "api/auth.py", "refresh_session"),
        ("src/x.py 未定义 handle_submit", "src/x.py", "handle_submit"),
        ("handle_submit 在 src/x.py 中不存在", "src/x.py", "handle_submit"),
    ]
    for text, path, sym in cases:
        claims = parse_file_claims(text)
        assert claims, text
        assert claims[0]["path"] == path, (text, claims)
        assert claims[0]["symbol"] == sym, (text, claims)
    # 解析不了 → 空
    assert parse_file_claims("整体炸了") == []


@pytest.mark.asyncio
async def test_submit_fail_receipt_includes_claim_check_fact_bits(env):
    """verdict=FAIL 提交成功回执附 claim_check 事实位，且 evidence 落库。"""
    svc = TaskService()
    ws = Path(env["workspace_path"])
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "auth.py").write_text("def login():\n", encoding="utf-8")
    tid = await _mk_running_task(env, svc, title="VERIFY: 交互", tags=["verify"], kind=VERIFY_KIND)

    async def _fake_verify_ids(project_id, ids, **kwargs):
        return True, ""

    captured: dict = {}

    async def _fake_submit(project_id, task_id, evidence):
        captured["evidence"] = evidence

    with ExitStack() as stack:
        for cm in _base_patches():
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.required_attestation_kinds",
                return_value=frozenset(),
            )
        )
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.attestation_service.verify_ids",
                new=AsyncMock(side_effect=_fake_verify_ids),
            )
        )
        stack.enter_context(
            patch(
                "hiveweave.services.task.TaskService.submit_task",
                new=AsyncMock(side_effect=_fake_submit),
            )
        )
        result = await submit_task_tool(
            SubmitTaskParams(
                task_id=tid, summary="FAIL: 核验", tests_passed=True,
                verdict="FAIL",
                blocking_issues=["src/auth.py 缺少 token_refresh 处理"],
            ),
            EXEC_ID, env["workspace_path"],
        )

    assert result.success is True, result.error
    out = result.output or ""
    assert "claim_check" in out, out
    assert "→ verified" in out, out
    ev = captured["evidence"]
    assert ev.get("claim_check"), ev
    assert ev["claim_check"][0]["result"] == "verified"


# ── 任务5：doc_review 分档 + reviewer filesChanged ──────────────────


def _gate_task(wt: str, title: str = "docs task") -> dict:
    return {
        "id": "t-doc",
        "title": title,
        "assignee_id": "exec-doc",
        "implementer_id": "exec-doc",
        "implementer_worktree": wt,
    }


@pytest.mark.asyncio
async def test_doc_review_tier_allows_doc_only_files(env):
    """全文档清单（存在）→ 分档放行，不走 diverged 比对。"""
    tmp = Path(env["workspace_path"])
    main_ws = tmp / "main"
    wt = tmp / "wt"
    (wt / "docs").mkdir(parents=True, exist_ok=True)
    (wt / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
    task = _gate_task(str(wt))
    evidence = {"files_changed": ["docs/guide.md"]}
    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        new=AsyncMock(return_value=str(main_ws)),
    ):
        deny, meta = await review_worktree_gate(PROJECT_ID, task, evidence)
    assert deny is None, deny
    assert meta.get("skipped") == "doc_review_tier", meta
    assert _all_doc_paths(["docs/a.md", "b.markdown", "c.txt", "docs/d"])


@pytest.mark.asyncio
async def test_doc_review_tier_denies_missing_doc_files(env):
    """文档清单指向不存在的文件 → 拒绝并指出缺哪个。"""
    tmp = Path(env["workspace_path"])
    main_ws = tmp / "main2"
    wt = tmp / "wt2"
    wt.mkdir(parents=True, exist_ok=True)
    task = _gate_task(str(wt))
    evidence = {"files_changed": ["docs/absent.md", "README.md"]}
    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        new=AsyncMock(return_value=str(main_ws)),
    ):
        deny, meta = await review_worktree_gate(PROJECT_ID, task, evidence)
    assert deny is not None
    assert "absent.md" in deny, deny
    assert meta.get("doc_review_tier") == "missing_files"


@pytest.mark.asyncio
async def test_code_task_not_routed_through_doc_tier(env):
    """代码文件（非全文档）不进 doc_review 分档，走原 compare 门。"""
    tmp = Path(env["workspace_path"])
    main_ws = tmp / "main3"
    wt = tmp / "wt3"
    (wt / "src").mkdir(parents=True, exist_ok=True)
    (main_ws / "src").mkdir(parents=True, exist_ok=True)
    (wt / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (main_ws / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    (wt / "src" / "ghost.py").write_text("y = 1\n", encoding="utf-8")
    task = _gate_task(str(wt))
    evidence = {"files_changed": ["src/a.py", "src/ghost.py"]}
    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        new=AsyncMock(return_value=str(main_ws)),
    ):
        deny, meta = await review_worktree_gate(PROJECT_ID, task, evidence)
    # ghost.py 在 MAIN 缺失 → diverged（正常代码路径），不是 doc 分档
    assert meta.get("doc_review_tier") is None, meta
    assert meta.get("skipped") != "doc_review_tier", meta
    assert deny is None  # diverged 文件存在 → 原 compare 放行


@pytest.mark.asyncio
async def test_reviewer_files_changed_substitutes_evidence(env):
    """reviewer 传 filesChanged → 替代 evidence.files_changed 参与判定。"""
    tmp = Path(env["workspace_path"])
    main_ws = tmp / "main4"
    wt = tmp / "wt4"
    (wt / "docs").mkdir(parents=True, exist_ok=True)
    (wt / "docs" / "spec.md").write_text("# spec\n", encoding="utf-8")
    task = _gate_task(str(wt))
    evidence = {"files_changed": ["src/stale_wrong.py"]}
    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        new=AsyncMock(return_value=str(main_ws)),
    ):
        deny, meta = await review_worktree_gate(
            PROJECT_ID, task, evidence,
            reviewer_files=["docs/spec.md"],
        )
    assert meta.get("files_changed_source") == "reviewer_files_changed", meta
    assert deny is None, deny
    assert meta.get("skipped") == "doc_review_tier", meta


# ── 任务6：acceptance_criteria 覆盖门 ───────────────────────────────


def test_parse_acceptance_items_shapes():
    assert parse_acceptance_items(None) == []
    assert parse_acceptance_items(["a", " b "]) == ["a", "b"]
    assert parse_acceptance_items('["x","y"]') == ["x", "y"]
    dicts = [{"text": "t1", "required": True}, {"text": "t2"}]
    assert parse_acceptance_items(dicts) == ["t1", "t2"]


def test_uncovered_items_state_judgment():
    """#14：判据是「结构化 id 声明 + 已核验凭证」，**文本一律不算覆盖**。

    同步形态不做核验 ⇒ 未传 ``verified_ids`` 时 fail-closed（门禁走 async 版
    ``uncovered_acceptance_items_verified``，见 tests/test_acceptance_state_gate.py）。
    """
    criteria = ["导出 CSV 功能可用", "边界情况有测试覆盖"]
    # 全缺：含照抄原文 / 编号引用 / 裸 N/A 三种旧"覆盖"文本形态
    # （同步形态 fail-closed，故 gap 里多一条「未接线」自诊断）
    for ev in (
        {"summary": "跑了一遍"},
        {"summary": "验证完成：导出 CSV 功能可用；边界情况有测试覆盖"},
        {"summary": "条目1 实测通过；条目2 N/A: 本环境无边界数据集"},
    ):
        gaps = uncovered_acceptance_items(criteria, ev)
        assert len(gaps) == 3, (ev, gaps)
        assert [g for g in gaps if g.startswith("条目")] == [
            "条目1: 导出 CSV 功能可用", "条目2: 边界情况有测试覆盖",
        ], gaps
    # 结构化声明 + 调用方已核验的凭证 id ⇒ 覆盖
    covered = uncovered_acceptance_items(
        criteria,
        {"acceptance_coverage": {
            "1": {"attestation_ids": ["a1"]},
            "2": {"attestation_ids": ["a2"]},
        }},
        verified_ids={"a1", "a2"},
    )
    assert covered == []
    # 只声明了 id、凭证未核验 ⇒ 该条仍点名（声明 ≠ 自述放行）
    partial = uncovered_acceptance_items(
        criteria,
        {"acceptance_coverage": {"1": {"attestation_ids": ["a1"]}}},
        verified_ids={"a1"},
    )
    assert len(partial) == 1 and "条目2" in partial[0], partial
    # 拒绝文案带处方（指结构化的覆盖声明）
    msg = format_acceptance_coverage_error(partial)
    assert "acceptance_coverage" in msg and "test_run" in msg and "N/A" in msg


@pytest.mark.asyncio
async def test_service_submit_rejects_uncovered_checklist(env):
    """E1 处：清单缺覆盖 → ValueError 点名缺条；文本形态一律不算覆盖（#14）。"""
    svc = TaskService()
    pid = env["project_id"]
    criteria = ["导出 CSV 功能可用", "边界情况有测试覆盖"]
    vid = await _mk_running_task(
        env, svc, title="VERIFY: 导出", tags=["verify"],
        acceptance_criteria=criteria,
        kind=VERIFY_KIND,
    )
    with pytest.raises(ValueError) as ei:
        await svc.submit_task(pid, vid, evidence={"verdict": "PASS"})
    msg = str(ei.value)
    assert "条目1" in msg and "条目2" in msg, msg
    assert (await svc.get_task(pid, vid))["status"] == "running"

    # 照抄条目原文 / 编号引用 / 裸 N/A 三种旧"覆盖"形态 → 全部仍被拒
    for ev in (
        {"verdict": "PASS",
         "summary": "导出 CSV 功能可用；边界情况有测试覆盖"},
        {"verdict": "PASS", "summary": "第1条 通过；第2条 N/A：无数据集"},
    ):
        with pytest.raises(ValueError):
            await svc.submit_task(pid, vid, evidence=ev)
        assert (await svc.get_task(pid, vid))["status"] == "running"


@pytest.mark.asyncio
async def test_service_submit_na_and_index_text_rejected(env):
    """#14（改断言）：编号引用 + 显式 N/A 文本**不再放行**（放行须平台凭证/
    waiver 行）；无清单任务不受门影响。"""
    svc = TaskService()
    pid = env["project_id"]
    criteria = ["导出 CSV 功能可用", "边界情况有测试覆盖"]
    vid = await _mk_running_task(
        env, svc, title="VERIFY: 导出2", tags=["verify"],
        acceptance_criteria=criteria,
        kind=VERIFY_KIND,
    )
    with pytest.raises(ValueError) as ei:
        await svc.submit_task(
            pid, vid,
            evidence={
                "verdict": "PASS",
                "blocking_issues": [],
                "summary": "条目1 通过；条目2 N/A: 本环境无边界数据集，已人工抽查",
            },
        )
    assert "acceptance_coverage" in str(ei.value)
    assert (await svc.get_task(pid, vid))["status"] == "running"

    # 无 acceptance_criteria 的 VERIFY 不受门影响
    vid2 = await _mk_running_task(env, svc, title="VERIFY: 无清单", tags=["verify"], kind=VERIFY_KIND)
    await svc.submit_task(pid, vid2, evidence={"verdict": "PASS"})
    assert (await svc.get_task(pid, vid2))["status"] == "submitted"


# ── #14 端到端（"修了≠生效"的判据）：声明 id + 本任务执行凭证 ⇒ 真提交成功 ─


@pytest.mark.asyncio
async def test_service_submit_covered_by_task_credentials(env):
    """逐条声明 id + 各自锚一条**本任务真 test_run 凭证** ⇒ submitted。"""
    svc = TaskService()
    pid = env["project_id"]
    criteria = ["导出 CSV 功能可用", "边界情况有测试覆盖"]
    vid = await _mk_running_task(
        env, svc, title="VERIFY: 导出3", tags=["verify"],
        acceptance_criteria=criteria,
        kind=VERIFY_KIND,
    )
    a1 = await att_module.attestation_service.create(
        pid, agent_id=EXEC_ID, kind="test_run", task_id=vid,
        command_or_url="uv run pytest tests/test_export.py -q", exit_code=0,
        stdout="54 passed",
    )
    a2 = await att_module.attestation_service.create(
        pid, agent_id=EXEC_ID, kind="test_run", task_id=vid,
        command_or_url="uv run pytest tests/test_edge.py -q", exit_code=0,
        stdout="9 passed",
    )
    await svc.submit_task(
        pid, vid,
        evidence={
            "verdict": "PASS",
            # 措辞与条目原文完全无关 —— 判据不看文本
            "summary": "both items verified against the real runs",
            "acceptance_coverage": {
                "1": {"attestation_ids": [a1]},
                "2": {"attestation_ids": [a2]},
            },
        },
    )
    assert (await svc.get_task(pid, vid))["status"] == "submitted"


@pytest.mark.asyncio
async def test_service_submit_ui_item_covered_by_browse_e2e(env):
    """误拒方向：UI 类 VERIFY（policy=ui_browser_e2e）由 browse_e2e 覆盖 ⇒ 提交成功。"""
    svc = TaskService()
    pid = env["project_id"]
    vid = await _mk_running_task(
        env, svc, title="VERIFY: 登录页", tags=["verify"],
        policy_id="ui_browser_e2e",
        acceptance_criteria=["登录页可点通并截图"],
        kind=VERIFY_KIND,
    )
    aid = await att_module.attestation_service.create(
        pid, agent_id=EXEC_ID, kind="browse_e2e", task_id=vid,
        command_or_url="browse http://localhost:3000", exit_code=0, stdout="ok",
    )
    await svc.submit_task(
        pid, vid,
        evidence={"verdict": "PASS",
                  "acceptance_coverage": {"1": {"attestation_ids": [aid]}}},
    )
    assert (await svc.get_task(pid, vid))["status"] == "submitted"


@pytest.mark.asyncio
async def test_service_submit_soft_policy_accepts_execution_kinds(env):
    """soft policy（VERIFY 常态 coordinator_review）下 browse_e2e 也构成覆盖 ——

    回落"全部执行类 kind"而不是只认 test_run，避免真交付被误拒。"""
    svc = TaskService()
    pid = env["project_id"]
    vid = await _mk_running_task(
        env, svc, title="VERIFY: 冒烟", tags=["verify"],
        acceptance_criteria=["冒烟脚本可跑通"],
        kind=VERIFY_KIND,
    )
    aid = await att_module.attestation_service.create(
        pid, agent_id=EXEC_ID, kind="browse_e2e", task_id=vid,
        command_or_url="browse http://localhost:3000/smoke", exit_code=0,
        stdout="ok",
    )
    await svc.submit_task(
        pid, vid,
        evidence={"verdict": "PASS",
                  "acceptance_coverage": {"1": {"attestation_ids": [aid]}}},
    )
    assert (await svc.get_task(pid, vid))["status"] == "submitted"


# ── 审计修复：P0-1 tool_failure 自批放行 / P1-1 穿越防御 / P2 注入防御 ─


async def _submitted_task_with_doc(env, svc) -> str:
    """建一条已 submitted 的普通任务，交付物为 worktree 中真实存在的文档。"""
    ws = Path(env["workspace_path"])
    wt = ws / "wt-e901"
    (wt / "docs").mkdir(parents=True, exist_ok=True)
    (wt / "docs" / "plan.md").write_text("# plan\n", encoding="utf-8")
    tid = await _mk_running_task(env, svc)
    await svc.submit_task(
        env["project_id"], tid,
        evidence={"tests_passed": True, "files_changed": ["docs/plan.md"]},
    )
    assert (await svc.get_task(env["project_id"], tid))["status"] == "submitted"
    return tid


@pytest.mark.asyncio
async def test_tool_failure_waiver_self_approve_allowed(env):
    """P0-1：waive(kind=tool_failure) → waived_by 自己 approve 成功（放行+留痕）。"""
    svc = TaskService()
    tid = await _submitted_task_with_doc(env, svc)
    await create_waiver(
        env["project_id"], task_id=tid,
        waived_by=env["coordinator_id"],
        reason="audit LLM transient outage", kind="tool_failure",
    )
    with patch(
        "hiveweave.tools.helpers.get_project_id",
        new=AsyncMock(return_value=PROJECT_ID),
    ):
        result = await review_task_tool(
            ReviewTaskParams(taskId=tid, decision="approve"),
            env["coordinator_id"], env["workspace_path"],
        )
    assert result.success is True, result.error
    out = result.output or ""
    assert "VERDICT: APPROVE" in out, out
    # 回执明示豁免类型与依据
    assert "工具失败类豁免" in out, out
    assert "保留审批权" in out, out
    task = await svc.get_task(env["project_id"], tid)
    assert task["status"] == "approved"
    ev = task.get("evidence") or {}
    assert ev.get("waiver_tool_failure_self_approve") is True, ev
    assert ev.get("override") == "waiver_tool_failure_self_approve", ev


@pytest.mark.asyncio
async def test_quality_waiver_self_approve_still_rejected(env):
    """回归：quality（默认）waiver 的发起人自批仍被拒，拒绝文案逐字保持。"""
    svc = TaskService()
    tid = await _submitted_task_with_doc(env, svc)
    await create_waiver(
        env["project_id"], task_id=tid,
        waived_by=env["coordinator_id"],
        reason="real quality exemption", kind="quality",
    )
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.services.unblock_soft.is_small_team_sole_reviewer",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.unblock_soft.no_lawful_approver",
            new=AsyncMock(return_value=""),
        ),
    ):
        result = await review_task_tool(
            ReviewTaskParams(taskId=tid, decision="approve"),
            env["coordinator_id"], env["workspace_path"],
        )
    assert result.success is False
    err = result.error or ""
    assert "Cannot approve: you issued the waiver for this task." in err, err
    # 未放行：任务仍停留 submitted
    assert (await svc.get_task(env["project_id"], tid))["status"] == "submitted"


def test_doc_path_traversal_defenses():
    """P1-1 单元：.. 段一律不判文档；normpath 后再判 docs/ 前缀。"""
    assert _has_parent_segment("docs/../src/auth.py")
    assert not _has_parent_segment("docs/sub/x.md")
    assert _is_doc_path("docs/guide.md")
    assert _is_doc_path("docs/./guide.md")
    assert not _is_doc_path("docs/../src/auth.py")
    assert not _is_doc_path("docs/sub/../../src/note.md")  # 归一后 src/ + .md 也不行
    assert not _is_doc_path("src/auth.py")


@pytest.mark.asyncio
async def test_traversal_path_refused_doc_tier_falls_back_to_compare(env):
    """P1-1：docs/../src/auth.py 拒进 doc 分档，落回原 compare 门裁决。"""
    tmp = Path(env["workspace_path"])
    main_ws = tmp / "mainT"
    wt = tmp / "wtT"
    (wt / "src").mkdir(parents=True, exist_ok=True)
    (wt / "src" / "auth.py").write_text("x = 1\n", encoding="utf-8")
    task = _gate_task(str(wt))
    evidence = {"files_changed": ["docs/../src/auth.py"]}
    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        new=AsyncMock(return_value=str(main_ws)),
    ):
        deny, meta = await review_worktree_gate(PROJECT_ID, task, evidence)
    # 未进 doc 分档（存在即放行通道被关死）
    assert meta.get("doc_review_tier") is None, meta
    assert meta.get("skipped") != "doc_review_tier", meta
    assert meta.get("path_traversal_suspect") == ["docs/../src/auth.py"], meta
    # 落回 compare：MAIN 缺该文件 → diverged → 原门放行（同代码任务）
    assert deny is None, deny


@pytest.mark.asyncio
async def test_claim_check_dash_commit_skipped(env):
    """P2：evidence.commit 以 '-' 开头 → 该主张 skipped，git show 不执行。"""
    ws = Path(env["workspace_path"])
    task = {"id": "t-dash", "title": "VERIFY: x", "assignee_id": None}
    evidence = {
        "commit": "-oProxyCommand=evil",
        "verdict": "FAIL",
        "blocking_issues": ["src/auth.py 缺少 token_refresh 处理"],
    }
    with (
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            new=AsyncMock(return_value=str(ws)),
        ),
        patch(
            "hiveweave.services.git_worktree._git",
            new=AsyncMock(),
        ) as git_mock,
    ):
        results = await run_verdict_claim_check(PROJECT_ID, task, evidence)
    assert len(results) == 1, results
    assert results[0]["result"] == "skipped", results
    assert "选项注入" in results[0]["detail"], results
    git_mock.assert_not_awaited()
