"""Agent — single agent asyncio task with state machine (契约 04).

核心状态机: idle → processing → idle

关键流程:
1. chat(): 检查 busy/paused → 构建 messages → 启动 LLM task → 设置安全超时
2. _run_llm(): LLM 调用 + tool loop + 空响应重试
3. _handle_completion(): 保存消息 + 标记 inbox 已读 + 自检 re-trigger
4. _handle_empty_response(): 退避重试 [5s, 15s, 45s]，超限升级上级
5. _handle_safety_timeout(): 10 分钟安全超时 → 清理 zombie → inbox 保持未读 + RESUME CHECKPOINT + 冷却后恢复
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
import json
import time
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

DEFAULT_MAX_TOOL_ROUNDS = 100
"""所有角色统一的 tool loop 最大轮次。不再按角色区别对待。"""

CONTEXT_WINDOW_DEFAULT = 128_000
"""默认 context window（模型配置缺失时）。"""

# ── 工具描述 ────────────────────────────────────────────────
# PermissionService 返回工具名列表，_build_tool_definitions 从
# hiveweave.tools.executor.TOOL_PARAM_SCHEMAS 取参数 schema。
# 本模块不再维护工具 schema 副本 (历史 _TOOL_SCHEMAS 已删除 — 存在
# 两份副本导致改一处忘改另一处, hire_agent permissionType 就是因此漏改)。


def _build_tool_definitions(tool_names: list[str]) -> list[dict]:
    """将工具名列表转为 LLM 工具定义。

    从 hiveweave.tools.executor.TOOL_PARAM_SCHEMAS 取参数 schema（让 LLM
    看到可用参数，如 hire_agent 的 parentId/permissionType）。未列出的工具
    用 permissive schema（additionalProperties: true），实际参数校验由
    ToolExecutor 执行。
    """
    tools: list[dict] = []
    for name in tool_names:
        # Use centralized schema from executor for BOTH description and params
        from hiveweave.tools.executor import get_tool_schema_for_llm, TOOL_PARAM_SCHEMAS
        params = get_tool_schema_for_llm(name)
        desc = TOOL_PARAM_SCHEMAS.get(name, {}).get("description") or f"Execute the {name} tool."
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
        self._resume_cooldown_until: float = 0.0  # monotonic deadline；超时后防 doom loop

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

        # ── 缓存 ──
        self._identity_prompt: str | None = None
        self._workspace_path: str | None = None

        # ── 后台 inbox watcher（BUG-010 修复）───────────────
        # 启动协程，每 5s 轮询未读 inbox 消息并触发 trigger。
        # 解决：executor inbox→agent.run() 链路断裂 — API 写入 inbox 后
        # 没有调用方主动 trigger target agent。
        self._inbox_watcher_task: asyncio.Task | None = None
        self._stop_watcher = False
        try:
            loop = asyncio.get_running_loop()
            self._inbox_watcher_task = loop.create_task(
                self._inbox_watcher_loop(),
                name=f"agent-{agent_id}-inbox-watcher",
            )
        except RuntimeError:
            # 没有 running loop（e.g. 测试场景）— 跳过 watcher
            pass

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
                if self.status == AgentState.IDLE:
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
            # 检查 busy → queue the message instead of dropping it
            if self.status == AgentState.PROCESSING:
                self._message_queue.append((message, opts, int(time.time() * 1000)))
                # Save to chat_history so the message persists in the UI
                await self._chat_msg.save_message({
                    "agent_id": self.id, "role": "user",
                    "content": message,
                    "is_background": False, "is_read": False,
                })
                log.info("chat_queued", agent_id=self.id,
                         queue_len=len(self._message_queue),
                         preview=message[:80])
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

            # 设置状态
            self.status = AgentState.PROCESSING
            self._cancel_reason = None
            self.empty_retry_count = 0
            self._broadcast_status("processing")

            # 保存 inbox_msg_ids（在 LLM 非空输出后标记已读）
            self.pending_inbox_msg_ids = opts.get("inbox_msg_ids")

            # 记录当前 job
            self.current_job = {
                "message": message,
                "opts": opts,
                "started_at": int(time.time() * 1000),
            }

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

    async def cancel(self) -> None:
        """取消当前处理。

        对齐 Elixir agent.ex:131 handle_cast(:cancel)。
        """
        self._cancel_reason = "cancelled"
        self._cancel_safety_timer()

        if self._llm_task and not self._llm_task.done():
            self._llm_task.cancel()
            try:
                await self._llm_task
            except asyncio.CancelledError:
                pass

        # BUG-010 修复：停 inbox watcher
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
            # BUG-010 修复：cancel 时也标记 inbox 已读，避免 watcher 无限重试
            if self.pending_inbox_msg_ids:
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
            # A6(2) 修复：cancel 时清理 streaming 标志，防止僵尸消息
            # 对齐错误路径和安全超时路径，它们都调用了此清理
            try:
                if self._streaming_msg_id:
                    await self._chat_msg.update_message(
                        self.id, self._streaming_msg_id,
                        {"content": "[对话被中断]", "is_streaming": False},
                    )
                    self._streaming_msg_id = None
                # 兜底：清理该 agent 的所有僵尸 streaming 消息
                await self._chat_msg.update_streaming_messages_done(self.id)
            except Exception as e:
                log.warning("cancel_clear_streaming_failed",
                            agent_id=self.id, error=str(e))
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

            # 创建 Streamer（统一 max_tool_rounds = 100）
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
        context = await self._build_context_prompt()
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

    async def _build_context_prompt(self) -> str:
        """构建 context prompt（动态，每轮重建）。

        对齐 Elixir streamer.ex: build_context_prompt/1。
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
        from hiveweave.services.handoff import HandoffService

        handoff_service = HandoffService()
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
            memories=memory_text or "",
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
        """获取模型配置。

        对齐 Elixir agent.ex:474 get_model_config/1。
        """
        model_id = self.config.get("model_id")
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
        """获取工具定义列表。

        对齐 Elixir tool_executor.ex: get_tools/2。
        """
        mode = await permission_service.get_permission_mode(self.id)
        tool_names = permission_service.get_tools_for_mode(mode)

        # custom 模式返回空列表 — 需要从 agent 配置获取 allowed_tools
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
        """获取 tool loop 最大轮次。所有角色统一 100 次。"""
        return DEFAULT_MAX_TOOL_ROUNDS

    def _get_context_window(self) -> int:
        """获取 context window 大小。"""
        # 从 agent config 的 model 配置中获取，或用默认值
        # ModelService.get() 返回的 dict 有 context_window 字段
        # 但在构建 messages 时 model_config 可能还没获取
        # 这里用 config 中缓存的值或默认值
        return self.config.get("_context_window", CONTEXT_WINDOW_DEFAULT)

    async def _get_workspace_path(self) -> str:
        """获取工作区路径（缓存）。

        优先使用 agent 专属的 worktree 路径（agents.workspace_path），
        实现 agent 间工作区隔离。若未分配 worktree（coordinator 角色
        或旧数据），回退到项目根目录。
        """
        if self._workspace_path is None:
            # 1. 先查 agents.workspace_path（executor 应有自己的 worktree）
            try:
                from hiveweave.services.org import OrgService
                org = OrgService()
                agent_row = await org.get_agent(self.id)
                if agent_row and agent_row.get("workspace_path"):
                    ws = agent_row["workspace_path"]
                    # 校验 worktree 目录确实存在（防止指向已删除的目录）
                    import os as _os
                    if _os.path.isdir(ws):
                        self._workspace_path = ws
                        return self._workspace_path
            except Exception:
                pass  # 回退到项目根
            # 2. 回退：项目根目录
            ws = await meta_db.get_project_workspace(self.project_id)
            self._workspace_path = ws or ""
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
            if self._streaming_msg_id:
                await self._chat_msg.update_message(
                    self.id, self._streaming_msg_id,
                    {
                        "content": content,
                        "thinking": thinking,
                        "tool_calls": json.dumps(tool_calls, ensure_ascii=False)
                        if tool_calls else "[]",
                        "is_streaming": False,
                    },
                )
                self._streaming_msg_id = None
            else:
                await self._chat_msg.save_message(
                    {
                        "agent_id": self.id,
                        "role": "assistant",
                        "content": content,
                        "thinking": thinking,
                        "tool_calls": json.dumps(tool_calls, ensure_ascii=False)
                        if tool_calls
                        else "[]",
                        "is_streaming": False,
                        "is_background": True if is_trigger else False,
                    }
                )
        except Exception as e:
            _save_failed = True
            _save_error_msg = str(e)
            log.error("completion_save_failed",
                      agent_id=self.id, error=_save_error_msg)
            # 降级：清掉 is_streaming flag，至少不让前端显示僵尸
            try:
                if self._streaming_msg_id:
                    await self._chat_msg.update_message(
                        self.id, self._streaming_msg_id,
                        {"content": content[:500] if content else "(empty)",
                         "is_streaming": False},
                    )
                    self._streaming_msg_id = None
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

        # 3. 标记 inbox 已读（仅非空输出时）
        if self.pending_inbox_msg_ids:
            # 分组：需要回复的 vs 不需要回复的
            unreplied = await self._check_unreplied_expect_report(tool_calls)

            if unreplied and self._reply_reminder_count < self._REPLY_REMINDER_MAX:
                # 只标记不需要回复的消息已读，需要回复的保持未读
                unreplied_ids = {m["id"] for m in unreplied}
                no_reply_ids = [
                    mid for mid in self.pending_inbox_msg_ids
                    if mid not in unreplied_ids
                ]
                if no_reply_ids:
                    await self._inbox.mark_read_by_ids(self.id, no_reply_ids)

                # 注入精准提示：列出已回复和未回复的人
                hint = await self._build_reply_hint(unreplied)
                await self._conversation.append_turn(
                    self.id, self.project_id,
                    [{"role": "user", "content": hint}]
                )
                self._reply_reminder_count += 1
                log.info(
                    "reply_reminder_injected",
                    agent_id=self.id,
                    from_count=len(unreplied),
                    marked_read=len(no_reply_ids),
                    reminder_round=self._reply_reminder_count,
                )
            elif unreplied and self._reply_reminder_count >= self._REPLY_REMINDER_MAX:
                # 达到上限仍未回复 → 升级到上级
                await self._inbox.mark_read_by_ids(
                    self.id, self.pending_inbox_msg_ids
                )
                await self._escalate_unreplied(unreplied)
                self._reply_reminder_count = 0
            else:
                # 全部标记已读（已回复 / 无需回复）
                await self._inbox.mark_read_by_ids(
                    self.id, self.pending_inbox_msg_ids
                )
                self._reply_reminder_count = 0

            self.pending_inbox_msg_ids = None

        # 成功完成 → 清除 resume 冷却
        self._resume_cooldown_until = 0.0

        # 4. 状态 → idle (先取消 safety timer，再 reset)
        self._cancel_safety_timer()
        self._reset_to_idle()

        # 5. 发送 done 事件（前端 streamChat 等待此事件停止 loading）
        self._broadcast_stream_event({
            "type": "done",
            "content": content,
            "agentId": self.id,
        })

        # 6. Process queued user messages (sent while agent was busy)
        await self._drain_message_queue()

        # 7. 自检 re-trigger
        await self._maybe_self_retrigger()

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
        # 但 _escalate_empty_response 遗漏，导致 is_streaming=1 僵尸消息
        try:
            if self._streaming_msg_id:
                await self._chat_msg.update_message(
                    self.id, self._streaming_msg_id,
                    {
                        "content": getattr(self, "_streaming_text_acc", "") or
                                   "[空响应超限，已升级上级处理]",
                        "is_streaming": False,
                    },
                )
                self._streaming_msg_id = None
            # 兜底：批量清理该 agent 的所有残留 streaming 消息
            await self._chat_msg.update_streaming_messages_done(self.id)
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

        # 保存错误消息到 DB — 更新 streaming placeholder 而非插入新消息
        # 包 try/except: 保存失败不应阻止 agent 恢复到 idle
        is_trigger = bool(self.pending_inbox_msg_ids)
        try:
            if self._streaming_msg_id:
                await self._chat_msg.update_message(
                    self.id, self._streaming_msg_id,
                    {
                        "content": f"[ERROR] {error_msg}",
                        "is_streaming": False,
                    },
                )
                self._streaming_msg_id = None
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

        # 不 ACK inbox — 可恢复错误应保留未读，冷却后 resume
        inbox_ids = list(self.pending_inbox_msg_ids or [])
        if inbox_ids:
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
            )

        self._cancel_safety_timer()
        self._reset_to_idle()

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

        信息链恢复语义:
        - 不 ACK inbox（消息保持未读，冷却结束后可 resume）
        - 写入 RESUME CHECKPOINT，让下一轮带着断点上下文继续
        - 记录 work_log，便于监控/stall watchdog 关联
        """
        # 清理 zombie streaming 消息
        await self._chat_msg.update_streaming_messages_done(self.id)

        inbox_ids = list(self.pending_inbox_msg_ids or [])

        # 更新 streaming placeholder（如果有的话）为超时标记
        if self._streaming_msg_id:
            await self._chat_msg.update_message(
                self.id, self._streaming_msg_id,
                {
                    "content": (
                        "[TIMEOUT] LLM call exceeded 10 minute safety limit. "
                        "Inbox left unread for resume after cooldown."
                    ),
                    "is_streaming": False,
                },
            )
            self._streaming_msg_id = None
        else:
            await self._chat_msg.save_message(
                {
                    "agent_id": self.id,
                    "role": "assistant",
                    "content": (
                        "[TIMEOUT] LLM call exceeded 10 minute safety limit. "
                        "Inbox left unread for resume after cooldown."
                    ),
                    "is_streaming": False,
                }
            )

        await self._write_resume_checkpoint(
            reason="safety_timeout",
            inbox_ids=inbox_ids,
        )
        self._arm_resume_cooldown(TIMEOUT_RESUME_COOLDOWN_S)

        # Keep pending_inbox_msg_ids cleared from this turn, but messages
        # stay unread in DB so watcher can pick them up after cooldown.
        self.pending_inbox_msg_ids = None

        self._cancel_safety_timer()
        self._reset_to_idle()
        log.warning(
            "safety_timeout_resume_armed",
            agent_id=self.id,
            inbox_left_unread=len(inbox_ids),
            cooldown_s=TIMEOUT_RESUME_COOLDOWN_S,
        )

    async def _handle_cancel(self) -> None:
        """用户取消处理。

        对齐 Elixir agent.ex:131 handle_cast(:cancel)。
        """
        # 清理 zombie streaming 消息
        await self._chat_msg.update_streaming_messages_done(self.id)

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

        expect_reply_msgs = [m for m in target_msgs if m.get("expect_report")]
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
                and m.get("expect_report")
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

    # ── 内部: 自检 re-trigger ────────────────────────────────

    async def _maybe_self_retrigger(self) -> None:
        """自检 re-trigger。

        对齐 Elixir agent.ex:890 maybe_self_retrigger/1。

        检查:
        1. 是否有未读 inbox 消息 → trigger
        2. 是否有未回答的用户消息 → trigger
        """
        await asyncio.sleep(SELF_RETRIGGER_DELAY_MS / 1000.0)

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
        """Arm cooldown so inbox watcher won't immediately re-fire."""
        self._resume_cooldown_until = time.monotonic() + max(0.0, seconds)

    async def _write_resume_checkpoint(
        self, *, reason: str, inbox_ids: list[str]
    ) -> None:
        """Persist a structured resume hint into conversation + work_log.

        Next successful trigger loads conversation history, so the agent can
        continue unfinished work instead of treating the obligation as gone.
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
            "running/claimed/rework tasks and resume them."
        )
        try:
            await self._conversation.append_turn(
                self.id,
                self.project_id,
                [{"role": "user", "content": checkpoint}],
            )
        except Exception as e:
            log.warning(
                "resume_checkpoint_persist_failed",
                agent_id=self.id,
                error=str(e),
            )
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
        except Exception:
            pass

    # ── 内部: 状态管理 ────────────────────────────────────────

    async def _drain_message_queue(self) -> None:
        """Process queued user messages that arrived while the agent was busy."""
        if not self._message_queue:
            return
        message, opts, _ts = self._message_queue.pop(0)
        log.info("chat_dequeued", agent_id=self.id,
                 remaining=len(self._message_queue),
                 preview=message[:80])
        # Process asynchronously — don't block the current completion
        import asyncio
        # Use chat() to go through normal flow (acquires lock, sets PROCESSING)
        await self.chat(message, opts)

    def _reset_to_idle(self) -> None:
        """重置到 idle 状态。

        对齐 Elixir agent.ex:876 reset_to_idle/1。
        """
        self.status = AgentState.IDLE
        self.empty_retry_count = 0
        self.current_job = None
        self._llm_task = None
        self._cancel_reason = None
        self._broadcast_status("idle")

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

    # ── Streamer 回调 ────────────────────────────────────────

    async def _on_delta(self, event: dict) -> None:
        """SSE delta 回调 — 转发给流事件回调 + 实时落库。

        每次 text_delta 都立即写入 DB streaming placeholder，确保：
        1. 前端长时间看不到新消息时（agent 多轮工具调用，
           placeholder 一直空），不会误判为 [对话被中断]
        2. 后端崩溃/重启时，部分输出已持久化
        """
        # 第一个 delta 到达 → 停止心跳（LLM 开始产出内容了）
        self._stop_heartbeat()
        self._broadcast_stream_event(event)
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

        # 执行工具
        result = await self._tool_executor.execute(
            agent_id=self.id,
            tool_name=tool_name,
            tool_args=tool_args,
            workspace_path=workspace,
        )

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
        }
