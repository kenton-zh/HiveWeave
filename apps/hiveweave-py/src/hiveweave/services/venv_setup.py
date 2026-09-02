"""E9 (复盘 P1)：venv 产品化 —— 项目创建时初始化 workspace 内 .venv。

复盘 A236：agent 用 ``uv pip install --target`` 民间姿势装 Python 依赖，未进
虚拟环境，环境不可见/不可复现/污染源码树。产品化路径：
1. 项目创建时在 workspace 根初始化 ``.venv``（进 gitignore —— 模板已包含
   ``.venv/``/``venv/``，此处对既存仓库补条目兜底）；
2. bash / python_script 等工具优先解析 ``.venv`` 解释器（``project_venv_python``）；
3. 纯 best-effort：初始化失败只告警，绝不阻断项目创建（fail-open）。
"""

from __future__ import annotations

import re
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


# ── 39 审计 P0-1 第二步：契约 deps 代装 ─────────────────────────────
# 平台以宿主令牌把契约声明的依赖装进项目 .venv——agent 沙箱写不进依赖树
# 是缺路（EPERM），代装 = 平台代跑不靠自觉（与冒烟门同哲学）。
# 安全边界：命令由平台拼装（每个 dep 过安全正则，禁 flag/URL），uv 从
# 官方 index 拉——不执行项目侧提供的任意命令行。

_SAFE_DEP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*(\[[a-zA-Z0-9_,\-]+\])?([=<>!~]+[A-Za-z0-9._\-*,]+([=<>!~]+[A-Za-z0-9._\-*,]+)*)?")


def validate_deps(deps: list) -> tuple[list[str], list[str]]:
    """拆分安全/不安全的依赖声明。禁 flag（-e/--index-url）与 URL 形态。"""
    ok: list[str] = []
    bad: list[str] = []
    for d in deps or []:
        d = str(d).strip()
        if d and not d.startswith("-") and _SAFE_DEP_RE.fullmatch(d):
            ok.append(d)
        else:
            bad.append(d)
    return ok, bad


async def install_project_deps_async(
    workspace: str | Path | None, deps: list, *, timeout_s: float = 300.0
) -> dict:
    """契约 deps 代装：``uv pip install --python <project .venv> <deps>``。

    宿主令牌执行（与 venv_setup 同信任级）；输出尾部随结果返回。
    Returns ``{"ok": bool, "exit": int|None, "output": str, "reason": str}``。
    """
    import asyncio

    ok_deps, bad = validate_deps(deps)
    if bad:
        return {
            "ok": False,
            "exit": None,
            "output": "",
            "reason": f"unsafe dep spec rejected: {bad}",
        }
    if not ok_deps:
        return {"ok": True, "exit": 0, "output": "", "reason": "no deps"}
    venv_dir = project_venv_dir(workspace)
    if not _venv_python(venv_dir).exists():
        return {
            "ok": False,
            "exit": None,
            "output": "",
            "reason": "project .venv 不存在（venv_setup 未完成或失败）",
        }
    cmd = ["uv", "pip", "install", "--python", str(_venv_python(venv_dir)), *ok_deps]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(venv_dir.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return {
                "ok": False,
                "exit": None,
                "output": "",
                "reason": f"deps install timeout (>{timeout_s:.0f}s)",
            }
        output = (out or b"").decode("utf-8", errors="replace")
        ok = proc.returncode == 0
        log.info(
            "project_deps_installed",
            workspace=str(venv_dir.parent),
            deps=ok_deps,
            ok=ok,
            exit_code=proc.returncode,
        )
        return {
            "ok": ok,
            "exit": proc.returncode,
            "output": output[-2000:],
            "reason": "" if ok else f"uv pip install failed (exit={proc.returncode})",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "exit": None,
            "output": "",
            "reason": "uv 不在 PATH——宿主需安装 uv（venv_setup 同前置）",
        }
    except Exception as e:  # noqa: BLE001 — 代装失败不阻断建任务
        return {"ok": False, "exit": None, "output": "", "reason": str(e)}
