"""宿主环境探测 — 执行器（超时 + 懒缓存 + fail-closed）。

抄 DSH sandbox-local 的四条机制（计划 §0 Phase 0 表格）：

- **真跑一次命令**：探测函数自己 spawn 子进程，runner 不做文件存在性猜测；
- **探测一次并缓存**（``??= chainVerdict()``，sandbox-local:492）：结果对象
  不可变，命中直接返回；不可用也缓存（负缓存），重探测用 disposer/reset；
- **超时保护**：默认 5s，**拒绝 0**（DSH ``probeTimeoutMs`` 语义）；
- **fail-closed**：探测异常 = 不可用（:class:`CapabilityUnavailableError`），
  绝不「大概能用」。

线程模型：探测函数在 daemon 线程里跑（join(timeout) 守墙钟）。超时后线程
不可杀会残留到进程退出 —— daemon 化保证不阻塞退出；这是 Python 线程的
固有限制，DSH 的 spawnSync 超时杀进程在这里用「放任残留 + 不阻塞退出」
等价（探测函数自身传给子进程的 timeout 通常更早生效）。
"""
from __future__ import annotations

import asyncio
import subprocess
import threading
from typing import Any

import structlog

from .registry import all_entries, get_entry
from .types import CapabilityUnavailableError, ProbeResult, ProbeTiming

log = structlog.get_logger(__name__)

#: 默认探测超时（秒）。DSH probeTimeoutMs 默认 5000；**拒绝 0**。
DEFAULT_PROBE_TIMEOUT_S = 5.0

_guard = threading.Lock()
# 结果缓存：key = "name"（STARTUP）或 "name\x1fparams_key"（LAZY 带参数）。
# 值 = ProbeResult 或 CapabilityUnavailableError（负缓存，fail-closed 一次）。
_results: dict[str, Any] = {}
# in-flight 互斥：防并发首访同一 LAZY 项重复探测（check-then-act 竞态，
# 审计 P1-2）。key 与 _results 相同；锁对象常驻（量级 = 探测项×参数组合）。
_inflight: dict[str, threading.Lock] = {}

# 瞬态原因不进负缓存（审计 P2-6）：超时/探测函数异常可能是冷盘、慢宿主等
# 瞬态故障，永久负缓存会让进程「失明」到重启。结构性不可用（not-found /
# windows-only / config-off / …）仍负缓存。
_TRANSIENT_REASONS = frozenset({"timeout", "probe-error"})

_startup_done = False


def _get_inflight(key: str) -> threading.Lock:
    with _guard:
        return _inflight.setdefault(key, threading.Lock())


def _cache_key(name: str, params: dict[str, Any]) -> str:
    if not params:
        return name
    params_key = ",".join(f"{k}={params[k]!r}" for k in sorted(params))
    return f"{name}\x1f{params_key}"


def evict_cache(name: str) -> None:
    """清掉某探测项的全部结果缓存（disposer 与测试 reset 用）。"""
    with _guard:
        for key in [k for k in _results if k == name or k.split("\x1f", 1)[0] == name]:
            _results.pop(key, None)
        _inflight.pop(name, None)


def reset_runner() -> None:
    """仅测试用：清空结果缓存 + startup 完成标记。"""
    global _startup_done
    with _guard:
        _results.clear()
        _inflight.clear()
        _startup_done = False


def reject_bad_timeout(timeout_s: float, probe: str) -> float:
    """超时校验：拒绝 0 / 负数（DSH probeTimeoutMs 语义，fail-closed）。"""
    if timeout_s is None or timeout_s <= 0:
        raise ValueError(
            f"probe {probe!r}: timeout must be > 0 (got {timeout_s!r}) — "
            "拒绝 0，超时保护不可关"
        )
    return timeout_s


def run_command(
    argv: list[str],
    *,
    timeout_s: float,
    cwd: str | None = None,
    encoding: str = "utf-8",
) -> subprocess.CompletedProcess:
    """探测用子进程执行（同步，带超时）。探不到/超时 → CapabilityUnavailableError。

    这是探测函数的唯一执行缝 —— 测试 monkeypatch 这里即可模拟任意命令结果，
    不必造假二进制。

    ``encoding``：本地化输出的解码代码页。默认 utf-8；输出系统 ANSI 代码页
    的工具（icacls 等）传 ``"mbcs"`` —— 非 ASCII 路径（中文用户名的 TEMP）
    用 utf-8 解码会变乱码，回读比对恒失败（审计 P1-1 实证）。
    """
    reject_bad_timeout(timeout_s, argv[0] if argv else "run_command")
    # SW_HIDE startupinfo（不用 CREATE_NO_WINDOW —— 孙进程会弹新控制台，
    # 见 util/win_subprocess.py 顶部注释）。
    from hiveweave.util.win_subprocess import windows_no_window_kwargs

    try:
        raw = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout_s,
            cwd=cwd,
            check=False,
            **windows_no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise CapabilityUnavailableError(
            f"{argv[0]} not found on PATH", probe=argv[0], reason="not-found"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise CapabilityUnavailableError(
            f"{argv[0]} timed out after {timeout_s}s",
            probe=argv[0],
            reason="timeout",
        ) from e
    # 不用 text=True：探测对象（icacls/npm/…）会按系统代码页输出本地化
    # 文本，text=True 的 UTF-8 解码直接炸掉 reader 线程 → stdout=None。
    # 抓字节自己 replace 解码（与 git_cmd.py 同款）；ASCII 版本号无损，
    # 本地化输出的解码页由调用方用 encoding 指定（icacls → mbcs）。
    dec = encoding or "utf-8"
    return subprocess.CompletedProcess(
        argv,
        raw.returncode,
        stdout=(raw.stdout or b"").decode(dec, errors="replace"),
        stderr=(raw.stderr or b"").decode(dec, errors="replace"),
    )


def _run_probe_fn(
    entry, params: dict[str, Any], timeout_s: float | None = None
) -> ProbeResult:
    """带超时保护地跑一个探测函数（daemon 线程 + join 守墙钟）。

    ``timeout_s``：调用方覆盖执行超时（get_capability 透传，审计 P1-3）；
    None = 用默认。只影响本次执行的墙钟，不进缓存键（缓存按探测身份）。
    """
    effective = reject_bad_timeout(
        timeout_s if timeout_s is not None else DEFAULT_PROBE_TIMEOUT_S,
        entry.name,
    )
    kwargs = {"timeout_s": effective, **params}
    outcome: list[Any] = []
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            outcome.append(entry.fn(**kwargs))
        except BaseException as e:  # noqa: BLE001 — fail-closed 收一切
            errors.append(e)

    t = threading.Thread(
        target=_target, name=f"host-env-probe-{entry.name}", daemon=True
    )
    t.start()
    t.join(effective)
    if t.is_alive():
        raise CapabilityUnavailableError(
            f"probe {entry.name!r} exceeded {effective}s wall clock",
            probe=entry.name,
            reason="timeout",
        )
    if errors:
        e = errors[0]
        if isinstance(e, CapabilityUnavailableError):
            raise e
        raise CapabilityUnavailableError(
            f"probe {entry.name!r} raised {type(e).__name__}: {e}",
            probe=entry.name,
            reason="probe-error",
        ) from e
    result = outcome[0]
    if not isinstance(result, ProbeResult):
        raise CapabilityUnavailableError(
            f"probe {entry.name!r} returned {type(result).__name__}, expected ProbeResult",
            probe=entry.name,
            reason="bad-probe-contract",
        )
    return result


def _store(key: str, value: Any) -> None:
    with _guard:
        _results[key] = value


def _peek(key: str) -> Any | None:
    with _guard:
        return _results.get(key)


def run_startup_probes() -> dict[str, ProbeResult]:
    """启动时跑全部 STARTUP 探测项（T3.2 需在 Agent 起来前定完工具暴露）。

    成功结果与不可用原因都落缓存；返回成功结果字典。单条失败不炸启动
    （沿用 main.py lifespan 的 try/except + log.warning 模式），消费方查
    :func:`get_capability` 会拿到 fail-closed 的异常。
    """
    global _startup_done
    results: dict[str, ProbeResult] = {}
    entries = all_entries(timing=ProbeTiming.STARTUP)
    for entry in entries:
        key = _cache_key(entry.name, {})
        try:
            result = _run_probe_fn(entry, {})
        except CapabilityUnavailableError as e:
            _store(key, e)
            log.warning(
                "host_env_probe_unavailable",
                probe=entry.name,
                reason=e.reason,
                error=str(e),
            )
            continue
        except Exception as e:  # noqa: BLE001 — 防御：不炸启动
            wrapped = CapabilityUnavailableError(
                f"probe {entry.name!r} unexpected failure: {e}",
                probe=entry.name,
                reason="probe-error",
            )
            _store(key, wrapped)
            log.warning(
                "host_env_probe_unavailable",
                probe=entry.name,
                reason="probe-error",
                error=str(e),
            )
            continue
        _store(key, result)
        results[entry.name] = result
        log.info(
            "host_env_probe_ok",
            probe=entry.name,
            level=result.level.value,
            detail=result.detail,
        )
    with _guard:
        _startup_done = True
    unavailable = [e.name for e in entries if e.name not in results]
    log.info(
        "host_env_startup_probes_done",
        ok=sorted(results),
        unavailable=sorted(unavailable),
    )
    return results


def get_capability(name: str, timeout_s: float | None = None, **params: Any) -> ProbeResult:
    """取探测结果（同步，可阻塞至多一个超时周期）。

    - STARTUP 探测项：必须已在启动时跑过 —— 未跑抛错（T3.2 的消费时点在
      Agent 起，启动已过；提前查询是编程错误）。不接受参数。
    - LAZY 探测项：首访真跑（``??=``），之后命中缓存；带参数的探测按参数
      缓存（如 workspace.cache_writable 按 path 各存一份）。并发首访由
      per-key in-flight 锁去重（审计 P1-2）——探测函数只执行一次。
    - 不可用：抛 :class:`CapabilityUnavailableError`。结构性不可用负缓存；
      瞬态原因（timeout/probe-error）不缓存，下次重探（审计 P2-6）。
    - ``timeout_s``：本次执行的墙钟覆盖（审计 P1-3），不进缓存键。
    """
    entry = get_entry(name)
    if timeout_s is not None:
        reject_bad_timeout(timeout_s, name)
    key = _cache_key(name, params)

    if entry.timing is ProbeTiming.STARTUP:
        if params:
            raise CapabilityUnavailableError(
                f"startup probe {name!r} takes no parameters (got "
                f"{sorted(params)}) — it is probed once at app boot",
                probe=name,
                reason="bad-params",
            )
        cached = _peek(key)
        if cached is None:
            raise CapabilityUnavailableError(
                f"startup probe {name!r} has not run yet — "
                "call run_startup_probes() at app boot first",
                probe=name,
                reason="not-probed-yet",
            )
        if isinstance(cached, CapabilityUnavailableError):
            raise cached
        return cached

    cached = _peek(key)
    if cached is not None:
        if isinstance(cached, CapabilityUnavailableError):
            raise cached
        return cached

    with _get_inflight(key):
        cached = _peek(key)  # 双检：别的线程可能刚跑完
        if cached is not None:
            if isinstance(cached, CapabilityUnavailableError):
                raise cached
            return cached
        try:
            result = _run_probe_fn(entry, params, timeout_s=timeout_s)
        except CapabilityUnavailableError as e:
            if e.reason not in _TRANSIENT_REASONS:
                _store(key, e)
            raise
        _store(key, result)
        return result


async def aget_capability(name: str, **params: Any) -> ProbeResult:
    """异步上下文取探测结果（懒探测的子进程跑在线程里，不堵事件循环）。"""
    entry = get_entry(name)
    if entry.timing is ProbeTiming.STARTUP:
        return get_capability(name, **params)
    key_hint = _cache_key(name, params)
    if _peek(key_hint) is not None:
        return get_capability(name, **params)
    return await asyncio.to_thread(get_capability, name, **params)


def capability_snapshot() -> dict[str, Any]:
    """诊断视图：已注册项 × 已缓存结果。

    带参探测（workspace.cache_writable）可能有多个参数变体、成败混杂 ——
    按 name 归组，levels/errors 全量列出，不互相覆盖（审计 P2-7）。
    """
    with _guard:
        snapshot = dict(_results)
    grouped: dict[str, dict[str, Any]] = {}
    for key, value in snapshot.items():
        name = key.split("\x1f", 1)[0]
        slot = grouped.setdefault(
            name, {"name": name, "levels": [], "errors": [], "data": {}}
        )
        if isinstance(value, CapabilityUnavailableError):
            slot["errors"].append({"reason": value.reason, "error": str(value)})
        else:
            slot["levels"].append(value.level.value)
            slot["detail"] = value.detail
            slot["data"].update(dict(value.data))
    out: dict[str, Any] = dict(grouped)
    out["__startup_done__"] = _peek_startup_done()
    return out


def _peek_startup_done() -> bool:
    with _guard:
        return _startup_done
