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
    _enforcement_stamp,
    _executed_stamp,
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
        from hiveweave.util.win_subprocess import hidden_exec

        proc = await hidden_exec(
            *argv,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError) as exc:
        # M2/T2（2026-09-16）：**spawn 失败 = 命令从未执行** ⇒ 构造点声明位。
        # 以前这里既没有位、也没有文本可兜（`python_script` 不在
        # `SHELL_SECURITY_LEVEL_TOOLS` 里，且这两条出口 `blocked=False`），
        # 于是落 `outcome_unknown`（"结果未知、别盲目重试"）—— 而
        # `exit_code is None` 已经**明确**说明进程根本没起来。
        return {"output": "", "stdout": "", "stderr": "",
                "exit_code": None, "timed_out": False,
                "fact": "runner_failed",
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
        # #1 治本：spawn 经**唯一入口** —— 判定/路由/盖戳都在那里，本工具不再
        # 自己判沙箱。此前这里是 `if acl_sandbox_active():` + 「入口返回 None
        # ⇒ 沙箱坏，拒绝执行」，而其余四条路（bash / run_command / dev_server /
        # alarm）把同一个 None 读成「沙箱关 ⇒ 回落原生」。于是项目级
        # `danger-full-access`（显式逃生门）下四跑一拒 —— 同一信号五种解释。
        # 现在这个 None 根本不会到达工具层（判定与回落都在入口内部完成）。
        from hiveweave.services.acl_sandbox.entry import spawn_agent_command
        from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
        from hiveweave.services.acl_sandbox.integration import (
            PwshUnavailableError,
            build_confined_argv,
        )
        from hiveweave.services.acl_sandbox.service import spawn_confined

        project_id = await get_project_id(agent_id)

        async def _native_exec() -> dict[str, Any]:
            return await _run_native_argv(argv, workspace, timeout_s)

        async def _confined(ctx) -> dict[str, Any] | None:
            # DSH_33 P0：受限路径经 pwsh 承载，`"interp" "script"` 在 pwsh 里是
            # ParserError（第二个引号串没有调用运算符）——实测 7/7 全失败。
            # 用 dialect="pwsh" 直传并显式加 `&` 调用运算符，且**不**再经
            # _normalize_for_pwsh（那会把路径里的 $ 之类当 bash 变量改写）。
            try:
                cargv = build_confined_argv(
                    f'& "{interp}" "{script_file}"', dialect="pwsh"
                )
            except PwshUnavailableError as exc:
                # ⚠ F5（2026-09-17）：此处返回的是**普通 dict 而不是 None**
                # ⇒ `entry` 的 `result is not None` 成立 ⇒ 不抛异常 ⇒ 照常
                # 盖 `enforcement="confined"`。而进程**根本没启动**
                #（`exit_code=None`、`error="pwsh not found"`）⇒ 回执说
                # "被沙箱约束"、事实是没有进程、没有边界 —— **戳在说谎**。
                #
                # 本批**只加观测、不改失败形态**（改成 raise 会动调用方的
                # except 契约，需自己的验收用例 —— 见 fixqueue F5 的
                # "须独立批次"备注）。故显式声明 `executed=False`：
                # 让「沙箱判定成立」与「命令从未启动」两个事实**并排**存在，
                # 下游不必靠 `exit_code is None` 反推。
                return {
                    "output": "", "stdout": "", "stderr": "",
                    "exit_code": None, "timed_out": False, "error": str(exc),
                    "executed": False,
                    "fact": "runner_failed",
                }
            return await spawn_confined(
                argv=cargv, timeout_s=timeout_s, **ctx.confined_kwargs()
            )

        routed = await spawn_agent_command(
            entry="python_script",
            agent_id=agent_id,
            workspace_path=workspace,
            workdir=workspace,
            project_id=project_id,
            confined=_confined,
            native=_native_exec,
        )
        result = routed.result
        if result.get("long_running"):
            # F3：spawn **已经成功路由并盖了戳** ⇒ 这条出口属于「适用且已判定」，
            # 必须带戳（本条是本文件里唯一能拿到戳却在重建 dict 之前 return 的
            # 出口 —— 戳丢了 agent 就看不到"这次到底在不在沙箱里"）。
            return ToolResult.err(
                "python_script: background unsupported",
                **_enforcement_stamp(result),
            )
        result = {
            "output": "",
            "stdout": result.get("stdout", "") or "",
            "stderr": result.get("stderr", "") or "",
            "exit_code": result.get("exit_code"),
            "timed_out": bool(result.get("timed_out", False)),
            "error": result.get("error"),
            # 构造点声明的 fact must survive （M2/T2）：本处是**重建** dict，
            # 不带过来等于位又被这一层吃掉。
            **({"fact": result["fact"]} if result.get("fact") else {}),
            # 执行面戳随结果上报（落 run_steps.enforcement / 日志）。
            # ⚠ M3（2026-09-17 审计必修）：改用 `bash._enforcement_stamp` ——
            # 键名以 `policy.SPAWN_STAMP_KEYS` 为**唯一登记点**。原先这里是
            # `k.startswith("enforcement")` **本地前缀过滤**，与 bash/dev_server
            # 的口径不同源 ⇒ 加固面 `git_hardened`（不含 "enforcement" 前缀）
            # 在本工具被**静默丢掉**。这正是本仓反复栽的「每处各列一份清单」
            # 形态：两份清单必然各自演化。
            **_enforcement_stamp(result),
        }
    except SandboxUnavailableError as e:
        # fail-closed：判定为受限但受限路径起不来 ⇒ 干净拒绝，绝不落原生
        #（「以为在沙箱里、其实在沙箱外」比明确拒绝糟得多）。
        # F5：带上 `executed` 事实位 —— 异常是从 `spawn_agent_command` 抛出的，
        # 它已在该异常上记了「命令从未启动」（`executed=False`）。不带过来，
        # 这条出口就只有"被拒绝"而没有"根本没跑"这两个正交事实中的后一个，
        # 下游会把 `confined` 读成"在沙箱里跑过"。
        return ToolResult.err(
            f"python_script: sandbox unavailable: {e}",
            fact="runner_failed",
            **_executed_stamp(e),
        )
    except Exception as e:
        return ToolResult.err(f"python_script: execution failed: {e}")

    try:
        script_file.unlink(missing_ok=True)
    except Exception:
        pass

    # ── 执行面戳的落点（F3，2026-09-17）────────────────────────────────
    # ⚠ 上方重建 dict 时**已正确**把 `enforcement*` 带了过来（:248），但随后
    # 每一条出口都新构造 `ToolResult`，不带 extra ⇒ 戳在类型转换处第二次丢失。
    # 实证：58/59/60/61 共 4863 行 run_steps 的 enforcement 全 NULL。
    # ⇒ 提取一次，各出口 `**_stamp` 展开（新增出口必须带上）。
    # ⚠ M3（2026-09-17 审计必修）：改用 `bash._enforcement_stamp`，键名以
    # `policy.SPAWN_STAMP_KEYS` 为唯一登记点 —— 原先的本地前缀过滤会把
    # `git_hardened` 静默排除（本工具此前与该键**永不同源**）。
    _stamp = _enforcement_stamp(result)

    if result.get("error"):
        # 位要跟着走（M2/T2）：`finalize_tool_result` 的归因阶梯**位优先于文本**，
        # 只有把构造点声明的位带到这里，spawn 失败才会被判成 `runner_failed`
        # 而不是靠（不可达的）文本兜底。
        return ToolResult.err(
            f"python_script: {result['error']}",
            fact=result.get("fact"),
            **_stamp,
        )
    if result["timed_out"]:
        return ToolResult.err(
            f"python_script: timed out after {int(timeout_s)}s; "
            f"trim loops / raise timeout",
            # F7：超时统一分类 —— python_script 自身的执行超时属 command
            # 超时（脚本跑起来了但没按时完成）。与 bash/run_command 同形状，
            # 供 run_steps.timeout_kind/timeout_ms 统一分组统计。
            timeout_kind="command",
            timeout_ms=int(timeout_s * 1000),
            **_stamp,
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
        return ToolResult.ok(f"{body}\n\nExit code: 0", **_stamp)
    # 失败必须带 stderr 尾部（对齐 bash P2-1）：真正的报错（堆栈/缺失依赖）
    # 几乎总在输出末尾，否则 agent 只见 exit code 盲目重试。
    try:
        from hiveweave.tools.bash import _error_tail

        err_tail = _error_tail(stderr)
    except Exception:
        err_tail = stderr[-2000:] if stderr else ""
    detail = f"\n[stderr tail]\n{err_tail}" if err_tail.strip() else ""
    return ToolResult.err(
        f"python_script exited with code {exit_code}{detail}{stderr_hint}",
        **_stamp,
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