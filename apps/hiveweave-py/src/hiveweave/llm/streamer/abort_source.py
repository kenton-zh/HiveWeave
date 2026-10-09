"""N3 批28-1 施工2：per-request abort 信号源（模块级注册表）。

上游参照（pi，最新 HEAD 已核验）：

- ``packages/ai/src/api/openai-completions.ts:371/688/718`` —— 全 API 层
  透传 ``options.signal``，abort 时 ``stopReason="aborted"`` **而非 error**
  （下游可区分「人为掐」与「真失败」）。
- DSH timeout-policy 思想 —— 超时时**替换结果而非杀进程**（结构化错误码
  ``TOOL_TIMEOUT``），外层重试可路由。

本仓适配（形态差异）：

- httpx 层是**同步线程 + queue 事件泵**（Windows 上 asyncio
  CancelledError 掐不断 httpx 的 socket read，见 http_stream 模块注释），
  所以 pi 的「signal 传进请求库」无法照抄 —— 落地为：本注册表 +
  ``_do_streaming_request`` 请求循环周期检查（每收到 chunk + 每
  ``ABORT_POLL_S`` 秒心动 poller），置位 ⇒ 主动 ``client.close()`` 断流
  （线程内 iter_bytes 立即出错退出）+ 归类 ``aborted``。
- 注册 key = agent_id（每 agent 同时至多一条活 LLM 流 —— tool loop 串行）。
  条目携带 run_id 供陈旧判废：老 run 的 abort 请求不得掐死新 run 的流。
- ``abort_stream(agent_id, run_id, reason)`` 是平台侧唯一调用入口（批28-2
  的失控流检测器）。用户主动停止仍走既有 task.cancel() → CancelledError
  路径（同为「人为掐」家族，run 层已按 interrupted 收口）—— 不双轨。

线程安全：``abort_stream`` 预期在主事件循环内调用（本平台全部 service 均
async on main loop）；若从其它线程调用，Event 置位经
``loop.call_soon_threadsafe`` 中转（失败只丢快速唤醒，``.reason`` 字段
仍已被原子写入 —— poller 按 ``event.is_set()`` 检查，同循环内必见）。
"""
from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger(__name__)


class AbortSource:
    """单个在途 LLM 流的 abort 信号。

    ``event``：同循环内置位立即生效（请求循环的 per-chunk 检查直读）；
    ``reason``：掐流原因（abort_stream 记入，随 aborted 结果带出）。
    """

    __slots__ = ("agent_id", "run_id", "round_num", "event", "reason", "loop")

    def __init__(
        self,
        agent_id: str,
        run_id: str | None = None,
        round_num: int = 0,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.run_id = run_id
        self.round_num = round_num
        self.event = asyncio.Event()
        self.reason: str | None = None
        self.loop = loop


#: agent_id → 当前在途请求的 abort 源（每 agent 至多一条活流）。
_ACTIVE_SOURCES: dict[str, AbortSource] = {}


def register_abort_source(
    agent_id: str,
    *,
    run_id: str | None = None,
    round_num: int = 0,
) -> AbortSource:
    """请求开始时登记本 agent 的 abort 源（顶替遗留条目 —— 正常路径
    unregister 已清理，顶替只是防御请求异常退出未注销的僵尸条目）。"""
    src = AbortSource(
        agent_id,
        run_id=run_id,
        round_num=round_num,
        loop=asyncio.get_running_loop(),
    )
    _ACTIVE_SOURCES[agent_id] = src
    return src


def unregister_abort_source(agent_id: str, source: AbortSource) -> bool:
    """请求结束注销；只注销仍是自己（已被新请求顶替时不误删新条目）。"""
    if _ACTIVE_SOURCES.get(agent_id) is source:
        _ACTIVE_SOURCES.pop(agent_id, None)
        return True
    return False


def abort_stream(
    agent_id: str,
    run_id: str | None = None,
    reason: str = "",
) -> bool:
    """平台侧掐流入口：置位该 agent 当前在途 LLM 流的 abort 源。

    Args:
        agent_id: 目标 agent。
        run_id: 期望掐的 run。非空且与在途注册的 run_id 不一致 ⇒ 陈旧
            abort（老 run 的迟到指令），拒发并记日志 —— 不得掐死新 run。
            在途注册 run_id 为 None（调用方未接线 run_id 的旁路流）时
            放行（best-effort，按 agent 掐）。
        reason: 掐流原因（记日志 + 随 aborted 结果带出，观测用）。

    Returns:
        是否真正置位（无在途流 / 陈旧 run ⇒ False）。
    """
    src = _ACTIVE_SOURCES.get(agent_id)
    if src is None:
        return False
    if run_id and src.run_id and src.run_id != run_id:
        log.info(
            "llm_stream_abort_stale_run",
            agent_id=agent_id,
            wanted_run=run_id,
            active_run=src.run_id,
            note="stale abort for an old run ignored; active stream untouched",
        )
        return False
    if src.reason is None:
        src.reason = reason or "aborted"
    log.warning(
        "llm_stream_abort_requested",
        agent_id=agent_id,
        run_id=src.run_id,
        round=src.round_num,
        reason=src.reason,
    )
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is src.loop:
        src.event.set()
    else:
        # 跨线程调用：Event.set() 非线程安全，经 loop 中转。失败不静默
        # （空 catch 棘轮纪律）：loop 已关 ⇒ 本流的事件循环已终止，poller
        # 不可达、无需唤醒，只留 debug 痕迹。
        try:
            assert src.loop is not None
            src.loop.call_soon_threadsafe(src.event.set)
        except RuntimeError as e:
            log.debug(
                "llm_stream_abort_wakeup_skipped",
                agent_id=agent_id,
                error=str(e),
            )
    return True


def reset_abort_registry() -> None:
    """清空注册表（测试隔离用）。"""
    _ACTIVE_SOURCES.clear()


def active_abort_sources() -> dict[str, AbortSource]:
    """只读快照（观测/测试断言用；调用方不得改内部状态）。"""
    return dict(_ACTIVE_SOURCES)
