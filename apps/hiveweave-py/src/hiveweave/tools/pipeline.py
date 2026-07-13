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

log = structlog.get_logger()


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
        # Include received args keys to help LLM understand what went wrong
        received_keys = list(raw_args.keys()) if raw_args else []
        return ToolResult.err(
            f"Parameter error in '{tool_name}': {error}. "
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
        # Build a helpful hint for coordinator agents
        try:
            from hiveweave.db import meta_db

            agent_info = await meta_db.get_agent_by_id(agent_id)
            perm_type = (agent_info or {}).get("permission_type", "")
        except Exception:
            perm_type = ""

        if perm_type == "coordinator":
            hint = (
                f"Permission denied: coordinator agents cannot use '{tool_name}'. "
                f"This is a read-only role. Use dispatch_task to assign this work "
                f"to an executor agent, or use send_message to request an executor to do it."
            )
        else:
            hint = f"Permission denied: {tool_name} is blocked for this agent."
        return ToolResult.err(hint).to_dict()

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
        security_error = _check_file_security(params, workspace_path)
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


def _check_file_security(params: Any, workspace_path: str) -> str | None:
    """Unified file operation security check.

    Checks path traversal, .hiveweave protection, and sensitive file patterns.
    Returns an error message string, or ``None`` if the path is safe.
    """
    from .file import _resolve_safe, _check_hiveweave_dir, _is_sensitive

    # Extract file path from params — try common field names
    file_path = (
        getattr(params, "file_path", None)
        or getattr(params, "filePath", None)
        or getattr(params, "path", None)
        or getattr(params, "dirPath", None)
        or getattr(params, "dir_path", None)
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
                err = _check_single_file(patch_path, workspace_path,
                                         _resolve_safe, _check_hiveweave_dir,
                                         _is_sensitive)
                if err:
                    return err
        return None

    if not file_path:
        return None  # No file path to check

    return _check_single_file(file_path, workspace_path,
                              _resolve_safe, _check_hiveweave_dir,
                              _is_sensitive)


def _check_single_file(
    file_path: str,
    workspace_path: str,
    _resolve_safe,
    _check_hiveweave_dir,
    _is_sensitive,
) -> str | None:
    """Check a single file path for security violations."""
    resolved = _resolve_safe(workspace_path, file_path)
    if resolved is None:
        return f"Error: Sandbox violation - path must be within workspace: {file_path}"
    if _check_hiveweave_dir(resolved, workspace_path):
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

    Checks for self-destructive patterns and .hiveweave file operations.
    """
    from .bash import check_self_destructive, _check_hiveweave_command

    command = getattr(params, "command", None) or getattr(params, "cmd", None)
    if not command:
        return None

    if check_self_destructive(command):
        return "Error: Command blocked - self-destructive pattern detected"

    if _check_hiveweave_command(command):
        return "Error: Command blocked - cannot access .hiveweave system directory"

    return None
