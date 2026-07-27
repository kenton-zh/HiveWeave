"""Capability-based PolicyService — hard gates that allowed_tools cannot elevate.

Evaluation order (P0 Hard Gates):
1. Hard capability deny (role family matrix)
2. Parameter scope (path kind / prefixes for write_file & edit_file)
3. User rules: deny → ask → allow
4. Mode fallback

Role families: ceo | hr | coordinator | executor | qa

- ceo: 行政 + 里程碑验收 + DOC_WRITE（任意文档，禁源码/配置）。无写码/bash/test。
- coordinator: 中层 builder（player-coach）— 协调权叠加写码权
  （SOURCE_WRITE / BASH_SHELL / TEST_RUN / BROWSE）。
"""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)

WriteKind = Literal["document", "source", "other"]


class Capability(str, Enum):
    STAFFING = "staffing"
    MANAGE_ORG = "manage_org"
    BIND_SKILL = "bind_skill"
    DISPATCH = "dispatch"
    REVIEW = "review"
    MERGE = "merge"
    SOURCE_READ = "source_read"
    SOURCE_WRITE = "source_write"
    # Prose/markup only — orthogonal to SOURCE_WRITE (CEO document authority)
    DOC_WRITE = "doc_write"
    TEST_RUN = "test_run"
    BROWSER_ACCEPTANCE = "browser_acceptance"
    BASH_SHELL = "bash_shell"
    BROWSE = "browse"


RoleFamily = str  # "ceo" | "hr" | "coordinator" | "executor" | "qa"

# Default capability matrix — hard coded.
FAMILY_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "ceo": frozenset({
        # CEO: 行政 + 里程碑验收 + 文档权。无写码/bash/test/staffing。
        Capability.DISPATCH,
        Capability.REVIEW,
        Capability.MERGE,  # 升级兜底（中层缺席时救场合并）
        Capability.SOURCE_READ,
        Capability.MANAGE_ORG,
        Capability.DOC_WRITE,
    }),
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
        # 中层 builder（player-coach）：协调权叠加写码权 —— 自己搭骨架/写
        # 关键路径，与 executor 同契约拥有独立 worktree。
        Capability.SOURCE_WRITE,
        Capability.BASH_SHELL,
        Capability.TEST_RUN,
        Capability.BROWSE,
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
        # 测试工程师的本职是写测试代码；hire 流程对 executor 一律给 readwrite
        # + worktree，缺 SOURCE_WRITE 会把 write_file 硬门死（Echo 事故）。
        Capability.SOURCE_WRITE,
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
    "reassign_task": frozenset({Capability.DISPATCH}),
    "review_task": frozenset({Capability.REVIEW}),
    "waive_attestation": frozenset({Capability.REVIEW}),
    "waive_merge": frozenset({Capability.REVIEW}),
    "git_worktree_create": frozenset({Capability.MERGE}),
    "git_worktree_merge": frozenset({Capability.MERGE}),
    "git_worktree_remove": frozenset({Capability.MERGE}),
    "bash": frozenset({Capability.BASH_SHELL}),
    "run_command": frozenset({Capability.BASH_SHELL}),
    "browse": frozenset({Capability.BROWSE, Capability.BROWSER_ACCEPTANCE}),
    "assert_visual": frozenset({Capability.BROWSE, Capability.BROWSER_ACCEPTANCE}),
    # DOC_WRITE agents (CEO) may edit docs; SOURCE_WRITE covers all paths
    "edit_file": frozenset({Capability.SOURCE_WRITE, Capability.DOC_WRITE}),
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

# Paths HR (no DOC_WRITE / SOURCE_WRITE) may write — legacy prefix scope.
# Keep in sync with file.py allowed_subdirs + bash.py _ALLOWED_HW_SUBDIRS
COORDINATOR_WRITE_PREFIXES = (
    "docs/",
    "doc/",
    ".hiveweave/shared/",
    ".hiveweave/reports/",
    ".hiveweave/drafts/",
    "README.md",
    "README",
    "CHANGELOG",
    "AGENTS.md",
    "CLAUDE.md",
)

# Substrings that mark allowed .hiveweave work dirs even in absolute paths
_HW_WRITE_MARKERS = (
    "/.hiveweave/shared/",
    "/.hiveweave/reports/",
    "/.hiveweave/drafts/",
)

# Human prose / markup — DOC_WRITE may create or edit these anywhere.
_DOCUMENT_EXTENSIONS = frozenset({
    ".md", ".mdx", ".markdown",
    ".txt", ".text",
    ".rst", ".rest",
    ".adoc", ".asciidoc",
    ".org",
})

# Executable / buildable / stylesheet — never DOC_WRITE.
_SOURCE_EXTENSIONS = frozenset({
    ".ts", ".tsx", ".mts", ".cts",
    ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".pyi", ".pyw",
    ".go", ".rs", ".java", ".kt", ".kts",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx",
    ".cs", ".fs", ".vb",
    ".rb", ".php", ".swift", ".m", ".mm",
    ".vue", ".svelte",
    ".css", ".scss", ".sass", ".less",
    ".wasm", ".so", ".dll", ".dylib",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".sql", ".lua", ".r", ".zig", ".nim",
})

# Extensionless conventional prose filenames (classifier impl detail, not prompts)
_DOCUMENT_BASENAMES_NO_EXT = frozenset({
    "readme", "changelog", "license", "licence", "authors",
    "contributing", "history", "news", "todo", "copying", "notice",
})


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
    # role==ceo 优先于 permission_type=coordinator —— CEO 是独立行政 family，
    # 不享受中层 builder 的写码权。
    if role_l == "ceo":
        return "ceo"
    if perm == "coordinator" or role_l == "coordinator":
        return "coordinator"
    return "executor"


# ── Model tier mapping ─────────────────────────────────────

ModelTier = str  # "management" | "executor"

_MANAGEMENT_FAMILIES = frozenset({"ceo", "coordinator"})


def model_tier_for_agent(agent: dict[str, Any]) -> ModelTier:
    """Map agent to model tier: management (good models) or executor (cheap).

    management: CEO + Coordinator — 决策层用质量更好的模型
    executor: Executor + QA + HR — 执行层用性价比更高的模型
    """
    family = infer_role_family(agent)
    if family in _MANAGEMENT_FAMILIES:
        return "management"
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


def _normalize_write_path(file_path: str) -> str:
    """Normalize path for write-scope checks (preserve leading '.' segments)."""
    from hiveweave.tools.file import normalize_input_path

    # Do NOT use str.lstrip("./") — that strips every leading '.' and breaks
    # ".hiveweave/…" into "hiveweave/…" (TEST11 evening P3-2).
    norm = normalize_input_path(file_path or "").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def classify_write_kind(file_path: str) -> WriteKind:
    """Classify a path as document / source / other (extension-based).

    Used by DOC_WRITE hard gates. Implementation detail lives here — prompts
    must state the principle only ("any documentation, never source code").
    """
    norm = _normalize_write_path(file_path)
    if not norm or norm.endswith("/"):
        return "other"
    name = PurePosixPath(norm).name
    # multi-dot suffixes: take the last suffix (PurePosixPath.suffix)
    ext = PurePosixPath(name).suffix.lower()
    if ext in _DOCUMENT_EXTENSIONS:
        return "document"
    if ext in _SOURCE_EXTENSIONS:
        return "source"
    if not ext and name.lower() in _DOCUMENT_BASENAMES_NO_EXT:
        return "document"
    return "other"


def _prefix_write_allowed(norm: str) -> bool:
    """Legacy HR / no-DOC_WRITE scope: docs + .hiveweave shared dirs + root meta."""
    check = norm if norm.startswith("/") else f"/{norm}"
    for marker in _HW_WRITE_MARKERS:
        if marker in check:
            return True
    for prefix in COORDINATOR_WRITE_PREFIXES:
        if prefix.endswith("/"):
            if norm.startswith(prefix) or norm == prefix.rstrip("/"):
                return True
        else:
            if norm == prefix or norm.startswith(prefix + "."):
                return True
    base = PurePosixPath(norm).name.lower()
    if base in {"charter.md", "goals.md", "spec.md"}:
        return True
    return False


def write_path_allowed(agent: dict[str, Any], file_path: str) -> str | None:
    """Return deny reason if write/edit path is out of scope for this agent.

    Precedence:
    1. SOURCE_WRITE → anywhere
    2. DOC_WRITE → document kind only (any path); source/other denied
    3. else → legacy prefix whitelist (HR)
    """
    caps = capabilities_for(agent)
    if Capability.SOURCE_WRITE in caps:
        return None  # executors / builder coordinators may write anywhere

    norm = _normalize_write_path(file_path)
    family = infer_role_family(agent)

    if Capability.DOC_WRITE in caps:
        kind = classify_write_kind(norm)
        if kind == "document":
            return None
        return (
            f"Hard scope deny: path '{file_path}' is kind={kind}; "
            f"role_family={family} has doc_write (documentation only) — "
            f"source code and runtime config require source_write. "
            f"Delegate code changes to a mid-level coordinator."
        )

    if _prefix_write_allowed(norm):
        return None
    return (
        f"Hard scope deny: write path '{file_path}' requires source_write "
        f"or doc_write, or must be under docs/ / "
        f".hiveweave/{{shared,reports,drafts}}/ "
        f"(role_family={family})"
    )


def _extract_file_path(tool_args: dict | None) -> str:
    if not tool_args:
        return ""
    return str(
        tool_args.get("filePath")
        or tool_args.get("file_path")
        or tool_args.get("path")
        or ""
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
        # write_file + edit_file share path-kind / prefix scope
        if tool_name in ("write_file", "edit_file"):
            return write_path_allowed(agent, _extract_file_path(tool_args))
        return None


policy_service = PolicyService()
