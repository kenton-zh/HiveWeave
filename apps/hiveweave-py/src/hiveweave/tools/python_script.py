"""E11 (复盘 P1)：python_script —— Python 脚本一等公民工具。

复盘 A236：agent 只能在 bash 里拼 ``python -c`` / here-doc，引号地狱 +
``--target`` 民间姿势（E9 已产品化 .venv）。本工具提供结构化入口：
- ``script`` 或 ``scriptPath``（workspace 相对路径）二选一；
- 解释器优先项目 ``.venv``（E9），缺省回退 ``python``（uv run 语义由
  PATH 上已有的 uv 环境承担）；
- 写临时文件执行（避免 ``-c`` 引号问题），**native 路径** create_subprocess_exec
  直传 argv（不经壳层二次解析）；**沙箱路径**经 ``spawn_confined`` 受限执行
  （受限令牌只能经 pwsh 承载，属壳层包装的必然形式）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hiveweave.tools.base import tool
from hiveweave.tools.bash import (
    MAX_TIMEOUT_S,
    TOOL_DEFAULT_TIMEOUT_MS,
    _truncate_output,
)
from hiveweave.tools.result import ToolResult


class PythonScriptParams(BaseModel):
    """Run Python code inside the project workspace (first-class tool)."""

    script: str | None = Field(
        default=None,
        description=(
            "Python source code to run. Multi-line allowed; executed from a "
            "temp file (no -c quoting issues). Mutually exclusive with "
            "scriptPath (script wins if both given)."
        ),
    )
    scriptPath: str | None = Field(
        default=None,
        alias="scriptPath",
        description=(
            "Path (workspace-relative) to an existing .py file to run. "
            "Mutually exclusive with script."
        ),
    )
    timeout: int | None = Field(
        default=None,
        description=(
            "Timeout ms (5s–10min). Default 120000 (2 min). Values 1-600 "
            "are treated as seconds (30 = 30s)."
        ),
    )

    model_config = {"populate_by_name": True}


async def _resolve_interpreter(workspace: str) -> str:
    """E9 集成：项目 .venv 解释器优先，缺省 python（PATH）。"""
    try:
        from hiveweave.services.venv_setup import project_venv_python

        venv_py = project_venv_python(workspace)
        if venv_py:
            return venv_py
    except Exception:
        pass
    return "python"


async def _run_native_argv(argv: list[str], cwd: str, timeout_s: int | None) -> dict[str, Any]:
    """native 执行：create_subprocess_exec 直传 argv（不经过壳层）。"""
    try:
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            **windows_no_window_kwargs(),
        )
    except (FileNotFoundError, OSError) as exc:
        return {"output": "", "stdout": "", "stderr": "",
                "exit_code": None, "timed_out": False,
                "error": f"Failed to spawn python: {exc}"}
    try:
        if timeout_s is None or timeout_s <= 0:
            out_b, err_b = await proc.communicate()
        else:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        return {
            "output": out_b.decode("utf-8", errors="replace")
                      + ("\n" if out_b else "")
                      + err_b.decode("utf-8", errors="replace"),
            "stdout": out_b.decode("utf-8", errors="replace"),
            "stderr": err_b.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode,
            "timed_out": False,
        }
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()  # 回收句柄，防 Windows 僵尸残留
        except Exception:
            pass
        return {"output": "", "stdout": "", "stderr": "",
                "exit_code": None, "timed_out": True,
                "error": "Timed out"}


async def python_script_execute(
    params: PythonScriptParams,
    agent_id: str,
    workspace: str,
) -> ToolResult:
    """Run a Python script in the project workspace."""
    from hiveweave.tools.helpers import get_project_id

    if not workspace:
        return ToolResult.err("python_script requires a workspace")

    script = (params.script or "").strip()
    script_path: str | None = params.scriptPath or None
    if not script and not script_path:
        return ToolResult.err(
            "python_script requires 'script' (source code) or 'scriptPath' "
            "(path to a .py file)"
        )

    # 解析超时（数值 1-600 视为秒）
    # A-2 (P1-4): 未显式给超时时按工具声明取默认（python_script 300s）。
    timeout_ms = int(params.timeout or TOOL_DEFAULT_TIMEOUT_MS["python_script"])
    if 1 <= timeout_ms <= 600:
        timeout_ms = timeout_ms * 1000
    timeout_ms = max(5_000, min(timeout_ms, MAX_TIMEOUT_S * 1000))
    timeout_s = timeout_ms / 1000

    # 写临时脚本文件（.hiveweave/tool_outputs → 不入库不被误扫）
    root = Path(workspace)
    tool_out_dir = root / ".hiveweave" / "tool_outputs"
    try:
        tool_out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        tool_out_dir = root  # 目录不可写时退到 workspace 根（仍受沙箱约束）

    if script:
        src = script
        fname = f"py_{agent_id.split('-')[-1][:8]}_{uuid.uuid4().hex[:8]}.py"
    else:
        if not script_path:
            return ToolResult.err("python_script: scriptPath required")
        try:
            resolved = (root / script_path).resolve()
            if not str(resolved).startswith(str(root.resolve())):
                return ToolResult.err(
                    f"python_script: scriptPath must stay inside workspace: {script_path}"
                )
            src = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult.err(
                f"python_script: cannot read scriptPath {script_path!r}: {e}"
            )
        fname = Path(script_path).name

    script_file = tool_out_dir / fname
    stderr_hint = ""
    try:
        script_file.write_text(src, encoding="utf-8")
    except Exception as e:
        return ToolResult.err(f"python_script: cannot write temp script: {e}")

    interp = await _resolve_interpreter(workspace)
    argv = [interp, str(script_file)]
    try:
        # ACL 沙箱 on → 受限执行（经 spawn_confined，argv 逐元素引用）
        from hiveweave.services.acl_sandbox.integration import (
            acl_sandbox_active,
            build_confined_argv,
            resolve_project_root,
        )
        from hiveweave.services.acl_sandbox.service import spawn_confined

        if acl_sandbox_active():
            project_id = await get_project_id(agent_id)
            project_root = await resolve_project_root(project_id)
            # DSH_33 P0：受限路径经 pwsh 承载，`"interp" "script"` 在 pwsh 里是
            # ParserError（第二个引号串没有调用运算符）——实测 7/7 全失败。
            # 用 dialect="pwsh" 直传并显式加 `&` 调用运算符，且**不**再经
            # _normalize_for_pwsh（那会把路径里的 $ 之类当 bash 变量改写）。
            result = await spawn_confined(
                argv=build_confined_argv(
                    f'& "{interp}" "{script_file}"', dialect="pwsh"
                ),
                workdir=workspace,
                workspace_path=workspace,
                agent_id=agent_id,
                project_id=project_id,
                project_workspace_path=project_root,
                timeout_s=timeout_s,
                entry="python_script",
            )
            if result is None:
                return ToolResult.err("python_script: sandbox unavailable")
            if result.get("long_running"):
                return ToolResult.err("python_script: background unsupported")
            result = {
                "output": "",
                "stdout": result.get("stdout", "") or "",
                "stderr": result.get("stderr", "") or "",
                "exit_code": result.get("exit_code"),
                "timed_out": bool(result.get("timed_out", False)),
            }
        else:
            result = await _run_native_argv(argv, workspace, timeout_s)
    except Exception as e:
        return ToolResult.err(f"python_script: execution failed: {e}")

    try:
        script_file.unlink(missing_ok=True)
    except Exception:
        pass

    if result.get("error"):
        return ToolResult.err(f"python_script: {result['error']}")
    if result["timed_out"]:
        return ToolResult.err(
            f"python_script: timed out after {int(timeout_s)}s; "
            f"trim loops / raise timeout",
            # F7：超时统一分类 —— python_script 自身的执行超时属 command
            # 超时（脚本跑起来了但没按时完成）。与 bash/run_command 同形状，
            # 供 run_steps.timeout_kind/timeout_ms 统一分组统计。
            timeout_kind="command",
            timeout_ms=int(timeout_s * 1000),
        )

    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    combined = (stdout + ("\n" + stderr if stdout and stderr else stderr))
    body = _truncate_output(combined) if combined.strip() else "(no output)"
    exit_code = result.get("exit_code")
    try:
        low = stderr.lower()
        if "module not found error" in low or "no module named" in low:
            from hiveweave.services.venv_setup import project_venv_python

            vp = project_venv_python(workspace)
            if vp:
                stderr_hint = (
                    f"\n[venv hint] 缺依赖请装进项目 .venv："
                    f'uv pip install --python "{vp}" <包>'
                )
    except Exception:
        pass
    if exit_code == 0:
        return ToolResult.ok(f"{body}\n\nExit code: 0")
    # 失败必须带 stderr 尾部（对齐 bash P2-1）：真正的报错（堆栈/缺失依赖）
    # 几乎总在输出末尾，否则 agent 只见 exit code 盲目重试。
    try:
        from hiveweave.tools.bash import _error_tail

        err_tail = _error_tail(stderr)
    except Exception:
        err_tail = stderr[-2000:] if stderr else ""
    detail = f"\n[stderr tail]\n{err_tail}" if err_tail.strip() else ""
    return ToolResult.err(
        f"python_script exited with code {exit_code}{detail}{stderr_hint}"
    )


@tool(
    "python_script",
    "Run Python code in YOUR workspace as a first-class tool. "
    "Use for data munging, scripted automation, one-off computations — "
    "anything where bash + python -c quoting is painful. "
    "Provide 'script' (source) or 'scriptPath' (workspace-relative .py file). "
    "Runs in a fresh process with the project .venv interpreter when "
    "available. cwd = workspace. Check Exit code / error on every result. "
    "Long output truncated. Set timeout ms (max 10min) for heavy loops. "
    "Note: this tool issues NO test_run attestation — for validation "
    "scripts that must count as test evidence use bash(testEvidence=true).",
    requires_workspace=True,
    security_level="shell",
)
async def python_script_tool(
    params: PythonScriptParams, agent_id: str, workspace: str
) -> ToolResult:
    """First-class python script execution (E11)."""
    return await python_script_execute(params, agent_id, workspace)