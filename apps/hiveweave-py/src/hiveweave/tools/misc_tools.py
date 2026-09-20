"""Misc tools: Git worktree, legacy task tools, message_user, webfetch.

Migrated from executor.py ``_tool_*`` methods and inline dispatch code to
``@tool``-registered standalone functions.

Tools:
    Git worktree:  git_worktree_create, git_worktree_list,
                   git_worktree_merge, git_worktree_remove,
                   git_worktree_status, git_worktree_sync,
                   git_worktree_checkpoint
    Other:         message_user, webfetch
"""

from __future__ import annotations

import asyncio
import base64
import html as html_mod
import ipaddress
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog

from pydantic import BaseModel, Field, ConfigDict, field_validator

from .base import tool
from .result import ToolResult
from .helpers import coerce_to_list, get_project_id
from hiveweave.util.tree_label import tree_relpath

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Section 1: Git worktree tools
# ═══════════════════════════════════════════════════════════════════════


async def _get_worktree_context(
    agent_id: str, ctx: Any = None
) -> tuple[str, str, str] | ToolResult:
    """Resolve workspace_path and short_id for git worktree operations.

    Returns ``(workspace_path, short_id, project_id)`` on success,
    or a ``ToolResult`` error on failure.
    """
    from hiveweave.db import meta as meta_db

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    ws_path = await meta_db.get_project_workspace(project_id)
    if not ws_path:
        return ToolResult.err(
            f"No workspace path for project {project_id}"
        )
    workspace_path = str(ws_path)

    # Resolve agent short_id for worktree naming
    short_id = agent_id[:8]
    if ctx and getattr(ctx, "org", None):
        agent_rec = await ctx.org.get_agent(agent_id)
        if agent_rec:
            short_id = agent_rec.get("short_id", agent_id[:8])
    else:
        try:
            from hiveweave.services.org import OrgService

            org = OrgService()
            agent_rec = await org.get_agent(agent_id)
            if agent_rec:
                short_id = agent_rec.get("short_id", agent_id[:8])
        except Exception:
            pass

    return workspace_path, short_id, project_id


# ── git_worktree_create ──────────────────────────────────


class GitWorktreeCreateParams(BaseModel):
    """Parameters for git_worktree_create tool."""

    model_config = ConfigDict(populate_by_name=True)

    branch_name: str = Field(
        alias="branchName",
        description=(
            "Branch/task name for the worktree. A unique branch will be "
            "generated from this name and the agent's short_id."
        ),
        json_schema_extra={
            "aliases": ["branchName", "branch_name", "branch", "name", "taskName", "task_name", "task"]
        },
    )
    base_branch: str | None = Field(
        default=None,
        alias="baseBranch",
        description="Base branch to create from (default: main).",
        json_schema_extra={"aliases": ["baseBranch", "base_branch", "base"]},
    )


@tool(
    "git_worktree_create",
    "DEPRECATED for agents: executor worktrees are created automatically on "
    "hire/dispatch. Coordinators must not create worktrees (especially not "
    "under their own short_id). Use git_worktree_merge after approve.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_create_tool(
    params: GitWorktreeCreateParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Create a git worktree — blocked for coordinators; prefer system ensure."""
    # Hard ban: coordinators must not own/create write worktrees
    try:
        from hiveweave.services.org import OrgService

        caller = await OrgService().resolve_agent(agent_id)
        perm = ((caller or {}).get("permission_type") or "").lower()
        if perm == "coordinator" or perm == "hr":
            return ToolResult.err(
                "git_worktree_create is disabled for coordinators/HR. "
                "Executor worktrees are auto-created on hire and dispatch "
                "(ensure_executor_worktree). You only git_worktree_merge after "
                "approve. Never create a worktree under CEO/A001/your short_id."
            )
    except Exception:
        pass

    # BUG-034: 防止嵌套 worktree — 如果 agent 已在 worktree 中，拒绝创建
    if ".hiveweave" in workspace and "worktrees" in workspace:
        return ToolResult.err(
            "You are already inside a worktree. Do NOT create nested worktrees. "
            "Write code directly in your current directory. "
            "Use git_worktree_checkpoint to save progress."
        )

    return ToolResult.err(
        "git_worktree_create is not available. "
        "Worktrees are provisioned automatically when an executor is hired "
        "or receives dispatch_task."
    )


# ── git_worktree_list ────────────────────────────────────


class GitWorktreeListParams(BaseModel):
    """Parameters for git_worktree_list tool."""

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "git_worktree_list",
    "List all active git worktrees with their branch names and paths.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_list_tool(
    params: GitWorktreeListParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """List all git worktrees."""
    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, _, _ = wt_ctx

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    result = await gwt.list(workspace_path)
    if result.get("success"):
        wts = result.get("worktrees", result.get("entries", []))
        if not wts:
            return ToolResult.ok("No active worktrees")
        lines = []
        for w in wts:
            abs_path = str(w.get("path") or "")
            rel = tree_relpath(abs_path) or "MAIN"
            sid = w.get("short_id") or "?"
            branch = w.get("branch") or "?"
            lines.append(f"{sid}: {branch} {rel}")
        return ToolResult.ok("\n".join(lines))
    return ToolResult.err(result.get("message", "Failed to list worktrees"))


# ── git_worktree_merge ───────────────────────────────────

# short_id 形状：ASCII 字母 + 2-4 位数字（A001/A066）。
# 不能用 str.isalnum() 判断 —— CJK 字符同样 isalnum，
# 短中文任务名（如"后端工程师"）会被误判为 short_id 走错查询路径。
_SHORT_ID_RE = re.compile(r"[A-Za-z]\d{2,4}")


def _parse_branch_list(branch_out: str) -> list[str]:
    """解析 `git branch --list` 输出为干净分支名列表。

    输出前缀规则：当前分支 "* name"，worktree 检出分支 "+ name"，其他 "  name"。
    此前用 lstrip("* ") 无法去掉 "+ " 前缀 → worktree 分支名被解析成
    "+ hw/A004/..."，git merge 拿这个名字直接失败，被误判成"假冲突"。
    """
    out: list[str] = []
    for line in (branch_out or "").strip().split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        if line.startswith(("* ", "+ ")):
            name = line[2:].strip()
        else:
            name = line.strip()
        if name:
            out.append(name)
    return out


class GitWorktreeMergeParams(BaseModel):
    """Parameters for git_worktree_merge tool."""

    model_config = ConfigDict(populate_by_name=True)

    branch_name: str = Field(
        alias="branchName",
        description="Branch/task name of the worktree to merge.",
        json_schema_extra={
            "aliases": ["branchName", "branch_name", "branch", "name", "taskName", "task_name", "task"]
        },
    )
    target_branch: str | None = Field(
        default=None,
        alias="targetBranch",
        description="Target branch to merge into (default: main).",
        json_schema_extra={"aliases": ["targetBranch", "target_branch", "target"]},
    )
    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description=(
            "Optional task ID. When given, the stable P0 branch "
            "hw/<shortId>/t-<taskId[:8]> of the task assignee is tried "
            "first (exact hit); falls back to legacy name resolution."
        ),
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )
    dry_run: bool = Field(
        default=False,
        alias="dryRun",
        description=(
            "Preflight (dry-run): when true, run ONLY the precondition checks "
            "(worktree health / dirty main / branch exists) and return the "
            "complete missing-items list. NO mutations: no merge, no "
            "worktree teardown, no auto-repair. Default false = real merge."
        ),
        json_schema_extra={"aliases": ["dryRun", "dry_run", "preflight", "check"]},
    )


async def _resolve_stable_task_branch(
    workspace_path: str, project_id: str, task_id: str
) -> tuple[str, str] | None:
    """P0 稳定命名解析：task_id → 任务 assignee → hw/<sid>/t-<taskid8> 精确命中。

    分支真实存在才返回 ``(branch, short_id)``；任何一步失败返回 None，
    调用方回落 legacy 解析（老 slug 命名分支依然可 merge）。
    """
    try:
        from hiveweave.services.git_worktree import _git, compute_branch_name
        from hiveweave.services.org import OrgService
        from hiveweave.services.task import TaskService

        task = await TaskService().get_task(project_id, task_id)
        assignee_id = (task or {}).get("assignee_id")
        if not assignee_id:
            return None
        agent = await OrgService().resolve_agent(str(assignee_id))
        short_id = (agent or {}).get("short_id")
        if not short_id:
            return None
        candidate = compute_branch_name(str(short_id), task_id)
        ok, out = await _git(["branch", "--list", candidate], workspace_path)
        if ok and candidate in _parse_branch_list(out):
            return candidate, str(short_id)
    except Exception as e:
        log.warning("merge_stable_branch_resolve_failed",
                    task_id=task_id, error=str(e))
    return None


async def _route_conflict_marker_cleanup(
    project_id: str,
    creator_id: str,
    *,
    merged_short_id: str | None,
    files: list[str],
) -> str:
    """merge 成功后 main 残留冲突标记 → 给被合并 worktree 的 owner 建清理任务。

    走 TaskService.create_task (与 VERIFY spawn 同一条系统建账路径),
    assignee 从被合并分支的 short_id (worktree 记录) 解析。
    任务创建失败不回滚 merge — 降级为纯警告。返回拼进 tool result 的警告文本。
    """
    shown = [str(f) for f in files[:20]]
    file_list = ", ".join(shown)

    owner_id: str | None = None
    if merged_short_id:
        try:
            from hiveweave.tools.task_tools import resolve_agent_id_by_short_id

            owner_id = await resolve_agent_id_by_short_id(
                project_id, merged_short_id
            )
        except Exception as e:
            log.warning("conflict_marker_owner_resolve_failed",
                        short_id=merged_short_id, error=str(e))

    description = (
        "git_worktree_merge 成功后，main 上仍检测到未解决的 git 冲突标记 "
        "(<<<<<<< / >>>>>>>)。请在你的 worktree 内修复这些文件（不要在 main "
        "上直接改），对齐 main 后 checkpoint 并通知 coordinator 重新合并：\n"
        + "\n".join(f"  - {f}" for f in shown)
    )
    try:
        from hiveweave.services.task import TaskService

        task_id = await TaskService().create_task(
            project_id,
            title="清理合并残留冲突标记",
            description=description,
            creator_id=creator_id,
            assignee_id=owner_id,
            priority=1,
            tags=["merge-cleanup"],
            source="system",
        )
    except Exception as e:
        # 建账失败不拖垮 merge — 合并已完成不可回滚, 只留警告兜底
        log.warning("conflict_marker_cleanup_task_failed",
                    short_id=merged_short_id, error=str(e))
        return (
            f" WARNING: unresolved git conflict markers remain on main "
            f"after merge: {file_list}. Failed to create cleanup task ({e}) "
            f"— manually rework {merged_short_id or 'the worktree owner'} "
            f"to fix them inside their worktree."
        )

    owner_note = merged_short_id if owner_id else "unassigned (owner not resolved)"
    log.info("conflict_marker_cleanup_task_created",
             task_id=task_id, short_id=merged_short_id, file_count=len(files))
    return (
        f" WARNING: unresolved git conflict markers remain on main "
        f"after merge: {file_list}. Created cleanup task "
        f"'清理合并残留冲突标记' (id={task_id}, assignee={owner_note}) "
        f"— the worktree owner fixes them inside their worktree; "
        f"coordinator reviews only."
    )


async def _check_self_merge_gate(
    project_id: str,
    agent_id: str,
    task_id: str | None,
    branch: str | None,
) -> str | None:
    """自有分支合并门：合并调用者自己 short_id 的分支时，要求对应任务已
    approved 且批准人 ≠ 调用者（防中层 builder 自写自审自合绕审）。

    Returns error text or None if OK. 任务解析顺序：taskId → 稳定分支名
    hw/<sid>/t-<id8> → 调用者名下最新任务。
    """
    import json as _json

    from hiveweave.services.task import TaskService

    ts = TaskService()
    task: dict | None = None
    if task_id:
        try:
            task = await ts.get_task(project_id, task_id)
        except Exception:
            task = None
    if task is None and branch:
        m = re.match(r"^hw/[^/]+/t-([0-9a-fA-F]{8})$", branch)
        if m:
            prefix = m.group(1).lower()
            try:
                for t in await ts.list_tasks(project_id):
                    if str(t.get("id") or "").lower().startswith(prefix):
                        task = t
                        break
            except Exception:
                task = None
    if task is None:
        try:
            cands = [
                t
                for t in await ts.list_tasks(project_id)
                if str(t.get("assignee_id") or "") == str(agent_id)
            ]
            cands.sort(
                key=lambda t: (
                    t.get("status") != "approved",
                    -(t.get("updated_at") or 0),
                )
            )
            task = cands[0] if cands else None
        except Exception:
            task = None

    if task is None:
        return (
            "Refusing to merge your own branch: no corresponding task found. "
            "Pass taskId for the task this branch implements."
        )
    # Explicit taskId must endorse THIS agent's work — not a foreign approved task.
    # Bypass was: merge(own-branch, taskId=<other's approved>) to skip self-review gate.
    if task_id:
        assignee = str(task.get("assignee_id") or "")
        tid8 = str(task.get("id") or "")[:8].lower()
        # Explicit taskId must belong to caller — empty assignee is not a free pass.
        if not assignee:
            return (
                f"Refusing to merge your own branch: taskId {tid8} has no "
                f"assignee; cannot use it as self-merge endorsement."
            )
        if assignee != str(agent_id):
            return (
                f"Refusing to merge your own branch: taskId {tid8} is not "
                f"assigned to you (assignee≠caller). Pass the task you "
                f"implemented, not another agent's approval."
            )
    tid = str(task.get("id") or "")[:8]
    # P1 fix(TEST10): 接受 closed 作为 approved 的等价后继状态。
    # approve 后系统可能因 worktree==main 自动 close，但 merge 门禁
    # 不应因此拒绝——只要 evidence.reviewed_by 存在即证明曾经过审批。
    # P2-2 fix: "verifying" 也是 post-approve 合法态（VERIFY spawn 后）。
    status = task.get("status")
    if status not in ("approved", "closed", "verifying"):
        return (
            f"Refusing to merge your own branch: task {tid} is "
            f"'{status}', not approved/verifying. Get your superior to "
            f"review_task(decision='approve') first."
        )
    evidence = task.get("evidence") or {}
    if isinstance(evidence, str):
        try:
            evidence = _json.loads(evidence)
        except Exception:
            evidence = {}
    reviewer = evidence.get("reviewed_by") if isinstance(evidence, dict) else None
    # closed 状态必须有 reviewed_by 证据（证明是 approve 后自动关闭，而非手动关闭）
    if status == "closed" and not reviewer:
        return (
            f"Refusing to merge your own branch: task {tid} is 'closed' "
            f"without approval evidence (no reviewed_by). It may have been "
            f"closed manually. Get your superior to re-approve via "
            f"review_task(decision='approve')."
        )
    if not reviewer:
        return (
            f"Refusing to merge your own branch: task {tid} has no recorded "
            "reviewer. Ask your superior to approve it via review_task first "
            "(approval records reviewed_by)."
        )
    if str(reviewer) == str(agent_id):
        return (
            f"Refusing to merge your own branch: task {tid} was approved by "
            "yourself. Own-branch merge requires approval by a DIFFERENT "
            "reviewer (e.g. the CEO)."
        )
    return None


async def _auto_submit_merged_running_tasks(
    project_id: str,
    workspace_path: str,
    *,
    branch: str,
    short_id: str | None,
    merged_by: str,
    merge_commit: str | None,
) -> list[str]:
    """merge 成功后同分支 running 任务自动结转（里程碑主任务死锁治愈）。

    merge 已确认成功（含 noop 已合入），无需再跑 git branch --merged。
    失败只记日志，绝不中断 merge。
    """
    try:
        from hiveweave.services.git_worktree.service_merge import (
            auto_submit_running_task_after_merge,
        )

        _n, titles = await auto_submit_running_task_after_merge(
            project_id,
            workspace_path,
            branch=branch,
            short_id=short_id,
            merged_by=merged_by,
            merge_commit=merge_commit,
            already_on_main=True,
        )
        return titles
    except Exception as e:
        log.warning("merge_auto_submit_failed", error=str(e))
        return []


async def _supersede_merge_pending_after_merge(
    project_id: str,
    caller_agent_id: str,
    *,
    task_id: str | None,
    branches: list[str | None],
) -> None:
    """merge 成功 ⇒ 清掉该任务的 [MERGE PENDING] / [MERGE PROXY] 待办。

    不变式：**通知发出与清理必须成对**（approve 经
    ``_inject_merge_pending_wake`` 发出 ⇒ merge 成功 / waive / close 清除）。
    此前 approve 注入的提醒在 merge 成功路径从不清除，owner 收件箱堆着
    已完成事项的假账（TEST_DSH_64 #8 现场：4 个清账回合、17 LLM + 17
    tool 全耗在向已合并的任务对账）。

    清理对象 = 任务 id 前 8 位（t-<8> 分支反查 + params.taskId）+ 不编码
    任务 id 的分支名本身（``hw/<sid>/work`` 的 wake 正文含 branchName）；
    收件人 = caller + 按 id 反查出的 merge owner（creator/reviewer 经
    ``resolve_merge_owner``，第三方代审场景 caller ≠ owner）。对非收件人
    调 supersede 是无害的（UPDATE 0 行）。失败只记日志，不回滚 merge。
    """
    try:
        from hiveweave.db import project as project_db
        from hiveweave.services.inbox import InboxService
        from hiveweave.services.tasks.verify import resolve_merge_owner

        tid_tokens: set[str] = (
            {str(task_id)[:8].lower()} if task_id else set()
        )
        branch_tokens: set[str] = set()
        for br in {b for b in branches if b}:
            m = re.match(r"^hw/([^/]+)/t-([0-9a-fA-F]{8})$", str(br))
            if m:
                tid_tokens.add(m.group(2).lower())
            else:
                # hw/<sid>/work 等稳定分支不编码任务 id：用分支名做指纹。
                branch_tokens.add(str(br))
        owners = {caller_agent_id}
        if tid_tokens:
            conn = await project_db.get_project_db_by_project_id(project_id)
            cur = await conn.execute(
                "SELECT id, creator_id, reviewer_id FROM tasks WHERE "
                + " OR ".join("id LIKE ?" for _ in tid_tokens),
                [f"{t}%" for t in sorted(tid_tokens)],
            )
            for r in await cur.fetchall():
                own = resolve_merge_owner(dict(r), caller_agent_id)
                if own:
                    owners.add(str(own))
            await cur.close()
        for owner in owners:
            for token in sorted(tid_tokens | branch_tokens):
                await InboxService().supersede_watchdog_messages(
                    owner,
                    prefixes=["[MERGE PENDING]", "[MERGE PROXY]"],
                    contains=token,
                )
    except Exception as e:
        log.warning("merge_pending_supersede_failed", error=str(e))


@tool(
    "git_worktree_merge",
    "Merge a worktree branch into main and remove the worktree. "
    "Pass taskId for an exact hit on the stable branch "
    "hw/<shortId>/t-<taskId[:8]>. "
    "On conflict: main merge is aborted; rework the executor to rebase/merge "
    "main in THEIR worktree, then retry. Does not auto-spawn VERIFY. After a "
    "milestone is on MAIN, dispatch one QA task with milestoneVerify=true.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_merge_tool(
    params: GitWorktreeMergeParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Merge a git worktree branch back into main and remove the worktree.

    Bug G fix: 支持架构师合并其他 agent 的 worktree。
    branch_name 解析顺序：
    - task_id 提供时（P0 稳定命名）— 按契约算 hw/<sid>/t-<taskid8>
      精确命中；未命中回落下列 legacy 路径
    - "hw/..." 完整分支名 — 直接使用
    - short_id (如 "A066") — 查找该 agent 的分支
    - task_name (如 "后端工程师" / "feat-x") — 先查调用者自己的分支；
      查不到再全局搜索 hw/*/<name>，唯一匹配则合并，
      零匹配/多匹配则报错并列出候选分支（不再静默拼调用者前缀）。
    """
    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, caller_short_id, project_id = wt_ctx

    # TEST_DSH_32 P6（merge 时机报错化）：任务还在 submitted/reviewing
    # （未 approved）时 merge，git 会成功但账本义务在 approve 时才生成——
    # 先合后批会把 merge 义务留在账上，事后 MERGE PENDING 循环催办、
    # 重试只会 dry-run/no-op。此处当场报错，把顺序讲清楚。
    if params.task_id:
        try:
            from hiveweave.services.task import TaskService

            _mt = await TaskService().get_task(project_id, params.task_id)
            if _mt and not _mt.get("is_archived"):
                _mst = (_mt.get("status") or "").lower()
                if _mst in ("submitted", "reviewing"):
                    return ToolResult.err(
                        f"Task {params.task_id[:8]} is '{_mst}' (not approved "
                        "yet). Do NOT merge before approve — the merge "
                        "obligation only exists after approve, so merging now "
                        "lands the code but leaves the ledger dirty (you "
                        "would be nudged to merge forever with nothing to "
                        "merge). Order: review_task(approve) FIRST, then "
                        "git_worktree_merge."
                    )
        except Exception as e:
            log.debug("merge_task_status_prefetch_failed", error=str(e))

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    branch_name = params.branch_name or "task"
    # 未显式指定 targetBranch 时，动态解析仓库实际默认基分支（main → master
    # 二级回退），避免硬编码 "main" 在 master 默认分支仓库上
    # `git checkout main` 失败（tiny-tool 实测 Bug）。
    from hiveweave.services.git_worktree import _resolve_base_branch

    if params.target_branch:
        target_branch = params.target_branch
    else:
        target_branch = (
            await _resolve_base_branch(workspace_path) or "main"
        )
    merged_branch: str | None = None
    merged_short: str | None = None
    merge_call: Any = None  # 延迟执行 —— 自有分支门通过后才真正 merge

    # P0 稳定命名优先：调用方提供 task_id 时按契约 hw/<sid>/t-<taskid8>
    # 精确命中；未命中/解析失败回落下面的 legacy 解析（老 slug 分支照合）。
    stable = (
        await _resolve_stable_task_branch(
            workspace_path, project_id, params.task_id
        )
        if params.task_id
        else None
    )

    # Bug G fix: 智能解析 branch_name
    if stable:
        merged_branch, merged_short = stable

        async def _do_merge() -> dict:
            return await gwt.merge_by_branch(
                workspace_path, merged_branch, target_branch  # type: ignore[arg-type]
            )

        merge_call = _do_merge
    elif branch_name.startswith("hw/"):
        merged_branch = branch_name
        from hiveweave.tools.task_tools import parse_short_id_from_branch

        merged_short = parse_short_id_from_branch(branch_name)

        async def _do_merge() -> dict:
            return await gwt.merge_by_branch(
                workspace_path, branch_name, target_branch
            )

        merge_call = _do_merge
    elif _SHORT_ID_RE.fullmatch(branch_name):
        # 严格 short_id 形状（字母+数字，如 "A066"）— 查找该 agent 的分支。
        # 注意：不能用 isalnum() —— CJK 字符也算 alnum，短中文任务名
        # （如"后端工程师"）会被误判成 short_id。
        from hiveweave.services.git_worktree import _git
        ok, branch_out = await _git(
            ["branch", "--list", f"hw/{branch_name}/*"],
            workspace_path
        )
        branches = _parse_branch_list(branch_out)
        if not branches:
            # F13b（平台修复计划 2026-08-30）：分支查找零匹配 ≠ 冲突，也
            # ≠ 必须出错 —— 分支可能已在上次 merge 后被清理，但 main 已经
            # 包含它。幂等重入直接给成功态（r4：两个 Agent 各踩一次
            # "No worktree branch found" 才弄清不是冲突）。
            # 前缀纪律：branch_name 是 short_id（本分支）→ 拼 hw/<sid>；
            # 已带 hw/ 前缀则直接用（防 hw/hw/ 双拼）。
            _candidate = branch_name if str(branch_name).startswith("hw/") else (
                f"hw/{branch_name}"
            )
            _already, _anc = await _git(
                ["merge-base", "--is-ancestor", _candidate, target_branch],
                workspace_path,
            )
            if not _already:
                # ref 已删：退而按 merge commit 历史判定（P2 边界审计：
                # --fixed-strings 字面匹配，防分支名含正则元字符误判）
                _already, _anc = await _git(
                    ["log", "-1", "--oneline", "--merges", "--fixed-strings",
                     f"--grep=Merge branch '{_candidate}'"],
                    workspace_path,
                )
            if _already:
                # TEST_DSH_62 P5/L8：outcome token + 与服务层
                # merge_by_branch 幂等重入回执（service_merge.py）收敛为
                # 同一句文案，两处「已合并」变体只留这一份。
                return ToolResult.ok(
                    f"outcome=already_merged: Branch {_candidate} was "
                    f"already merged into {target_branch} by a prior "
                    "merge — idempotent success. Nothing to do; the "
                    "worktree branch was cleaned up. Do not call "
                    "git_worktree_merge again for this branch."
                )
            # T2.1: 分支查找零匹配 ≠ 冲突 —— 对照组（task_name 零匹配路径）
            # 从不拼冲突提示，这里此前却无条件拼 MERGE_CONFLICT_HINT，把
            # coordinator 引导去 rework 不存在的冲突。改用专属 hint。
            from hiveweave.services.worktree_review import BRANCH_LOOKUP_FAILED_HINT

            return ToolResult.err(
                f"No worktree branch found for agent {branch_name}\n\n"
                f"{BRANCH_LOOKUP_FAILED_HINT}"
            )
        merged_short = branch_name
        merged_branch = branches[0]

        async def _do_merge() -> dict:
            result = await gwt.merge_by_branch(
                workspace_path, branches[0], target_branch
            )
            if result.get("success") and len(branches) > 1:
                remaining = branches[1:]
                result["message"] = (
                    f"Merged {branches[0]}. "
                    f"Remaining branches: {remaining}"
                )
            return result

        merge_call = _do_merge
    else:
        # task_name — 先按原行为查调用者自己的分支；查不到则全局搜索
        # hw/*/<slug>（coordinator 按名字合并 executor 分支的场景）。
        # 旧行为：静默用调用者 short_id 拼前缀 → coordinator 传 executor 的
        # 分支名时必然找错（井字棋实测：解析成 hw/A001/feat-tictactoe-a004）。
        from hiveweave.services.git_worktree import _git, _slugify

        slug = _slugify(branch_name)
        caller_branch = f"hw/{caller_short_id}/{slug}"
        ok_c, out_c = await _git(
            ["branch", "--list", caller_branch], workspace_path
        )
        caller_exists = bool(ok_c and caller_branch in (out_c or ""))

        if caller_exists:
            merged_short = caller_short_id
            merged_branch = caller_branch

            async def _do_merge() -> dict:
                return await gwt.merge(
                    workspace_path, caller_short_id, str(branch_name),
                    target_branch,
                )

            merge_call = _do_merge
        else:
            ok_g, out_g = await _git(
                ["branch", "--list", f"hw/*/{slug}"], workspace_path
            )
            matches = _parse_branch_list(out_g)
            if len(matches) == 1:
                merged_branch = matches[0]
                from hiveweave.tools.task_tools import (
                    parse_short_id_from_branch,
                )

                merged_short = parse_short_id_from_branch(matches[0])

                async def _do_merge() -> dict:
                    return await gwt.merge_by_branch(
                        workspace_path, matches[0], target_branch
                    )

                merge_call = _do_merge
            else:
                ok_all, out_all = await _git(
                    ["branch", "--list", "hw/*/*"], workspace_path
                )
                all_branches = _parse_branch_list(out_all)
                listing = (
                    "Available worktree branches:\n"
                    + "\n".join(f"  - {b}" for b in all_branches)
                    if all_branches
                    else "No hw/*/* worktree branches exist."
                )
                if len(matches) > 1:
                    return ToolResult.err(
                        f"Ambiguous branch name '{branch_name}' matches "
                        f"{len(matches)} branches — pass the full name "
                        f"(hw/<shortId>/<name>) instead:\n"
                        + "\n".join(f"  - {m}" for m in matches)
                    )
                return ToolResult.err(
                    f"No worktree branch found matching '{branch_name}' "
                    f"(tried '{caller_branch}' and 'hw/*/{slug}').\n\n{listing}"
                )

    # ── 自有分支合并门：合并调用者自己 short_id 的分支须异人 approved ──
    gate_err = None
    if (
        merged_short
        and caller_short_id
        and merged_short.upper() == str(caller_short_id).upper()
    ):
        gate_err = await _check_self_merge_gate(
            project_id, agent_id, params.task_id, merged_branch
        )

    # ── dry-run：只读预检，列出全部缺失项，零改动 ──
    if params.dry_run:
        issues: list[dict] = []
        if gate_err:
            issues.append({"code": "self_merge_gate", "message": gate_err})
        report = await gwt.preflight_merge(
            workspace_path,
            merged_short or caller_short_id,
            merged_branch,
            target_branch,
        )
        issues += report.get("missing", [])
        if report.get("already_up_to_date"):
            msg = (
                f"dry-run（信息性预检结果，非错误）：分支 {merged_branch} "
                f"已合入 {target_branch}，无需 merge。"
            )
        elif not issues:
            msg = (
                "dry-run（信息性预检结果，非错误）：所有 merge 前置条件"
                "已满足，可以执行 merge。"
            )
        else:
            # TEST_DSH_62 P5/L8：dry-run 是独立形态（不带 outcome token），
            # 但缺失项清单曾被读成「本次调用失败」——明确这是信息性
            # 预检结果：当前调用本身成功，未满足项只影响后续真实 merge。
            msg = (
                "dry-run（信息性预检结果，非错误）：以下前置条件未满足，"
                "实际执行 merge 时会被拒绝（本次 dry-run 调用本身成功、"
                "零改动）：\n"
                + "\n".join(f"- [{i['code']}] {i['message']}" for i in issues)
            )
        return ToolResult.ok(msg, dry_run=True, missing=issues)

    if gate_err:
        return ToolResult.err(gate_err)

    result = await merge_call()

    short = result.get("short_id") or merged_short or caller_short_id
    branch = result.get("branch") or merged_branch
    files = result.get("files") or result.get("conflicts")

    # TEST_DSH_32 P8（隔离必发通知）：merge 把 MAIN 未提交文件搬进
    # 隔离区时，向调用者发紧急通知（此前 0 通知、纯静默搬移）。
    # 不依赖 success——隔离后重试失败的路径同样要通知（审计 P3-5）。
    q_events = result.get("quarantined")
    if isinstance(q_events, list) and q_events:
        try:
            from hiveweave.services.inbox import InboxService

            qf = [f for e in q_events for f in (e.get("files") or [])]
            dest = q_events[0].get("dest") or ".hiveweave/merge-quarantine"
            await InboxService().send_message(
                from_agent_id="system",
                to_agent_id=agent_id,
                message=(
                    f"[MERGE QUARANTINE] git_worktree_merge 检测到 "
                    f"{target_branch} 上有未提交的本地改动，已搬移到 "
                    f"{dest}（未丢弃，可恢复）：\n- "
                    + "\n- ".join(qf[:20])
                    + "\n恢复指引：进入该目录对比/取回文件后重新提交；"
                    "若改动已无价值可忽略。"
                    + (
                        "本次 merge 最终失败，请先处理冲突后重试。"
                        if not result.get("success")
                        else "合并本体不受影响。"
                    )
                ),
                message_type="task",
                priority="urgent",
                task_id=params.task_id,
                wake=True,
            )
        except Exception as qe:
            log.warning("merge_quarantine_notify_failed", error=str(qe))

    if result.get("success"):
        # Defense in depth: conflict markers must never be treated as success.
        marker_files = result.get("conflict_markers")
        if isinstance(marker_files, list) and marker_files:
            reason = "conflict_markers_landed"
            reworked = 0
            try:
                from hiveweave.tools.task_tools import rework_tasks_after_merge_conflict

                reworked = await rework_tasks_after_merge_conflict(
                    project_id,
                    agent_id,
                    merged_short_id=short,
                    merged_branch=branch,
                    conflicts=marker_files,
                    merged_files=files if isinstance(files, list) else None,
                )
            except Exception as e:
                log.warning("merge_marker_rework_failed", error=str(e))
            err = result.get("message") or (
                f"Merge aborted: conflict markers on {target_branch}: "
                + ", ".join(str(f) for f in marker_files[:12])
            )
            if reworked:
                err = f"{err}\n\nAuto-reworked {reworked} approved task(s) → executor."
            return ToolResult.err(err)

        # Idempotent noop: already on main with no new files — skip VERIFY spawn.
        branch_files = (
            result.get("files") if isinstance(result.get("files"), list) else None
        )
        if result.get("already_up_to_date") and not branch_files:
            # 事实短路（07 报告 #4）：no-op merge（分支已在 main）也必须结算
            # merge 义务——这是 07 实测 3 条义务僵尸 4.5h 的最可能路径
            # （no-op 早退 → 从不 fulfill → 只能等 dwell 超时）。
            try:
                from hiveweave.services.obligation import ObligationLedger

                for _br in {branch, branch_name} - {None, ""}:
                    m = re.match(r"^hw/([^/]+)/t-([0-9a-fA-F]{8})$", str(_br))
                    if m:
                        await ObligationLedger().fulfill(
                            project_id, m.group(2), "merge",
                            merge_commit=result.get("hash"),
                        )
            except Exception as e:
                log.warning("merge_obligation_fulfill_failed", error=str(e))
            auto_titles = await _auto_submit_merged_running_tasks(
                project_id, workspace_path,
                branch=branch or branch_name, short_id=short,
                merged_by=agent_id, merge_commit=result.get("hash"),
            )
            try:
                from hiveweave.services.task import TaskService

                await TaskService().migrate_orphan_approved(project_id)
            except Exception as mig_err:
                log.warning("orphan_migrate_after_merge_failed", error=str(mig_err))
            msg = result.get(
                "message",
                "Branch already on main (no new commits) — merge noop.",
            )
            if auto_titles:
                msg = (
                    f"{msg} Auto-submitted running task(s): "
                    f"{', '.join(str(t)[:60] for t in auto_titles)}"
                )
            return ToolResult.ok(msg)

        # TEST16 D2: fulfill merge obligation — merge landed, stop escalation.
        # 事实短路（07 报告 #4）：分支名编码模块任务 id（hw/<sid>/t-<8>）。
        # 统建合并多个模块分支时 params.task_id 指向统建任务，模块级 merge
        # 义务会漏掉——07 实测 3 条义务僵尸 4.5h。按分支反查逐分支清义务；
        # 分支与 task_id 都没匹配上才退回按 owner 清（旧行为兜底）。
        try:
            from hiveweave.services.obligation import ObligationLedger

            fulfilled = 0
            seen_branch_tasks: set[str] = set()
            for _br in {branch, branch_name} - {None, ""}:
                m = re.match(r"^hw/([^/]+)/t-([0-9a-fA-F]{8})$", str(_br))
                if m and m.group(2).lower() not in seen_branch_tasks:
                    seen_branch_tasks.add(m.group(2).lower())
                    fulfilled += await ObligationLedger().fulfill(
                        project_id, m.group(2), "merge",
                        merge_commit=result.get("hash"),
                    )
            if params.task_id:
                fulfilled += await ObligationLedger().fulfill(
                    project_id, params.task_id, "merge",
                    merge_commit=result.get("hash"),
                )
            # 审计[1]：分支/task 结算后，caller 名下若仍有 pending merge 义务
            # （如历史非规范分支名遗留、或 caller 自身其它任务的义务），
            # 记 warning 供观测——这些义务不会再被本次 merge 事件短路，
            # 只能等 dwell 超时兜底。
            if fulfilled:
                leftovers = [
                    o for o in await ObligationLedger().get_pending_for_agent(
                        project_id, agent_id
                    )
                    if o.get("obligation_type") == "merge"
                ]
                if leftovers:
                    log.warning(
                        "merge_obligation_leftover_pending",
                        agent_id=agent_id,
                        count=len(leftovers),
                        task_ids=[
                            str(o.get("task_id"))[:8] for o in leftovers
                        ],
                    )
            if not fulfilled:
                # No branch/task match — fulfill by owner (the caller did the merge)
                await ObligationLedger().fulfill_by_owner(
                    project_id, agent_id, "merge"
                )
        except Exception as e:
            log.warning("merge_obligation_fulfill_failed", error=str(e))

        # #8（TEST_DSH_64）：merge 成功 ⇒ 成对清理本任务/本分支的
        # [MERGE PENDING] 提醒（发出与清理必须成对，见 helper docstring）。
        await _supersede_merge_pending_after_merge(
            project_id,
            agent_id,
            task_id=params.task_id,
            branches=[branch, branch_name],
        )

        # 里程碑主任务结转：同分支 running 任务 merge 后自动 submit
        auto_titles = await _auto_submit_merged_running_tasks(
            project_id, workspace_path,
            branch=branch or branch_name, short_id=short,
            merged_by=agent_id, merge_commit=result.get("hash"),
        )

        # Post-merge: stamp merge fact + nudge existing MAIN VERIFY (no auto-spawn)
        try:
            from hiveweave.tools.task_tools import nudge_verify_tasks_after_merge

            nudged = await nudge_verify_tasks_after_merge(
                project_id,
                agent_id,
                merged_short_id=short,
                merged_branch=branch,
                merged_files=files if isinstance(files, list) else None,
                merge_commit=result.get("hash"),
                target_branch=target_branch,
            )
        except Exception as e:
            log.warning("verify_nudge_after_merge_failed", error=str(e))
            try:
                from hiveweave.services.task import TaskService

                await TaskService().migrate_orphan_approved(project_id)
            except Exception:
                pass
            return ToolResult.ok(
                f"{result.get('message', 'Worktree merged and cleaned up')} "
                f"WARNING: post-merge VERIFY nudge failed ({e}). "
                f"Coordinators dispatch milestone QA with milestoneVerify=true."
            )
        try:
            from hiveweave.services.task import TaskService

            await TaskService().migrate_orphan_approved(project_id)
        except Exception as mig_err:
            log.warning("orphan_migrate_after_merge_failed", error=str(mig_err))
        msg = result.get("message", "Worktree merged and cleaned up")
        if auto_titles:
            msg = (
                f"{msg} Auto-submitted {len(auto_titles)} running task(s): "
                f"{', '.join(str(t)[:60] for t in auto_titles)}"
            )
        if nudged:
            msg = (
                f"{msg} Nudged {nudged} existing VERIFY task(s) on MAIN."
            )
        elif files is not None:
            msg = (
                f"{msg} Merge recorded. Milestone QA is not auto-spawned "
                f"per leaf merge; coordinators dispatch one MAIN VERIFY "
                f"via milestoneVerify."
            )
        return ToolResult.ok(msg)

    # Conflict: auto-rework only for real content conflicts.
    # untracked_on_target / merge_failed are MAIN hygiene or unknown — do NOT rework.
    reason = result.get("reason") or ""
    reworked = 0
    if reason in ("merge_conflict", "conflict_markers_landed") or (
        not reason
        and isinstance(result.get("conflicts"), list)
        and result.get("conflicts")
    ):
        try:
            from hiveweave.tools.task_tools import rework_tasks_after_merge_conflict

            reworked = await rework_tasks_after_merge_conflict(
                project_id,
                agent_id,
                merged_short_id=short,
                merged_branch=branch,
                conflicts=result.get("conflicts") if isinstance(result.get("conflicts"), list) else None,
                merged_files=files if isinstance(files, list) else None,
            )
        except Exception as e:
            log.warning("merge_conflict_rework_failed", error=str(e))
            reworked = 0

    # T2.1: 失败归因选择器单点（worktree_review.format_merge_failure_message）
    # —— reason 非 untracked_on_target 一律走冲突文案的旧兜底把
    # precondition_failed / merge_failed 错标成冲突。
    from hiveweave.services.worktree_review import format_merge_failure_message

    err = result.get("message")
    if not err:
        err = format_merge_failure_message(
            reason=str(reason or ""),
            branch=str(branch or branch_name),
            target=target_branch,
            conflicts=result.get("conflicts")
            if isinstance(result.get("conflicts"), list)
            else None,
            untracked=result.get("untracked")
            if isinstance(result.get("untracked"), list)
            else None,
        )
    if reworked:
        err = f"{err}\n\nAuto-reworked {reworked} approved task(s) → executor."

    # P0-1 fail-closed: precondition failures must not be silent.
    # Reopen obligation + notify creator + agent_health warning.
    if reason == "precondition_failed" and params.task_id:
        try:
            from hiveweave.services.obligation import ObligationLedger

            task_svc = None
            try:
                from hiveweave.services.task import TaskService
                task_svc = TaskService()
                task_obj = await task_svc.get_task(project_id, params.task_id)
            except Exception:
                task_obj = None
            creator = (task_obj or {}).get("creator_id")
            if creator:
                await ObligationLedger().create(
                    project_id,
                    str(creator),
                    "merge",
                    task_id=params.task_id,
                    context={
                        "reason": "merge_precondition_failed",
                        "source": "merge_fail_closed",
                        "detail": (result.get("message") or "")[:200],
                    },
                )
                # Notify creator that merge failed and obligation is open
                try:
                    from hiveweave.services.inbox import InboxService

                    await InboxService().send_message(
                        from_agent_id="system",
                        to_agent_id=str(creator),
                        message=(
                            f"[MERGE FAILED] git_worktree_merge for task "
                            f"{params.task_id[:8]} hit precondition failure: "
                            f"{(result.get('message') or 'worktree corrupted')[:200]}\n"
                            f"Merge obligation reopened. Investigate worktree "
                            f"health for {short} and retry, or repair the "
                            f"worktree (delete + recreate) then merge again."
                        ),
                        message_type="system",
                    )
                except Exception:
                    pass
        except Exception as obl_err:
            log.warning(
                "merge_fail_closed_obligation_error",
                task_id=params.task_id,
                error=str(obl_err),
            )
        # agent_health warning (yellow frame)
        try:
            from hiveweave.agents.agent import broadcast_agent_health

            await broadcast_agent_health(
                agent_id, "error",
                f"merge precondition failed for task {params.task_id[:8]}",
            )
        except Exception:
            pass

    return ToolResult.err(err)


# ── git_worktree_remove ──────────────────────────────────


class GitWorktreeRemoveParams(BaseModel):
    """Parameters for git_worktree_remove tool."""

    model_config = ConfigDict(populate_by_name=True)

    branch_name: str = Field(
        alias="branchName",
        description="Branch/task name of the worktree to remove.",
        json_schema_extra={
            "aliases": ["branchName", "branch_name", "branch", "name", "taskName", "task_name", "task"]
        },
    )


@tool(
    "git_worktree_remove",
    "Remove a worktree and its branch without merging. Discards changes.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_remove_tool(
    params: GitWorktreeRemoveParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Remove a git worktree."""
    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, short_id, _ = wt_ctx

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    task_name = params.branch_name or "task"

    result = await gwt.delete(workspace_path, short_id, task_name)
    # 0-2：独立可 grep 的 husk 事件（与 merge / close-gc / dismiss 同一实现）
    from hiveweave.services.git_worktree.service_lifecycle import (
        _surface_husk_left,
    )

    _surface_husk_left(
        result,
        short_id=short_id,
        branch=str(result.get("branch") or ""),
        event="git_worktree_remove_husk_left",
    )
    if not result.get("success"):
        return ToolResult.err(result.get("message", "Failed to remove worktree"))
    # 0-2：``success=True`` 不等于目录已消失 —— delete() 的 removed=False 表示
    # remove/prune 之后目录仍在（Windows 文件锁下的 husk）。此前这里无条件回
    # "Worktree removed"，是「假装删除成功」的工具层残余（Python 层早就诚实了）。
    # ⚠ **保持 ok（不升硬失败）**：第 0 步口径是"先能看见"，「removed=False 是否
    # 该变成失败」由 1-4 单独立项、按全项目频次×后果评估 —— 这里改成 err 会提前
    # 把 24 次"本来没事"的删除失败变成停摆，并污染 1-4 的数据口径（审计 Q3）。
    if result.get("removed") is not True:
        return ToolResult.ok(
            f"Worktree registration/branch handled, but the directory is still "
            f"on disk at {result.get('path') or 'path unknown'} — something "
            "holds it open (Device busy). Registered worktree + branch were "
            "still cleaned up; reconcile retries the directory."
        )
    return ToolResult.ok("Worktree removed")


# ── git_worktree_status ──────────────────────────────────


class GitWorktreeStatusParams(BaseModel):
    """Parameters for git_worktree_status tool."""

    model_config = ConfigDict(populate_by_name=True)

    short_id: str | None = Field(
        default=None,
        alias="shortId",
        description=(
            "Agent short_id whose worktree to inspect (e.g. A004). "
            "Omit to use the caller's own worktree."
        ),
        json_schema_extra={
            "aliases": ["shortId", "short_id", "agentShortId", "target"]
        },
    )


@tool(
    "git_worktree_status",
    "Show branch, dirty flag, HEAD, and whether tip is already on main "
    "(tip_is_ancestor_of_main / commits_ahead). "
    "Pass shortId to inspect a subordinate's worktree. "
    "Use tip_is_ancestor_of_main=true before claiming work is delivered.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_status_tool(
    params: GitWorktreeStatusParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Show git worktree status."""
    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, short_id, _ = wt_ctx

    target_sid = (params.short_id or "").strip() or short_id
    # Resolve name/UUID → short_id when coordinator passes an agent ref
    if params.short_id and ctx and getattr(ctx, "org", None):
        try:
            resolved = await ctx.org.resolve_agent(params.short_id)
            if resolved and resolved.get("short_id"):
                target_sid = resolved["short_id"]
        except Exception:
            pass

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    result = await gwt.info(workspace_path, target_sid)
    if not result.get("success"):
        return ToolResult.err(result.get("message", "Failed to get worktree status"))

    # GitWorktreeService.info returns {success, status: {...}} — not "info"
    info = result.get("status")
    if info is None:
        # P2-6: 无 worktree 角色（CEO/HR/新 agent）回 MAIN 项目根视角 ——
        # 合并职责者恰恰最需要看 MAIN 状态；不再裸报 "No worktree found"。
        if target_sid == short_id:
            try:
                from hiveweave.services.git_worktree.git_cmd import (
                    _current_branch,
                    _git,
                )

                mb = await _current_branch(workspace_path)
                ok_dirty, _porc = await _git(
                    ["-c", "core.quotepath=false", "status",
                     "--porcelain", "-z"],
                    workspace_path,
                )
                main_dirty = bool(_porc and _porc.strip())
                ok_head, head = await _git(
                    ["rev-parse", "--short", "HEAD"], workspace_path
                )
                return ToolResult.ok(
                    f"[MAIN] ({target_sid} has no worktree — project root)\n"
                    f"Branch: {mb or '?'} | dirty={main_dirty} | "
                    f"head={head or '?'} | base=main\n"
                    "Tip is 'on main' by definition. Use git_worktree_list "
                    "to see all agent worktrees."
                )
            except Exception as e:
                return ToolResult.err(
                    f"No worktree found for short_id={target_sid} "
                    f"(MAIN inspect failed: {e})"
                )
        # TEST_DSH_62 P5/L8：查**他人**已拆 worktree 查不到是正常答案——
        # 真合并必拆 worktree（service_merge 清理链），此前按 failed 报
        # 「No worktree found」会误导重建。对齐服务层 info() 的
        # success:True/status:None 语义，success + found=false token。
        return ToolResult.ok(
            f"found=false — No worktree found for short_id={target_sid} "
            f"(expected path under .hiveweave/worktrees/{target_sid}/). "
            "该 worktree 不存在——若此前合并过，合并时会自动拆除"
            "(cleaned up)，无需重建。"
        )
    branch = info.get("branch") or "?"
    dirty = bool(info.get("has_uncommitted"))
    head = info.get("head") or "?"
    base = info.get("base_branch") or "main"
    tip_anc = info.get("tip_is_ancestor_of_main")
    ahead = info.get("commits_ahead")
    tip_s = (
        "unknown" if tip_anc is None else ("true" if tip_anc else "false")
    )
    ahead_s = "unknown" if ahead is None else str(ahead)
    return ToolResult.ok(
        f"short_id={target_sid}, Branch: {branch}, "
        f"dirty={dirty}, head={head}, base={base}, "
        f"tip_is_ancestor_of_main={tip_s}, commits_ahead={ahead_s}"
    )


# ── git_worktree_sync ────────────────────────────────────


class GitWorktreeSyncParams(BaseModel):
    """Parameters for git_worktree_sync tool."""

    model_config = ConfigDict(populate_by_name=True)

    short_id: str | None = Field(
        default=None,
        alias="shortId",
        description=(
            "Agent short_id whose worktree to sync with MAIN (e.g. A004). "
            "Omit to sync the caller's own worktree. Only your own or a "
            "direct subordinate's worktree is allowed."
        ),
        json_schema_extra={
            "aliases": ["shortId", "short_id", "agentShortId", "target"]
        },
    )
    mode: str | None = Field(
        default=None,
        alias="mode",
        description=(
            "merge (default): conflict-safe sync — predicted conflicts are "
            "refused before anything runs. materialize_conflict: merge and "
            "leave the conflict in your worktree so you can resolve it by "
            "hand (skips the pre-checks on purpose). abort: abandon an "
            "in-progress merge and return to the pre-merge HEAD."
        ),
        json_schema_extra={
            "aliases": [
                "mode", "syncMode", "sync_mode",
                "materializeConflict", "materialize_conflict",
            ]
        },
    )


async def _resolve_sync_target(
    caller_agent_id: str,
    caller_short_id: str,
    short_param: str | None,
    ctx=None,
) -> tuple[str | None, str | None, str | None]:
    """P1-1 越权门：只准同步自己的 worktree，或**直属 subordinate** 的。

    对任意同伴 worktree 触发 sync = auto-checkpoint（把同伴未提交 WIP
    提交进其分支）+ 搬走同伴 untracked 文件 —— 超出 SOURCE_WRITE 的写面，
    必须限组织父子关系。关系查不到一律拒绝（fail-closed）。

    Returns ``(target_sid, target_agent_id, error)``；error 非 None 即拒绝。
    """
    wanted = (short_param or "").strip()
    if not wanted or wanted.upper() == str(caller_short_id or "").upper():
        return caller_short_id, caller_agent_id, None

    org = None
    if ctx is not None and getattr(ctx, "org", None) is not None:
        org = ctx.org
    else:
        try:
            from hiveweave.services.org import OrgService

            org = OrgService()
        except Exception:
            org = None

    resolved: dict | None = None
    if org is not None:
        try:
            resolved = await org.resolve_agent(wanted)
        except Exception:
            resolved = None
    if not resolved:
        return None, None, (
            f"Refusing to sync: no agent found for '{wanted}'. You may only "
            "sync your own worktree (omit shortId) or a direct "
            "subordinate's."
        )

    target_sid = str(resolved.get("short_id") or wanted)
    target_agent_id = resolved.get("id")
    if target_sid.upper() == str(caller_short_id or "").upper():
        return caller_short_id, caller_agent_id, None

    parent_id = str(resolved.get("parent_id") or "").strip()
    if not caller_agent_id or parent_id != str(caller_agent_id):
        return target_sid, target_agent_id, (
            f"Refusing to sync worktree {target_sid}: it is not your own "
            "worktree and you are not its direct superior. You may only "
            "sync your own worktree (omit shortId); for anyone else, ask "
            "the worktree owner to run git_worktree_sync themselves."
        )
    return target_sid, target_agent_id, None


@tool(
    "git_worktree_sync",
    "Sync MAIN's new commits into an agent worktree (MAIN → worktree). "
    "Your own tree by default; a direct subordinate's via shortId. "
    "Up-to-date trees no-op. Untracked worktree files that MAIN's incoming "
    "commits would overwrite are moved to .hiveweave/merge-quarantine "
    "(not deleted, recoverable — receipt lists them and an inbox notice "
    "is sent). mode=merge (default) refuses predicted content conflicts "
    "BEFORE anything runs (resolve in the worktree, then retry); "
    "mode=materialize_conflict merges and leaves the conflict in your "
    "worktree so you can resolve it by hand (receipt lists the conflicted "
    "files; checkpoint refuses while that merge is unresolved); "
    "mode=abort returns the worktree to its pre-merge HEAD. "
    "Use this instead of bare `git merge main`.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_sync_tool(
    params: GitWorktreeSyncParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Sync MAIN's new commits into an agent worktree (MAIN → worktree)."""
    from hiveweave.services.git_worktree import GitWorktreeService
    from hiveweave.services.git_worktree.service_sync import (
        sync_main_into_worktree,
    )

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, caller_short_id, _project_id = wt_ctx

    # P1-1 越权门：自己的树，或直属 subordinate 的树（组织父子，fail-closed）
    target_sid, target_agent_id, gate_err = await _resolve_sync_target(
        agent_id, caller_short_id, params.short_id, ctx
    )
    if gate_err or not target_sid:
        return ToolResult.err(
            gate_err or "Refusing to sync: could not resolve target worktree."
        )

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    result = await sync_main_into_worktree(
        workspace_path, target_sid, mode=(params.mode or "merge")
    )

    # 通知面（审计备注 E）：target ≠ caller 时，隔离/同步通知必须同时发
    # 文件属主（target agent）一份 —— 不能只发调用者。
    notify_ids = [agent_id]
    if target_agent_id and str(target_agent_id) != str(agent_id):
        notify_ids.append(str(target_agent_id))

    q_events = result.get("quarantined")
    q_files: list[str] = []
    if isinstance(q_events, list) and q_events:
        q_files = [f for e in q_events for f in (e.get("files") or [])]
        try:
            from hiveweave.services.inbox import InboxService

            dest = q_events[0].get("dest") or ".hiveweave/merge-quarantine"
            # ⚠ 不能只看 `success`：materialize 是 `success=True, merged=False`
            # （操作完成了它被要求的事，但**合并没结束**）—— 那时说"合并本体不受
            # 影响"是假话。判据要落到 `state`/`merged` 上。
            if result.get("state") == "conflict_materialized":
                outcome = (
                    "合并**尚未结束**：冲突已按你的要求留在树里，"
                    "隔离只涉及未跟踪文件。"
                )
            elif result.get("merged"):
                outcome = "合并本体不受影响。"
            else:
                outcome = "本次 sync 最终失败，请先处理失败原因后重试。"
            for nid in notify_ids:
                await InboxService().send_message(
                    from_agent_id="system",
                    to_agent_id=nid,
                    message=(
                        f"[WORKTREE SYNC QUARANTINE] git_worktree_sync 检测到 "
                        f"MAIN 新提交将覆写 worktree {target_sid} 里的未跟踪"
                        f"文件，已搬移到 {dest}（未丢弃，可恢复）：\n- "
                        + "\n- ".join(q_files[:20])
                        + "\n恢复指引：进入该目录对比/取回文件后重新提交；"
                        f"若改动已无价值可忽略。{outcome}"
                    ),
                    message_type="task",
                    priority="urgent",
                    wake=True,
                )
        except Exception as qe:
            log.warning("sync_quarantine_notify_failed", error=str(qe))

    # 属主告知（E）：别人代同步了你的树 —— 属主必须可发现。
    if (
        target_agent_id
        and str(target_agent_id) != str(agent_id)
        and result.get("success")
        and result.get("merged")
    ):
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().send_message(
                from_agent_id="system",
                to_agent_id=str(target_agent_id),
                message=(
                    f"[WORKTREE SYNC] 你的 worktree ({target_sid}, branch "
                    f"{result.get('branch') or '?'}) 由上级代为同步了 MAIN "
                    f"新提交：new HEAD {result.get('new_head') or '?'}。"
                    "请 git status 确认工作区状态后再继续编码。"
                ),
                message_type="system",
            )
        except Exception as ne:
            log.warning("sync_owner_notify_failed", error=str(ne))

    if not result.get("success"):
        return ToolResult.err(
            result.get("message", "git_worktree_sync failed"),
            reason=result.get("reason"),
            state=result.get("state"),
            conflicts=result.get("conflicts") or [],
            behind_before=result.get("behind_before", 0),
            quarantined=q_events if isinstance(q_events, list) else [],
        )

    if not result.get("merged"):
        # 三种"没合并"要分开：up_to_date（幂等）/ conflict_materialized
        # （**成功**，冲突留给 agent 解）/ aborted·no_merge_in_progress。
        # 旧的硬编码 `conflicts=[]` 会把 materialize 的清单吞掉 —— agent 只能
        # 自己 `git status` 猜哪些文件要解（正是本仓最烦的"缺事实"）。
        return ToolResult.ok(
            result.get("message", "Worktree already up to date with MAIN."),
            merged=False,
            reason=result.get("reason"),
            state=result.get("state"),
            new_head=result.get("new_head", ""),
            behind_before=result.get("behind_before", 0),
            quarantined=q_events if isinstance(q_events, list) else [],
            conflicts=result.get("conflicts") or [],
            conflicted=result.get("conflicted") or [],
            post_checkpoint=result.get("post_checkpoint"),
            checkpoint=result.get("checkpoint"),
        )

    msg = result.get("message") or (
        f"Synced MAIN into worktree {target_sid} "
        f"(new HEAD {result.get('new_head', '?')})."
    )
    return ToolResult.ok(
        msg,
        merged=True,
        new_head=result.get("new_head", ""),
        behind_before=result.get("behind_before", 0),
        quarantined=q_events if isinstance(q_events, list) else [],
        conflicts=[],
    )


# ── git_worktree_checkpoint ──────────────────────────────


class GitWorktreeCheckpointParams(BaseModel):
    """Parameters for git_worktree_checkpoint tool."""

    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(
        description="Checkpoint commit message.",
        json_schema_extra={
            "aliases": ["message", "commitMessage", "commit_message", "summary"]
        },
    )
    branch_name: str | None = Field(
        default=None,
        alias="branchName",
        description="Optional branch/task name (for API compatibility).",
        json_schema_extra={
            "aliases": ["branchName", "branch_name", "branch", "taskName", "task_name"]
        },
    )


@tool(
    "git_worktree_checkpoint",
    "Stage all changes and create a checkpoint commit in the active "
    "worktree.",
    requires_workspace=True,
    security_level="standard",
)
async def git_worktree_checkpoint_tool(
    params: GitWorktreeCheckpointParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Create a checkpoint commit in the worktree."""
    from hiveweave.services.git_worktree import GitWorktreeService

    wt_ctx = await _get_worktree_context(agent_id, ctx)
    if isinstance(wt_ctx, ToolResult):
        return wt_ctx
    workspace_path, short_id, _ = wt_ctx

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(workspace_path)

    message = params.message or "checkpoint"
    result = await gwt.checkpoint(workspace_path, short_id, str(message))
    if result.get("success"):
        # T1.2: message 必须一并透出（剥离说明 / 忽略文件警告都在里面）——
        # 只回 hash 的话 service 层写的说明 Agent 一个字也看不到。
        parts = [
            f"Checkpoint saved: "
            f"{result.get('commit', result.get('hash', 'unknown'))}"
        ]
        result_message = (result.get("message") or "").strip()
        if result_message:
            parts.append(result_message)
        return ToolResult.ok("\n".join(parts))
    return ToolResult.err(result.get("message", "Failed to checkpoint"))


# ═══════════════════════════════════════════════════════════════════════
# Section 3: Other tools (message_user, webfetch)
# ═══════════════════════════════════════════════════════════════════════


# ── message_user ─────────────────────────────────────────


class MessageUserParams(BaseModel):
    """Parameters for message_user tool."""

    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(
        description="Message body to send to the user.",
        json_schema_extra={"aliases": ["content", "body", "text"]},
    )
    priority: str | None = Field(
        default=None,
        description="Message priority: 'normal' or 'urgent'.",
        json_schema_extra={"aliases": ["level"]},
    )
    images: list[str] | None = Field(
        default=None,
        description=(
            "Optional images shown to the user together with the message "
            "(效果图/截图). Each item is a data URL, an http(s):// image "
            "URL, or a screenshot path under .hiveweave/reports/ (平台自动"
            "读取内联，无需自己转 base64); max 5 images, each up to ~2MB."
        ),
        json_schema_extra={"aliases": ["image", "picture", "screenshot"]},
    )

    @field_validator("images", mode="before")
    @classmethod
    def _coerce_images(cls, v: Any) -> Any:
        # LLM 常把数组字段传成裸字符串/JSON 字符串 —— 统一收成 list
        return coerce_to_list(v)


# message_user 图片上限（件3 2026-09-05）：≤5 张、单张 ~2MB 软上限。
# 2MB 二进制经 base64 膨胀 ~1.37x → 字符数上限取 2_800_000。
_MESSAGE_USER_MAX_IMAGES = 5
_MESSAGE_USER_MAX_IMAGE_CHARS = 2_800_000

# 报告截图直传（2026-09-05）：CEO 无 bash/SOURCE_WRITE（硬门），无法自己把
# 验收截图转 base64 —— images 允许直接传项目 `.hiveweave/reports/` 下的截图
# 路径，平台读文件转 data URL 内联（与既有 data URL 同形态，前端零改动）。
_MESSAGE_USER_REPORT_PREFIX = ".hiveweave/reports/"
_MESSAGE_USER_REPORT_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
})
_MESSAGE_USER_REPORT_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# 2MiB 二进制 base64 后 ~2.80M chars，恰好收在单张字符上限内。
_MESSAGE_USER_REPORT_MAX_BYTES = 2 * 1024 * 1024


def _message_user_reports_root(ctx: Any, workspace: str) -> Path | None:
    """定位项目 ``.hiveweave/reports/``（验收截图的宿主目录）。

    优先 ctx.extra.project_root（executor 注入），回退 infer_project_root
    （剥离 worktree 前缀 —— reports 落在 MAIN 项目根，不在叶子 worktree）。
    定位失败 → None，报告路径条目走 fail-closed 白名单拒绝。
    """
    project_root: Any = None
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        project_root = extra.get("project_root")
    if not project_root:
        try:
            from hiveweave.tools.file import infer_project_root

            project_root = infer_project_root(workspace or "")
        except Exception:
            return None
    root = str(project_root or "").strip()
    if not root:
        return None
    return Path(root) / ".hiveweave" / "reports"


def _normcase_inside(child: str, base: str) -> bool:
    """``child`` 是否落在 ``base`` 内（normcase 宽松，Windows 大小写/分隔符）。"""
    c = os.path.normcase(os.path.normpath(child))
    b = os.path.normcase(os.path.normpath(base))
    if c == b:
        return True
    if not b.endswith(os.sep):
        b += os.sep
    return c.startswith(b)


def _report_image_error(i: int, problem: str, raw: str) -> str:
    """报告截图路径解析/读取失败 —— fail-closed 报错附处方，不静默丢图。"""
    return (
        f"message_user rejected: images[{i}] {problem}（收到：{raw}）。"
        "处方：确认路径在项目 .hiveweave/reports/ 下、指向已生成的截图"
        "（browse 取证保存后才可引用），后缀限 .png/.jpg/.jpeg/.gif/.webp；"
        "或改用 data:image/… / http(s)://… 图片串。"
    )


def _resolve_report_image_path(
    reports_root: Path, raw: str
) -> tuple[Path | None, str | None]:
    """把 images 条目解析为 reports 内的图片文件；返回 (path, err)。

    接受两种形态：``.hiveweave/reports/…`` 项目根相对路径，或规范化后仍
    落在 reports 内的绝对路径。防逃逸双确认：① normpath 字符串前缀校验
    （含 ``..`` 直接拒）；② resolve() 展开符号链接后再次前缀校验。
    返回 (None, None) 表示「不是报告路径形态」，交回白名单拒绝分支。
    """
    p = raw.replace("\\", "/")
    if ".." in p.split("/"):
        return None, "路径不得包含 '..'（防穿越）"
    from hiveweave.tools.file import normalize_input_path

    p = normalize_input_path(p)
    if Path(p).is_absolute():
        if not _normcase_inside(p, str(reports_root)):
            return None, None  # reports 外绝对路径 → 白名单拒绝分支
        candidate = Path(p)
    else:
        s = p
        while s.startswith("./"):
            s = s[2:]
        if not s.lower().startswith(_MESSAGE_USER_REPORT_PREFIX):
            return None, None  # 非报告路径形态 → 白名单拒绝分支
        candidate = reports_root / s[len(_MESSAGE_USER_REPORT_PREFIX):]
        # 注意：remainder 带盘符/UNC 时 pathlib 会整体替换（构造即越界），
        # 真正的兜底是下方 resolve 展开后的二次前缀校验——勿删。
    try:
        resolved = candidate.resolve()
        resolved_root = reports_root.resolve()
    except OSError:
        return None, "路径无法解析"
    if not _normcase_inside(str(resolved), str(resolved_root)):
        return None, "符号链接展开后越出 .hiveweave/reports/（防逃逸）"
    if candidate.suffix.lower() not in _MESSAGE_USER_REPORT_IMAGE_EXTS:
        return None, "不是图片文件"
    return candidate, None


def _read_report_image_data_url(path: Path) -> tuple[str | None, str | None]:
    """读报告截图并转 data URL（≤2MiB；to_thread 读防阻塞事件循环）。"""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None, "截图文件不存在"
    except OSError:
        return None, "截图文件不可访问"
    if size > _MESSAGE_USER_REPORT_MAX_BYTES:
        return None, (
            f"截图超过单张 ~2MB 上限（{size} bytes），请压缩或降分辨率后重试"
        )
    try:
        data = path.read_bytes()
    except OSError:
        return None, "截图文件读取失败"
    mime = _MESSAGE_USER_REPORT_EXT_MIME.get(path.suffix.lower(), "image/png")
    b64 = base64.b64encode(data).decode("ascii")
    # stat→read 之间文件可增长（browse 正在写截图是常态）：读后二次限幅。
    if len(b64) > _MESSAGE_USER_MAX_IMAGE_CHARS:
        return None, (
            f"截图超过单张 ~2MB 上限（base64 {len(b64)} chars），"
            "请压缩或降分辨率后重试"
        )
    return f"data:{mime};base64,{b64}", None


def _validate_message_user_images(
    images: list[str],
    reports_root: Path | None = None,
) -> tuple[list[str], str | None]:
    """校验并解析 images 参数；返回 (落库 images, 错误文案或 None)。

    - data:image/… 与 http(s)://… 原样保留（既有前缀白名单）。
    - `.hiveweave/reports/` 下的截图路径：平台读文件转 data URL 内联
      （与既有 data URL 同形态，前端零改动）。解析/读取失败 → 报错附
      处方（fail-closed，绝不静默丢图）。
    """
    if len(images) > _MESSAGE_USER_MAX_IMAGES:
        return [], (
            f"message_user rejected: images 超过 {_MESSAGE_USER_MAX_IMAGES} 张上限"
            f"（收到 {len(images)} 张）。处方：压缩合并图片，或减少张数、"
            "分多条 message_user 发送。"
        )
    resolved: list[str] = []
    for i, img in enumerate(images, 1):
        if not isinstance(img, str) or not img.strip():
            return [], (
                f"message_user rejected: images[{i}] 必须是非空字符串"
                "（data:image/ 开头的 data URL、http(s):// 图片 URL，"
                "或 .hiveweave/reports/ 下的截图路径）。"
            )
        raw = img.strip()
        low = raw.lower()
        if low.startswith(("data:image/", "http://", "https://")):
            if len(raw) > _MESSAGE_USER_MAX_IMAGE_CHARS:
                return [], (
                    f"message_user rejected: images[{i}] 超过单张 ~2MB 软上限"
                    f"（{len(raw)} chars）。处方：压缩或降分辨率后重发，"
                    "或减少张数分多条发送。"
                )
            resolved.append(raw)
            continue
        if reports_root is not None:
            path, perr = _resolve_report_image_path(reports_root, raw)
            if path is not None:
                data_url, rerr = _read_report_image_data_url(path)
                if data_url is not None:
                    resolved.append(data_url)
                    continue
                return [], _report_image_error(i, rerr or "截图读取失败", raw)
            if perr is not None:
                return [], _report_image_error(i, perr, raw)
        # 备注②（审计 2026-09-05）：前缀白名单 —— 只收 data:image/ /
        # http(s):// / .hiveweave/reports/ 截图路径三种；裸 base64 缺 mime
        # 头前端 <img> 渲染不出，reports 外本地路径/文本则完全是噪音。
        return [], (
            f"message_user rejected: images[{i}] 不是可渲染的图片串"
            "（仅接受 data:image/…、http(s)://… 或 .hiveweave/reports/ 下的"
            "截图路径）。处方：裸 base64 请补前缀成 data:image/<格式>;base64,"
            "<数据>；验收截图可直接传 .hiveweave/reports/ 下的路径（平台自动"
            "内联），或提供可访问的图片 URL。"
        )
    return resolved, None


# ═══════════════════════════════════════════════════════════════════════
# fixplan #8：交付状态是**算出来的**，不是**声明出来的**
# ═══════════════════════════════════════════════════════════════════════
# 前身（E4 补 / 复盘 P0-1 G4）是「CEO 出口核验」：用一张 8 词中英词表对
# 消息正文做子串匹配，命中才查账本。09-14 实测该形态整体失效——同一语义
# 换成「记录之X（不做完工判断）」/整段法文即绕过，且工具描述把触发条件
# 写给了模型（等于绕过说明书）。
#
# 现形态：判据落在 **project_meta 的状态位**（唯一写者 = 本文件的
# `mark_delivery_complete_tool`，其内部跑三条**状态查询**，不接受任何可
# 影响判定的文本参数）。消息出口**不做任何文本判断**，只把真实状态挂到
# chat_messages.metadata 供用户侧渲染徽章 ⇒ 谎报在用户侧一眼可辨。
#
# 三条实质判据（FAIL 终验 / approved 未 closed / 未读人工消息）本身是
# 状态查询、完全可靠 —— 本次换的是「**何时**检查」，不是「检查什么」。
# （2026-09-19 起为**四条**：+ ``EVIDENCE_NOT_LANDED``，TEST_DSH_64 报告
# #1——它同样只做状态/磁盘查询，不读任何消息文本。）
DELIVERY_STATE_UNMARKED = "unmarked"   # project_meta.delivery_state IS NULL
DELIVERY_STATE_COMPLETE = "complete"
DELIVERY_STATE_BLOCKED = "blocked"     # 平台核验未通过时写入（非 agent 声明）

# 政策码（DSH `blockedReason: {code, message}` 形态）：可断言、可统计、
# 可渲染、可路由；message 只供人读。
POLICY_LEDGER_FAIL_VERDICT = "LEDGER_FAIL_VERDICT"
POLICY_LEDGER_APPROVED_OPEN = "LEDGER_APPROVED_OPEN"
POLICY_INBOX_UNREAD_HUMAN = "INBOX_UNREAD_HUMAN"
# TEST_DSH_64 报告 #1：终验证据滞留产出分支，从未落到 MAIN 工作区磁盘。
POLICY_EVIDENCE_NOT_LANDED = "EVIDENCE_NOT_LANDED"

# 平台自管证据前缀（EVIDENCE_NOT_LANDED 判据只盯这两类路径：它们由
# message_user/平台写入 MAIN，是「证据必须可从 MAIN 看到」的承诺面；
# 业务代码路径走 merge 门，不归本判据管）。
_EVIDENCE_PLATFORM_PREFIXES = (".hiveweave/reports/", ".hiveweave/shared/")

# 未读人工消息在回执里最多列举几条（只供人读；判定用 COUNT，不受此限）
_DELIVERY_UNREAD_SAMPLE = 5


async def _delivery_context(
    agent_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """解析 ``(agent_row, project_id)``；任一不可得 → ``(None, None)``。

    交付状态是**项目级**事实（每项目一个），所以两者缺一不可。
    """
    try:
        from hiveweave.services.org import OrgService

        agent = await OrgService().get_agent(agent_id)
    except Exception as e:  # noqa: BLE001 — 身份解析故障 → 视为"未知"，不猜
        log.debug("delivery_agent_lookup_failed", agent_id=agent_id, error=str(e))
        return None, None
    if not agent:
        return None, None
    try:
        project_id = await get_project_id(agent_id)
    except Exception:  # noqa: BLE001
        project_id = None
    return agent, project_id


async def _delivery_blockers(agent_id: str) -> list[dict[str, str]]:
    """账本一致性**状态判据**（与消息文案完全无关）。

    E4 补原有的三条判据**原样搬入**（判据本身不变，只把返回形态从自由
    文本改成 ``[{code, message}]``，便于断言 / 统计 / 渲染 / 路由）：

      · ``LEDGER_FAIL_VERDICT``  —— 未解决的 FAIL 终验
      · ``LEDGER_APPROVED_OPEN`` —— approved 未 closed 的任务
      · ``INBOX_UNREAD_HUMAN``   —— **自己**未读的人工消息
      · ``EVIDENCE_NOT_LANDED``  —— VERIFY 证据文件未落到 MAIN 磁盘
        （TEST_DSH_64 #1，2026-09-19 补）：已 approved/closed 的 VERIFY
        任务在 ``evidence.files_changed`` 里声称的 ``.hiveweave/reports/``
        / ``.hiveweave/shared/`` 路径必须真实存在于 **MAIN 工作区磁盘**。
        判据口径是「磁盘存在」而非「git 已跟踪」—— browse_main 直写的
        截图是合法 untracked，按 git 跟踪判会误伤；只扫 VERIFY 类 +
        近 7 天（updated_at）+ 上限 50 条做性能护栏。

    未读只计人工消息：系统副本（含平台自身的投递失败回执）不是 CEO 的
    账（TEST_DSH_32 P3）。查询失败 / 无项目 → ``[]``（**fail-open**，与
    原实现一致：判据故障不得把 CEO 卡成"永远标记不了"）。
    """
    blockers: list[dict[str, str]] = []
    try:
        from hiveweave.db import project as project_db

        project_id = await get_project_id(agent_id)
        if not project_id:
            return blockers
        conn = await project_db.get_project_db_by_project_id(project_id)
    except Exception as e:  # noqa: BLE001
        log.debug("delivery_blockers_db_failed", agent_id=agent_id, error=str(e))
        return blockers

    try:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM tasks "
            "WHERE is_archived = 0 AND status NOT IN ('closed','cancelled') "
            "AND upper(json_extract(evidence, '$.verdict')) = 'FAIL'"
        )
        row = await cur.fetchone()
        await cur.close()
        if row and int(row["c"] or 0) > 0:
            blockers.append({
                "code": POLICY_LEDGER_FAIL_VERDICT,
                "message": f"{row['c']} 个未解决的 FAIL 终验",
            })
    except Exception:  # noqa: BLE001 — FAIL 计数失败按无此 blocker（fail-open，见 docstring：判据故障不得把 CEO 卡成"永远标记不了"）
        pass
    try:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM tasks "
            "WHERE is_archived = 0 AND status = 'approved'"
        )
        row = await cur.fetchone()
        await cur.close()
        if row and int(row["c"] or 0) > 0:
            blockers.append({
                "code": POLICY_LEDGER_APPROVED_OPEN,
                "message": f"{row['c']} 个 approved 未 closed 任务",
            })
    except Exception:  # noqa: BLE001 — approved 计数失败按无此 blocker（fail-open，见 docstring）
        pass
    try:
        cur = await conn.execute(
            "SELECT id, from_agent_id, substr(message, 1, 80) AS preview "
            "FROM inbox WHERE to_agent_id = ? AND read = 0 "
            "AND COALESCE(from_agent_id, '') NOT IN ('system', '用户') "
            "ORDER BY created_at ASC LIMIT ?",
            [agent_id, _DELIVERY_UNREAD_SAMPLE],
        )
        rows = await cur.fetchall()
        await cur.close()
        if rows:
            listing = "; ".join(
                f"{str(r['from_agent_id'])[:8]}: {r['preview']}" for r in rows
            )
            blockers.append({
                "code": POLICY_INBOX_UNREAD_HUMAN,
                "message": f"你还有 {len(rows)}+ 条未读人工消息（{listing}）",
            })
    except Exception:  # noqa: BLE001 — 未读列举失败按无此 blocker（fail-open，与前两条计数同语义，不猜）
        pass

    # 判据 4（TEST_DSH_64 报告 #1：证据滞留）：已 approved/closed 的 VERIFY
    # 任务，evidence.files_changed 里带平台自管前缀（.hiveweave/reports/ 或
    # .hiveweave/shared/）的路径必须真的落到 MAIN 工作区磁盘 —— 64 现场
    # 4 份终验 md 只 checkpoint 到 hw/A140/work 分支从未合 MAIN，三条旧
    # 判据零触文件系统 ⇒ ship 照常触发。口径 =「MAIN 磁盘存在」（见
    # docstring：不按 git 跟踪判，browse_main 直写的 untracked 截图合法）。
    # 无文件型证据的任务天然不触发；MAIN workspace 解析不出来按无此
    # blocker（fail-open，同前三条，不猜）。
    try:
        from hiveweave.services.worktree_review import (
            normalize_files_changed,
            project_main_workspace,
        )

        main_ws = await project_main_workspace(str(project_id))
        if main_ws and os.path.isdir(main_ws):
            week_ago_ms = int(time.time() * 1000) - 7 * 86400 * 1000
            cur = await conn.execute(
                "SELECT id, evidence FROM tasks "
                "WHERE is_archived = 0 AND status IN ('approved','closed') "
                "AND kind = 'verify' AND updated_at >= ? "
                "ORDER BY updated_at DESC LIMIT 50",
                [week_ago_ms],
            )
            ev_rows = await cur.fetchall()
            await cur.close()
            missing_bits: list[str] = []
            for ev_row in ev_rows:
                ev_raw = ev_row["evidence"]
                try:
                    ev_obj = (
                        json.loads(ev_raw)
                        if isinstance(ev_raw, str)
                        else ev_raw
                    )
                except Exception:
                    continue
                if not isinstance(ev_obj, dict):
                    continue
                files = (
                    ev_obj.get("files_changed")
                    or ev_obj.get("filesChanged")
                    or []
                )
                for rel in normalize_files_changed(list(files or [])):
                    if not rel.startswith(_EVIDENCE_PLATFORM_PREFIXES):
                        continue
                    if not (Path(main_ws) / rel).exists():
                        missing_bits.append(
                            f"{rel}（任务 {str(ev_row['id'])[:8]}）"
                        )
                        break  # 每任务报一件即可，不刷屏
            if missing_bits:
                blockers.append({
                    "code": POLICY_EVIDENCE_NOT_LANDED,
                    "message": (
                        f"{len(missing_bits)} 份终验证据未落到 MAIN 工作区："
                        + "; ".join(
                            missing_bits[:_DELIVERY_UNREAD_SAMPLE]
                        )
                        + "——需 merge 产出分支或在共享空间补齐"
                    ),
                })
    except Exception:  # noqa: BLE001 — fail-open（docstring：判据故障不得把 CEO 卡成"永远标记不了"）
        pass
    return blockers


async def _delivery_snapshot(agent_id: str) -> dict[str, Any]:
    """标记时刻的核验快照（只供审计 / 展示，**不参与判定**）。

    读数失败不阻断标记（快照是留痕，不是闸门）：标 ``degraded`` 让下游
    知道这份快照不完整，而不是伪造一个全 0 的"干净"读数。
    """
    snap: dict[str, Any] = {
        "tasks_total": 0,
        "tasks_closed": 0,
        "unread_human": 0,
    }
    try:
        from hiveweave.db import project as project_db

        project_id = await get_project_id(agent_id)
        conn = await project_db.get_project_db_by_project_id(project_id)
        cur = await conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status IN ('closed','cancelled') THEN 1 ELSE 0 END) "
            "AS closed FROM tasks WHERE is_archived = 0"
        )
        row = await cur.fetchone()
        await cur.close()
        if row:
            snap["tasks_total"] = int(row["total"] or 0)
            snap["tasks_closed"] = int(row["closed"] or 0)
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM inbox "
            "WHERE to_agent_id = ? AND read = 0 "
            "AND COALESCE(from_agent_id, '') NOT IN ('system', '用户')",
            [agent_id],
        )
        row = await cur.fetchone()
        await cur.close()
        if row:
            snap["unread_human"] = int(row["c"] or 0)
    except Exception as e:  # noqa: BLE001
        log.debug("delivery_snapshot_failed", agent_id=agent_id, error=str(e))
        snap["degraded"] = True
    return snap


async def _read_delivery_state(project_id: str | None) -> dict[str, Any]:
    """读项目级交付状态位（**只读**；写者是 `mark_delivery_complete`）。

    ``delivery_state IS NULL`` / 列不存在 / 无行 / 读失败 → ``state=None``
    ——**未知不猜**（老项目因此显示"未标记完工"而不是"未完成"，避免误伤）。
    """
    empty: dict[str, Any] = {"state": None, "at": None, "snapshot": None}
    if not project_id:
        return empty
    try:
        from hiveweave.db import project as project_db

        conn = await project_db.get_project_db_by_project_id(str(project_id))
        cur = await conn.execute(
            "SELECT delivery_state, delivered_at, delivery_snapshot "
            "FROM project_meta WHERE project_id = ?",
            [str(project_id)],
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return empty
        snapshot = row["delivery_snapshot"]
        if snapshot:
            try:
                snapshot = json.loads(snapshot)
            except (TypeError, ValueError):
                pass  # 坏 JSON 原样透出；判定不用快照
        return {
            "state": row["delivery_state"] or None,
            "at": row["delivered_at"],
            "snapshot": snapshot,
        }
    except Exception as e:  # noqa: BLE001
        log.debug("delivery_state_read_failed", project_id=project_id, error=str(e))
        return empty


async def _write_delivery_state(
    project_id: str, state: str, snapshot: dict[str, Any]
) -> None:
    """写交付状态位 —— **全平台唯一写者**（`mark_delivery_complete`）。

    ``delivered_at`` 存 ISO-8601（`delivery_state` 系三列按详案定为
    TEXT，与 `created_at INTEGER` 的既有惯例刻意不同：这是给人看、可读
    的时刻，不是排序用的毫秒）。project_meta 行缺失时 UPSERT 建行。
    """
    from datetime import datetime, timezone

    from hiveweave.db import project as project_db

    conn = await project_db.get_project_db_by_project_id(str(project_id))
    now_ms = int(time.time() * 1000)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await conn.execute(
        "INSERT INTO project_meta (project_id, delivery_state, delivered_at, "
        "delivery_snapshot, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(project_id) DO UPDATE SET "
        "delivery_state = excluded.delivery_state, "
        "delivered_at = excluded.delivered_at, "
        "delivery_snapshot = excluded.delivery_snapshot, "
        "updated_at = excluded.updated_at",
        [str(project_id), state, stamp, json.dumps(snapshot, ensure_ascii=False), now_ms],
    )
    await conn.commit()


async def delivery_badge_metadata(agent_id: str) -> dict[str, Any] | None:
    """交付状态徽章（挂 ``chat_messages.metadata``，用户侧渲染）。

    **零文本判断**：结果只取决于 project_meta 的状态位 + 三条账本判据，
    与消息正文完全无关 —— 中文 / 英文 / 法文 / 西班牙文 / 否定句 / 复述
    旧拒绝文案，回执与 metadata 逐字相同（详案 §六.1 反措辞守卫）。

    仅 **CEO** 的消息带徽章（徽章语义 = "这次交付声明是否已被平台核验"）；
    非 CEO / 身份或项目解析不出来 → ``None``（**不加、不拦**，行为与改动
    前一致）。任何异常 → ``None``（徽章是观测面，不引入新故障面）。

    ⚠ **不拦消息**是刻意的：徽章与正文并排 ⇒ 谎报在用户侧一眼可辨。
    "a gate the agent can write to is not a gate"（译：agent 能自己写进去
    的闸门，不算闸门）—— 拦反而把状态变成可博弈的措辞游戏。
    """
    try:
        agent, project_id = await _delivery_context(agent_id)
        if not agent or not project_id:
            return None
        from hiveweave.services.policy import infer_role_family

        if infer_role_family(agent) != "ceo":
            return None
        state = await _read_delivery_state(project_id)
        badge: dict[str, Any] = {
            "delivery_state": state.get("state") or DELIVERY_STATE_UNMARKED,
        }
        if state.get("at"):
            badge["delivery_at"] = state["at"]
        if state.get("state") == DELIVERY_STATE_COMPLETE:
            badge["delivery_snapshot"] = state.get("snapshot")
        else:
            # 未标记 / blocked：把**当前**真实阻塞项一并给出（比快照新）
            badge["delivery_blockers"] = await _delivery_blockers(agent_id)
        return badge
    except Exception as e:  # noqa: BLE001
        log.debug("delivery_badge_failed", agent_id=agent_id, error=str(e))
        return None


def delivery_badge_line(badge: dict[str, Any] | None) -> str:
    """把徽章压成一行给人/给 agent 读的文案（工具回执用）。"""
    if not badge:
        return ""
    state = badge.get("delivery_state")
    if state == DELIVERY_STATE_COMPLETE:
        return f"交付状态：✅ 已标记交付完成（{badge.get('delivery_at') or '已核验'}）。"
    if state == DELIVERY_STATE_BLOCKED:
        return "交付状态：⚠️ 未标记完工（上次核验未通过，账本仍有未收口项）。"
    return "交付状态：⚠️ 未标记完工（用户侧会看到该徽章）。"


@tool(
    "message_user",
    "Send a message directly to the human user. The message appears in "
    "the user's chat window. Your project's delivery state is attached to "
    "the message as a badge the user sees (`delivery_state` in metadata): "
    "「未标记完工」 until you call mark_delivery_complete and the platform "
    "verifies the ledger, then 「交付完成」 with a timestamp. The message "
    "itself is never filtered or reworded — say exactly what the real "
    "status is.",
    requires_workspace=False,
    security_level="standard",
)
async def message_user_tool(
    params: MessageUserParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Send a message to the human user.

    fixplan #8：本出口**不做文本判断、不拦截**（旧的 8 词完结断言词表已
    下线）——行为只取决于项目交付状态位。详见
    ``deliverables/fix8-delivery-state-design-2026-09-14.md``。
    """
    if not params.message:
        return ToolResult.err("message_user requires 'message' (body text)")

    # 件3（agent→用户发图 2026-09-05）：可选 images 校验（≤5 张、单张
    # ~2MB 软上限），超限报错并附处方（压缩/降张数）。
    # 报告截图直传（2026-09-05）：`.hiveweave/reports/` 下截图路径在此处
    # 由平台读文件转 data URL 内联（CEO 无 bash/SOURCE_WRITE，转不了 base64）。
    images = params.images or []
    if images:
        # 报告截图读文件+b64 最坏 ~10MiB 同步 IO：to_thread 防阻塞事件循环。
        images, images_err = await asyncio.to_thread(
            _validate_message_user_images,
            images,
            _message_user_reports_root(ctx, workspace),
        )
        if images_err:
            return ToolResult.err(images_err)

    # fixplan #8：本出口**不做任何文本判断**（旧形态的 8 词中英词表已下线）。
    # 只读项目交付状态位，把**真实状态**挂到 metadata 上 ⇒ 无论这条消息写
    # 什么（措辞/语言/否定句/复述旧拒绝文案），行为与 metadata 逐字一致。
    # 不拦截：徽章与正文并排，谎报在用户侧一眼可辨。
    badge = await delivery_badge_metadata(agent_id)

    from hiveweave.services.chat_message import ChatMessageService

    chat_service = ChatMessageService()
    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "role": "assistant",
        "content": params.message,
        "thinking": None,
        "tool_calls": "[]",
        "is_streaming": False,
        "is_background": False,
    }
    metadata: dict[str, Any] = {}
    if images:
        # 落库路径取证：message_user 直接写 chat_messages（用户 Chat 面板
        # 的消息源），不经 inbox 中转 —— images 列直接落这条消息即可，
        # metadata.source 标记来源供追溯。
        payload["images"] = images
        metadata["source"] = "agent_to_user"
    if badge:
        # 交付状态只走 metadata（**不往正文插文本** —— 不污染 agent 的表达）
        metadata.update(badge)
    if metadata:
        payload["metadata"] = metadata
    await chat_service.save_message(payload)

    # Push via WebSocket so the frontend updates in real-time
    try:
        from hiveweave.realtime.event_bus import status_event_bus

        ws_message: dict[str, Any] = {
            "role": "assistant",
            "content": params.message,
        }
        if images:
            ws_message["images"] = images
        if badge:
            ws_message["metadata"] = badge
        await status_event_bus.publish_chat_message(
            agent_id=agent_id,
            message=ws_message,
        )
    except Exception as evt_err:
        log.debug("message_user_event_push_failed", error=str(evt_err))

    return ToolResult.ok("Message sent to user. " + delivery_badge_line(badge))


class MarkDeliveryCompleteParams(BaseModel):
    """Parameters for mark_delivery_complete tool.

    ⚠ **刻意不含任何参数**：交付判定的输入只有平台状态位与账本行，
    agent 无法用措辞/理由/自述影响结果（"agent 能自己写进去的闸门，
    不算闸门"）。新增参数前先回答：它能不能改变判定？
    """

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "mark_delivery_complete",
    "把本项目标记为「交付完成」。平台会在此刻核验项目账本（未解决的 "
    "FAIL 终验 / approved 未 closed 的任务 / 你自己的未读人工消息）；"
    "核验通过才写入完成状态，不通过会列出待收口项并记为 blocked。"
    "目标真正达成时才调用它（困难、不确定、还有有用的工作可做，都不算"
    "完成）。未标记时你发给用户的消息会带「未标记完工」徽章。仅 CEO 可用。",
    requires_workspace=False,
    security_level="standard",
)
async def mark_delivery_complete_tool(
    params: MarkDeliveryCompleteParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """显式动作把「交付完成」落成项目级状态位（fixplan #8）。

    与 DSH `dsh-goal` 同形：``complete`` 是**动作**、状态是 **enum**、
    核验在**动作内部**（不是消费者侧的行内文本启发式）。

    - 判定输入 = project_meta 状态位 + 三条账本状态查询；**没有**文本参数
    - 拒绝时写 ``delivery_state='blocked'`` + policy codes 快照（可断言、
      可统计、可路由），而不是只返回一段文案
    - 仅 CEO（`infer_role_family == "ceo"`）；能力硬门另在
      `services/policy.py::TOOL_CAPABILITY`（DOC_WRITE 为 ceo 独有）
    """
    agent, project_id = await _delivery_context(agent_id)
    if not agent or not project_id:
        return ToolResult.err(
            "无法定位你的身份/项目，交付状态未写入（请稍后重试或向用户"
            "如实说明现状）。"
        )
    from hiveweave.services.policy import infer_role_family

    if infer_role_family(agent) != "ceo":
        return ToolResult.err("仅 CEO 可标记交付完成。")

    blockers = await _delivery_blockers(agent_id)
    if blockers:
        snapshot = {
            "policy_codes": [b["code"] for b in blockers],
            "blockers": [b["message"] for b in blockers],
        }
        try:
            await _write_delivery_state(
                project_id, DELIVERY_STATE_BLOCKED, snapshot
            )
        except Exception as e:  # noqa: BLE001 — 写失败不掩盖拒绝原因
            log.debug(
                "delivery_blocked_write_failed", agent_id=agent_id, error=str(e)
            )
        return ToolResult.err(
            "交付状态未达完成（已记为 blocked）："
            + "；".join(b["message"] for b in blockers)
            + "。请先推动收口，或如实向用户说明现状（此时不写入完成状态）。"
        )

    snapshot = await _delivery_snapshot(agent_id)
    try:
        await _write_delivery_state(
            project_id, DELIVERY_STATE_COMPLETE, snapshot
        )
    except Exception as e:  # noqa: BLE001
        log.warning("delivery_state_write_failed", agent_id=agent_id, error=str(e))
        return ToolResult.err(
            f"核验已通过，但交付状态写入失败（{e}）。请重试；"
            "在此之前用户侧仍会看到「未标记完工」徽章。"
        )
    return ToolResult.ok(
        f"已标记交付完成（核验快照：tasks {snapshot.get('tasks_closed')}/"
        f"{snapshot.get('tasks_total')} closed，未读人工消息 "
        f"{snapshot.get('unread_human')} 条）。"
    )


# ── webfetch ─────────────────────────────────────────────


# SSRF protection: blocked hosts and IP ranges
_SSRF_BLOCKED_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "169.254.169.254",  # cloud metadata
    "metadata.google.internal",
})


def _is_ssrf_blocked(host: str) -> bool:
    """Check if a host is an internal/blocked address."""
    host_lower = host.lower().rstrip(".")
    if host_lower in _SSRF_BLOCKED_HOSTS:
        return True
    # Block private IP ranges
    try:
        ip = ipaddress.ip_address(host_lower)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        pass  # Not an IP, it's a hostname
    # Block common internal hostnames
    if host_lower.endswith(".internal") or host_lower.endswith(".local"):
        return True
    return False


class WebfetchParams(BaseModel):
    """Parameters for webfetch tool."""

    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(
        description="URL to fetch (http or https only).",
        json_schema_extra={"aliases": ["url", "link", "href", "address"]},
    )
    query: str | None = Field(
        default=None,
        description="Optional question or instruction about the page content.",
        json_schema_extra={
            "aliases": ["query", "prompt", "question", "instruction"]
        },
    )


@tool(
    "webfetch",
    "Fetch a URL, extract readable text, and optionally answer a question "
    "about the page. Has SSRF protection.",
    requires_workspace=False,
    security_level="standard",
)
async def webfetch_tool(
    params: WebfetchParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Fetch a URL and convert to text, optionally answering a prompt.

    Security:
    - Scheme validation: only http/https allowed
    - SSRF protection: reject private IPs, localhost, link-local addresses
    - Content-length pre-check: reject >5MB responses
    - Redirect validation: each redirect target checked for SSRF
    """
    import httpx

    url = params.url
    prompt = params.query or ""

    if not url:
        return ToolResult.err("webfetch requires 'url'")

    # 1. URL scheme validation
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ToolResult.err(
            f"Invalid URL scheme: {parsed.scheme}. Only http/https allowed."
        )
    if not parsed.hostname:
        return ToolResult.err("Invalid URL: no hostname")

    # 2. SSRF protection
    hostname = parsed.hostname
    if hostname and _is_ssrf_blocked(hostname):
        return ToolResult.err(
            f"Access denied: cannot fetch internal address {hostname}"
        )

    try:
        # 3. HEAD request to check content-length (if server supports it)
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False
        ) as client:
            try:
                head_resp = await client.head(
                    url, headers={"User-Agent": "HiveWeave/1.0"}
                )
                cl = head_resp.headers.get("content-length")
                if cl and int(cl) > 5_000_000:
                    return ToolResult.err(
                        f"Response too large: {int(cl)} bytes (max 5MB)"
                    )
            except Exception:
                pass  # Some servers don't support HEAD, continue with GET

            # 4. GET request -- don't auto-follow redirects, validate each one
            resp = await client.get(
                url, headers={"User-Agent": "HiveWeave/1.0"}
            )
            redirects = 0
            while resp.is_redirect and redirects < 5:
                loc = resp.headers.get("location", "")
                if not loc:
                    break
                redirect_url = str(httpx.URL(url).join(loc))
                redirect_parsed = urlparse(redirect_url)
                if redirect_parsed.scheme not in ("http", "https"):
                    return ToolResult.err(
                        f"Redirect to non-http scheme blocked: "
                        f"{redirect_parsed.scheme}"
                    )
                redirect_hostname = redirect_parsed.hostname
                if redirect_hostname and _is_ssrf_blocked(redirect_hostname):
                    return ToolResult.err(
                        f"Redirect to internal address blocked: "
                        f"{redirect_hostname}"
                    )
                url = redirect_url
                resp = await client.get(
                    url, headers={"User-Agent": "HiveWeave/1.0"}
                )
                redirects += 1

            # 5. Size check on actual response
            content_length = len(resp.content)
            if content_length > 5_000_000:
                return ToolResult.err(
                    f"Response too large: {content_length} bytes (max 5MB)"
                )
            html = resp.text[:500_000]  # Cap at 500KB for processing
    except Exception as e:
        return ToolResult.err(f"Failed to fetch {url}: {e}")

    # Strip HTML tags for plain text
    text = re.sub(
        r"<script[^>]*>.*?</script>", "", html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"<style[^>]*>.*?</style>", "", text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:20_000]

    if prompt:
        return ToolResult.ok(
            f"Fetched {url} ({len(text)} chars). "
            f"Prompt: {prompt}\n\n{text}"
        )
    return ToolResult.ok(text)


# ── list_available_mcp（45 轮 #9 悬空引用修复）──────────────
# coordinator.py:384/409 早就在让 HR 调这个工具名，但实现只存在于
# services/mcp.py:497 且从未注册——HR 照提示调用必被未知工具 fast-fail。
# 薄注册：直接透传 McpService.list_available_mcp() 的格式化文本。


class ListAvailableMcpParams(BaseModel):
    """list_available_mcp takes no parameters."""

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "list_available_mcp",
    "List MCP servers configured for this deployment (name / transport / "
    "enabled). Read-only directory — attach one with the bind_mcp tool.",
    requires_workspace=False,
    security_level="read",
)
async def list_available_mcp_tool(
    params: ListAvailableMcpParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """List configured MCP servers (formatted text directory)."""
    from hiveweave.services.mcp import mcp_service

    text = await mcp_service.list_available_mcp()
    return ToolResult.ok(text or "(no MCP servers configured)")
