"""Unified tool execution pipeline.

All registered tools go through this pipeline:
1. Registry lookup
2. Pydantic parameter validation + alias normalization
3. Permission evaluation (deny/ask/allow)
4. Security checks (auto-injected based on ``security_level``)
5. Tool execution
6. Result normalization (ToolResult → dict, forward-compat dict wrapping)

The pipeline replaces the 450-line ``_dispatch`` if-elif chain in
``executor.py``. Tools registered via ``@tool`` are automatically routed;
unregistered tools fall through to the legacy ``_dispatch`` path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import difflib
import inspect
import structlog

from .base import _TOOL_REGISTRY, ToolDef
from .result import ToolResult

from hiveweave.services.policy import COORDINATOR_WRITE_PREFIXES

log = structlog.get_logger()

# BUG-9: these tools must land in the agent's write worktree, never project root.
_WRITE_REQUIRE_WORKTREE_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "apply_patch",
    "create_directory",
    "delete_file",
    "delete_directory",
    "move_file",
})



# 拒绝时应向 coordinator/HR 展示写白名单的源码写工具
_SOURCE_WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "apply_patch", "delete_file",
    "move_file", "create_directory", "delete_directory",
})


def build_deny_hint(
    tool_name: str, family: str, hard_reason: str | None = None
) -> str:
    """Build a truthful permission-deny hint for the agent model.

    旧文案把 coordinator 一概定性为 'read-only role'，与 policy 不符：
    coordinator/HR 实际拥有受限写白名单（docs/、.hiveweave/shared/ 等）。
    hard_reason 为 policy 硬门的真实拒绝原因（此前只写日志、不返回模型）。
    """
    base = (
        f"Permission denied: {hard_reason}"
        if hard_reason
        else f"Permission denied: {tool_name} is blocked for this agent."
    )
    scope = (
        ", ".join(COORDINATOR_WRITE_PREFIXES)
        + ", charter.md/goals.md/spec.md"
    )
    if family == "ceo":
        if tool_name in _SOURCE_WRITE_TOOLS:
            return (
                f"{base} CEO has DOC_WRITE: create/edit any documentation "
                "(prose/markup). Never modify source code or runtime config — "
                "dispatch_task those to a mid-level coordinator."
            )
        return (
            f"{base} This tool is outside CEO capabilities "
            "(org design, docs, milestone dispatch/review, final verification). "
            "Delegate hands-on code work to your mid-level coordinators."
        )
    if family == "coordinator":
        if tool_name in _SOURCE_WRITE_TOOLS:
            return (
                f"{base} 中层 builder 有 SOURCE_WRITE —— 被拒通常是路径越界："
                f"请在你自己的 worktree（.hiveweave/worktrees/<你的shortId>）"
                f"内改代码；协调文档可写 {scope}。"
            )
        return (
            f"{base} This tool is outside coordinator capabilities "
            "(dispatch/review/merge + writing code in your own worktree). "
            " staffing 走 HR（send_message 提需求）。"
        )
    if family == "hr":
        if tool_name in _SOURCE_WRITE_TOOLS:
            return f"{base} HR agents may write only to: {scope}."
        return (
            f"{base} This tool is outside HR capabilities "
            f"(staffing/org management, docs writes)."
        )
    return base


@dataclass
class ToolContext:
    """Service container passed to tools that need access to shared services.

    Not all tools need this — simple tools (read_file, bash, etc.) don't
    use it. The pipeline inspects the tool function's signature and only
    passes ``ctx`` if the function accepts it.
    """

    org: Any = None
    inbox: Any = None
    charter: Any = None
    roster: Any = None
    skills: Any = None
    templates: Any = None
    dispatch: Any = None
    task_service: Any = None
    alarm_service: Any = None
    review_llm_callback: Any = None
    oneshot_llm_callback: Any = None
    permission: Any = None
    approval: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


LEGACY_DISPATCH_TOOLS = frozenset({
    "review",
    "run_code_review",
    "run_security_audit",
    "run_tests",
    "run_perf_audit",
    "run_full_review",
})
"""未注册但由 ``ToolExecutor._dispatch`` 兜住的 legacy 评审套件。

可达性 = 注册表 ∪ 本集合。新增 legacy 分支必须同步登记，否则会被
未知工具 fast-fail 拦下（这正是我们要的失败模式：漏登记立刻显形，
而不是静默走 120s 审批再报 "Unknown tool"）。
"""

_HALLUCINATED_TOOL_PREFIXES = (
    "self.", "this.", "agent.", "hive.", "hiveweave.", "tools.", "functions.",
)
"""模型幻觉前缀 —— 把工具当成自身方法调用（``self.bash``）。

DSH_33 实测 19 次全部形如 ``self.<真实工具名>``：名字本身是对的，只是多
了个前缀。剥前缀即可给出确切纠正路径，不必让模型去猜。
"""


_TOOL_NAME_FUZZY_CUTOFF = 0.75
"""拼写纠正的相似度下限（difflib）。0.75 实测可救 ``bahs``→``bash``、
``get_task``→``get_tasks``；再低会开始给无关名字乱配。"""


def suggest_tool_name(tool_name: str, known: set[str]) -> tuple[str, str] | None:
    """未知工具名 → ``(建议名, 归因)``，无把握时返回 ``None``。

    归因 ``"prefix"`` = 幻觉前缀（``self.bash`` → ``bash``，名字本身没错）；
    ``"typo"`` = 近似拼写。两者给模型的纠正话术不同，不能混为一谈。
    """
    lowered = tool_name.lower()
    for prefix in _HALLUCINATED_TOOL_PREFIXES:
        if lowered.startswith(prefix):
            stripped = tool_name[len(prefix):]
            if stripped in known:
                return stripped, "prefix"
            if stripped.lower() in known:
                return stripped.lower(), "prefix"
            break
    bare = tool_name.rsplit(".", 1)[-1]
    for candidate in (tool_name, bare):
        matches = difflib.get_close_matches(
            candidate, sorted(known), n=1, cutoff=_TOOL_NAME_FUZZY_CUTOFF
        )
        if matches:
            return matches[0], "typo"
    return None


def build_unknown_tool_error(tool_name: str, known: set[str]) -> str:
    """未知工具的回执 —— 必须带「正确路径」，不能只报裸 unknown tool。

    对齐 DSH ``ToolNotFoundError(toolName, reachableFrom)``（core/tools/
    src/index.ts:494）：模型读到裸 "unknown tool" 会以为部署坏了，于是
    重试或改道，而不是纠正自己的调用方式。
    """
    # 不加 "[Tool Error] " 前缀：agents/streaming.py 回传时已拼 "Error: "，
    # 与同层 "Parameter error in '...'" 的无前缀风格一致。
    base = f"Unknown tool '{tool_name}'."
    suggested = suggest_tool_name(tool_name, known)
    if suggested is None:
        return (
            f"{base} 该名字没有注册在本平台的任何工具上。请只调用本次对话里"
            "声明给你的工具名（可用 get_platform_state 查看你当前的能力范围），"
            "不要自造工具名或调用别的 agent 的工具。"
        )
    name, kind = suggested
    if kind == "prefix":
        return (
            f"{base} Did you mean '{name}'? "
            f"直接用不带 self. 前缀的工具名 '{name}' 调用 —— 工具是平台提供的"
            "调用项，不是你的方法，不要写成 self./this./agent. 形式。"
        )
    return f"{base} Did you mean '{name}'? 请用准确的工具名 '{name}' 重新调用。"


async def _refuse_project_root_write(
    agent_id: str,
    workspace_path: str,
    tool_name: str,
    ctx: ToolContext | None,
) -> str | None:
    """Return an error if a write-eligible agent is about to write on project root.

    TEST6 evening audit P0-2: VERIFY-only / idle writers are intentionally
    routed to MAIN (no personal tree). They must be allowed to write
    throwaway verify scripts on project root — the refuse gate only applies
    when the agent still *needs* a write worktree for code tasks.
    """
    from pathlib import Path

    try:
        from hiveweave.services.git_worktree import (
            agent_gets_write_worktree,
            _assignee_needs_write_worktree,
        )
        from hiveweave.tools.file import infer_project_root

        agent = None
        if ctx is not None and ctx.org is not None:
            agent = await ctx.org.get_agent(agent_id)
        if not agent:
            return None
        if not agent_gets_write_worktree(agent):
            return None  # CEO/HR stay on project root by design

        root = Path(infer_project_root(workspace_path)).resolve()
        ws = Path(workspace_path).resolve()
        if ws != root:
            return None

        short = (agent.get("short_id") or "?").strip()
        # VERIFY-only / no in-flight write tasks → main writes allowed
        project_id = (agent.get("project_id") or "").strip()
        if not project_id and ctx is not None:
            project_id = str(getattr(ctx, "project_id", "") or "")
        if short and project_id:
            try:
                from hiveweave.db import meta as meta_db

                proj_ws = await meta_db.get_project_workspace(project_id) or ""
                if proj_ws and not await _assignee_needs_write_worktree(
                    proj_ws, short
                ):
                    return None
            except Exception as e:
                log.debug(
                    "refuse_root_write_verify_check_failed",
                    error=str(e),
                )

        return (
            f"Refusing {tool_name} on project root for write-worktree agent "
            f"{short}. Your workspace must be "
            f".hiveweave/worktrees/{short}/ — worktree missing or unbound. "
            "Wait for worktree heal / re-hire, or ask coordinator to ensure "
            "your worktree before writing. Do NOT write to main."
        )
    except Exception as e:
        log.debug("refuse_project_root_write_check_failed", error=str(e))
        return None


async def execute_registered_tool(
    tool_name: str,
    raw_args: dict[str, Any],
    agent_id: str,
    workspace_path: str,
    permission: Any,
    approval: Any,
    ctx: ToolContext | None = None,
) -> dict[str, Any] | None:
    """Execute a registered tool through the unified pipeline.

    Returns ``None`` if the tool is not in the registry (caller should
    fall back to the legacy ``_dispatch`` path).
    """
    # 1. Lookup
    tool_def = _TOOL_REGISTRY.get(tool_name)
    if tool_def is None:
        return None  # Not registered — fall back to legacy dispatch

    # 2. Parameter validation + alias normalization (Pydantic)
    params, error = tool_def.validate(raw_args)
    if error:
        log.info(
            "pipeline.args_invalid",
            agent_id=agent_id,
            tool=tool_name,
            error=error[:200],
        )
        # Include received args keys + expected required fields so the LLM
        # can self-correct (empty [] usually means schema was missing upstream).
        received_keys = list(raw_args.keys()) if raw_args else []
        expected = ""
        try:
            schema = tool_def.to_llm_schema()
            req = schema.get("required") or []
            props = list((schema.get("properties") or {}).keys())
            if req:
                expected = f" Required: {req}."
            elif props:
                expected = f" Expected parameters: {props}."
        except Exception:
            pass
        return ToolResult.err(
            f"Parameter error in '{tool_name}': {error}.{expected} "
            f"You provided these parameters: {received_keys}. "
            f"Check the parameter names and make sure all required fields are included."
        ).to_dict()

    # 3. Permission evaluation
    deny_reason: str | None = None
    try:
        if hasattr(permission, "evaluate_detailed"):
            decision, deny_reason = await permission.evaluate_detailed(
                agent_id, tool_name, raw_args
            )
        else:
            decision = await permission.evaluate(agent_id, tool_name, raw_args)
    except Exception as exc:  # noqa: BLE001
        log.error("pipeline.permission_failed", error=str(exc))
        return ToolResult.err(f"Error: Permission check failed: {exc}").to_dict()

    if decision == "deny":
        # 如实提示：硬门 / 用户 deny / 工具表 原因 + 角色指引
        try:
            from hiveweave.db import meta as meta_db
            from hiveweave.services.policy import infer_role_family

            agent_info = await meta_db.get_agent_by_id(agent_id)
            family = infer_role_family(agent_info or {})
        except Exception:
            family = ""

        return ToolResult.blocked_err(
            build_deny_hint(tool_name, family, deny_reason)
        ).to_dict()

    if decision == "ask":
        from hiveweave.services.approval import PermissionRejected, PermissionTimeout

        try:
            await approval.request_permission(
                agent_id=agent_id,
                tool_name=tool_name,
                tool_args=raw_args,
                description=f"Agent {agent_id} wants to use {tool_name}",
            )
        except PermissionTimeout:
            return ToolResult.blocked_err(
                "Permission request timed out (120s). The user may be away."
            ).to_dict()
        except PermissionRejected as exc:
            return ToolResult.blocked_err(f"Permission rejected: {exc}").to_dict()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.err(f"Error: Approval request failed: {exc}").to_dict()

    # BUG-9: writers must not silently dump files on project root when their
    # worktree is missing — refuse and point them at ensure/heal.
    # 平台路由护栏拒绝 → blocked_err（H3 分流），与权限/沙箱拒绝一致。
    if tool_name in _WRITE_REQUIRE_WORKTREE_TOOLS:
        refuse = await _refuse_project_root_write(
            agent_id, workspace_path, tool_name, ctx
        )
        if refuse:
            return ToolResult.blocked_err(refuse).to_dict()

    # 4. Security checks (auto-injected based on security_level)
    if tool_def.security_level == "file_op":
        project_root = None
        if ctx is not None:
            project_root = ctx.extra.get("project_root")
        # P1 (§5.5b①)：读工具白名单需在 pipeline 层一并放行外部只读目录
        # （否则在 file.py 之前就被 `path must be within project` 拦下）。
        from .file import fetch_additional_read_dirs, infer_project_root

        extra_read_dirs = await fetch_additional_read_dirs(
            project_root or infer_project_root(workspace_path)
        )
        security_error = _check_file_security(
            params, workspace_path, tool_name=tool_name, project_root=project_root,
            extra_read_dirs=extra_read_dirs,
        )
        if security_error:
            return ToolResult.blocked_err(security_error).to_dict()
    elif tool_def.security_level == "shell":
        security_error = _check_shell_security(params)
        if security_error:
            return ToolResult.blocked_err(security_error).to_dict()

    # 5. Execute tool
    try:
        # Check if the tool function accepts a ctx parameter
        sig = inspect.signature(tool_def.execute_fn)
        accepts_ctx = "ctx" in sig.parameters
        if accepts_ctx and ctx is not None:
            result = await tool_def.execute_fn(params, agent_id, workspace_path, ctx=ctx)
        else:
            result = await tool_def.execute_fn(params, agent_id, workspace_path)
    except Exception as exc:  # noqa: BLE001
        log.error("pipeline.execute_failed", tool=tool_name, error=str(exc))
        return ToolResult.err(f"Error: {type(exc).__name__}: {exc}").to_dict()

    # 6. Normalize result shape
    if isinstance(result, ToolResult):
        return result.to_dict()
    elif isinstance(result, dict):
        # Forward compat: wrap legacy dict returns.
        # blocked 必须显式透传：进 extra 会被 ToolResult 字段恒胜覆盖抹掉
        # （审计 P2，潜伏陷阱）。
        return ToolResult(
            success=result.get("success", True),
            output=result.get("output", ""),
            error=result.get("error"),
            blocked=bool(result.get("blocked")),
            extra={
                k: v
                for k, v in result.items()
                if k not in ("success", "output", "error", "blocked")
            },
        ).to_dict()
    else:
        return ToolResult.ok(str(result)).to_dict()


# ── Security helpers ─────────────────────────────────────


def _check_file_security(
    params: Any,
    workspace_path: str,
    tool_name: str = "",
    project_root: str | None = None,
    extra_read_dirs: list[str] | None = None,
) -> str | None:
    """Unified file operation security check.

    Checks path traversal, .hiveweave protection, and sensitive file patterns.
    Returns an error message string, or ``None`` if the path is safe.

    Read tools (read_file / list_files / search_files / …) may resolve
    anywhere under the project root; write tools stay inside workspace_path.
    """
    from .file import (
        READ_PATH_TOOLS,
        _check_hiveweave_dir,
        _is_sensitive,
        _resolve_for_read_detail,
        _resolve_safe_detail,
        infer_project_root,
    )

    allow_project_read = tool_name in READ_PATH_TOOLS
    root = project_root or infer_project_root(workspace_path)

    def _resolve_detail(path: str) -> tuple[str | None, str | None]:
        if allow_project_read:
            return _resolve_for_read_detail(
                workspace_path, path, root, extra_read_dirs
            )
        return _resolve_safe_detail(workspace_path, path)

    # Extract file path from params — try common field names
    file_path = (
        getattr(params, "file_path", None)
        or getattr(params, "filePath", None)
        or getattr(params, "path", None)
        or getattr(params, "dirPath", None)
        or getattr(params, "dir_path", None)
        or getattr(params, "directory", None)
        or getattr(params, "source_path", None)
        or getattr(params, "destination_path", None)
    )

    # For patch operations, check each patch's file_path
    patches = getattr(params, "patches", None)
    if patches and not file_path:
        for patch in patches:
            patch_path = (
                getattr(patch, "file_path", None)
                or getattr(patch, "filePath", None)
            )
            if patch_path:
                err = _check_single_file(
                    patch_path,
                    workspace_path,
                    _resolve_detail,
                    _check_hiveweave_dir,
                    _is_sensitive,
                    hiveweave_root=root if allow_project_read else workspace_path,
                    allow_project_read=allow_project_read,
                )
                if err:
                    return err
        return None

    # move_file: both source and destination must pass write sandbox
    src = getattr(params, "source_path", None)
    dst = getattr(params, "destination_path", None)
    if src and dst and tool_name == "move_file":
        for p in (src, dst):
            err = _check_single_file(
                p,
                workspace_path,
                _resolve_detail,
                _check_hiveweave_dir,
                _is_sensitive,
                hiveweave_root=workspace_path,
                allow_project_read=False,
            )
            if err:
                return err
        return None

    if not file_path:
        return None  # No file path to check

    return _check_single_file(
        file_path,
        workspace_path,
        _resolve_detail,
        _check_hiveweave_dir,
        _is_sensitive,
        hiveweave_root=root if allow_project_read else workspace_path,
        allow_project_read=allow_project_read,
    )


def _check_single_file(
    file_path: str,
    workspace_path: str,
    _resolve,
    _check_hiveweave_dir,
    _is_sensitive,
    hiveweave_root: str | None = None,
    allow_project_read: bool = False,
) -> str | None:
    """Check a single file path for security violations."""
    resolved, hint = _resolve(file_path)
    if hint is not None:
        return f"Error: {hint}"
    if resolved is None:
        scope = "project" if allow_project_read else "workspace"
        return f"Error: Sandbox violation - path must be within {scope}: {file_path}"
    hw_base = hiveweave_root or workspace_path
    if _check_hiveweave_dir(resolved, hw_base):
        # Allow listing .hiveweave root (read-only, shows subdirs)
        # but block write operations to protected areas
        from pathlib import Path

        from .file import HIVEWEAVE_DIR

        if Path(resolved).name == HIVEWEAVE_DIR:
            return None  # list_files on .hiveweave is allowed
        return "Error: Access denied - cannot modify .hiveweave system directory"
    if _is_sensitive(file_path):
        return f"Error: Access denied - '{file_path}' is a sensitive file"
    return None


def _check_shell_security(params: Any) -> str | None:
    """Shell command security check.

    Checks self-destructive patterns, .hiveweave ops, and platform kill guards.
    """
    from hiveweave.services.process_registry import check_platform_process_kill

    from .bash import check_self_destructive, _check_hiveweave_command

    command = getattr(params, "command", None) or getattr(params, "cmd", None)
    if not command:
        return None

    # check_self_destructive returns (bool, str) tuple — must unpack, not truthy-check
    # (False, "") is truthy as a non-empty tuple, which would block ALL commands
    blocked, _reason = check_self_destructive(command)
    if blocked:
        return "Error: Command blocked - self-destructive pattern detected"

    plat_err = check_platform_process_kill(command)
    if plat_err:
        return f"Error: {plat_err}"

    if _check_hiveweave_command(command):
        return "Error: Command blocked - cannot access .hiveweave system directory"

    # slack-clone_01 P0: 与 bash.py 同一套命令模式护栏，预检早失败
    # （排在 .hiveweave 目标型护栏之后：多重命中时报更具体的原因，同 bash.py）
    from hiveweave.services.command_guard import evaluate_command

    verdict = evaluate_command(command)
    if verdict.blocked:
        return f"Error: Command blocked: {verdict.reason}"

    return None
