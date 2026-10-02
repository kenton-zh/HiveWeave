"""多 provider 故障转移（T34）—— 波次级无界骑乘的互补逃生舱。

分工（2026-09-29 设计更新钦定）：**无界重试（T33）是「骑过波次」，
故障转移（T34）是「换掉持续故障的上游」**——同一 agent 同一 run 内
连续 ≥3 次上游级故障（5xx/零分块超时/连接失败，复用 RetryableError 分类）
⇒ 从模型池（meta DB ``llm_models``）选一个**非当前模型**的可用档接管。

实测病灶（80 三案 + 84 两案）：上游 longcat 免费档 500 波次，单波
1.5-3 分钟、波间隔 10-20 分钟。骑乘吸收单波；**持续**波次（波不退或
逐波连败）由本模块换引擎。

关键语义：

- **瞬态成功重置计数**（T33.2）：同 run 内某次调用成功（哪怕后续又失败）
  ⇒ 连续失败计数归零 —— 80 实测「attempt-1 吸收单发 500 后又跑了 3 步才
  死」，第二次波次必须按新梯算，而非累计。
- **转移是本回合临时生效**：只覆盖该 run 的模型解析结果（Streamer 实例态），
  **不写 DB**；run 结束 / 下回合由 agent 重新 ``_get_model_config`` 自然
  恢复主模型。
- **循环防护**：转移链最多走完池内可用模型数，不回头（``tried_ids`` 按
  DB 主键记账，含主模型与已切换模型）。
- **禁用模型跳过**：``is_active=0`` 由 ``list_active_full`` 天然过滤；
  ``SKIP_MODELS`` 名单（默认含 ``open_DS_V``——09-29 用户钦定禁用该池）
  + env ``HIVEWEAVE_LLM_FAILOVER_SKIP_MODELS``（逗号分隔，**覆盖**默认）。
- **方言安全约束**：候选必须与当前模型同 ``provider_type``（线协议方言）
  —— 跨方言切换会让 usage 归一（``normalize_usage`` 按 provider_type）与
  思考外显（``supports_thinking``）按主模型口径错位记账；「跨 provider」
  语义落在本仓的 **网关（base_url）维度**：优先同网关异模型，次同方言异
  网关。
- **诚实原则**：转移时打结构化日志 ``llm_failover_switched``（from/to
  model）+ 给 agent 发 platform_notice（「主模型连续故障，本回合已切换
  <model>」——模型该知道自己换了引擎）。

计数口径：以 **with_retry 内每次上游级失败（RetryableError 分类）** 为
一次——无界骑乘下 3 连败 ≈ 30-60s 的持续故障，正是「波次没过去」的
判据；单发 500 被 next-retry 吸收后成功即重置，不会误触发。
"""

from __future__ import annotations

import os as _os
import time as _time

import structlog

log = structlog.get_logger(__name__)

FAILOVER_THRESHOLD_DEFAULT = 3
"""连续上游级故障触发转移的阈值（次）。"""

SKIP_MODELS: frozenset[str] = frozenset({"open_ds_v"})
"""默认禁用名单（**小写**存储；匹配对 model_id / name 大小写不敏感）。

``open_ds_v``（即 open_DS_V）：09-29 用户钦定不用该池
（AI_MEMORY.local.md 模型约束）。env
``HIVEWEAVE_LLM_FAILOVER_SKIP_MODELS``（逗号分隔，存前小写化）设置时
**整体覆盖**本默认。
"""


def failover_threshold() -> int:
    """转移阈值：env ``HIVEWEAVE_LLM_FAILOVER_THRESHOLD``（默认 3）。"""
    raw = _os.environ.get("HIVEWEAVE_LLM_FAILOVER_THRESHOLD")
    if raw is None or not str(raw).strip():
        return FAILOVER_THRESHOLD_DEFAULT
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return FAILOVER_THRESHOLD_DEFAULT
    return v if v > 0 else FAILOVER_THRESHOLD_DEFAULT


def skip_models() -> frozenset[str]:
    """禁用模型名单（小写集合）。

    env ``HIVEWEAVE_LLM_FAILOVER_SKIP_MODELS``（逗号分隔）设置时覆盖
    默认 :data:`SKIP_MODELS`；匹配对 ``model_id`` 与 ``name`` 均生效
    （大小写不敏感）。
    """
    raw = _os.environ.get("HIVEWEAVE_LLM_FAILOVER_SKIP_MODELS")
    if raw is None or not str(raw).strip():
        return SKIP_MODELS
    items = {s.strip().lower() for s in str(raw).split(",") if s.strip()}
    return frozenset(items) if items else SKIP_MODELS


def _model_in_skip(model: dict, skip: frozenset[str]) -> bool:
    for key in ("model_id", "name"):
        val = str(model.get(key) or "").strip().lower()
        if val and val in skip:
            return True
    return False


async def _pick_candidate(
    current: dict, tried_ids: set[str]
) -> dict | None:
    """从模型池选一个故障转移候选（无候选返回 None = 继续骑乘）。

    过滤：非已试（含当前）主键、禁用名单、API 四要素齐全
    （model_id/base_url/api_key）、**同 provider_type**（方言安全约束）。
    排序：同网关（base_url）优先，组内保持 ``created_at`` 序
    （``list_active_full`` 的 ORDER BY，stable sort 保序）。
    """
    from hiveweave.services.model import ModelService  # 惰性导入防循环

    try:
        pool = await ModelService().list_active_full()
    except Exception as e:  # noqa: BLE001 — 池不可读 = 无候选（继续骑乘）
        log.warning("failover_pool_read_failed", error=str(e)[:200])
        return None
    if not pool:
        return None

    skip = skip_models()
    current_id = str(current.get("id") or "")
    current_base = str(current.get("base_url") or "").rstrip("/")
    current_dialect = str(current.get("provider_type") or "")

    same_gateway: list[dict] = []
    cross_gateway: list[dict] = []
    for m in pool:
        mid = str(m.get("id") or "")
        if not mid or mid in tried_ids or mid == current_id:
            continue
        if _model_in_skip(m, skip):
            continue
        # 方言安全约束：跨 provider_type 不作候选（usage/思考方言错位）。
        # 当前模型缺方言信息（current_config 未登记等）时退化为不限方言
        # —— 拿不到当前方言不该把故障转移整体废掉。
        if current_dialect and str(m.get("provider_type") or "") != current_dialect:
            continue
        if not (
            str(m.get("model_id") or "").strip()
            and str(m.get("base_url") or "").strip()
            and str(m.get("api_key") or "").strip()
        ):
            continue
        if str(m.get("base_url") or "").rstrip("/") == current_base:
            same_gateway.append(m)
        else:
            cross_gateway.append(m)

    ordered = same_gateway + cross_gateway
    if not ordered:
        return None
    return ordered[0]


async def _notify_switch(
    agent_id: str, from_model: str, to_model: str
) -> None:
    """给 agent 发 platform_notice（诚实原则：模型该知道自己换了引擎）。

    best-effort：通知失败只记日志，绝不影响转移本体。wake=False —— 转移
    发生在 run 进行中，通知落 inbox 由 agent 下次读信时消费，不额外造唤醒。
    """
    try:
        from hiveweave.services.inbox import InboxService

        bucket = int(_time.time() // 600)  # 10min 桶防池遍历期的通知风暴
        await InboxService().send_message(
            from_agent_id="system",
            to_agent_id=agent_id,
            message=(
                f"主模型连续故障，本回合已切换 {to_model}"
                f"（原 {from_model or 'primary'}）。"
                "本回合后续请求都走新模型；下回合自动恢复主模型。"
            ),
            message_type="platform_notice",
            priority="normal",
            wake=False,
            trusted_platform=True,
            idempotency_key=f"llm-failover:{agent_id}:{to_model}:{bucket}",
        )
    except Exception as e:  # noqa: BLE001 — 通知是旁支
        log.warning(
            "failover_notice_failed",
            agent_id=agent_id,
            error=str(e)[:200],
        )


class RunFailover:
    """per-run（per-Streamer 实例）故障转移状态机。

    生命周期 = 一个 run（Streamer 实例由 agent 每 turn 新建，实例态天然
    随 run 结束丢弃 ⇒ 「run 结束/下回合恢复主模型」无需显式清理）。
    """

    def __init__(self) -> None:
        self.streak: int = 0
        self.tried_ids: set[str] = set()
        self.active_config: dict | None = None
        self.current_config: dict | None = None

    # ── 状态登记 ─────────────────────────────────────────────

    def bind_primary(self, model_config: dict | None) -> None:
        """登记本 run 的主模型（stream() 入口调用；幂等，不清已切换态）。"""
        if not model_config:
            return
        mid = str(model_config.get("id") or "")
        if mid:
            self.tried_ids.add(mid)
        if self.current_config is None:
            self.current_config = model_config

    def effective_config(self, default_config: dict | None) -> dict | None:
        """当前生效的模型配置：已切换 ⇒ 切换后配置；否则原配置。"""
        if self.active_config is not None:
            return self.active_config
        return default_config

    # ── 计数 ─────────────────────────────────────────────────

    def observe_failure(self) -> int:
        """上游级失败一次（with_retry 闸每失败调用一次），返回累计连败数。"""
        self.streak += 1
        return self.streak

    def observe_success(self) -> None:
        """瞬态成功重置计数（T33.2）：哪怕后续又失败，波次按新梯算。"""
        self.streak = 0

    # ── 转移决策 ─────────────────────────────────────────────

    async def maybe_switch(
        self,
        *,
        agent_id: str,
        from_model: str = "",
    ) -> dict | None:
        """连败达阈值时选候选并切换；无候选返回 None（继续骑乘）。

        切换动作：更新 tried/active 态 + 结构化日志 ``llm_failover_switched``
        + platform_notice（诚实原则）。当前模型的排除由 ``tried_ids``
        （bind_primary/每次切换都登记 DB 主键）保证 —— 不回头。
        """
        if self.streak < failover_threshold():
            return None
        base = self.current_config or self.active_config or {}
        candidate = await _pick_candidate(base, self.tried_ids)
        if candidate is None:
            log.info(
                "llm_failover_no_candidate",
                agent_id=agent_id,
                streak=self.streak,
                tried=len(self.tried_ids),
                note="pool exhausted; keep riding current ladder",
            )
            return None

        to_label = (
            str(candidate.get("name") or "")
            or str(candidate.get("model_id") or "")
            or "failover"
        )
        prev = self.active_config or base or {}
        self.tried_ids.add(str(candidate.get("id") or ""))
        self.active_config = candidate
        self.current_config = candidate
        self.streak = 0  # 新模型新梯：连败计数归零重算
        log.warning(
            "llm_failover_switched",
            agent_id=agent_id,
            from_model=from_model
            or str(prev.get("name") or prev.get("model_id") or "primary"),
            to_model=to_label,
            to_model_id=str(candidate.get("model_id") or ""),
            to_base_url=str(candidate.get("base_url") or "")[:80],
            note=(
                "consecutive upstream failures reached threshold; "
                "temporary for this run only (not persisted to DB)"
            ),
        )
        try:
            from hiveweave.services.event_audit import event_audit

            await event_audit.log(
                agent_id=agent_id,
                project_id="",
                event_type="llm_failover_switched",
                payload={
                    "from_model": str(
                        prev.get("name") or prev.get("model_id") or "primary"
                    ),
                    "to_model": to_label,
                    "to_model_id": str(candidate.get("model_id") or ""),
                },
            )
        except Exception as e:  # noqa: BLE001 — 落账是 best-effort
            log.debug("failover_audit_failed", error=str(e)[:200])
        await _notify_switch(
            agent_id,
            str(prev.get("name") or prev.get("model_id") or ""),
            to_label,
        )
        return candidate
