"""spawn_subagent 工具 — 同步嵌套子代理（opencode 模型，2026-08-01 设计）。

子代理：
- 全新上下文：身份提示 + 项目共享层（build_project_context）+ 任务描述（用户消息）。
- 权限完全继承父：工具调用转发父的 agent_id 给 ToolExecutor；深度 1（工具列表
  去掉 spawn_subagent 本身）。
- 独立预算：max_tool_rounds = SUBAGENT_MAX_TOOL_ROUNDS（父默认 budget_tool_calls），
  rounds 80% 警告按 streamer 既有机制触发；不扣父 run ledger 计数。
- commit_turn 被本地拦截（不写父的 turn_session / work_log / 门禁 / lessons），
  返回 end_turn=True 结束子代理工具循环；最终文本作为 ToolResult 返回父。
- 硬限 timeout_s（默认 240s，上限 480s）：asyncio.wait_for 外部套超时；
  spawn 时父的 elapsed 预算与 safety timer 顺延（Task 2）。
- 只记结果：子代理过程不落库。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import structlog
from pydantic import BaseModel, Field

from hiveweave.llm.streamer import Streamer
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

SUBAGENT_TIMEOUT_S = 240
SUBAGENT_MAX_TIMEOUT_S = 480
SUBAGENT_MAX_TOOL_ROUNDS = 100  # 与 run_ledger 默认 budget_tool_calls 一致

# ──────────────────────────────────────────────────────────────────────────
# 子代理类型 → 工具白名单（唯一权威）
# 设计见 docs/superpowers/specs/2026-08-01-subagent-tool-profiles-design.md
# §3。白名单是唯一权威：未显式加入的工具不出现在任何子代理工具列表
# （宁可少给不可漏越权）。spawn_subagent 本身永不出现（深度 1）。
#
# 三类型语义：
# - readonly: 只读 + 检索，不写码不跑测试不流转任务
# - audit:    只读 + 跑测试/browse 看页面 + 提交任务/请求审查。不出证据
#             （attest_doc_review）、不豁免（waive_attestation）——证据由
#             父代理基于子代理输出自行决定提交。子代理只干简单繁重的活。
# - write:    只读 + 写码 + 任务流转 + git_worktree 全套。不给 browse
#             （视觉 QA 归 audit）、不给 attest 类（出证据归父）。
# ──────────────────────────────────────────────────────────────────────────

_SUBAGENT_COMMON_TOOLS = frozenset({
    # 13 个：所有子代理都有
    "commit_turn", "send_message", "ask_agent", "get_tasks",
    "read_work_logs", "read_memory", "write_memory", "calculate",
    "websearch", "check_agent_status", "get_platform_state",
    "read_file", "list_files",
})

_SUBAGENT_READONLY_EXTRA = frozenset({
    "grep", "search_files", "webfetch", "read_skill",
    "read_charter", "read_goals", "view_org_chart",
})

_SUBAGENT_AUDIT_EXTRA = frozenset({
    # 跑测试 + browse 看页面（视觉 QA），任务流转（不含 attest/waive）
    "bash", "run_command", "run_tests", "browse",
    "claim_task", "submit_task", "request_review", "update_task_status",
})

_SUBAGENT_WRITE_EXTRA = frozenset({
    # 写码 + 自测自改闭环（bash/run_tests）+ 任务流转 + git_worktree 全套
    # 不含 browse（视觉 QA 归 audit）、不含 attest（出证据归父）
    "write_file", "edit_file", "apply_patch",
    "create_directory", "delete_file", "delete_directory", "move_file",
    "bash", "run_command", "run_tests",
    "git_worktree_status", "git_worktree_checkpoint",
    "git_worktree_list",  # merge/remove stay parent-owned (not subagent)
    "claim_task", "submit_task", "request_review", "update_task_status",
})

_SUBAGENT_TYPE_TOOLS: dict[str, frozenset[str]] = {
    "readonly": _SUBAGENT_COMMON_TOOLS | _SUBAGENT_READONLY_EXTRA,
    "audit": (
        _SUBAGENT_COMMON_TOOLS
        | _SUBAGENT_READONLY_EXTRA
        | _SUBAGENT_AUDIT_EXTRA
    ),
    "write": (
        _SUBAGENT_COMMON_TOOLS
        | _SUBAGENT_READONLY_EXTRA
        | _SUBAGENT_WRITE_EXTRA
    ),
}

_VALID_SUBAGENT_TYPES = frozenset(_SUBAGENT_TYPE_TOOLS.keys())


class SpawnSubagentParams(BaseModel):
    """Parameters for spawn_subagent tool."""

    model_config = {"populate_by_name": True}

    subagent_type: str = Field(
        description=(
            "REQUIRED (no default). One of: 'readonly' | 'write' | 'audit'. "
            "readonly = read-only scout; audit = run tests/browse + submit "
            "task (no attestation); write = edit code + git_worktree (parent "
            "must have SOURCE_WRITE). Missing or invalid value returns an "
            "error without changing the parent's turn."
        ),
        json_schema_extra={"aliases": ["type", "kind"]},
    )
    prompt: str = Field(
        description=(
            "What you need this subagent to do, in its own fresh context. "
            "Be concrete: files, goals, acceptance criteria."
        ),
        json_schema_extra={"aliases": ["task", "instructions", "work"]},
    )
    description: str | None = Field(
        default=None,
        description="One-line description shown in the waiting context.",
        json_schema_extra={"aliases": ["desc", "title"]},
    )
    timeout_s: int | None = Field(
        default=None,
        description=(
            f"Hard deadline in seconds (default {SUBAGENT_TIMEOUT_S}, "
            f"max {SUBAGENT_MAX_TIMEOUT_S}). Larger bounded jobs only; "
            "work expected to exceed this should be dispatched async via "
            "dispatch_task instead."
        ),
        json_schema_extra={"aliases": ["timeout", "timeoutSeconds"]},
    )


@tool(
    "spawn_subagent",
    "Spawn a synchronous nested subagent in this turn. The subagent works in "
    "YOUR worktree with YOUR permissions (no more privilege than you), sees "
    "only the project shared memory + your prompt (not your conversation), "
    "and must commit_turn before returning its final text. Multiple spawns "
    "in one round run in parallel — but they share your worktree, so do not "
    "spawn two that write the same files. Jobs bigger than a few minutes "
    "should go through dispatch_task instead of this tool. "
    "subagent_type is REQUIRED: 'readonly' (read-only scout), 'audit' (run "
    "tests/browse + submit task — no attestation), or 'write' (edit code + "
    "git_worktree; requires parent SOURCE_WRITE). The subagent never produces "
    "attestations itself — pass its findings back and decide evidence "
    "yourself.",
    requires_workspace=False,
    security_level="standard",
)
async def spawn_subagent_tool(
    params: SpawnSubagentParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Spawn a synchronous nested subagent and return its final text."""
    from hiveweave.agents.supervisor import agent_manager

    parent = agent_manager.get_agent(agent_id)
    if parent is None:
        return ToolResult.err(
            "spawn_subagent failed: parent agent is not live "
            "(agent_manager has no instance)"
        )

    # subagent_type 必填 + 取值校验（缺省/非法 → err，父回合不受影响）
    subagent_type = (params.subagent_type or "").strip().lower()
    if subagent_type not in _VALID_SUBAGENT_TYPES:
        return ToolResult.err(
            f"spawn_subagent requires subagent_type ∈ "
            f"{sorted(_VALID_SUBAGENT_TYPES)}; got "
            f"{subagent_type!r}. Missing or invalid value is rejected "
            f"without changing the parent's turn."
        )

    # write 可用性：父必须具备 SOURCE_WRITE（executor/coordinator/qa 家族）。
    # CEO/HR 无 SOURCE_WRITE → err。复用 FAMILY_CAPABILITIES，不另写一套。
    if subagent_type == "write" and not _parent_has_source_write(parent):
        return ToolResult.err(
            "subagent_type='write' requires a code-writing parent "
            "(SOURCE_WRITE); CEO/HR may only use readonly/audit"
        )

    prompt = (params.prompt or "").strip()
    if not prompt:
        return ToolResult.err("spawn_subagent requires a non-empty prompt")
    timeout_s = params.timeout_s or SUBAGENT_TIMEOUT_S
    timeout_s = max(1, min(int(timeout_s), SUBAGENT_MAX_TIMEOUT_S))

    # 预算与 timer 顺延（不扣父计数，只回拨墙钟窗口）
    run_id = getattr(parent, "_current_run_id", None)
    if run_id:
        try:
            await parent._run_ledger.extend_elapsed_budget(
                agent_id, run_id, timeout_s * 1000
            )
        except Exception:
            pass
    try:
        parent._extend_safety_timer(timeout_s)
    except Exception:
        pass

    try:
        result = await _run_subagent(
            parent, prompt, params.description, timeout_s, subagent_type
        )
    except Exception as e:  # 兜底：任何异常都不炸父回合
        return ToolResult.err(f"spawn_subagent failed: {type(e).__name__}: {e}")

    if result.get("status") != "ok":
        return ToolResult.err(
            f"subagent failed: {result.get('error') or 'unknown error'}"
        )
    text = result.get("content") or "(subagent returned no text)"
    return ToolResult.ok(
        f"[subagent result]\n{text}",
        extra={"subagent": {"timeout_s": timeout_s, "type": subagent_type}},
    )


def _parent_has_source_write(parent: Any) -> bool:
    """父是否具备 write 子代理资格（SOURCE_WRITE + 独立 worktree）。

    设计文档 §2 要求「SOURCE_WRITE 判定复用既有权限矩阵
    （`agent_gets_write_worktree` / permission 评估同源逻辑），不新写一套」。
    用 `agent_gets_write_worktree` 判定 —— 它要求 executor 或 builder
    coordinator（perm + family 双重校验），且与 worktree 资格同源：
    「能 spawn write 子代理」≡「有 write worktree」，避免父无 worktree
    时 write 子代理 workspace 落到项目根的越权。

    QA 边缘情况：role="qa" + perm="coordinator"（hire_agent 推断）时，
    agent_gets_write_worktree=False（family≠coordinator），正确拒绝。
    """
    from hiveweave.services.git_worktree.ensure import agent_gets_write_worktree

    config = getattr(parent, "config", None) or {}
    return agent_gets_write_worktree(config)


def _subagent_identity(
    parent: Any,
    description: str | None,
    timeout_s: int,
    subagent_type: str,
    workspace: str | None,
) -> str:
    name = (parent.config or {}).get("name") or parent.id
    role = (parent.config or {}).get("role") or "agent"
    ws_line = ""
    if workspace:
        ws_line = (
            f"\nYou work in the parent's workspace: {workspace}. "
            "Do NOT write outside it."
        )
        if subagent_type == "write":
            ws_line += (
                " You may only write inside the parent's workspace "
                f"({workspace})."
            )
    lines = [
        f"You are a {subagent_type} subagent of {name} ({role}). You are "
        "helping your parent get work done during its turn — you are a "
        "hands, not a planner.",
        f"The parent is waiting for you. Task: {description or '(see user message)'}",
        "You have exactly the same permissions as your parent (no more). "
        "You work inside the parent's workspace. You CANNOT spawn subagents.",
        (
            f"You MUST finish and call commit_turn within {timeout_s}s "
            "(the platform enforces this deadline — it will kill you). "
            "Budget your tool calls: watch the 'remaining calls' warning."
        ),
        "If the work would clearly exceed this deadline, do not overreach: "
        "stop, commit_turn with phase=blocked and waiting_on the parent, and "
        "tell the parent to use dispatch_task for async delegation instead.",
        "commit_turn is REQUIRED to finish. Never end without it.",
        "You do NOT produce attestations (attest_doc_review / waive_attestation) "
        "yourself — pass findings back to the parent, who decides whether to "
        "register evidence.",
    ]
    if subagent_type == "write":
        lines.append(
            "You can run tests (bash/run_tests). Self-test before submitting: "
            "if tests fail, fix and re-test until green, then report to the "
            "parent. Do not submit broken work."
        )
    if ws_line:
        lines.append(ws_line.lstrip("\n"))
    return "\n".join(lines)


async def _run_subagent(
    parent: Any,
    prompt: str,
    description: str | None,
    timeout_s: int,
    subagent_type: str,
) -> dict[str, Any]:
    """Run the subagent's own Streamer loop. Returns Streamer result dict."""
    # 1. 工作区：先取（身份提示要用）
    workspace = await parent._get_workspace_path()

    # 2. 消息：身份 + 项目共享层 + 任务（全新上下文，无父私有记忆/历史）
    identity = _subagent_identity(
        parent, description, timeout_s, subagent_type, workspace
    )
    project_ctx = None
    try:
        memory = getattr(parent, "_memory", None)
        if memory is not None:
            project_ctx = await memory.build_project_context(parent.project_id)
    except Exception:
        project_ctx = None
    messages: list[dict] = [{"role": "system", "content": identity}]
    if project_ctx:
        messages.append(
            {"role": "system", "content": f"## Project Constitution (Shared)\n{project_ctx}"}
        )
    messages.append({"role": "user", "content": prompt})

    # 3. 模型：父的模型配置
    model_config = await parent._get_model_config()
    if not model_config:
        return {"status": "error", "error": "no model config available"}

    # 4. 工具：父 defs ∩ 类型白名单 − spawn_subagent（深度 1 硬门）。
    #    父 defs 已按父权限过滤，故白名单天然不越权；执行时权限硬门不变
    #    （转发父 agent_id）。白名单是唯一权威——未显式加入的工具不出现。
    whitelist = _SUBAGENT_TYPE_TOOLS[subagent_type]
    defs = await parent._get_tool_definitions()
    tools = [
        t for t in defs
        if t.get("function", {}).get("name") in whitelist
        and t.get("function", {}).get("name") != "spawn_subagent"
    ]

    # 5. 项目根（用于工具执行回退路径）
    project_root = None
    try:
        from hiveweave.db import meta as meta_db
        project_root = await meta_db.get_project_workspace(parent.project_id)
    except Exception:
        pass

    # 6. 执行器：复用父的 ToolExecutor（权限继承 = 同一个 permission 实例）。
    #    子代理流用合成 agent_id（避免 poll-gate 计数/遥测污染父），
    #    但工具执行始终转发父的 agent_id（权限/硬门按父身份评估）。
    executor = parent._tool_executor
    sub_id = f"sub-{parent.id}-{uuid.uuid4().hex[:8]}"
    holder: dict[str, dict[str, str]] = {}
    on_tool_call = _subagent_on_tool_call(
        parent, executor, workspace, project_root, holder, whitelist
    )

    streamer = Streamer(max_tool_rounds=SUBAGENT_MAX_TOOL_ROUNDS)
    try:
        result = await asyncio.wait_for(
            streamer.stream(
                agent_id=sub_id,
                messages=messages,
                model_config=model_config,
                tools=tools,
                on_tool_call=on_tool_call,
                max_tool_rounds=SUBAGENT_MAX_TOOL_ROUNDS,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return {
            "status": "error",
            "error": f"subagent timed out after {timeout_s}s",
        }
    except Exception as e:  # 网络/熔断等 — 转 err，不炸父
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # Token metering (F4): 子代理的 LLM usage 归属到父代理
    # （token 由父的模型配置/账户消耗），request_type="subagent" 标记来源，
    # run/task 沿用父的当前上下文，便于成本归因。best-effort 不阻塞。
    rounds = result.get("usage_rounds") or []
    if rounds:
        try:
            from hiveweave.services.token_meter import token_meter
            await token_meter.record_rounds(
                agent_id=parent.id,
                project_id=parent.project_id,
                run_id=getattr(parent, "_current_run_id", None),
                task_id=getattr(parent, "_current_task_id", None),
                rounds=rounds,
                model_id=model_config.get("model_id"),
                provider=model_config.get("provider_type"),
                request_type="subagent",
            )
        except Exception as meter_err:
            log.warning("subagent_token_meter_failed",
                        parent_id=parent.id, error=str(meter_err))

    if result.get("status") != "ok":
        return result
    text = (result.get("content") or "").strip()
    # 附加 commit 摘要（若有）— 只读本子代理自己的 holder，与其他 spawn 隔离
    for tr in holder.values():
        if tr.get("phase") != "in_progress":
            text = f"{text}\n\n[commit] {tr.get('phase')}: {tr.get('summary')}"
            break
    return {**result, "content": text}


def _subagent_on_tool_call(
    parent: Any,
    executor: Any,
    workspace: str,
    project_root: str | None,
    holder: dict[str, dict[str, str]] | None = None,
    whitelist: frozenset[str] | None = None,
):
    """Build the subagent's tool-call callback.

    commit_turn 被本地拦截：写入本回调独用的 holder（未传则自建，与任何
    其他 spawn 隔离），返回 end_turn=True —— 绝不碰父的 turn_session /
    work_log / 门禁。其余工具转发给 ToolExecutor，agent_id 用父的（权限继承）。

    深度防御：whitelist 非空时，白名单外的 tool_name 直接拒绝（不落
    executor）。防止 LLM 幻觉/注入白名单外工具被父权限执行越权。
    """
    if holder is None:
        holder = {}

    async def callback(tool_name: str, arguments: str, tool_call_id: str) -> dict:
        if tool_name == "commit_turn":
            return await _subagent_commit(arguments, tool_call_id, holder)

        # 深度防御：白名单外的工具直接拒绝，不转发给 executor
        if whitelist is not None and tool_name not in whitelist:
            return {
                "role": "tool",
                "content": (
                    f"[Tool Error] {tool_name} is not in the subagent tool "
                    f"whitelist; subagent may only use whitelisted tools"
                ),
                "tool_call_id": tool_call_id,
            }

        try:
            tool_args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            tool_args = {}
        try:
            result = await executor.execute(
                parent.id,
                tool_name,
                tool_args,
                workspace,
                project_root,
            )
        except Exception as e:
            return {
                "role": "tool",
                "content": f"[Tool Error] {tool_name}: {type(e).__name__}: {e}",
                "tool_call_id": tool_call_id,
            }
        content = result.get("output") or result.get("error") or "(empty)"
        return {
            "role": "tool",
            "content": str(content),
            "tool_call_id": tool_call_id,
        }

    return callback


async def _subagent_commit(
    arguments: str, tool_call_id: str, holder: dict[str, dict[str, str]]
) -> dict:
    """Minimal local commit for the subagent (no gates, no persistence)."""
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        args = {}
    phase = str(args.get("phase") or "done_slice")
    summary = str(args.get("summary") or "").strip()
    if not summary:
        return {
            "role": "tool",
            "content": (
                "commit_turn rejected: summary required. "
                "Provide a summary of what you did."
            ),
            "tool_call_id": tool_call_id,
            "end_turn": False,
        }
    holder[tool_call_id] = {
        "phase": phase,
        "summary": summary[:2000],
    }
    return {
        "role": "tool",
        "content": (
            f"STOP: TurnResult committed (phase={phase}). "
            "Do NOT call any more tools. Your final text will be returned "
            "to the parent."
        ),
        "tool_call_id": tool_call_id,
        "end_turn": True,
    }
