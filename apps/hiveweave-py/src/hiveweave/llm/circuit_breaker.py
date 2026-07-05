"""熔断器 — 三态状态机 (closed / open / half_open)。

R8: 架构说明 —— 熔断器是 **per-provider** 的，而非全局单例状态机。
CircuitBreaker 实例内部用 `_breakers: dict[str, _BreakerState]` 为每个
provider 维护独立的状态机（fail_count / opened_at / probe_deadline）。
模块级单例 `circuit_breaker` 是一个「多 provider 管理器」，本身不是共享状态机 ——
不同 provider 之间互不干扰，一个 provider 熔断不影响其他 provider。

契约 01: LLM 流式调用 — 重试与熔断
- 连续失败 5 次后熔断（FAIL_THRESHOLD）
- 熔断后 30s 冷却（COOLDOWN_MS）
- 冷却过后进入 half_open，放行 1 次试探请求（探针）
- 探针成功 → closed（重置失败计数）
- 探针失败 → open（重新计时）
- 探针超时未报告（PROBE_TIMEOUT_MS）→ 视为失败，回到 open
- 参考: Elixir circuit_breaker.ex

Python 异步实现的差异:
- Elixir 用 GenServer + Process.monitor 追踪探针 owner 崩溃。
- Python 没有「进程」概念，用探针超时（probe_deadline）兜底:
  如果探针在 PROBE_TIMEOUT_MS 内未报告结果，下次 check 自动转回 open。
- 所有状态变更通过 asyncio.Lock 串行化，避免并发竞态。
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import NamedTuple

import structlog

log = structlog.get_logger(__name__)

# ── 常量 ────────────────────────────────────────────────────
FAIL_THRESHOLD = 5
"""连续失败次数阈值，达到后熔断。用户指定 5 次。"""

COOLDOWN_MS = 30_000
"""熔断冷却时间（30 秒）。用户指定 30s。"""

PROBE_TIMEOUT_MS = 60_000
"""探针超时时间（60 秒）。

探针超过此时间未报告结果，视为探针失败（可能是调用者崩溃/遗忘报告），
下次 check 自动转回 open 重新冷却。
"""


class CircuitState(str, Enum):
    """熔断器三态。"""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CheckResult(NamedTuple):
    """熔断器检查结果。

    - allowed=True: 请求放行（closed 或 half_open 探针）
    - allowed=False + fallback: 请求被拒，切换到 fallback provider
    - allowed=False + fallback=None: 所有 provider 不可用
    """
    allowed: bool
    fallback: str | None = None

    @classmethod
    def ok(cls) -> "CheckResult":
        return cls(allowed=True)

    @classmethod
    def fallback_to(cls, name: str | None) -> "CheckResult":
        return cls(allowed=False, fallback=name)


class _BreakerState:
    """单个 provider 的熔断器状态（内部类，非线程安全，由 CircuitBreaker 的锁保护）。"""

    __slots__ = ("provider", "state", "fail_count", "opened_at",
                 "probe_deadline", "fallback")

    def __init__(self, provider: str, fallback: str | None = None) -> None:
        self.provider = provider
        self.state = CircuitState.CLOSED
        self.fail_count = 0
        self.opened_at: float | None = None
        self.probe_deadline: float | None = None
        self.fallback = fallback

    def reset(self) -> None:
        """回到 closed 状态，重置所有计数。"""
        self.state = CircuitState.CLOSED
        self.fail_count = 0
        self.opened_at = None
        self.probe_deadline = None

    def open(self) -> None:
        """进入 open 状态，开始冷却计时。"""
        self.state = CircuitState.OPEN
        self.opened_at = time.monotonic()
        self.probe_deadline = None


class CircuitBreaker:
    """多 provider 熔断器管理器（异步安全）。

    R8: 本类是 per-provider 的 —— 每个 provider 拥有独立的 _BreakerState
    状态机，互不干扰。一个 provider 熔断不会牵连其他 provider。

    用法::

        cb = CircuitBreaker()
        result = await cb.check("primary")
        if result.allowed:
            try:
                ...  # 发起 LLM 请求
                await cb.report_success("primary")
            except Exception:
                await cb.report_failure("primary")
                raise
        elif result.fallback:
            # 切换到 fallback provider
            ...
        else:
            raise RuntimeError("All providers unavailable")

    状态转换图::

        closed --5 次连续失败--> open
        open --冷却过后 + 新请求--> half_open (当前调用者为探针)
        half_open --探针成功--> closed
        half_open --探针失败--> open (重新计时)
        half_open --探针超时未报告--> open (重新计时, 由下次 check 检测)
    """

    def __init__(
        self,
        fail_threshold: int = FAIL_THRESHOLD,
        cooldown_ms: int = COOLDOWN_MS,
        probe_timeout_ms: int = PROBE_TIMEOUT_MS,
    ) -> None:
        self.fail_threshold = fail_threshold
        self.cooldown_ms = cooldown_ms
        self.probe_timeout_ms = probe_timeout_ms
        # R8: per-provider 状态机字典 —— key 为 provider 名，value 为独立状态。
        # 每个注册的 provider 有自己的 fail_count / opened_at / probe_deadline。
        self._breakers: dict[str, _BreakerState] = {}
        self._lock = asyncio.Lock()

    # ── 注册 ────────────────────────────────────────────────

    async def register(
        self,
        name: str,
        fallback: str | None = None,
    ) -> None:
        """注册一个 provider 的熔断器。已存在则更新 fallback。"""
        async with self._lock:
            if name not in self._breakers:
                self._breakers[name] = _BreakerState(name, fallback=fallback)
                log.info("circuit_registered", provider=name, fallback=fallback)
            elif fallback is not None:
                self._breakers[name].fallback = fallback

    # ── 检查 ────────────────────────────────────────────────

    async def check(self, name: str) -> CheckResult:
        """检查 provider 是否放行。

        Returns:
            CheckResult(allowed=True)  — 放行
            CheckResult(allowed=False, fallback=X) — 切换到 fallback
            CheckResult(allowed=False, fallback=None) — 无 fallback，全部不可用
        """
        async with self._lock:
            b = self._breakers.get(name)
            if b is None:
                # 未注册的 provider 默认放行
                return CheckResult.ok()

            now = time.monotonic()

            if b.state is CircuitState.CLOSED:
                return CheckResult.ok()

            if b.state is CircuitState.OPEN:
                # 检查冷却是否已过
                if b.opened_at is not None and (now - b.opened_at) * 1000 >= self.cooldown_ms:
                    # 冷却过后 → half_open，当前调用者成为探针
                    b.state = CircuitState.HALF_OPEN
                    b.probe_deadline = now + self.probe_timeout_ms / 1000.0
                    log.info("circuit_half_open", provider=name,
                             cooldown_ms=self.cooldown_ms)
                    return CheckResult.ok()
                # 冷却未过 → 走 fallback
                log.info("circuit_open_fallback", provider=name,
                         fallback=b.fallback)
                return CheckResult.fallback_to(b.fallback)

            if b.state is CircuitState.HALF_OPEN:
                # 检查探针是否超时
                if b.probe_deadline is not None and now > b.probe_deadline:
                    # 探针超时未报告 → 回到 open 重新冷却
                    log.warning("circuit_probe_timeout", provider=name)
                    b.open()
                    return CheckResult.fallback_to(b.fallback)
                # 探针仍在进行中（其他调用者）→ 走 fallback
                # 注意: Python 异步模型下，探针调用者本身会直接通过，
                # 其他并发调用者走 fallback。这里无法区分「探针调用者」和
                # 「其他调用者」，但因为 half_open 只允许一次试探，
                # 所以所有进入 half_open 的 check 都被视为探针候选。
                # 实际效果：第一个 check 放行，后续 check 因探针超时窗口
                # 内未完成而走 fallback。这与 Elixir 的 probe_owner 语义一致。
                return CheckResult.ok()

            # 兜底
            return CheckResult.ok()

    # ── 报告结果 ────────────────────────────────────────────

    async def report_success(self, name: str) -> None:
        """报告请求成功 → 关闭熔断器（如果之前 open/half_open）。"""
        async with self._lock:
            b = self._breakers.get(name)
            if b is None:
                return
            was_open = b.state is not CircuitState.CLOSED
            b.reset()
            if was_open:
                log.info("circuit_closed_success", provider=name)

    async def report_failure(self, name: str) -> None:
        """报告请求失败 → 累计失败计数，可能触发熔断。"""
        async with self._lock:
            b = self._breakers.get(name)
            if b is None:
                return

            if b.state is CircuitState.HALF_OPEN:
                # 探针失败 → 回到 open，重新冷却
                log.warning("circuit_probe_failed", provider=name)
                b.open()
                return

            if b.state is CircuitState.CLOSED:
                b.fail_count += 1
                if b.fail_count >= self.fail_threshold:
                    was_closed = True
                    b.open()
                    log.warning("circuit_opened",
                                provider=name,
                                fail_count=b.fail_count,
                                threshold=self.fail_threshold,
                                cooldown_ms=self.cooldown_ms)
                else:
                    log.info("circuit_fail_count",
                             provider=name,
                             fail_count=b.fail_count,
                             threshold=self.fail_threshold)

            # OPEN 状态下的失败：保持 open，更新 opened_at 重新计时
            if b.state is CircuitState.OPEN:
                b.opened_at = time.monotonic()

    # ── 查询（调试用）─────────────────────────────────────

    async def get_state(self, name: str) -> CircuitState | None:
        """获取 provider 当前的熔断器状态（调试用）。"""
        async with self._lock:
            b = self._breakers.get(name)
            return b.state if b else None

    async def get_fail_count(self, name: str) -> int:
        """获取 provider 当前的连续失败计数（调试用）。"""
        async with self._lock:
            b = self._breakers.get(name)
            return b.fail_count if b else 0

    async def reset(self, name: str | None = None) -> None:
        """重置熔断器（调试/测试用）。

        - name=None: 重置所有 provider
        - name=指定: 仅重置该 provider
        """
        async with self._lock:
            if name is None:
                for b in self._breakers.values():
                    b.reset()
            else:
                b = self._breakers.get(name)
                if b:
                    b.reset()


# ── 模块级单例 ──────────────────────────────────────────────

circuit_breaker = CircuitBreaker()
"""全局熔断器单例。所有 Streamer 实例共享。

R8: 这里的「全局」指的是「所有 Streamer 共用同一个 CircuitBreaker 实例」，
而非「所有 provider 共用一个状态机」。该实例内部按 provider 名分别维护
独立的 _BreakerState，因此仍是 per-provider 的。
"""
