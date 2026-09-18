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
- 本工具立即返回 waiting_on；完成后 inbox 三类回执叫醒父：
  ``[SUBAGENT DONE]``（跑完了）/ ``[SUBAGENT DONE_TRUNCATED]``（**轮次预算
  切断，没跑完、产出可能没落盘 ⇒ 父须先验货**）/ ``[SUBAGENT FAILED]``。
- 只记结果：子代理过程不落库。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from hiveweave.llm.retry import compute_backoff
from hiveweave.llm.streamer import Streamer
from hiveweave.services.run_ledger import run_ledger as _rl
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

SUBAGENT_TIMEOUT_S = 240  # per-subagent explicit deadline; turn budget always on
SUBAGENT_MAX_TIMEOUT_S = 480
SUBAGENT_MAX_TOOL_ROUNDS = 100  # 与 run_ledger 默认 budget_tool_calls 一致

# 45 轮 P1「子代理断流空等」：75s stream idle 帽在 streamer 内部转为 error
# dict 返回（不抛异常），旧代码原样透传 → 子代理一次 idle 死即 [SUBAGENT
# FAILED] 终局，父级只能空等到 wait TTL。upstream 类错误现在由
# _run_subagent 的 attempt 循环自动退避重试。上限很小：重试窗（每次 ≤90s
# idle 窗 + 秒级退避）必须远小于父 wait TTL（15min）与 zombie 帽（300s）。
_SUBAGENT_STREAM_RETRIES = 2
try:
    _SUBAGENT_STREAM_RETRIES = max(
        0, int(os.getenv("HIVEWEAVE_SUBAGENT_STREAM_RETRIES", "2"))
    )
except ValueError:
    pass

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
    "bash", "bash_main", "run_command", "run_tests", "browse", "browse_main",
    "game_run_case", "game_run_case_main", "assert_visual",
    "claim_task", "submit_task", "update_task_status",
})

_SUBAGENT_WRITE_EXTRA = frozenset({
    # 写码 + 自测自改闭环（bash/run_tests）+ 任务流转 + git_worktree 全套
    # 不含 browse（视觉 QA 归 audit）、不含 attest（出证据归父）
    "write_file", "edit_file", "apply_patch",
    "create_directory", "delete_file", "delete_directory", "move_file",
    "bash", "bash_main", "run_command", "run_tests",
    "git_worktree_status", "git_worktree_checkpoint",
    "git_worktree_list",  # merge/remove stay parent-owned (not subagent)
    "claim_task", "submit_task", "update_task_status",
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
            f"{SUBAGENT_MAX_TIMEOUT_S}). Omit or 0: no *separate* subagent "
            "clock — but the turn still hits the platform turn budget "
            "(~9-10 min hard cap with graceful checkpoint). Finish with "
            "commit_turn; the parent can job_kill you. Work expected to "
            "outlive a deadline should be dispatched via dispatch_task "
            "instead."
        ),
        json_schema_extra={"aliases": ["timeout", "timeoutSeconds"]},
    )


@tool(
    "spawn_subagent",
    "Delegate a self-contained task to a subagent in its own context "
    "(it does not see this conversation). Each spawn returns its own "
    "waiting_on entry — batch ALL pending entries (spawns + background bash) "
    "into ONE commit_turn(phase=waiting, waiting_on=[...]). Woken with "
    "[SUBAGENT DONE], [SUBAGENT FAILED], or [SUBAGENT DONE_TRUNCATED]. "
    "DONE_TRUNCATED means the child hit the turn budget before finishing — "
    "its output is UNVERIFIED (the work may not have landed): check the "
    "worktree before relying on it. The subagent works in YOUR worktree "
    "with YOUR permissions, returns its result not intermediate steps, and "
    "must commit_turn before finishing. Give a complete standalone prompt. "
    "subagent_type is REQUIRED: 'readonly' (read-only scout), 'audit' (run "
    "tests/browse + submit — no attestation), or 'write' (edit code + "
    "git_worktree; requires parent SOURCE_WRITE). Multiple write subagents "
    "share YOUR worktree: concurrent writes to the same files collide — "
    "partition files or run them sequentially. Do not nest this work inside "
    "the current LLM turn.",
    requires_workspace=False,
    security_level="standard",
)
async def spawn_subagent_tool(
    params: SpawnSubagentParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Start a subagent off-turn and return waiting_on immediately."""
    from hiveweave.agents.supervisor import agent_manager
    from hiveweave.services.offturn import (
        OFFTURN_STATE,
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

    # P2-5：**在这里**（父 turn 确定活跃）快照 run 上下文 —— 子代理
    # off-turn 执行时父 turn 已收口、属性会被清空；若在 _work 内部读会
    # 得到 None（审计实证竞态：start_offturn_job 经 create_task 延迟调度，
    # 可能晚于父 commit_turn(waiting) 收口），步骤记录又静默跳过。
    snap_run_id = getattr(parent, "_current_run_id", None)
    snap_counter = getattr(parent, "_run_step_counter", 0)

    async def _work() -> tuple:
        result = await _run_subagent(
            parent,
            prompt,
            params.description,
            timeout_s,
            subagent_type,
            workspace=resolved_ws,
            snap_run_id=snap_run_id,
            snap_step_counter=snap_counter,
        )
        if result.get("status") != "ok":
            return False, str(result.get("error") or "unknown error")
        # P0-1：父在等的是「这批活干完没有」。子代理的 LLM 轮次被预算切断时
        # streamer 返回的是 `status=ok + budget_exhausted=True`（内容仍是
        # ok，所以落盘与否**未知**）——若在这里折成 `True`，offturn 会照样
        # 打 `[SUBAGENT DONE]`，父以为完成、不再验货（TEST_DSH_60 实测 7 条
        # 回执里 3 条如此）。
        # ⚠ 终态必须**显式返回**：既不许读 `text` 里的前缀（那是子代理的自由
        # 文本，据此判终态 = 用文案推断意图，用户 09-14 钦定禁用），也不许
        # 绕到 payload 的 `id()` 上去猜（裸地址，会被复用而误判）。
        text = str(result.get("content") or "(subagent returned no text)")
        if result.get("budget_exhausted"):
            return (
                True,
                f"{text}\n\n"
                "[SUBAGENT TRUNCATED] The turn budget was exhausted before this "
                "child finished — the output above may be incomplete and its work "
                "may not have landed. VERIFY before relying on it.",
                OFFTURN_STATE.SUBAGENT_DONE_TRUNCATED,
            )
        return True, text

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
        "You will be woken with [SUBAGENT DONE], [SUBAGENT FAILED], or "
        "[SUBAGENT DONE_TRUNCATED] (it hit the turn budget before finishing — "
        "verify its work before relying on it). "
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
            "No separate subagent clock, but the platform turn budget "
            "(~9-10 min hard cap) still applies and checkpoints your work. "
            "Finish with commit_turn; the parent can job_kill you. Budget "
            "your tool calls: watch the 'remaining calls' warning."
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
    # 45 轮 P0 旁证：子代理拿不到 executor 剧本的方言段（coordinator 生的
    # 子代理 3 次方言失败）——身份是子代理唯一的系统提示，pwsh 宿主上一律
    # 注入，与子代理类型无关（bash 系工具在各类型白名单里都可能出现）。
    # Git Bash 原生宿主（沙箱 off）gate 闭口，方言段注入也随之关闭。
    try:
        from hiveweave.prompts.executor import _SHELL_DIALECT_SECTION
        from hiveweave.tools.bash import _pwsh_is_effective_shell

        if _pwsh_is_effective_shell():
            # 子代理适配注：各类型白名单无 pwsh 工具、readonly 无写工具——
            # bash 工具命令原样交 pwsh 执行，PowerShell 写法直接写进 bash。
            lines.append(
                _SHELL_DIALECT_SECTION
                + "\n\n（子代理附注：上文提到的工具若不在你的工具列表里，"
                "忽略对应句；PowerShell 写法直接写进 bash 工具即可。）"
            )
    except Exception as e:
        log.debug("subagent_dialect_section_skip", error=str(e))
    return "\n".join(lines)


#: 上游/环境类失败关键字（小写匹配）—— 只做事实位判定，不是重分类器。
#: 命中 = 环境瞬断（原样重试常可成）；未命中但有 reason = 子侧逻辑/配置。
#: #13 批 B（2026-09-18）：本地 **15 词**表**收编**到
#: ``llm/retry.UPSTREAM_STREAM_ERROR_KEYWORDS``（唯一登记点 / F9-C；
#: 集合完全一致 ⇒ 判定零变化，仅消掉「每处各列一份清单」的第二份）。
from hiveweave.llm.retry import (
    UPSTREAM_STREAM_ERROR_KEYWORDS as _UPSTREAM_FAILURE_KEYWORDS,
)
#: HTTP error_status 的量程归类（读到什么用什么，不发明新桶）。
_UPSTREAM_ERROR_STATUS = {429, 500, 502, 503, 504, 529}
_LOGIC_ERROR_STATUS = {400, 401, 402, 403, 404, 409, 422}


def _subagent_failure_class(
    reason: str | None, error_status: int | None = None
) -> str:
    """子代理失败分类事实位：upstream（环境瞬断）vs logic（子侧问题）。

    42 轮实证（dsh42 青岩×2/方糖、s3c09 潮汐）：错误恒为「subagent
    failed」，父无法区分「上游瞬断可原样重试」与「子逻辑错误需改 prompt」。
    本判定只读既有事实（子 run 的 error_status + reason 关键字），
    证据不足返回 unknown，不臆断。
    """
    if error_status is not None:
        try:
            es = int(error_status)
        except (TypeError, ValueError):
            es = None
        if es is not None:
            if es in _UPSTREAM_ERROR_STATUS:
                return "upstream"
            if es in _LOGIC_ERROR_STATUS:
                return "logic"
    text = (reason or "").strip().lower()
    if not text:
        return "unknown"
    if any(k in text for k in _UPSTREAM_FAILURE_KEYWORDS):
        return "upstream"
    return "logic"


async def _record_subagent_step(
    parent: Any,
    status: str = "completed",
    *,
    run_id: str | None = None,
    counter: int | None = None,
    reason: str | None = None,
    error_status: int | None = None,
    child_status: str | None = None,
) -> None:
    """P1-6/P2-5：子代理在父 run 的 run_steps 留一条步骤。

    spawn_subagent 是 **off-turn**（父 turn 立即返回 waiting_on，子代理在
    后台执行）——执行时 ``parent._current_run_id`` 已被父 turn 收口清空，
    若直接读 parent 属性会静默跳过（R2 P2-5 实锤：线上 0 条子代理步骤）。
    因此由 spawn 侧在父 turn 活跃时**快照** run_id/counter，子代理完成后
    用模块级 ``run_ledger`` 单例落账（不依赖父对象存活）。
    best-effort 不阻塞子代理返回。

    失败时（status != "completed"）：
    - ``reason``: 子 run 的失败原因原文（error 文案 / 异常），截 200 字符；
    - ``error_status``: 子 run 的 HTTP error_status（上游/逻辑分类用）；
    - ``child_status``: 子 run 终止 status（如 streamer 的 "error"）。
    """
    if run_id is None:
        run_id = getattr(parent, "_current_run_id", None)
    if not run_id:
        return
    try:
        idx = counter if counter is not None else getattr(parent, "_run_step_counter", 0)
        if counter is not None:
            # 快照路径：计数器由 spawn 侧递增，避免回写已收口父对象
            pass
        step_id = await _rl.record_step_start(
            getattr(parent, "id", None) or getattr(parent, "short_id", None),
            run_id,
            idx,
            "subagent",
            tool_name="spawn_subagent",
        )
        if step_id:
            # 审计 2026-09-12（report TEST_DSH_54 #2 同族）：本步骤在子代理**跑完
            # 之后**才记录，也就是"这一步确实执行过"。必须在 INSERT 后立刻置
            # started=1 —— 否则进程若死在 INSERT 与 record_step_end 之间，孤儿
            # 清扫会把它判成 not_started（"从未执行、无副作用、可直接重试"），
            # 而子代理其实已经跑过、可能已产生副作用 ⇒ 副作用双发。
            await _rl.mark_step_started(
                getattr(parent, "id", None) or getattr(parent, "short_id", None),
                step_id,
            )
            # 42 轮 P1：失败不能只写「subagent failed」——带子 run 终止
            # status/reason（截 200 字符防泄漏/超长）+ 上游/逻辑分类位，
            # 父代理据此区分「环境瞬断可原样重试」与「子逻辑错误需改 prompt」。
            error = None
            if status != "completed":
                reason_text = (reason or "unknown reason").strip()
                # 分类用原始 reason（_subagent_failure_class None-safe）：
                # 兜底文案「unknown reason」只做展示、不参与分类 —— 否则
                # 「证据不足」被臆断成 logic，误导父代理改 prompt 而非重试
                # （审计 P1：docstring 承诺 unknown 不臆断）。
                failure_class = _subagent_failure_class(reason, error_status)
                error = (
                    f"subagent failed (status={child_status or status}, "
                    f"class={failure_class}, reason={reason_text[:200]})"
                )
            await _rl.record_step_end(
                getattr(parent, "id", None) or getattr(parent, "short_id", None),
                step_id,
                status=status,
                error=error,
            )
    except Exception:
        pass  # best-effort


async def _run_subagent(
    parent: Any,
    prompt: str,
    description: str | None,
    timeout_s: float | None,
    subagent_type: str,
    workspace: str | None = None,
    snap_run_id: str | None = None,
    snap_step_counter: int | None = None,
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
    # P1-6：子代理 usage 与父共享 sink —— 子代理 token 归父账户；父被取消时
    # 子代理协程一并中断（自身 record_rounds 不会执行），父 flush 兜住。
    # L4（2026-09-11）：sink 是唯一权威源（result 里的 usage_rounds 已退役）。
    # 子代理与父**共用**同一个 sink，但要按 `request_type="subagent"` 单独归属
    # ⇒ 记下进入时的下标，结束时切片取「本子代理新增的那一段」——
    # 这样父的 main 段落与子的 subagent 段落互不重叠、不会双计。
    if getattr(parent, "_pending_usage", None) is None:
        parent._pending_usage = []
    _sink_base = len(parent._pending_usage)

    def _new_stream_coro():
        # 39 审计 P1-1 修复的姊妹语义（对齐主 agent 的 auto-retrigger）：
        # 协程只能 await 一次——重试必须重建。_ssl 风暴期 LLM 重试耗尽会把
        # 整个窗口吃光，单次超时即 FAILED 对子代理是"一次失败即终局"。
        return streamer.stream(
            agent_id=sub_id,
            messages=messages,
            model_config=model_config,
            tools=tools,
            on_tool_call=on_tool_call,
            max_tool_rounds=SUBAGENT_MAX_TOOL_ROUNDS,
            usage_sink=parent._pending_usage.append,
        )

    # ── attempt 循环 ──────────────────────────────────────────────
    # 两类可原样重试的失败，统一在一个循环里收口：
    # 1. 显式墙钟超时（wait_for TimeoutError）→ 39 审计的 retry-once，
    #    语义保持（重试一次、无退避等待、终局文案不变）；
    # 2. stream error 且分类=upstream（45 轮 P1 新增）：75s idle 帽抛
    #    PermanentError 后 streamer 返回 error dict（core.py 不抛异常），
    #    旧代码直接透传 → 一次 idle 死即终局（主循环同错误走 recovery
    #    checkpoint+冷却自动整轮续跑 —— 重试不对称的精确位置）。退避用
    #    llm/retry.compute_backoff（秒级起 ±25% jitter）。
    # HTTP 层异常（connect/SSL 抛出路径）不在此重试——retry.py 已做 5 层。
    result: dict[str, Any] = {}
    stream_retries = 0
    timeout_retried = False
    while True:
        timed_out = False
        try:
            if timeout_s is None or timeout_s <= 0:
                result = await _new_stream_coro()
            else:
                result = await asyncio.wait_for(
                    _new_stream_coro(), timeout=timeout_s
                )
        except asyncio.TimeoutError:
            timed_out = True
        except Exception as e:  # noqa: BLE001 — 网络/熔断等，转 err 不炸父
            infra_err = f"{type(e).__name__}: {e}"
            if timeout_retried or stream_retries:
                infra_err = f"subagent retry failed: {e}"
                reason = f"retry failed: {e}"
            else:
                reason = infra_err
            await _record_subagent_step(
                parent, status="failed",
                run_id=snap_run_id, counter=snap_step_counter,
                reason=reason,
            )
            return {"status": "error", "error": infra_err}

        if not timed_out and result.get("status") == "ok":
            break

        if timed_out:
            err_text = f"timed out after {timeout_s:g}s"
            # 两类重试互斥（审计 M1）：串连会让显式超时路径的最坏墙钟涨到
            # ~3×timeout_s（480s 档 ≈24min > 父 wait TTL 15min）。互斥后
            # 上界回到旧水平（2×timeout_s 或 1+N 个 idle 窗）。
            can_retry = not timeout_retried and not stream_retries
        else:
            err_text = str(result.get("error") or "")
            can_retry = (
                not timeout_retried
                and stream_retries < _SUBAGENT_STREAM_RETRIES
                and _subagent_failure_class(
                    err_text, result.get("error_status")
                )
                == "upstream"
            )
        if not can_retry:
            break

        if timed_out:
            timeout_retried = True
            delay_s = 0.0
            log.warning(
                "subagent_timeout_retry_once",
                sub_id=sub_id, timeout_s=timeout_s,
            )
        else:
            stream_retries += 1
            delay_ms = compute_backoff(stream_retries)  # 2s, 4s, …
            delay_s = delay_ms / 1000.0
            log.warning(
                "subagent_stream_retry",
                sub_id=sub_id, attempt=stream_retries,
                delay_ms=delay_ms, upstream_error=err_text[:160],
            )
            await asyncio.sleep(delay_s)

    if timed_out:
        # 显式超时且 retry-once 仍超时 —— 终局（文案保持 39 审计语义）
        await _record_subagent_step(
            parent, status="failed",
            run_id=snap_run_id, counter=snap_step_counter,
            reason=(
                f"timed out after {timeout_s:g}s "
                "(auto-retried once; upstream gateway likely degraded)"
            ),
            child_status="timeout",
        )
        return {
            "status": "error",
            "error": (
                f"subagent timed out after {timeout_s:g}s "
                "(auto-retried once; upstream gateway likely degraded — "
                "check provider/proxy, then re-spawn)"
            ),
        }

    if stream_retries:
        if result.get("status") == "ok":
            log.info(
                "subagent_stream_retry_recovered",
                sub_id=sub_id, attempts=stream_retries,
            )
        else:
            # 失败回执带上重试事实：父看到的 [SUBAGENT FAILED] 不再把
            # 「已自动重试过 N 次」的 upstream 死当首撞处理。
            result = {
                **result,
                "error": (
                    f"{result.get('error') or 'unknown error'} "
                    f"(auto-retried {stream_retries}x on upstream-class error)"
                ),
            }

    # Token metering (F4): 子代理的 LLM usage 归属到父代理
    # （token 由父的模型配置/账户消耗），request_type="subagent" 标记来源，
    # run/task 沿用父的当前上下文，便于成本归因。best-effort 不阻塞。
    # L4：取共享 sink 里**本子代理新增的那一段**（切片而非 result 键）。
    rounds = list(parent._pending_usage[_sink_base:])
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
        else:
            # L4：已自行落库的轮次**从共享 sink 里摘掉** —— 否则父 attempt
            # 失败重试时会把这一段当自己的账再记一遍（双计）。
            # 按**元素身份**摘（不是按下标切片）：`await record_rounds` 会让出
            # 控制权，期间可能有别的协程往同一个 sink 里追加，下标会漂。
            _delivered_ids = {id(r) for r in rounds}
            parent._pending_usage = [
                r for r in parent._pending_usage if id(r) not in _delivered_ids
            ]

    # P1-6/P2-5：子代理步骤留痕（父 run 的 run_steps，off-turn 用快照落账）
    child_ok = result.get("status") == "ok"
    await _record_subagent_step(
        parent,
        status="completed" if child_ok else "failed",
        run_id=snap_run_id,
        counter=snap_step_counter,
        # 原始 error 透传（缺失则 None）：分类是事实位判定，提前伪造
        # 「unknown error」会把 unknown 类污染成 logic（审计 P1）；
        # 展示兜底由 _record_subagent_step 统一加。
        reason=(
            None if child_ok or not result.get("error")
            else str(result["error"])
        ),
        error_status=None if child_ok else result.get("error_status"),
        child_status=None if child_ok else str(result.get("status") or "error"),
    )

    if result.get("status") != "ok":
        return result
    text = (result.get("content") or "").strip()
    # P0-1（TEST_DSH_60）：预算切断**显式穿透**到 _work() 的终态判定。
    # 这里只是把 streamer 已给出的结构化事实位原样带上（不新增判据、不看
    # 文案）——`result["content"]` 里那句 "[TURN BUDGET] Hard turn budget
    # exhausted" 届时**只是旁证**，机器判定一律走本字段。
    if result.get("budget_exhausted"):
        text = (
            f"{text}\n\n"
            "[SUBAGENT TRUNCATED] The turn budget was exhausted before this "
            "child finished — the output above may be incomplete and its work "
            "may not have landed. VERIFY before relying on it."
        )
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
