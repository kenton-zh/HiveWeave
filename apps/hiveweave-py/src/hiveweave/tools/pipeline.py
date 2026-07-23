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
import inspect
import structlog

from .base import _TOOL_REGISTRY, ToolDef
from .result import ToolResult

from hiveweave.services.policy import COORDINATOR_WRITE_PREFIXES

log = structlog.get_logger()


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
                f"{base} CEO agents may write only to: {scope}. "
                "CEO 不写业务代码 — 骨架/关键路径派给直属中层 coordinator "
                "（dispatch_task），模块实现由中层再派 executor。"
            )
        return (
            f"{base} This tool is outside CEO capabilities "
            "(org design, milestone dispatch/review, final verification). "
            "Delegate hands-on work to your mid-level coordinators."
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
    permission: Any = None
    approval: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


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
    try:
        decision = await permission.evaluate(agent_id, tool_name, raw_args)
    except Exception as exc:  # noqa: BLE001
        log.error("pipeline.permission_failed", error=str(exc))
        return ToolResult.err(f"Error: Permission check failed: {exc}").to_dict()

    if decision == "deny":
        # 如实提示：返回 policy 硬门真实原因 + coordinator/HR 写白名单指引
        try:
            from hiveweave.db import meta as meta_db
            from hiveweave.services.policy import (
                infer_role_family,
                policy_service,
            )

            agent_info = await meta_db.get_agent_by_id(agent_id)
            family = infer_role_family(agent_info or {})
            hard_reason = (
                policy_service.hard_check(agent_info, tool_name, raw_args)
                if agent_info
                else None
            )
        except Exception:
            family, hard_reason = "", None

        return ToolResult.err(
            build_deny_hint(tool_name, family, hard_reason)
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
            return ToolResult.err(
                "Permission request timed out (120s). The user may be away."
            ).to_dict()
        except PermissionRejected as exc:
            return ToolResult.err(f"Permission rejected: {exc}").to_dict()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.err(f"Error: Approval request failed: {exc}").to_dict()

    # 4. Security checks (auto-injected based on security_level)
    if tool_def.security_level == "file_op":
        project_root = None
        if ctx is not None:
            project_root = ctx.extra.get("project_root")
        security_error = _check_file_security(
            params, workspace_path, tool_name=tool_name, project_root=project_root
        )
        if security_error:
            return ToolResult.err(security_error).to_dict()
    elif tool_def.security_level == "shell":
        security_error = _check_shell_security(params)
        if security_error:
            return ToolResult.err(security_error).to_dict()

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
        # Forward compat: wrap legacy dict returns
        return ToolResult(
            success=result.get("success", True),
            output=result.get("output", ""),
            error=result.get("error"),
            extra={
                k: v
                for k, v in result.items()
                if k not in ("success", "output", "error")
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
        _resolve_safe,
        infer_project_root,
        resolve_for_read,
    )

    allow_project_read = tool_name in READ_PATH_TOOLS
    root = project_root or infer_project_root(workspace_path)

    def _resolve(path: str) -> str | None:
        if allow_project_read:
            return resolve_for_read(workspace_path, path, root)
        return _resolve_safe(workspace_path, path)

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
                    _resolve,
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
                _resolve,
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
        _resolve,
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
    resolved = _resolve(file_path)
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

    return None
