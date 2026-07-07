"""Permission service — tool permission evaluation with 4-mode presets.

契约 08: 权限与审批
- 4 模式: readonly (24 工具), readwrite, full, custom
- 评估顺序: deny → ask → allow → fallback
- glob 匹配: 支持 * 通配符
- 参数级匹配: ToolName(arg_pattern) — 工具名大小写不敏感（仅参数级），无参数模式大小写敏感
- RECONCILE A2: 未知/默认 mode 返回 'ask'（非 'allow'，修正源码安全漏洞）
- saved_rules: agent 的 allowed_tools/denied_tools/ask_tools JSON 字段
"""

import json
import re
from typing import Any

import structlog

from hiveweave.db import meta as meta_db

logger = structlog.get_logger()

# readonly preset (契约 08)
READONLY_TOOLS = frozenset({
    "bash", "grep", "apply_patch", "question", "todowrite", "websearch", "webfetch",
    "schedule_alarm", "list_alarms", "cancel_alarm",
    "review", "read_file", "list_files", "read_skill", "list_available_skills", "bind_skill",
    "unbind_skill", "read_memory", "write_memory",
    "send_message", "message_superior", "message_subordinate",
    "message_peer", "message_team",
    "read_roster", "update_roster", "write_work_log",
    "report_completion", "request_review", "list_subordinates",
    "list_agent_templates", "hire_agent",
    "read_charter", "read_goals", "view_org_chart", "read_work_logs",
    "git_worktree_list", "git_worktree_status",
})

# readwrite = readonly + write/edit/dispatch tools
READWRITE_TOOLS = READONLY_TOOLS | frozenset({
    "write_file", "edit_file", "delete_file", "move_file",
    "create_directory", "delete_directory", "search_files",
    "dispatch_task", "report_completion", "request_review",
    "hire_agent", "approve_work", "reject_work",
    "run_code_review", "run_security_audit", "run_tests",
    "run_perf_audit", "run_full_review",
    "git_worktree_create", "git_worktree_merge", "git_worktree_remove",
    "git_worktree_checkpoint",
    "save_charter", "update_goals", "dismiss_agent", "transfer_agent",
})

# All known tools (full mode)
ALL_TOOLS = READWRITE_TOOLS | frozenset({"run_command"})


class PermissionService:
    """Evaluates tool permissions for agents with 4-mode presets."""

    async def get_permission_mode(self, agent_id: str) -> str:
        """Get permission mode from Meta DB agents table (permission_mode column)."""
        agent = await meta_db.get_agent_by_id(agent_id)
        if agent is None:
            return "custom"
        return agent.get("permission_mode") or "readonly"

    async def evaluate(
        self, agent_id: str, tool_name: str, tool_args: dict | None = None
    ) -> str:
        """Evaluate whether a tool is allowed. Returns 'allow'/'deny'/'ask'.

        Evaluation order: deny → ask → allow → fallback (by mode).
        """
        agent = await meta_db.get_agent_by_id(agent_id)
        if agent is None:
            return "ask"

        mode = agent.get("permission_mode") or "readonly"
        denied = self._parse_list(agent.get("denied_tools"))
        ask = self._parse_list(agent.get("ask_tools"))
        allowed = self._parse_list(agent.get("allowed_tools"))

        # Evaluation order: deny → ask → allow → fallback
        if self._matches_pattern(tool_name, denied, tool_args):
            return "deny"
        if self._matches_pattern(tool_name, ask, tool_args):
            return "ask"
        if self._matches_pattern(tool_name, allowed, tool_args):
            return "allow"

        # Fallback based on mode
        if mode == "full":
            return "allow"
        if mode == "readwrite":
            return "allow" if tool_name in READWRITE_TOOLS else "ask"
        if mode == "readonly":
            return "allow" if tool_name in READONLY_TOOLS else "ask"
        if mode == "custom":
            return "ask"
        return "ask"  # RECONCILE A2: unknown/default → ask (not allow)

    def get_tools_for_mode(self, mode: str) -> list[str]:
        """Return the list of tools allowed for a permission mode."""
        if mode == "readonly":
            return sorted(READONLY_TOOLS)
        if mode == "readwrite":
            return sorted(READWRITE_TOOLS)
        if mode == "full":
            return sorted(ALL_TOOLS)
        return []  # custom or unknown — depends on saved rules

    # ── Pattern matching ──────────────────────────────────────

    def _parse_list(self, raw: Any) -> list[str]:
        """Parse a JSON tool list from agent's saved rules field."""
        if not raw:
            return []
        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _matches_pattern(
        self, tool_name: str, patterns: list[str], tool_args: dict | None
    ) -> bool:
        """Check if tool_name matches any pattern in the list."""
        if not patterns:
            return False
        args = self._extract_args_string(tool_name, tool_args)
        return any(self._single_match(tool_name, p, args) for p in patterns)

    def _single_match(self, tool_name: str, pattern: str, args: str) -> bool:
        """Match a single pattern against tool_name (and args if param pattern)."""
        if pattern == "*":
            return True
        # Parameter-level pattern: "ToolName(arg_pattern)"
        if self._has_param_pattern(pattern):
            return self._match_param_pattern(tool_name, pattern, args)
        # Plain pattern: case-sensitive exact or glob match
        if pattern == tool_name:
            return True
        if "*" in pattern:
            regex = self._glob_to_regex(pattern)
            return re.match(f"^{regex}$", tool_name) is not None
        return False

    def _has_param_pattern(self, pattern: str) -> bool:
        """A param pattern contains '(' and ends with ')'."""
        return "(" in pattern and pattern.endswith(")")

    def _match_param_pattern(
        self, tool_name: str, pattern: str, args: str
    ) -> bool:
        """Match ToolName(arg_pattern) — tool name case-insensitive, args glob."""
        m = re.match(r"^(\w+)\((.+)\)$", pattern)
        if not m:
            return False
        rule_tool, arg_pattern = m.group(1), m.group(2)
        # Case-insensitive tool name comparison (param-level only)
        if rule_tool.lower() != tool_name.lower():
            return False
        regex = self._glob_to_regex(arg_pattern)
        return re.match(f"^{regex}$", args) is not None

    def _glob_to_regex(self, pattern: str) -> str:
        """Convert a glob pattern (* wildcard) to a regex source string."""
        return re.escape(pattern).replace(r"\*", ".*")

    def _extract_args_string(self, tool_name: str, tool_args: dict | None) -> str:
        """Build the argument string for parameter-level pattern matching."""
        if tool_args is None:
            return ""
        if tool_name == "bash":
            return str(tool_args.get("command", tool_args.get("cmd", "")))
        parts = []
        for v in tool_args.values():
            parts.append(v if isinstance(v, str) else str(v))
        return " ".join(parts)


permission_service = PermissionService()
