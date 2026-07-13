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
import subprocess
import sys
import tempfile

from fastapi import APIRouter, HTTPException
import structlog

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
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if _IS_WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 仅在 Windows subprocess 上存在
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        new_pgrp = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = detached | new_pgrp
    else:
        # POSIX: start_new_session=True → setsid() 脱离父进程组，
        # 行为等价于 nohup + & ，父进程退出不影响子进程
        kwargs["start_new_session"] = True
        # close_fds=True 释放继承的 fd
        kwargs["close_fds"] = True
    subprocess.Popen(argv, **kwargs)


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
