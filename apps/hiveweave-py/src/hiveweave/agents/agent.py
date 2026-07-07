"""Agent — single agent asyncio task with state machine (契约 04).

核心状态机: idle → processing → idle

关键流程:
1. chat(): 检查 busy/paused → 构建 messages → 启动 LLM task → 设置安全超时
2. _run_llm(): LLM 调用 + tool loop + 空响应重试
3. _handle_completion(): 保存消息 + 标记 inbox 已读 + 自检 re-trigger
4. _handle_empty_response(): 退避重试 [5s, 15s, 45s]，超限升级上级
5. _handle_safety_timeout(): 10 分钟安全超时 → 清理 zombie → 重置 idle
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

MAX_TOOL_ROUNDS_BY_ROLE: dict[str, int] = {
    "CEO": 60,
    "HR": 40,
    "coordinator": 50,
    "manager": 50,
    "executor": 80,
}
"""角色专属 tool loop 最大轮次。对齐 Elixir agent.ex:39 max_tool_rounds_for_role/1。"""

DEFAULT_MAX_TOOL_ROUNDS = 25
"""未知角色的默认 tool loop 轮次。"""

CONTEXT_WINDOW_DEFAULT = 128_000
"""默认 context window（模型配置缺失时）。"""

# ── 工具描述（最小注册表）────────────────────────────────────
# PermissionService 返回工具名列表，Streamer 需要完整定义。
# 这里维护一个名称→描述的映射，参数 schema 用 permissive（additionalProperties: true）。

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "bash": "Execute a bash command in the workspace.",
    "run_command": "Run a shell command (restricted).",
    "read_file": "Read file contents from the workspace.",
    "write_file": "Write content to a file in the workspace.",
    "edit_file": "Edit an existing file using search-and-replace.",
    "delete_file": "Delete a file from the workspace.",
    "move_file": "Move or rename a file.",
    "list_files": "List files in a directory.",
    "search_files": "Search for files by glob pattern.",
    "create_directory": "Create a new directory.",
    "delete_directory": "Delete a directory.",
    "grep": "Search for patterns in files (ripgrep).",
    "apply_patch": "Apply a multi-file patch.",
    "todowrite": "Update the task todo list.",
    "question": "Ask the user a question and wait for response.",
    "websearch": "Search the web.",
    "webfetch": "Fetch a URL and convert HTML to readable text. Use to read documentation, API references, or any web page content.",
    "review": "Run a code review.",
    "run_code_review": "Run a code quality review.",
    "run_security_audit": "Run a security audit.",
    "run_tests": "Run tests and review results.",
    "run_perf_audit": "Run a performance audit.",
    "run_full_review": "Run full review (code + security + tests + perf).",
    "read_skill": "Read a skill's full instructions.",
    "list_available_skills": "List available skills.",
    "bind_skill": "Bind a skill to this agent.",
    "read_memory": "Read memories (project/agent/archive layer).",
    "write_memory": "Write a memory entry.",
    "message_superior": "Send a message to the superior agent.",
    "message_subordinate": "Send a message to a subordinate agent.",
    "message_peer": "Send a message to a peer agent.",
    "message_team": "Send a message to the team group chat.",
    "dispatch_task": "Dispatch a task to a subordinate agent.",
    "report_completion": "Report task completion to superior.",
    "request_review": "Request a code review from superior.",
    "approve_work": "Approve a subordinate's work.",
    "reject_work": "Reject a subordinate's work (request rework).",
    "list_subordinates": "List direct subordinates.",
    "read_roster": "Read the personnel roster.",
    "write_work_log": "Write a work log entry.",
    "hire_agent": "Hire a new agent (HR only). Places the new agent under a specified parent (default: CEO). Pass templateId to pre-fill role/goal/skills.",
    "transfer_agent": "Transfer an agent to a new parent.",
    "dismiss_agent": "Dismiss an agent.",
    "save_charter": "Save the project charter.",
    "update_goals": "Update enterprise goals.",
    "git_worktree_create": "Create a git worktree for an agent.",
    "git_worktree_merge": "Merge a worktree branch into main.",
    "git_worktree_remove": "Remove a git worktree.",
    "git_worktree_list": "List all worktrees.",
    "git_worktree_status": "Check worktree status.",
    "git_worktree_checkpoint": "Save a checkpoint (commit) in an agent's worktree.",
    "update_roster": "Update the personnel roster for an agent (HR only).",
    "list_agent_templates": "Browse agent templates catalog. HR only. Returns templates with role/goal/skills pre-configured.",
    "unbind_skill": "Unbind a skill from an agent.",
    "read_charter": "Read the project charter.",
    "read_goals": "Read enterprise goals.",
    "view_org_chart": "View the full organization chart.",
    "read_work_logs": "Read work logs from subordinates.",
    "schedule_alarm": "Schedule an alarm/reminder. One-shot fire at specified game time. Can target self (toAgentId=your own id) or others. Include a purpose message delivered on fire.",
    "schedule_alarm": "Schedule a reminder. One-shot or recurring. Use when you need to: check back on something later (e.g. 'check build status in 1 game hour'), set a self-reminder before a deadline, or coordinate team timing. The alarm fires as an inbox message from '你自己的闹钟' or 'XXX的闹钟'.\n\nExamples:\n- Self-reminder: schedule_alarm(toAgentId='self', purpose='Check if HR has reported back', fireInGameSeconds=3600)\n- Recurring check: schedule_alarm(toAgentId='self', purpose='Poll build status', fireInGameSeconds=7200, repeatIntervalSeconds=7200)\n- Remind teammate: schedule_alarm(toAgentId='墨言', purpose='Sprint review in 30 game minutes', fireInGameSeconds=1800)",
    "list_alarms": "List all pending alarms for the current project. Shows alarm ID, remaining game seconds until fire, and purpose.",
    "cancel_alarm": "Cancel a pending alarm by its ID. Use when a reminder is no longer needed. Get the alarmId from list_alarms.",
    "send_message": "Send a message to other agents.",
}


# Per-tool explicit parameter schemas. Tools listed here expose a typed
# schema to the LLM (so it knows which params exist — e.g. parentId for
# hire_agent, which lets HR place a new agent under a specific manager
# instead of the default CEO). Tools not listed fall back to the
# permissive schema in _build_tool_definitions.
_TOOL_SCHEMAS: dict[str, dict] = {
    "hire_agent": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Agent codename — a creative Chinese flower-name (花名), "
                    "2-4 chars. Example: 折纸, 拾光, 鹿鸣, 鲸落."
                ),
            },
            "role": {
                "type": "string",
                "description": (
                    "Chinese job title (e.g. 前端工程师, 后端开发, 测试工程师). "
                    "Determines permission_type: coordinator roles "
                    "(ceo/hr/qa/cto/architect/manager/pm) → readonly, others → readwrite."
                ),
            },
            "goal": {
                "type": "string",
                "description": (
                    "Agent's goal. Defaults to 'Execute {role} responsibilities.' "
                    "if omitted."
                ),
            },
            "backstory": {
                "type": "string",
                "description": (
                    "2-4 sentence personal narrative — past experience, "
                    "personality, hobbies. NOT project-related. Makes the agent "
                    "feel like a real character."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill slugs to bind. Use list_available_skills to search "
                    "by keyword. See HR Recruitment Skill Standards table for "
                    "role→skills mapping."
                ),
            },
            "parentId": {
                "type": "string",
                "description": (
                    "Parent agent ID — places the new agent under a specific "
                    "manager in the org tree. Accepts UUID, short_id (e.g. "
                    "'A001'), or agent name. Default: CEO. "
                    "IRON RULE: never set parentId to your own ID — HR is a "
                    "service role, not an org manager. Default new agents under "
                    "the CEO or the requesting business manager."
                ),
            },
            "templateId": {
                "type": "string",
                "description": (
                    "Template ID to pre-fill role/goal/backstory/skills. Use "
                    "list_agent_templates to browse. Explicit params override "
                    "template values."
                ),
            },
        },
        "required": ["name", "role"],
    },
    # BUG-036: question tool schema — without this the LLM guesses
    # parameter names (message/content/query) and hits 'question is required'
    "question": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user. Be clear and specific.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of choices for the user to pick from.",
            },
        },
        "required": ["question"],
    },
    # BUG-036: alarm tool schemas
    "schedule_alarm": {
        "type": "object",
        "properties": {
            "toAgentId": {
                "type": "string",
                "description": "Agent ID to notify (use your own id for self-reminder, or another agent's id/name/short_id). Use 'self' for self-targeting.",
            },
            "purpose": {
                "type": "string",
                "description": "Message delivered when the alarm fires (e.g. 'Check build status'). Delivered via inbox with [ALARM] prefix.",
            },
            "fireInGameSeconds": {
                "type": "number",
                "description": "Seconds of GAME TIME from now until first fire. 1 real hour = 1 game day (86400 game seconds). E.g. 3600 game seconds ≈ 2.5 real minutes.",
            },
            "repeatIntervalSeconds": {
                "type": "number",
                "description": "If set, alarm repeats every N game seconds. E.g. 43200 = repeat every 12 game hours (30 real minutes). Omit for one-shot.",
            },
            "scriptCommand": {
                "type": "string",
                "description": "Shell command to execute when the alarm fires (e.g. 'python check_build.py'). Runs BEFORE inbox notification. 120s timeout.",
            },
        },
        "required": ["toAgentId", "purpose", "fireInGameSeconds"],
    },
    "cancel_alarm": {
        "type": "object",
        "properties": {
            "alarmId": {
                "type": "string",
                "description": "The ID of the alarm to cancel.",
            },
        },
        "required": ["alarmId"],
    },
    # BUG-036: send_message schema — LLM needs to know expectReport parameter
    "send_message": {
        "type": "object",
        "properties": {
            "recipients": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of recipient agent names, short_ids, or roles (e.g. ['HR'], ['A005'], ['墨言']).",
            },
            "message": {
                "type": "string",
                "description": "Message body. CAVEMAN style for agents — no pleasantries, just facts.",
            },
            "expectReport": {
                "type": "boolean",
                "description": "If true, recipient sees **[REPLY REQUIRED]** and should respond. Use for task assignments and review requests.",
            },
            "priority": {
                "type": "string",
                "enum": ["normal", "urgent"],
                "description": "Message priority. Use 'urgent' for escalations and critical issues.",
            },
        },
        "required": ["recipients", "message"],
    },
    # BUG-036: update_goals schema — LLM needs to know expected parameter names
    "update_goals": {
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "Updated project objective (mission statement).",
            },
            "focus": {
                "type": "string",
                "description": "Current focus area or priority.",
            },
            "keyResults": {
                "type": "array",
                "items": {"type": "object"},
                "description": "List of key results, each with 'text' and optional 'status' fields.",
            },
            "userInvolvement": {
                "type": "string",
                "description": "Desired user involvement level: low/medium/high.",
            },
        },
    },
}


def _build_tool_definitions(tool_names: list[str]) -> list[dict]:
    """将工具名列表转为 LLM 工具定义。

    优先使用 _TOOL_SCHEMAS 中的显式 schema（让 LLM 看到可用参数，如
    hire_agent 的 parentId，从而支持创建时指定父级）。未列出的工具用
    permissive schema（additionalProperties: true），实际参数校验由
    ToolExecutor 执行。
    """
    tools: list[dict] = []
    for name in tool_names:
        desc = _TOOL_DESCRIPTIONS.get(name, f"Execute the {name} tool.")
        params = _TOOL_SCHEMAS.get(name) or {
            "type": "object",
            "additionalProperties": True,
        }
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

        # ── asyncio 原语 ──
        self._llm_task: asyncio.Task | None = None
        self._safety_timer: asyncio.TimerHandle | None = None
        self._lock = asyncio.Lock()

        # ── 回调 ──
        self._on_status_change = on_status_change
        self._on_stream_event = on_stream_event

        # ── 服务实例 ──
        self._streamer: Streamer | None = None  # 延迟创建（需要 role）
        self._tool_executor = ToolExecutor(permission_service, approval_service)
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
                    pending = await self._inbox.get_pending_messages(self.id)
                    if pending:
                        log.info(
                            "inbox_watcher_found_pending",
                            agent_id=self.id,
                            count=len(pending),
                            trigger_fail_count=trigger_fail_count,
                        )
                        # 延迟导入避免循环
                        from hiveweave.agents.trigger import trigger_subordinate
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
            # 检查 busy
            if self.status == AgentState.PROCESSING:
                return {"error": "busy"}

            # 检查暂停
            if system_state.paused():
                return {"error": "paused"}

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

            # 创建 Streamer（角色专属 max_tool_rounds）
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

        # Goals
        goals = await charter_service.read_goals(self.project_id)

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

        context = build_context_prompt(
            agent_id=self.id,
            memories=memory_text or "",
            handoffs=handoffs,
            goals=goals,
            involvement_level=involvement,
            bound_skills=bound_skills_json,
            memory_text=memory_text,
        )

        # 追加 skills section
        if skills_section:
            context = f"{context}\n\n{skills_section}"

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
        """获取角色专属 tool loop 最大轮次。

        对齐 Elixir agent.ex:39 max_tool_rounds_for_role/1。
        """
        role = self.config.get("role", "")
        return MAX_TOOL_ROUNDS_BY_ROLE.get(role, DEFAULT_MAX_TOOL_ROUNDS)

    def _get_context_window(self) -> int:
        """获取 context window 大小。"""
        # 从 agent config 的 model 配置中获取，或用默认值
        # ModelService.get() 返回的 dict 有 context_window 字段
        # 但在构建 messages 时 model_config 可能还没获取
        # 这里用 config 中缓存的值或默认值
        return self.config.get("_context_window", CONTEXT_WINDOW_DEFAULT)

    async def _get_workspace_path(self) -> str:
        """获取项目工作区路径（缓存）。"""
        if self._workspace_path is None:
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

        # 1. 保存 assistant 消息到 chat_messages
        # BUG-034: 如果这是 trigger 触发的后台处理，标记 is_background，
        # 避免污染前端主聊天窗口。用户对话的 assistant 回复不标记。
        # 注意: trigger assistant 回复是 agent 内部处理(读文件/分析)，
        # 不是对外消息。真正的 agent 间通信由 send_message 工具通过
        # TeamChatService (role='team') 记录。
        is_trigger = opts.get("trigger", False)
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

        # BUG-026 修复：自动写 work_log，确保前端 Logs tab 有内容。
        # 不依赖 LLM 主动调用 write_work_log 工具——每轮完成都记录一条，
        # summary 取最终输出（或用户消息），details 记录工具调用清单与轮次。
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

        # 2. 追加到 conversation store
        # user message + tool turn messages (assistant+tool pairs) + final assistant
        turn_messages: list[dict] = [{"role": "user", "content": message}]
        turn_messages.extend(tool_turn_messages)
        await self._conversation.append_turn(
            self.id, self.project_id, turn_messages
        )

        # 3. 标记 inbox 已读（仅非空输出时）
        if self.pending_inbox_msg_ids:
            await self._inbox.mark_read_by_ids(
                self.id, self.pending_inbox_msg_ids
            )
            self.pending_inbox_msg_ids = None

        # 4. 状态 → idle
        self._reset_to_idle()

        # 5. 发送 done 事件（前端 streamChat 等待此事件停止 loading）
        self._broadcast_stream_event({
            "type": "done",
            "content": content,
            "agentId": self.id,
        })

        # 6. 自检 re-trigger
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
        1. 标记 pending inbox 已读（避免重复触发）
        2. 通知上级 agent
        3. 状态 → idle
        """
        log.warning(
            "empty_escalate",
            agent_id=self.id,
            retry_count=self.empty_retry_count,
            msg="escalating to superior after max empty retries",
        )

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

        # 发送 error 事件（前端 streamChat 等待此事件停止 loading）
        # 必须在 _reset_to_idle 之前发送，确保前端先处理错误再看到 idle 状态
        self._broadcast_stream_event({
            "type": "error",
            "message": error_msg,
            "errorType": error_type,
            "agentId": self.id,
        })

        # 保存错误消息到 DB
        # BUG-034: 如果是 trigger 触发的后台处理，标记 is_background
        is_trigger = bool(self.pending_inbox_msg_ids)
        await self._chat_msg.save_message(
            {
                "agent_id": self.id,
                "role": "assistant",
                "content": f"[ERROR] {error_msg}",
                "is_streaming": False,
                "is_background": True if is_trigger else False,
            }
        )

        # 标记 inbox 已读（避免僵尸消息）
        if self.pending_inbox_msg_ids:
            await self._inbox.mark_read_by_ids(
                self.id, self.pending_inbox_msg_ids
            )
            self.pending_inbox_msg_ids = None

        self._reset_to_idle()

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

        对齐 Elixir agent.ex:675 on_safety_timeout/1。
        """
        # 清理 zombie streaming 消息
        await self._chat_msg.update_streaming_messages_done(self.id)

        # 标记 inbox 已读
        if self.pending_inbox_msg_ids:
            await self._inbox.mark_read_by_ids(
                self.id, self.pending_inbox_msg_ids
            )
            self.pending_inbox_msg_ids = None

        # 保存超时消息
        await self._chat_msg.save_message(
            {
                "agent_id": self.id,
                "role": "assistant",
                "content": "[TIMEOUT] LLM call exceeded 10 minute safety limit.",
                "is_streaming": False,
            }
        )

        self._reset_to_idle()

    async def _handle_cancel(self) -> None:
        """用户取消处理。

        对齐 Elixir agent.ex:131 handle_cast(:cancel)。
        """
        # 清理 zombie streaming 消息
        await self._chat_msg.update_streaming_messages_done(self.id)

        # 保留 inbox 未读（用户取消不应标记已读）
        # pending_inbox_msg_ids 保持不变，下次 trigger 可重新处理

        self._reset_to_idle()

    # ── 内部: 自检 re-trigger ────────────────────────────────

    async def _maybe_self_retrigger(self) -> None:
        """自检 re-trigger。

        对齐 Elixir agent.ex:890 maybe_self_retrigger/1。

        检查:
        1. 是否有未读 inbox 消息 → trigger
        2. 是否有未回答的用户消息 → trigger
        """
        await asyncio.sleep(SELF_RETRIGGER_DELAY_MS / 1000.0)

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

    # ── 内部: 状态管理 ────────────────────────────────────────

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
        """SSE delta 回调 — 转发给流事件回调。"""
        self._broadcast_stream_event(event)

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
