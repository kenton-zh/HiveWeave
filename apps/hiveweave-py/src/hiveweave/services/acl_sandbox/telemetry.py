"""ACL 沙箱遥测（spec §13 P1：fail-closed 次数 / 拒绝命中率 / 传播耗时 / mint P95）。

进程内轻量计数器（thread-safe）。P3 可外接指标后端；P1 先提供
``snapshot()`` 供管理 API 与哨兵探针判据读取。
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_counters: dict[str, float] = {}      # 单调计数/累计毫秒
_samples: dict[str, list[float]] = {}  # 抽样窗口（mint 耗时等）


def _inc(key: str, by: float = 1.0) -> None:
    with _lock:
        _counters[key] = _counters.get(key, 0.0) + by


def _sample(key: str, value: float, max_len: int = 200) -> None:
    with _lock:
        buf = _samples.setdefault(key, [])
        buf.append(value)
        if len(buf) > max_len:
            del buf[: len(buf) - max_len]


def record_fail_closed(api_name: str = "") -> None:
    _inc("fail_closed_count")
    if api_name:
        _inc(f"fail_closed_api:{api_name}")


def record_rejection(hit: bool) -> None:
    """hit=True 命中拒绝方言（非零退出 + 拒绝特征）；否则记录一次运行。"""
    _inc("runs_total")
    if hit:
        _inc("rejection_hits")


def record_propagation_ms(ms: float) -> None:
    _inc("propagation_ms_total", ms)
    _inc("propagation_count")
    _sample("propagation_ms", ms)


def record_mint_ms(ms: float) -> None:
    _inc("mint_ms_total", ms)
    _inc("mint_count")
    _sample("mint_ms", ms)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def snapshot() -> dict:
    """当前遥测快照（供管理 API / 哨兵判据）。"""
    with _lock:
        runs = _counters.get("runs_total", 0.0)
        return {
            "fail_closed_count": int(_counters.get("fail_closed_count", 0.0)),
            "rejection_hits": int(_counters.get("rejection_hits", 0.0)),
            "runs_total": int(runs),
            "rejection_hit_rate": (
                round(_counters.get("rejection_hits", 0.0) / runs, 4)
                if runs else 0.0
            ),
            "propagation_ms_p95": _p95(_samples.get("propagation_ms", [])),
            "propagation_count": int(_counters.get("propagation_count", 0.0)),
            "mint_ms_p95": _p95(_samples.get("mint_ms", [])),
            "mint_count": int(_counters.get("mint_count", 0.0)),
        }


def reset_for_tests() -> None:
    with _lock:
        _counters.clear()
        _samples.clear()


def _t_ms() -> float:
    return time.monotonic() * 1000.0


class _Stopwatch:
    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = _t_ms()

    def elapsed(self) -> float:
        return _t_ms() - self._start
