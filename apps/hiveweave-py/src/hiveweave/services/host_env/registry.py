"""宿主环境探测 — 注册表（抄 deepseek-harness invariants 的注册式）。

三条纪律（invariants ``register(packageName, installer)``，index.ts:136）：

1. **重复注册抛错** —— 防静默覆盖（同一条能力被后注册者悄悄换掉）；
2. **返回 disposer** —— 反注册并清掉该条目的缓存结果，重注册从头探测；
3. **注册表只管「有哪些探测项」** —— 执行与结果缓存归 runner（``??=``
   语义在 runner，不在注册表）。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .types import ProbeFn, ProbeTiming

_probe_guard = threading.Lock()


@dataclass(frozen=True)
class ProbeEntry:
    """一条已注册的探测项（纯声明，无状态）。"""

    name: str
    fn: ProbeFn
    timing: ProbeTiming
    description: str = ""


_entries: dict[str, ProbeEntry] = {}


def register(
    name: str,
    fn: ProbeFn,
    *,
    timing: ProbeTiming = ProbeTiming.LAZY,
    description: str = "",
) -> Callable[[], None]:
    """注册一条探测项；重复注册抛错（invariants 纪律 1）。返回 disposer。"""
    with _probe_guard:
        if name in _entries:
            raise RuntimeError(
                f"host_env probe {name!r} already registered "
                f"(timing={_entries[name].timing.value}) — duplicate registration"
            )
        _entries[name] = ProbeEntry(
            name=name, fn=fn, timing=timing, description=description
        )

    def _dispose() -> None:
        """反注册 + 清缓存（invariants 纪律 2：失败 dispose 子资源同理）。"""
        unregister(name)
        # 局部 import 断环（runner → registry）；旧注册的结果缓存一并失效，
        # 重注册从头探测。
        from . import runner as _runner

        _runner.evict_cache(name)

    return _dispose


def unregister(name: str) -> None:
    """移除探测项；runner 侧缓存由 disposer 通过 :func:`evict_cache` 清。"""
    with _probe_guard:
        _entries.pop(name, None)


def get_entry(name: str) -> ProbeEntry:
    """按名取探测项；未注册抛 KeyError（消费方按名查，名字拼错尽早炸）。"""
    with _probe_guard:
        if name not in _entries:
            raise KeyError(
                f"host_env probe {name!r} not registered — "
                f"known: {sorted(_entries)}"
            )
        return _entries[name]


def all_entries(timing: ProbeTiming | None = None) -> list[ProbeEntry]:
    """全部（或按 timing 过滤的）探测项，按名字排序保证确定性。"""
    with _probe_guard:
        entries = list(_entries.values())
    if timing is not None:
        entries = [e for e in entries if e.timing == timing]
    return sorted(entries, key=lambda e: e.name)


def reset_registry() -> None:
    """仅测试用：清空注册表（built-in 探测项可重新注册）。

    同时复位 probes 包的「已注册」幂等标志 —— 否则 reset 后再调
    ``register_builtin_probes()`` 是 no-op，注册表保持空白。
    """
    with _probe_guard:
        _entries.clear()
    from . import probes as _probes_mod

    if hasattr(_probes_mod, "_registered"):
        _probes_mod._registered = False
