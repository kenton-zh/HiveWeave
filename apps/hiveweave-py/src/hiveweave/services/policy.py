"""Capability-based PolicyService — hard gates that allowed_tools cannot elevate.

Evaluation order (P0 Hard Gates):
1. Hard capability deny (role family matrix)
2. Parameter scope (path prefixes for write_file, etc.)
3. User rules: deny → ask → allow
4. Mode fallback

Role families: hr | coordinator | executor | qa
"""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class Capability(str, Enum):
    STAFFING = "staffing"
    MANAGE_ORG = "manage_org"
    BIND_SKILL = "bind_skill"
    DISPATCH = "dispatch"
    REVIEW = "review"
    MERGE = "merge"
    SOURCE_READ = "source_read"
    SOURCE_WRITE = "source_write"
    TEST_RUN = "test_run"
    BROWSER_ACCEPTANCE = "browser_acceptance"
    BASH_SHELL = "bash_shell"
    BROWSE = "browse"


RoleFamily = str  # "hr" | "coordinator" | "executor" | "qa"

# Default capability matrix — hard coded.
FAMILY_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "hr": frozenset({
        Capability.STAFFING,
        Capability.MANAGE_ORG,
        Capability.BIND_SKILL,
        Capability.SOURCE_READ,
    }),
    "coordinator": frozenset({
        Capability.DISPATCH,
        Capability.REVIEW,
        Capability.MERGE,
        Capability.SOURCE_READ,
        Capability.BIND_SKILL,  # bind skills on subordinates via tools
        Capability.MANAGE_ORG,  # dismiss/transfer within span
    }),
    "executor": frozenset({
        Capability.SOURCE_WRITE,
        Capability.TEST_RUN,
        Capability.SOURCE_READ,
        Capability.BASH_SHELL,
        Capability.BROWSE,  # self-check OK; attestation gate is Phase 3
    }),
    "qa": frozenset({
        Capability.BROWSER_ACCEPTANCE,
        Capability.TEST_RUN,
        Capability.SOURCE_READ,
        Capability.BASH_SHELL,
        Capability.BROWSE,
    }),
}

# Tool → required capability (any one of the set; empty = no hard cap beyond family)
TOOL_CAPABILITY: dict[str, frozenset[Capability]] = {
    "hire_agent": frozenset({Capability.STAFFING}),
    "dismiss_agent": frozenset({Capability.MANAGE_ORG, Capability.STAFFING}),
    "transfer_agent": frozenset({Capability.MANAGE_ORG, Capability.STAFFING}),
    "list_agent_templates": frozenset({Capability.STAFFING}),
    "bind_skill": frozenset({Capability.BIND_SKILL}),
    "unbind_skill": frozenset({Capability.BIND_SKILL}),
    "create_task": frozenset({Capability.DISPATCH}),
    "dispatch_task": frozenset({Capability.DISPATCH}),
    "cancel_task": frozenset({Capability.DISPATCH}),
    "unclaim_task": frozenset({Capability.DISPATCH}),
    "review_task": frozenset({Capability.REVIEW}),
    "waive_attestation": frozenset({Capability.REVIEW}),
    "git_worktree_create": frozenset({Capability.MERGE}),
    "git_worktree_merge": frozenset({Capability.MERGE}),
    "git_worktree_remove": frozenset({Capability.MERGE}),
    "bash": frozenset({Capability.BASH_SHELL}),
    "run_command": frozenset({Capability.BASH_SHELL}),
    "browse": frozenset({Capability.BROWSE, Capability.BROWSER_ACCEPTANCE}),
    "edit_file": frozenset({Capability.SOURCE_WRITE}),
    "apply_patch": frozenset({Capability.SOURCE_WRITE}),
    "delete_file": frozenset({Capability.SOURCE_WRITE}),
    "move_file": frozenset({Capability.SOURCE_WRITE}),
    "create_directory": frozenset({Capability.SOURCE_WRITE}),
    "delete_directory": frozenset({Capability.SOURCE_WRITE}),
    "run_tests": frozenset({Capability.TEST_RUN}),
    "run_code_review": frozenset({Capability.SOURCE_READ}),
    "run_security_audit": frozenset({Capability.SOURCE_READ}),
    "run_perf_audit": frozenset({Capability.SOURCE_READ}),
    "run_full_review": frozenset({Capability.SOURCE_READ}),
    # write_file: capability depends on path scope (checked separately)
}

# Paths coordinators/HR may write without SOURCE_WRITE
COORDINATOR_WRITE_PREFIXES = (
    "docs/",
    "doc/",
    ".hiveweave/shared/",
    "README.md",
    "README",
    "CHANGELOG",
    "AGENTS.md",
    "CLAUDE.md",
)


def is_test_engineer_role(role: str) -> bool:
    """Match QA / 测试工程师 roles (shared with prompts.executor)."""
    original = role or ""
    r = original.strip().lower()
    if r in {"test_engineer", "qa_engineer", "qa engineer", "qa"}:
        return True
    if "test engineer" in r or "qa engineer" in r:
        return True
    if "测试工程师" in original or "测试专员" in original:
        return True
    if "浏览器测试" in original or "e2e" in r:
        return True
    if "evidence collector" in r:
        return True
    if r.endswith(" qa"):
        return True
    return False


def infer_role_family(agent: dict[str, Any]) -> RoleFamily:
    """Derive role family from agent row (role / permission_type / explicit)."""
    explicit = (agent.get("role_family") or "").strip().lower()
    if explicit in FAMILY_CAPABILITIES:
        return explicit

    role = (agent.get("role") or "").strip()
    role_l = role.lower()
    perm = (agent.get("permission_type") or "").strip().lower()

    if role_l == "hr" or role == "人力资源" or "人力资源" in role:
        return "hr"
    if is_test_engineer_role(role):
        return "qa"
    if perm == "coordinator" or role_l in ("ceo", "coordinator"):
        return "coordinator"
    return "executor"


def capabilities_for(agent: dict[str, Any]) -> frozenset[Capability]:
    family = infer_role_family(agent)
    return FAMILY_CAPABILITIES.get(family, FAMILY_CAPABILITIES["executor"])


def has_capability(agent: dict[str, Any], cap: Capability) -> bool:
    return cap in capabilities_for(agent)


def tool_hard_deny(agent: dict[str, Any], tool_name: str) -> str | None:
    """Return deny reason if tool is blocked by hard capability, else None."""
    caps = capabilities_for(agent)
    required = TOOL_CAPABILITY.get(tool_name)
    if required is None:
        # write_file handled via scope; unknown tools fall through
        if tool_name == "write_file":
            return None
        return None
    if caps.isdisjoint(required):
        family = infer_role_family(agent)
        return (
            f"Hard capability deny: '{tool_name}' requires "
            f"{sorted(c.value for c in required)}; "
            f"role_family={family} has {[c.value for c in sorted(caps, key=lambda x: x.value)]}"
        )
    # Extra: hire_agent is HR-only even though STAFFING is HR-only already
    if tool_name == "hire_agent" and infer_role_family(agent) != "hr":
        return "Hard capability deny: only HR may hire_agent"
    return None


def write_path_allowed(agent: dict[str, Any], file_path: str) -> str | None:
    """Return deny reason if write_file path is out of scope for this agent."""
    caps = capabilities_for(agent)
    if Capability.SOURCE_WRITE in caps:
        return None  # executors may write anywhere in sandbox

    # Coordinators / HR / QA without source_write: docs & shared only
    norm = (file_path or "").replace("\\", "/").lstrip("./")
    for prefix in COORDINATOR_WRITE_PREFIXES:
        if prefix.endswith("/"):
            if norm.startswith(prefix) or norm == prefix.rstrip("/"):
                return None
        else:
            if norm == prefix or norm.startswith(prefix + "."):
                return None
    # charter-like project meta files at root
    base = PurePosixPath(norm).name.lower()
    if base in {"charter.md", "goals.md", "spec.md"}:
        return None
    return (
        f"Hard scope deny: write_file path '{file_path}' requires source_write "
        f"or must be under docs/ / .hiveweave/shared/ (role_family="
        f"{infer_role_family(agent)})"
    )


class PolicyService:
    """Unified policy evaluation for tools and REST."""

    def hard_check(
        self,
        agent: dict[str, Any],
        tool_name: str,
        tool_args: dict | None = None,
    ) -> str | None:
        """Return deny reason string, or None if hard gates pass."""
        reason = tool_hard_deny(agent, tool_name)
        if reason:
            return reason
        if tool_name == "write_file":
            path = ""
            if tool_args:
                path = str(
                    tool_args.get("filePath")
                    or tool_args.get("file_path")
                    or tool_args.get("path")
                    or ""
                )
            return write_path_allowed(agent, path)
        return None


policy_service = PolicyService()
