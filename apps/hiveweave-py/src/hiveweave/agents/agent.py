"""Agent — single agent asyncio task with state machine (契约 04).

核心状态机: idle → processing → idle

关键流程:
1. chat(): 检查 busy/paused → 构建 messages → 启动 LLM task → 设置安全超时
2. _run_llm(): LLM 调用 + tool loop + 空响应重试
3. _handle_completion(): 保存消息 + 标记 inbox 已读 + 自检 re-trigger
4. _handle_empty_response(): 退避重试 [5s, 15s, 45s]，超限升级上级
5. _handle_safety_timeout(): 10 分钟安全超时 → 清理 zombie → 与 _handle_error 统一计数；
   未超限: inbox 保持未读 + RESUME CHECKPOINT + 冷却后恢复；超限: ACK 放弃 + 升级上级
6. cancel(): 取消当前 LLM task → 清理 → 重置 idle

消息布局（DeepSeek 前缀缓存友好）:
  [System 1: identity_prompt]        ← 静态，不随轮次变化
  [System: compacted_prefix]         ← 压缩摘要（如果有）
  [history...]                       ← 对话历史
  [System 2: context_prompt]         ← 动态，每轮重建
  [user message]                     ← 当前用户消息

移植自 Elixir agent.ex (909 行 GenServer)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import random
from enum import Enum
from typing import Any, Awaitable, Callable

import structlog

from hiveweave.conversation.store import ConversationStore, conversation_store
from hiveweave.db import meta as meta_db
from hiveweave.llm.streamer import Streamer
from hiveweave.prompts.context import build_context_prompt
from hiveweave.prompts.identity import build_identity_prompt
from hiveweave.services.approval import approval_service
from hiveweave.services.charter import charter_service
from hiveweave.services.chat_message import ChatMessageService
from hiveweave.services.inbox import InboxService
from hiveweave.services.memory import MemoryService
from hiveweave.services.model import ModelService
from hiveweave.services.org import OrgService
from hiveweave.services.permission import permission_service, PermissionService
from hiveweave.services.skill_registry import SkillRegistryService
from hiveweave.services.system_state import system_state
from hiveweave.services.work_log import WorkLogService
from hiveweave.tools.executor import ToolExecutor
from hiveweave.tools.review import ReviewLLMCallback

log = structlog.get_logger(__name__)


def _short_hash(data: str) -> str:
    """Short SHA256 hash for tool args/results dedup identification."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]

# ── 常量（契约 04）──────────────────────────────────────────

SAFETY_TIMEOUT_MS = 600_000
"""10 分钟安全超时。对齐 Elixir agent.ex:32 @safety_timeout_ms。"""

EMPTY_RETRY_DELAYS = [5_000, 15_000, 45_000]
"""空响应退避序列（5s/15s/45s）。契约 04。"""

MAX_EMPTY_RETRIES = 3
"""空响应最大重试次数。超过则升级上级。"""

TRIGGER_DELAY_MS = 100
"""触发前延迟，等 DB 写入落盘。"""

SELF_RETRIGGER_DELAY_MS = 500
"""自检 retrigger 前的延迟。"""

TIMEOUT_RESUME_COOLDOWN_S = 90.0
"""超时/可恢复错误后，禁止立即重触发同一 agent 的冷却时间。

防止「inbox 未 ACK → watcher 立刻再 trigger → 再超时」的 doom loop，
同时保留消息未读，冷却结束后由 watcher / stall watchdog 恢复信息链。
"""

ERROR_RESUME_COOLDOWN_S = 30.0
"""可恢复 LLM 错误后的短冷却。"""

RATE_LIMIT_RESUME_COOLDOWN_S = 120.0
"""429 / AccountRateLimit 耗尽后的独立冷却（不计入放弃计数）。"""


def is_rate_limit_error(error: BaseException | None) -> bool:
    """True when the failure is provider rate-limit (429), not a hard fault.

    Rate limits must not increment consecutive-error give-up — they are
    temporary quota pressure, not agent failure.
    """
    if error is None:
        return False
    try:
        from hiveweave.llm.retry import RetryableError

        if isinstance(error, RetryableError) and getattr(error, "status", None) == 429:
            return True
    except Exception:
        pass
    msg = str(error).lower()
    needles = (
        "429",
        "accountratelimit",
        "rate limit",
        "ratelimitexceeded",
        "too many requests",
        "rate_limit",
    )
    return any(n in msg for n in needles)


DEFAULT_MAX_TOOL_ROUNDS = 600
"""所有角色统一的 tool loop 最大轮次。不再按角色区别对待。"""

CONTEXT_WINDOW_DEFAULT = 128_000
"""默认 context window（模型配置缺失时）。"""

# ── 工具描述 ────────────────────────────────────────────────
# PermissionService 返回工具名列表；_build_tool_definitions 经
# get_tool_schema_for_llm / get_tool_description 取 schema：
# 优先 TOOL_PARAM_SCHEMAS，否则回退 @tool 注册表（防空 schema）。


def _build_tool_definitions(tool_names: list[str]) -> list[dict]:
    """将工具名列表转为 LLM 工具定义。

    Schema 来自 executor.get_tool_schema_for_llm：优先手写 TOOL_PARAM_SCHEMAS，
    否则回退 @tool 注册表的 Pydantic 模型（避免 waive_attestation 等空 schema）。
    """
    tools: list[dict] = []
    for name in tool_names:
        from hiveweave.tools.executor import (
            get_tool_description,
            get_tool_schema_for_llm,
        )

        params = get_tool_schema_for_llm(name)
        desc = get_tool_description(name)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": params,
                },
            }
        )
    return tools


# ── 状态机 ──────────────────────────────────────────────────


class AgentState(Enum):
    """Agent 状态。对齐 Elixir agent.ex 的 %{} state.status 字段。"""

    IDLE = "idle"
    PROCESSING = "processing"


# ── 回调类型 ────────────────────────────────────────────────

StatusCallback = Callable[[str, str, dict], Awaitable[None] | None]
"""状态变更回调: (agent_id, status, extra) → None。
批次 4 会连接到 WebSocket 广播。"""

StreamEventCallback = Callable[[str, dict], Awaitable[None] | None]
"""流事件回调: (agent_id, event) → None。
批次 4 会连接到 WebSocket 广播。"""


# ── Agent 类 ────────────────────────────────────────────────


class Agent:
    """单个 agent 的 asyncio task 封装。

    对应 Elixir 的 Hiveweave.Agents.Agent GenServer。
    不是长驻 task — LLM 调用是短生命周期 asyncio.Task。
    Agent 对象本身是长期存在的状态容器。

    生命周期:
        am = AgentManager()
        agent = await am.start_agent(agent_id, project_id, config)
        result = await agent.chat("hello")
        # ... agent 内部异步处理 ...
        await am.stop_agent(agent_id)
    """

    def __init__(
        self,
        agent_id: str,
        project_id: str,
        config: dict,
        *,
        on_status_change: StatusCallback | None = None,
        on_stream_event: StreamEventCallback | None = None,
    ) -> None:
        """初始化 Agent。

        Args:
            agent_id: Agent UUID
            project_id: 项目 UUID
            config: agent 配置 dict（来自 Meta DB agents 表）
            on_status_change: 状态变更回调（批次 4 连接 WebSocket）
            on_stream_event: 流事件回调（批次 4 连接 WebSocket）
        """
        self.id = agent_id
        self.project_id = project_id
        self.config = config

        # ── 状态 ──
        self.status = AgentState.IDLE
        self.empty_retry_count = 0
        self.pending_inbox_msg_ids: list[str] | None = None
        self.current_job: dict | None = None
        self._cancel_reason: str | None = None
        self._message_queue: list[tuple[str, dict, int]] = []
        self._streaming_msg_id: str | None = None
        self._reply_reminder_count = 0  # expect_reply 提示次数，3 次后升级到上级
        self._REPLY_REMINDER_MAX = 3   # 即时提醒上限
        self._task_reminder_count = 0  # open-task 收工续跑次数
        self._TASK_REMINDER_MAX = 2    # 防死循环
        self._turn_gate_count = 0      # TurnResult 退出门禁续跑次数
        self._TURN_GATE_MAX = 1        # P0: at most one repair retrigger
        self._resume_cooldown_until: float = 0.0  # monotonic deadline；超时后防 doom loop
        self._consecutive_errors: int = 0  # 连续错误次数，超过阈值后停止 resume
        self._CONSECUTIVE_ERROR_MAX = 3   # 连续错误上限，超过后 ACK inbox 不再 resume
        # Give-up latch: block non-user wakes until user / task unlock / decay
        self._resume_suppressed: bool = False
        self._resume_suppressed_at: float = 0.0  # monotonic when latch armed
        self._RESUME_SUPPRESS_DECAY_S = 30 * 60  # 30 min auto-unlock
        # Ephemeral resume hint — injected once into next _build_messages, not history
        self._pending_resume_hint: str | None = None
        self.disposition: str = "runnable"  # waiting_human|blocked|complete|…
        self._slice_budget: int = 0  # remaining auto-slices for this external wake
        self._SLICE_BUDGET_MAX = 2
        self._progress_fingerprint: str | None = None
        self._no_progress_streak: int = 0
        self._empty_done_slice_streak: int = 0
        self._stream_timeout_streak: int = 0
        self.visibility: str = "foreground"  # foreground|background|system
        self._MERGE_WINDOW_MS = 300  # P1: coalesce trigger wakes

        # ── asyncio 原语 ──
        self._llm_task: asyncio.Task | None = None
        self._safety_timer: asyncio.TimerHandle | None = None
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_active = False

        # ── 回调 ──
        self._on_status_change = on_status_change
        self._on_stream_event = on_stream_event

        # ── 服务实例 ──
        self._streamer: Streamer | None = None  # 延迟创建（需要 role）
        self._tool_executor = ToolExecutor(
            permission_service, approval_service,
            review_llm_callback=self._review_llm_callback,
        )
        self._conversation = conversation_store
        self._inbox = InboxService()
        self._org = OrgService()
        self._model_service = ModelService()
        self._memory = MemoryService()
        self._chat_msg = ChatMessageService()
        self._work_log = WorkLogService()

        # ── Durable Run Ledger ──
        from hiveweave.services.run_ledger import run_ledger
        self._run_ledger = run_ledger
        self._current_activation_id: str | None = None
        self._current_run_id: str | None = None
        self._run_step_counter: int = 0
        # P1 escape valve(TEST10): 连续被 UNREPLIED_ASKS 阻塞的次数
        self._unreplied_asks_streak: int = 0

        # ── 缓存 ──
        self._identity_prompt: str | None = None
        self._workspace_path: str | None = None

        # ── 后台 inbox watcher（BUG-010 修复）───────────────
        # 启动协程，每 5s 轮询未读 inbox 消息并触发 trigger。
        # 解决：executor inbox→agent.run() 链路断裂 — API 写入 inbox 后
        # 没有调用方主动 trigger target agent。
        self._inbox_watcher_task: asyncio.Task | None = None
        self._stop_watcher = False
        self._ensure_watcher_alive()

        log.info(
            "agent_init",
            agent_id=agent_id,
            project_id=project_id,
            name=config.get("name"),
            role=config.get("role"),
        )

    async def _inbox_watcher_loop(self) -> None:
        """BUG-010 修复：后台轮询 inbox，未读时触发 trigger_subordinate。

        间隔 5s（与前端的 chat polling 节流一致），避免过度空转。
        只在 idle 状态触发；processing 状态由 trigger 自己 skip。

        BUG-010 增强：如果 trigger 返回后 agent 仍 idle 且仍有 pending
        inbox 消息，说明 trigger 静默跳过了（e.g. auto-start 失败），
        使用指数退避重试 [5s, 15s, 45s]。
        """
        INTERVAL_S = 5.0
        RETRY_DELAYS = [5.0, 15.0, 45.0]  # 指数退避（秒）
        trigger_fail_count = 0
        # 启动后等 1s 再开始（避开与 trigger.py 的 100ms 起步冲突）
        await asyncio.sleep(1.0)
        while not self._stop_watcher:
            try:
                # 仅当明确 is_started=0 时跳过；查不到项目则 fail-open 继续轮询
                try:
                    from hiveweave.services.project_lifecycle import (
                        project_known_off_duty,
                    )

                    if await project_known_off_duty(self.project_id):
                        await asyncio.sleep(INTERVAL_S)
                        continue
                except Exception:
                    pass
                if self.status == AgentState.IDLE:
                    if self._resume_suppressed:
                        # 被动衰减检查：30min 过期则清除锁存器，落入正常处理
                        if self.try_clear_resume_suppressed(
                            opts={"trigger": True}
                        ):
                            log.debug(
                                "inbox_watcher_suppressed_skip",
                                agent_id=self.id,
                            )
                            await asyncio.sleep(INTERVAL_S)
                            continue
                    if self._in_resume_cooldown():
                        log.debug(
                            "inbox_watcher_cooldown_skip",
                            agent_id=self.id,
                            cooldown_remaining_s=round(
                                self._resume_cooldown_until - time.monotonic(), 1
                            ),
                        )
                        await asyncio.sleep(INTERVAL_S)
                        continue
                    pending = await self._inbox.get_pending_messages(self.id)
                    if pending:
                        log.info(
                            "inbox_watcher_found_pending",
                            agent_id=self.id,
                            count=len(pending),
                            trigger_fail_count=trigger_fail_count,
                        )
                        # 延迟导入避免循环
                        from hiveweave.agents.trigger import (
                            is_coordinator,
                            trigger_coordinator,
                            trigger_subordinate,
                        )
                        role = self.config.get("role", "")
                        if is_coordinator(role):
                            await trigger_coordinator(self.id)
                        else:
                            await trigger_subordinate(self.id)

                        # BUG-010 增强：短暂等待后检查 trigger 是否真的
                        # 启动了处理。如果 idle 且仍有 pending，说明 trigger
                        # 静默跳过（e.g. agent 不在 manager 中且 auto-start 失败）。
                        await asyncio.sleep(2.0)
                        still_pending = await self._inbox.get_pending_messages(self.id)
                        if still_pending and self.status == AgentState.IDLE:
                            trigger_fail_count += 1
                            delay = (
                                RETRY_DELAYS[min(trigger_fail_count - 1, len(RETRY_DELAYS) - 1)]
                                if trigger_fail_count <= len(RETRY_DELAYS)
                                else RETRY_DELAYS[-1]
                            )
                            log.warning(
                                "inbox_watcher_trigger_ineffective",
                                agent_id=self.id,
                                pending_count=len(still_pending),
                                trigger_fail_count=trigger_fail_count,
                                retry_delay_s=delay,
                            )
                            # 退避重试 — 不 sleep interval，用退避延迟
                            try:
                                await asyncio.sleep(delay)
                            except asyncio.CancelledError:
                                break
                            continue
                        else:
                            # trigger 成功，重置失败计数
                            trigger_fail_count = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(
                    "inbox_watcher_error",
                    agent_id=self.id,
                    error=str(e),
                )
            # 用 sleep 替代固定 wait，便于 cancel
            try:
                await asyncio.sleep(INTERVAL_S)
            except asyncio.CancelledError:
                break

    def _ensure_watcher_alive(self) -> None:
        """确保 inbox watcher 存活；被 cancel() 杀掉后复活它。

        cancel() 置 _stop_watcher=True 并取消 watcher 协程，但 Agent 对象
        仍留在 manager 中可被再次激活（chat/trigger；deactivate→activate
        时 supervisor 跳过已存在实例）。不复活的话 watcher 永久死亡，
        agent 读不到同伴消息（"失联"），直到进程重启。

        幂等：watcher 存活时直接返回，不重复启动。同步方法（检查与赋值
        之间无 await），与 cancel() 在同一事件循环内不会交错；
        cancel() 置标志 + cancel task 之间同样无 await，即时停止语义不变。
        """
        if not hasattr(self, "_stop_watcher"):
            # object.__new__(Agent) 裸实例（测试 double，未走 __init__）—
            # watcher 不在其生命周期内，跳过
            return
        task = self._inbox_watcher_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有 running loop（e.g. 测试场景）— 跳过，下次激活再试
            return
        self._stop_watcher = False
        self._inbox_watcher_task = loop.create_task(
            self._inbox_watcher_loop(),
            name=f"agent-{self.id}-inbox-watcher",
        )
        log.info("inbox_watcher_started", agent_id=self.id)

    def _arm_resume_suppressed(self) -> None:
        """Arm give-up latch (blocks non-user/non-task wakes until decay)."""
        self._resume_suppressed = True
        self._resume_suppressed_at = time.monotonic()

    def _clear_resume_suppressed(self, *, reason: str = "ok") -> None:
        if not getattr(self, "_resume_suppressed", False):
            return
        self._resume_suppressed = False
        self._resume_suppressed_at = 0.0
        log.info(
            "resume_suppressed_cleared",
            agent_id=self.id,
            by=reason,
        )

    def try_clear_resume_suppressed(self, opts: dict | None = None) -> bool:
        """Clear latch if user/task wake or 30min decay. Return True if still blocked."""
        if not getattr(self, "_resume_suppressed", False):
            return False
        opts = opts or {}
        is_user_wake = not bool(opts.get("trigger"))
        source = opts.get("source") or ""
        is_task_wake = (
            source
            in (
                "task",
                "dispatch",
                "task_transition",
                "inbox_task",
                "verify",
            )
            or bool(opts.get("task_id"))
            or opts.get("message_type") == "task"
        )
        suppressed_at = float(getattr(self, "_resume_suppressed_at", 0.0) or 0.0)
        decay_s = float(
            getattr(self, "_RESUME_SUPPRESS_DECAY_S", 30 * 60) or (30 * 60)
        )
        aged = (
            time.monotonic() - suppressed_at >= decay_s if suppressed_at else False
        )
        if is_user_wake or is_task_wake or aged:
            self._clear_resume_suppressed(
                reason=(
                    "user"
                    if is_user_wake
                    else "task"
                    if is_task_wake
                    else "decay"
                )
            )
            return False
        return True

    # ── 公共 API ─────────────────────────────────────────────

    async def chat(self, message: str, opts: dict | None = None) -> dict:
        """发起 LLM 调用。返回 {ok, error}。

        - 如果 processing → 返回 {error: "busy"}
        - 如果系统暂停 → 返回 {error: "paused"}
        - 否则启动 LLM task，返回 {ok: true}

        对齐 Elixir agent.ex:67 handle_call({:chat, message, opts})。
        """
        opts = opts or {}

        async with self._lock:
            # cancel() 会杀掉 inbox watcher — agent 再次被激活时复活它，
            # 否则 agent 此后读不到同伴消息（"失联"）直到进程重启。
            self._ensure_watcher_alive()

            # 检查 busy → queue the message instead of dropping it
            if self.status == AgentState.PROCESSING:
                self._message_queue.append((message, opts, int(time.time() * 1000)))
                # FIX(dup-user-msg): API 层在调 chat() 前已保存了 user 消息，
                # 但如果 API 的 busy 预检和 chat() 的锁之间发生竞态（agent 刚变
                # busy），这里会再存一份。用 3 秒窗口去重。
                already_saved = False
                try:
                    recent = await self._chat_msg.get_messages(
                        self.id, limit=3
                    )
                    now_ms = int(time.time() * 1000)
                    for m in (recent or []):
                        if (
                            m.get("role") == "user"
                            and m.get("content") == message
                            and (now_ms - (m.get("created_at") or 0)) < 3000
                        ):
                            already_saved = True
                            break
                except Exception:
                    pass  # 去重失败不阻断，宁可多存一条
                if not already_saved:
                    await self._chat_msg.save_message({
                        "agent_id": self.id, "role": "user",
                        "content": message,
                        "is_background": False, "is_read": False,
                    })
                log.info("chat_queued", agent_id=self.id,
                         queue_len=len(self._message_queue),
                         preview=message[:80],
                         deduped=already_saved)
                return {"ok": True, "queued": True}

            # 检查暂停
            if system_state.paused():
                return {"error": "paused"}

            # Bug K fix: 检查项目是否"上班"状态
            from hiveweave.db import meta as _meta_db
            _proj = await _meta_db.query_one(
                "SELECT is_started FROM projects WHERE id = ?",
                [self.project_id]
            )
            if not _proj or not dict(_proj).get("is_started"):
                return {"error": "project_not_started"}

            # Give-up latch: user / task-class wakes unlock; else 30min decay
            if self.try_clear_resume_suppressed(opts):
                log.info(
                    "wake_suppressed_gave_up",
                    agent_id=self.id,
                    source=(opts or {}).get("source") or "trigger",
                )
                return {"ok": True, "suppressed": True}

            # Complete + non-user trigger: skip unless admit_wake pierces
            # (same gate as inbox wake bits — ask / superior command / task).
            if self.disposition == "complete" and (opts or {}).get("trigger"):
                o = opts or {}
                from hiveweave.services.wake_policy import admit_wake

                wake_cat = (o.get("wake_category") or "command") or "command"
                if wake_cat not in (
                    "command",
                    "ask",
                    "approval",
                    "task_transition",
                    "progress",
                ):
                    wake_cat = "command"
                parent_id = self.config.get("parent_id")
                admit = admit_wake(
                    wake_cat,  # type: ignore[arg-type]
                    disposition="complete",
                    from_agent_id=o.get("from_agent_id"),
                    recipient_parent_id=parent_id,
                )
                # Task-class sources still pierce even if category missing
                source = o.get("source") or ""
                is_task_wake = source in (
                    "task",
                    "dispatch",
                    "task_transition",
                    "inbox_task",
                    "verify",
                ) or o.get("message_type") == "task" or bool(o.get("task_id"))
                # Reminder / wait-timeout must not burn tokens after complete
                if source in (
                    "open_task_reminder",
                    "wait_timeout",
                    "turn_exit_gate",
                ):
                    log.info(
                        "chat_complete_skip_trigger",
                        agent_id=self.id,
                        source=source,
                        wake_category=wake_cat,
                        admit_reason="complete_no_reminder",
                    )
                    return {"ok": True, "skipped": "complete"}
                if not admit.ok and not is_task_wake and not o.get("from_user"):
                    log.info(
                        "chat_complete_skip_trigger",
                        agent_id=self.id,
                        source=source,
                        wake_category=wake_cat,
                        admit_reason=admit.reason,
                    )
                    return {"ok": True, "skipped": "complete"}

            # 设置状态
            self.status = AgentState.PROCESSING
            self._cancel_reason = None
            self.empty_retry_count = 0
            source = (opts or {}).get("source") or ""
            # External wakes refill slice budget; gate/reminder turns do not
            if source not in ("turn_exit_gate", "open_task_reminder"):
                self._slice_budget = self._SLICE_BUDGET_MAX
                # New wake: allow [TASK ADVANCE] again; clear explicit 不推进
                self._task_reminder_count = 0
                try:
                    from hiveweave.services.turn_session import (
                        clear_task_advance_deferred,
                    )

                    clear_task_advance_deferred(self.id)
                except Exception:
                    pass
                # Clear wait contracts only on user wakes or wait-satisfaction
                # wakes — NOT on stall/ledger/watchdog triggers (TEST11 audit C2).
                # Stall nudges must not wipe a legal waiting_on agent contract.
                _CLEAR_WAIT_SOURCES = frozenset({
                    "", "user", "chat",
                    "wait_timeout", "wait_cycle", "wait_satisfied",
                    "message_from_ref",
                })
                should_clear_waits = (
                    bool(opts.get("clear_waits"))
                    or not opts.get("trigger")
                    or source in _CLEAR_WAIT_SOURCES
                )
                if should_clear_waits:
                    try:
                        from hiveweave.services.wait_contract import (
                            wait_contract_service,
                        )

                        await wait_contract_service.clear_waits(
                            self.project_id, self.id
                        )
                    except Exception as e:
                        log.debug("clear_waits_on_wake_failed", error=str(e))
                if source in ("user", "chat", "") or not opts.get("trigger"):
                    # User-facing chat clears waiting_human into runnable while processing
                    if self.disposition == "waiting_human" and not opts.get("trigger"):
                        self.disposition = "runnable"
            self.visibility = (
                "system" if source in ("turn_exit_gate", "open_task_reminder")
                else "foreground" if not opts.get("trigger") else "background"
            )
            try:
                from hiveweave.services.telemetry import telemetry

                reason = (
                    "user" if not opts.get("trigger")
                    else source or "trigger"
                )
                if opts.get("merged_wakes"):
                    reason = "merged_trigger"
                telemetry.agent_wake(self.id, reason, source=source)
            except Exception:
                pass
            self._broadcast_status(
                "processing",
                {
                    "disposition": self.disposition,
                    "visibility": self.visibility,
                },
            )

            # Fresh turn — drop stale TurnResult + re-resolve workspace (worktree may bind mid-flight)
            from hiveweave.services.turn_session import clear_pending_turn_result

            clear_pending_turn_result(self.id)
            self._workspace_path = None

            # 保存 inbox_msg_ids（在 LLM 非空输出后标记已读）
            self.pending_inbox_msg_ids = opts.get("inbox_msg_ids")

            # 记录当前 job
            self.current_job = {
                "message": message,
                "opts": opts,
                "started_at": int(time.time() * 1000),
            }

            # ── Durable Run Ledger: create activation ──
            # Check for interrupted run from previous activation
            interrupted_summary = None
            interrupted_run_id = None
            try:
                prev_interrupted = await self._run_ledger.find_interrupted_run(self.id)
                if prev_interrupted:
                    interrupted_run_id = prev_interrupted["run_id"]
                    interrupted_summary = await self._run_ledger.generate_checkpoint(
                        self.id, interrupted_run_id
                    )
                    log.info(
                        "run_ledger.found_interrupted",
                        agent_id=self.id,
                        interrupted_run_id=interrupted_run_id,
                        summary_preview=interrupted_summary[:200],
                    )
            except Exception as e:
                log.debug("run_ledger.check_interrupted_failed", error=str(e))

            # Create activation record
            trigger = opts.get("trigger") or {}
            trigger_type = (opts.get("source") or "chat")
            trigger_source = ""
            if isinstance(trigger, dict):
                trigger_source = trigger.get("from_agent_id") or trigger.get("source") or ""
            inbox_ids = opts.get("inbox_msg_ids") or []
            try:
                self._current_activation_id = await self._run_ledger.create_activation(
                    agent_id=self.id,
                    trigger_type=trigger_type,
                    trigger_source=trigger_source,
                    trigger_detail=message[:200],
                    inbox_msg_ids=inbox_ids,
                    interrupted_run_id=interrupted_run_id,
                    checkpoint_summary=interrupted_summary,
                )
            except Exception as e:
                log.debug("run_ledger.create_activation_failed", error=str(e))
            self._current_run_id = None
            self._run_step_counter = 0

            # If we have an interrupted run checkpoint, prepend it to the message
            if interrupted_summary:
                message = (
                    f"[RUN RECOVERY] Previous run was interrupted. "
                    f"Completed steps:\n{interrupted_summary}\n"
                    f"Please continue from where you left off.\n\n{message}"
                )

            # Drop leftover streaming rows from a prior incomplete turn before
            # inserting a new placeholder (product auto-heal — no manual clear).
            try:
                await self._chat_msg.update_streaming_messages_done(self.id)
            except Exception as e:
                log.warning(
                    "pre_turn_clear_streaming_failed",
                    agent_id=self.id,
                    error=str(e),
                )

            # Save a streaming placeholder BEFORE the LLM call.
            # If the agent crashes mid-response, this partial record survives.
            is_trigger = opts.get("trigger", False)
            saved = await self._chat_msg.save_message({
                "agent_id": self.id,
                "role": "assistant",
                "content": "",
                "thinking": None,
                "tool_calls": "[]",
                "is_streaming": True,
                "is_background": True if is_trigger else False,
            })
            self._streaming_msg_id = saved["id"]

            # 启动 LLM task
            self._llm_task = asyncio.create_task(
                self._run_llm(message, opts),
                name=f"agent-{self.id}-llm",
            )

            # 设置安全超时
            self._start_safety_timer()

            return {"ok": True}

    async def cancel(self, *, reason: str = "cancelled") -> None:
        """取消当前处理。

        对齐 Elixir agent.ex:131 handle_cast(:cancel)。
        reason=off_duty：下班停机 — 不 ACK pending inbox（已由 park 处理），
        streaming 文案区分于普通中断。
        """
        from hiveweave.services.project_lifecycle import (
            OFF_DUTY_CANCEL_REASON,
            OFF_DUTY_STREAM_CONTENT,
        )

        self._cancel_reason = reason
        is_off_duty = reason == OFF_DUTY_CANCEL_REASON
        self._cancel_safety_timer()

        if self._llm_task and not self._llm_task.done():
            self._llm_task.cancel()
            try:
                await self._llm_task
            except asyncio.CancelledError:
                pass

        # BUG-010 修复：停 inbox watcher（下次激活时由
        # _ensure_watcher_alive() 复活 — 见 chat()/enqueue_wake()）
        self._stop_watcher = True
        if self._inbox_watcher_task and not self._inbox_watcher_task.done():
            self._inbox_watcher_task.cancel()
            try:
                await self._inbox_watcher_task
            except asyncio.CancelledError:
                pass
            except RuntimeError as e:
                # 跨 loop 取消（TestClient 等多 loop 场景）
                if "attached to a different loop" in str(e):
                    pass
                else:
                    raise

        # 确保状态重置
        if self.status == AgentState.PROCESSING:
            # 普通 cancel：ACK pending，避免 watcher 死循环。
            # 下班：保留未读（已 park），复工 briefing 会合并唤醒。
            if self.pending_inbox_msg_ids and not is_off_duty:
                try:
                    await self._inbox.mark_read_by_ids(
                        self.id, self.pending_inbox_msg_ids
                    )
                    log.info("cancel_marked_inbox_read",
                             agent_id=self.id,
                             msg_count=len(self.pending_inbox_msg_ids))
                except Exception as e:
                    log.warning("cancel_mark_inbox_read_failed",
                                agent_id=self.id, error=str(e))
                self.pending_inbox_msg_ids = None
            elif is_off_duty:
                self.pending_inbox_msg_ids = None
            # A6(2) 修复：cancel 时清理 streaming 标志，防止僵尸消息
            try:
                await self._finalize_streaming_turn(
                    content=(
                        OFF_DUTY_STREAM_CONTENT
                        if is_off_duty
                        else "[对话被中断]"
                    ),
                )
            except Exception as e:
                log.warning("cancel_clear_streaming_failed",
                            agent_id=self.id, error=str(e))
            self._reset_to_idle()
        elif is_off_duty:
            # idle：只清悬挂状态，不强行写「被中断」气泡
            self.pending_inbox_msg_ids = None
            self._reset_to_idle()

    async def trigger(self, trigger_type: str = "subordinate") -> dict:
        """触发 agent 处理待处理内容。

        延迟导入 trigger.py 避免循环依赖:
        agent.py → trigger.py → supervisor.py → agent.py
        """
        from hiveweave.agents.trigger import _do_trigger

        await _do_trigger(self.id, trigger_type)
        return {"ok": True}

    # ── 内部: LLM 调用 ───────────────────────────────────────

    async def _run_llm(self, message: str, opts: dict) -> None:
        """内部: LLM 调用 + tool loop + 空响应重试。作为 asyncio.Task 运行。

        对齐 Elixir agent.ex:336 run_llm/2 + handle_info({ref, result})。
        """
        current_task = asyncio.current_task()
        # Placeholder created in chat() before this task starts — own it so
        # finally never clears a newer turn's streaming row.
        owned_streaming_id = self._streaming_msg_id
        try:
            # 构建 messages
            messages = await self._build_messages(message, opts)

            # 获取模型配置
            model_config = await self._get_model_config()
            if model_config is None:
                await self._handle_error(
                    ValueError(
                        f"No model configured for agent {self.id} "
                        f"(model_id={self.config.get('model_id')})"
                    )
                )
                return

            # 获取工具定义
            tools = await self._get_tool_definitions()

            # ── Durable Run Ledger: create run ──
            try:
                self._current_run_id = await self._run_ledger.create_run(
                    agent_id=self.id,
                    activation_id=self._current_activation_id or "",
                )
            except Exception as e:
                log.debug("run_ledger.create_run_failed", error=str(e))
            self._run_step_counter = 0

            # 创建 Streamer（统一 max_tool_rounds = 600）
            max_rounds = self._get_max_tool_rounds()
            streamer = Streamer(max_tool_rounds=max_rounds)

            log.info(
                "llm_start",
                agent_id=self.id,
                model=model_config.get("model_id"),
                tool_count=len(tools),
                max_rounds=max_rounds,
                msg_count=len(messages),
            )

            # 启动 thinking 心跳 — 让前端知道 agent 在工作
            self._start_heartbeat()

            # 空响应重试循环
            current_messages = list(messages)
            while True:
                # Unified activation budget check — stop before exceeding limits
                _run_id = getattr(self, "_current_run_id", None)
                if _run_id:
                    try:
                        exceeded, reason = await self._run_ledger.check_budget(
                            self.id, _run_id
                        )
                        if exceeded:
                            log.warning(
                                "activation_budget_exceeded",
                                agent_id=self.id,
                                reason=reason,
                            )
                            await self._handle_safety_timeout()
                            return
                    except Exception:
                        pass  # best-effort

                result = await streamer.stream(
                    agent_id=self.id,
                    messages=current_messages,
                    model_config=model_config,
                    tools=tools,
                    on_delta=self._on_delta,
                    on_tool_call=self._on_tool_call,
                    max_tool_rounds=max_rounds,
                )

                status = result.get("status", "error")

                if status == "empty":
                    # 空响应处理
                    should_retry = await self._handle_empty_response(
                        result, current_messages
                    )
                    if not should_retry:
                        # 已升级上级，退出循环
                        break
                    # 重置文本累积器 — 防止跨重试轮次堆叠"（收到空响应…）"
                    self._streaming_text_acc = ""
                    # 重新构建 messages（注入重试提示）
                    current_messages = await self._build_messages(
                        message, opts, retry_hint=True
                    )
                    continue

                if status == "ok":
                    await self._handle_completion(result, message, opts)
                else:
                    error_msg = result.get("error") or "Unknown LLM error"
                    await self._handle_error(ValueError(error_msg))

                break

        except asyncio.CancelledError:
            reason = self._cancel_reason or "unknown"
            log.warning("llm_task_cancelled", agent_id=self.id, reason=reason)
            if reason == "safety_timeout":
                await self._handle_safety_timeout()
            else:
                await self._handle_cancel()
            # 吞掉 CancelledError — 已在 handler 中清理

        except Exception as e:
            log.error(
                "llm_task_crashed",
                agent_id=self.id,
                error=str(e),
                exc_info=True,
            )
            await self._handle_error(e)

        finally:
            # 确保心跳停止（所有退出路径的兜底）
            self._stop_heartbeat()
            # 只有当前 task 仍是 self._llm_task 时才清理状态
            # （cancel() 后若新 chat() 启动了新 task，不应清理新 task 的状态）
            if self._llm_task is current_task:
                self._llm_task = None
                self._cancel_safety_timer()
                # Finalize only this task's placeholder. Prefer row update;
                # allow agent-wide fallback only if we still own the pointer
                # (no newer chat() has replaced _streaming_msg_id).
                if owned_streaming_id:
                    try:
                        await self._finalize_streaming_turn(
                            msg_id=owned_streaming_id,
                            allow_agent_wide_fallback=(
                                self._streaming_msg_id == owned_streaming_id
                            ),
                        )
                    except Exception as e:
                        log.warning(
                            "finally_clear_streaming_failed",
                            agent_id=self.id,
                            error=str(e),
                        )
                if self.status == AgentState.PROCESSING:
                    self._reset_to_idle()

    # ── 内部: 消息构建 ────────────────────────────────────────

    async def _build_messages(
        self,
        message: str,
        opts: dict,
        *,
        retry_hint: bool = False,
    ) -> list[dict]:
        """构建 LLM 消息列表。

        布局（DeepSeek 前缀缓存友好）:
        [System 1: identity_prompt]        ← 静态
        [System: compacted_prefix]         ← 压缩摘要（如果有）
        [history...]                       ← 对话历史
        [System 2: context_prompt]         ← 动态
        [user message]                     ← 当前用户消息

        对齐 Elixir agent.ex:433 build_messages/3。
        """
        messages: list[dict] = []

        # 1. Identity prompt (System 1 — 静态，前缀缓存友好)
        identity = self._get_identity_prompt()
        messages.append({"role": "system", "content": identity})

        # 2. Compacted prefix (System — 压缩摘要)
        compacted = self._conversation.get_compacted_prefix(
            self.project_id, self.id
        )
        if compacted:
            messages.append({"role": "system", "content": compacted})

        # 3. History
        context_window = self._get_context_window()
        token_budget = max(context_window // 2, 16_000)
        history = await self._conversation.get_history(
            self.id, self.project_id, token_budget
        )
        messages.extend(history)

        # 4. Context prompt (System 2 — 动态)
        # FIX(trigger-dedup): trigger 调用时跳过 handoffs（已在 trigger 上下文中）
        is_trigger = bool(opts.get("trigger"))
        context = await self._build_context_prompt(skip_handoffs=is_trigger)
        if context:
            messages.append({"role": "system", "content": context})

        # 5. User message
        user_content = message
        if retry_hint:
            user_content = (
                f"{message}\n\n"
                "[系统提示] 上一轮响应为空。请确保输出有效内容或调用工具。"
            )
        messages.append({"role": "user", "content": user_content})

        # 5b. Ephemeral RESUME CHECKPOINT — once per interrupt, not into history
        hint = self._pending_resume_hint
        if hint:
            messages.append({"role": "user", "content": hint})
            self._pending_resume_hint = None

        return messages

    def _get_identity_prompt(self) -> str:
        """构建 identity prompt（缓存）。

        对齐 Elixir streamer.ex: build_identity_prompt/1。
        """
        if self._identity_prompt is not None:
            return self._identity_prompt

        self._identity_prompt = build_identity_prompt(
            role=self.config.get("role", "executor"),
            role_type=self.config.get("role_type", "executor"),
            backstory=self.config.get("backstory", ""),
            name=self.config.get("name", ""),
            goal=self.config.get("goal", ""),
            model_id=self.config.get("model_id", ""),
        )
        return self._identity_prompt

    async def _build_context_prompt(self, *, skip_handoffs: bool = False) -> str:
        """构建 context prompt（动态，每轮重建）。

        对齐 Elixir streamer.ex: build_context_prompt/1。

        skip_handoffs: trigger 调用时为 True — trigger 上下文的 Pending Tasks
        block 已包含 handoff 信息，context prompt 不再重复。
        """
        # Memories
        memory_text = await self._memory.build_agent_context(
            self.id, self.project_id, module_id=None
        )

        # Goals — only inject when dirty (CEO/user modified since last read).
        # Saves tokens: unchanged goals skip the ~200-token workbook block.
        goals = None
        if charter_service.goals_dirty(self.id, self.project_id):
            goals = await charter_service.read_goals(self.project_id)
            if goals:
                cur_ver = charter_service.get_goals_version(self.project_id)
                await charter_service.set_agent_goals_version(self.id, cur_ver)

        # Org directory — only inject when dirty (org chart changed since last read).
        # 仿照 goals_dirty: create/dismiss/transfer agent 时 bump version,
        # agent 首次对话注入一次精简通讯录后清除标记, 避免重复注入浪费 token.
        org_directory = ""
        if self._org.org_dirty(self.id, self.project_id):
            org_directory = await self._org.build_org_directory(self.project_id)
            if org_directory:
                cur_org_ver = self._org.get_org_version(self.project_id)
                await self._org.set_agent_org_version(self.id, cur_org_ver)

        # Involvement level
        involvement = self.config.get("involvement_level", "medium")

        # Bound skills
        bound_skills_json = self.config.get("bound_skills", "[]")
        skills_section = SkillRegistryService.build_active_skills_section(
            bound_skills_json
        )

        # Handoffs (accepted, for context)
        # FIX(trigger-dedup): trigger 上下文已包含 handoff 信息，跳过以避免重复
        from hiveweave.services.handoff import HandoffService

        handoff_service = HandoffService()
        if skip_handoffs:
            handoffs = None
        else:
            handoffs = await handoff_service.get_accepted_handoffs(
                self.project_id, self.id
            )

        # Project rules from charter (CEO 摸底后填入)
        project_rules = ""
        try:
            charter_data = await charter_service.read_charter(self.project_id)
            if charter_data and charter_data.get("project_rules"):
                project_rules = charter_data["project_rules"]
        except Exception:
            log.debug("read_charter for project_rules failed", project_id=self.project_id, exc_info=True)

        context = build_context_prompt(
            agent_id=self.id,
            memories=None,  # memory_text 单独传（见下），不走 memories list
            handoffs=handoffs,
            goals=goals,
            involvement_level=involvement,
            bound_skills=bound_skills_json,
            memory_text=memory_text,
            project_rules=project_rules,
        )

        # 追加 skills section
        if skills_section:
            context = f"{context}\n\n{skills_section}"

        # 追加 org directory (仅 org chart 变更后首次对话注入)
        if org_directory:
            context = f"{context}\n\n{org_directory}"

        return context

    # ── 内部: 模型 & 工具 ────────────────────────────────────

    async def _get_model_config(self) -> dict | None:
        """获取模型配置；多渠道激活时 round-robin 摊配额。

        对齐 Elixir agent.ex:474 get_model_config/1。
        """
        model_id = self.config.get("model_id")
        try:
            from hiveweave.config import settings

            if getattr(settings, "model_pool_enabled", True):
                picked = await self._model_service.pick_from_pool(model_id)
                if picked:
                    return picked
        except Exception as e:
            log.debug("model_pool_pick_failed", error=str(e))
        if not model_id:
            return None
        return await self._model_service.get(model_id)

    async def _review_llm_callback(self, system_prompt: str, user_prompt: str) -> str:
        """LLM callback for review tools — makes a non-streaming LLM call.

        Uses the agent's model config + provider to call the LLM with
        system_prompt + user_prompt and return the text response.
        This is used by run_code_review / run_tests / etc.
        """
        model_config = await self._get_model_config()
        if model_config is None:
            raise RuntimeError("No model configured for review LLM callback")

        from hiveweave.llm.provider import provider_factory
        provider = provider_factory.create(model_config)

        import httpx
        body = provider.build_body(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            temperature=0.3,
        )
        headers = provider.build_headers()
        # Non-streaming: override Accept header
        headers["Accept"] = "application/json"

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
        ) as client:
            resp = await client.post(
                provider.build_url(), json=body, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return ""

    async def _get_tool_definitions(self) -> list[dict]:
        """获取工具定义列表（family-aware；硬能力由 PolicyService 在 evaluate 时再挡）。"""
        mode = await permission_service.get_permission_mode(self.id)
        role_type = self.config.get("role_type", "executor")
        tool_names = permission_service.get_tools_for_agent({
            **self.config,
            "role": self.config.get("role") or role_type,
            "permission_type": self.config.get("permission_type")
            or ("coordinator" if role_type == "coordinator" else "executor"),
            "permission_mode": mode,
        })
        if not tool_names:
            tool_names = permission_service.get_tools_for_mode(mode)

        if not tool_names and mode == "custom":
            allowed_raw = self.config.get("allowed_tools", "[]")
            try:
                tool_names = json.loads(allowed_raw) if isinstance(
                    allowed_raw, str
                ) else (allowed_raw or [])
            except (json.JSONDecodeError, TypeError):
                tool_names = []

        return _build_tool_definitions(tool_names)

    def _get_max_tool_rounds(self) -> int:
        """获取 tool loop 最大轮次。所有角色统一 600 次。"""
        return DEFAULT_MAX_TOOL_ROUNDS

    def _get_context_window(self) -> int:
        """获取 context window 大小。"""
        # 从 agent config 的 model 配置中获取，或用默认值
        # ModelService.get() 返回的 dict 有 context_window 字段
        # 但在构建 messages 时 model_config 可能还没获取
        # 这里用 config 中缓存的值或默认值
        return self.config.get("_context_window", CONTEXT_WINDOW_DEFAULT)

    async def _get_workspace_path(self) -> str:
        """获取工作区路径（每轮 chat 开始会清空缓存）。

        优先使用 agent 专属的 worktree 路径（agents.workspace_path），
        实现 agent 间工作区隔离。Executor / builder coordinator 若未绑定或
        路径失效，会懒创建 worktree 并写回 DB（不依赖仅启动时的 lifespan
        recovery）。CEO/HR 强制项目根（并清掉误绑的 worktree 路径）；
        恢复失败时回退到项目根目录。
        """
        if self._workspace_path is not None:
            return self._workspace_path

        import os as _os
        from pathlib import Path as _Path

        project_ws = await meta_db.get_project_workspace(self.project_id) or ""

        try:
            from hiveweave.services.org import OrgService
            from hiveweave.services.git_worktree import agent_gets_write_worktree

            org = OrgService()
            agent_row = await org.get_agent(self.id)
            if agent_row:
                ws = agent_row.get("workspace_path") or ""

                # CEO/HR must never work from a write worktree；builder
                # coordinator（family=coordinator 有 SOURCE_WRITE）保留自己的树
                if not agent_gets_write_worktree(agent_row):
                    if ws and "worktrees" in ws.replace("\\", "/"):
                        try:
                            await org.update_agent(self.id, {"workspace_path": None})
                        except Exception:
                            pass
                    self._workspace_path = project_ws
                    return self._workspace_path

                if ws and _os.path.isdir(ws) and (_Path(ws) / ".git").exists():
                    self._workspace_path = ws
                    return self._workspace_path

                # Writer without a valid worktree — allocate now
                if project_ws and (_Path(project_ws) / ".git").exists():
                    from hiveweave.services.git_worktree import ensure_executor_worktree

                    # P0: 此处没有当前任务上下文（agent 不跟踪 current task），
                    # 不传 task_id → 落 hw/<shortId>/work 稳定分支；
                    # task_name 兼容保留但不再参与命名。
                    ensured = await ensure_executor_worktree(
                        self.project_id,
                        self.id,
                        task_name=agent_row.get("role") or "executor",
                    )
                    if ensured.get("success") and ensured.get("path"):
                        self._workspace_path = ensured["path"]
                        return self._workspace_path
        except Exception:
            pass  # 回退到项目根

        self._workspace_path = project_ws
        return self._workspace_path

    # ── 内部: 完成处理 ────────────────────────────────────────

    async def _handle_completion(
        self,
        result: dict,
        message: str,
        opts: dict,
    ) -> None:
        """正常完成处理。

        对齐 Elixir agent.ex:553 handle_info({ref, {:ok, ...}})。

        流程:
        1. 取消安全定时器
        2. 保存消息到 chat_messages + conversation store
        3. 标记 inbox 已读
        4. 状态 → idle
        5. 自检 re-trigger
        """
        content = result.get("content", "")
        thinking = result.get("thinking")
        tool_calls = result.get("tool_calls", [])

        # ── Durable Run Ledger: mark run completed ──
        _run_id = getattr(self, "_current_run_id", None)
        if _run_id:
            try:
                summary = (content or "")[:200]
                await self._run_ledger.complete_run(
                    agent_id=self.id,
                    run_id=_run_id,
                    result_summary=summary,
                )
            except Exception as e:
                log.debug("run_ledger.complete_run_failed", error=str(e))
        tool_turn_messages = result.get("tool_turn_messages", [])

        log.info(
            "llm_completion",
            agent_id=self.id,
            content_len=len(content),
            tool_calls=len(tool_calls),
            rounds=result.get("rounds", 0),
            usage=result.get("usage"),
        )

        # 1. 先写 work_log（在 update_message 之前，确保监控有数据）
        # BUG-026 修复：自动写 work_log，确保前端 Logs tab 有内容。
        # 放在 update_message 之前——update_message 可能因类型问题崩溃
        # （如 thinking 意外为 dict），work_log 不应被其连累。
        try:
            summary_src = content if content else message
            summary = (summary_src or "").strip().replace("\n", " ")[:140]
            if not summary:
                summary = "(empty response)"
            log_type = "completion" if content else "discussion"
            details: dict | None = None
            if tool_calls:
                names = sorted({
                    tc.get("function", {}).get("name", "?")
                    for tc in tool_calls
                    if isinstance(tc, dict)
                })
                details = {
                    "tool_calls": names,
                    "rounds": result.get("rounds", 0),
                }
            await self._work_log.write_work_log(
                self.project_id, self.id, None, log_type, summary,
                details=details,
            )
        except Exception as e:
            log.warning("auto_work_log_failed", agent_id=self.id, error=str(e))

        # 2. 保存 assistant 消息到 chat_messages
        # 更新先前保存的 streaming placeholder，而非插入新消息。
        # 用 try/except 包裹——保存失败不应导致整个 completion 崩溃。
        # 失败了就降级保存一条简单消息 + 注入对话反馈，让 AI 知道格式有问题。
        is_trigger = opts.get("trigger", False)
        _save_failed = False
        _save_error_msg = ""
        try:
            tool_calls_json = (
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls else "[]"
            )
            if self._streaming_msg_id:
                cleared = await self._finalize_streaming_turn(
                    content=content,
                    thinking=thinking if thinking is not None else None,
                    tool_calls_json=tool_calls_json,
                )
                if not cleared:
                    _save_failed = True
                    _save_error_msg = "finalize_streaming_turn failed"
            else:
                await self._chat_msg.save_message(
                    {
                        "agent_id": self.id,
                        "role": "assistant",
                        "content": content,
                        "thinking": thinking,
                        "tool_calls": tool_calls_json,
                        "is_streaming": False,
                        "is_background": True if is_trigger else False,
                    }
                )
        except Exception as e:
            _save_failed = True
            _save_error_msg = str(e)
            log.error("completion_save_failed",
                      agent_id=self.id, error=_save_error_msg)
            try:
                await self._finalize_streaming_turn(
                    content=content[:500] if content else "(empty)",
                )
            except Exception:
                pass  # 尽力了

        # 3. 追加到 conversation store
        # user message + tool turn messages (assistant+tool pairs) + final assistant
        turn_messages: list[dict] = [{"role": "user", "content": message}]
        # 如果消息保存失败，注入错误反馈让 AI 意识到问题
        if _save_failed:
            turn_messages.append({
                "role": "tool",
                "tool_call_id": "_save_message",
                "content": (
                    f"SYSTEM ERROR: Your last response was produced successfully "
                    f"(content length: {len(content)}, tool calls: {len(tool_calls)}), "
                    f"but saving it to the database failed with: {_save_error_msg}. "
                    f"This is a platform bug (type mismatch in message field), NOT your fault. "
                    f"The user can still see your response in the conversation history. "
                    f"Continue your work as normal."
                ),
            })
        turn_messages.extend(tool_turn_messages)
        await self._conversation.append_turn(
            self.id, self.project_id, turn_messages
        )

        # 3. Turn exit gates — validate only; scheduler decides continue/park
        from hiveweave.db import meta as meta_db
        from hiveweave.services.turn_exit import (
            ExitContext,
            collect_unreplied_asks,
            evaluate_turn_exit,
        )
        from hiveweave.services.turn_session import pop_pending_turn_result

        pending_msgs: list[dict] = []
        all_pending: list[dict] = []
        if self.pending_inbox_msg_ids:
            all_pending = await self._inbox.get_pending_messages(self.id)
            id_set = set(self.pending_inbox_msg_ids)
            pending_msgs = [m for m in all_pending if m["id"] in id_set]

        # P1(reply_required 硬门): 本 turn 处理的消息 = 触发携带的 inbox
        # 消息 ∪ turn 开始后到达的未读消息。后者（闹钟/超时唤醒等路径）
        # 此前不参加未回复校验，却会在成功退出时被一并 ACK ——
        # reply_required 消息被静默已读，对方 agent_waits 永远不满足。
        turn_started_ms = int((self.current_job or {}).get("started_at") or 0)
        if turn_started_ms:
            try:
                if not all_pending:
                    all_pending = await self._inbox.get_pending_messages(
                        self.id
                    )
                extra_ids = set(
                    await self._inbox.get_pending_ids_since(
                        self.id, turn_started_ms
                    )
                )
                seen = {m["id"] for m in pending_msgs}
                for m in all_pending:
                    if m["id"] in extra_ids and m["id"] not in seen:
                        pending_msgs.append(m)
                        seen.add(m["id"])
            except Exception as e:
                log.debug("reply_gate_mid_turn_merge_failed", error=str(e))

        name_by_id: dict[str, str] = {}
        exempt_senders: set[str] = set()
        from hiveweave.services.wake_policy import is_user_sender

        for m in pending_msgs:
            fid = m.get("from_agent_id") or ""
            if not fid:
                continue
            ag = await meta_db.get_agent_by_id(fid)
            if fid not in name_by_id:
                name_by_id[fid] = ag.get("name", fid[:8]) if ag else fid[:8]
            # 豁免边界（结构化判定，不猜文案）：
            # - user/system 发送方：回复通道是 assistant 输出本身
            # - 发送方已归档/不存在：回复义务随其消亡，不得死锁退出门禁
            if (
                is_user_sender(fid)
                or fid == "system"
                or ag is None
                or (ag.get("status") or "") == "archived"
            ):
                exempt_senders.add(fid)

        # 本 turn 成功送达的收件人（inbox 落库 = send_message 成功的 DB 证据）
        sent_to: set[str] = set()
        replied_contracts: set[str] = set()
        if turn_started_ms:
            try:
                # TEST10 修复: 判定窗口从「本 turn 开始后」扩展到「最老待回复
                # 消息到达之后」。此前 sent_to / replied_contracts 都只扫本
                # turn —— agent 在上一 turn 回复了 ask（无论是否带 reply_to）
                # 都不算数，合约/回复义务跨 turn 永远关闭不了 → gate 死锁。
                reply_window_ms = turn_started_ms
                expect_ts = [
                    m.get("created_at")
                    for m in pending_msgs
                    if m.get("expect_report")
                ]
                expect_ts = [
                    t for t in expect_ts if isinstance(t, (int, float)) and t
                ]
                if expect_ts:
                    reply_window_ms = min(reply_window_ms, min(expect_ts))
                sent_to = await self._inbox.get_sent_recipients_since(
                    self.id, reply_window_ms
                )
                replied_contracts = await self._inbox.get_replied_contracts_since(
                    self.id, reply_window_ms
                )
            except Exception as e:
                log.debug("reply_gate_sent_lookup_failed", error=str(e))

        unreplied_asks = collect_unreplied_asks(
            pending_msgs,
            tool_calls,
            name_by_id,
            extra_replied_to=sent_to,
            exempt_senders=exempt_senders,
            replied_contracts=replied_contracts,
        )

        # TEST11 #1a: evidence for WAIT_WITHOUT_ASK
        outbound_ask_refs: set[str] = set()
        try:
            outbound_ask_refs = await self._inbox.get_outstanding_ask_recipients(
                self.id
            )
        except Exception as e:
            log.debug("outbound_ask_refs_failed", error=str(e))
        # Enrich messaged_refs with display names for ref matching
        messaged_refs = set(sent_to)
        for aid in list(sent_to):
            if aid in name_by_id:
                messaged_refs.add(name_by_id[aid])
        for aid in list(outbound_ask_refs):
            if aid not in name_by_id:
                try:
                    ag = await meta_db.get_agent_by_id(aid)
                    if ag and ag.get("name"):
                        name_by_id[aid] = ag["name"]
                except Exception:
                    pass
            if aid in name_by_id:
                outbound_ask_refs.add(name_by_id[aid])

        # ── P1 escape valve(TEST10): 连续 N 次被 UNREPLIED_ASKS 阻塞后强制降级 ──
        # 防止 ask 合约因 LLM 不理解 reply_to 参数而永久死锁。
        _ESCAPE_VALVE_THRESHOLD = 5
        if unreplied_asks:
            self._unreplied_asks_streak += 1
            # TEST10 修复: streak 此前只存在内存、随 run 结束清零，跨 run 永远
            # 到不了阈值。这里叠加 DB 证据（近 30 分钟 commit_turn 被
            # UNREPLIED_ASKS 拒绝的累计次数），实现跨 run 累计。
            db_streak = await self._count_recent_ask_gate_rejections()
            effective_streak = max(self._unreplied_asks_streak, db_streak)
            if effective_streak >= _ESCAPE_VALVE_THRESHOLD:
                # 强制标记为已读，解除死锁
                force_ids = [m["id"] for m in unreplied_asks if m.get("id")]
                if force_ids:
                    try:
                        await self._inbox.mark_read_by_ids(self.id, force_ids)
                    except Exception:
                        pass
                log.warning(
                    "unreplied_asks_escape_valve",
                    agent_id=self.id,
                    streak=effective_streak,
                    db_streak=db_streak,
                    force_closed=len(force_ids),
                    senders=[m.get("from_name", "?") for m in unreplied_asks[:5]],
                )
                unreplied_asks = []
                self._unreplied_asks_streak = 0
        else:
            self._unreplied_asks_streak = 0

        open_obligations: list[dict] = []
        try:
            from hiveweave.services.task import TaskService

            open_obligations = await TaskService().get_actionable_obligations(
                self.project_id, self.id
            )
        except Exception as e:
            log.warning(
                "turn_exit_obligations_failed",
                agent_id=self.id,
                error=str(e),
            )

        tasks_advanced = self._task_ids_advanced_this_turn(tool_calls)
        exit_decision = evaluate_turn_exit(
            ExitContext(
                agent_id=self.id,
                project_id=self.project_id,
                tool_calls=tool_calls,
                pending_inbox_msgs=pending_msgs,
                unreplied_asks=unreplied_asks,
                open_task_obligations=open_obligations,
                tasks_advanced=tasks_advanced,
                messaged_refs=messaged_refs,
                outbound_ask_refs=outbound_ask_refs,
                name_by_id=name_by_id,
            )
        )

        gate_retrigger_hint: str | None = None
        continue_slice = False
        carry_inbox_ids: list[str] | None = None

        # Progress fingerprint for no-progress circuit breaker
        fp = self._compute_progress_fingerprint(
            open_obligations, tool_calls, tasks_advanced
        )
        if self._progress_fingerprint == fp:
            self._no_progress_streak += 1
        else:
            self._no_progress_streak = 0
            self._progress_fingerprint = fp

        if not exit_decision.ok:
            unreplied_ids = {m["id"] for m in unreplied_asks}
            if self.pending_inbox_msg_ids:
                no_reply_ids = [
                    mid
                    for mid in self.pending_inbox_msg_ids
                    if mid not in unreplied_ids
                ]
                if no_reply_ids:
                    await self._inbox.mark_read_by_ids(self.id, no_reply_ids)

            if exit_decision.should_park or (
                "OPEN_TASKS_UNDECLARED" in exit_decision.violations
                and not exit_decision.should_repair
            ):
                # Ledger mismatch → park on real books, do not re-run LLM
                pop_pending_turn_result(self.id)
                self._turn_gate_count = 0
                self.disposition = exit_decision.disposition or "runnable"
                if open_obligations:
                    self.disposition = "runnable"
                log.warning(
                    "turn_exit_parked",
                    agent_id=self.id,
                    violations=exit_decision.violations,
                    disposition=self.disposition,
                )
                try:
                    from hiveweave.services.telemetry import telemetry

                    telemetry.turn_exit_gate(
                        self.id,
                        exit_decision.violations,
                        "park",
                        gate_round=self._turn_gate_count,
                    )
                except Exception:
                    pass
                if self.pending_inbox_msg_ids and not unreplied_asks:
                    await self._inbox.mark_read_by_ids(
                        self.id, self.pending_inbox_msg_ids
                    )
                self.pending_inbox_msg_ids = None
            elif (
                exit_decision.should_repair
                and self._turn_gate_count < self._TURN_GATE_MAX
            ):
                self._turn_gate_count += 1
                gate_retrigger_hint = exit_decision.hint
                carry_inbox_ids = list(self.pending_inbox_msg_ids or [])
                # Keep unreplied ask ids for the repair turn
                if unreplied_asks:
                    carry_inbox_ids = list(
                        {*(carry_inbox_ids or []), *(m["id"] for m in unreplied_asks)}
                    )
                # FIX(dup-hint): 不再直接 append_turn — retrigger_for_turn_gate
                # 调用 chat(hint) 时 hint 会作为 user 消息正常保存。之前这里
                # 额外 append 了一次，导致同一条 [TURN EXIT BLOCKED] 在
                # conversation_turns 中出现两份（一份来自这里，一份来自 chat()）。
                log.info(
                    "turn_exit_repair",
                    agent_id=self.id,
                    violations=exit_decision.violations,
                    gate_round=self._turn_gate_count,
                )
                try:
                    from hiveweave.services.telemetry import telemetry

                    telemetry.turn_exit_gate(
                        self.id,
                        exit_decision.violations,
                        "repair",
                        gate_round=self._turn_gate_count,
                    )
                except Exception:
                    pass
                # Do not clear pending_inbox_msg_ids yet — carried into opts
            else:
                if unreplied_asks:
                    await self._escalate_unreplied(unreplied_asks)
                if self.pending_inbox_msg_ids:
                    await self._inbox.mark_read_by_ids(
                        self.id, self.pending_inbox_msg_ids
                    )
                pop_pending_turn_result(self.id)
                self._turn_gate_count = 0
                self._reply_reminder_count = 0
                self.disposition = "blocked"
                log.warning(
                    "turn_exit_gate_exhausted",
                    agent_id=self.id,
                    violations=exit_decision.violations,
                )
                try:
                    from hiveweave.services.telemetry import telemetry

                    telemetry.turn_exit_gate(
                        self.id,
                        exit_decision.violations,
                        "exhausted",
                        gate_round=self._turn_gate_count,
                    )
                except Exception:
                    pass
                self.pending_inbox_msg_ids = None
        else:
            ack_ids: list[str] = list(self.pending_inbox_msg_ids or [])
            # Mid-turn arrivals: unread wake=1 created after this turn started
            try:
                started = int((self.current_job or {}).get("started_at") or 0)
                if started:
                    extra = await self._inbox.get_pending_ids_since(
                        self.id, started
                    )
                    if extra:
                        seen = set(ack_ids)
                        for mid in extra:
                            if mid not in seen:
                                ack_ids.append(mid)
                                seen.add(mid)
            except Exception as e:
                log.debug("mid_turn_ack_lookup_failed", error=str(e))
            if ack_ids:
                await self._inbox.mark_read_by_ids(self.id, ack_ids)
            self.pending_inbox_msg_ids = None
            self._turn_gate_count = 0
            self._reply_reminder_count = 0
            # Do NOT reset _task_reminder_count here — that would defeat the
            # agent.turn.after nudge cap and allow infinite [TASK ADVANCE] loops.
            pop_pending_turn_result(self.id)
            self.disposition = exit_decision.disposition or "runnable"

            # Empty done_slice/waiting streak — consecutive hollow exits park hard (TEST4)
            # Also covers phase="waiting": CEO repeatedly get_tasks→commit_turn(waiting)
            # with no substantive work should be detected as empty, not just done_slice.
            phase = (
                exit_decision.turn_result.phase
                if exit_decision.turn_result
                else None
            )
            if phase in ("done_slice", "waiting") and self._is_empty_done_slice_turn(
                tool_calls
            ):
                self._empty_done_slice_streak += 1
            else:
                self._empty_done_slice_streak = 0

            # P1: persist / clear Wait Contracts from accepted TurnResult
            try:
                from hiveweave.services.wait_contract import wait_contract_service

                tr = exit_decision.turn_result
                if tr and tr.phase in ("waiting", "blocked") and tr.waiting_on:
                    await wait_contract_service.replace_waits(
                        self.project_id,
                        self.id,
                        tr.waiting_on,
                        phase=tr.phase,
                        obligations=open_obligations,
                    )
                else:
                    await wait_contract_service.clear_waits(
                        self.project_id, self.id
                    )
            except Exception as e:
                log.warning(
                    "wait_contract_persist_failed",
                    agent_id=self.id,
                    error=str(e),
                )

            # No-progress fault
            if self._no_progress_streak >= 2 and open_obligations:
                self.disposition = "blocked"
                log.warning(
                    "faulted_no_progress",
                    agent_id=self.id,
                    streak=self._no_progress_streak,
                    fingerprint=fp[:16] if fp else None,
                )
                try:
                    from hiveweave.services.telemetry import telemetry

                    telemetry.agent_no_progress(
                        self.id, streak=self._no_progress_streak
                    )
                except Exception:
                    pass
            elif self._empty_done_slice_streak >= 2:
                # Two hollow done_slices → stay complete, no auto-resume
                self.disposition = "complete"
                continue_slice = False
                log.info(
                    "empty_done_slice_parked",
                    agent_id=self.id,
                    streak=self._empty_done_slice_streak,
                )
            else:
                # At most one more slice if obligations remain AND fingerprint moved
                # and phase was in_progress (declaration only — scheduler decides)
                if (
                    phase == "in_progress"
                    and open_obligations
                    and self._no_progress_streak == 0
                    and self._slice_budget > 0
                ):
                    continue_slice = True
                    self._slice_budget -= 1

            log.info(
                "turn_exit_ok",
                agent_id=self.id,
                phase=phase,
                disposition=self.disposition,
                continue_slice=continue_slice,
                slice_budget=self._slice_budget,
                empty_done_slice_streak=self._empty_done_slice_streak,
            )
            try:
                from hiveweave.services.telemetry import telemetry

                telemetry.turn_exit_gate(
                    self.id,
                    [],
                    "ok",
                    gate_round=self._turn_gate_count,
                )
            except Exception:
                pass

        # 成功完成 → 清除 resume 冷却 + 重置连续错误计数 + 解除 give-up latch
        self._resume_cooldown_until = 0.0
        self._consecutive_errors = 0
        self._stream_timeout_streak = 0
        self._clear_resume_suppressed(reason="turn_ok")

        # 3.5 持久化裁剪旧工具输出（OpenCode prune 模式）
        try:
            await self._conversation.prune_persisted(self.id, self.project_id)
        except Exception as e:
            log.warning("prune_persisted_failed", agent_id=self.id, error=str(e))

        # 4. 状态 → idle (先取消 safety timer，再 reset；残留 streaming 再 finalize 一次)
        self._cancel_safety_timer()
        await self._go_idle()

        # 5. 发送 done 事件（前端 streamChat 等待此事件停止 loading）
        self._broadcast_stream_event({
            "type": "done",
            "content": content,
            "agentId": self.id,
            "disposition": self.disposition,
        })

        # 5.5 广播健康事件 — 成功完成一轮 LLM 调用 → health="ok"
        self._broadcast_agent_health("ok")

        # 6. Process queued user messages (sent while agent was busy)
        await self._drain_message_queue()

        # 7. Lifecycle hook agent.turn.after (task-advance nudge, etc.)
        turn_after_hint: str | None = None
        try:
            from hiveweave.hooks import AGENT_TURN_AFTER, hooks

            hook_out: dict = {"hint": None, "skip_reason": None}
            from hiveweave.services.turn_session import is_task_advance_deferred

            await hooks.run(
                AGENT_TURN_AFTER,
                {
                    "agent_id": self.id,
                    "project_id": self.project_id,
                    "tool_calls": tool_calls,
                    "open_obligations": open_obligations,
                    "tasks_advanced": list(tasks_advanced),
                    "phase": (
                        exit_decision.turn_result.phase
                        if exit_decision.turn_result
                        else None
                    ),
                    "disposition": self.disposition,
                    "exit_ok": exit_decision.ok,
                    "gate_repairing": bool(gate_retrigger_hint),
                    "continue_slice": continue_slice,
                    "deferred": is_task_advance_deferred(self.id),
                    "reminder_count": self._task_reminder_count,
                    "reminder_max": self._TASK_REMINDER_MAX,
                },
                hook_out,
            )
            raw_hint = hook_out.get("hint")
            if isinstance(raw_hint, str) and raw_hint.strip():
                turn_after_hint = raw_hint.strip()
            elif hook_out.get("skip_reason") in (
                "no_obligations",
                "all_advanced",
                "deferred",
            ):
                self._task_reminder_count = 0
        except Exception as e:
            log.warning(
                "agent_turn_after_hook_failed",
                agent_id=self.id,
                error=str(e),
            )

        # 8. Repair once OR one progress slice OR hook nudge — never unlimited
        if gate_retrigger_hint:
            await self._retrigger_for_turn_gate(
                gate_retrigger_hint, inbox_msg_ids=carry_inbox_ids
            )
        elif continue_slice:
            await self._retrigger_for_turn_gate(
                "[TURN CONTINUE] You still have actionable obligations and made "
                "progress this slice. Continue once more, then commit_turn "
                "(prefer waiting/done_slice when idle on the user).",
                inbox_msg_ids=None,
            )
        elif turn_after_hint:
            self._task_reminder_count += 1
            await self._retrigger_for_open_tasks(turn_after_hint)
        else:
            await self._maybe_self_retrigger()

    def _compute_progress_fingerprint(
        self,
        obligations: list[dict],
        tool_calls: list,
        tasks_advanced: set[str],
    ) -> str:
        """Minimal fingerprint: task versions + tool outcome signals."""
        import hashlib

        parts: list[str] = []
        for t in obligations:
            parts.append(f"{t.get('id')}:{t.get('status')}")
        parts.sort()
        replied = any(
            isinstance(tc, dict)
            and ((tc.get("function") or {}).get("name") in (
                "send_message", "ask_agent", "notify_agent", "submit_task",
                "review_task", "claim_task", "write_file", "edit_file", "bash",
            ))
            for tc in (tool_calls or [])
        )
        parts.append(f"adv={','.join(sorted(tasks_advanced))}")
        parts.append(f"replied={int(replied)}")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    @staticmethod
    def _is_empty_done_slice_turn(tool_calls: list) -> bool:
        """True when the turn only had commit_turn (or no tools) — hollow exit."""
        substantive = {
            "submit_task", "review_task", "claim_task", "create_task",
            "hire_agent", "write_file", "edit_file", "bash", "apply_patch",
            "git_worktree_merge", "ask_agent", "send_message", "approve_work",
            "reject_work", "dispatch_task",
        }
        names: set[str] = set()
        for tc in tool_calls or []:
            if not isinstance(tc, dict):
                continue
            n = (tc.get("function") or {}).get("name") or tc.get("name") or ""
            if n:
                names.add(n)
        if not names:
            return True
        return not (names & substantive)

    async def _enrich_hint_with_inbox(self, hint: str) -> str:
        """修 #2: 把未读 inbox 消息摘要拼进 hint，避免 retrigger 时遗漏紧急消息。"""
        try:
            pending = await self._inbox.get_pending_messages(self.id)
        except Exception:
            return hint
        if not pending:
            return hint
        # 构建简洁摘要：发件人 + 消息预览（截断）
        lines: list[str] = []
        for msg in pending[:5]:  # 最多 5 条，避免 hint 过长
            sender = msg.get("from_agent_name") or msg.get("from_agent_id", "?")
            if isinstance(sender, str) and len(sender) > 12:
                sender = sender[:12]
            preview = (msg.get("message") or "")[:80]
            lines.append(f"  - {sender}: {preview}")
        summary = "\n".join(lines)
        return (
            f"{hint}\n\n"
            f"[INBOX] 处理上述事项之前，先查看以下未读消息：\n{summary}"
        )

    async def _retrigger_for_turn_gate(
        self, hint: str, *, inbox_msg_ids: list[str] | None = None
    ) -> None:
        """Re-enter chat with a turn-exit / continue hint; preserve inbox ids."""
        await asyncio.sleep(SELF_RETRIGGER_DELAY_MS / 1000.0)
        if self._in_resume_cooldown():
            return
        if self.status != AgentState.IDLE:
            return
        # 修 #2: retrigger 前查 inbox，把未读消息摘要拼进 hint
        hint = await self._enrich_hint_with_inbox(hint)
        log.info("turn_gate_retrigger", agent_id=self.id)
        opts: dict = {
            "trigger": True,
            "is_background": True,
            "source": "turn_exit_gate",
        }
        if inbox_msg_ids:
            opts["inbox_msg_ids"] = inbox_msg_ids
        asyncio.create_task(
            self.chat(hint, opts=opts),
            name=f"agent-{self.id}-turn-gate-retrigger",
        )

    async def _handle_empty_response(
        self,
        result: dict,
        current_messages: list[dict],
    ) -> bool:
        """空响应处理。

        对齐 Elixir agent.ex:587 handle_info({ref, {:empty, ...}})。

        - retry_count + 1
        - 如果 > MAX_EMPTY_RETRIES: 升级上级，返回 False
        - 否则: 退避 [5s, 15s, 45s]，返回 True（重试）

        Returns:
            True = 重试, False = 已升级上级（退出循环）
        """
        self.empty_retry_count += 1
        retry_count = self.empty_retry_count

        log.warning(
            "empty_response",
            agent_id=self.id,
            retry_count=retry_count,
            tool_calls=len(result.get("tool_calls", [])),
        )

        if retry_count > MAX_EMPTY_RETRIES:
            # 升级上级
            await self._escalate_empty_response()
            return False

        # 退避
        delay_idx = min(retry_count - 1, len(EMPTY_RETRY_DELAYS) - 1)
        delay_s = EMPTY_RETRY_DELAYS[delay_idx] / 1000.0

        log.info(
            "empty_retry_backoff",
            agent_id=self.id,
            retry_count=retry_count,
            delay_s=delay_s,
        )

        await asyncio.sleep(delay_s)
        return True

    async def _escalate_empty_response(self) -> None:
        """空响应超限，升级到上级。

        对齐 Elixir agent.ex:610 escalate_empty/1。

        流程:
        1. 清理 streaming placeholder（防止僵尸消息）
        2. 标记 pending inbox 已读（避免重复触发）
        3. 通知上级 agent
        4. 状态 → idle
        """
        log.warning(
            "empty_escalate",
            agent_id=self.id,
            retry_count=self.empty_retry_count,
            msg="escalating to superior after max empty retries",
        )

        # BUG-038: 清理 streaming placeholder — 其他退出路径都清理了，
        # 但 _escalate_empty_response 曾遗漏，导致 is_streaming=1 僵尸消息
        try:
            await self._finalize_streaming_turn(
                content=(
                    getattr(self, "_streaming_text_acc", "")
                    or "[空响应超限，已升级上级处理]"
                ),
            )
        except Exception as e:
            log.warning("empty_escalate_streaming_cleanup_failed",
                        agent_id=self.id, error=str(e))
        self._streaming_text_acc = ""

        # 标记 inbox 已读
        if self.pending_inbox_msg_ids:
            await self._inbox.mark_read_by_ids(
                self.id, self.pending_inbox_msg_ids
            )
            self.pending_inbox_msg_ids = None

        # 通知上级
        superior = await self._org.get_superior(self.id)
        if superior:
            superior_id = superior["id"]
            agent_name = self.config.get("name", self.id)
            await self._inbox.send_message(
                from_agent_id=self.id,
                to_agent_id=superior_id,
                message=(
                    f"[ESCALATION] Subordinate {agent_name} has produced "
                    f"empty responses {self.empty_retry_count} times. "
                    f"They may be stuck. Please check on them."
                ),
                message_type="escalation",
                priority="urgent",
            )
            # 触发上级
            from hiveweave.agents.trigger import trigger_coordinator

            await trigger_coordinator(superior_id)
        else:
            log.error(
                "empty_escalate_no_superior",
                agent_id=self.id,
                msg="no superior to escalate to",
            )

        self._cancel_safety_timer()
        self._reset_to_idle()

    async def _handle_error(self, error: Exception) -> None:
        """错误处理。

        对齐 Elixir agent.ex:644 handle_info({:DOWN, ref, :process, ...})。

        BUG-032 修复: 先广播 error 事件再 reset_to_idle。之前顺序导致
        前端先收到 status→idle 再收到 error，错误信息可能因 streamDraft
        已被清理而无法展示。参考 OpenCode SSE 错误模式：错误事件作为终止
        信号先于状态变更到达。
        """
        error_msg = str(error)
        error_type = type(error).__name__

        # ── Durable Run Ledger: mark run errored ──
        _run_id = getattr(self, "_current_run_id", None)
        if _run_id:
            try:
                await self._run_ledger.error_run(
                    agent_id=self.id,
                    run_id=_run_id,
                    error_reason=f"{error_type}: {error_msg}",
                )
            except Exception as e:
                log.debug("run_ledger.error_run_failed", error=str(e))

        log.error(
            "llm_error",
            agent_id=self.id,
            error=error_msg,
            error_type=error_type,
        )

        # 写 work_log — 确保错误在监控面板可见
        try:
            await self._work_log.write_work_log(
                self.project_id, self.id, None,
                "error",
                f"[{error_type}] {error_msg}"[:140],
                details={"error_type": error_type, "error": error_msg[:500]},
            )
        except Exception:
            pass

        # 写 agent_events — 监控面板依赖此表
        try:
            from hiveweave.services.event_audit import event_audit
            await event_audit.log(
                project_id=self.project_id,
                agent_id=self.id,
                event_type=f"llm_error.{error_type}",
                payload={"error": error_msg[:500], "error_type": error_type},
            )
        except Exception:
            pass

        # 发送 error 事件（前端 streamChat 等待此事件停止 loading）
        # 必须在 _reset_to_idle 之前发送，确保前端先处理错误再看到 idle 状态
        self._broadcast_stream_event({
            "type": "error",
            "message": error_msg,
            "errorType": error_type,
            "agentId": self.id,
        })

        # 广播健康事件 — LLM 调用出错 → health="error"（message 截断 200 字符）
        self._broadcast_agent_health("error", error_msg[:200])

        # 保存错误消息到 DB — 更新 streaming placeholder 而非插入新消息
        is_trigger = bool(self.pending_inbox_msg_ids)
        try:
            if self._streaming_msg_id:
                await self._finalize_streaming_turn(
                    content=f"[ERROR] {error_msg}",
                )
            else:
                await self._chat_msg.save_message(
                    {
                        "agent_id": self.id,
                        "role": "assistant",
                        "content": f"[ERROR] {error_msg}",
                        "is_streaming": False,
                        "is_background": True if is_trigger else False,
                    }
                )
        except Exception as e:
            log.error("error_save_failed",
                      agent_id=self.id, save_error=str(e))
            try:
                await self._finalize_streaming_turn()
            except Exception:
                pass

        # 连续错误计数 — 超过阈值后 ACK inbox，不再 resume
        # 429 / rate-limit: 不计入放弃；独立长冷却后 resume
        if is_rate_limit_error(error):
            inbox_ids = list(self.pending_inbox_msg_ids or [])
            if inbox_ids:
                await self._write_resume_checkpoint(
                    reason=f"rate_limit:{error_type}",
                    inbox_ids=inbox_ids,
                )
                self.pending_inbox_msg_ids = None
            self._arm_resume_cooldown(RATE_LIMIT_RESUME_COOLDOWN_S)
            log.warning(
                "llm_rate_limit_deferred",
                agent_id=self.id,
                cooldown_s=RATE_LIMIT_RESUME_COOLDOWN_S,
                consecutive_errors=self._consecutive_errors,
                inbox_left_unread=len(inbox_ids),
            )
            self._cancel_safety_timer()
            await self._go_idle()
            return

        self._consecutive_errors += 1
        is_total_timeout = (
            "请求总超时" in error_msg
            or "total timeout" in error_msg.lower()
            or "stream_total_timeout" in error_msg.lower()
        )
        if is_total_timeout:
            self._stream_timeout_streak += 1
            # BUG-8: per-agent streak (park at >=2). Global telemetry count in
            # streamer is process-wide and must not be read as this streak.
            log.warning(
                "stream_timeout_agent_streak",
                agent_id=self.id,
                agent_streak=self._stream_timeout_streak,
                will_park=self._stream_timeout_streak >= 2,
            )
        else:
            self._stream_timeout_streak = 0

        inbox_ids = list(self.pending_inbox_msg_ids or [])

        # TEST4: ≥2 consecutive stream total timeouts → park waiting + escalate
        if is_total_timeout and self._stream_timeout_streak >= 2:
            await self._park_after_stream_timeouts(
                inbox_ids=inbox_ids, error_msg=error_msg
            )
            self._cancel_safety_timer()
            await self._go_idle()
            return

        if inbox_ids and self._consecutive_errors <= self._CONSECUTIVE_ERROR_MAX:
            # 未超阈值: 保留未读，冷却后 resume
            await self._write_resume_checkpoint(
                reason=f"llm_error:{error_type}",
                inbox_ids=inbox_ids,
            )
            self._arm_resume_cooldown(ERROR_RESUME_COOLDOWN_S)
            self.pending_inbox_msg_ids = None
            log.warning(
                "llm_error_inbox_left_unread",
                agent_id=self.id,
                inbox_left_unread=len(inbox_ids),
                cooldown_s=ERROR_RESUME_COOLDOWN_S,
                consecutive_errors=self._consecutive_errors,
            )
        elif inbox_ids and self._consecutive_errors > self._CONSECUTIVE_ERROR_MAX:
            # 超过阈值: 选择性 ACK（保留待审/升级类）+ 账本再挂 + 升级上级
            self._arm_resume_suppressed()
            try:
                await self._ack_inbox_on_give_up(inbox_ids)
            except Exception as ack_err:
                log.error("inbox_ack_failed", agent_id=self.id, error=str(ack_err))
            self.pending_inbox_msg_ids = None
            await self._escalate_turn_interruption(reason=f"llm_error:{error_type}")
        elif self._consecutive_errors > self._CONSECUTIVE_ERROR_MAX:
            self._arm_resume_suppressed()
            try:
                await self._inject_ledger_review_wake()
            except Exception as e:
                log.debug("ledger_rewake_on_give_up_failed", error=str(e))
            await self._escalate_turn_interruption(reason=f"llm_error:{error_type}")

        self._cancel_safety_timer()
        await self._go_idle()

    async def _count_recent_ask_gate_rejections(self, window_ms: int = 30 * 60 * 1000) -> int:
        """Count recent commit_turn rejections caused by UNREPLIED_ASKS (DB evidence).

        The in-memory ``_unreplied_asks_streak`` resets every run, so the escape
        valve could never reach its threshold across runs. This queries the
        durable run ledger for failed commit_turn steps whose error mentions
        unreplied asks within ``window_ms``, giving a cross-run cumulative
        streak. Best-effort: returns 0 on any failure.
        """
        try:
            from hiveweave.db import project as project_db

            since_ms = int(time.time() * 1000) - window_ms
            rows = await project_db.query(
                self.id,
                "SELECT COUNT(*) AS c FROM run_steps rs "
                "JOIN agent_runs ar ON ar.id = rs.run_id "
                "WHERE ar.agent_id = ? AND rs.tool_name = 'commit_turn' "
                "AND rs.status = 'failed' "
                "AND rs.error LIKE '%有未回复的 ask%' "
                "AND rs.started_at >= ?",
                [self.id, since_ms],
            )
            if rows:
                return int(rows[0]["c"] or 0)
        except Exception as e:
            log.debug("ask_gate_rejection_count_failed", error=str(e))
        return 0

    async def _park_after_stream_timeouts(
        self, *, inbox_ids: list[str], error_msg: str
    ) -> None:
        """After consecutive stream total timeouts: park waiting + wake parent.

        Does not auto-approve. Structured escalation lists pending submitted
        tasks so the superior can review/merge (TEST4 tech-lead SPOF).
        """
        from hiveweave.services.turn_result import WaitingOnItem
        from hiveweave.services.wait_contract import wait_contract_service

        self.disposition = "waiting_agent"
        self._arm_resume_suppressed()
        self._stream_timeout_streak = 0

        pending_task_ids: list[str] = []
        try:
            from hiveweave.services.task import TaskService

            tasks = await TaskService().list_tasks(self.project_id)
            for t in tasks or []:
                if t.get("status") not in ("submitted", "reviewing"):
                    continue
                # Creator or any coordinator reviewing — include if we own review
                if (
                    t.get("created_by") == self.id
                    or t.get("creator_id") == self.id
                    or t.get("reviewer_id") == self.id
                ):
                    pending_task_ids.append(str(t.get("id") or "")[:12])
        except Exception as e:
            log.warning(
                "stream_timeout_park_tasks_failed",
                agent_id=self.id,
                error=str(e),
            )

        try:
            await wait_contract_service.replace_waits(
                self.project_id,
                self.id,
                [
                    WaitingOnItem(
                        kind="timer",
                        ref="stream_total_timeout_recovery",
                        note="Parked after consecutive stream total timeouts",
                    )
                ],
                phase="waiting",
            )
        except Exception as e:
            log.warning(
                "stream_timeout_wait_persist_failed",
                agent_id=self.id,
                error=str(e),
            )

        if inbox_ids:
            try:
                await self._inbox.mark_read_by_ids(self.id, inbox_ids)
            except Exception:
                pass
        self.pending_inbox_msg_ids = None

        agent_name = self.config.get("name", self.id)
        task_blob = ", ".join(pending_task_ids) or "(none listed)"
        try:
            superior = await self._org.get_superior(self.id)
            if superior:
                await self._inbox.send_message(
                    from_agent_id=self.id,
                    to_agent_id=superior["id"],
                    message=(
                        f"[tech_lead_incapacitated] {agent_name} hit consecutive "
                        f"stream total timeouts and is parked waiting. "
                        f"Pending review taskIds: {task_blob}. "
                        f"Please review/merge if appropriate (do not assume "
                        f"auto-approve). Last error: {error_msg[:120]}"
                    ),
                    message_type="escalation",
                    priority="urgent",
                )
                from hiveweave.agents.trigger import trigger_coordinator

                await trigger_coordinator(superior["id"])
                log.warning(
                    "stream_timeout_parked_escalated",
                    agent_id=self.id,
                    superior_id=superior["id"],
                    pending_tasks=pending_task_ids,
                )
            else:
                log.warning(
                    "stream_timeout_parked_no_superior",
                    agent_id=self.id,
                )
        except Exception as e:
            log.error(
                "stream_timeout_escalate_failed",
                agent_id=self.id,
                error=str(e),
            )

    # ── 内部: thinking 心跳 ──────────────────────────────────

    _HEARTBEAT_INTERVAL_S: float = 5.0
    """心跳间隔（秒）。每 5 秒发一次 thinking 事件，让前端知道 agent 还在工作。"""

    def _start_heartbeat(self) -> None:
        """启动 thinking 心跳任务。

        在 LLM 调用期间周期性广播 thinking 事件，让前端在 agent
        长时间无输出（thinking 模式/多轮工具调用）时不至于"看似卡死"。
        首个 text_delta/thinking_delta/tool_call_start 事件会停止心跳。
        """
        if self._heartbeat_task is not None:
            return
        self._heartbeat_active = True
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"agent-{self.id}-heartbeat"
        )

    async def _heartbeat_loop(self) -> None:
        """心跳循环 — 每 N 秒广播一次 thinking 事件。"""
        elapsed = 0.0
        try:
            while self._heartbeat_active:
                await asyncio.sleep(self._HEARTBEAT_INTERVAL_S)
                if not self._heartbeat_active:
                    break
                elapsed += self._HEARTBEAT_INTERVAL_S
                self._broadcast_stream_event({
                    "type": "thinking",
                    "elapsed_s": elapsed,
                    "agentId": self.id,
                })
        except asyncio.CancelledError:
            pass

    def _stop_heartbeat(self) -> None:
        """停止 thinking 心跳任务。"""
        self._heartbeat_active = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    # ── 内部: 安全超时 ────────────────────────────────────────

    def _start_safety_timer(self) -> None:
        """启动 10 分钟安全超时。

        对齐 Elixir agent.ex:664 schedule_safety_timer/1。
        """
        self._cancel_safety_timer()
        loop = asyncio.get_event_loop()
        self._safety_timer = loop.call_later(
            SAFETY_TIMEOUT_MS / 1000.0,
            self._on_safety_timeout_sync,
        )

    def _cancel_safety_timer(self) -> None:
        """取消安全定时器。"""
        if self._safety_timer is not None:
            self._safety_timer.cancel()
            self._safety_timer = None

    def _on_safety_timeout_sync(self) -> None:
        """安全超时同步回调（由事件循环 call_later 调用）。

        只做最小操作：设置取消原因 + 取消 LLM task。
        异步清理在 _run_llm 的 CancelledError handler 中执行。
        """
        log.warning(
            "safety_timeout",
            agent_id=self.id,
            msg="10min safety timeout reached, force cancelling LLM task",
        )
        self._cancel_reason = "safety_timeout"
        if self._llm_task and not self._llm_task.done():
            self._llm_task.cancel()

    async def _handle_safety_timeout(self) -> None:
        """安全超时异步清理。

        与 _handle_error 统一的非致命中断策略（计数 + 冷却 resume + 超限放弃）:
        - 未超限: 不 ACK inbox（消息保持未读）+ RESUME CHECKPOINT + 冷却 resume
        - 连续超限: ACK inbox 放弃本轮 + 升级上级一次 —— 堵住
          「10min 超时 → 90s 冷却 → 再超时」的无限死循环，且不再注入
          CHECKPOINT 撑大上下文让下一轮更易超时
        - 记录 work_log，便于监控/stall watchdog 关联
        """
        inbox_ids = list(self.pending_inbox_msg_ids or [])

        # ── Durable Run Ledger: mark run interrupted ──
        _run_id = getattr(self, "_current_run_id", None)
        if _run_id:
            try:
                await self._run_ledger.interrupt_run(
                    agent_id=self.id,
                    run_id=_run_id,
                    reason="safety_timeout",
                )
            except Exception as e:
                log.debug("run_ledger.interrupt_run_failed", error=str(e))

        # 连续中断计数 — 与 _handle_error 共用同一阈值
        self._consecutive_errors += 1
        give_up = self._consecutive_errors > self._CONSECUTIVE_ERROR_MAX

        timeout_msg = (
            "[TIMEOUT] LLM call exceeded 10 minute safety limit. "
            + (
                f"Gave up after {self._consecutive_errors} consecutive "
                "interrupted turns; escalated to superior."
                if give_up
                else "Inbox left unread for resume after cooldown."
            )
        )
        if self._streaming_msg_id:
            await self._finalize_streaming_turn(content=timeout_msg)
        else:
            await self._chat_msg.update_streaming_messages_done(self.id)
            await self._chat_msg.save_message(
                {
                    "agent_id": self.id,
                    "role": "assistant",
                    "content": timeout_msg,
                    "is_streaming": False,
                }
            )

        if not give_up:
            # 未超阈值: 保留未读 + CHECKPOINT + 冷却，watcher 冷却后恢复信息链
            await self._write_resume_checkpoint(
                reason="safety_timeout",
                inbox_ids=inbox_ids,
            )
            self._arm_resume_cooldown(TIMEOUT_RESUME_COOLDOWN_S)
        else:
            # 超阈值放弃: 选择性 ACK + 账本再挂 + 升级上级
            self._arm_resume_suppressed()
            if inbox_ids:
                try:
                    await self._ack_inbox_on_give_up(inbox_ids)
                except Exception as ack_err:
                    log.error("inbox_ack_failed", agent_id=self.id, error=str(ack_err))
            else:
                try:
                    await self._inject_ledger_review_wake()
                except Exception as e:
                    log.debug("ledger_rewake_on_timeout_failed", error=str(e))
            try:
                await self._work_log.write_work_log(
                    self.project_id, self.id, None,
                    "error",
                    f"[safety_timeout] gave up after {self._consecutive_errors} "
                    "consecutive interrupted turns; non-critical inbox ACKed, "
                    "review-critical kept; escalated",
                    details={
                        "reason": "safety_timeout",
                        "inbox_ids": inbox_ids[:20],
                        "consecutive_errors": self._consecutive_errors,
                        "resume": False,
                    },
                )
            except Exception:
                pass
            await self._escalate_turn_interruption(reason="safety_timeout")

        # Keep pending_inbox_msg_ids cleared from this turn; 未放弃时消息
        # 在 DB 保持未读，冷却结束后由 watcher 恢复信息链。
        self.pending_inbox_msg_ids = None

        self._cancel_safety_timer()
        await self._go_idle()

        # 广播健康事件 — LLM 调用 10 分钟安全超时 → health="error"
        self._broadcast_agent_health(
            "error", "LLM call exceeded 10 minute safety limit"
        )
        log.warning(
            "safety_timeout_gave_up" if give_up else "safety_timeout_resume_armed",
            agent_id=self.id,
            inbox_left_unread=0 if give_up else len(inbox_ids),
            inbox_acked=len(inbox_ids) if give_up else 0,
            consecutive_errors=self._consecutive_errors,
            cooldown_s=0.0 if give_up else TIMEOUT_RESUME_COOLDOWN_S,
        )

    async def _ack_inbox_on_give_up(self, inbox_ids: list[str]) -> None:
        """ACK noisy inbox on give-up but keep review/escalation/ask wakes.

        Also inject a ledger [LEDGER REVIEW] wake when this agent still owns
        submitted/reviewing tasks as creator — so CREATOR_MUST_REVIEW survives
        even if the original [TASK SUBMITTED] rows were somehow lost.
        """
        to_ack, to_spare = await self._inbox.partition_give_up_ack(
            self.id, list(inbox_ids or [])
        )
        if to_ack:
            await self._inbox.mark_read_by_ids(self.id, to_ack)
        if to_spare:
            await self._inbox.ensure_wake(self.id, to_spare)
        log.warning(
            "llm_error_inbox_selective_ack",
            agent_id=self.id,
            inbox_acked=len(to_ack),
            inbox_spared=len(to_spare),
            consecutive_errors=self._consecutive_errors,
        )
        await self._inject_ledger_review_wake()

    async def _inject_ledger_review_wake(self) -> None:
        """If creator still has review/merge duties, force a task-class wake."""
        try:
            from hiveweave.services.task import TaskService

            obl = await TaskService().get_actionable_obligations(
                self.project_id, self.id
            )
        except Exception as e:
            log.debug("ledger_obligations_lookup_failed", error=str(e))
            return
        review = [
            t
            for t in (obl or [])
            if t.get("role_hint") == "creator"
            and t.get("status") in ("submitted", "reviewing")
        ]
        merge = [
            t
            for t in (obl or [])
            if t.get("role_hint") == "creator" and t.get("status") == "approved"
        ]
        if not review and not merge:
            return

        async def _send(body: str, task_id: str | None, kind: str) -> None:
            try:
                await self._inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=self.id,
                    message=body,
                    message_type="task",
                    priority="urgent",
                    task_id=task_id,
                    wake=True,
                )
                log.info(
                    f"ledger_{kind}_wake_injected",
                    agent_id=self.id,
                    count=(
                        len(review) if kind == "review" else len(merge)
                    ),
                )
            except Exception as e:
                log.warning(
                    f"ledger_{kind}_wake_failed",
                    agent_id=self.id,
                    error=str(e),
                )

        if review:
            lines = []
            for t in review[:8]:
                tid = str(t.get("id") or "")
                title = (t.get("title") or "(untitled)").split("\n")[0][:50]
                lines.append(f"  - {tid[:8]} [{t.get('status')}] {title}")
            extra = len(review) - len(lines)
            body = (
                f"[LEDGER REVIEW] You still have {len(review)} task(s) awaiting "
                f"review_task (status submitted/reviewing). Do not ignore the ledger "
                f"after an LLM error. Use review_task(taskId, decision):\n"
                + "\n".join(lines)
                + (f"\n  - …and {extra} more" if extra > 0 else "")
            )
            await _send(body, str(review[0].get("id") or "") or None, "review")

        if merge:
            lines = []
            for t in merge[:8]:
                tid = str(t.get("id") or "")
                title = (t.get("title") or "(untitled)").split("\n")[0][:50]
                lines.append(f"  - {tid[:8]} [approved] {title}")
            extra = len(merge) - len(lines)
            body = (
                f"[MERGE PENDING] You still have {len(merge)} approved task(s) "
                f"awaiting git_worktree_merge. Do not leave worktree-only. "
                f"Call git_worktree_merge(branchName=shortId or hw/...):\n"
                + "\n".join(lines)
                + (f"\n  - …and {extra} more" if extra > 0 else "")
            )
            await _send(body, str(merge[0].get("id") or "") or None, "merge")
            # Give-up + approved: escalate MERGE PROXY to parent with MERGE
            try:
                from hiveweave.services.merge_proxy import escalate_merge_proxy

                for t in merge[:5]:
                    await escalate_merge_proxy(
                        self.project_id,
                        t,
                        reason="creator_give_up",
                        trigger=True,
                    )
            except Exception as e:
                log.debug(
                    "merge_proxy_on_give_up_failed",
                    agent_id=self.id,
                    error=str(e),
                )

    async def _escalate_turn_interruption(self, *, reason: str) -> None:
        """连续中断超限，给上级发一次升级消息。

        每个失败 streak 只升级一次（计数恰好越限的那次）——后续连续失败仍
        选择性 ACK inbox 止血，但不重复打扰上级，避免「升级 → 上级追问 → 再失败 →
        再升级」的跨 agent 振荡。成功后计数归零，新的 streak 会再次升级。
        best-effort：升级失败只记日志，不阻断清理流程。
        """
        if self._consecutive_errors != self._CONSECUTIVE_ERROR_MAX + 1:
            return
        try:
            superior = await self._org.get_superior(self.id)
            if superior:
                agent_name = self.config.get("name", self.id)
                await self._inbox.send_message(
                    from_agent_id=self.id,
                    to_agent_id=superior["id"],
                    message=(
                        f"[ESCALATION] Subordinate {agent_name} gave up a turn "
                        f"after {self._consecutive_errors} consecutive "
                        f"interruptions (last: {reason}). Non-critical inbox "
                        f"was ACKed; review-critical / ask messages were kept. "
                        f"Please check on them and any submitted tasks."
                    ),
                    message_type="escalation",
                    priority="urgent",
                    wake=True,
                )
                # 触发上级
                from hiveweave.agents.trigger import trigger_coordinator

                await trigger_coordinator(superior["id"])
                log.warning(
                    "interruption_escalated",
                    agent_id=self.id,
                    superior_id=superior["id"],
                    reason=reason,
                    consecutive_errors=self._consecutive_errors,
                )
            else:
                log.warning(
                    "interruption_escalate_no_superior",
                    agent_id=self.id,
                    reason=reason,
                    msg="no superior to escalate to",
                )
        except Exception as e:
            log.error(
                "interruption_escalate_failed", agent_id=self.id, error=str(e)
            )

    async def _handle_cancel(self) -> None:
        """用户取消处理。

        对齐 Elixir agent.ex:131 handle_cast(:cancel)。
        """
        # ── Durable Run Ledger: mark run interrupted ──
        _run_id = getattr(self, "_current_run_id", None)
        if _run_id:
            try:
                await self._run_ledger.interrupt_run(
                    agent_id=self.id,
                    run_id=_run_id,
                    reason="cancelled_by_user",
                )
            except Exception as e:
                log.debug("run_ledger.interrupt_cancel_failed", error=str(e))

        await self._finalize_streaming_turn(content="[对话被中断]")

        # 保留 inbox 未读（用户取消不应标记已读）
        # pending_inbox_msg_ids 保持不变，下次 trigger 可重新处理

        self._cancel_safety_timer()
        self._reset_to_idle()

    # ── 内部: expect_reply 回复检查 ──────────────────────────

    async def _check_unreplied_expect_report(
        self, tool_calls: list
    ) -> list[dict]:
        """检查这轮处理的 inbox 消息中，是否有 expect_report=1 但没被 send_message 回复的。

        精准检测：A 收到 B/C/D 要求回复的消息，只回复了 B → 仍返回 C/D 的消息。
        不再因为"调了 send_message"就认为全部已回复。

        返回需要回复的消息列表（空列表 = 不需要提示）。
        """
        if not self.pending_inbox_msg_ids:
            return []

        pending = await self._inbox.get_pending_messages(self.id)
        msg_ids_set = set(self.pending_inbox_msg_ids)
        target_msgs = [m for m in pending if m["id"] in msg_ids_set]

        # Structural only: expect_report (ask-chain downgrade clears it)
        expect_reply_msgs = [
            m
            for m in target_msgs
            if m.get("expect_report")
        ]
        if not expect_reply_msgs:
            return []

        # 批量解析 from_agent_id → name（避免 N+1 查询）
        from_ids = {m.get("from_agent_id", "") for m in expect_reply_msgs if m.get("from_agent_id")}
        name_map: dict[str, str] = {}
        for fid in from_ids:
            agent = await meta_db.get_agent_by_id(fid)
            if agent:
                name_map[fid] = agent.get("name", fid[:8])
        for m in expect_reply_msgs:
            fid = m.get("from_agent_id", "")
            m["from_name"] = name_map.get(fid, fid[:8])

        # 从 tool_calls 中提取 send_message 的实际收件人
        replied_to: set[str] = set()
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            if func.get("name") != "send_message":
                continue
            raw_args = func.get("arguments", "")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    continue
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                continue
            recipients = args.get("recipients") or args.get("to") or []
            if isinstance(recipients, str):
                recipients = [recipients]
            replied_to.update(recipients)

        # 如果没有调 send_message，全部未回复
        if not replied_to:
            return expect_reply_msgs

        # 精准过滤：只保留 A 没有回复的消息
        unreplied: list[dict] = []
        for m in expect_reply_msgs:
            from_id = m.get("from_agent_id", "")
            from_name = m.get("from_name", "")
            if from_id in replied_to or (from_name and from_name in replied_to):
                continue  # 已回复
            unreplied.append(m)

        return unreplied

    async def _build_reply_hint(self, unreplied_msgs: list[dict]) -> str:
        """构建精准回复提示：列出已回复和未回复的人。

        场景: A 收到 B/C/D/E 消息，B/C/D 要求回复，A 只回复了 B。
        提示应明确告诉 A："你已回复 B，但 C/D 仍未回复"。
        """
        # unreplied_msgs 已带有 from_name（由 _check_unreplied_expect_report 解析）
        unreplied_details: list[str] = []
        unreplied_names: list[str] = []
        for m in unreplied_msgs:
            name = m.get("from_name") or m.get("from_agent_id", "?")[:8]
            unreplied_names.append(name)
            preview = (m.get("message") or "")[:60]
            unreplied_details.append(f"  ❌ {name}：{preview}")

        # 查找本轮已回复的人
        replied_names: list[str] = []
        if self.pending_inbox_msg_ids:
            pending = await self._inbox.get_pending_messages(self.id)
            unreplied_ids = {m["id"] for m in unreplied_msgs}
            # 需要解析名字的 from_id 集合
            replied_from_ids = [
                m.get("from_agent_id", "") for m in pending
                if m["id"] in self.pending_inbox_msg_ids
                and m["id"] not in unreplied_ids
                and (
                    m.get("expect_report")
                )
            ]
            # 批量解析
            for fid in replied_from_ids:
                agent = await meta_db.get_agent_by_id(fid)
                name = agent.get("name", fid[:8]) if agent else fid[:8]
                replied_names.append(name)

        round_num = self._reply_reminder_count + 1
        parts = [f"[REPLY REQUIRED] (第 {round_num} 次提醒)"]

        if replied_names:
            parts.append(f"你已回复：{', '.join(replied_names)} ✅")
            parts.append(f"但以下 {len(unreplied_names)} 人仍在等待你的回复：")
        else:
            parts.append(f"以下 {len(unreplied_names)} 人标记了需要回复，但你上一轮没有调用 send_message：")

        parts.extend(unreplied_details)
        parts.append(
            "你的文字输出其他 agent 看不到。"
            "请立即调用 send_message(recipients=['花名'], message='...') "
            "回复上述每一个人。注意：回复给其他人不算回复给这些人。"
        )
        return "\n".join(parts)

    async def _escalate_unreplied(self, unreplied_msgs: list[dict]) -> None:
        """达到提醒上限后，升级到上级。

        给上级发消息，列出下属未回复的人员和消息。
        """
        # 获取自己的 name 和 parent_id
        me = await meta_db.get_agent_by_id(self.id)
        my_name = me.get("name", self.id[:8]) if me else self.id[:8]
        parent_id = me.get("parent_id") if me else None

        if not parent_id:
            log.warning("escalate_no_parent",
                        agent_id=self.id, name=my_name,
                        unreplied_count=len(unreplied_msgs))
            return

        # unreplied_msgs 已带有 from_name（由 _check_unreplied_expect_report 解析）
        lines = []
        for m in unreplied_msgs:
            from_name = m.get("from_name") or m.get("from_agent_id", "?")[:8]
            preview = (m.get("message") or "")[:60]
            lines.append(f"  - {from_name}：{preview}")

        msg = (
            f"[ESCALATION] 你的下属 {my_name} 经过 {self._REPLY_REMINDER_MAX} 次提醒后，"
            f"仍未回复以下 {len(unreplied_msgs)} 人的消息，请直接介入协调：\n"
            + "\n".join(lines)
        )

        try:
            from hiveweave.services.inbox import InboxService
            await InboxService().send_message(
                "system", parent_id, msg,
                message_type="system", priority="urgent")
            from hiveweave.agents.trigger import trigger_subordinate
            await trigger_subordinate(parent_id)
            log.warning("reply_escalated",
                        agent_id=self.id, name=my_name,
                        parent_id=parent_id,
                        unreplied_count=len(unreplied_msgs))
        except Exception as e:
            log.error("escalate_failed", agent_id=self.id, error=str(e))

    # ── 内部: open-task 收工提醒 ─────────────────────────────

    @staticmethod
    def _tool_call_name(tc: dict) -> str:
        if not isinstance(tc, dict):
            return ""
        func = tc.get("function") or {}
        if isinstance(func, dict) and func.get("name"):
            return str(func["name"])
        return str(tc.get("name") or "")

    @staticmethod
    def _tool_call_args(tc: dict) -> dict:
        if not isinstance(tc, dict):
            return {}
        func = tc.get("function") or {}
        raw = func.get("arguments") if isinstance(func, dict) else tc.get("arguments")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def _task_ids_advanced_this_turn(self, tool_calls: list) -> set[str]:
        """Task IDs that this turn already progressed (submit/review/block)."""
        advanced: set[str] = set()
        for tc in tool_calls or []:
            name = self._tool_call_name(tc)
            args = self._tool_call_args(tc)
            tid = args.get("taskId") or args.get("task_id") or args.get("id")
            if not tid:
                continue
            tid = str(tid)
            if name == "submit_task":
                advanced.add(tid)
            elif name == "review_task":
                advanced.add(tid)
            elif name in ("claim_task", "dispatch_task", "close_task"):
                advanced.add(tid)
            elif name == "update_task_status":
                status = str(args.get("status") or "running").lower()
                if status in ("blocked", "running", "claimed"):
                    advanced.add(tid)
            elif name == "update_progress":
                advanced.add(tid)
        return advanced

    def _build_open_task_hint(self, obligations: list[dict]) -> str:
        """Deprecated wrapper — hint text lives in hooks.handlers.task_advance."""
        from hiveweave.hooks.handlers.task_advance import build_task_advance_hint

        return build_task_advance_hint(obligations)

    async def _retrigger_for_open_tasks(self, hint: str) -> None:
        """Chat with [TASK ADVANCE] hint from agent.turn.after hook."""
        await asyncio.sleep(SELF_RETRIGGER_DELAY_MS / 1000.0)
        if self._in_resume_cooldown():
            return
        if self.status != AgentState.IDLE:
            return
        # Dogfood 2026-07-24: complete agents were still woken into
        # open_task_reminder → stall burn after ledger close.
        if self.disposition == "complete":
            log.info(
                "open_task_retrigger_skip_complete",
                agent_id=self.id,
            )
            return
        # 修 #2: retrigger 前查 inbox，把未读消息摘要拼进 hint
        hint = await self._enrich_hint_with_inbox(hint)
        log.info("open_task_retrigger", agent_id=self.id)
        asyncio.create_task(
            self.chat(
                hint,
                opts={
                    "trigger": True,
                    "is_background": True,
                    "source": "open_task_reminder",
                },
            ),
            name=f"agent-{self.id}-open-task-retrigger",
        )

    # ── 内部: 自检 re-trigger ────────────────────────────────

    async def _maybe_self_retrigger(self) -> None:
        """自检 re-trigger。

        对齐 Elixir agent.ex:890 maybe_self_retrigger/1。

        检查:
        1. 是否有未读 inbox 消息 → trigger
        2. 是否有未回答的用户消息 → trigger
        """
        await asyncio.sleep(SELF_RETRIGGER_DELAY_MS / 1000.0)

        if self._resume_suppressed:
            # 被动衰减检查：30min 过期则清除锁存器，继续自检
            if self.try_clear_resume_suppressed(opts={"trigger": True}):
                log.info("self_retrigger_suppressed_skip", agent_id=self.id)
                return

        if self.disposition == "complete":
            log.info("self_retrigger_complete_skip", agent_id=self.id)
            return

        if self._in_resume_cooldown():
            log.info(
                "self_retrigger_cooldown_skip",
                agent_id=self.id,
                cooldown_remaining_s=round(
                    self._resume_cooldown_until - time.monotonic(), 1
                ),
            )
            return

        # 检查未读 inbox
        pending = await self._inbox.get_pending_messages(self.id)
        if pending:
            log.info(
                "self_retrigger_inbox",
                agent_id=self.id,
                pending_count=len(pending),
            )
            from hiveweave.agents.trigger import trigger_subordinate

            await trigger_subordinate(self.id)
            return

        # 检查未回答的用户消息
        has_unanswered = await self._chat_msg.has_unanswered_user_messages(
            self.id
        )
        if has_unanswered:
            log.info(
                "self_retrigger_unanswered",
                agent_id=self.id,
            )
            from hiveweave.agents.trigger import trigger_subordinate

            await trigger_subordinate(self.id)

    # ── 内部: 超时/错误恢复 ──────────────────────────────────

    def _in_resume_cooldown(self) -> bool:
        """Whether this agent is inside a post-timeout/error resume cooldown."""
        return time.monotonic() < self._resume_cooldown_until

    def _arm_resume_cooldown(self, seconds: float) -> None:
        """Arm cooldown so inbox watcher won't immediately re-fire.

        Adds ±25% jitter so concurrent agents don't resurrect in lockstep
        and stampede the LLM semaphore (TEST3 timeout storms).
        """
        base = max(0.0, seconds)
        if base > 0:
            jitter = base * 0.25
            base = base + random.uniform(-jitter, jitter)
            base = max(1.0, base)
        self._resume_cooldown_until = time.monotonic() + base

    async def _write_resume_checkpoint(
        self, *, reason: str, inbox_ids: list[str]
    ) -> None:
        """Store an ephemeral resume hint for the next LLM turn only.

        Does **not** append into conversation history (avoids checkpoint bloat).
        Next ``_build_messages`` injects the hint once, then clears it.
        Work_log retains an ops trail.
        """
        now_ms = int(time.time() * 1000)
        ids_preview = ", ".join(inbox_ids[:8]) if inbox_ids else "(none)"
        checkpoint = (
            "[RESUME CHECKPOINT]\n"
            f"reason: {reason}\n"
            f"timeout_at_ms: {now_ms}\n"
            f"pending_inbox_ids: {ids_preview}\n"
            "Instruction: Your previous turn did not finish. Continue the "
            "unfinished work from where you left off. Do NOT restart from "
            "scratch if partial progress exists. Call get_tasks to locate any "
            "running/claimed/rework tasks and resume them.\n"
            "IMPORTANT: For tasks already in 'running' status, DO NOT call "
            "claim_task or update_task_status again — just continue coding "
            "and call submit_task when done."
        )
        self._pending_resume_hint = checkpoint
        try:
            await self._work_log.write_work_log(
                self.project_id,
                self.id,
                None,
                "error",
                f"[{reason}] turn interrupted; inbox left unread for resume",
                details={
                    "reason": reason,
                    "inbox_ids": inbox_ids[:20],
                    "resume": True,
                },
            )
        except Exception as e:
            log.warning(
                "resume_checkpoint_work_log_failed",
                agent_id=self.id,
                error=str(e),
            )

    async def _go_idle(self) -> None:
        """Finalize any leftover streaming placeholder, then reset to idle."""
        if self._streaming_msg_id:
            try:
                await self._finalize_streaming_turn(
                    allow_agent_wide_fallback=True
                )
            except Exception as e:
                log.warning(
                    "go_idle_finalize_failed",
                    agent_id=self.id,
                    error=str(e),
                )
        try:
            await self._org.touch_last_active(self.id)
        except Exception as e:
            log.warning(
                "touch_last_active_failed",
                agent_id=self.id,
                error=str(e),
            )
        self._reset_to_idle()

    # ── 内部: 状态管理 ────────────────────────────────────────

    async def enqueue_wake(
        self, message: str, opts: dict | None = None
    ) -> dict:
        """Enqueue a wake while busy (P1 single-flight). Does not start LLM."""
        opts = opts or {}
        if self.try_clear_resume_suppressed(opts) and opts.get("trigger"):
            log.info(
                "enqueue_wake_suppressed_gave_up",
                agent_id=self.id,
                source=opts.get("source") or "trigger",
            )
            return {"ok": True, "suppressed": True}
        async with self._lock:
            # 与 chat() 同理：busy 期间的 trigger wake 也算一次激活信号
            self._ensure_watcher_alive()
            self._message_queue.append(
                (message, opts, int(time.time() * 1000))
            )
            log.info(
                "wake_enqueued",
                agent_id=self.id,
                queue_len=len(self._message_queue),
                trigger=bool(opts.get("trigger")),
                preview=(message or "")[:80],
            )
            return {"ok": True, "queued": True}

    async def _drain_message_queue(self) -> None:
        """Process queued wakes with a short merge window (P1).

        Trigger/watcher wakes coalesce into one turn; user messages stay FIFO.
        """
        if not self._message_queue:
            return
        # Merge window: let near-simultaneous triggers pile up
        await asyncio.sleep(self._MERGE_WINDOW_MS / 1000.0)
        if not self._message_queue:
            return

        batch = list(self._message_queue)
        self._message_queue.clear()

        triggers = [(m, o, t) for m, o, t in batch if (o or {}).get("trigger")]
        users = [(m, o, t) for m, o, t in batch if not (o or {}).get("trigger")]

        if triggers:
            inbox_ids: list[str] = []
            for _m, o, _t in triggers:
                for mid in (o or {}).get("inbox_msg_ids") or []:
                    if mid not in inbox_ids:
                        inbox_ids.append(mid)
            message = triggers[-1][0]
            opts = dict(triggers[-1][1] or {})
            opts["inbox_msg_ids"] = inbox_ids
            opts["merged_wakes"] = len(triggers)
            opts.setdefault("source", "merged_trigger")
            # User messages wait behind the coalesced trigger
            self._message_queue.extend(users)
            log.info(
                "wake_merged",
                agent_id=self.id,
                merged=len(triggers),
                inbox_ids=len(inbox_ids),
                users_queued=len(users),
            )
            await self.chat(message, opts)
            return

        if users:
            message, opts, _ts = users[0]
            self._message_queue.extend(users[1:])
            log.info(
                "chat_dequeued",
                agent_id=self.id,
                remaining=len(self._message_queue),
                preview=message[:80],
            )
            await self.chat(message, opts)

    async def _finalize_streaming_turn(
        self,
        *,
        msg_id: str | None = None,
        content: str | None = None,
        thinking: object | None = None,
        tool_calls_json: str | None = None,
        allow_agent_wide_fallback: bool = True,
    ) -> bool:
        """Close this turn's streaming placeholder — never leave a DB orphan.

        ``update_message`` can return False without raising (no DB / no row).
        Callers used to clear ``_streaming_msg_id`` anyway → true orphans.
        This helper only drops the in-memory pointer after a confirmed clear.
        """
        target_id = self._streaming_msg_id if msg_id is None else msg_id
        attrs: dict = {}
        if content is not None:
            attrs["content"] = content
        if thinking is not None:
            attrs["thinking"] = thinking
        if tool_calls_json is not None:
            attrs["tool_calls"] = tool_calls_json

        # Never agent-wide-clear if a newer turn already owns another placeholder
        fallback = allow_agent_wide_fallback
        if (
            fallback
            and target_id is not None
            and self._streaming_msg_id is not None
            and self._streaming_msg_id != target_id
        ):
            fallback = False

        cleared = await self._chat_msg.finalize_streaming_message(
            self.id,
            target_id,
            attrs or None,
            allow_agent_wide_fallback=fallback,
        )
        if cleared and self._streaming_msg_id == target_id:
            self._streaming_msg_id = None
        return cleared

    def _reset_to_idle(self) -> None:
        """重置到 idle 状态。

        对齐 Elixir agent.ex:876 reset_to_idle/1。
        """
        self.status = AgentState.IDLE
        self.empty_retry_count = 0
        self.current_job = None
        self._llm_task = None
        self._cancel_reason = None
        self._broadcast_status(
            "idle",
            {"disposition": self.disposition},
        )
        # 根因修复：安全网 — 回到 idle 时若有残留 streaming msg_id，
        # 异步清除 DB 标志（不阻塞 reset）。_finalize_streaming_turn
        # 正常路径应已清除，此处仅兜底防漏
        if self._streaming_msg_id is not None:
            orphan_id = self._streaming_msg_id
            self._streaming_msg_id = None
            asyncio.create_task(
                self._chat_msg.finalize_streaming_message(
                    self.id, orphan_id, None,
                    allow_agent_wide_fallback=False,
                ),
                name=f"agent-{self.id}-orphan-streaming-cleanup",
            )

    # ── 内部: 回调 ───────────────────────────────────────────

    def _broadcast_status(self, status: str, extra: dict | None = None) -> None:
        """广播状态变更（通过回调，不直接依赖 WebSocket）。"""
        if self._on_status_change is not None:
            try:
                result = self._on_status_change(
                    self.id, status, extra or {}
                )
                # 如果回调返回协程，不需要 await（fire-and-forget）
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                pass

    def _broadcast_stream_event(self, event: dict) -> None:
        """广播流事件（通过回调，不直接依赖 WebSocket）。"""
        etype = event.get("type", "?")
        has_cb = self._on_stream_event is not None
        log.debug("agent_broadcast_stream", agent_id=self.id, event_type=etype, has_callback=has_cb)
        if self._on_stream_event is not None:
            try:
                result = self._on_stream_event(self.id, event)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                log.warning("agent_broadcast_failed", agent_id=self.id, error=str(e))

    def _broadcast_agent_health(self, health: str, message: str = "") -> None:
        """广播 agent 健康事件（LLM 调用出错 / 恢复）。

        前端契约（经 publish_stream_event 发到 lobby + agent:{id} 频道）::

            {"type": "agent_health", "agentId": ..., "projectId": ...,
             "health": "error" | "ok", "message": "<错误摘要，截断 200 字符；ok 时为空串>",
             "at": <毫秒时间戳>}

        "agent_health" 不属于 _DELTA_ONLY_TYPES，因此会分发到 lobby。
        广播失败静默吞掉 —— 绝不能因广播异常搞挂 agent。
        """
        try:
            self._broadcast_stream_event({
                "type": "agent_health",
                "agentId": self.id,
                "projectId": self.project_id,
                "health": health,
                "message": message[:200],
                "at": int(time.time() * 1000),
            })
        except Exception as e:
            log.warning(
                "agent_health_broadcast_failed",
                agent_id=self.id,
                error=str(e),
            )

    # ── Streamer 回调 ────────────────────────────────────────

    async def _on_delta(self, event: dict) -> None:
        """SSE delta 回调 — 转发给流事件回调 + 实时落库。

        每次 text_delta 都立即写入 DB streaming placeholder，确保：
        1. 前端长时间看不到新消息时（agent 多轮工具调用，
           placeholder 一直空），不会误判为 [对话被中断]
        2. 后端崩溃/重启时，部分输出已持久化

        FIX(text-acc): 收到 round_start 事件时重置累积器，
        避免工具循环中间轮的文本在前端实时显示中重复堆叠。
        """
        # 第一个 delta 到达 → 停止心跳（LLM 开始产出内容了）
        self._stop_heartbeat()
        self._broadcast_stream_event(event)

        # 工具循环新一轮 → 重置文本累积器 + BUG-7 按轮次累加 LLM 调用
        if event.get("type") == "round_start":
            self._streaming_text_acc = ""
            _run_id = getattr(self, "_current_run_id", None)
            if _run_id:
                try:
                    await self._run_ledger.increment_llm_calls(self.id, _run_id)
                except Exception:
                    pass
            return

        if event.get("type") == "text_delta" and self._streaming_msg_id:
            acc = getattr(self, "_streaming_text_acc", "")
            acc += event.get("content", "")
            self._streaming_text_acc = acc
            # Save every chunk (not batch) — long tool-call sequences
            # produce few text deltas, and DB writes are cheap.
            try:
                await self._chat_msg.update_message(
                    self.id, self._streaming_msg_id,
                    {"content": acc},
                )
            except Exception:
                pass  # Best-effort

    async def _on_tool_call(
        self, tool_name: str, arguments: str, tool_call_id: str
    ) -> dict:
        """工具执行回调 — 桥接 Streamer 和 ToolExecutor。

        Streamer 期望返回: {"role": "tool", "content": "...", "tool_call_id": "..."}
        ToolExecutor 返回: {"success": bool, "output": str, "error": str | None}
        """
        # 解析参数
        try:
            tool_args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            tool_args = {}

        workspace = await self._get_workspace_path()
        project_ws = await meta_db.get_project_workspace(self.project_id) or workspace

        # 工具调用开始 → 停止心跳（agent 已进入工具执行阶段）
        self._stop_heartbeat()

        # 广播工具调用开始
        self._broadcast_stream_event(
            {
                "type": "tool_call_start",
                "tool_name": tool_name,
                "arguments": arguments,
                "tool_call_id": tool_call_id,
            }
        )

        # ── Durable Run Ledger: record step start ──
        step_id = None
        _run_id = getattr(self, "_current_run_id", None)
        if _run_id:
            args_hash = _short_hash(arguments) if arguments else None
            # P2 fix(TEST10): 预分配 index 再 await，避免并行工具调用竞态
            # （多个并行 call 同时读 counter → 同 index）。先自增再落库。
            current_index = self._run_step_counter
            self._run_step_counter += 1
            try:
                step_id = await self._run_ledger.record_step_start(
                    agent_id=self.id,
                    run_id=_run_id,
                    step_index=current_index,
                    step_type="tool_call",
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    tool_args_hash=args_hash,
                )
                # BUG-7: increment tool counter at step start (covers interrupt path)
                await self._run_ledger.increment_tool_calls(self.id, _run_id)
            except Exception as e:
                log.debug("run_ledger.step_start_failed", error=str(e))

        # 执行工具
        result = await self._tool_executor.execute(
            agent_id=self.id,
            tool_name=tool_name,
            tool_args=tool_args,
            workspace_path=workspace,
            project_root=project_ws,
        )

        # ── Durable Run Ledger: record step end ──
        if step_id:
            try:
                result_content = result.get("output") or ""
                await self._run_ledger.record_step_end(
                    agent_id=self.id,
                    step_id=step_id,
                    status="completed" if result.get("success") else "failed",
                    result_hash=_short_hash(result_content[:1000]) if result_content else None,
                    result_size=len(result_content),
                    error=result.get("error"),
                    result_excerpt=result_content or result.get("error"),
                )
            except Exception as e:
                log.debug("run_ledger.step_end_failed", error=str(e))

        # 转换格式
        content = result.get("output") or ""
        if result.get("error") and not content:
            content = f"Error: {result['error']}"

        # 广播工具调用结束
        self._broadcast_stream_event(
            {
                "type": "tool_call_end",
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "success": result.get("success", False),
            }
        )

        return {
            "role": "tool",
            "content": content,
            "tool_call_id": tool_call_id,
            # success / duplicate / end_turn 透传给 streamer：
            # - success: 失败调用
            # - duplicate: 同参已执行过无新效果（doom 加速）
            # - end_turn: commit_turn 已接受 → 硬断本轮工具循环（BUG-3）
            # 这些键不会进入发给 LLM 的消息体 —— _execute_tools 重新组包时剥离。
            "success": result.get("success", False),
            "duplicate": result.get("duplicate", False),
            "end_turn": bool(result.get("end_turn")),
        }
