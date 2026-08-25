"""E9 (复盘 P1)：venv 产品化 —— 项目创建时初始化 workspace 内 .venv。

复盘 A236：agent 用 ``uv pip install --target`` 民间姿势装 Python 依赖，未进
虚拟环境，环境不可见/不可复现/污染源码树。产品化路径：
1. 项目创建时在 workspace 根初始化 ``.venv``（进 gitignore —— 模板已包含
   ``.venv/``/``venv/``，此处对既存仓库补条目兜底）；
2. bash / python_script 等工具优先解析 ``.venv`` 解释器（``project_venv_python``）；
3. 纯 best-effort：初始化失败只告警，绝不阻断项目创建（fail-open）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

VENV_DIR_NAME = ".venv"

# 初始化超时：uv venv / python -m venv 均应在秒级内完成；超时放弃不阻塞。
_VENV_INIT_TIMEOUT_S = 120.0

_GITIGNORE_ENTRY = VENV_DIR_NAME + "/"


def project_venv_dir(workspace: str | Path) -> Path:
    """项目 .venv 目录（不校验存在性）。"""
    return Path(workspace) / VENV_DIR_NAME


def _venv_python(venv_dir: Path) -> Path:
    """按平台定位 .venv 解释器。"""
    if sys.platform.startswith("win"):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def project_venv_python(workspace: str | Path | None) -> str | None:
    """项目 .venv 解释器路径；不存在/不可用 → None（调用方回退系统 python）。"""
    if not workspace:
        return None
    try:
        py = _venv_python(project_venv_dir(workspace))
        if py.is_file():
            return str(py)
    except Exception:
        pass
    return None


def _ensure_gitignore(ws: Path) -> None:
    """对既存仓库的根 .gitignore 补 ``.venv/`` 条目（模板已含时为 no-op）。

    注意 ``venv/``（标准模板）匹配的是名为 ``venv`` 的目录，**不能**覆盖
    ``.venv`` —— 只有 ``.venv`` / ``.venv/`` 才算已覆盖（审计 E9 M1）。
    """
    try:
        gi = ws / ".gitignore"
        if not gi.is_file():
            return  # 无 .gitignore 的裸目录：不代建，避免越权
        text = gi.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if line.strip() in (VENV_DIR_NAME, _GITIGNORE_ENTRY):
                return
        gi.write_text(
            text.rstrip("\n") + "\n\n# Python 虚拟环境（E9 venv 产品化）\n"
            + _GITIGNORE_ENTRY + "\n",
            encoding="utf-8",
        )
        log.info("venv_gitignore_entry_added", workspace=str(ws))
    except Exception as e:
        log.debug("venv_gitignore_entry_failed", workspace=str(ws), error=str(e))


def ensure_project_venv(workspace: str | Path | None) -> bool:
    """best-effort 初始化 workspace 内 .venv；成功 → True（解释器存在）。

    优先 ``uv venv``（项目既有工具链），缺 uv 回退 stdlib ``venv``。
    已存在时只做 gitignore 兜底 + 校验解释器。任何失败 → False 且仅告警。
    """
    if not workspace:
        return False
    ws = Path(workspace)
    if not ws.is_dir():
        return False
    venv_dir = project_venv_dir(ws)
    try:
        if not venv_dir.exists():
            uv = shutil.which("uv")
            if uv:
                subprocess.run(
                    [uv, "venv", str(venv_dir), "--quiet"],
                    check=True,
                    capture_output=True,
                    timeout=_VENV_INIT_TIMEOUT_S,
                )
            else:
                import venv as _venv

                _venv.EnvBuilder(with_pip=True).create(str(venv_dir))
            log.info("venv_initialized", workspace=str(ws), via="uv" if uv else "stdlib")
        _ensure_gitignore(ws)
        return _venv_python(venv_dir).is_file()
    except Exception as e:
        log.warning(
            "venv_init_failed",
            workspace=str(ws),
            error=f"{type(e).__name__}: {e}"[:300],
        )
        return False


async def ensure_project_venv_async(workspace: str | Path | None) -> bool:
    """线程内执行初始化（venv 创建为 CPU/IO 混合，不阻塞事件循环）。"""
    import asyncio

    try:
        started = time.monotonic()
        ok = await asyncio.to_thread(ensure_project_venv, workspace)
        log.info(
            "project_venv_init_done",
            workspace=str(workspace) if workspace else None,
            ok=ok,
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return ok
    except Exception:
        return False