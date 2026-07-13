"""System control endpoints (restart backend / frontend)."""

from __future__ import annotations

import os
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


@router.post("/restart-backend")
async def restart_backend() -> dict:
    """Restart the backend (uvicorn) process.

    Spawns a detached batch script that:
    1. Waits 2 seconds (for this HTTP response to complete)
    2. Runs start-backend.bat (kills port 4000, starts fresh uvicorn)
    """
    bat_path = os.path.join(_PROJECT_ROOT, "start-backend.bat")
    if not os.path.exists(bat_path):
        raise HTTPException(status_code=500, detail=f"start-backend.bat not found at {bat_path}")

    # Write a small wrapper batch that waits then runs start-backend.bat
    wrapper = os.path.join(tempfile.gettempdir(), "hiveweave_restart_backend.bat")
    with open(wrapper, "w", encoding="utf-8") as f:
        f.write("@echo off\n")
        f.write("timeout /t 2 /nobreak >nul\n")
        f.write(f'call "{bat_path}"\n')

    try:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            ["cmd", "/c", wrapper],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            cwd=_PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("system_restart_backend_triggered")
        return {"ok": True, "message": "Backend restarting in 2s..."}
    except Exception as e:
        log.error("system_restart_backend_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/restart-frontend")
async def restart_frontend() -> dict:
    """Restart the frontend (Vite dev server) process.

    Spawns a detached batch script that:
    1. Waits 2 seconds (for this HTTP response to complete)
    2. Runs start-frontend.bat (kills node.exe, starts fresh vite)
    """
    bat_path = os.path.join(_PROJECT_ROOT, "start-frontend.bat")
    if not os.path.exists(bat_path):
        raise HTTPException(status_code=500, detail=f"start-frontend.bat not found at {bat_path}")

    wrapper = os.path.join(tempfile.gettempdir(), "hiveweave_restart_frontend.bat")
    with open(wrapper, "w", encoding="utf-8") as f:
        f.write("@echo off\n")
        f.write("timeout /t 2 /nobreak >nul\n")
        f.write(f'call "{bat_path}"\n')

    try:
        subprocess.Popen(
            ["cmd", "/c", wrapper],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            cwd=_PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("system_restart_frontend_triggered")
        return {"ok": True, "message": "Frontend restarting in 2s..."}
    except Exception as e:
        log.error("system_restart_frontend_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
