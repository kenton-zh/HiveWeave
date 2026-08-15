"""spawn_subagent 工具 — off-turn 子代理。

子代理：
- 全新上下文：身份提示 + 项目共享层（build_project_context）+ 任务描述（用户消息）。
- 权限完全继承父：工具调用转发父的 agent_id 给 ToolExecutor；深度 1（工具列表
  去掉 spawn_subagent 本身）。
- 独立预算：max_tool_rounds = SUBAGENT_MAX_TOOL_ROUNDS（父默认 budget_tool_calls），
  rounds 80% 警告按 streamer 既有机制触发；不扣父 run ledger 计数。
- commit_turn 被本地拦截（不写父的 turn_session / work_log / 门禁 / lessons），
  返回 end_turn=True 结束子代理工具循环。
- 默认无墙钟。可选 ``timeout_s`` 才套在子代理
  自己的 Streamer 上；**不**顺延父 safety timer / 不嵌进 streamer HARD 570。
- 本工具立即返回 waiting_on；完成后 inbox ``[SUBAGENT DONE|FAILED]`` 叫醒父。
- 只记结果：子代理过程不落库。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from hiveweave.llm.streamer import Streamer
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

SUBAGENT_TIMEOUT_S = 240  # opt-in only; default spawn has no wall clock
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
            "The complete, self-contained task. The child does not "
            "share this conversation, so include files, goals, and "
            "acceptance criteria."
        ),
        json_schema_extra={"aliases": ["task", "instructions", "work"]},
    )
    description: str | None = Field(
        default=None,
        description="Short (3-5 word) label for the waiting context.",
        json_schema_extra={"aliases": ["desc", "title"]},
    )
    timeout_s: int | None = Field(
        default=None,
        description=(
            "Optional hard deadline in seconds (max "
            f"{SUBAGENT_MAX_TIMEOUT_S}). Omit or 0: no session wall clock "
            "(stop on commit_turn, job_kill, or stream idle). "
            "Work expected to outlive a deadline should be dispatched via "
            "dispatch_task instead."
        ),
        json_schema_extra={"aliases": ["timeout", "timeoutSeconds"]},
    )


@tool(
    "spawn_subagent",
    "Delegate a self-contained task to a subagent in its own context "
    "(it does not see this conversation). Returns immediately with waiting_on — "
    "then commit_turn(phase=waiting) using that list. Woken with "
    "[SUBAGENT DONE] / [SUBAGENT FAILED]. The subagent works in YOUR worktree "
    "with YOUR permissions, returns its result not intermediate steps, and "
    "must commit_turn before finishing. Give a complete standalone prompt. "
    "subagent_type is REQUIRED: 'readonly' (read-only scout), 'audit' (run "
    "tests/browse + submit — no attestation), or 'write' (edit code + "
    "git_worktree; requires parent SOURCE_WRITE). Concurrent writes to the "
    "same files will collide. Do not nest this work inside the current LLM turn.",
    requires_workspace=False,
    security_level="standard",
)
async def spawn_subagent_tool(
    params: SpawnSubagentParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Start a subagent off-turn and return waiting_on immediately."""
    from hiveweave.agents.supervisor import agent_manager
    from hiveweave.services.offturn import (
        build_waiting_on,
        next_action_waiting,
        resolve_assignee_task_id,
        start_offturn_job,
    )

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

    # write 可用性：父必须具备写 worktree（executor / builder coordinator）。
    # AgentManager 重启后 config 只有 role_type（SQL alias），须 remap。
    if subagent_type == "write" and not _parent_has_source_write(parent):
        return ToolResult.err(
            "subagent_type='write' requires a code-writing parent with a "
            "write worktree (SOURCE_WRITE + executor/builder coordinator); "
            "CEO/HR may only use readonly/audit"
        )

    prompt = (params.prompt or "").strip()
    if not prompt:
        return ToolResult.err("spawn_subagent requires a non-empty prompt")
    raw_timeout = params.timeout_s
    if raw_timeout is None or int(raw_timeout) <= 0:
        timeout_s: float | None = None
    else:
        timeout_s = max(1, min(int(raw_timeout), SUBAGENT_MAX_TIMEOUT_S))

    resolved_ws = workspace or ""
    try:
        got = await parent._get_workspace_path()
        if got:
            resolved_ws = str(got)
    except Exception:
        pass

    if subagent_type == "write":
        deny_main = await _write_spawn_main_deny(parent, resolved_ws)
        if deny_main:
            return ToolResult.err(deny_main)

    async def _work() -> tuple[bool, str]:
        result = await _run_subagent(
            parent,
            prompt,
            params.description,
            timeout_s,
            subagent_type,
            workspace=resolved_ws,
        )
        if result.get("status") != "ok":
            return False, str(result.get("error") or "unknown error")
        return True, str(result.get("content") or "(subagent returned no text)")

    project_id = str(getattr(parent, "project_id", "") or "")
    task_id = await resolve_assignee_task_id(project_id, agent_id)
    job_id = start_offturn_job(
        kind="subagent",
        agent_id=agent_id,
        project_id=project_id,
        worktree=resolved_ws,
        work=_work,
        task_id=task_id,
    )
    waiting_on = build_waiting_on(job_id, task_id, agent_id=agent_id)
    return ToolResult.ok(
        "Subagent started off the org turn "
        f"(job={job_id}, type={subagent_type}). "
        f"{next_action_waiting(waiting_on)} "
        "You will be woken with [SUBAGENT DONE] or [SUBAGENT FAILED]. "
        "Continue the org turn; do not nest this work in the current LLM call.",
        job_id=job_id,
        waiting_on=waiting_on,
        task_id=task_id,
        subagent={"timeout_s": timeout_s, "type": subagent_type},
    )


def _parent_config_for_write_gate(parent: Any) -> dict:
    """Config for write-worktree gate.

    After restart, AgentManager SQL aliases ``permission_type AS role_type``
    so live agents often lack ``permission_type``. Copy the alias rather
    than inventing executor/coordinator — CEO stored as ``ceo`` must stay
    fail-closed.
    """
    config = dict(getattr(parent, "config", None) or {})
    if not str(config.get("permission_type") or "").strip():
        role_type = str(config.get("role_type") or "").strip()
        if role_type:
            config["permission_type"] = role_type
    return config


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

    return agent_gets_write_worktree(_parent_config_for_write_gate(parent))


async def _write_spawn_main_deny(parent: Any, workspace: str) -> str | None:
    """Fail closed: write spawn must run in the agent's write worktree.

    Same fail-closed shape as other write-worktree gates: missing/unresolvable
    or a MAIN / MAIN-subdirectory path is denied. Do not fail open.
    """
    ws = (workspace or "").strip()
    if not ws:
        return (
            "subagent_type='write' requires the parent's write worktree, "
            "not an empty/MAIN workspace"
        )
    try:
        from hiveweave.db import meta as meta_db

        root = await meta_db.get_project_workspace(parent.project_id)
    except Exception:
        return (
            "subagent_type='write' requires a project root so MAIN can be "
            "refused (missing project workspace)"
        )
    if not root:
        return (
            "subagent_type='write' requires a project root so MAIN can be "
            "refused (missing project workspace)"
        )
    try:
        ws_res = Path(ws).resolve()
        root_res = Path(root).resolve()
    except OSError:
        return "subagent_type='write' worktree path is not resolvable"
    if ws_res == root_res:
        return (
            "subagent_type='write' requires the parent's write worktree, "
            "not project MAIN"
        )
    trees = (root_res / ".hiveweave" / "worktrees").resolve()
    try:
        ws_res.relative_to(trees)
        under_trees = ws_res != trees
    except ValueError:
        under_trees = False
    if not under_trees:
        return (
            "subagent_type='write' must run in the agent's write worktree "
            f"({trees}), not a MAIN subdirectory"
        )
    return None


def _subagent_identity(
    parent: Any,
    description: str | None,
    timeout_s: float | None,
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
        "helping your parent get work done — you are a hands, not a planner.",
        f"The parent continues its org turn. Task: {description or '(see user message)'}",
        "Your result is delivered off-turn via [SUBAGENT DONE] / "
        "[SUBAGENT FAILED]; the parent is not blocked waiting inside its LLM call.",
        "You have exactly the same permissions as your parent (no more). "
        "You work inside the parent's workspace. You CANNOT spawn subagents. "
        "bash(background=true) is parent-only — run bash in the foreground here.",
        (
            f"You MUST finish and call commit_turn within {int(timeout_s)}s "
            "(the platform enforces this deadline — it will kill you). "
            "Budget your tool calls: watch the 'remaining calls' warning."
            if timeout_s
            else
            "No session wall clock. Finish with commit_turn; the parent can "
            "job_kill you. Budget your tool calls: watch the 'remaining calls' "
            "warning."
        ),
        "If the slice is too large, stop, commit_turn with phase=blocked "
        "and waiting_on the parent, and tell the parent to dispatch_task "
        "for async delegation instead.",
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
    timeout_s: float | None,
    subagent_type: str,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Run the subagent's own Streamer loop. Returns Streamer result dict."""
    # 1. 工作区：优先用 spawn 时已校验的路径（write 已拒绝 MAIN）
    if not (workspace or "").strip():
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
    stream_coro = streamer.stream(
        agent_id=sub_id,
        messages=messages,
        model_config=model_config,
        tools=tools,
        on_tool_call=on_tool_call,
        max_tool_rounds=SUBAGENT_MAX_TOOL_ROUNDS,
    )
    try:
        if timeout_s is None or timeout_s <= 0:
            result = await stream_coro
        else:
            result = await asyncio.wait_for(stream_coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        return {
            "status": "error",
            "error": f"subagent timed out after {timeout_s:g}s",
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
        if tool_name == "bash" and _args_want_background(tool_args):
            return {
                "role": "tool",
                "content": (
                    "bash(background=true) is not available inside a subagent. "
                    "Run bash in the foreground here, or let the parent use "
                    "background=true off the org turn."
                ),
                "tool_call_id": tool_call_id,
            }
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
            # H3 透传：子代理路径的工具结果同样要带 success + blocked 标记。
            # 缺 success 时 tool_exec 的 error_ids 恒空 → `error_ids and
            # blocked_ids >= error_ids` 短路，blocked 分流在此路径完全不生效
            # （审计 P1）——与 agents/streaming.py 的透传键对齐。
            "success": bool(result.get("success", True)),
            "blocked": bool(result.get("blocked")),
        }

    return callback


def _args_want_background(tool_args: dict) -> bool:
    """True when bash args request off-turn background (explicit param)."""
    v = tool_args.get("background", tool_args.get("bg"))
    if v is None or v is False:
        return False
    if v is True:
        return True
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


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
