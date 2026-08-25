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
async def test_run_integrity_checks_no_workspace_skips_not_fails():
    """无 workspace → skipped=True（未扫描≠FAIL，调用方不得注入 verdict）。"""
    report = await run_integrity_checks(None, branch="hw/EXEC/t-abc")
    assert report.skipped is True
    assert report.passed is True  # fail-open：不把 skipped 当 FAIL
    assert report.issues == []


# ── 审计追加：except as e / 多异常 / 行序 / 真实 git ────────────


def test_scan_flags_as_e_swallow():
    """主流写法 ``except Exception as e:`` + pass / 挂空兜底 → 红牌。"""
    code = (
        "try:\n"
        "    from app.admin.router import router as admin_router\n"
        "except Exception as e:\n"
        "    admin_router = APIRouter()\n"
    )
    issues = scan_silent_swallow(code, "app/main.py")
    assert any("静默降级兜底" in i for i in issues)

    code2 = "try:\n    x = risky()\nexcept Exception as e:\n    pass\n"
    issues2 = scan_silent_swallow(code2, "s.py")
    assert any("仅 pass" in i for i in issues2)


def test_scan_flags_multi_exception_forms():
    """多异常 / 跨行括号 except 形态同样识别。"""
    code = (
        "try:\n"
        "    x = risky()\n"
        "except (RuntimeError, ValueError):\n"
        "    x = None\n"
    )
    issues = scan_silent_swallow(code, "a.py")
    assert any("静默降级兜底" in i for i in issues)

    multi = (
        "try:\n"
        "    x = risky()\n"
        "except (\n"
        "    RuntimeError,\n"
        "    ValueError,\n"
        "):\n"
        "    x = {}\n"
    )
    issues2 = scan_silent_swallow(multi, "b.py")
    assert any("静默降级兜底" in i for i in issues2)


def test_scan_order_independent_with_explicit_log():
    """赋值行先于日志行 → 显式降级仍放行（行序无关，审计修正）。"""
    code = (
        "try:\n"
        "    from app.admin.router import router as admin_router\n"
        "except Exception:\n"
        "    admin_router = APIRouter()\n"
        "    log.warning('degraded admin router', exc_info=True)\n"
    )
    assert scan_silent_swallow(code, "app/main.py") == []


def test_scan_comment_log_does_not_mask_pass_only():
    """``pass  # log.would_help`` 的注释不算显式处理（审计修正）。"""
    code = "try:\n    x = risky()\nexcept Exception:\n    pass  # log.would_help\n"
    issues = scan_silent_swallow(code, "s.py")
    assert any("仅 pass" in i for i in issues)


@pytest.mark.asyncio
async def test_changed_python_files_real_git_first_parent():
    """真实 git：merge 后 HEAD^1..HEAD 恰列出被引入的 .py（审计实证路径）。"""
    import asyncio
    import subprocess
    import tempfile
    from pathlib import Path

    from hiveweave.services.git_worktree.integrity import _changed_python_files
    from tests.test_idle_architecture_p0 import task_env  # noqa: F401

    def _git(cwd: str, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=cwd, check=True,
            capture_output=True, text=True,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        _git(tmpdir, "init", "-q")
        _git(tmpdir, "config", "user.email", "t@t")
        _git(tmpdir, "config", "user.name", "t")
        (Path(tmpdir) / "main.py").write_text("print(1)\n")
        _git(tmpdir, "add", ".")
        _git(tmpdir, "commit", "-qm", "c1")
        # 第二个提交引入一个带吞错的 .py —— HEAD^1..HEAD 应扫到它
        (Path(tmpdir) / "admin.py").write_text(
            "try:\n    import risky\nexcept Exception:\n    admin = []\n"
        )
        _git(tmpdir, "add", ".")
        _git(tmpdir, "commit", "-qm", "c2")
        files = await _changed_python_files(tmpdir)
        assert "admin.py" in files
        assert "main.py" not in files  # 上一提交的不算本次引入


@pytest.mark.asyncio
async def test_integrity_real_git_fail_via_scan(tmpdir):
    """端到端（审计 E8-1+E8-2 合并）：merge 场景 run_integrity_checks 扫到吞错。"""
    import subprocess
    from pathlib import Path

    from hiveweave.services.git_worktree.integrity import run_integrity_checks

    def _git(cwd: str, *args: str) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    _git(tmpdir, "init", "-q")
    _git(tmpdir, "config", "user.email", "t@t")
    _git(tmpdir, "config", "user.name", "t")
    (Path(tmpdir) / "main.py").write_text("# ok\n")
    _git(tmpdir, "add", ".")
    _git(tmpdir, "commit", "-qm", "c1")
    (Path(tmpdir) / "app.py").write_text(
        "try:\n    import x\nexcept Exception as e:\n    x = APIRouter()\n"
    )
    _git(tmpdir, "add", ".")
    _git(tmpdir, "commit", "-qm", "c2")
    report = await run_integrity_checks(tmpdir, branch="hw/EXEC/t-abc")
    assert report.passed is False
    assert any("app.py" in i for i in report.issues)


# ── 审计 E8-3：非 VERIFY 实现任务 integrity FAIL → E2 rework 闭环 ───


@pytest.mark.asyncio
async def test_non_verify_task_integrity_fail_routes_rework(task_env):
    """普通实现任务带 integrity_check=fail → approve 强制 rework（E8-3 闭环）。

    evidence 不含 verdict——锁定 integrity 触发路径（2026-08-25 审计：
    原用例带 verdict=FAIL，实际测的是 verdict 分支，integrity 分支被掩盖）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature impl", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid,
        tid,
        evidence={
            "blocking_issues": ["app.py:3 静默降级兜底"],
            "integrity_check": "fail",
        },
    )
    await ts.start_review(pid, tid)
    await ts.review_task(pid, tid, "approve")
    after = await ts.get_task(pid, tid)
    assert after["status"] == "running"
    assert after["status"] not in ("approved", "closed")