"""System control endpoints (restart backend / frontend).

Bug-8 修复: 原实现硬编码 Windows .bat + cmd /c + Windows-only creationflags
(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP), 在 Linux/macOS 直接 500。
改为按 platform.system() 走对应路径：
- Windows: .bat + cmd /c + Windows creationflags
- POSIX:   .sh + sh + start_new_session=True (nohup-like 行为)
"""

from __future__ import annotations

import os
import platform
import shlex
import sys
import tempfile

from fastapi import APIRouter, HTTPException, Query
import structlog

from hiveweave.util.win_subprocess import DEVNULL

router = APIRouter(prefix="/api/system", tags=["system"])
log = structlog.get_logger(__name__)

# HiveWeave project root (5 levels up from this file:
# api/ → hiveweave/ → src/ → hiveweave-py/ → apps/ → HiveWeave/)
_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..")
)

_IS_WINDOWS = platform.system() == "Windows"


def _build_restart_command(script_path: str, wait_seconds: int = 2) -> tuple[list[str], str]:
    """构造跨平台"延迟 N 秒后启动脚本"的命令。

    Returns (popen_argv, wrapper_script_path) — 调用方用 subprocess.Popen
    以 detached 模式跑 wrapper_script_path。
    """
    if _IS_WINDOWS:
        # Windows: 写 .bat 包装器（用 timeout 命令）
        wrapper = os.path.join(tempfile.gettempdir(), f"hiveweave_restart_{os.path.basename(script_path)}.bat")
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write(f"timeout /t {wait_seconds} /nobreak >nul\n")
            f.write(f'call "{script_path}"\n')
        return ["cmd", "/c", wrapper], wrapper
    else:
        # POSIX: 写 .sh 包装器（用 sleep 命令）
        wrapper_dir = tempfile.mkdtemp(prefix="hiveweave_restart_")
        wrapper = os.path.join(wrapper_dir, f"restart_{os.path.basename(script_path)}.sh")
        quoted = shlex.quote(script_path)
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
            f.write(f"sleep {wait_seconds}\n")
            f.write(f"{quoted}\n")
        os.chmod(wrapper, 0o755)
        return [wrapper], wrapper


def _spawn_detached(argv: list[str], cwd: str) -> None:
    """跨平台 detached 启动包装器。"""
    kwargs: dict = {
        "cwd": cwd,
        "stdout": DEVNULL,
        "stderr": DEVNULL,
        "stdin": DEVNULL,
    }
    if _IS_WINDOWS:
        from hiveweave.util.win_subprocess import (
            CREATE_NEW_PROCESS_GROUP,
            DETACHED_PROCESS,
        )

        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        # POSIX: start_new_session=True → setsid() 脱离父进程组，
        # 行为等价于 nohup + & ，父进程退出不影响子进程
        kwargs["start_new_session"] = True
        # close_fds=True 释放继承的 fd
        kwargs["close_fds"] = True
    from hiveweave.util.win_subprocess import hidden_popen

    hidden_popen(argv, **kwargs)


@router.post("/restart-backend")
async def restart_backend() -> dict:
    """Restart the backend (uvicorn) process.

    跨平台：
    - Windows 走 start-backend.bat
    - POSIX 走 start-backend.sh
    """
    script_basename = "start-backend.bat" if _IS_WINDOWS else "start-backend.sh"
    script_path = os.path.join(_PROJECT_ROOT, script_basename)
    if not os.path.exists(script_path):
        # POSIX 用户友好回退: 接受 .bat (开发机同时有) 或 .sh
        alt = "start-backend.sh" if _IS_WINDOWS else "start-backend.bat"
        alt_path = os.path.join(_PROJECT_ROOT, alt)
        if os.path.exists(alt_path):
            script_path = alt_path
        else:
            raise HTTPException(
                status_code=500,
                detail=f"start script not found: tried {script_path} and {alt_path}",
            )

    try:
        argv, wrapper = _build_restart_command(script_path, wait_seconds=2)
        _spawn_detached(argv, cwd=_PROJECT_ROOT)
        log.info("system_restart_backend_triggered", wrapper=wrapper, platform=platform.system())
        return {"ok": True, "message": "Backend restarting in 2s..."}
    except Exception as e:
        log.error("system_restart_backend_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/restart-frontend")
async def restart_frontend() -> dict:
    """Restart the frontend (Vite dev server) process.

    跨平台：
    - Windows 走 start-frontend.bat
    - POSIX 走 start-frontend.sh
    """
    script_basename = "start-frontend.bat" if _IS_WINDOWS else "start-frontend.sh"
    script_path = os.path.join(_PROJECT_ROOT, script_basename)
    if not os.path.exists(script_path):
        alt = "start-frontend.sh" if _IS_WINDOWS else "start-frontend.bat"
        alt_path = os.path.join(_PROJECT_ROOT, alt)
        if os.path.exists(alt_path):
            script_path = alt_path
        else:
            raise HTTPException(
                status_code=500,
                detail=f"start script not found: tried {script_path} and {alt_path}",
            )

    try:
        argv, wrapper = _build_restart_command(script_path, wait_seconds=2)
        _spawn_detached(argv, cwd=_PROJECT_ROOT)
        log.info("system_restart_frontend_triggered", wrapper=wrapper, platform=platform.system())
        return {"ok": True, "message": "Frontend restarting in 2s..."}
    except Exception as e:
        log.error("system_restart_frontend_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/code-drift")
async def code_drift_status() -> dict:
    """F14 源码指纹漂移快照（只读：进程内存启动指纹 vs 磁盘当前指纹）。

    返回 ``{drift, checked_at, changed_count, changed_files}``（changed_files
    截前 20 条）。数据全部取自 services.code_fingerprint.code_drift() 现有
    API，不做新计算、无副作用。指纹未初始化（理论上不会——lifespan 启动
    即记录）时 code_drift() fail-open 返回 drift=False + reason，本端点照
    200 透出，不 503（观测端点不该比被观测对象更脆）。
    """
    from datetime import datetime, timezone

    from hiveweave.services.code_fingerprint import code_drift

    d = code_drift()
    changed = list(d.get("changed_files") or [])
    return {
        "drift": bool(d.get("drift")),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "changed_count": len(changed),
        "changed_files": changed[:20],
        "reason": str(d.get("reason") or ""),
        "startup": d.get("startup"),
        "current": d.get("current"),
    }


@router.get("/acl-sandbox-stats")
async def acl_sandbox_stats() -> dict:
    """ACL 沙箱遥测快照（spec §13 P1/P3：fail-closed/拒绝命中/传播/mint P95）。

    同时返回最近一轮哨兵探针结果（若有）与沙箱开关状态，供管理 UI / 测试判据。
    """
    from hiveweave.services.acl_sandbox import telemetry
    from hiveweave.services.acl_sandbox.integration import acl_sandbox_active
    from hiveweave.services.acl_sandbox.sentinel import sentinel_last

    stats = telemetry.snapshot()
    stats["active"] = acl_sandbox_active()
    stats["sentinel_last"] = sentinel_last()
    return stats


@router.get("/verify-efficiency")
async def verify_efficiency(
    project_id: str = Query(..., description="Project id"),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """VERIFY 时长比监控（只读）：最近 N 个 closed VERIFY 任务的
    总时长 / 有效验证时长 比值报告。

    口径见 docs/2026-09-05/verify-efficiency-metric.md；纯只读，
    不改变任何任务行为/门禁。项目不存在时 404。
    """
    from hiveweave.db.project import ProjectDbError
    from hiveweave.services.tasks.verify_efficiency import (
        verify_efficiency_report,
    )

    try:
        return await verify_efficiency_report(project_id, limit=limit)
    except ProjectDbError as e:
        raise HTTPException(status_code=404, detail=str(e))
