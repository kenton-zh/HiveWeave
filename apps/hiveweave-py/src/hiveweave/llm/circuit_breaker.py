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
from collections import deque
from enum import Enum
from typing import NamedTuple

import structlog

log = structlog.get_logger(__name__)


def _now() -> float:
    """单调时钟（模块级别名：窗口判据测试可 monkeypatch 本名）。"""
    return time.monotonic()

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

UPSTREAM_BREAKER_COOLDOWN_S = 60
"""确定性上游死亡快熔（``open_for``）的默认冷却秒数（TEST_DSH_63 方案 B+）。

63 实测：30 秒上游抖动窗内 4 个在跑流被打断 + 2 个新 run 即刻撞墙，
各自烧满 300s 才死。403 RegionError 首次命中即 open_for（不等 FAIL_THRESHOLD
次累计），开启期并行请求在 check() 处秒败（错误带 ``UPSTREAM_BREAKER_MARKER``）。
60s = 抖动窗（30s）的两倍余量；冷却过后 half_open 探针自动放行恢复。
"""

# ── 时间窗失败率（TEST_DSH_70 P2-6 批3）────────────────────
# consecutive-fail 的结构盲区：429 闪断**穿插成功** → report_success 里的
# b.reset() 复位 fail_count ⇒ 128 条 429 全落在首 2h、熔断 0 open。窗口判据
# 与连续计数**独立**：近 FAIL_RATE_WINDOW_S 秒内样本 ≥ FAIL_RATE_MIN_SAMPLES
# 且失败率 ≥ FAIL_RATE_THRESHOLD 即 open。成功**不清洗**窗口历史（时间流逝
# 才淘汰旧事件）；原 consecutive 逻辑一字未动。
FAIL_RATE_WINDOW_S = 120.0
"""时间窗长度（秒）：近这个窗内的成败样本参与失败率判定。"""

FAIL_RATE_MIN_SAMPLES = 8
"""窗口最小样本数：不足则不判（避免 2 失败 0 成功 = 100% 的假阳性开闸）。"""

FAIL_RATE_THRESHOLD = 0.6
"""窗口失败率阈值：≥ 此值即 open（429 风暴窗内哪怕穿插成功也逃不掉）。"""


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
                 "probe_deadline", "fallback", "last_error_code",
                 "open_cooldown_ms", "window_events")

    def __init__(self, provider: str, fallback: str | None = None) -> None:
        self.provider = provider
        self.state = CircuitState.CLOSED
        self.fail_count = 0
        self.opened_at: float | None = None
        self.probe_deadline: float | None = None
        self.fallback = fallback
        # #13 批 A（2026-09-18）：最近一次失败的稳定错误码（字符串，来自
        # RetryableError.error_code）—— 熔断器的第一个结构化消费方：
        # 「为什么熔断」从只读日志变成状态可查（snapshot 暴露）。
        self.last_error_code: str | None = None
        # 本次 open 的单次冷却覆盖（毫秒）；None = 用管理器级 cooldown_ms。
        # 只由 open_for（确定性快熔，60s）设置；阈值触发的常规 open 沿用
        # 管理器默认 30s。reset() 清除。
        self.open_cooldown_ms: int | None = None
        # P2-6（TEST_DSH_70 批3）：时间窗成败样本 [(monotonic_ts, is_fail)]。
        # ⚠ 与 fail_count 生命周期**不同**：b.reset()（report_success 每次成功
        # 都调）**不清**窗口 —— 那正是「穿插成功复位连续计数绕过熔断」的病根；
        # 只有时间流逝淘汰旧事件 + 管理器级 reset()（调试/手动解除前置）显式清。
        self.window_events: deque[tuple[float, bool]] = deque()

    def reset(self) -> None:
        """回到 closed 状态，重置所有计数。

        ⚠ 不清 ``window_events``（见上：成功复位连续计数不得连带清洗窗口
        失败史）。管理器级 :meth:`CircuitBreaker.reset` 负责显式清窗口。
        """
        self.state = CircuitState.CLOSED
        self.fail_count = 0
        self.opened_at = None
        self.probe_deadline = None
        self.last_error_code = None
        self.open_cooldown_ms = None

    def open(self, cooldown_ms: int | None = None) -> None:
        """进入 open 状态，开始冷却计时。

        ``cooldown_ms``：本次 open 的冷却覆盖（None = 管理器默认）。
        open_for 的确定性快熔传 UPSTREAM_BREAKER_COOLDOWN_S*1000；
        阈值触发/探针超时路径不传，沿用管理器级 COOLDOWN_MS。
        """
        self.state = CircuitState.OPEN
        self.opened_at = time.monotonic()
        self.probe_deadline = None
        self.open_cooldown_ms = cooldown_ms

    def effective_cooldown_ms(self, default_ms: int) -> int:
        """本次 open 生效的冷却毫秒（单次覆盖 > 管理器默认）。"""
        if self.open_cooldown_ms is not None:
            return self.open_cooldown_ms
        return default_ms


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

    # ── 时间窗失败率（P2-6 批3）────────────────────────────

    @staticmethod
    def _record_window_event(b: _BreakerState, is_fail: bool) -> None:
        """记录一次成败样本并淘汰窗外旧事件（调用方须持锁）。"""
        now = _now()
        b.window_events.append((now, is_fail))
        cutoff = now - FAIL_RATE_WINDOW_S
        ev = b.window_events
        while ev and ev[0][0] < cutoff:
            ev.popleft()

    @staticmethod
    def _window_rate(b: _BreakerState) -> tuple[int, int, float] | None:
        """窗口失败率 ``(fails, total, rate)``；样本不足返回 None。

        判定前先淘汰窗外事件（时间流逝是窗口唯一的清洗途径）。
        """
        now = _now()
        cutoff = now - FAIL_RATE_WINDOW_S
        ev = b.window_events
        while ev and ev[0][0] < cutoff:
            ev.popleft()
        total = len(ev)
        if total < FAIL_RATE_MIN_SAMPLES:
            return None
        fails = sum(1 for _t, is_fail in ev if is_fail)
        return fails, total, fails / total

    @classmethod
    def _window_rate_trip(cls, b: _BreakerState) -> tuple[int, int, float] | None:
        """窗口判据命中即返回 ``(fails, total, rate)``，未命中返回 None。"""
        wr = cls._window_rate(b)
        if wr is not None and wr[2] >= FAIL_RATE_THRESHOLD:
            return wr
        return None

    # ── 检查 ────────────────────────────────────────────────

    async def open_for(
        self,
        name: str,
        cooldown_s: float = UPSTREAM_BREAKER_COOLDOWN_S,
    ) -> None:
        """立即熔断指定 provider（确定性上游死亡快熔，TEST_DSH_63 方案 B+）。

        403 RegionError 等确定性不可恢复错误**不走** FAIL_THRESHOLD 阈值
        累计 —— 首次命中即 open，开启期并行/后续请求在 check() 处秒败
        （错误文案带 ``UPSTREAM_BREAKER_MARKER``，见 core._breaker_open_error），
        不再各自烧满重试预算。冷却用本调用给定的单次覆盖
        （默认 UPSTREAM_BREAKER_COOLDOWN_S = 60s，可长于管理器级
        COOLDOWN_MS）；冷却过后 check() 正常转 half_open 放行探针，
        探针成功经 report_success 自动恢复 —— 无需手动复位。

        fail_count 保持现状：快熔是确定性判定，不依赖连续计数。
        未注册的 provider 自动建账（与 register 等效，避免调用序敏感）。
        判据统一走 ``hiveweave.llm.retry.is_upstream_death``（跨组契约），
        本方法只负责状态变更。
        """
        async with self._lock:
            b = self._breakers.get(name)
            if b is None:
                b = _BreakerState(name)
                self._breakers[name] = b
            b.open(cooldown_ms=int(cooldown_s * 1000))
            log.warning(
                "circuit_force_opened",
                provider=name,
                cooldown_s=cooldown_s,
            )

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
                # P2-6（TEST_DSH_70 批3）：窗口失败率判据 —— consecutive
                # 计数被穿插成功复位时，429 风暴仍能在这里被拦下。
                wr = self._window_rate_trip(b)
                if wr is not None:
                    b.open()
                    log.warning(
                        "circuit_opened_window_rate",
                        provider=name,
                        window_failures=wr[0],
                        window_total=wr[1],
                        window_rate=round(wr[2], 3),
                        window_s=FAIL_RATE_WINDOW_S,
                    )
                    return CheckResult.fallback_to(b.fallback)
                return CheckResult.ok()

            if b.state is CircuitState.OPEN:
                # 检查冷却是否已过（open_for 快熔的单次覆盖优先于管理器默认）
                if b.opened_at is not None and (
                    (now - b.opened_at) * 1000
                    >= b.effective_cooldown_ms(self.cooldown_ms)
                ):
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
        """报告请求成功 → 关闭熔断器（如果之前 open/half_open）。

        ⚠ P2-6：成功**先记窗口样本再 reset** —— reset 复位连续计数但不动
        窗口历史（穿插成功不得清洗 429 风暴的失败史）。
        """
        async with self._lock:
            b = self._breakers.get(name)
            if b is None:
                return
            self._record_window_event(b, False)
            was_open = b.state is not CircuitState.CLOSED
            b.reset()
            if was_open:
                log.info("circuit_closed_success", provider=name)

    async def report_failure(
        self, name: str, *, error_code: str | None = None
    ) -> None:
        """报告请求失败 → 累计失败计数，可能触发熔断。

        ``error_code``（#13 批 A）：稳定错误码字符串（``RetryableError
        .error_code``），记录最近一次失败属于哪一族 —— 供 snapshot/诊断
        消费；不传 = 未知（探针路径等无异常上下文的调用方）。
        """
        async with self._lock:
            b = self._breakers.get(name)
            if b is None:
                return
            # P2-6：失败样本进时间窗（与状态机无关 —— half_open/open 期
            # 的失败同样是窗口失败率的一部分）。
            self._record_window_event(b, True)
            if error_code is not None:
                b.last_error_code = error_code

            if b.state is CircuitState.HALF_OPEN:
                # 探针失败 → 回到 open，重新冷却
                log.warning(
                    "circuit_probe_failed",
                    provider=name,
                    error_code=error_code,
                )
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
                    # 窗口失败率判据（consecutive 之外的第二条独立通道）：
                    # 连续计数未到、但窗内失败率已越线 ⇒ 同样 open。
                    wr = self._window_rate_trip(b)
                    if wr is not None:
                        b.open()
                        log.warning(
                            "circuit_opened_window_rate",
                            provider=name,
                            window_failures=wr[0],
                            window_total=wr[1],
                            window_rate=round(wr[2], 3),
                            window_s=FAIL_RATE_WINDOW_S,
                        )

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

    def cooldown_left_s(self, name: str) -> int:
        """provider 剩余冷却秒数（open 状态）；未注册/非 open 返回 0。

        与 ``snapshot()`` 同为**无锁同步读** —— 只用于错误文案展示/诊断
        （_breaker_open_error 带 "还剩几秒"），不用于任何放行判定；
        放行判定一律走 async check()。
        """
        b = self._breakers.get(name)
        if b is None or b.state is not CircuitState.OPEN or b.opened_at is None:
            return 0
        left = b.effective_cooldown_ms(self.cooldown_ms) / 1000 - (
            time.monotonic() - b.opened_at
        )
        return max(0, int(left))

    async def reset(self, name: str | None = None) -> None:
        """重置熔断器（调试/测试/手动解除前置用）。

        - name=None: 重置所有 provider
        - name=指定: 仅重置该 provider

        ⚠ P2-6：管理器级 reset 是**显式的人工复位** ⇒ 连时间窗失败史一起清
        （给操作者一张白纸）；per-state ``b.reset()``（report_success 每次成功
        都调）则**不清**窗口 —— 那条路径清窗口等于让穿插成功洗掉 429 风暴。
        """
        async with self._lock:
            if name is None:
                for b in self._breakers.values():
                    b.reset()
                    b.window_events.clear()
            else:
                breaker_state = self._breakers.get(name)
                if breaker_state:
                    breaker_state.reset()
                    breaker_state.window_events.clear()

    def snapshot(self) -> list[dict]:
        """全部 provider 的熔断状态快照（40 轮 #13：诊断 API / 手动解除前置）。

        返回 [{provider, state, fail_count, fail_threshold,
              cooldown_left_s, fallback, last_error_code,
              window_fail_rate, window_samples}]；冷却剩余按秒向下取整。
        P2-6：窗口两键 = 时间窗失败率判据的可观测面（样本不足 = None）。
        """
        now = time.monotonic()
        out: list[dict] = []
        for name, b in self._breakers.items():
            cooldown_left = 0
            if b.state == CircuitState.OPEN and b.opened_at is not None:
                cooldown_left = max(
                    0,
                    int(
                        b.effective_cooldown_ms(self.cooldown_ms) / 1000
                        - (now - b.opened_at)
                    ),
                )
            # 窗口统计（只读、不淘汰 —— snapshot 是无锁同步读，不碰 deque 结构）。
            # ⚠ deque 在被并发 popleft/append 时迭代会抛
            # ``RuntimeError: deque mutated during iteration`` —— 快照是诊断面，
            # 撞上竞态就放弃本轮窗口统计（None），绝不让诊断调用反过来炸调用方。
            cutoff = now - FAIL_RATE_WINDOW_S
            try:
                recent = [f for (t, f) in b.window_events if t >= cutoff]
            except RuntimeError:
                recent = []
            if len(recent) >= FAIL_RATE_MIN_SAMPLES:
                window_rate = sum(1 for f in recent if f) / len(recent)
            else:
                window_rate = None
            out.append({
                "provider": name,
                "state": b.state.value,
                "fail_count": b.fail_count,
                "fail_threshold": self.fail_threshold,
                "cooldown_left_s": cooldown_left,
                "fallback": b.fallback,
                "last_error_code": b.last_error_code,
                "window_fail_rate": window_rate,
                "window_samples": len(recent),
            })
        return out


# ── 模块级单例 ──────────────────────────────────────────────

circuit_breaker = CircuitBreaker()
"""全局熔断器单例。所有 Streamer 实例共享。

R8: 这里的「全局」指的是「所有 Streamer 共用同一个 CircuitBreaker 实例」，
而非「所有 provider 共用一个状态机」。该实例内部按 provider 名分别维护
独立的 _BreakerState，因此仍是 per-provider 的。
"""
