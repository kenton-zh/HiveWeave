"""Permission service — tool permission evaluation with capability hard gates.

契约 08 + P0 Hard Gates:
- Evaluation order: hard capability deny → user deny → ask → allow → role/mode fallback
- allowed_tools MUST NOT elevate past hard capability denies
- 4 模式: readonly, readwrite, full, custom (executor fallback)
"""

import json
import re
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.policy import (
    infer_role_family,
    policy_service,
)

logger = structlog.get_logger()

# ──────────────────────────────────────────────────────────────────────────
# Tool presets — visibility for LLM tool lists (not sole authorization)
# Hard capability in PolicyService is the real gate.
# ──────────────────────────────────────────────────────────────────────────

_BASE_TOOLS = frozenset({
    "grep", "question", "todowrite", "websearch", "webfetch",
    "schedule_alarm", "list_alarms", "cancel_alarm",
    "review", "read_file", "list_files", "read_skill", "list_available_skills",
    "read_memory", "write_memory",
    "send_message", "message_superior", "message_subordinate",
    "message_peer", "message_team",
    "ask_agent", "notify_agent", "commit_turn", "defer_task_advance",
    "lookup_dev_server",
    "read_roster", "update_roster", "write_work_log",
    "request_review", "list_subordinates", "check_agent_status",
    "get_platform_state",
    "read_charter", "read_goals", "view_org_chart", "read_work_logs",
    "git_worktree_list", "git_worktree_status",
    "get_tasks",
    "claim_task", "update_task_status", "submit_task", "update_progress",
    "attest_doc_review",
})

CEO_TOOLS = _BASE_TOOLS | frozenset({
    # CEO: 行政 + 里程碑验收。无写码/bash/test 工具。
    "write_file",  # docs/shared/charter scope only via policy hard gate
    "create_task", "dispatch_task", "review_task",
    "cancel_task", "unclaim_task", "waive_attestation",
    "save_charter", "update_goals", "dismiss_agent", "transfer_agent",
    # merge/remove 仅作升级兜底 —— create 仍由系统侧在 dispatch/hire 时建
    "git_worktree_merge", "git_worktree_remove",
    # 终验对用户说
    "message_user",
    "start_dev_server",
})

COORDINATOR_BUILDER_TOOLS = _BASE_TOOLS | frozenset({
    # 中层 builder（player-coach）：协调权 + 写码权（与 executor 同契约
    # 拥有独立 worktree，自己搭骨架/写关键路径）。
    "write_file",
    "bind_skill", "unbind_skill",
    "create_task", "dispatch_task", "review_task",
    # 台账出口：废弃/释放误绑任务 + 豁免 attestation（policy 已映射 DISPATCH/REVIEW）
    "cancel_task", "unclaim_task", "waive_attestation",
    "save_charter", "update_goals", "dismiss_agent", "transfer_agent",
    # merge/remove only — create is system-side on dispatch/hire
    "git_worktree_merge", "git_worktree_remove",
    "start_dev_server",
    # 写码/验证工具
    "edit_file", "apply_patch", "delete_file", "move_file",
    "create_directory", "delete_directory", "search_files",
    "bash", "run_command", "run_tests", "browse",
    "git_worktree_checkpoint",
    "run_code_review", "run_security_audit", "run_perf_audit",
    "run_full_review",
})

# Legacy alias — builder coordinator 即原 COORDINATOR_TOOLS 语义的超集。
COORDINATOR_TOOLS = COORDINATOR_BUILDER_TOOLS

HR_TOOLS = _BASE_TOOLS | frozenset({
    "hire_agent", "dismiss_agent", "transfer_agent",
    "list_agent_templates",
    "bind_skill", "unbind_skill",
    "write_file",
})

# Legacy name kept for imports/tests — executor listing after hard gates.
# Do NOT include hire/dispatch/bash elevation here for "readonly" meaning;
# PolicyService still hard-denies based on role family.
READONLY_TOOLS = _BASE_TOOLS | frozenset({
    "bash", "write_file", "browse", "edit_file",
    "bind_skill", "unbind_skill",
    "start_dev_server",
    "run_tests", "apply_patch",
    "create_directory", "delete_file", "move_file", "search_files",
    "delete_directory",
    "run_code_review", "run_security_audit", "run_perf_audit", "run_full_review",
    "git_worktree_checkpoint",
})

READWRITE_TOOLS = READONLY_TOOLS | frozenset({
    "run_command",
})

ALL_TOOLS = READWRITE_TOOLS | COORDINATOR_BUILDER_TOOLS | HR_TOOLS | frozenset({
    "run_command",
    "git_worktree_create", "git_worktree_merge", "git_worktree_remove",
    "create_task", "dispatch_task", "review_task",
    "hire_agent", "dismiss_agent", "transfer_agent",
    "save_charter", "update_goals",
    "message_user",
})

# 工具本体（task_tools）无角色守卫；executor 滥用由两层拦截：
# policy 硬能力门（DISPATCH/REVIEW，executor 族不具备）+ 此集合 deny。
COORDINATOR_ONLY_TOOLS = frozenset({
    "create_task", "dispatch_task", "review_task",
    "cancel_task", "unclaim_task", "waive_attestation",
    "git_worktree_merge", "git_worktree_remove",
})

EXECUTOR_ONLY_TOOLS: frozenset[str] = frozenset()


class PermissionService:
    """Evaluates tool permissions with hard capability gates first."""

    async def get_permission_mode(self, agent_id: str) -> str:
        agent = await meta_db.get_agent_by_id(agent_id)
        if agent is None:
            return "custom"
        return agent.get("permission_mode") or "readonly"

    async def evaluate(
        self, agent_id: str, tool_name: str, tool_args: dict | None = None
    ) -> str:
        """Evaluate whether a tool is allowed. Returns 'allow'/'deny'/'ask'.

        Order: hard capability → user deny → ask → allow → family/mode fallback.
        ``allowed_tools`` cannot override hard capability deny.
        """
        agent = await meta_db.get_agent_by_id(agent_id)
        if agent is None:
            return "ask"

        hard = policy_service.hard_check(agent, tool_name, tool_args)
        if hard:
            logger.info(
                "policy.hard_deny",
                agent_id=agent_id,
                tool=tool_name,
                reason=hard[:200],
            )
            return "deny"

        mode = agent.get("permission_mode") or "readonly"
        denied = self._parse_list(agent.get("denied_tools"))
        ask = self._parse_list(agent.get("ask_tools"))
        allowed = self._parse_list(agent.get("allowed_tools"))

        if self._matches_pattern(tool_name, denied, tool_args):
            return "deny"
        if self._matches_pattern(tool_name, ask, tool_args):
            return "ask"
        if self._matches_pattern(tool_name, allowed, tool_args):
            return "allow"

        family = infer_role_family(agent)
        permission_type = (agent.get("permission_type") or "").lower()

        if family == "hr":
            return "allow" if tool_name in HR_TOOLS else "deny"
        if family == "ceo":
            return "allow" if tool_name in CEO_TOOLS else "deny"
        if family == "coordinator" or permission_type == "coordinator":
            if (
                tool_name in COORDINATOR_ONLY_TOOLS
                or tool_name in COORDINATOR_BUILDER_TOOLS
            ):
                return "allow"
            return "deny"

        if tool_name in COORDINATOR_ONLY_TOOLS:
            return "deny"

        if mode == "full":
            return "allow"
        if mode == "readwrite":
            return "allow" if tool_name in READWRITE_TOOLS else "ask"
        if mode == "readonly":
            return "allow" if tool_name in READONLY_TOOLS else "ask"
        if mode == "custom":
            return "ask"
        return "ask"

    def get_tools_for_mode(self, mode: str) -> list[str]:
        if mode == "readonly":
            return sorted(READONLY_TOOLS)
        if mode == "readwrite":
            return sorted(READWRITE_TOOLS)
        if mode == "full":
            return sorted(ALL_TOOLS)
        return []

    def get_tools_for_agent(self, agent: dict[str, Any]) -> list[str]:
        family = infer_role_family(agent)
        if family == "hr":
            return sorted(HR_TOOLS)
        if family == "ceo":
            # 显式 ceo 分支 —— 禁止 unknown family 落 READWRITE 兜底泄漏工具。
            return sorted(CEO_TOOLS)
        if family == "coordinator":
            return sorted(COORDINATOR_BUILDER_TOOLS | COORDINATOR_ONLY_TOOLS)
        mode = agent.get("permission_mode") or "readwrite"
        if family == "qa":
            return sorted(READWRITE_TOOLS)
        if family == "executor":
            return self.get_tools_for_mode(
                mode if mode != "readonly" else "readwrite"
            )
        # Unknown family — 最小暴露，不给 READWRITE 兜底。
        return sorted(READONLY_TOOLS)

    def _parse_list(self, raw: Any) -> list[str]:
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
        if not patterns:
            return False
        args = self._extract_args_string(tool_name, tool_args)
        return any(self._single_match(tool_name, p, args) for p in patterns)

    def _single_match(self, tool_name: str, pattern: str, args: str) -> bool:
        if pattern == "*":
            return True
        if self._has_param_pattern(pattern):
            return self._match_param_pattern(tool_name, pattern, args)
        if pattern == tool_name:
            return True
        if "*" in pattern:
            regex = self._glob_to_regex(pattern)
            return re.match(f"^{regex}$", tool_name) is not None
        return False

    def _has_param_pattern(self, pattern: str) -> bool:
        return "(" in pattern and pattern.endswith(")")

    def _match_param_pattern(
        self, tool_name: str, pattern: str, args: str
    ) -> bool:
        m = re.match(r"^(\w+)\((.+)\)$", pattern)
        if not m:
            return False
        rule_tool, arg_pattern = m.group(1), m.group(2)
        if rule_tool.lower() != tool_name.lower():
            return False
        regex = self._glob_to_regex(arg_pattern)
        return re.match(f"^{regex}$", args) is not None

    def _glob_to_regex(self, pattern: str) -> str:
        return re.escape(pattern).replace(r"\*", ".*")

    def _extract_args_string(self, tool_name: str, tool_args: dict | None) -> str:
        if tool_args is None:
            return ""
        if tool_name == "bash":
            return str(tool_args.get("command", tool_args.get("cmd", "")))
        parts = []
        for v in tool_args.values():
            parts.append(v if isinstance(v, str) else str(v))
        return " ".join(parts)


permission_service = PermissionService()
