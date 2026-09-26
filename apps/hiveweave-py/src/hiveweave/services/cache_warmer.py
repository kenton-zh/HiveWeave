"""跨 run 续暖 cache-warmer（批 G P1-7③，2026-09-26）。

来源：report `platform-issue-report-四项目慢因-2026-09-26.html` P1-7 ——
实测命中率 89-90% 五轮破 95% 红线，真病灶①：**唤醒间隔 > 缓存窗口**
（间隔 <60s 命中 92.2% → >60min 0.25%，单调坍缩）。上游 pi 的
``packages/coding-agent/src/core/cache-warmer.ts``（453 行，HEAD 2b0a123de）
正是治这个的：用一个定时器在缓存条目过期前重放请求（1-token 输出上限）
把条目续住。本模块移植其**决策数学**（常量逐字照搬，不自研）：

- ``get_cache_warming_delay_ms``：TTL×90%，保底 10s 余量（pi :29-32）；
- 双安全上限：streaming 1h / idle 30min（pi :12-18）——idle 用短上限是因
  为「续跑概率估计随年龄失真」；
- idle 阶段续跑概率 0.15（pi :21-26，"Measured from our own usage; 
  per-session estimates were not better than this constant"）；
- 预期省钱 ≥ $0.05 才发（pi :19-20）；
- 成本估算公式（pi ``evaluate`` :378-400）：warmCost = 全前缀 cache_read
  单价 + 1 输出 token；missCost = 下次真请求丢缓存时的**增量**（write 价
  − read 价）；expectedSavings = p×missCost − warmCost。

与 pi 的**差异（适配裁决）**：
1. pi 在**每次真实请求** ``start()``（streaming 阶段 p=1），
   ``onAgentSettled``（pi :245-255）切 idle；本仓只在 **run 收尾**
   （``Agent._go_idle``，即 onAgentSettled 等价点）armed——run 内
   tool loop 相邻请求秒级相连无窗口可补，且 run 中并发打 LLM 会与
   预算/重试机件搅缠；实测病灶就是 idle 间隙，故只做 idle 阶段。
2. pi 的续跑请求经 ``streamSimple``（maxTokens:1, maxRetries:0）；
   本仓复用 compaction 同款非流式 httpx 路径 + 全局 LLM 信号量 +
   ``build_headers(session_id=agent_id)``（**与主链路同缓存域**，
   见 P1-7②），单发不重试（best-effort，失败静默）。
3. pi 的 TTL 取自 ``model.promptCache[short|long]``（pi :39-46）；本仓
   llm_models 无该列 ⇒ 按协议推：anthropic 随
   ``cache_control_long_ttl`` 开关 1h/5min，openai 系 5min（report
   实测窗口 5-10min 单调坍缩，取下沿），google 无证据 → 不续暖。
4. pi ``isReplayable``（pi :55-58）：anthropic + 预算式 thinking 的
   重放会改 budget_tokens（Anthropic 以其键缓存）⇒ 不可安全重放。
   本仓无 forceAdaptiveThinking 标志 ⇒ 保守全跳过。
5. 快照新鲜度卫兵 15min（pi 无此需要——它逐请求 start；本仓
   observe 与 arm 分离，_go_idle 可能与最后一次请求隔很远）。

软失败契约：续暖是纯增益优化，任何失败不得影响 agent 主流程——
全路径 try/except + 单发不重试。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ── 决策数学常量（逐字移植 pi cache-warmer.ts :12-26）──────────────

#: Streaming 阶段续暖总窗（"Streaming warming never continues past this
#: long after the real request that started it."）——本仓 idle-only，仅
#: 供 phase 参数表完整保留。
MAX_WARMING_AGE_MS = 60 * 60_000
#: Idle 续暖总窗（"Idle warming uses a shorter horizon because
#: continuation estimates become less reliable with age."）
MAX_IDLE_WARMING_AGE_MS = 30 * 60_000
#: 预期省钱阈值（"A refresh is sent only when it is expected to save at
#: least this many dollars."）
CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS = 0.05
#: Idle 续跑概率（"Chance that a real request arrives before the cache
#: entry expires while the agent sits idle."）
IDLE_CONTINUATION_PROBABILITY = 0.15
#: 重排延迟下限余量（pi :28-31：TTL ≤10s 不续；否则保 10s 安全余量）
_WARM_MARGIN_MS = 10_000

#: observe 与 arm 的最大间隔（本仓适配项，理由见模块头 §5）。
WARM_SNAPSHOT_MAX_AGE_MS = 15 * 60_000

# TODO(批 G 遗留)：本仓 llm_models / global_settings **无单价列**
# （schema.py llm_models 无 price 字段），无法按模型取价。保守默认取
# Anthropic Sonnet 档公开价（$/1M tokens：input 3 / cache_write 3.75 /
# cache_read 0.30 / output 15）—— 上游 pi 的 $0.05 阈值就是按这档价
# 校准的工作点（idle 续暖的最小有值前缀 ≈ 23 万 token；更便宜模型在该
# 阈值下几乎不会 idle 续暖，TTL 开关才是它们的主杠杆）。单价**高估**会
# 多花续暖钱、**低估**会漏暖——取上游同档最不意外。llm_models 加 price
# 列后，compute_warm_decision 的 prices 参数直接接 DB 值替换本表。
DEFAULT_PRICES: dict[str, float] = {
    "input": 3.0,
    "output": 15.0,
    "cache_read": 0.30,
    "cache_write": 3.75,
}


def get_cache_warming_delay_ms(ttl_ms: int) -> int | None:
    """TTL×90% 触发延迟，保底 10s 余量（pi ``getCacheWarmingDelayMs`` :29-32）。

    原话："Refresh at 90% of the TTL while preserving at least ten seconds
    of margin."。TTL ≤10s ⇒ None（不可续）。
    """
    if ttl_ms <= _WARM_MARGIN_MS:
        return None
    return max(1, int(min(ttl_ms * 0.9, ttl_ms - _WARM_MARGIN_MS)))


def estimate_prompt_cache_ttl_ms(api_format: str | None) -> int | None:
    """按协议推 prompt cache 条目寿命（pi ``getPromptCacheTtlMs`` 的适配版）。

    pi 原话（:34-38）："Lifetime of the prompt cache entry a request
    writes, from the model's `promptCache` tier for the retention the
    request used. Undefined when the model has no lifetime for that tier
    or caching is off." 本仓模型表无 promptCache 列，按协议静态推：
    - anthropic：``cache_control_long_ttl`` 开 ⇒ 1h（断点 TTL 同源），
      关 ⇒ 5min（Anthropic 默认 ephemeral 窗口）；
    - openai 系（openai / openai-compatible / openai-responses）：隐式
      prefix caching，report 实测窗口 5-10min 单调坍缩 ⇒ 保守取 5min；
    - 其他（google 等）：无可靠证据 ⇒ None ⇒ 不续暖（同 pi 的
      "cache lifetime unavailable" → stop 语义）。
    """
    fmt = (api_format or "").strip().lower()
    if fmt == "anthropic":
        from hiveweave.config import settings

        if getattr(settings, "cache_control_long_ttl", True):
            return 60 * 60_000
        return 5 * 60_000
    if fmt in ("openai", "openai-compatible", "openai-responses"):
        return 5 * 60_000
    return None


def compute_warm_decision(
    prompt_tokens: int,
    prices: dict[str, float] | None = None,
    *,
    phase: str = "idle",
) -> dict[str, Any]:
    """warm-or-stop 决策（pi ``evaluate`` :378-400 的纯函数化移植）。

    公式（pi 原样）::

        cacheHitCost  = price(cacheRead: promptTokens)
        cacheMissCost = price(cacheWrite>0 ? cacheWrite : input: promptTokens)
        warmCost      = price(cacheRead: promptTokens, output: 1)
        missCost      = max(0, cacheMissCost − cacheHitCost)
        continuationProbability = idle ? 0.15 : 1
        expectedSavings = p × missCost − warmCost
        action = expectedSavings ≥ 0.05 ? "warm" : "stop"
        economicsAvailable = promptTokens > 0 ∧ (hitCost > 0 ∨ missCost > 0)
    """
    p = dict(DEFAULT_PRICES if prices is None else prices)
    n = max(0, int(prompt_tokens or 0))
    read_rate = p.get("cache_read", 0.0) / 1_000_000
    write_rate = p.get("cache_write", 0.0)
    paid_rate = (write_rate if write_rate > 0 else p.get("input", 0.0)) / 1_000_000
    cache_hit_cost = n * read_rate
    cache_miss_cost = n * paid_rate
    warm_cost = n * read_rate + p.get("output", 0.0) / 1_000_000
    miss_cost = max(0.0, cache_miss_cost - cache_hit_cost)
    continuation_probability = (
        IDLE_CONTINUATION_PROBABILITY if phase == "idle" else 1.0
    )
    economics_available = bool(n > 0 and (cache_hit_cost > 0 or cache_miss_cost > 0))
    expected_savings = continuation_probability * miss_cost - warm_cost
    action = (
        "warm"
        if expected_savings >= CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS
        else "stop"
    )
    return {
        "phase": phase,
        "warm_cost": warm_cost,
        "miss_cost": miss_cost,
        "continuation_probability": continuation_probability,
        "expected_savings": expected_savings,
        "economics_available": economics_available,
        "action": action,
    }


class _WarmRun:
    """一个 agent 的在武装续暖任务（对应 pi ``ActiveRun`` 的 idle 子集）。"""

    __slots__ = (
        "agent_id",
        "project_id",
        "model_config",
        "messages",
        "tools",
        "prompt_tokens",
        "ttl_ms",
        "delay_ms",
        "started_at",
        "phase",
        "task",
    )

    def __init__(
        self,
        agent_id: str,
        project_id: str | None,
        model_config: dict,
        messages: list[dict],
        tools: list[dict] | None,
        prompt_tokens: int,
        ttl_ms: int,
        delay_ms: int,
        started_at: float,
    ) -> None:
        self.agent_id = agent_id
        self.project_id = project_id
        self.model_config = model_config
        self.messages = messages
        self.tools = tools
        self.prompt_tokens = prompt_tokens
        self.ttl_ms = ttl_ms
        self.delay_ms = delay_ms
        self.started_at = started_at
        self.phase = "idle"
        self.task: asyncio.Task | None = None


class CacheWarmer:
    """per-agent 续暖调度（单例，进程级）。

    生命周期（对齐 pi ``CacheWarmer`` 的 start/onAgentSettled/cancel）：
    - ``observe_request``：每次真实 LLM 请求收尾时记录快照（纯记录，无副作用）；
    - ``on_agent_settled``：run 收尾（``_go_idle``）时 armed 定时续暖；
    - ``cancel``：agent 再次活动（下一次 ``_run_llm`` 发请求前）取消；
    - ``cancel_all``：进程关闭。
    新武装替换旧武装（pi ``start`` 的 "replaces any previous run" 语义）。
    """

    def __init__(self) -> None:
        self._latest: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, _WarmRun] = {}

    # ── 记录 / 武装 / 取消 ────────────────────────────────────

    def observe_request(
        self,
        agent_id: str,
        project_id: str | None,
        model_config: dict,
        messages: list[dict],
        tools: list[dict] | None,
        prompt_tokens: int = 0,
    ) -> None:
        """记录该 agent 最近一次真实请求快照（pi ``CacheWarmRequest`` 等价物）。

        消息列表做浅拷贝钉住当前形态（"exactly as it was sent"，pi :134）；
        后续 run 重建消息数组不改写旧 dict。best-effort：永不 raise。
        """
        try:
            self._latest[agent_id] = {
                "project_id": project_id,
                "model_config": model_config,
                "messages": list(messages),
                "tools": tools,
                "prompt_tokens": int(prompt_tokens or 0),
                "ts": time.monotonic(),
            }
        except Exception as e:  # noqa: BLE001 — 观测不得影响主流程
            log.debug("cache_warmer_observe_failed", agent_id=agent_id, error=str(e))

    def on_agent_settled(self, agent_id: str) -> None:
        """run 收尾武装续暖（pi ``onAgentSettled`` 的 idle 语义）。

        快照过旧（> ``WARM_SNAPSHOT_MAX_AGE_MS``）或开关关 ⇒ 不武装。
        任何失败静默（软失败契约）。
        """
        try:
            from hiveweave.config import settings

            if not getattr(settings, "cache_warmer_enabled", True):
                return
            snap = self._latest.get(agent_id)
            if not snap:
                return
            age_ms = (time.monotonic() - float(snap.get("ts") or 0.0)) * 1000.0
            if age_ms > WARM_SNAPSHOT_MAX_AGE_MS:
                log.debug(
                    "cache_warmer_snapshot_stale",
                    agent_id=agent_id,
                    age_ms=int(age_ms),
                )
                return
            api_format = str((snap.get("model_config") or {}).get("provider_type") or "")
            ttl_ms = estimate_prompt_cache_ttl_ms(api_format)
            if ttl_ms is None:
                return  # "cache lifetime unavailable" → 不续
            delay_ms = get_cache_warming_delay_ms(ttl_ms)
            if delay_ms is None:
                return
            # ⭐ P1-1（审计 2026-09-26）：delay 超过 idle 安全上限 ⇒ **不武装**。
            # 病灶：默认 long_ttl=True ⇒ anthropic TTL=1h ⇒ delay=54min >
            # MAX_IDLE_WARMING_AGE_MS(30min) ⇒ _warm_loop 首轮 age-limit
            # 即 return —— armed 日志后一个续暖都不发（armed 即死），遥测
            # 还误导。1h 档的前缀存活靠 HIVEWEAVE_CACHE_CONTROL_LONG_TTL
            # 断点长 TTL 本身（写 1h 条目），不需要也不该续暖；idle 续暖
            # 仅对 delay ≤ 30min 的档位（当前即 5min 档）生效。
            if delay_ms > MAX_IDLE_WARMING_AGE_MS:
                log.info(
                    "cache_warmer_skip_ttl_exceeds_idle_horizon",
                    agent_id=agent_id,
                    ttl_ms=ttl_ms,
                    delay_ms=delay_ms,
                    idle_horizon_ms=MAX_IDLE_WARMING_AGE_MS,
                    note="1h 档靠 TTL 开关存活；idle 续暖仅 5min 档生效",
                )
                return
            # anthropic + thinking：重放改 budget_tokens ⇒ 缓存键漂移
            # （pi ``isReplayable`` :55-58，本仓保守全跳过）
            if api_format == "anthropic" and bool(
                (snap.get("model_config") or {}).get("supports_thinking")
            ):
                return
            self.cancel(agent_id)  # 替换旧武装（先清计时器）
            run = _WarmRun(
                agent_id=agent_id,
                project_id=snap.get("project_id"),
                model_config=snap["model_config"],
                messages=list(snap["messages"]),
                tools=snap.get("tools"),
                prompt_tokens=int(snap.get("prompt_tokens") or 0),
                ttl_ms=ttl_ms,
                delay_ms=delay_ms,
                started_at=time.monotonic(),
            )
            run.task = asyncio.create_task(self._warm_loop(run))
            self._runs[agent_id] = run
            log.info(
                "cache_warmer_armed",
                agent_id=agent_id,
                ttl_ms=ttl_ms,
                delay_ms=delay_ms,
                prompt_tokens=run.prompt_tokens,
            )
        except Exception as e:  # noqa: BLE001 — 续暖不得影响收口路径
            log.debug("cache_warmer_arm_failed", agent_id=agent_id, error=str(e))

    def cancel(self, agent_id: str) -> None:
        """取消该 agent 的在武装续暖（agent 再次活动 / 被停时调用）。"""
        run = self._runs.pop(agent_id, None)
        if run is None:
            return
        if run.task and not run.task.done():
            run.task.cancel()
        log.info("cache_warmer_cancelled", agent_id=agent_id)

    def cancel_all(self) -> None:
        """进程关闭时取消全部在武装任务（main.py lifespan shutdown）。"""
        for agent_id in list(self._runs):
            self.cancel(agent_id)

    # ── 续暖循环（pi ``schedule``/``refresh`` 的 idle 子集）──────

    async def _warm_loop(self, run: _WarmRun) -> None:
        try:
            while True:
                now = time.monotonic()
                # ⚠ 单位：delay/ttl/上限常量一律毫秒（照搬 pi），monotonic
                # 运算一律秒 —— 在此处统一换算，勿把 ms 直接喂 sleep。
                delay_s = run.delay_ms / 1000.0
                next_warm_at = now + delay_s
                # 半余量迟到卫兵（pi :286-290 原话："A timer can run late
                # after sleep or event-loop blockage. Keep half of the
                # planned pre-expiry margin for that delay … a late refresh
                # is likely a full-price cache write, not a cache warm."）
                refresh_deadline = next_warm_at + (
                    (run.ttl_ms - run.delay_ms) / 2
                ) / 1000.0
                age_limit = (
                    run.started_at
                    + (
                        MAX_IDLE_WARMING_AGE_MS
                        if run.phase == "idle"
                        else MAX_WARMING_AGE_MS
                    )
                    / 1000.0
                )
                if next_warm_at > age_limit or now >= age_limit:
                    log.info(
                        "cache_warmer_age_limit",
                        agent_id=run.agent_id,
                        phase=run.phase,
                    )
                    return
                await asyncio.sleep(max(0.0, next_warm_at - time.monotonic()))
                if self._runs.get(run.agent_id) is not run:
                    return  # 已被替换/取消
                if time.monotonic() > refresh_deadline:
                    log.info(
                        "cache_warmer_deadline_missed", agent_id=run.agent_id
                    )
                    self._runs.pop(run.agent_id, None)
                    return
                decision = compute_warm_decision(
                    run.prompt_tokens, phase=run.phase
                )
                if decision["action"] != "warm":
                    reason = (
                        "expected savings below threshold"
                        if decision["economics_available"]
                        else "cache economics unavailable"
                    )
                    log.info(
                        "cache_warmer_stopped",
                        agent_id=run.agent_id,
                        reason=reason,
                        expected_savings=round(
                            float(decision["expected_savings"]), 6
                        ),
                    )
                    self._runs.pop(run.agent_id, None)
                    return
                try:
                    await self._send_warm_request(run)
                except Exception as send_err:  # noqa: BLE001
                    # 单次续暖失败不终止续暖（对齐 pi ``refresh`` 的
                    # catch-and-reschedule："Cache warming is best-effort
                    # and must not affect the active agent run."）；
                    # 终止只由 idle 上限 / cancel / 决策 stop 决定。
                    log.debug(
                        "cache_warmer_send_failed",
                        agent_id=run.agent_id,
                        error=str(send_err),
                    )
                # 续住后再排下一轮，直到 idle 上限（pi :354
                # "if (this.run === run) this.schedule(run)"）
        except asyncio.CancelledError:
            return  # cancel()/cancel_all() 的正常退出路径
        except Exception as e:  # noqa: BLE001 — best-effort，绝不外溢
            log.warning(
                "cache_warmer_loop_error", agent_id=run.agent_id, error=str(e)
            )
        finally:
            if self._runs.get(run.agent_id) is run:
                self._runs.pop(run.agent_id, None)

    async def _send_warm_request(self, run: _WarmRun) -> None:
        """发续暖请求：当前会话前缀 + max_tokens=1 的真实请求。

        复用 provider 机件（``provider_factory`` / ``build_body``——
        anthropic 断点随 ``supports_prompt_cache`` 自动注入，与主链路
        同形）+ ``build_headers(session_id=agent_id)``（与主链路同缓存
        域，P1-7②）+ 全局 LLM 信号量。成功后按 ``request_type=
        "cache_warm"`` 落 token meter（与 compaction 的 F3 打点同纪律：
        绕过 Streamer 的调用必须单独记账）。
        """
        import httpx

        from hiveweave.llm.provider import provider_factory
        from hiveweave.llm.streamer.constants import _get_llm_semaphore
        from hiveweave.services.token_meter import token_meter

        started = time.monotonic()
        provider = provider_factory.create(run.model_config)
        # ⭐ P1-3（审计 2026-09-26）：**复刻主链路哨兵规则** —— http_stream
        # （:281-288）发请求前对「末条非 user」的请求追加 CONTINUE_SENTINEL
        # user 哨兵（跳过网关 tool id 签名校验），而 tool loop 多处对
        # messages 原地 append ⇒ run 收尾快照的尾条几乎恒非 user ⇒ 线上
        # 请求的末断点条目键含哨兵块，重放若不带 ⇒ 命中只到 run 首请求
        # 条目、尾部全价，warmCost 系统性低估 10-20×（$0.05 闸门失真）。
        # 哨兵是每请求的临时副本（不入库、不回写），快照按「恰如落库时」
        # 钉形状是对的；重放时套同一条规则即与线上 wire 形状逐字节一致。
        # ⚠ 常量从 llm/streamer/constants 导入（与 http_stream 同源），
        # 勿另立文案。
        from hiveweave.llm.streamer.constants import CONTINUE_SENTINEL

        warm_messages = list(run.messages)  # 不改写快照
        if warm_messages and warm_messages[-1].get("role") != "user":
            warm_messages.append({"role": "user", "content": CONTINUE_SENTINEL})
        body = provider.build_body(
            messages=warm_messages,
            stream=False,
            temperature=0.0,
            max_tokens=1,
            tools=run.tools,
        )
        headers = provider.build_headers(session_id=run.agent_id)
        headers["Accept"] = "application/json"
        sem = _get_llm_semaphore()
        async with sem:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    provider.build_url(), json=body, headers=headers
                )
        if resp.status_code != 200:
            log.info(
                "cache_warmer_http_error",
                agent_id=run.agent_id,
                status=resp.status_code,
            )
            return
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            log.debug("cache_warmer_bad_json", agent_id=run.agent_id, error=str(e))
            return
        usage = data.get("usage")
        if not usage and isinstance(data.get("response"), dict):
            usage = data["response"].get("usage")
        details: dict[str, Any] = {}
        if isinstance(usage, dict):
            details = (
                usage.get("prompt_tokens_details")
                or usage.get("input_tokens_details")
                or {}
            )
        raw = {
            "input": (usage or {}).get("prompt_tokens")
            or (usage or {}).get("input_tokens")
            or 0,
            "output": (usage or {}).get("completion_tokens")
            or (usage or {}).get("output_tokens")
            or 0,
            "prompt_tokens_details": details,
            "prompt_cache_hit_tokens": (usage or {}).get("prompt_cache_hit_tokens"),
            "prompt_cache_miss_tokens": (usage or {}).get("prompt_cache_miss_tokens"),
        }
        # 透传 provider 原字段（与 compaction 记账同纪律：按键存在判 reported）。
        # ⭐ P1-2（审计 2026-09-26）：anthropic 的 ``cache_read_input_tokens``
        # 必须映射进 raw["cache_read"] —— llm/util.openai_wire_cache_read
        # 只认 cached_tokens / prompt_cache_hit_tokens / cache_read / cached，
        # 漏映射 ⇒ 续暖命中被记成 cache_read=0（warm 成本账全错）。
        for wire_field, src in (
            ("cache_read", "cache_read_input_tokens"),
            ("cache_creation", "cache_creation_input_tokens"),
            ("cache_creation_tokens", "cache_creation_tokens"),
        ):
            if (usage or {}).get(src) is not None:
                raw[wire_field] = (usage or {}).get(src)
        try:
            from hiveweave.llm.util import normalize_usage

            norm = normalize_usage(raw, provider.api_format.value)
            await token_meter.record_rounds(
                run.agent_id,
                run.project_id,
                [
                    {
                        "input": norm.get("input", 0),
                        "output": norm.get("output", 0),
                        "cache_read": norm.get("cache_read", 0),
                        "cache_creation": norm.get("cache_creation", 0),
                        "total": norm.get("total", 0),
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "cache_creation_reported": bool(
                            norm.get("cache_creation_reported")
                        ),
                    }
                ],
                model_id=str(run.model_config.get("model_id") or ""),
                provider=provider.api_format.value,
                request_type="cache_warm",
            )
        except Exception as e:  # noqa: BLE001 — 记账失败不影响续暖本体
            log.debug("cache_warmer_meter_failed", agent_id=run.agent_id, error=str(e))
        log.info(
            "cache_warm_sent",
            agent_id=run.agent_id,
            model=str(run.model_config.get("model_id") or ""),
            prompt_tokens=run.prompt_tokens,
        )


# 单例（agent.py / main.py 引用）
cache_warmer = CacheWarmer()
