"""Provider-neutral 稳定错误码表（批次 D 结构位 #1）。

DSH 教条：一个 ``code: str`` 字段让重试/熔断/剥图/审计四个消费方全部
解耦于 provider 文案。HiveWeave 此前在 retry.py（状态码+正则）、
circuit_breaker.py（熔断）、provider.py（剥图负缓存）各自匹配状态码
与文案——本模块统一收口。

用法：provider 层抛出/返回错误时调用 ``classify_error()`` 得到稳定码；
消费方（retry/circuit/UI）按码路由，不再匹配状态码或文案。
"""

from __future__ import annotations

import re
from enum import Enum


class ErrorCode(str, Enum):
    """Provider-neutral 稳定错误码（对齐 DSH error.ts 码集）。"""

    AUTH = "AUTH"
    RATE_LIMIT = "RATE_LIMIT"
    SERVER = "SERVER"
    TIMEOUT = "TIMEOUT"
    TRANSPORT = "TRANSPORT"
    QUOTA = "QUOTA"
    CONTEXT_WINDOW = "CONTEXT_WINDOW_EXCEEDED"
    INVALID_REQUEST = "INVALID_REQUEST"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"
    UNKNOWN = "UNKNOWN"


#: 429 优先级低于 QUOTA——QUOTA 文案判据（``_quota_text_hit`` → retry 的
#: 容量词表，含裸 ``quota``）命中时即便状态码 429 也归 QUOTA。
#: ⚠ #13 批 B（2026-09-18）：QUOTA 文案判据已**收编**到
#: ``llm/retry.py::is_capacity_error``（容量 needles 唯一事实源）——
#: 本模块不再持第二份 quota 词表（F9-C）；裸 ``quota`` 等成员已并入该表，
#: 跨词通配形态的收窄在该表注释里登记在案。
_CONTEXT_RE = re.compile(
    r"context.*(length|window|overflow)|maximum.*tokens|too many.*tokens|"
    r"prompt.*too.*long|input.*exceeds",
    re.IGNORECASE,
)


def _quota_text_hit(body: str) -> bool:
    """QUOTA 文案层：容量词表（唯一事实源）∪ **裸 ``quota``**。

    裸 quota 归本函数而不归 retry 的容量表 —— E7 容量链（不逐次重试）
    的语义由 test_e7_capacity_slowdown 钉住："resource exhausted: quota
    for current provider" 是瞬态 429 族，不得进容量表（推送前全量实锤）。
    """
    from hiveweave.llm.retry import is_capacity_error

    return "quota" in body.lower() or is_capacity_error(body)


def classify_error(
    status: int | None = None,
    body: str = "",
    *,
    provider: str | None = None,
    model: str | None = None,
    agent_id: str | None = None,
    note_sample: bool = True,
) -> ErrorCode:
    """按 HTTP 状态码 + 响应体文案分类为稳定错误码。

    分类优先级：QUOTA 文案 > RATE_LIMIT(429) > AUTH(401/403) >
    CONTEXT(400+文案) > SERVER(5xx) > INVALID_REQUEST(4xx) > UNKNOWN。

    **判据来源分层（fixplan #13）**：状态码 = **第一层**（结构化、与措辞
    和语言无关）；`_QUOTA_RE` / `_CONTEXT_RE` 文案 = **第二层 fallback**。
    fallback 未命中而落到 ``UNKNOWN`` 时**必须留样本**（见下），否则上游换
    措辞/换语言导致的降级是静默的。

    ``provider`` / ``model`` 只用于记录样本，不参与判定。
    ``note_sample=False`` 供**已有自己留样逻辑**的调用方复用本分类
    （如 ``retry.classify_http_error`` 会落带 ``extra`` 的样本），避免
    同一次失败被两层各记一条样本、污染 E23 的分母口径。
    """
    if status == 402 or (body and _quota_text_hit(body)):
        return ErrorCode.QUOTA
    if status == 429:
        return ErrorCode.RATE_LIMIT
    if status in (401, 403):
        return ErrorCode.AUTH
    if status == 413:
        return ErrorCode.INVALID_REQUEST
    if status is not None and 400 <= status < 500:
        if body and _CONTEXT_RE.search(body):
            return ErrorCode.CONTEXT_WINDOW
        return ErrorCode.INVALID_REQUEST
    if status is not None and status >= 500:
        return ErrorCode.SERVER
    if status is None and body:
        # 流内错误（HTTP 200 但 body 含 error）——只能按文案分类。
        # ⇒ 这里是 fallback 的**唯一位置**：两条判据都不中就是真的不认识。
        if _CONTEXT_RE.search(body):
            return ErrorCode.CONTEXT_WINDOW
        if _quota_text_hit(body):
            return ErrorCode.QUOTA
    # 无任何正面识别信号 ⇒ fail-loud 留样本（fixplan #13 修法 2）。
    # ⚠ 只有 `status is None` 落到这里才是「fallback 失配」；status 落在
    # 其它值（如 1xx/3xx 异常态）是另一类问题，一并记录便于发现新错误族。
    if note_sample:
        from hiveweave.llm.unknown_error_samples import note_unknown_sample

        note_unknown_sample(
            source="classify_error",
            status=status,
            body=body,
            provider=provider,
            model=model,
            agent_id=agent_id,
        )
    return ErrorCode.UNKNOWN


def is_retryable_code(code: ErrorCode) -> bool:
    """稳定码→可重试判定（取代散落的状态码+正则匹配）。

    ⚠ ``UNKNOWN`` 的归属是**显式决策**，不是"不在列表里所以不可重试"的省略
    —— 见 :data:`hiveweave.llm.retry.UNKNOWN_SIGNAL_IS_RETRYABLE` 的注释
    （其中写明与 ``llm/streamer/http_stream.py`` 传输层分支默认相反，以及为什么）。
    """
    if code is ErrorCode.UNKNOWN:
        from hiveweave.llm.retry import UNKNOWN_SIGNAL_IS_RETRYABLE

        return UNKNOWN_SIGNAL_IS_RETRYABLE
    return code in (
        ErrorCode.RATE_LIMIT,
        ErrorCode.SERVER,
        ErrorCode.TIMEOUT,
        ErrorCode.TRANSPORT,
        ErrorCode.EMPTY_RESPONSE,
    )
