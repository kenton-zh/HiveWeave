"""ACL 沙箱哨兵探针（spec §13 P1 可测判据）。

沙箱 on 时后端周期性从各入口注入探针，断言**受限令牌行为**生效。探针不用
``whoami /groups | findstr S-1-4``（实测本机受限令牌不暴露 restricting SID），
改用两个内核为真的行为判别：

1. **TEMP 标记**（bash / run_command 入口，走真实入口函数，抓接线回归）：
   ``echo $env:TEMP`` —— 受限下 TEMP 指向私有 sandbox-temp（含 "sandbox-temp"），
   native 指向用户 %TEMP%；输出含标记 = 受限，缺 = native 泄漏。
2. **`.hiveweave` 写拒绝**（bash_main / alarm 入口，直接 spawn_confined）：
   项目根角色写 `<root>/.hiveweave/.acl_sentinel` —— 受限被拒（pass-2 落空，
   非零退出）；native 可写（零退出）= 泄漏。

任一入口探针未过 = 判据未达（接线回归），log.error + 遥测。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# TEMP 标记探针：按受限 shell 选语法（pwsh 用 $env:TEMP；cmd 兜底用 %TEMP%）。
# 受限下 TEMP 指向私有 sandbox-temp（含 "sandbox-temp"），native 指向用户 %TEMP%。
_TEMP_MARKER = "sandbox-temp"
_ENTRIES_TEMP = ("bash", "run_command")
_ENTRIES_DENY = ("bash_main", "alarm")


def _temp_probe() -> str:
    import shutil

    return "echo $env:TEMP" if shutil.which("pwsh") else "echo %TEMP%"

_stop = threading.Event()
_task: asyncio.Task | None = None
_guard = threading.Lock()
_last: dict | None = None  # 最近一轮探针结果（测试/管理 API 读取）


def _probe_passed(r: dict | None) -> dict:
    if r is None:
        return {"ok": False, "error": "sandbox returned None (未启用?)"}
    text = (
        (r.get("output") or r.get("stdout") or "")
        + "\n"
        + (r.get("stderr") or "")
    )
    return {
        "ok": _TEMP_MARKER in text,
        "exit": r.get("exit_code"),
        "snippet": text.strip()[-120:],
    }


def _probe_denied(r: dict | None) -> dict:
    """写拒绝探针：受限 = 非零退出（EACCES）；native = 零退出（泄漏）。"""
    if r is None:
        return {"ok": False, "error": "sandbox returned None (未启用?)"}
    return {
        "ok": r.get("exit_code") != 0,
        "exit": r.get("exit_code"),
        "snippet": (r.get("stderr") or r.get("stdout") or "")[-120:],
    }


async def _probe_via_spawn(project_root: str, project_id: str | None, entry: str) -> dict:
    """经 spawn_confined 直接探针（bash_main / alarm 入口角色）：写 .hiveweave 被拒。"""
    from hiveweave.services.acl_sandbox.integration import build_confined_command
    from hiveweave.services.acl_sandbox.service import spawn_confined

    target = str(Path(project_root) / ".hiveweave" / ".acl_sentinel")
    r = await spawn_confined(
        command=build_confined_command(f"echo x > {target}"),
        workdir=project_root,
        workspace_path=project_root,
        agent_id="__sentinel__",
        project_id=project_id,
        project_workspace_path=project_root,
        entry=entry,
        timeout_s=20,
    )
    return _probe_denied(r)


async def _probe_via_tool(project_root: str, project_id: str | None, entry: str) -> dict:
    """经真实工具入口探针（bash / run_command —— 抓接线回归）：TEMP 标记。"""
    if entry == "bash":
        from hiveweave.tools.bash import execute_bash

        r = await execute_bash(
            command=_temp_probe(),
            workdir="",
            workspace_path=project_root,
            project_id=project_id,
            agent_id="__sentinel__",
            timeout_ms=15000,
        )
    else:  # run_command
        from hiveweave.tools.bash import execute_run_command

        r = await execute_run_command(
            command=_temp_probe(),
            cwd="",
            timeout_ms=15000,
            workspace_path=project_root,
            agent_id="__sentinel__",
            project_id=project_id,
        )
    return _probe_passed(r)


async def run_sentinel_probes(project_root: str, project_id: str | None) -> dict:
    """对单个项目根从各入口注入探针。返回 {entry: {ok, …}}。"""
    results: dict = {}
    for entry in _ENTRIES_TEMP:
        try:
            results[entry] = await _probe_via_tool(project_root, project_id, entry)
        except Exception as e:  # fail-closed：探针异常 = 该入口未过
            results[entry] = {"ok": False, "error": str(e)}
    for entry in _ENTRIES_DENY:
        try:
            results[entry] = await _probe_via_spawn(project_root, project_id, entry)
        except Exception as e:  # fail-closed
            results[entry] = {"ok": False, "error": str(e)}
    results["dev_server"] = {"ok": None, "skipped": "E2E 场景 G 覆盖（周期探针不起停服务器）"}
    return results


async def run_probe_all_projects() -> dict:
    """枚举全部项目，逐个项目根探针。返回 {project_root: results}。

    sandbox_mode=danger-full-access（逃生门）的项目跳过 —— 其命令走 native，
    探针必然 None/假阳性，不判接线回归（P3 §9）。
    """
    from hiveweave.db.meta import query
    from hiveweave.services.acl_sandbox.integration import project_sandbox_mode

    rows = await query("SELECT id, workspace_path FROM projects WHERE 1=1")
    out: dict = {}
    for r in rows:
        root = r["workspace_path"]
        if not root:
            continue
        if await project_sandbox_mode(r["id"]) == "danger-full-access":
            out[root] = {"danger-full-access": {"ok": None, "skipped": True}}
            continue
        out[root] = await run_sentinel_probes(root, r["id"])
        bad = [
            e for e, res in out[root].items()
            if res.get("ok") is False
        ]
        if bad:
            log.error(
                "acl_sandbox_sentinel_failed",
                project_id=r["id"], workspace=root, entries=bad,
            )
    return out


def start_sentinel_loop() -> None:
    """沙箱 on 时由 lifespan 启动周期探针循环（幂等）。"""
    global _task
    with _guard:
        if _task is not None and not _task.done():
            return
        _stop.clear()
        _task = asyncio.get_running_loop().create_task(_sentinel_loop())


def stop_sentinel_loop() -> None:
    global _task
    with _guard:
        _stop.set()
        if _task is not None:
            _task.cancel()
            _task = None


async def _sentinel_loop() -> None:
    from hiveweave.config import settings

    interval = max(10, int(getattr(settings, "acl_sentinel_interval_s", 300) or 300))
    # 用 asyncio.sleep 而非 threading.Event.wait —— 后者同步阻塞整个事件循环
    # （Critical：首轮探针后后端全局冻结）。停循环靠 task.cancel。
    while True:
        try:
            global _last
            _last = await run_probe_all_projects()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("acl_sandbox_sentinel_loop_error", error=str(e))
        await asyncio.sleep(interval)


def sentinel_last() -> dict | None:
    """最近一轮探针结果（管理 API / 测试读取）。"""
    return _last


def reset_for_tests() -> None:
    global _last
    _last = None
