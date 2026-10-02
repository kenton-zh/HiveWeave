"""重试逻辑 — 波次级骑乘（无界重试+节流）+ Retry-After header 解析。

契约 01: LLM 流式调用 — 重试与熔断
- 可重试状态码: 429 + 全量 5xx（RETRYABLE_STATUSES）
- 主链路（streamer）：**波次级无界骑乘**（T33，2026-09-29 设计更新，对照
  DSH ``mode:'always'`` retryPolicy）—— 同 run 内上游级失败（5xx/零分块/
  连接失败）按节流节奏无限重试，直到成功或终止信号（用户停止 / 会话取消 /
  deadline 预算到点 / 故障转移闸中止）。实测病灶：上游 500 波次单波
  1.5-3 分钟、波间隔 10-20 分钟，有限梯（6 连 ~90s）必杀 run。
- 通用/辅助路径（vision 等）：有界，MAX_RETRIES=8（env 可调）。
- 节流节奏: 短指数 ramp（1/2/4/8s，jitter ±25%）后进入稳态 10-15s；
  Retry-After ≤60s 原样尊重，>60s 忽略（配额级窗口走 park/恢复定时器）；
  计算值受 retry_max_delay_ms()（env，默认 45s）单次封顶。
- T34 故障转移闸（``failover_gate``）：每次上游级失败咨询一次；闸返回
  True ⇒ 抛 :class:`RetryAborted`，由调用方换模型重启链。
- ``deadline``（monotonic 刻度）：下一次退避睡眠将越过截止 ⇒ 停止重试并
  抛最后一次错误（交既有 error→durable recovery 路收口），骑乘不越预算。
- 解析 Retry-After header（OpenAI retry-after-ms + 标准 retry-after 秒/HTTP-date）

注意:
- 本模块仅处理「单次 HTTP 请求」级别的重试，由 streamer 在 tool loop 每轮调用。
- 空响应重试（无 content 无 tool_calls）由 streamer 自行处理，不走本模块。
- 超时重试同样由 streamer 的 request_with_retry 驱动，本模块提供 should_retry 判定。
"""

from __future__ import annotations

import asyncio
import os as _os
import random
import re
import time as _time
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, TypeVar

import structlog

from hiveweave.llm.error_codes import ErrorCode, classify_error

log = structlog.get_logger(__name__)

# ── 常量 ────────────────────────────────────────────────────
MAX_RETRIES = 8
"""有界路径的最大重试次数（不含首次请求）；主链路默认无界（见下）。

原值 5。T33（2026-09-29）：任务口径「重试次数 6 → 8」按总尝试计
（5+1=6 → 8 次重试）。通用 RetryHandler 默认走本值（vision 等辅路）；
主链路（streamer）经 :func:`main_ride_budget` 默认传 None = 波次级无界骑乘，
本 env 同时作主链路的紧急刹车（>0 = 有界）。
"""
BASE_DELAY_MS = 1_000
"""基础退避延迟（1 秒）。用户指定 base=1s。"""

MAX_DELAY_MS = 45_000
"""单次退避帽的**默认值**（45 秒）。运行时以 :func:`retry_max_delay_ms`
（env ``HIVEWEAVE_LLM_RETRY_MAX_DELAY_S``）为准；保留常量供既有导入方
（llm/streamer/oneshot.py）与测试引用。原值 30s。"""

RETRY_AFTER_HONOR_CAP_MS = 60_000
"""Retry-After 尊重上限（60 秒）。≤60s 原样尊重（上游显式指令优先于
本地节奏）；>60s 视为配额级窗口信号，忽略（走 park / 恢复定时器治理，
不进逐次重试）。"""

RIDE_STEADY_AFTER_ATTEMPT = 4
"""进入稳态骑乘节奏的 attempt 阈值（0 起算）。attempt < 4 走短指数 ramp
（1/2/4/8s），≥4 走稳态 10-15s。"""

RIDE_STEADY_MIN_MS = 10_000
"""波次骑乘稳态节奏下限（10 秒）。"""

RIDE_STEADY_MAX_MS = 15_000
"""波次骑乘稳态节奏上限（15 秒）。"""


def _env_float(name: str, default: float) -> float:
    """env 读取浮点秒数——非法值回退默认，绝不在导入期/调用期抛异常。"""
    raw = _os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("retry.bad_env", key=name, value=raw, default=default)
        return default
    return value if value > 0 else default


def retry_max_delay_ms() -> int:
    """单次退避帽（毫秒）——env ``HIVEWEAVE_LLM_RETRY_MAX_DELAY_S``（秒）。

    默认 45s（T33，原 30s）。只封顶**本地计算**的退避（ramp/稳态节奏）；
    Retry-After ≤60s 是上游显式指令，原样尊重不受本帽约束。
    """
    return int(_env_float("HIVEWEAVE_LLM_RETRY_MAX_DELAY_S", MAX_DELAY_MS / 1000.0) * 1000)


def bounded_retry_budget() -> int:
    """有界路径的重试预算：env ``HIVEWEAVE_LLM_RETRY_MAX_RETRIES``。

    unset / 非法 / ≤0 ⇒ 默认 MAX_RETRIES（8）。vision 等辅助 RetryHandler
    走本值；主链路用 :func:`main_ride_budget`（默认无界）。
    """
    raw = _os.environ.get("HIVEWEAVE_LLM_RETRY_MAX_RETRIES")
    if raw is None or not str(raw).strip():
        return MAX_RETRIES
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return MAX_RETRIES
    return v if v > 0 else MAX_RETRIES


def main_ride_budget() -> int | None:
    """主链路（streamer）重试预算：**默认 None = 波次级无界骑乘**。

    env ``HIVEWEAVE_LLM_RETRY_MAX_RETRIES``：>0 = 有界（紧急刹车，运维
    钳制骑乘总次数）；unset / ≤0 / 非法 = None（无界，靠 deadline/取消/
    故障转移闸终止）。T33 设计更新（2026-09-29）：无界骑乘是「骑过波次」
    的根因修复，有限梯（6 连 ~90s）在持续波次下必杀 run。
    """
    raw = _os.environ.get("HIVEWEAVE_LLM_RETRY_MAX_RETRIES")
    if raw is None or not str(raw).strip():
        return None
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


RIDE_WALL_CAP_DEFAULT_S = 900.0
"""单条重试链（一次 ``with_retry`` 调用）的兜底墙钟默认值（秒）。

无界骑乘在 **auto 模式**（T16：turn 墙钟退役，budget_deadline=None）下
没有外部墙 —— 本帽是最后一道防线：到点停止骑乘并抛最后一次错误，交既有
error → durable recovery 路。legacy 模式下 turn 预算（~540s）先于本帽
生效（min 取小）。900s ≈ 覆盖数个实测波次（1.5-3min/波）+ 恢复余量。
"""


def ride_wall_cap_s() -> float:
    """骑乘兜底墙（秒）：env ``HIVEWEAVE_LLM_RIDE_WALL_CAP_S``，默认 900。"""
    return _env_float("HIVEWEAVE_LLM_RIDE_WALL_CAP_S", RIDE_WALL_CAP_DEFAULT_S)


class RetryAborted(Exception):
    """重试链被外部策略主动中止（T34 故障转移闸）。

    与 RetryableError（预算耗尽）/ PermanentError（不可重试）三分：
    本异常 = 链**还有**重试预算，但故障转移闸判定「换模型比继续骑更优」，
    调用方（http_stream）捕获后切换 provider 并重启链。

    ``last_error``：触发中止的最后一次上游错误（防死循环兜底时可复抛）。
    """

    def __init__(self, message: str, *, last_error: BaseException | None = None) -> None:
        super().__init__(message)
        self.last_error = last_error

RETRYABLE_STATUSES: frozenset[int] = frozenset(range(500, 600)) | frozenset({429})
"""可重试的 HTTP 状态码：429 + 全量 5xx。

历史实现只认 {429, 503, 504, 529}，但多厂商网关（ARK / DeepSeek / OpenAI /
任何 OpenAI-compatible）抽风时常用 500 / 502 / 524。漏掉这些会把瞬态故障
误判为 PermanentError，直接炸掉 agent。对齐 opencode retry.ts 的
`status >= 500` 一律可重试策略。
"""

UNKNOWN_SIGNAL_IS_RETRYABLE: bool = False
"""「没有任何结构化信号」时 `classify_http_error` 的默认分类。

**这是显式决策，不是省略**（fixplan-16items §三 #13：「UNKNOWN 归入"可重试"
还是"永久"要**显式定，不许默认**」）。当前 = 永久（不重试），与既有测试
`tests/test_retry_message_classify.py::test_unknown_error_without_status_is_permanent`
（注释「不盲目重试」）一致。

⚠ **另有一处默认与它相反，且那是有意的** ——
`llm/streamer/http_stream.py` 的「传输层裸错误」分支（`raw` 里既无
`http_status` 也非地域类）默认**重试**，理由是 opaque 网关的瞬态错误被判
Permanent 会秒死（该处有独立注释与 TEST_DSH_47 #8 的实测依据）。两者分界：

  · **本常量** = 「上游给了错误体，但我们不认识它」⇒ 重试多半还是同样的拒绝
    （对应 SSE `error` chunk 与测试里的 `unrecognized provider error`）；
  · **那一处** = 「连接层压根没给结构化信息」⇒ 多半是瞬态。

⇒ 改任一处**必须同时看另一处**。两处的默认值由
`tests/test_unknown_error_classification_contract.py` 钉在同一个测试里，
改动即转红。
"""

# 消息内容级可重试模式（厂商无关，移植自 opencode retry.ts RETRYABLE_MESSAGE_PATTERNS）。
# 适用场景：网关在 HTTP 200 / 非标准 4xx body 里包瞬态错误文本（如
# "upstream server error"、"rate limit reached"），仅靠状态码无法识别。
# 注意：这里匹配的是错误 body / 错误消息文本，不是正常响应流。
#
# 状态码单条用 (?<![\d])…(?![\d]) 锚定为独立数字，避免误命中真实业务数字
# （"requested 100500 tokens"→500 / 日期→429 / "524288"→524）。real 事故见
# tests/test_retry_message_classify.py::test_non_retryable。
_RETRYABLE_MESSAGE_PATTERNS: tuple[str, ...] = (
    r"(?<![\d])(?:429|5\d{2})(?![\d])",
    r"rate increased too quickly|rate limit|rate-limit|rate_limit|too many requests|too_many_requests",
    r"overloaded|service unavailable|service_unavailable|service-unavailable|"
    r"internal error|internal_error|internal server error|server error|server_error|server-error|"
    r"provider returned error|provider_returned_error|provider-returned-error",
    r"terminated|fetch failed|failed to fetch|network error|upstream connect|"
    r"connection error|connection refused|connection lost|"
    r"socket connection was closed|socket hang up|reset by peer|getaddrinfo|gai_error|"
    r"enotfound|eai_again|econnrefused|econnreset|etimedout",
    r"^timeout$|\b(?:request|response|connection|network|stream|read) (?:timeout|timed out|time out)\b",
    r"try your request|retry your request|resource exhausted|resource_exhausted",
    # TLS / 连接被上游截断：TEST_DSH_29 实测 57 次重试 100% 属这一类
    # （"[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of
    # protocol"）。httpx 通常包成 ConnectError/ReadError（已由
    # should_retry_exception 覆盖），但走 body/消息分类路径时也必须命中，
    # 否则被误判为永久错误直接炸掉整个 turn。
    r"unexpected_eof|unexpected eof|eof occurred|violation of protocol|"
    r"sslerror|ssl error|tlsv\d|decryption failed|bad record mac|"
    r"handshake failure|stream ended|incomplete ?read",
    # 中文厂商文案（ARK / 火山引擎 / 字节豆包 / 通义等常见 on-line 网关）：
    # 英文模式对中文错误文本 miss，导致瞬态错误被误判为永久错误。
    r"限流|请求过于频繁|访问频率过高|触发(?:了)?限流",
    r"过载|服务器(?:内部|异常|不可用|繁忙)|服务(?:繁忙|不可用)|上游|暂时无法|暂不可用|"
    r"连接(?:失败|中断|重置|超时)|网络连接|读超时|(?:请求|响应|读)超时",
    r"请(?:稍后|重新)重试|请(?:您)?重试|稍后重试|临时(?:故障|问题)",
)
_COMPILED_RETRYABLE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE) for p in _RETRYABLE_MESSAGE_PATTERNS
)

T = TypeVar("T")


# ── 状态码判定 ──────────────────────────────────────────────

def is_retryable_status(status: int) -> bool:
    """判断 HTTP 状态码是否可重试（429 + 全量 5xx）。"""
    return status in RETRYABLE_STATUSES


# ── E7: 容量错误分类（vs 瞬时限流）─────────────────────────────
# 复盘致命链二：daily_quota / GoUsageLimitError 是**容量耗尽**（5 小时滚动
# 配额 / 日配额），秒级退避重试无济于事，只会白撞 75 个 error runs。容量
# 错误的恢复钥匙是「配额窗口重置」，应进项目级暂停/排队，而不是逐次重试。
_CAPACITY_NEEDLES: tuple[str, ...] = (
    "gousagelimiterror",        # muse / ox 5 小时滚动配额
    "usage_limit_reached",
    "usage limit exceeded",
    "daily_quota",
    "daily quota",
    "quota exhausted",
    "quota_exhausted",
    "quota exceeded",
    "quota_exceeded",
    "insufficient quota",
    # #13 批 B（2026-09-18）：并入原 error_codes._QUOTA_RE 的成员（该表已
    # 删除、由本表作为 QUOTA/容量词的**唯一事实源**）—— 「余额/billing」
    # 与配额同族（恢复钥匙都是充值/配额重置，不是重试）。
    # ⚠ 裸 "quota" **不进本表**（推送前全量实锤）：本表同时是 E7 容量链
    # （RetryHandler「不逐次重试」）的判据，"resource exhausted: quota for
    # current provider" 是瞬态 429 族（test_e7_capacity_slowdown 钉住它
    # 必须逐次重试）。裸 quota 的 QUOTA 分类覆盖放在
    # error_codes._quota_text_hit（稳定码层），与本表（E7 行为层）分离。
    "billing",
    "insufficient balance",
    "insufficient funds",
    "余额不足",
    "欠费",
    # ⚠ 登记在案的残余收窄（09-18 审计）：旧 _QUOTA_RE 的
    # ``insufficient.*balance`` 是**通配**（"insufficient account balance"
    # 也命中）；本表是子串 needles，跨词形态（insufficient … balance 中间
    # 插词）不再覆盖。补齐 = 往文本规则里继续堆词表 = 已知反模式，故显式
    # 登记不补。
)


def is_capacity_error(message: str) -> bool:
    """是否容量类错误（配额耗尽）。

    与瞬时限流（typical 429 rate limit）区分：容量错误的重置以「窗口」计，
    进重试只会白撞；is_daily_quota（header 解析 > 10min）在此之上给出
    确定的重置时刻。

    ⚠ #13 批 B（2026-09-18）：本表的 needles 是 **QUOTA/容量词的唯一事实
    源** —— ``error_codes.classify_error`` 的 QUOTA 文案层也走这里（原
    ``error_codes._QUOTA_RE`` 已删）。改词表 = 同时改 E7 容量链与稳定码
    分类的 QUOTA 认定，一处改动两边生效。
    """
    if not message:
        return False
    m = str(message).lower()
    return any(n in m for n in _CAPACITY_NEEDLES)


# 窗口词：容量错误触发「项目级暂停」需要重置窗口信号（daily/hourly/window/
# reset 或中文 日/小时/窗口/滚动），避免把普通每分钟限流误判成 1 小时暂停。
_WINDOW_NEEDLES: tuple[str, ...] = (
    "window", "daily", "hourly", "reset",
    "滚动", "窗口", "日配额", "每日", "小时",
)


def is_window_quota_error(message: str, *, is_daily: bool = False) -> bool:
    """容量错误且带明确窗口重置信号（可安全做项目级暂停）。

    比 ``is_capacity_error`` 更窄：后者只用于 RetryHandler「不逐次重试」
    （宽判防白撞），前者用于「组织级降速到重置窗口」（窄判防过度冷却）。
    """
    if is_daily:
        return True
    if not message:
        return False
    m = str(message).lower()
    if not is_capacity_error(m):
        return False
    return any(w in m for w in _WINDOW_NEEDLES)


def matches_retryable_message(value: str) -> bool:
    """判断错误文本是否命中可重试内容模式（厂商无关）。

    移植 opencode retry.ts：错误消息即使状态码正常（如 HTTP 200 / 非标准
    4xx），只要文本命中速率限制 / 过载 / 网络问题 / 明确要求重试等模式，
    就应视为瞬态故障重试。厂商无关，写死的只是通用英文错误文本。
    """
    return any(pattern.search(value) for pattern in _COMPILED_RETRYABLE_PATTERNS)


#: 上游/环境类 stream error 关键字表 —— **唯一登记点**（#13 批 B / F9-C，
#: 2026-09-18）。此前 ``agents/agent.py::_MAIN_LOOP_UPSTREAM_KEYWORDS``（9 词）
#: 与 ``tools/subagent.py::_UPSTREAM_FAILURE_KEYWORDS``（**15 词**，与本表
#: **集合完全相同**）是两份并行表、注释自陈「刻意不共享导入」——同族第 3
#: 次复发。收编到 llm 层后 agents/tools 都只依赖 llm，分层顾虑不再成立。
#: ⚠ 语义：**小写子串匹配**，只判「是否上游/环境瞬断」，不是重分类器。
#: agent 主循环因此从 9 词扩到本表全量（rate limit / overloaded / gateway
#: 等同为瞬态族；主循环每 turn 重试上限 ``_MAIN_LOOP_STREAM_RETRIES`` 不变）。
UPSTREAM_STREAM_ERROR_KEYWORDS: tuple[str, ...] = (
    "stream idle", "ssl", "eof", "connection", "connect",
    "timed out", "timeout", "reset by peer", "broken pipe",
    "circuit breaker", "rate limit", "overloaded", "temporarily",
    "unavailable", "gateway",
)


# ── 地域/不可用类 fast-fail（TEST_DSH_47 #8）────────────────────
# RegionError（模型在当前地域不可用）是确定性不可重试错误：退避重试只会
# 把一次 7s 快死拖成 476s 慢死。必须在分类层显式 fast-fail（PermanentError），
# 不进指数退避、不换重试预算。匹配语义与网关透传文案对齐：
# "RegionError" / "not available in your country/region" / 地域不可用。
_REGION_FASTFAIL_PATTERNS: tuple[str, ...] = (
    r"regionerror",
    r"not available in your (country|region)",
    r"region[^a-z]{0,10}not (available|supported)",
    r"unsupported (country|region)",
    r"地域不可用|当前地区不可用|所在地区不支持",
)

_COMPILED_REGION_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE) for p in _REGION_FASTFAIL_PATTERNS
)


def is_region_unavailable_error(message: str) -> bool:
    """是否地域/不可用类错误（确定性 fast-fail，不重试）。

    TEST_DSH_47 #8：快门三遇 RegionError（476s / 7s / 10s）——首遇慢死
    8 分钟。地域不可用是上游主责，平台侧唯一优化点是首遇 fast-fail。
    """
    if not message:
        return False
    return any(p.search(message) for p in _COMPILED_REGION_PATTERNS)


# ── 上游死亡跨组契约（TEST_DSH_63，方案 B+）────────────────────
# 63 的 6 个 run 死于同一个 30 秒上游抖动窗（4 个在跑流被打断 + 2 个新 run
# 即刻撞墙），各自烧满 300s 才死。B+ 方案：403 RegionError 首次命中即把
# 熔断器 open（不再等 5 次阈值），开启期并行请求在 check() 处秒败，
# 冷却（UPSTREAM_BREAKER_COOLDOWN_S，定义在 circuit_breaker.py）过后
# half_open 探针自动放行恢复。本模块持有跨组契约的**单一事实源**：
# 标记常量 + 判定函数。run 级恢复（组4）与 llm 层熔断（组3）都只 import 这里。

UPSTREAM_BREAKER_MARKER = "upstream_breaker_open"
"""上游熔断开启期快败标记（跨组契约 token）。

熔断开启期的请求被 check() 拒绝时，错误文案**必须**包含本标记 +
provider 名 + 剩余冷却秒数（见 ``Streamer._breaker_open_error``）。
组4 的 run 级恢复、日志回归、外部消费方都靠这个 token 区分
「熔断快败」与「真实上游错误」——改值 = 同时改两处消费方，禁止
只改常量。
"""


def is_upstream_death(err: BaseException | str | None) -> bool:
    """判定是否「上游确定性死亡」（跨组契约的**唯一判据**）。

    ⚠ 本函数是 run 级恢复（组4：自动重醒/通知/占位账）与 llm 层熔断
    （组3：403 → ``CircuitBreaker.open_for``）共享的唯一判据 —— 两边
    **不许**各自另写匹配规则；判据变更只改这里（本 docstring 即契约）。

    True 当（宁窄勿宽）：
      1. ``PermanentError`` 且 ``status == 403``（classify_http_error 的
         地域 fast-fail 主形态）；
      2. 文本（str(err) / err.args / 异常类型名）命中地域不可用族
         （``is_region_unavailable_error``，含 "RegionError" 字面与
         "not available in your country/region" 等 —— 覆盖 http_stream
         transport_raw 无状态码 ``PermanentError(err_text)`` 形态）；
      3. 文本含 ``UPSTREAM_BREAKER_MARKER``（熔断快败结果再入判定的场景）；
      4. ``RetryableError`` 且 ``status`` 为 5xx —— 重试耗尽后的最终错误
         （classify_http_error 的 message 形态即 "HTTP 5xx: ..."）。判据用
         结构化 ``status`` 字段而非解析消息文案，避免文案误命中真实业务数字。

    一律 False：AUTH 401 / 参数 400 / 余额 402 等客户端类 PermanentError、
    429 限流、超时与连接错误（status None，瞬态会自愈）、普通未知错误。
    """
    if err is None:
        return False
    if isinstance(err, PermanentError):
        if getattr(err, "status", None) == 403:
            return True
    if isinstance(err, RetryableError):
        status = getattr(err, "status", None)
        if isinstance(status, int) and 500 <= status < 600:
            return True
    texts = [str(err), type(err).__name__]
    for arg in getattr(err, "args", ()):  # 多参异常的原始消息形态
        texts.append(str(arg))
    for text in texts:
        if not text:
            continue
        if UPSTREAM_BREAKER_MARKER in text:
            return True
        if is_region_unavailable_error(text):
            return True
    return False


# ── 流空转跨组契约（TEST_DSH_64 #3）────────────────────────────
# 64 现场：首 chunk 后流空转原抛 PermanentError（status=None）⇒ 既不进
# with_retry 重试，也不被重醒门认出（is_upstream_death 判 False）——两条
# 死 run 全靠「恰好有未读 inbox」的 30s 冷却运气。终版修法：idle 改判
# RetryableError（同请求专属重试 1 次）→ 耗尽升级 PermanentError，由
# run 级 durable 重醒接管（schedule_upstream_recovery_wait 60/180/600s）。
# ⚠ 本组常量/谓词是**跨组契约**：组2 在 tools/subagent.py 的分类器按
# ``STREAM_IDLE_ERROR_CODE`` 字面量对齐 —— 改名 = 同时改 http_stream 抛出点、
# 本模块谓词、subagent 分类器，禁止只改一处。
# ⚠ 判据收窄红线：**不许**把 idle 纳入 is_upstream_death —— 该谓词与 60s
# 快熔共判据（_open_breaker_if_upstream_death），扩它 = 慢流对全项目关闸。
STREAM_IDLE_ERROR_CODE = "stream_idle"
"""首 chunk 后流空转（RetryableError 形态）的稳定错误码（跨组契约）。"""

STREAM_IDLE_EXHAUSTED_ERROR_CODE = "stream_idle_exhausted"
"""流空转同请求重试耗尽后的 PermanentError 稳定错误码（跨组契约）。"""

STREAM_IDLE_MAX_RETRIES = 1
"""流空转在单次 ``with_retry`` 内的专属重试上限。

不经通用 ``MAX_RETRIES``=5 次退避表：idle 每次判定已烧满 150s 空转窗，
逐次退避重试只会把一条死流拖成十几分钟的僵尸；1 次重试后仍空转即判死，
交给 run 级 durable 重醒（60/180/600s 退避）接管。64 实测 idle 全为真死，
非退避可救。
"""

_STREAM_IDLE_EXHAUSTED_TEXT_NEEDLES: tuple[str, ...] = (
    "流空转重试已耗尽",
    "idle retries exhausted",
)


def is_stream_idle_exhausted(err: BaseException | str | None) -> bool:
    """判定是否「流空转重试已耗尽」终态（PermanentError 形态，窄谓词）。

    判据：``error_code == STREAM_IDLE_EXHAUSTED_ERROR_CODE``（结构化优先，
    agent.py 重建异常时不丢码即必中），否则文案 needle 兜底（覆盖错误经
    result dict 只剩 ``str(e)`` 的形态）。供 agent 重醒门
    ``_maybe_schedule_upstream_recovery`` 使用 —— **不**参与熔断判定。
    """
    if err is None:
        return False
    if getattr(err, "error_code", None) == STREAM_IDLE_EXHAUSTED_ERROR_CODE:
        return True
    text = str(err)
    return any(n in text for n in _STREAM_IDLE_EXHAUSTED_TEXT_NEEDLES)


def is_stream_idle_error(err: BaseException | str | None) -> bool:
    """判定是否「首 chunk 后流空转」类错误（可重试形态）。

    判据：``error_code == STREAM_IDLE_ERROR_CODE``，或文本含 "stream idle"
    （兜底重建/历史形态）。与 :func:`is_stream_idle_exhausted` **互斥**：
    耗尽态是终态（PermanentError），不再算可重试的空转类。

    用途：① ``RetryHandler.with_retry`` 的专属重试闸；② http_stream
    RetryableError 耗尽出口跳过 report_failure（暗坑③：idle 原是
    PermanentError 从不喂熔断，改判 Retryable 后若照喂，会经 FAIL_THRESHOLD
    「5 次累计」后门把 provider 全项目熔断）。不参与 is_upstream_death。
    """
    if err is None:
        return False
    if is_stream_idle_exhausted(err):
        return False
    if getattr(err, "error_code", None) == STREAM_IDLE_ERROR_CODE:
        return True
    return "stream idle" in str(err).lower()


def classify_http_error(
    status: int | None,
    body: str,
    headers: dict[str, str] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    agent_id: str | None = None,
) -> RetryableError | PermanentError:
    """把一次 HTTP/流错误分类为可重试或永久错误。

    优先级：地域不可用（fast-fail, TEST_DSH_47 #8）> 状态码（429 + 5xx）
    或 body 内容命中可重试模式 → RetryableError；否则 PermanentError。
    用于 ``streamer/http_stream.py`` 的非 200 分支和流中 error chunk ——
    兜住多厂商「状态码正常但 body 包瞬态错误」的情况。

    ``provider`` / ``model`` / ``agent_id`` **只用于记录未识别样本**（见下），
    不参与判定。``agent_id`` 是给**进程级共享的样本缓冲**做归属用的 —— 不给
    会让并发 agent 的样本挂错人（归因错位）。
    """
    snippet = body[:500]
    if status is not None:
        message = f"HTTP {status}: {snippet}"
    else:
        message = snippet
    # E23（2026-09-16）：记**分母** —— 「这一层判过多少次」。没有它，
    # 样本数只是分子，答不出"不认识占多大比例"。
    from hiveweave.llm.unknown_error_samples import note_judgement

    note_judgement("http_error")
    # #13 批 A 接线（2026-09-18）：算一次 provider-neutral 稳定码并挂到
    # 返回的异常上 —— 判定**仍由下方既有链条决定**（地域 fast-fail → 状态码
    # / 文本模式 → UNKNOWN 显式决策），稳定码此刻只作结构化随附
    # （熔断器 / error result 消费），不改任何判定结果。批 B 再收敛判定。
    # ``note_sample=False``：本函数对「真的不认识」有自己的留样（带 extra），
    # 别让两层各记一条污染 E23 分母。
    code = classify_error(status=status, body=body, note_sample=False)
    # 地域类错误确定性不可恢复：即使状态码/文案恰好命中可重试模式
    # （如 body 里夹带 "server error"），也不重试 —— 退避只会把 7s
    # 快死拖成 476s 慢死。
    if is_region_unavailable_error(body):
        return PermanentError(message, status=status, error_code=code.value)
    status_retryable = status is not None and is_retryable_status(status)
    text_retryable = matches_retryable_message(body)
    if status_retryable or text_retryable:
        return RetryableError(
            message, status=status, headers=headers or {}, error_code=code.value
        )
    # ── 走到这里 = 没有任何正面识别信号 ─────────────────────────────
    # fixplan #13 修法 2：文本 fallback **未命中**时必须 fail-loud 留样本，
    # 否则上游换措辞/换语言后分类静默退化成 UNKNOWN、无人知晓。
    # 判据：只有当**连状态码这一层也没有**（或状态码不属已知客户端错误家族）时
    # 才算「真的不认识」—— status 落在 4xx（非 429）是**已识别**的客户端错误，
    # 不是 fallback 失配。
    if not (status is not None and 400 <= status < 500):
        from hiveweave.llm.unknown_error_samples import note_unknown_sample

        note_unknown_sample(
            source="classify_http_error",
            status=status,
            body=body,
            provider=provider,
            model=model,
            agent_id=agent_id,
            extra={
                "status_retryable": status_retryable,
                "text_retryable": text_retryable,
                "error_code": code.value,
            },
        )
    if UNKNOWN_SIGNAL_IS_RETRYABLE:
        return RetryableError(
            message, status=status, headers=headers or {}, error_code=code.value
        )
    return PermanentError(message, status=status, error_code=code.value)


def should_retry_exception(exc: BaseException) -> bool:
    """判断异常是否值得重试（网络错误/超时）。

    httpx.ConnectError / ReadTimeout / PoolTimeout / RemoteProtocolError
    等瞬时网络故障都应重试。参考 Elixir should_retry?/1。

    ssl.SSLError 显式列入：TEST_DSH_29 实测全部重试都源于
    ``[SSL: UNEXPECTED_EOF_WHILE_READING]``。httpx 多数情况会把它包成
    ConnectError/ReadError，但直连/裸 socket 路径可能原样抛出。
    """
    # 延迟导入避免循环依赖
    import ssl

    import httpx

    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout,
                        httpx.WriteTimeout, httpx.PoolTimeout,
                        httpx.RemoteProtocolError, httpx.ReadError)):
        return True
    if isinstance(exc, ssl.SSLError):
        return True
    # asyncio.TimeoutError 是 streamer 的 idle watchdog 抛出的
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return False


# ── Retry-After header 解析 ─────────────────────────────────

def parse_retry_after_ms(headers: dict[str, str] | None) -> int | None:
    """解析 Retry-After header，返回毫秒数。

    支持三种格式（对齐 TS retry-utils.ts）:
      1. retry-after-ms: 5000  — 毫秒（OpenAI 非标准但常用）
      2. retry-after: 5  — 秒
      3. retry-after: Wed, 21 Oct 2025 07:28:00 GMT  — HTTP-date

    Returns:
        延迟毫秒数，无 header 或解析失败返回 None。
    """
    if not headers:
        return None

    # 1. retry-after-ms（毫秒）
    ms_val = headers.get("retry-after-ms")
    if ms_val is not None:
        try:
            return max(0, int(ms_val))
        except (ValueError, TypeError):
            pass

    # 2/3. retry-after（秒或 HTTP-date）
    val = headers.get("retry-after")
    if val is None:
        # header 名大小写不敏感兜底
        val = _get_header_ci(headers, "retry-after")
    if val is None:
        ms_val = _get_header_ci(headers, "retry-after-ms")
        if ms_val is not None:
            try:
                return max(0, int(ms_val))
            except (ValueError, TypeError):
                pass
        return None

    # 尝试作为秒数解析
    try:
        seconds = float(val)
        return max(0, int(seconds * 1000))
    except (ValueError, TypeError):
        pass

    # 尝试作为 HTTP-date 解析
    try:
        dt = parsedate_to_datetime(val)
        if dt is not None:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            delta_ms = int((dt - now).total_seconds() * 1000)
            return max(0, delta_ms)
    except (ValueError, TypeError, OverflowError):
        pass

    return None


def _get_header_ci(headers: dict[str, str], name: str) -> str | None:
    """大小写不敏感地获取 header 值。"""
    name_lower = name.lower()
    for k, v in headers.items():
        if k.lower() == name_lower:
            return v
    return None


def parse_quota_reset(
    headers: dict[str, str] | None,
) -> dict[str, float | bool | None]:
    """Parse 429 quota reset timing from response headers.

    Prefers ``X-RateLimit-Reset`` (unix epoch or seconds-until), then
    ``Retry-After``. Used by agent-level park (not in-request retry cap).

    Returns::
        {
          retry_after_s: float | None,
          reset_at_epoch: float | None,
          is_daily_quota: bool,  # reset > QUOTA_EXHAUST_THRESHOLD_S
        }
    """
    import time as _time

    QUOTA_EXHAUST_THRESHOLD_S = 600.0  # >10 min → treat as quota exhaustion
    out: dict[str, float | bool | None] = {
        "retry_after_s": None,
        "reset_at_epoch": None,
        "is_daily_quota": False,
    }
    if not headers:
        return out

    now = _time.time()
    reset_raw = _get_header_ci(headers, "x-ratelimit-reset") or _get_header_ci(
        headers, "x-ratelimit-reset-requests"
    )
    if reset_raw:
        try:
            val = float(str(reset_raw).strip())
            # Heuristic: values > 1e9 look like unix epoch; else seconds-until
            if val > 1_000_000_000:
                out["reset_at_epoch"] = val
                out["retry_after_s"] = max(0.0, val - now)
            else:
                out["retry_after_s"] = max(0.0, val)
                out["reset_at_epoch"] = now + max(0.0, val)
        except (ValueError, TypeError):
            pass

    if out["retry_after_s"] is None:
        ra_ms = parse_retry_after_ms(headers)
        if ra_ms is not None:
            # parse_retry_after_ms is capped at MAX_DELAY_MS for in-request
            # retry — for quota park, re-parse without the 30s cap.
            ra_uncapped = _parse_retry_after_uncapped(headers)
            secs = (ra_uncapped if ra_uncapped is not None else ra_ms) / 1000.0
            out["retry_after_s"] = secs
            out["reset_at_epoch"] = now + secs

    ra = out["retry_after_s"]
    if isinstance(ra, (int, float)) and ra > QUOTA_EXHAUST_THRESHOLD_S:
        out["is_daily_quota"] = True
    return out


def _parse_retry_after_uncapped(headers: dict[str, str]) -> int | None:
    """Like parse_retry_after_ms but without MAX_DELAY_MS cap (for quota park)."""
    ms_val = _get_header_ci(headers, "retry-after-ms")
    if ms_val is not None:
        try:
            return max(0, int(float(ms_val)))
        except (ValueError, TypeError):
            pass
    val = _get_header_ci(headers, "retry-after")
    if val is None:
        return None
    try:
        return max(0, int(float(val) * 1000))
    except (ValueError, TypeError):
        pass
    try:
        dt = parsedate_to_datetime(val)
        if dt is not None:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            return max(0, int((dt - now).total_seconds() * 1000))
    except (ValueError, TypeError, OverflowError):
        pass
    return None


# ── 退避计算 ────────────────────────────────────────────────

def compute_backoff(attempt: int, retry_after_ms: int | None = None) -> int:
    """计算第 attempt 次重试的退避延迟（毫秒）——波次级骑乘节奏。

    调度规则（T33，2026-09-29 设计更新）：
    - ``retry_after_ms`` ≤60s（``RETRY_AFTER_HONOR_CAP_MS``）：**原样尊重**
      —— 上游显式指令优先于本地节奏（含 0）。
    - ``retry_after_ms`` >60s：忽略（配额级窗口信号，恢复钥匙是 park /
      恢复定时器，不是逐次重试），落回本地节奏。
    - ``attempt < RIDE_STEADY_AFTER_ATTEMPT``：短指数 ramp —— BASE × 2^attempt
      × jitter（jitter ∈ [0.75, 1.25]，即 1/2/4/8s 附近）。
    - ``attempt ≥ RIDE_STEADY_AFTER_ATTEMPT``：稳态骑乘节奏 ——
      ``[RIDE_STEADY_MIN_MS, RIDE_STEADY_MAX_MS]``（10-15s）均匀取值。
      无界骑乘下不再让指数增长失控（对照 DSH ``mode:'always'``）。
    - 本地计算值最终受 :func:`retry_max_delay_ms`（env，默认 45s）封顶
      （单次退避帽）。

    Args:
        attempt: 当前重试序号（0-based，0 = 第一次重试）。
        retry_after_ms: 来自 Retry-After header 的延迟（毫秒），优先级最高
            （仅 ≤60s 时）。

    Returns:
        延迟毫秒数。
    """
    if (
        retry_after_ms is not None
        and retry_after_ms <= RETRY_AFTER_HONOR_CAP_MS
    ):
        return max(0, retry_after_ms)

    if attempt < RIDE_STEADY_AFTER_ATTEMPT:
        # 短指数 ramp: 1s, 2s, 4s, 8s（jitter ±25%）
        base = BASE_DELAY_MS * (2 ** attempt)
        jitter = 0.75 + random.random() * 0.5
        delay = int(base * jitter)
    else:
        # 稳态骑乘节奏：10-15s（波次内匀速探活，不 hammer 也不失速）
        delay = random.randint(RIDE_STEADY_MIN_MS, RIDE_STEADY_MAX_MS)
    return min(delay, retry_max_delay_ms())


# ── 错误分类 ────────────────────────────────────────────────

class RetryableError(Exception):
    """可重试的错误（HTTP 429/503/504/529 或网络错误）。

    携带 HTTP 状态码和响应头，供 RetryHandler 解析 Retry-After。

    ``error_code``（#13 批 A 接线，2026-09-18）：provider-neutral 稳定码
    （``llm/error_codes.ErrorCode`` 的 ``.value`` 字符串），由
    ``classify_http_error`` 在分类时挂上 —— 下游（熔断器 / error result /
    agent 层）按码消费，不再各自匹配状态码或文案。
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.headers = headers or {}
        self.error_code = error_code


class PermanentError(Exception):
    """不可重试的错误（401 认证失败、400 请求错误等）。"""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code


# ── RetryHandler ────────────────────────────────────────────

class RetryHandler:
    """异步重试处理器（波次级骑乘）。

    用法::

        handler = RetryHandler()                # 有界（bounded_retry_budget）
        result = await handler.with_retry(lambda: do_request())

    回调应返回结果或抛出 RetryableError / PermanentError。
    网络异常（httpx.ConnectError 等）自动判定为可重试。

    ``max_retries=None`` = **无界骑乘**（主链路默认，:func:`main_ride_budget`）
    —— 重试不设次数上限，终止靠：成功 / 取消信号 / ``deadline`` 预算到点 /
    ``failover_gate`` 中止（:class:`RetryAborted`）。

    每次「实际重试」会触发 on_retry 回调（用于日志/遥测）。
    有界路径全部重试耗尽后抛出最后一次的 RetryableError。
    """

    def __init__(
        self,
        max_retries: int | None = MAX_RETRIES,
        on_retry: Callable[[int, int, BaseException], Awaitable[None] | None] | None = None,
    ) -> None:
        self.max_retries = max_retries
        self.on_retry = on_retry

    @staticmethod
    async def _consult_failover_gate(
        gate: Callable[[], Awaitable[bool]] | None,
        exc: BaseException,
    ) -> None:
        """T34：每次上游级失败咨询一次故障转移闸。

        闸返回 True ⇒ 抛 :class:`RetryAborted`（携带触发错误），调用方
        （http_stream）捕获后切换模型并重启链。闸自身故障一律 fail-open
        （继续骑乘，不因观测/切换层故障放大主流程）。
        """
        if gate is None:
            return
        try:
            abort = await gate()
        except Exception as ge:  # noqa: BLE001 — 闸故障不得放大主流程
            log.warning("failover_gate_failed", error=str(ge)[:200])
            return
        if abort:
            raise RetryAborted(
                "failover gate aborted retry chain", last_error=exc
            )

    async def with_retry(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        persist: (
            Callable[[int, int, BaseException, bool], Awaitable[None]] | None
        ) = None,
        deadline: float | None = None,
        failover_gate: Callable[[], Awaitable[bool]] | None = None,
    ) -> T:
        """执行带重试的异步函数（波次级骑乘）。

        - 首次调用 + 重试；``max_retries=None`` = 无界骑乘，否则最多
          ``max_retries`` 次。
        - 节流节奏见 :func:`compute_backoff`（短 ramp + 10-15s 稳态 +
          Retry-After ≤60s 尊重 + 45s 单次帽）。
        - PermanentError 立即抛出，不重试。
        - 非可重试异常也不重试（直接抛出）。
        - ``deadline``（time.monotonic 刻度，可选）：每次调度重试前检查
          「now + delay 是否越过截止」，越过 ⇒ 停止骑乘（落 exhausted 账）
          并抛最后一次错误 —— 骑乘不越 turn 预算（auto 模式无墙钟时由
          http_stream 侧另武装 ``LLM_RIDE_WALL_CAP_S`` 兜底墙）。
        - ``failover_gate``（T34，可选异步闸）：每次上游级失败咨询一次
          （在调度重试之前）；返回 True ⇒ 抛 :class:`RetryAborted`。
        - 流空转（``is_stream_idle_error``，TEST_DSH_64 #3）：**专属**预算
          ``STREAM_IDLE_MAX_RETRIES``（1 次），不经通用 max_retries 退避表、
          不 sleep（idle 判定本身已等满 150s）；重试仍空转 ⇒ 升级为
          ``PermanentError(error_code=stream_idle_exhausted)``，交 run 级
          durable 重醒接管。attempt 计数器手动推进（while 结构）：idle 重试
          **不占**通用 attempt 名额，两条预算完全独立。
        - ``persist``：可选持久化回调 ``(attempt, delay_ms, exc, exhausted)``
          —— 每次决定重试（exhausted=False）与重试耗尽（exhausted=True）时
          调用，供调用方把重试事件落库（**先落账后等待**——持久化先于
          退避睡眠，进程重启不丢；DSH 先例）。参考 DSH 把 retry 写入持久
          事件流，重启后仍可查/可审计。
        """
        last_exc: BaseException | None = None
        idle_retries = 0
        attempt = 0  # 通用重试序号（0-based；idle 专属重试不推进它）
        while True:
            try:
                return await fn()
            except PermanentError:
                # 不可重试 — 直接抛出
                raise
            except RetryableError as e:
                # E7: 容量错误（配额耗尽）不进逐次重试——退避解决不了窗口级
                # 问题，白撞只会更快烧完上下文/预算。立即上抛，交给 agent 层
                # 项目级暂停/排队（配额窗口恢复后再批量唤醒）。
                if is_capacity_error(str(e)):
                    log.warning(
                        "capacity_error_no_retry",
                        status=e.status,
                        error=str(e)[:200],
                    )
                    raise
                # T34：上游级失败先问故障转移闸（≥阈值且有候选 → 中止本链，
                # 调用方换模型重启；无候选 → 继续骑乘）。
                await self._consult_failover_gate(failover_gate, e)
                # 流空转（TEST_DSH_64 #3）：专属 1 次重试，不经通用退避表。
                # 每次空转判定已烧满 idle 窗口（150s），再指数退避纯属加时；
                # 耗尽即升级 PermanentError（重醒门认 stream_idle_exhausted）。
                if is_stream_idle_error(e):
                    if idle_retries >= STREAM_IDLE_MAX_RETRIES:
                        log.warning(
                            "stream_idle_retry_exhausted",
                            attempt=attempt,
                            idle_retries=idle_retries,
                            error=str(e),
                        )
                        if persist is not None:
                            await self._safe_persist(
                                persist, attempt + 1, 0, e, True
                            )
                        raise PermanentError(
                            f"{e} — 流空转重试已耗尽"
                            f"（{STREAM_IDLE_MAX_RETRIES} retry）",
                            status=getattr(e, "status", None),
                            error_code=STREAM_IDLE_EXHAUSTED_ERROR_CODE,
                        ) from e
                    idle_retries += 1
                    last_exc = e
                    log.info(
                        "stream_idle_retry",
                        attempt=attempt + 1,
                        error=str(e),
                    )
                    if persist is not None:
                        await self._safe_persist(
                            persist, attempt + 1, 0, e, False
                        )
                    await self._fire_retry(attempt + 1, 0, e)
                    # 立即重试：上游已静默满 idle 窗，退避不改变结局；
                    # attempt 不推进 —— 通用预算不被 idle 侵占
                    continue
                last_exc = e
                if self.max_retries is not None and attempt >= self.max_retries:
                    log.warning(
                        "retry_exhausted",
                        attempt=attempt,
                        status=e.status,
                        error=str(e),
                    )
                    if persist is not None:
                        await self._safe_persist(persist, attempt + 1, 0, e, True)
                    raise
                delay = compute_backoff(attempt, parse_retry_after_ms(e.headers))
                # 骑乘不越预算：本次退避睡眠将越过 deadline ⇒ 停止（exhausted
                # 落账）并抛最后一次错误，交既有 error→durable recovery 路。
                if deadline is not None and (
                    _time.monotonic() + delay / 1000.0 > deadline
                ):
                    log.warning(
                        "ride_deadline_exhausted",
                        attempt=attempt,
                        delay_ms=delay,
                        status=e.status,
                        error=str(e)[:200],
                    )
                    if persist is not None:
                        await self._safe_persist(persist, attempt + 1, 0, e, True)
                    raise
                log.info(
                    "retry_scheduled",
                    attempt=attempt + 1,
                    delay_ms=delay,
                    status=e.status,
                    reason=str(e),
                )
                if persist is not None:
                    await self._safe_persist(
                        persist, attempt + 1, delay, e, False
                    )
                await self._fire_retry(attempt + 1, delay, e)
                attempt += 1
                await asyncio.sleep(delay / 1000.0)
            except Exception as e:
                # 其他异常 — 检查是否为可重试的网络错误
                if should_retry_exception(e):
                    last_exc = e
                    await self._consult_failover_gate(failover_gate, e)
                    if self.max_retries is not None and attempt >= self.max_retries:
                        log.warning("retry_exhausted_network", attempt=attempt, error=str(e))
                        if persist is not None:
                            await self._safe_persist(
                                persist, attempt + 1, 0, e, True
                            )
                        raise
                    delay = compute_backoff(attempt)
                    if deadline is not None and (
                        _time.monotonic() + delay / 1000.0 > deadline
                    ):
                        log.warning(
                            "ride_deadline_exhausted_network",
                            attempt=attempt,
                            delay_ms=delay,
                            error=str(e)[:200],
                        )
                        if persist is not None:
                            await self._safe_persist(
                                persist, attempt + 1, 0, e, True
                            )
                        raise
                    log.info(
                        "retry_scheduled_network",
                        attempt=attempt + 1,
                        delay_ms=delay,
                        reason=type(e).__name__,
                    )
                    if persist is not None:
                        await self._safe_persist(
                            persist, attempt + 1, delay, e, False
                        )
                    await self._fire_retry(attempt + 1, delay, e)
                    attempt += 1
                    await asyncio.sleep(delay / 1000.0)
                else:
                    raise

        # 理论上不可达（循环内必返回或抛出），保险起见
        assert last_exc is not None
        raise last_exc

    @staticmethod
    async def _safe_persist(
        persist: Callable[[int, int, BaseException, bool], Awaitable[None]],
        attempt: int,
        delay_ms: int,
        exc: BaseException,
        exhausted: bool,
    ) -> None:
        """持久化回调 best-effort：持久化失败不得中断重试主流程。"""
        try:
            await persist(attempt, delay_ms, exc, exhausted)
        except Exception as _p:
            log.warning(
                "retry_persist_failed",
                attempt=attempt,
                error=str(_p)[:200],
            )

    async def _fire_retry(
        self, attempt: int, delay_ms: int, exc: BaseException
    ) -> None:
        """触发 on_retry 回调（支持同步/异步）。"""
        if self.on_retry is None:
            return
        result = self.on_retry(attempt, delay_ms, exc)
        if asyncio.iscoroutine(result):
            await result
