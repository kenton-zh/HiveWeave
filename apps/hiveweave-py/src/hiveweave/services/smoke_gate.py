"""交付级冒烟门（s3-clone_07 GAP · 交付探针契约）。

背景：07 轮 17/18 任务内部门禁全绿，官方 verifier 90 秒打出 0/22——
`/_health` PASS 而数据面全死（S3 router 从未挂载，ImportError 被静默吞）。
团队 18 个探针全是直调函数，没有一个拉起服务走真协议客户端。根因是
**门禁体系没有"交付物作为整体是否可用"这道门**。

本模块实现 `service_smoke` 机器验收条款（task_contract._MACHINE_TYPES）：

    acceptance: [{
        "id": "smoke-1",
        "type": "service_smoke",
        "script": "tests/smoke/smoke_test.py",     # 探针脚本（相对工作区）
        "startCommand": "python -m uvicorn app.main:app --port {port}",
        "deps": ["boto3"],                          # 可选：uv run --with
        "timeout": 120,                             # 可选：总预算秒
        "portEnv": "SMOKE_PORT"                     # 可选：端口注入的环境变量名
    }]

执行语义（确定型门禁，零 LLM token）：
1. 冻结校验：脚本 sha256 与首次验证时记录的一致——实现者中途削弱脚本 = 立即失败
2. 分配空闲端口，`{port}`/`{python}` 占位符替换后启动服务（boot ≤ min(30, timeout/4)）
3. 等端口 LISTEN（TCP 连接探测）
4. 以 SMOKE_PORT 环境变量运行探针脚本（真协议客户端，cwd=工作区），限时
5. 退出码 0 = PASS；任何失败附日志尾 + 复现信息（疏导：堵外壳疏内核）

脚本作者 = 设计者（架构师/QA），实现者只是"被验证的人"——这正是 07 轮
18 个自写探针全部盲视 seam 断裂的反面。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import structlog

from hiveweave.services.task_contract import ClauseResult

log = structlog.get_logger(__name__)

_BOOT_BUDGET_CAP_S = 30
_LOG_TAIL_CHARS = 1500


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fail(
    clause: dict[str, Any],
    message: str,
    evidence: str | None = None,
    freeze: dict[str, Any] | None = None,
) -> tuple[ClauseResult, dict[str, Any] | None]:
    cid = str(clause.get("id") or "service_smoke")
    return (
        ClauseResult(
            id=cid,
            type="service_smoke",
            passed=False,
            message=message,
            evidence=(evidence or "")[:_LOG_TAIL_CHARS] or None,
        ),
        freeze,
    )


async def run_service_smoke_clause(
    clause: dict[str, Any],
    *,
    workspace_root: str,
    frozen: dict[str, Any] | None = None,
) -> tuple[ClauseResult, dict[str, Any] | None]:
    """执行一个 ``service_smoke`` 验收条款。

    Returns:
        (ClauseResult, freeze_info)。freeze_info 含脚本 sha256，由调用方
        持久化进 contract_json["smoke_freeze"]——首次验证后脚本被改动即失败
        （防实现者削弱验收探针）。
    """
    cid = str(clause.get("id") or "service_smoke")
    script_rel = str(clause.get("script") or "").strip()
    start_command_tpl = str(
        clause.get("startCommand") or clause.get("start_command") or ""
    ).strip()
    timeout_s = int(clause.get("timeout") or 120)
    port_env = str(clause.get("portEnv") or "SMOKE_PORT")
    deps = [str(d) for d in (clause.get("deps") or []) if str(d).strip()]

    root = Path(workspace_root).resolve()
    script_abs = (root / script_rel).resolve()
    try:
        script_abs.relative_to(root)
    except ValueError:
        return _fail(clause, f"smoke script escapes workspace: {script_rel}")
    if not script_abs.is_file():
        return _fail(
            clause,
            f"smoke script not found: {script_rel}（设计者应随设计文档提交探针脚本）",
        )

    # ── 冻结校验：首次运行后脚本被改动 = 立即失败（防削弱验收探针）──────
    current_sha = _sha256_file(script_abs)
    if frozen and frozen.get("sha256") and frozen["sha256"] != current_sha:
        log.warning(
            "smoke_script_modified_after_freeze",
            script=script_rel,
            frozen_sha=frozen.get("sha256"),
            current_sha=current_sha,
        )
        # 冻结保持原样（返回 None = 调用方保留既有指纹），不给削弱后的脚本立新基线
        return _fail(
            clause,
            "smoke 脚本在首次验证后被修改——验收探针由设计者冻结，实现者不得"
            f"更改（sha256 {str(frozen.get('sha256'))[:12]}… ≠ 当前 "
            f"{current_sha[:12]}…）。恢复脚本原内容，或由设计者重新派发契约。",
        )
    # 失败路径也冻结：否则"首次失败→削弱脚本→重跑立新基线"就是洗白通道
    freeze = {"sha256": current_sha, "at_ms": int(time.time() * 1000),
              "script": script_rel}

    total_deadline = time.monotonic() + timeout_s
    boot_deadline = time.monotonic() + min(_BOOT_BUDGET_CAP_S, timeout_s // 4)
    port = _free_port()
    start_cmd = (
        start_command_tpl.replace("{port}", str(port))
        .replace("{python}", sys.executable)
    )

    log_fd, log_path = tempfile.mkstemp(
        prefix=f"smoke-{cid[:24]}-", suffix=".log"
    )
    os.close(log_fd)
    service_proc: asyncio.subprocess.Process | None = None
    try:
        # ── 1. 启动服务 ──────────────────────────────────────────────
        env = os.environ.copy()
        env[port_env] = str(port)
        service_proc = await asyncio.create_subprocess_shell(
            start_cmd,
            cwd=str(root),
            stdout=open(log_path, "ab"),
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        # ── 2. 等端口 LISTEN（TCP 探测）─────────────────────────────
        while True:
            if service_proc.returncode is not None:
                tail = _read_tail(log_path)
                return _fail(
                    clause,
                    f"服务进程在启动窗口内退出（code={service_proc.returncode}）。"
                    "冒烟探针未执行。检查 startCommand 与启动日志：",
                    tail,
                    freeze=freeze,
                )
            if time.monotonic() > boot_deadline:
                tail = _read_tail(log_path)
                return _fail(
                    clause,
                    f"服务在启动窗口内未监听端口 {port}（boot 超时）。"
                    "检查 startCommand 是否正确、依赖是否齐全。启动日志尾：",
                    tail,
                    freeze=freeze,
                )
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                break
            except OSError:
                await asyncio.sleep(0.25)

        # ── 3. 运行探针脚本（真协议客户端）──────────────────────────
        env2 = os.environ.copy()
        env2[port_env] = str(port)
        probe_deadline = max(5.0, total_deadline - time.monotonic())
        if deps:
            with_flags = " ".join(f"--with {d}" for d in deps)
            script_cmd = f'uv run {with_flags} python "{script_abs}"'
            probe = await asyncio.create_subprocess_shell(
                script_cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env2,
            )
        else:
            probe = await asyncio.create_subprocess_exec(
                sys.executable, str(script_abs),
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env2,
            )
        try:
            out, _ = await asyncio.wait_for(probe.communicate(), timeout=probe_deadline)
        except asyncio.TimeoutError:
            probe.kill()
            return _fail(
                clause,
                f"冒烟探针超时（>{probe_deadline:.0f}s）。服务在跑但主链路未在"
                "预算内走通——检查探针断言或服务响应。",
                _read_tail(log_path),
                freeze=freeze,
            )
        output = (out or b"").decode("utf-8", errors="replace")
        if probe.returncode == 0:
            log.info("service_smoke_passed", clause=cid, port=port,
                     script=script_rel)
            return (
                ClauseResult(
                    id=cid,
                    type="service_smoke",
                    passed=True,
                    message=f"冒烟通过（port={port}，exit=0）",
                    evidence=output[-_LOG_TAIL_CHARS:] or None,
                ),
                freeze,
            )
        return _fail(
            clause,
            f"冒烟探针失败（exit={probe.returncode}）。服务能启动但主链路未通"
            "——输出尾：",
            output,
            freeze=freeze,
        )
    except Exception as exc:  # noqa: BLE001 — 执行器自身故障也算冒烟失败（fail-loud）
        log.error("service_smoke_runner_error", clause=cid, error=str(exc))
        return _fail(clause, f"冒烟执行器故障：{type(exc).__name__}: {exc}")
    finally:
        # ── 清理：杀服务进程树（Windows taskkill /T；POSIX terminate）────
        # 必须等杀完再返回：Windows 下存活子进程的 cwd 会锁住工作区目录，
        # 导致外层 TemporaryDirectory 清理报 WinError 32（测试实证）。
        if service_proc is not None and service_proc.returncode is None:
            try:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(service_proc.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(killer.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        killer.kill()
                    try:
                        await asyncio.wait_for(service_proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        service_proc.kill()
                else:
                    service_proc.terminate()
                    try:
                        await asyncio.wait_for(service_proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        service_proc.kill()
            except Exception:  # noqa: BLE001 — 清理尽力而为
                try:
                    service_proc.kill()
                except Exception:  # noqa: BLE001
                    pass


def _read_tail(log_path: str, chars: int = _LOG_TAIL_CHARS) -> str:
    try:
        with open(log_path, "rb") as f:
            data = f.read()[-chars * 2:]
        return data.decode("utf-8", errors="replace")[-chars:]
    except Exception:  # noqa: BLE001
        return ""


def find_smoke_clause(contract: dict[str, Any] | None) -> dict[str, Any] | None:
    """从契约里取第一个 service_smoke 条款；无则 None。"""
    if not contract:
        return None
    for c in contract.get("acceptance") or []:
        if isinstance(c, dict) and (c.get("type") == "service_smoke"):
            return c
    return None
