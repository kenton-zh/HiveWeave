"""未识别上游错误的 **fail-loud 样本**（fixplan-16items §三 #13 修法 2）。

## 为什么需要它

上游错误分类的文本表（``llm/retry.py`` 的 ``_RETRYABLE_MESSAGE_PATTERNS`` /
``_CAPACITY_NEEDLES`` / ``_WINDOW_NEEDLES`` / ``_REGION_FASTFAIL_PATTERNS``、
``llm/error_codes.py`` 的两条 ``_RE``）是**第二层 fallback**：状态码优先，
文本兜底。上游一旦换措辞或换语言（整段法文/西文），fallback 命中不了
⇒ 分类**静默**退化成 UNKNOWN。

**「静默」才是病灶**：分类退化了没人知道，直到某天熔断/暂停全失效
（``agents/helpers/rate_limit.py`` docstring 自陈曾白撞 75 个 error run）。

⇒ 只要 UNKNOWN 留下**带原始文案**的样本，就能：
  ① 观测「不认识」的**真实比例**（这是**平台可控**的指标 —— 而「重试率」取决于
     上游质量，平台无法单方面降低，把它当 KPI 是归因错位）；
  ② 拿到真实文案后**在分类层重建判据**，而不是靠人猜上游会怎么写。

## 边界（别把它当安全网）

- 本模块**不参与分类判定** —— 改它不改变任何行为，它只负责「记录」。
- 它只覆盖**经过了分类函数**的路径。绕过 ``classify_http_error`` /
  ``classify_error`` 直接 raise 的错误**不会**留下样本（已知残余，见 §六 残留清单）。
- 样本是**有界环形缓冲**（``_MAX_SAMPLES``）：爆量时挤掉最旧的，不会无界增长。
- 脱敏复用 ``util/redact.py``（与 ``services/offturn.py`` 同一套正则）
  ⇒ **不新增密钥匹配面**。

## 消费方式（为什么分同步/异步两层）

分类函数（``classify_http_error`` / ``classify_error``）是**同步**的，
且**拿不到 agent_id**（agent_id 是调用方作用域的信息）。

⇒ 分两层，**不为记录而给分类函数透传 agent_id**：

- :func:`note_unknown_sample` —— 同步，就地调用 → 进缓冲 + ``log.warning``
  （日志即 fail-loud，CI/审计可捞；对齐本仓「fail loud 但不 fail hard」）。
- :func:`flush_unknown_samples` —— 异步，由**拿得到 agent_id 的调用方**
  （``llm/streamer/http_stream.py`` 的错误收口）调用，把缓冲落
  ``agent_events``（``event_type=llm_unknown_error_sample``）。
"""

from __future__ import annotations

from collections import deque
from typing import Any

import structlog

from hiveweave.util.redact import redact_secrets

log = structlog.get_logger(__name__)

#: 事件类型（与既有 ``llm_retry`` / ``llm_retry_exhausted`` 命名一致）。
EVENT_TYPE = "llm_unknown_error_sample"

#: 环形缓冲上限。取值依据：单轮 turn 的未知错误最多几次，200 足够覆盖
#: 「一个项目连续几轮」的排查窗口，且不会把内存/DB 写放大。
_MAX_SAMPLES = 200

#: 单条样本里保留的原始文案长度（够了：上游错误体前 300 字内必有特征词）。
_BODY_PREVIEW = 300

_samples: deque[dict[str, Any]] = deque(maxlen=_MAX_SAMPLES)
_total = 0


def note_unknown_sample(
    *,
    source: str,
    status: int | None = None,
    body: str = "",
    provider: str | None = None,
    model: str | None = None,
    agent_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """记录一次「无法分类」的上游错误，返回落库用 payload。

    ``source`` 指**哪个分类入口**判不出来（``classify_http_error`` /
    ``classify_error`` / …）—— 排查时第一件事就是区分「两处默认相反」的
    那个分界（见 ``llm/retry.py`` 末尾注释）。

    ``agent_id``：**能给就给**。缓冲是**进程级共享**的，多个 agent 并发流式
    时若不带 agent_id，flush 会把别人的样本挂到你的事件里 —— 那正是本项目
    最反感的「归因错位」。拿不到（如被 ``classify_error`` 这类无 agent 作用域
    的入口调用）就留 None，样本仍会进缓冲与日志，只是不做 DB 归属。

    ⚠ 本函数**不抛 `Exception`**（整体 try/except 兜住）：它挂在错误分类路径上，
    抛异常会把一次「分类失败」升级成「整条流炸掉」。
    ⚠ 边界：**`BaseException`（如 `asyncio.CancelledError`）仍会抛穿** —— 这是
    刻意的，取消语义不该被吞。此处没有 await 点，故当前不可触发；若将来在
    try 内加入 await，必须重新评估这条边界（消费方 `http_stream.py` 的
    `except Exception` 同样不覆盖 CancelledError）。

    ``extra`` 里的非标量值会被 ``repr`` 化：`event_audit` 落库时要 JSON 序列化，
    不可序列化的 payload 会被整条替换成 `{"error": "payload not serializable"}`，
    把 ``body_preview`` 一起丢掉（已在该处补 warning 日志）。
    """
    global _total
    try:
        payload: dict[str, Any] = {
            "source": source,
            "status": status,
            "body_preview": redact_secrets(str(body or ""))[:_BODY_PREVIEW],
            "provider": provider,
            "model": model,
            "agent_id": agent_id,
        }
        if extra:
            payload["extra"] = {
                k: v if isinstance(v, (str, int, float, bool, type(None))) else repr(v)
                for k, v in extra.items()
            }
        _total += 1
        _samples.append(payload)
        log.warning(
            "llm_unknown_error_sample",
            **{k: v for k, v in payload.items() if k != "extra"},
            action=(
                "分类层不认识这条上游文案 —— 它是文本 fallback 未覆盖的形态。"
                "先看样本分布再决定是否重建判据，**不要直接往词表里加词**"
            ),
        )
        return payload
    except Exception as e:  # noqa: BLE001 — 见 docstring：绝不抛
        log.debug("unknown_sample_note_failed", error=str(e)[:200])
        return {}


def recent_unknown_samples(agent_id: str | None = None) -> list[dict[str, Any]]:
    """当前缓冲里的样本（最旧→最新）。

    ``agent_id`` 非 None 时**只返回该 agent 的** —— 见 ``note_unknown_sample``
    里关于「进程级共享缓冲会归因错位」的说明。
    """
    if agent_id is None:
        return list(_samples)
    return [s for s in _samples if s.get("agent_id") == agent_id]


def unknown_sample_total() -> int:
    """累计记录次数（含已被环形缓冲挤掉的）。**这是「不认识的比例」的分子。**"""
    return _total


# ── E23（2026-09-16）：**分母**与比例 ───────────────────────────────
#
# 原来只有分子（`_total`）：能看见"有多少条不认识"，但答不出
# "**判据的覆盖率是多少**" ⇒ 也就没人能判断"要不要重建判据、还是本来就该这样"。
# 分母在**做判定的那一步**记（每个判定动作调一次 `note_judgement`），
# 于是"分母漏记"与"真的没判定"在数据上不同形（本仓对"看似有指标"过敏）。
_JUDGED_BY_FAMILY: dict[str, int] = {}


def note_judgement(family: str) -> None:
    """记一次**判定动作**（分母）。`family` 是闭合的判定入口名。

    ⚠ 只记次数、不记内容（内容属样本侧）；本函数**永不抛**（挂在主路径上）。
    """
    try:
        key = str(family or "unknown")
        _JUDGED_BY_FAMILY[key] = _JUDGED_BY_FAMILY.get(key, 0) + 1
    except Exception:  # noqa: BLE001 — 计数绝不打断主路径
        pass


def unknown_sample_stats() -> dict[str, Any]:
    """样本总量 + 分族判定次数 + **比例**（E23）。

    ⚠ **键名刻意不叫 `ratio`**（审计 D5）：分子与分母**不同源** ——
    分子 `total` 来自 4 个 `note_unknown_sample` 调用点，分母来自 2 个
    `note_judgement` 调用点；而 `fact_position` 族的样本走的是**另一个**
    计数器（`note_unclassified_sample` → `agent_events`），不进 `total`。
    混成一个 `ratio` 会被读成"覆盖率"（58 的量级 ≈ 1.4% ⇒ 看着很好），
    而这正是 E23 要防的误读 ⇒ 按族看请用 `judgedByFamily` + `bySource`。

    `judged == 0` 时返回 ``None`` —— **不返回 0.0**：
    0 会被读成"覆盖率完美"，而真相是"还没有数据"（本仓对"用默认值冒充结论"
    的既有教训）。
    """
    judged = sum(_JUDGED_BY_FAMILY.values())
    by_source: dict[str, int] = {}
    for s in _samples:
        src = str(s.get("source") or "unknown")
        by_source[src] = by_source.get(src, 0) + 1
    return {
        "total": _total,
        "buffered": len(_samples),
        "bySource": by_source,
        "judgedByFamily": dict(sorted(_JUDGED_BY_FAMILY.items())),
        "judged": judged,
        "unknownPerJudgedCall": None if not judged else round(_total / judged, 4),
    }


def clear_unknown_samples() -> None:
    """清空缓冲与计数（测试用；生产不调用）。"""
    global _total
    _samples.clear()
    _total = 0
    _JUDGED_BY_FAMILY.clear()


async def flush_unknown_samples(agent_id: str) -> int:
    """把**该 agent 的**样本**提交**给 ``agent_events``，并从缓冲移除。

    只处理 ``payload["agent_id"] == agent_id`` 的样本：缓冲是进程级共享的，
    不做归属过滤就会把并发其他 agent 的样本写进本 agent 的事件流。
    ``agent_id`` 为空的样本**不在此处提交**（无法确定该挂给谁）—— 它们仍已
    由 ``note_unknown_sample`` 打进日志，不会全丢。

    返回值 = **已提交条数**（``submitted``），**不是**「已落库条数」：
    ``event_audit.log`` 是 fire-and-forget（内部 ``asyncio.create_task``，
    ``services/event_audit.py``），``await`` 它只保证任务被创建，不保证写完。

    ⇒ **已知降级（不是缺陷，但要写清楚）**：若后台写失败（agent_events 缺失 /
    DB 锁 / 进程在任务跑完前退出），该样本不会落库，且因为已从缓冲移除，
    **不会重试**。缓解：``note_unknown_sample`` 的 ``log.warning`` 已经带了
    原文，所以最坏情况是「DB 里少一条、日志里仍有」—— 是降级不是全丢。
    不改成「写完才移除」的理由：那需要回执机制，而 fire-and-forget 下
    每条都会被重复提交（事件表被刷爆），代价高于收益。

    调用方：``llm/streamer/http_stream.py``（两处错误收口 —— 那里有 agent_id）。
    ⚠ 本函数**不抛 ``Exception``**：它挂在错误收口路径上（原始异常正在抛出中），
    这里再抛会**覆盖掉原始错误**，让排查彻底失去线索。CancelledError 例外，
    与 ``note_unknown_sample`` 同一边界。
    """
    try:
        mine = [s for s in _samples if s.get("agent_id") == agent_id]
        if not mine:
            return 0
        from hiveweave.services.event_audit import event_audit

        submitted = 0
        for payload in mine:
            try:
                await event_audit.log(
                    agent_id=agent_id,
                    project_id="",
                    event_type=EVENT_TYPE,
                    payload=payload,
                )
                submitted += 1
                # 提交即移除：避免下一次错误收口把同一批重复提交。
                # `deque.remove` 按 `==` 比对；两条样本若相等则 agent_id 必相等，
                # 故只可能删到 `mine` 里的自己，不会误删别的 agent 的样本。
                _samples.remove(payload)
            except Exception as e:  # noqa: BLE001 — 单条失败不影响其余
                log.warning("unknown_sample_event_submit_failed", error=str(e)[:200])
        return submitted
    except Exception as e:  # noqa: BLE001 — 见 docstring：不抛 Exception
        log.warning("unknown_sample_flush_failed", error=str(e)[:200])
        return 0
