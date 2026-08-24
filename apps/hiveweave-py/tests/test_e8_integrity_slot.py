"""E8 复盘验收：merge 后整体性检查槽（软件实例 = 静默吞错扫描）。

复盘致命链三 F1：except Exception 挂空 APIRouter → 启动零报错 → /_admin 404。
修复锁定：
- scan_silent_swallow：except 收敛块内空 APIRouter/空兜底/仅 pass → 红牌；
  显式日志/上抛放行。
- auto_submit_running_task_after_merge：merge 成功路径先跑整体性检查；
  FAIL → auto-submit evidence 前置 verdict=FAIL + blocking_issues，由 E2
  强制 rework（验收「人为注入吞错模式 → 检查 FAIL → 任务转 rework」）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services import task as task_module
from hiveweave.services.git_worktree.integrity import (
    IntegrityReport,
    run_integrity_checks,
    scan_silent_swallow,
)
from hiveweave.services.git_worktree.service_merge import (
    auto_submit_running_task_after_merge,
)
from hiveweave.services.task import TaskService

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


# ── 静态扫描器 ───────────────────────────────────────────────


def test_scan_flags_f1_router_swallow():
    """F1 指纹：except Exception 内挂空 APIRouter → 红牌。"""
    code = (
        "try:\n"
        "    from app.admin.router import router as admin_router\n"
        "except Exception:\n"
        "    from fastapi import APIRouter\n"
        "    admin_router = APIRouter()\n"
    )
    issues = scan_silent_swallow(code, "app/main.py")
    assert any("静默降级兜底" in i and "app/main.py" in i for i in issues)


def test_scan_flags_pass_only_swallow():
    """except 内仅 pass、无日志/上抛 → 红牌（静默吞错）。"""
    code = "try:\n    x = risky()\nexcept Exception:\n    pass\n"
    issues = scan_silent_swallow(code, "service.py")
    assert any("仅 pass" in i for i in issues)


def test_scan_allows_explicit_handling():
    """显式降级（log/raise）→ 放行。"""
    code = (
        "try:\n"
        "    x = risky()\n"
        "except Exception as e:\n"
        "    log.warning('x_failed', error=str(e))\n"
        "    raise\n"
    )
    assert scan_silent_swallow(code, "a.py") == []


def test_scan_allows_clean_code():
    """无吞错的正常 try/except（含三引号/空行干扰）→ 不误报。"""
    code = (
        '"""Module docstring with except: mention"""\n'
        "try:\n"
        "    import fastapi\n"
        "except ImportError:\n"
        "    raise RuntimeError('deps missing')\n"
        "\n"
        "def f():\n"
        "    return APIRouter()\n"
    )
    assert scan_silent_swallow(code, "a.py") == []


# ── merge 成功路径接线：FAIL → verdict=FAIL → E2 rework ───────


async def _mk_running_verify(ts: TaskService, pid: str) -> tuple[str, str]:
    """建 running VERIFY 任务，返回 (task_id, branch)。"""
    vid = await ts.create_task(
        pid,
        "VERIFY: merge integrity",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        tags=["verify", "mandatory"],
        source="system",
    )
    await ts.claim_task(pid, vid, EXEC, bypass_verify_serialize=True)
    await ts.start_task(pid, vid)
    branch = f"hw/EXEC/t-{vid[:8].lower()}"
    return vid, branch


async def _run_auto_submit(
    pid: str, workspace: str, branch: str, integrity: IntegrityReport
):
    with (
        patch(
            "hiveweave.services.git_worktree.integrity.run_integrity_checks",
            new=AsyncMock(return_value=integrity),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
    ):
        Org.return_value.list_agents = AsyncMock(
            return_value=[{"id": EXEC, "short_id": "EXEC"}]
        )
        return await auto_submit_running_task_after_merge(
            pid,
            workspace,
            branch=branch,
            short_id="EXEC",
            merged_by="test-merger",
            merge_commit="abc123",
            already_on_main=True,
        )


@pytest.mark.asyncio
async def test_auto_submit_injects_fail_verdict_on_integrity_fail(task_env):
    """人为注入吞错模式 → 整体性检查 FAIL → evidence 带 verdict=FAIL。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid, branch = await _mk_running_verify(ts, pid)

    fail_report = IntegrityReport(
        checks=["software: silent-swallow scan"],
        issues=["app/main.py:50 静默降级兜底（except 内挂空 APIRouter）"],
    )
    n, titles = await _run_auto_submit(
        pid, task_env["workspace"], branch, fail_report
    )
    assert n == 1
    task = await ts.get_task(pid, vid)
    ev = task["evidence"]
    assert ev["verdict"] == "FAIL"
    assert "app/main.py" in ev["blocking_issues"][0]
    assert ev["integrity_check"] == "fail"


@pytest.mark.asyncio
async def test_auto_submit_fail_verdict_drives_rework_via_e2(task_env):
    """FAIL evidence 的 auto-submit 任务，approve 后被 E2 强制 rework（验收闭环）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid, branch = await _mk_running_verify(ts, pid)

    fail_report = IntegrityReport(
        checks=["software: silent-swallow scan"],
        issues=["app/main.py:50 静默降级兜底"],
    )
    await _run_auto_submit(pid, task_env["workspace"], branch, fail_report)

    assert (await ts.get_task(pid, vid))["status"] == "submitted"
    await ts.start_review(pid, vid)
    await ts.review_task(pid, vid, "approve")
    after = await ts.get_task(pid, vid)
    # E2：verdict=FAIL 的 approve → rework → running（不是 approved/closed）
    assert after["status"] == "running"
    assert after["status"] not in ("approved", "closed")


@pytest.mark.asyncio
async def test_auto_submit_clean_passes_without_verdict(task_env):
    """整体性检查通过 → evidence 无 verdict 注入（integrity_check=pass）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    vid, branch = await _mk_running_verify(ts, pid)

    ok_report = IntegrityReport(checks=["software: silent-swallow scan"])
    await _run_auto_submit(pid, task_env["workspace"], branch, ok_report)
    task = await ts.get_task(pid, vid)
    ev = task["evidence"]
    # 整体性检查通过 → PASS verdict + pass 回执；approve 走正常 close
    assert ev.get("verdict") == "PASS"
    assert ev.get("integrity_check") == "pass"


@pytest.mark.asyncio
async def test_run_integrity_checks_no_workspace_fail_open():
    """无 workspace → 回执标记 NOT scanned 但 fail-open（不炸）。"""
    report = await run_integrity_checks(None, branch="hw/EXEC/t-abc")
    assert report.passed is False
    assert any("skipped" in i for i in report.issues)