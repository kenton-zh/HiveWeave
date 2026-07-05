"""ToolExecutor — permission gating + tool dispatch + output truncation.

契约 02: 工具执行器 — 主分发器
- 接收 tool_name + tool_args，执行对应工具
- 执行前检查权限（PermissionService.evaluate → allow/deny/ask）
- ask → ApprovalService.request_permission（120s 超时）
- 工具输出截断（> 2000 行或 50KB 存临时文件，返回 head+tail 预览）
- 错误处理：工具异常不崩溃，返回 "Error: ..." 字符串
- 临时文件保留 7 天（.hiveweave/tool_outputs/<agent>_<ts>_<tool>.txt）
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

import structlog

from hiveweave.services.approval import (
    ApprovalService, PermissionRejected, PermissionTimeout,
)
from hiveweave.services.permission import PermissionService
from hiveweave.tools.bash import execute_bash, execute_run_command
from hiveweave.tools.file import read_file, write_file, list_files
from hiveweave.tools.grep import execute_grep
from hiveweave.tools.patch import apply_patch
from hiveweave.tools.question import execute_question
from hiveweave.tools.review import execute_review, ReviewLLMCallback
from hiveweave.tools.todowrite import execute_todowrite
from hiveweave.tools.websearch import execute_websearch

log = structlog.get_logger(__name__)

# ── Constants (契约 02) ────────────────────────────────────

TOOL_OUTPUT_MAX_LINES = 2000
TOOL_OUTPUT_MAX_BYTES = 50_000
TOOL_OUTPUT_RETENTION_DAYS = 7
TOOL_OUTPUT_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10MB
PREVIEW_HEAD_LINES = 20
PREVIEW_TAIL_LINES = 5
PREVIEW_TAIL_THRESHOLD = 25  # only include tail if total > 25 lines

APPROVAL_TIMEOUT_S = 120

# Tool name regex for filename sanitization (non-alphanumeric → "_")
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


# ── Result type ────────────────────────────────────────────

class ToolResult(dict):
    """Dict with success/output/error keys (returned by all tools)."""


# ── ToolExecutor ───────────────────────────────────────────

class ToolExecutor:
    """Routes tool calls to implementations with permission gating +
    sandbox checks + output truncation.

    Usage:
        executor = ToolExecutor(permission_service, approval_service)
        result = await executor.execute(agent_id, "bash",
                                        {"command": "ls"}, workspace_path)
        # result: {"success": bool, "output": str, "error": str | None}
    """

    def __init__(
        self,
        permission_service: PermissionService,
        approval_service: ApprovalService,
        review_llm_callback: ReviewLLMCallback | None = None,
    ) -> None:
        self.permission = permission_service
        self.approval = approval_service
        self.review_llm_callback = review_llm_callback

    # ── Public API ────────────────────────────────────────

    async def execute(
        self,
        agent_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        workspace_path: str,
    ) -> dict[str, Any]:
        """Execute a tool call. Returns {success, output, error}."""
        # 1. Strip hiveweave__ prefix
        name = tool_name
        if name.startswith("hiveweave__"):
            name = name[len("hiveweave__"):]

        log.info("tool.execute", agent_id=agent_id, tool=name,
                 args_preview=str(tool_args)[:200])

        # 2. Permission evaluation
        try:
            decision = await self.permission.evaluate(
                agent_id, name, tool_args
            )
        except Exception as exc:  # noqa: BLE001
            log.error("permission.evaluate_failed", error=str(exc))
            return self._error(f"Error: Permission check failed: {exc}")

        if decision == "deny":
            return self._error(
                f"Permission denied: {name} is blocked for this agent."
            )

        if decision == "ask":
            # Request approval (120s timeout)
            try:
                await self.approval.request_permission(
                    agent_id=agent_id,
                    tool_name=name,
                    tool_args=tool_args,
                    description=f"Agent {agent_id} wants to use {name}",
                )
            except PermissionTimeout:
                return self._error(
                    "Permission request timed out (120s). "
                    "The user may be away."
                )
            except PermissionRejected as exc:
                return self._error(f"Permission rejected: {exc}")
            except Exception as exc:  # noqa: BLE001
                return self._error(
                    f"Error: Approval request failed: {exc}"
                )

        # 3. Dispatch to the tool implementation
        try:
            result = await self._dispatch(
                name, tool_args, agent_id, workspace_path
            )
        except Exception as exc:  # noqa: BLE001
            log.error("tool.dispatch_failed", tool=name, error=str(exc))
            return self._error(f"Error: {exc}")

        # 4. Normalize result shape — R7: 统一工具返回契约
        # 所有工具必须返回 {success, output, error} 三字段。此处作为单一保障点，
        # 为任何遗漏字段的工具补默认值（success=True / output="" / error=None），
        # 确保下游消费方（agent / conversation store）总能拿到一致结构。
        if not isinstance(result, dict):
            result = {"success": True, "output": str(result), "error": None}
        result.setdefault("success", True)
        result.setdefault("output", "")
        result.setdefault("error", None)

        # 5. Apply large-output truncation (layer 1)
        if result.get("output"):
            truncated = self._maybe_save_large_output(
                result["output"], agent_id, name, workspace_path
            )
            result["output"] = truncated

        return result

    # ── Dispatch ─────────────────────────────────────────

    async def _dispatch(
        self,
        name: str,
        args: dict[str, Any],
        agent_id: str,
        workspace_path: str,
    ) -> dict[str, Any]:
        """Route to the specific tool implementation by name."""
        if name == "bash":
            command = args.get("command") or ""
            workdir = args.get("workdir") or ""
            timeout = args.get("timeout")
            return await execute_bash(
                command=command,
                workdir=workdir,
                workspace_path=workspace_path,
                timeout_ms=int(timeout) if timeout else None,
            )

        if name == "run_command":
            command = args.get("command") or ""
            cwd = args.get("cwd") or ""
            timeout = args.get("timeout") or 120_000
            return await execute_run_command(
                command=command, cwd=cwd,
                timeout_ms=int(timeout),
                workspace_path=workspace_path,
            )

        if name == "read_file":
            file_path = args.get("filePath") or ""
            offset = int(args.get("offset") or 0)
            limit = int(args.get("limit") or 2000)
            return await read_file(
                file_path=file_path, offset=offset, limit=limit,
                workspace_path=workspace_path,
            )

        if name == "write_file":
            file_path = args.get("filePath") or ""
            content = args.get("content") or ""
            return await write_file(
                file_path=file_path, content=content,
                workspace_path=workspace_path,
            )

        if name == "list_files":
            path = args.get("path") or ""
            return await list_files(
                path=path, workspace_path=workspace_path,
            )

        if name == "grep":
            pattern = args.get("pattern") or ""
            path = args.get("path") or ""
            include = args.get("include")
            head_limit = args.get("head_limit") or args.get("limit")
            context = int(args.get("context") or 0)
            multiline = bool(args.get("multiline") or False)
            return await execute_grep(
                pattern=pattern, path=path, include=include,
                workspace_path=workspace_path,
                head_limit=int(head_limit) if head_limit else None,
                context=context, multiline=multiline,
            )

        if name == "apply_patch":
            return await apply_patch(
                patches=args.get("patches"),
                workspace_path=workspace_path,
                raw_input=args,
            )

        if name == "todowrite":
            todos = args.get("todos") or []
            return await execute_todowrite(
                agent_id=agent_id, todos=todos,
            )

        if name == "question":
            question = args.get("question") or ""
            options = args.get("options")
            return await execute_question(
                agent_id=agent_id, question=question, options=options,
            )

        if name == "websearch":
            query = args.get("query") or ""
            num_results = int(args.get("numResults") or 5)
            return await execute_websearch(
                query=query, num_results=num_results,
            )

        if name in (
            "run_code_review", "run_security_audit", "run_tests",
            "run_perf_audit", "run_full_review",
        ):
            review_type_map = {
                "run_code_review": "code_review",
                "run_security_audit": "security_audit",
                "run_tests": "test_review",
                "run_perf_audit": "perf_audit",
                "run_full_review": "full_review",
            }
            review_type = review_type_map[name]
            file_paths = args.get("filePaths") or []
            test_files = args.get("testFiles") or []
            return await execute_review(
                review_type=review_type,
                file_paths=file_paths,
                test_files=test_files,
                workspace_path=workspace_path,
                call_llm=self.review_llm_callback,
            )

        # Unknown tool — contract 02 error handling
        return self._error(f"Unknown tool: {name}")

    # ── Output truncation (layer 1) ──────────────────────

    def _maybe_save_large_output(
        self,
        output: str,
        agent_id: str,
        tool_name: str,
        workspace_path: str,
    ) -> str:
        """If output exceeds thresholds, save full to file and return preview.

        契约 02:
          - threshold: > 2000 lines OR > 50KB
          - file: .hiveweave/tool_outputs/<agent_id>_<ts>_<safe_tool>.txt
          - cap: 10MB per file
          - preview: head 20 lines + marker + tail 5 lines (tail only if > 25)
        """
        if not output:
            return output

        lines = output.split("\n")
        byte_len = len(output.encode("utf-8", errors="replace"))

        if len(lines) <= TOOL_OUTPUT_MAX_LINES \
                and byte_len <= TOOL_OUTPUT_MAX_BYTES:
            return output

        file_path = self._save_tool_output_file(
            output, agent_id, tool_name, workspace_path
        )

        head = lines[:PREVIEW_HEAD_LINES]
        tail = lines[-PREVIEW_TAIL_LINES:] if len(lines) > PREVIEW_TAIL_THRESHOLD \
            else []

        marker = (
            f"\n\n... [output truncated: {len(lines)} lines, "
            f"{byte_len} bytes. Full output saved to {file_path}] ...\n\n"
        )

        parts = head + [marker] + tail
        return "\n".join(parts)

    @staticmethod
    def _save_tool_output_file(
        output: str,
        agent_id: str,
        tool_name: str,
        workspace_path: str,
    ) -> str:
        """Save the full output to a temp file; return the file path.

        R6: 文件名内嵌创建时间戳（{agent_id}_{ts}_{tool}.txt），写入时 mtime
        也同步记录创建时间。cleanup_tool_outputs 据此判断保留期。
        """
        base_dir = workspace_path or os.getcwd()
        out_dir = Path(base_dir) / ".hiveweave" / "tool_outputs"
        out_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time() * 1000)
        safe_name = _SAFE_NAME_RE.sub("_", tool_name)
        filename = f"{agent_id}_{timestamp}_{safe_name}.txt"
        full_path = out_dir / filename

        encoded = output.encode("utf-8", errors="replace")
        if len(encoded) > TOOL_OUTPUT_FILE_MAX_BYTES:
            capped = encoded[:TOOL_OUTPUT_FILE_MAX_BYTES]
            capped += (
                f"\n\n... [file capped at "
                f"{TOOL_OUTPUT_FILE_MAX_BYTES} bytes]"
            ).encode("utf-8")
        else:
            capped = encoded

        try:
            full_path.write_bytes(capped)
        except OSError as exc:
            log.warning("tool_output.save_failed", error=str(exc))
            return f"<save failed: {exc}>"

        return str(full_path)

    @staticmethod
    def cleanup_tool_outputs(workspace_path: str | None = None) -> None:
        """Delete tool output files older than the retention period (7 days).

        R6: 清理机制 —— 在 main.py 的 lifespan 启动阶段对每个项目工作区调用
        本方法（见 main.py "tool_outputs_cleaned"）。用文件 mtime 判断创建时间，
        删除超过 TOOL_OUTPUT_RETENTION_DAYS（7 天）的临时文件。文件名中的时间戳
        仅用于可读性，实际保留期判断以 mtime 为准（对齐 Elixir/TS 7 天保留策略）。
        """
        base_dir = workspace_path or os.getcwd()
        out_dir = Path(base_dir) / ".hiveweave" / "tool_outputs"
        if not out_dir.exists():
            return

        now = time.time()
        retention_s = TOOL_OUTPUT_RETENTION_DAYS * 86400

        for entry in out_dir.iterdir():
            try:
                mtime = entry.stat().st_mtime
                if now - mtime > retention_s:
                    entry.unlink()
            except OSError:
                continue

    # ── Helpers ──────────────────────────────────────────

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        """Build an error result dict."""
        return {"success": False, "output": "", "error": message}
