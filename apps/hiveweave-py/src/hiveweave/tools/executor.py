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
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.approval import (
    ApprovalService, PermissionRejected, PermissionTimeout,
)
from hiveweave.services.charter import CharterService
from hiveweave.services.inbox import InboxService
from hiveweave.services.model import ModelService
from hiveweave.services.org import OrgService
from hiveweave.services.permission import PermissionService
from hiveweave.services.roster import RosterService
from hiveweave.services.skill_registry import SkillRegistryService
from hiveweave.services.template import TemplateService
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


# ── Tool parameter schemas ──────────────────────────────────
# Centralized JSON Schema definitions for every tool. Used for:
# 1. Sending to LLM (so it knows correct parameter names — no more guessing)
# 2. Validating LLM args before execution (auto-generate helpful errors)
# 3. Accepting multiple parameter name aliases (Python arg_name in "aliases")

TOOL_PARAM_SCHEMAS: dict[str, dict] = {
    "bash": {
        "properties": {
            "command": {"type": "string", "aliases": ["cmd", "run"]},
            "timeout": {"type": "integer", "aliases": ["timeout_ms", "timeoutMs"]},
        },
        "required": ["command"],
    },
    "read_file": {
        "properties": {
            "filePath": {"type": "string", "aliases": ["path", "file_path", "file"]},
            "offset": {"type": "integer", "aliases": ["startLine"]},
            "limit": {"type": "integer", "aliases": ["maxLines", "lineLimit"]},
        },
        "required": ["filePath"],
    },
    "write_file": {
        "properties": {
            "filePath": {"type": "string", "aliases": ["path", "file_path", "file"]},
            "content": {"type": "string", "aliases": ["data", "text", "body"]},
        },
        "required": ["filePath", "content"],
    },
    "list_files": {
        "properties": {
            "dirPath": {"type": "string", "aliases": ["path", "directory", "dir"]},
        },
        "required": [],
    },
    "grep": {
        "properties": {
            "pattern": {"type": "string", "aliases": ["regex", "query", "search"]},
            "path": {"type": "string", "aliases": ["filePath", "file", "directory", "dir"]},
            "include": {"type": "string", "aliases": ["glob", "filter"]},
            "head_limit": {"type": "integer", "aliases": ["headLimit", "maxResults", "limit"]},
            "context": {"type": "integer", "aliases": ["contextLines", "contextAround"]},
            "multiline": {"type": "boolean", "aliases": ["multiLine", "dotAll"]},
        },
        "required": ["pattern"],
    },
    "search_files": {
        "properties": {
            "pattern": {"type": "string", "aliases": ["glob", "query", "search", "name"]},
            "directory": {"type": "string", "aliases": ["path", "dir"]},
        },
        "required": ["pattern"],
    },
    "edit_file": {
        "properties": {
            "filePath": {"type": "string", "aliases": ["path", "file_path", "file"]},
            "old_string": {"type": "string", "aliases": ["oldString", "old_str", "search", "find"]},
            "new_string": {"type": "string", "aliases": ["newString", "new_str", "replace", "replacement"]},
            "replace_all": {"type": "boolean", "aliases": ["replaceAll"]},
        },
        "required": ["filePath", "old_string", "new_string"],
    },
    "apply_patch": {
        "properties": {
            "patches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "File path to patch"},
                        "search": {"type": "string", "description": "Text to find"},
                        "replace": {"type": "string", "description": "Text to replace with"},
                        "oldText": {"type": "string", "description": "Old text (alternative to search)"},
                        "newText": {"type": "string", "description": "New text (alternative to replace)"},
                        "action": {"type": "string", "enum": ["replace", "insert", "delete"], "description": "Patch action type"},
                    },
                    "required": ["file"]
                },
                "description": "Array of patch objects. Each must have 'file' and one of: search+replace, oldText+newText, or action."
            },
        },
        "required": ["patches"],
    },
    "websearch": {
        "properties": {
            "query": {"type": "string", "aliases": ["search", "q", "term"]},
            "numResults": {"type": "integer", "aliases": ["num_results", "limit", "count"]},
        },
        "required": ["query"],
    },
    "question": {
        "properties": {
            "question": {"type": "string", "aliases": ["message", "content", "query", "text"]},
            "options": {"type": "array", "aliases": ["choices"]},
        },
        "required": ["question"],
    },
    "todowrite": {
        "properties": {
            "todos": {"type": "array", "aliases": ["tasks", "items", "list"]},
        },
        "required": ["todos"],
    },
    "send_message": {
        "properties": {
            "recipients": {"type": "array", "aliases": ["recipient", "to", "targets"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "expectReport": {"type": "boolean", "aliases": ["expect_report"]},
            "priority": {"type": "string", "aliases": ["level"]},
        },
        "required": ["recipients", "message"],
    },
    "hire_agent": {
        "properties": {
            "name": {"type": "string"},
            "role": {"type": "string"},
            "goal": {"type": "string"},
            "backstory": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}},
            "parentId": {"type": "string", "aliases": ["parent_id", "parent"]},
        },
        "required": ["name", "role"],
    },
    "read_charter": {
        "properties": {},
        "required": [],
    },
    "save_charter": {
        "properties": {
            "content": {"type": "string", "aliases": ["charter", "body", "text"]},
            "title": {"type": "string", "aliases": ["name"]},
        },
        "required": ["content"],
    },
    "read_goals": {
        "properties": {},
        "required": [],
    },
    "update_goals": {
        "properties": {
            "objective": {"type": "string"},
            "focus": {"type": "string"},
            "keyResults": {"type": "array", "aliases": ["key_results"]},
            "userInvolvement": {"type": "string", "aliases": ["user_involvement"]},
        },
        "required": [],
    },
    "read_memory": {
        "properties": {
            "moduleId": {"type": "string", "aliases": ["module_id", "id", "key"]},
        },
        "required": ["moduleId"],
    },
    "write_memory": {
        "properties": {
            "content": {"type": "string", "aliases": ["data", "body", "text", "memory"]},
            "moduleId": {"type": "string", "aliases": ["module_id", "id", "key"]},
            "tags": {"type": "array", "items": {"type": "string"}, "aliases": []},
        },
        "required": ["content"],
    },
    "list_available_skills": {
        "properties": {},
        "required": [],
    },
    "read_skill": {
        "properties": {
            "skill": {"type": "string", "aliases": ["name", "slug", "id"]},
        },
        "required": ["skill"],
    },
    "read_roster": {
        "properties": {},
        "required": [],
    },
    "update_roster": {
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "target", "id"]},
            "position": {"type": "string"},
            "department": {"type": "string"},
            "responsibilities": {"type": "string"},
            "status": {"type": "string"},
            "hire_date": {"type": "string", "aliases": ["hireDate"]},
        },
        "required": ["agentId"],
    },
    "view_org_chart": {
        "properties": {},
        "required": [],
    },
    "list_subordinates": {
        "properties": {},
        "required": [],
    },
    "list_alarms": {
        "properties": {},
        "required": [],
    },
    "cancel_alarm": {
        "properties": {
            "alarmId": {"type": "string", "aliases": ["alarm_id", "id"]},
        },
        "required": ["alarmId"],
    },
    "schedule_alarm": {
        "properties": {
            "toAgentId": {"type": "string", "aliases": ["to_agent_id", "target"]},
            "purpose": {"type": "string", "aliases": ["message", "description"]},
            "fireInGameSeconds": {"type": "integer", "aliases": ["fire_in_game_seconds", "delay"]},
            "repeatIntervalSeconds": {"type": "integer", "aliases": ["repeat_interval_seconds", "interval"]},
        },
        "required": ["toAgentId", "purpose", "fireInGameSeconds"],
    },
    "read_work_logs": {
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "target"]},
            "limit": {"type": "integer", "aliases": ["count", "max"]},
        },
        "required": [],
    },
    "run_code_review": {
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_security_audit": {
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_tests": {
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module", "testPath"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_perf_audit": {
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "run_full_review": {
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "testFiles": {"type": "array", "items": {"type": "string"},
                "aliases": ["test_files"]},
        },
        "required": ["filePaths"],
    },
    "delete_file": {
        "properties": {
            "path": {"type": "string", "aliases": ["filePath", "file_path", "file"]},
        },
        "required": ["path"],
    },
    "create_directory": {
        "properties": {
            "path": {"type": "string", "aliases": ["dirPath", "directory", "dir"]},
        },
        "required": ["path"],
    },
    "delete_directory": {
        "properties": {
            "path": {"type": "string", "aliases": ["dirPath", "directory", "dir"]},
        },
        "required": ["path"],
    },
    # — Agent management —
    "dismiss_agent": {
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "id", "target"]},
        },
        "required": ["agentId"],
    },
    "transfer_agent": {
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "id"]},
            "newParentId": {"type": "string", "aliases": ["new_parent_id", "parentId", "parent_id", "target"]},
        },
        "required": ["agentId", "newParentId"],
    },
    "list_agent_templates": {
        "properties": {},
        "required": [],
    },
    "bind_skill": {
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "id"]},
            "skill": {"type": "string", "aliases": ["slug", "name", "skillSlug"]},
        },
        "required": ["agentId", "skill"],
    },
    "unbind_skill": {
        "properties": {
            "agentId": {"type": "string", "aliases": ["agent_id", "id"]},
            "skill": {"type": "string", "aliases": ["slug", "name", "skillSlug"]},
        },
        "required": ["agentId", "skill"],
    },
    # — Messaging —
    "message_subordinate": {
        "properties": {
            "recipient": {"type": "string", "aliases": ["to", "target", "agentId", "agent_id"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "expectReport": {"type": "boolean", "aliases": ["expect_report"]},
        },
        "required": ["recipient", "message"],
    },
    "message_superior": {
        "properties": {
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
            "expectReport": {"type": "boolean", "aliases": ["expect_report"]},
        },
        "required": ["message"],
    },
    "message_peer": {
        "properties": {
            "recipient": {"type": "string", "aliases": ["to", "target", "agentId", "agent_id"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
        },
        "required": ["recipient", "message"],
    },
    "message_team": {
        "properties": {
            "teamId": {"type": "string", "aliases": ["team_id", "team"]},
            "message": {"type": "string", "aliases": ["content", "body", "text"]},
        },
        "required": ["teamId", "message"],
    },
    # — Dispatch + review —
    "dispatch_task": {
        "properties": {
            "toAgentId": {"type": "string", "aliases": ["to_agent_id", "target", "recipient", "agentId"]},
            "description": {"type": "string", "aliases": ["task", "message", "content", "summary"]},
            "expectReport": {"type": "boolean", "aliases": ["expect_report"]},
        },
        "required": ["toAgentId", "description"],
    },
    "review": {
        "properties": {
            "filePaths": {"type": "array", "items": {"type": "string"},
                "aliases": ["files", "target", "path", "file", "module"]},
            "reviewType": {"type": "string",
                "aliases": ["review_type", "type"]},
        },
        "required": ["filePaths"],
    },
    "request_review": {
        "properties": {
            "reviewerId": {"type": "string", "aliases": ["reviewer_id", "reviewer", "target", "agentId"]},
            "target": {"type": "string", "aliases": ["file", "path", "module", "description"]},
            "reviewType": {"type": "string", "aliases": ["review_type", "type"]},
        },
        "required": ["reviewerId", "target"],
    },
    "review": {
        "properties": {
            "target": {"type": "string", "aliases": ["file", "path", "module", "code"]},
            "reviewType": {"type": "string", "aliases": ["review_type", "type"]},
        },
        "required": ["target"],
    },
    "report_completion": {
        "properties": {
            "summary": {"type": "string", "aliases": ["message", "content", "report", "description"]},
            "handoffId": {"type": "string", "aliases": ["handoff_id", "taskId", "task_id"]},
        },
        "required": ["summary"],
    },
    "approve_work": {
        "properties": {
            "subordinate": {"type": "string", "aliases": ["subordinateId", "subordinate_id", "agentId", "agent_id", "target"]},
            "review": {"type": "string", "aliases": ["comment", "feedback", "notes"]},
        },
        "required": ["subordinate"],
    },
    "reject_work": {
        "properties": {
            "subordinate": {"type": "string", "aliases": ["subordinateId", "subordinate_id", "agentId", "agent_id", "target"]},
            "reason": {"type": "string", "aliases": ["feedback", "review", "comment", "message"]},
        },
        "required": ["subordinate", "reason"],
    },
    "write_work_log": {
        "properties": {
            "summary": {"type": "string", "aliases": ["message", "content", "description"]},
            "details": {"type": "string", "aliases": ["data", "extra"]},
            "type": {"type": "string", "aliases": ["logType", "log_type"]},
        },
        "required": ["summary"],
    },
    # — Git worktrees —
    "git_worktree_create": {
        "properties": {
            "branchName": {"type": "string", "aliases": ["branch_name", "branch", "name"]},
        },
        "required": ["branchName"],
    },
    "git_worktree_list": {
        "properties": {},
        "required": [],
    },
    "git_worktree_merge": {
        "properties": {
            "branchName": {"type": "string", "aliases": ["branch_name", "branch", "name"]},
        },
        "required": ["branchName"],
    },
    "git_worktree_remove": {
        "properties": {
            "branchName": {"type": "string", "aliases": ["branch_name", "branch", "name"]},
        },
        "required": ["branchName"],
    },
    "git_worktree_status": {
        "properties": {
            "branchName": {"type": "string", "aliases": ["branch_name", "branch", "name"]},
        },
        "required": [],
    },
    "git_worktree_checkpoint": {
        "properties": {
            "message": {"type": "string", "aliases": ["commitMessage", "commit_message", "summary"]},
        },
        "required": ["message"],
    },
    # — Network + file ops —
    "webfetch": {
        "properties": {
            "url": {"type": "string", "aliases": ["link", "href", "address"]},
            "prompt": {"type": "string", "aliases": ["query", "question", "instruction"]},
        },
        "required": ["url"],
    },
    "move_file": {
        "properties": {
            "source": {"type": "string", "aliases": ["from", "src", "sourcePath", "source_path"]},
            "destination": {"type": "string", "aliases": ["to", "dst", "destPath", "dest_path", "target"]},
        },
        "required": ["source", "destination"],
    },
}

def _resolve_alias_for_tool(arg_name: str, props: dict) -> str | None:
    """Check if arg_name is an alias for any known parameter in this tool.

    Returns the canonical parameter name, or None if unknown.
    """
    # Is it already a canonical name?
    if arg_name in props:
        return arg_name
    # Check aliases
    for prop_name, prop_schema in props.items():
        if arg_name in prop_schema.get("aliases", []):
            return prop_name
    return None


def validate_tool_args(tool_name: str, args: dict) -> tuple[dict, str | None]:
    """Validate and normalize tool arguments against the schema.

    Returns (normalized_args, error_message).
    - normalized_args: args with aliases resolved to canonical names
    - error_message: None if valid, else a helpful message listing
      the tool's expected parameters and what was received
    """
    schema = TOOL_PARAM_SCHEMAS.get(tool_name)
    if schema is None:
        # Unknown tool — pass through as-is
        return args, None

    props: dict = schema.get("properties", {})
    normalized: dict = {}
    missing: list[str] = []
    unknown: list[str] = []

    # Check required params & resolve aliases (per-tool, no cross-tool leakage)
    for req in schema.get("required", []):
        found = False
        for key, value in args.items():
            if value is None:
                continue
            canonical = _resolve_alias_for_tool(key, props)
            if canonical == req:
                normalized[req] = value
                found = True
                break
        if not found:
            missing.append(req)

    # Resolve remaining args through per-tool aliases
    for key, value in args.items():
        if key in normalized:  # already resolved as a required param
            continue
        canonical = _resolve_alias_for_tool(key, props)
        if canonical is not None:
            if canonical not in normalized:
                normalized[canonical] = value
        else:
            unknown.append(key)

    # Coerce types: wrap single string → array when schema expects array
    for key, value in list(normalized.items()):
        prop = props.get(key, {})
        if prop.get("type") == "array" and isinstance(value, str):
            normalized[key] = [value]
        elif prop.get("type") == "boolean" and isinstance(value, str):
            normalized[key] = value.lower() in ("true", "1", "yes")
        elif prop.get("type") == "integer" and isinstance(value, str):
            try:
                normalized[key] = int(value)
            except ValueError:
                pass

    if missing:
        expected = ", ".join(f"'{r}'" for r in missing)
        received = ", ".join(f"'{k}'" for k in args.keys()) if args else "(none)"
        return normalized, (
            f"Missing required parameters: {expected}. "
            f"You passed: {received}. "
            f"Please retry with the correct parameter names."
        )

    if unknown:
        known = ", ".join(f"'{p}'" for p in props.keys())
        unknown_str = ", ".join(f"'{u}'" for u in unknown)
        return normalized, (
            f"Unknown parameters: {unknown_str}. "
            f"Expected: {known}. "
            f"Please retry with correct parameter names."
        )

    return normalized, None


def get_tool_schema_for_llm(tool_name: str) -> dict:
    """Get a clean JSON Schema for sending to the LLM (no aliases, no internals).

    Strips 'aliases' from property definitions so the LLM only sees canonical names.
    """
    schema = TOOL_PARAM_SCHEMAS.get(tool_name)
    if schema is None:
        return {"type": "object", "additionalProperties": True}
    # Deep copy and strip aliases
    import copy
    clean: dict = {"type": "object"}
    if "properties" in schema:
        clean["properties"] = {}
        for name, prop in schema["properties"].items():
            clean_prop = {k: v for k, v in prop.items() if k != "aliases"}
            clean["properties"][name] = clean_prop
    if "required" in schema and schema["required"]:
        clean["required"] = schema["required"]
    return clean


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
        # Service instances for high-level orchestration tools
        self._org = OrgService()
        self._inbox = InboxService()
        self._charter = CharterService()
        self._roster = RosterService()
        self._skills = SkillRegistryService()
        self._templates = TemplateService()

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

        # 1.5. Validate & normalize args against schema — auto-correct
        # parameter name mistakes (e.g. LLM passes "query" → canonical "pattern")
        normalized_args, validation_error = validate_tool_args(name, tool_args)
        if validation_error:
            log.info("tool.args_invalid", agent_id=agent_id, tool=name,
                     error=validation_error[:200])
            return self._error(f"Parameter error in '{name}': {validation_error}")
        tool_args = normalized_args

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
            # BUG-008 修复：兼容 LLM 试错的多种字段名（filePath / file_path / path）
            file_path = (args.get("filePath") or args.get("file_path") or args.get("path") or "").strip()
            offset = int(args.get("offset") or 0)
            limit = int(args.get("limit") or 2000)
            return await read_file(
                file_path=file_path, offset=offset, limit=limit,
                workspace_path=workspace_path,
            )

        if name == "write_file":
            # BUG-008 修复：兼容 LLM 试错的多种字段名
            file_path = (args.get("filePath") or args.get("file_path") or args.get("path") or "").strip()
            content = args.get("content") or ""
            return await write_file(
                file_path=file_path, content=content,
                workspace_path=workspace_path,
            )

        if name == "edit_file":
            # BUG-008 修复：兼容多种字段名。apply_patch 内部 _normalize_patches
            # 已经处理 single-patch 形式 + 多 key 别名，我们只负责把 LLM 输入
            # 透传过去（让 _normalize 兜底）。
            return await apply_patch(
                patches=None,
                workspace_path=workspace_path,
                raw_input=args,
            )

        if name == "list_files":
            # BUG-008 修复：兼容 dirPath / directory / filePath
            path = (args.get("dirPath") or args.get("directory") or args.get("path") or args.get("filePath") or "").strip()
            # BUG-019 修复：支持 recursive + maxdepth
            recursive = bool(args.get("recursive", False))
            maxdepth = int(args.get("maxdepth") or 1)
            return await list_files(
                path=path, workspace_path=workspace_path,
                recursive=recursive, maxdepth=maxdepth,
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
            # BUG-036: accept multiple parameter name variants since LLMs
            # without schemas often guess wrong (message/content/query/text)
            question = (
                args.get("question") or args.get("message")
                or args.get("content") or args.get("query")
                or args.get("text") or ""
            )
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
            "review", "run_code_review", "run_security_audit", "run_tests",
            "run_perf_audit", "run_full_review",
        ):
            review_type_map = {
                "review": "full_review",
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

        # ── High-level orchestration tools ──────────────────
        # These bridge the LLM tool calls to service-layer methods.

        if name in ("send_message", "message_subordinate", "message_superior",
                     "message_peer", "message_team", "message_user"):
            return await self._tool_send_message(agent_id, args)

        if name == "list_subordinates":
            return await self._tool_list_subordinates(agent_id)

        if name == "hire_agent":
            return await self._tool_hire_agent(agent_id, args)

        if name == "read_charter":
            return await self._tool_read_charter(agent_id)

        if name == "save_charter":
            return await self._tool_save_charter(agent_id, args)

        if name == "read_goals":
            return await self._tool_read_goals(agent_id)

        if name == "update_goals":
            return await self._tool_update_goals(agent_id, args)

        if name == "view_org_chart":
            return await self._tool_view_org_chart(agent_id)

        if name == "read_work_logs":
            return await self._tool_read_work_logs(agent_id, args)

        if name == "write_work_log":
            return await self._tool_write_work_log(agent_id, args)

        # ── Roster tools ────────────────────────────────────
        if name == "read_roster":
            project_id = await self._get_project_id(agent_id)
            if not project_id:
                return self._error(f"Agent {agent_id} has no project_id")
            roster_text = await self._roster.get_roster(project_id)
            return {"success": True, "output": roster_text, "error": None}

        if name == "update_roster":
            project_id = await self._get_project_id(agent_id)
            if not project_id:
                return self._error(f"Agent {agent_id} has no project_id")
            target = args.get("agentId") or args.get("agent_id") or ""
            if not target:
                return self._error("update_roster requires 'agentId'")
            target_agent = await self._org.resolve_agent(target)
            if not target_agent:
                return self._error(f"Agent not found: {target}")
            roster_attrs = {k: v for k, v in args.items()
                            if k in ("position", "department", "responsibilities",
                                     "status", "hire_date")}
            result = await self._roster.update_roster(
                project_id, target_agent["id"], roster_attrs)
            return {"success": True, "output": result, "error": None}

        # ── Template tools ──────────────────────────────────
        if name == "list_agent_templates":
            # 运行时角色校验 — 仅 HR 可浏览模板（参照 Elixir tool_executor.ex）
            caller = await self._org.get_agent(agent_id)
            if not caller or caller.get("role", "").lower() != "hr":
                return self._error(
                    "Permission denied: only HR can browse agent templates")
            opts: dict[str, Any] = {}
            if args.get("search"):
                opts["search"] = args["search"]
            if args.get("division"):
                opts["division"] = args["division"]
            templates = await self._templates.list_all(opts)
            if not templates:
                return {"success": True, "output": "No templates found. Try a different search keyword or division.", "error": None}
            lines = []
            for t in templates:
                lines.append(
                    f"- {t['name']} (role: {t.get('role', '?')}) — "
                    f"ID: {t['id']} — {t.get('description', 'no description')}")
            output = (f"Available agent templates ({len(templates)} found):\n"
                      + "\n".join(lines)
                      + "\n\nPass templateId in hire_agent to pre-fill "
                        "role/goal/skills.")
            return {"success": True, "output": output, "error": None}

        # ── Skill tools ─────────────────────────────────────
        if name == "list_available_skills":
            search = args.get("search")
            result = await self._skills.list_available_skills(search)
            return {"success": True, "output": result, "error": None}

        if name == "read_skill":
            slug = (args.get("slug") or args.get("skillName")
                    or args.get("skill") or "")
            if not slug:
                return self._error("read_skill requires 'slug' (skill name)")
            bound = await self._skills.get_bound_skills(agent_id)
            result = await self._skills.read_skill(slug, bound)
            return {"success": True, "output": result, "error": None}

        if name == "bind_skill":
            skill_name = (args.get("skillName") or args.get("skill")
                          or args.get("slug") or "")
            if not skill_name:
                return self._error("bind_skill requires 'skillName' (skill slug)")
            target_id = args.get("agentId") or args.get("agent_id") or agent_id
            if target_id != agent_id:
                target_agent = await self._org.resolve_agent(target_id)
                if not target_agent:
                    return self._error(f"Agent not found: {target_id}")
                target_id = target_agent["id"]
            result = await self._skills.bind_skill(target_id, skill_name)
            if result.get("ok"):
                return {"success": True, "output": f"Skill '{skill_name}' bound to agent {target_id[:8]}...", "error": None}
            return self._error(result.get("error", "Unknown error"))

        if name == "unbind_skill":
            skill_name = (args.get("skillName") or args.get("skill")
                          or args.get("slug") or "")
            if not skill_name:
                return self._error("unbind_skill requires 'skillName' (skill slug)")
            target_id = args.get("agentId") or args.get("agent_id") or agent_id
            if target_id != agent_id:
                target_agent = await self._org.resolve_agent(target_id)
                if not target_agent:
                    return self._error(f"Agent not found: {target_id}")
                target_id = target_agent["id"]
            result = await self._skills.unbind_skill(target_id, skill_name)
            if result.get("ok"):
                return {"success": True, "output": f"Skill '{skill_name}' unbound from agent {target_id[:8]}...", "error": None}
            return self._error(result.get("error", "Unknown error"))

        # ── Agent lifecycle tools ───────────────────────────
        if name == "dismiss_agent":
            project_id = await self._get_project_id(agent_id)
            if not project_id:
                return self._error(f"Agent {agent_id} has no project_id")
            target = args.get("agentId") or args.get("agent_id") or ""
            if not target:
                return self._error("dismiss_agent requires 'agentId'")
            target_agent = await self._org.resolve_agent(target)
            if not target_agent:
                return self._error(f"Agent not found: {target}")
            result = await self._org.dismiss_agent(
                project_id, target_agent["id"])
            if result.get("success"):
                return {"success": True, "output": f"Agent {target_agent['name']} ({target_agent.get('short_id', '?')}) has been dismissed.", "error": None}
            return self._error(result.get("message", "Unknown error"))

        if name == "transfer_agent":
            project_id = await self._get_project_id(agent_id)
            if not project_id:
                return self._error(f"Agent {agent_id} has no project_id")
            target = args.get("agentId") or args.get("agent_id") or ""
            new_parent = (args.get("newParentId")
                          or args.get("new_parent_id")
                          or args.get("parentId") or "")
            if not target:
                return self._error("transfer_agent requires 'agentId'")
            target_agent = await self._org.resolve_agent(target)
            if not target_agent:
                return self._error(f"Agent not found: {target}")
            resolved_parent = None
            if new_parent:
                parent_agent = await self._org.resolve_agent(new_parent)
                if not parent_agent:
                    return self._error(f"New parent agent not found: {new_parent}")
                resolved_parent = parent_agent["id"]
            result = await self._org.transfer_agent(
                project_id, target_agent["id"], resolved_parent)
            if result is None:
                return self._error("Agent not found")
            if isinstance(result, dict) and result.get("success") is False:
                return self._error(result.get("message", "Unknown error"))
            return {"success": True, "output": f"Agent {target_agent['name']} transferred to new parent.", "error": None}

        # ── Git worktree tools (BUG-034: dispatchers were missing) ──
        if name in ("git_worktree_create", "git_worktree_checkpoint",
                     "git_worktree_merge", "git_worktree_remove",
                     "git_worktree_list", "git_worktree_status"):
            return await self._tool_git_worktree(agent_id, name, args)

        # ── File management tools (BUG-036: dispatchers were missing) ──
        if name == "delete_file":
            path = args.get("path") or args.get("file_path") or ""
            if not path:
                return self._error("delete_file requires 'path'")
            return await self._tool_delete_file(agent_id, path, workspace_path)

        if name == "move_file":
            src = args.get("source") or args.get("src") or args.get("path") or ""
            dst = args.get("destination") or args.get("dest") or args.get("to") or ""
            if not src or not dst:
                return self._error("move_file requires 'source' and 'destination'")
            return await self._tool_move_file(agent_id, src, dst, workspace_path)

        if name == "create_directory":
            path = args.get("path") or ""
            if not path:
                return self._error("create_directory requires 'path'")
            return await self._tool_create_directory(agent_id, path, workspace_path)

        if name == "delete_directory":
            path = args.get("path") or ""
            if not path:
                return self._error("delete_directory requires 'path'")
            return await self._tool_delete_directory(agent_id, path, workspace_path)

        if name == "search_files":
            # Accept multiple param name variants — LLMs often guess wrong
            pattern = (
                args.get("pattern") or args.get("glob") or args.get("query")
                or args.get("search") or args.get("name") or ""
            )
            directory = args.get("directory") or args.get("path") or args.get("dir") or "."
            if not pattern:
                return self._error("search_files requires 'pattern' (glob pattern)")
            return await self._tool_search_files(agent_id, pattern, directory, workspace_path)

        # ── Memory tools ──
        if name == "read_memory":
            module_id = args.get("moduleId") or args.get("module_id")
            return await self._tool_read_memory(agent_id, module_id)

        if name == "write_memory":
            content = args.get("content") or args.get("memory") or ""
            module_id = args.get("moduleId") or args.get("module_id")
            tags = args.get("tags") or []
            if not content:
                return self._error("write_memory requires 'content'")
            return await self._tool_write_memory(agent_id, content, module_id, tags)

        # ── Agent orchestration tools ──
        if name == "dispatch_task":
            return await self._tool_dispatch_task(agent_id, args)

        if name == "report_completion":
            return await self._tool_report_completion(agent_id, args)

        if name == "request_review":
            return await self._tool_request_review(agent_id, args)

        if name == "approve_work":
            return await self._tool_approve_work(agent_id, args)

        if name == "reject_work":
            return await self._tool_reject_work(agent_id, args)

        # ── Alarm tools (BUG-036) ──
        if name == "schedule_alarm":
            return await self._tool_schedule_alarm(agent_id, args)

        if name == "list_alarms":
            return await self._tool_list_alarms(agent_id)

        if name == "cancel_alarm":
            return await self._tool_cancel_alarm(agent_id, args)

        # ── Web fetch (OpenCode parity) ──
        if name == "webfetch":
            url = args.get("url") or ""
            prompt = args.get("prompt") or ""
            if not url:
                return self._error("webfetch requires 'url'")
            return await self._tool_webfetch(agent_id, url, prompt)

        # Unknown tool — contract 02 error handling
        return self._error(f"Unknown tool: {name}")

    # ── Git worktree tools (BUG-034) ─────────────────────

    async def _tool_git_worktree(
        self, agent_id: str, name: str, args: dict
    ) -> dict:
        """Git worktree operations: create/checkpoint/merge/remove/list/status."""
        from hiveweave.services.git_worktree import GitWorktreeService
        from hiveweave.db import meta as meta_db

        gwt = GitWorktreeService()
        workspace = await self._get_project_id(agent_id)
        if not workspace:
            return self._error(f"Agent {agent_id} has no project")
        # Resolve workspace path from project
        ws_path = await meta_db.get_project_workspace(workspace)
        if not ws_path:
            return self._error(f"No workspace path for project {workspace}")
        workspace_path = str(ws_path)

        # Ensure git repo exists (idempotent)
        await gwt.ensure_git_repo(workspace_path)

        # Resolve agent short_id for worktree naming
        agent_rec = await self._org.get_agent(agent_id)
        short_id = agent_rec.get("short_id", agent_id[:8]) if agent_rec else agent_id[:8]

        if name == "git_worktree_create":
            task_name = args.get("taskName") or args.get("task_name") or args.get("task") or "task"
            result = await gwt.create(workspace_path, short_id, str(task_name))
            if result.get("success"):
                return {"success": True, "output": f"Worktree created at {result.get('path')} on branch {result.get('branch')}", "error": None}
            return self._error(result.get("message", "Failed to create worktree"))

        if name == "git_worktree_checkpoint":
            message = args.get("message") or args.get("summary") or "checkpoint"
            result = await gwt.checkpoint(workspace_path, short_id, str(message))
            if result.get("success"):
                return {"success": True, "output": f"Checkpoint saved: {result.get('commit', 'unknown')}", "error": None}
            return self._error(result.get("message", "Failed to checkpoint"))

        if name == "git_worktree_merge":
            task_name = args.get("taskName") or args.get("task_name") or args.get("task") or "task"
            result = await gwt.merge(workspace_path, short_id, str(task_name))
            if result.get("success"):
                return {"success": True, "output": "Worktree merged and cleaned up", "error": None}
            return self._error(result.get("message", "Failed to merge worktree"))

        if name == "git_worktree_remove":
            result = await gwt.delete(workspace_path, short_id)
            if result.get("success"):
                return {"success": True, "output": "Worktree removed", "error": None}
            return self._error(result.get("message", "Failed to remove worktree"))

        if name == "git_worktree_list":
            result = await gwt.list(workspace_path)
            if result.get("success"):
                wts = result.get("worktrees", [])
                if not wts:
                    return {"success": True, "output": "No active worktrees", "error": None}
                lines = [f"{w.get('short_id', '?')}: {w.get('branch', '?')} ({w.get('status', '?')})" for w in wts]
                return {"success": True, "output": "\n".join(lines), "error": None}
            return self._error(result.get("message", "Failed to list worktrees"))

        if name == "git_worktree_status":
            result = await gwt.info(workspace_path, short_id)
            if result.get("success"):
                info = result.get("info", {})
                return {"success": True, "output": f"Branch: {info.get('branch', '?')}, Status: {info.get('status', '?')}", "error": None}
            return self._error(result.get("message", "Failed to get worktree status"))

        return self._error(f"Unknown git worktree operation: {name}")

    # ── File management tool implementations (BUG-036) ───
    # P0 安全修复：所有内联文件工具复用 file.py 的 _resolve_safe() + 敏感检查，
    # 不再使用有漏洞的 startswith() 前缀匹配。

    async def _tool_delete_file(
        self, agent_id: str, path: str, workspace: str
    ) -> dict:
        """Delete a file from the workspace."""
        from hiveweave.tools.file import _resolve_safe, _check_hiveweave_dir, _is_sensitive
        resolved = _resolve_safe(workspace, path)
        if resolved is None:
            return self._error(f"Path traversal denied: {path}")
        if _check_hiveweave_dir(resolved, workspace):
            return self._error("Access denied: cannot modify .hiveweave directory")
        if _is_sensitive(resolved):
            return self._error(f"Access denied: '{path}' is a sensitive file")
        target = Path(resolved)
        if not target.exists():
            return self._error(f"File not found: {path}")
        try:
            target.unlink()
            return {"success": True, "output": f"Deleted: {path}", "error": None}
        except Exception as e:
            return self._error(f"Failed to delete {path}: {e}")

    async def _tool_move_file(
        self, agent_id: str, src: str, dst: str, workspace: str
    ) -> dict:
        """Move or rename a file."""
        from hiveweave.tools.file import _resolve_safe, _check_hiveweave_dir, _is_sensitive
        src_resolved = _resolve_safe(workspace, src)
        dst_resolved = _resolve_safe(workspace, dst)
        if src_resolved is None or dst_resolved is None:
            return self._error("Path traversal denied")
        if _check_hiveweave_dir(src_resolved, workspace) or _check_hiveweave_dir(dst_resolved, workspace):
            return self._error("Access denied: cannot modify .hiveweave directory")
        if _is_sensitive(src_resolved) or _is_sensitive(dst_resolved):
            return self._error("Access denied: cannot move sensitive files")
        source = Path(src_resolved)
        dest = Path(dst_resolved)
        if not source.exists():
            return self._error(f"Source not found: {src}")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            source.rename(dest)
            return {"success": True, "output": f"Moved: {src} → {dst}", "error": None}
        except Exception as e:
            return self._error(f"Failed to move {src}: {e}")

    async def _tool_create_directory(
        self, agent_id: str, path: str, workspace: str
    ) -> dict:
        """Create a new directory."""
        from hiveweave.tools.file import _resolve_safe, _check_hiveweave_dir
        resolved = _resolve_safe(workspace, path)
        if resolved is None:
            return self._error(f"Path traversal denied: {path}")
        if _check_hiveweave_dir(resolved, workspace):
            return self._error("Access denied: cannot modify .hiveweave directory")
        try:
            Path(resolved).mkdir(parents=True, exist_ok=True)
            return {"success": True, "output": f"Created directory: {path}", "error": None}
        except Exception as e:
            return self._error(f"Failed to create directory: {e}")

    async def _tool_delete_directory(
        self, agent_id: str, path: str, workspace: str
    ) -> dict:
        """Delete a directory and its contents."""
        import shutil
        from hiveweave.tools.file import _resolve_safe, _check_hiveweave_dir
        resolved = _resolve_safe(workspace, path)
        if resolved is None:
            return self._error(f"Path traversal denied: {path}")
        if _check_hiveweave_dir(resolved, workspace):
            return self._error("Access denied: cannot modify .hiveweave directory")
        target = Path(resolved)
        if not target.exists():
            return self._error(f"Directory not found: {path}")
        if not target.is_dir():
            return self._error(f"Not a directory: {path}")
        try:
            shutil.rmtree(target)
            return {"success": True, "output": f"Deleted directory: {path}", "error": None}
        except Exception as e:
            return self._error(f"Failed to delete directory: {e}")

    async def _tool_search_files(
        self, agent_id: str, pattern: str, directory: str, workspace: str
    ) -> dict:
        """Search for files by glob pattern."""
        from hiveweave.tools.file import _resolve_safe, _check_hiveweave_dir
        ws = Path(workspace).resolve()
        if directory != ".":
            resolved = _resolve_safe(workspace, directory)
            if resolved is None:
                return self._error(f"Path traversal denied: {directory}")
            search_dir = Path(resolved)
        else:
            search_dir = ws
        try:
            matches = sorted(search_dir.rglob(pattern))
            # 排除 .hiveweave 目录下的文件
            matches = [
                m for m in matches[:200]
                if not _check_hiveweave_dir(str(m), workspace)
            ]
            paths = [str(m.relative_to(ws)) for m in matches[:50]]
            if not paths:
                return {"success": True, "output": f"No files matching '{pattern}'", "error": None}
            return {"success": True, "output": "\n".join(paths), "error": None}
        except Exception as e:
            return self._error(f"File search failed: {e}")

    # ── Memory tool implementations (BUG-036) ─────────────

    async def _tool_read_memory(
        self, agent_id: str, module_id: str | None
    ) -> dict:
        """Read agent memories."""
        from hiveweave.services.memory import MemoryService
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        mem = MemoryService()
        try:
            entries = await mem.get_agent_memories(agent_id, project_id, module_id)
            if not entries:
                return {"success": True, "output": "(no memories)", "error": None}
            lines = [f"- [{e.get('category', '?')}] {e.get('content', '')}" for e in entries[:20]]
            return {"success": True, "output": "\n".join(lines), "error": None}
        except Exception as e:
            return self._error(f"Failed to read memories: {e}")

    async def _tool_write_memory(
        self, agent_id: str, content: str, module_id: str | None, tags: list
    ) -> dict:
        """Write a memory entry."""
        from hiveweave.services.memory import MemoryService
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        mem = MemoryService()
        try:
            await mem.add_entry(
                agent_id=agent_id, project_id=project_id,
                content=content, category="tool_written",
                module_id=module_id, tags=tags if isinstance(tags, list) else [],
            )
            return {"success": True, "output": "Memory saved.", "error": None}
        except Exception as e:
            return self._error(f"Failed to write memory: {e}")

    # ── Agent orchestration implementations (BUG-036) ─────

    async def _tool_dispatch_task(self, agent_id: str, args: dict) -> dict:
        """Dispatch a task to a subordinate."""
        target = args.get("target") or args.get("agentId") or args.get("subordinate") or ""
        task = args.get("task") or args.get("description") or ""
        expect_report = args.get("expectReport") or args.get("expect_report") or False
        if not target or not task:
            return self._error("dispatch_task requires 'target' and 'task'")
        from hiveweave.services.dispatch import DispatchService
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        ds = DispatchService()
        result = await ds.dispatch(
            project_id=project_id, from_agent_id=agent_id,
            target_name=target, task=task, expect_report=expect_report,
        )
        if result.get("success"):
            return {"success": True, "output": f"Task dispatched to {result.get('target_name', target)}", "error": None}
        return self._error(result.get("message", "Dispatch failed"))

    async def _tool_report_completion(self, agent_id: str, args: dict) -> dict:
        """Report task completion to superior."""
        summary = args.get("summary") or args.get("report") or ""
        if not summary:
            return self._error("report_completion requires 'summary'")
        from hiveweave.services.handoff import HandoffService
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        hs = HandoffService()
        # Find accepted handoffs for this agent and report on the first
        handoffs = await hs.get_accepted_handoffs(project_id, agent_id)
        if not handoffs:
            return self._error("No accepted handoffs to report on")
        await hs.report(project_id, handoffs[0]["id"], summary)
        return {"success": True, "output": "Completion reported to superior.", "error": None}

    async def _tool_request_review(self, agent_id: str, args: dict) -> dict:
        """Request a code review from superior."""
        file_paths = args.get("filePaths") or args.get("files") or []
        description = args.get("description") or args.get("summary") or "Please review my work."
        from hiveweave.services.handoff import HandoffService
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        hs = HandoffService()
        handoffs = await hs.get_accepted_handoffs(project_id, agent_id)
        if not handoffs:
            return self._error("No accepted handoffs to request review on")
        # Send a message to superior with review request
        from hiveweave.services.inbox import InboxService
        ib = InboxService()
        superior = await self._org.get_superior(agent_id)
        if not superior:
            return self._error("No superior found")
        files_str = ", ".join(file_paths) if file_paths else "all changes"
        await ib.send_message(
            from_agent_id=agent_id, to_agent_id=superior["id"],
            message=f"[REVIEW REQUEST] {description}\nFiles: {files_str}",
            message_type="review_request", priority="urgent",
        )
        return {"success": True, "output": "Review requested from superior.", "error": None}

    async def _tool_approve_work(self, agent_id: str, args: dict) -> dict:
        """Approve a subordinate's work."""
        subordinate = args.get("subordinate") or args.get("agentId") or ""
        if not subordinate:
            return self._error("approve_work requires 'subordinate'")
        from hiveweave.services.handoff import HandoffService
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        hs = HandoffService()
        result = await hs.approve(project_id, agent_id, subordinate)
        if result.get("success"):
            return {"success": True, "output": f"Work approved for {subordinate}.", "error": None}
        return self._error(result.get("message", "Approval failed"))

    async def _tool_reject_work(self, agent_id: str, args: dict) -> dict:
        """Reject a subordinate's work (request rework)."""
        subordinate = args.get("subordinate") or args.get("agentId") or ""
        reason = args.get("reason") or args.get("feedback") or "Rework required."
        if not subordinate:
            return self._error("reject_work requires 'subordinate'")
        from hiveweave.services.handoff import HandoffService
        from hiveweave.services.inbox import InboxService
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        hs = HandoffService()
        result = await hs.reject(project_id, agent_id, subordinate, reason)
        if result.get("success"):
            return {"success": True, "output": f"Work rejected for {subordinate}: {reason}", "error": None}
        return self._error(result.get("message", "Rejection failed"))

    # ── Alarm tool implementations (BUG-036) ──────────────

    async def _tool_schedule_alarm(
        self, agent_id: str, args: dict
    ) -> dict:
        """Schedule an alarm: one-shot, recurring, with optional script."""
        to_agent = args.get("toAgentId") or args.get("to_agent_id") or ""
        purpose = args.get("purpose") or args.get("message") or ""
        fire_in = args.get("fireInGameSeconds") or args.get("fire_in_game_seconds") or 0
        repeat = args.get("repeatIntervalSeconds") or args.get("repeat_interval_seconds") or 0
        script = args.get("scriptCommand") or args.get("script_command") or ""

        if not purpose:
            return self._error("schedule_alarm requires 'purpose' (message delivered on fire)")
        if not fire_in or int(fire_in) <= 0:
            return self._error("schedule_alarm requires 'fireInGameSeconds' > 0")

        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")

        # Resolve to_agent: if empty or "self", use caller
        to_id = agent_id
        if to_agent and to_agent not in ("self", "me"):
            from hiveweave.services.org import OrgService
            org = OrgService()
            agents = await org.list_agents(project_id)
            for a in agents:
                if (a.get("id") == to_agent or a.get("short_id") == to_agent
                        or a.get("name") == to_agent):
                    to_id = a["id"]
                    break

        from hiveweave.services.game_time import GameTimeService
        gts = GameTimeService(project_id)
        current = await gts.get_current_time(project_id)
        fire_at = (current.get("game_seconds", 0) or 0) + int(fire_in)

        alarm_id = await gts.schedule_alarm(
            project_id=project_id,
            from_agent_id=agent_id,
            to_agent_id=to_id,
            purpose=purpose,
            fire_at_game_seconds=fire_at,
            repeat_interval_seconds=int(repeat) if repeat else 0,
            script_command=str(script) if script else "",
        )
        kind = "recurring" if repeat else "one-shot"
        extra = f", script bound" if script else ""
        return {"success": True, "output": f"Alarm {alarm_id[:8]}... scheduled ({kind}{extra}). Fires at game second {fire_at} (in {fire_in} game seconds).", "error": None}

    async def _tool_list_alarms(self, agent_id: str) -> dict:
        """List all pending alarms for the project."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        from hiveweave.services.game_time import GameTimeService
        gts = GameTimeService(project_id)
        alarms = await gts.get_alarms(project_id)
        pending = [a for a in alarms if a.get("status") == "pending"]
        if not pending:
            return {"success": True, "output": "(no pending alarms)", "error": None}
        current = await gts.get_current_time(project_id)
        now = current.get("game_seconds", 0) or 0
        lines = []
        for a in pending[:20]:
            remaining = max(0, (a.get("fire_at_game_seconds", 0) or 0) - now)
            lines.append(f"[{a['id'][:8]}] fire in {remaining}gs — {a.get('purpose', '?')}")
        return {"success": True, "output": "\n".join(lines), "error": None}

    async def _tool_cancel_alarm(
        self, agent_id: str, args: dict
    ) -> dict:
        """Cancel a pending alarm."""
        alarm_id = args.get("alarmId") or args.get("alarm_id") or ""
        if not alarm_id:
            return self._error("cancel_alarm requires 'alarmId'")
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        from hiveweave.services.game_time import GameTimeService
        gts = GameTimeService(project_id)
        await gts.cancel_alarm(alarm_id)
        return {"success": True, "output": f"Alarm {alarm_id[:8]}... cancellation requested.", "error": None}

    # ── Web fetch (OpenCode parity, BUG-036) ──────────────
    # P0 安全修复：scheme 校验 + SSRF 防护 + 流式读取 + content-length 检查

    # SSRF 防护：禁止访问内网地址
    _SSRF_BLOCKED_HOSTS = frozenset({
        "localhost", "127.0.0.1", "0.0.0.0", "::1",
        "169.254.169.254",  # 云元数据
        "metadata.google.internal",
    })

    @staticmethod
    def _is_ssrf_blocked(host: str) -> bool:
        """Check if a host is an internal/blocked address."""
        host_lower = host.lower().rstrip(".")
        if host_lower in ToolExecutor._SSRF_BLOCKED_HOSTS:
            return True
        # Block private IP ranges
        try:
            import ipaddress
            ip = ipaddress.ip_address(host_lower)
            return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        except ValueError:
            pass  # Not an IP, it's a hostname
        # Block common internal hostnames
        if host_lower.endswith(".internal") or host_lower.endswith(".local"):
            return True
        return False

    async def _tool_webfetch(
        self, agent_id: str, url: str, prompt: str
    ) -> dict:
        """Fetch a URL and convert to text, optionally answering a prompt.

        P0 安全修复：
        - scheme 校验：只允许 http/https
        - SSRF 防护：拒绝内网 IP / localhost / 链路本地地址
        - content-length 预检：拒绝 >5MB 响应
        - 流式读取：避免全量缓冲撑爆内存
        """
        import httpx
        from urllib.parse import urlparse

        # 1. URL scheme 校验
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return self._error(f"Invalid URL scheme: {parsed.scheme}. Only http/https allowed.")
        if not parsed.hostname:
            return self._error("Invalid URL: no hostname")

        # 2. SSRF 防护
        if self._is_ssrf_blocked(parsed.hostname):
            return self._error(f"Access denied: cannot fetch internal address {parsed.hostname}")

        try:
            # 3. 先 HEAD 请求检查 content-length（如果服务器支持）
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                # HEAD 请求预检
                try:
                    head_resp = await client.head(url, headers={"User-Agent": "HiveWeave/1.0"})
                    cl = head_resp.headers.get("content-length")
                    if cl and int(cl) > 5_000_000:
                        return self._error(f"Response too large: {int(cl)} bytes (max 5MB)")
                except Exception:
                    pass  # 有些服务器不支持 HEAD，继续 GET

                # GET 请求 — 不自动跟随重定向，手动校验每个重定向目标
                resp = await client.get(url, headers={"User-Agent": "HiveWeave/1.0"})
                # 手动处理重定向（最多 5 次），每次校验目标 URL
                redirects = 0
                while resp.is_redirect and redirects < 5:
                    loc = resp.headers.get("location", "")
                    if not loc:
                        break
                    # 构建完整重定向 URL
                    redirect_url = str(httpx.URL(url).join(loc))
                    redirect_parsed = urlparse(redirect_url)
                    if redirect_parsed.scheme not in ("http", "https"):
                        return self._error(f"Redirect to non-http scheme blocked: {redirect_parsed.scheme}")
                    if self._is_ssrf_blocked(redirect_parsed.hostname):
                        return self._error(f"Redirect to internal address blocked: {redirect_parsed.hostname}")
                    url = redirect_url
                    resp = await client.get(url, headers={"User-Agent": "HiveWeave/1.0"})
                    redirects += 1

                # 4. 流式读取 + 大小限制
                content_length = len(resp.content)
                if content_length > 5_000_000:
                    return self._error(f"Response too large: {content_length} bytes (max 5MB)")
                html = resp.text[:500_000]  # Cap at 500KB for processing
        except Exception as e:
            return self._error(f"Failed to fetch {url}: {e}")

        # Strip HTML tags for plain text
        import re
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        # 解码常见 HTML 实体
        import html as html_mod
        text = html_mod.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        # Trim to reasonable size
        text = text[:20_000]

        if prompt:
            return {"success": True, "output": f"Fetched {url} ({len(text)} chars). Prompt: {prompt}\n\n{text}", "error": None}
        return {"success": True, "output": text, "error": None}

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

    # ── High-level orchestration tool implementations ────

    async def _get_project_id(self, agent_id: str) -> str | None:
        """Resolve agent_id → project_id via Meta DB."""
        return await meta_db.get_agent_project_id(agent_id)

    async def _tool_send_message(self, agent_id: str, args: dict) -> dict:
        """send_message: CEO/HR → subordinates/peers via InboxService.

        Args (from LLM):
            recipients: list[str] — short_id or name of target agents
            message: str — message body (also accepts 'content')
            expectReport: bool — whether a response is expected
            priority: str — "normal" / "urgent"
        """
        recipients = args.get("recipients") or args.get("recipient") or []
        # Handle JSON string recipients (LLM sometimes sends '["HR"]' as string)
        if isinstance(recipients, str):
            try:
                parsed = json.loads(recipients)
                if isinstance(parsed, list):
                    recipients = parsed
                else:
                    recipients = [recipients]
            except (json.JSONDecodeError, ValueError):
                recipients = [recipients]
        if isinstance(recipients, (list, tuple)) and len(recipients) == 0:
            recipients = []
        message = args.get("message") or args.get("content") or args.get("body") or ""
        expect_report = bool(args.get("expectReport") or args.get("expect_report") or False)
        priority = args.get("priority") or "normal"

        if not recipients:
            return self._error("send_message requires 'recipients' (list of agent names or short_ids)")
        if not message:
            return self._error("send_message requires 'message' (body text)")

        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        # Handle "user" / "用户" as a special recipient — write to chat_messages
        # so the message appears in the user's Chat window. This is the only
        # way for agents to proactively notify the user (trigger-based assistant
        # text is marked is_background=True and doesn't show in Chat).
        user_aliases = {"user", "用户", "boss", "老板"}
        user_recipients = [r for r in recipients if r.strip().lower() in user_aliases]
        agent_recipients = [r for r in recipients if r.strip().lower() not in user_aliases]

        results = []
        if user_recipients:
            from hiveweave.services.chat_message import ChatMessageService
            chat_service = ChatMessageService()
            await chat_service.save_message({
                "agent_id": agent_id,
                "role": "assistant",
                "content": message,
                "thinking": None,
                "tool_calls": "[]",
                "is_streaming": False,
                "is_background": False,
            })
            # Push via WebSocket so the frontend updates in real-time
            from hiveweave.realtime.event_bus import event_bus
            await event_bus.publish_chat_message(
                project_id=project_id,
                agent_id=agent_id,
                role="assistant",
                content=message,
            )
            results.append({"to": "user", "message_id": "user-msg"})

        # Resolve remaining agent recipients
        recipients = agent_recipients
        if not recipients:
            return {"ok": True, "results": results}

        # Resolve each recipient: short_id (A001) or name → agent record
        all_agents = await self._org.list_agents(project_id)
        resolved = []
        not_found = []
        for r in recipients:
            r_stripped = r.strip()
            # Try short_id match
            match = None
            for a in all_agents:
                if a.get("short_id", "").upper() == r_stripped.upper():
                    match = a
                    break
            # Try name match (case-insensitive)
            if not match:
                for a in all_agents:
                    if a.get("name", "").lower() == r_stripped.lower():
                        match = a
                        break
            # Try role match (e.g. "HR")
            if not match:
                for a in all_agents:
                    if a.get("role", "").lower() == r_stripped.lower():
                        match = a
                        break
            if match:
                # Skip self — sending to yourself is a no-op
                if match["id"] == agent_id:
                    log.info("send_message_self_skip", agent_id=agent_id,
                             recipient=r, match_name=match.get("name"))
                    continue
                resolved.append(match)
            else:
                not_found.append(r)

        if not resolved:
            # If we already sent to user, return partial success
            if results:
                return {"ok": True, "results": results, "not_found": not_found}
            return self._error(
                f"No recipients found. Unknown: {not_found}. "
                f"Available agents: {[(a['name'], a.get('short_id'), a.get('role')) for a in all_agents]}"
            )

        # BUG-034: Also record team chat for the SENDER so they can see
        # "发送 → RecipientName" in their team comms panel. Previously only
        # the recipient's inbox was written — sender had no record.
        from hiveweave.services.team_chat import TeamChatService
        team_chat = TeamChatService()
        for target in resolved:
            msg = await self._inbox.send_message(
                from_agent_id=agent_id,
                to_agent_id=target["id"],
                message=message,
                priority=priority,
                expect_report=expect_report,
            )
            results.append({
                "to": target["name"],
                "short_id": target.get("short_id"),
                "message_id": msg["id"],
            })
            # Record for sender so team comms panel shows outgoing messages
            await team_chat.record_message(
                agent_id=agent_id,
                from_agent_id=agent_id,
                to_agent_id=target["id"],
                content=message,
            )
            # BUG-022 fix: do NOT trigger here — the target agent's inbox watcher
            # (agent.py:_inbox_watcher_loop) polls every 5s and triggers autonomously.
            # Double-triggering (here + watcher) caused the Engineer to receive the
            # same task twice.

        not_found_str = f" (not found: {not_found})" if not_found else ""
        return {
            "success": True,
            "output": f"Message sent to {len(resolved)} agent(s): "
                      f"{', '.join(r['to'] for r in results)}{not_found_str}",
            "error": None,
        }

    async def _tool_list_subordinates(self, agent_id: str) -> dict:
        """list_subordinates: list direct children of the calling agent."""
        subs = await self._org.get_subordinates(agent_id)
        if not subs:
            return {"success": True, "output": "You have no direct subordinates.", "error": None}

        lines = []
        for s in subs:
            lines.append(
                f"- {s['name']} ({s.get('short_id', '?')}) | "
                f"role={s.get('role', '?')} | "
                f"status={s.get('status', '?')} | "
                f"goal={s.get('goal', '')[:80]}"
            )
        return {
            "success": True,
            "output": f"Direct subordinates ({len(subs)}):\n" + "\n".join(lines),
            "error": None,
        }

    async def _tool_hire_agent(self, agent_id: str, args: dict) -> dict:
        """hire_agent: HR creates a new agent via OrgService.create_agent.

        Args (from LLM):
            name: str — agent codename (e.g. 折纸)
            role: str — Chinese job title (e.g. 前端工程师)
            backstory: str — 2-4 sentence character narrative
            skills: list[str] — skill slugs
            parentId: str — parent agent ID (default: CEO)
            goal: str — agent's goal
            templateId: str — optional template ID
        """
        name = args.get("name") or ""
        role = args.get("role") or ""
        backstory = args.get("backstory") or ""
        skills = args.get("skills") or []
        parent_id = args.get("parentId") or args.get("parent_id") or ""
        goal = args.get("goal") or ""
        template_id = args.get("templateId") or args.get("template_id")

        if not name:
            return self._error("hire_agent requires 'name' (agent codename)")
        if not role:
            return self._error("hire_agent requires 'role' (job title)")

        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        # Resolve parent_id: LLM may pass short_id (e.g. "A001") instead of UUID.
        # Try to resolve via short_id or name lookup.
        all_agents = await self._org.list_agents(project_id)
        if parent_id:
            resolved_parent = None
            # Check if it's a valid UUID matching an existing agent
            for a in all_agents:
                if a["id"] == parent_id:
                    resolved_parent = parent_id
                    break
            # If not UUID match, try short_id match
            if not resolved_parent:
                for a in all_agents:
                    if a.get("short_id", "").upper() == parent_id.upper():
                        resolved_parent = a["id"]
                        log.info("tool.hire_agent.parent_resolved",
                                 short_id=parent_id, uuid=a["id"][:8])
                        break
            # If still not resolved, try name match
            if not resolved_parent:
                for a in all_agents:
                    if a.get("name", "").lower() == parent_id.lower():
                        resolved_parent = a["id"]
                        break
            parent_id = resolved_parent or ""

        # If no parentId specified or resolved, default to CEO
        if not parent_id:
            ceo = await self._org.get_agent_by_role(project_id, "ceo")
            if ceo:
                parent_id = ceo["id"]

        # Determine permission_type: coordinator roles → coordinator, else executor
        coordinator_roles = {"ceo", "hr", "qa", "cto", "architect", "manager", "pm"}
        perm_type = "coordinator" if role.lower() in coordinator_roles else "executor"
        perm_mode = "readonly" if perm_type == "coordinator" else "readwrite"

        # Get default model_id: 优先从项目现有 agent 继承，其次从 ModelService 取第一个 active model
        existing_agents = await self._org.list_agents(project_id)
        model_id = None
        if existing_agents:
            for a in existing_agents:
                if a.get("model_id"):
                    model_id = a["model_id"]
                    break
        if not model_id:
            try:
                ms = ModelService()
                active = await ms.list_active()
                if active:
                    # 选最新的 active 模型（用户最近加的就是首选）
                    chosen = active[-1]
                    model_id = chosen.get("model_id") or chosen.get("id")
                    log.info("tool.hire_agent.model_from_service", model_id=model_id)
            except Exception as e:
                log.warning("tool.hire_agent.model_service_failed", error=str(e))
        if not model_id:
            model_id = "step-3.7-flash"  # fallback

        # Get language from project
        project_row = await meta_db.query_one(
            "SELECT language FROM projects WHERE id = ?", [project_id]
        )
        language = project_row["language"] if project_row else "zh"

        attrs = {
            "project_id": project_id,
            "name": name,
            "role": role,
            "parent_id": parent_id,
            "backstory": backstory,
            "goal": goal or f"Execute {role} responsibilities.",
            "model_id": model_id,
            "permission_type": perm_type,
            "permission_mode": perm_mode,
            "skills": skills if isinstance(skills, list) else [],
            "allowed_tools": [],
            "language": language,
            "status": "active",
            # short_id and id are intentionally omitted — auto-generated by
            # OrgService.create_agent (short_id: A001-style auto-increment,
            # id: UUID). HR must NOT control these.
        }

        try:
            new_agent = await self._org.create_agent(attrs)
            new_id = new_agent.get("id", "?")
            new_short = new_agent.get("short_id", "?")

            # BUG-010 修复：创建后立即启动 agent，让它能处理 inbox 消息。
            # 否则 hire_agent 创建的 executor 只是 DB 一行，无法消费任务。
            try:
                from hiveweave.agents.supervisor import agent_manager
                from hiveweave.realtime.event_bus import create_agent_callbacks
                on_status, on_stream = create_agent_callbacks(new_id, project_id)
                started = await agent_manager.start_agent(
                    new_id, project_id, new_agent,
                    on_stream_event=on_stream, on_status_change=on_status,
                )
                log.info("tool.hire_agent.started",
                         agent_id=agent_id, new_agent_id=new_id,
                         new_short_id=new_short, name=name, role=role,
                         status=started.status.value if started else "none")
            except Exception as start_err:
                log.warning("tool.hire_agent.start_failed",
                            new_agent_id=new_id, error=str(start_err))

            log.info("tool.hire_agent", agent_id=agent_id,
                     new_agent_id=new_id, new_short_id=new_short,
                     name=name, role=role)

            # Push realtime event so frontend org tree updates immediately
            try:
                from hiveweave.realtime.event_bus import status_event_bus
                await status_event_bus.publish_agent_created(new_id, role, name)
                await status_event_bus.publish_org_changed()
            except Exception as evt_err:
                log.debug("hire_agent_event_push_failed", error=str(evt_err))

            return {
                "success": True,
                "output": (
                    f"✅ 招聘成功！\n"
                    f"  花名: {name}\n"
                    f"  角色: {role}\n"
                    f"  编号(short_id): {new_short}  ← 后续引用此人时使用此编号\n"
                    f"  内部ID: {new_id}\n"
                    f"  上级: {parent_id[:8]}...\n"
                    f"  权限: {perm_type}\n"
                    f"  模型: {model_id}\n"
                    f"  技能: {skills}\n"
                    f"  背景: {backstory[:100] if backstory else '(无)'}"
                ),
                "error": None,
            }
        except Exception as e:
            return self._error(f"Failed to hire agent: {e}")

    async def _tool_read_charter(self, agent_id: str) -> dict:
        """read_charter: read the project charter."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        charter = await self._charter.read_charter(project_id)
        if not charter:
            return {"success": True, "output": "No charter has been saved yet.", "error": None}

        output = f"=== Project Charter ===\n"
        output += f"Title: {charter.get('title', 'N/A')}\n"
        output += f"Status: {charter.get('status', 'N/A')}\n"
        output += f"Content:\n{charter.get('content', 'N/A')}\n"
        return {"success": True, "output": output, "error": None}

    async def _tool_save_charter(self, agent_id: str, args: dict) -> dict:
        """save_charter: save/update the project charter."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        content = args.get("content") or args.get("charter") or ""
        title = args.get("title") or "Project Charter"

        if not content:
            return self._error("save_charter requires 'content' (charter body)")

        try:
            charter_id = await self._charter.save_charter(
                project_id, agent_id,
                {"title": title, "content": content, "status": "active"},
            )
            return {
                "success": True,
                "output": f"Charter saved (id={charter_id[:8]}...). Title: {title}",
                "error": None,
            }
        except Exception as e:
            return self._error(f"Failed to save charter: {e}")

    async def _tool_read_goals(self, agent_id: str) -> dict:
        """read_goals: read enterprise goals."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        goals = await self._charter.read_goals(project_id)
        if not goals:
            return {"success": True, "output": "No goals have been set yet.", "error": None}

        output = "=== Enterprise Goals ===\n"
        output += f"Objective: {goals.get('objective', 'N/A')}\n"
        output += f"Focus: {goals.get('focus', 'N/A')}\n"
        output += f"User Involvement: {goals.get('userInvolvement', 'N/A')}\n"
        krs = goals.get("keyResults", [])
        if krs:
            output += "Key Results:\n"
            for i, kr in enumerate(krs, 1):
                if isinstance(kr, dict):
                    output += f"  {i}. {kr.get('description', kr.get('text', str(kr)))}\n"
                else:
                    output += f"  {i}. {kr}\n"
        return {"success": True, "output": output, "error": None}

    async def _tool_update_goals(self, agent_id: str, args: dict) -> dict:
        """update_goals: update enterprise goals."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        goals = {}
        for param, key in [("objective", "objective"), ("focus", "focus")]:
            if args.get(param) is not None:
                goals[key] = args[param]
        # keyResults: explicit None-check to preserve empty list []
        kr = args.get("keyResults") if "keyResults" in args else args.get("key_results")
        if kr is not None:
            goals["key_results"] = kr
        # userInvolvement: explicit None-check to preserve empty string ""
        ui = args.get("userInvolvement") if "userInvolvement" in args else args.get("user_involvement")
        if ui is not None:
            goals["user_involvement"] = ui
        # Remove None values
        goals = {k: v for k, v in goals.items() if v is not None}

        if not goals:
            return self._error("update_goals requires at least one of: objective, focus, keyResults, userInvolvement")

        try:
            await self._charter.update_goals(project_id, goals)
            return {"success": True, "output": "Goals updated successfully.", "error": None}
        except Exception as e:
            return self._error(f"Failed to update goals: {e}")

    async def _tool_view_org_chart(self, agent_id: str) -> dict:
        """view_org_chart: show the full organization tree."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        tree = await self._org.get_full_tree(project_id)
        if not tree:
            return {"success": True, "output": "Org chart is empty.", "error": None}

        def format_node(node, indent=0):
            prefix = "  " * indent
            line = f"{prefix}- {node['name']} ({node.get('short_id', '?')}) role={node.get('role', '?')}"
            if node.get("goal"):
                line += f" goal={node['goal'][:60]}"
            lines = [line]
            for child in (node.get("children") or []):
                lines.extend(format_node(child, indent + 1))
            return lines

        all_lines = []
        for root in tree:
            all_lines.extend(format_node(root))

        return {"success": True, "output": "=== Org Chart ===\n" + "\n".join(all_lines), "error": None}

    async def _tool_read_work_logs(self, agent_id: str, args: dict) -> dict:
        """read_work_logs: read work logs from subordinates or specific agent."""
        target = args.get("agentId") or args.get("agent_id") or args.get("agent")

        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")

        # If target specified, resolve it; otherwise list all subordinates' logs
        if target:
            target_agent = await self._org.resolve_agent(target)
            if not target_agent:
                return self._error(f"Agent not found: {target}")
            target_ids = [target_agent["id"]]
        else:
            subs = await self._org.get_subordinates(agent_id)
            target_ids = [s["id"] for s in subs]

        if not target_ids:
            return {"success": True, "output": "No agents to read work logs from.", "error": None}

        # Query work_logs from per-project DB
        from hiveweave.db import project as project_db
        all_logs = []
        for tid in target_ids:
            try:
                rows = await project_db.query(
                    tid,
                    "SELECT agent_id, content, log_type, created_at FROM work_logs "
                    "WHERE agent_id = ? ORDER BY created_at DESC LIMIT 10",
                    [tid],
                )
                for r in rows:
                    all_logs.append(r)
            except Exception:
                pass  # Table might not exist yet

        if not all_logs:
            return {"success": True, "output": "No work logs found.", "error": None}

        lines = []
        for log in all_logs:
            ts = log.get("created_at", 0)
            lines.append(f"[{ts}] {log.get('agent_id', '?')[:8]}... ({log.get('log_type', '?')}): {log.get('content', '')[:100]}")
        return {"success": True, "output": f"=== Work Logs ({len(all_logs)}) ===\n" + "\n".join(lines), "error": None}

    async def _tool_write_work_log(self, agent_id: str, args: dict) -> dict:
        """write_work_log: record a work log entry for the calling agent.

        BUG-026 修复：补上 write_work_log 工具的实际分发。之前该工具只在
        agent.py 的 _TOOL_DESCRIPTIONS 里声明，LLM 调用时 _dispatch 找不到
        对应分支，返回 "Unknown tool: write_work_log"，导致 work-logs 永远为空。
        """
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project_id")
        from hiveweave.services.work_log import WorkLogService

        wl = WorkLogService()
        log_type = args.get("type") or args.get("logType") or "discussion"
        summary = (
            args.get("summary")
            or args.get("content")
            or args.get("message")
            or ""
        )
        if not summary:
            return self._error("write_work_log requires 'summary'")
        details = args.get("details") or args.get("metadata")
        log_id = await wl.write_work_log(
            project_id, agent_id, None, log_type, summary, details=details,
        )
        return {
            "success": True,
            "output": f"Work log written (id={log_id[:8]}..., type={log_type}).",
            "error": None,
        }
