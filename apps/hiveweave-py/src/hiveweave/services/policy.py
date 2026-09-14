"""Capability-based PolicyService — hard gates that allowed_tools cannot elevate.

Evaluation order (P0 Hard Gates):
1. Hard capability deny (role family matrix)
2. Parameter scope (path kind / prefixes for write_file & edit_file)
3. User rules: deny → ask → allow
4. Mode fallback

Role families: ceo | hr | coordinator | executor | qa

- ceo: 行政 + 里程碑验收 + DOC_WRITE（任意文档，禁源码/配置）+ BROWSE（看产品）。
  无写码/bash/test 责任。可对单条任务 waive_attestation 关闸（可不附 evidence）；
  禁止一次关掉所有任务。browse 本身不关闸；自己的浏览不算 approve 证据。
- coordinator: 中层（设计者+接缝工）— 协调权叠加写码权
  （SOURCE_WRITE / BASH_SHELL / TEST_RUN / BROWSE；写码收敛到叶子间接缝）。
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
    # MCP 服务器绑定/目录（bind_mcp/unbind_mcp/list_available_mcp）。
    # 与 BIND_SKILL 分开：四族工作角色均持（含 executor/qa 自助绑定），
    # 但 CEO 不持 —— 绑定 stdio server 等于开执行通道，CEO 无执行通道。
    MCP_BIND = "mcp_bind"
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
        # 看产品，不是测试岗。无 TEST_RUN / BROWSER_ACCEPTANCE：
        # 自己的 browse 出证不算 approve。关闸走单条 waive_attestation。
        Capability.BROWSE,
    }),
    "hr": frozenset({
        Capability.STAFFING,
        Capability.MANAGE_ORG,
        Capability.BIND_SKILL,
        Capability.MCP_BIND,
        Capability.SOURCE_READ,
    }),
    "coordinator": frozenset({
        Capability.DISPATCH,
        Capability.REVIEW,
        Capability.MERGE,
        Capability.SOURCE_READ,
        Capability.BIND_SKILL,  # bind skills on subordinates via tools
        Capability.MCP_BIND,  # bind MCP servers (self or subordinates)
        Capability.MANAGE_ORG,  # dismiss/transfer within span
        # 中层（设计者+接缝工）：协调权叠加写码权 —— 写码收敛到叶子
        # 间接缝，与 executor 同契约拥有独立 worktree。
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
        Capability.MCP_BIND,  # self-bind configured MCP servers (叶子自助)
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
        Capability.MCP_BIND,  # 与 executor 同自助绑定权
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
    # git_worktree_sync（MAIN→worktree 方向）不是合并权威（那是 MERGE 的
    # worktree→MAIN 语义）——主要用户是被派单提示引导的 executor（无 MERGE），
    # 硬门跟"能写源码就能同步自己的树"对齐：SOURCE_WRITE 为主，MERGE 兜底
    # （CEO 升级救场路径）。与 git_worktree_merge 仅 MERGE 的映射刻意不同。
    "git_worktree_sync": frozenset({
        Capability.SOURCE_WRITE,
        Capability.MERGE,
    }),
    "bash": frozenset({Capability.BASH_SHELL}),
    "bash_main": frozenset({Capability.BASH_SHELL}),
    # pwsh = bash 的 PowerShell 方言同胞（DSH_33 P0）：同一条执行管线、同一
    # 硬门。不新造能力位 —— 凡有 BASH_SHELL 者可用，CEO/HR 照旧撞门。
    "pwsh": frozenset({Capability.BASH_SHELL}),
    # T3.2: MAIN 位（pwsh 宿主顶替 bash_main），同硬门。
    "pwsh_main": frozenset({Capability.BASH_SHELL}),
    "job_kill": frozenset({Capability.BASH_SHELL}),
    "run_command": frozenset({Capability.BASH_SHELL}),
    # dev server 可执行任意命令 → 与 bash 同硬门（2026-08-13 审计：此前
    # 无 TOOL_CAPABILITY 映射，CEO 无 bash 硬门被绕过）
    "start_dev_server": frozenset({Capability.BASH_SHELL}),
    "stop_dev_server": frozenset({Capability.BASH_SHELL}),
    "stop_processes_for_worktree": frozenset({Capability.BASH_SHELL}),
    # E11 python_script 是执行通道：native 路径 create_subprocess_exec 直传
    # argv、不经 command_guard —— 留在映射外即可用
    # `script="subprocess.run(['icacls',...])"` 一行绕过 bash 硬门（42 轮
    # 审计 P1）。与 bash 同门 BASH_SHELL：executor/中层 preset 均含
    # BASH_SHELL 不受影响；HR 无 BASH_SHELL 被挡属合理收紧。
    "python_script": frozenset({Capability.BASH_SHELL}),
    # fixplan #8 交付状态位（CEO 专属出口）。
    # ⚠ 走**能力硬门**、不进 `EXEMPT_TOOLS`：`EXEMPT_TOOLS` 的语义是
    # 「豁免能力硬门」（未映射工具对全家族放行，见 tool_capability_check），
    # 而本工具是**状态写者** —— 把状态写者放进豁免集，等于把「谁能改交付
    # 状态位」从硬门降成约定。`message_user` 能豁免只因它纯通信。
    # DOC_WRITE 仅 ceo 家族持有 ⇒ 一条映射同时给出 CEO-only 硬门。
    "mark_delivery_complete": frozenset({Capability.DOC_WRITE}),
    # 交付冒烟预跑 = 按任务契约启动项目服务并跑探针脚本，同为执行通道，
    # 归 TEST_RUN 门（QA/executor/中层均有 TEST_RUN；CEO/HR 无，合理收紧）。
    "run_smoke": frozenset({Capability.TEST_RUN}),
    # 只读查注册表，不是 spawn —— 用 SOURCE_READ，避免 CEO/HR 看见工具却撞 BASH 门
    "lookup_dev_server": frozenset({Capability.SOURCE_READ}),
    "browse": frozenset({Capability.BROWSE, Capability.BROWSER_ACCEPTANCE}),
    "browse_main": frozenset({Capability.BROWSE, Capability.BROWSER_ACCEPTANCE}),
    "assert_visual": frozenset({Capability.BROWSE, Capability.BROWSER_ACCEPTANCE}),
    "game_run_case": frozenset({Capability.BROWSE, Capability.BROWSER_ACCEPTANCE}),
    "game_run_case_main": frozenset({Capability.BROWSE, Capability.BROWSER_ACCEPTANCE}),
    # Seedream text-to-image — source-writing roles only (not CEO/HR)
    "generate_image": frozenset({Capability.SOURCE_WRITE}),
    "spawn_subagent": frozenset({Capability.SOURCE_WRITE}),
    # 45 轮 #9：MCP 目录只读。原映射 STAFFING（仅 HR）——MCP_BIND 自助
    # 绑定落地后放宽为 MCP_BIND（四族工作角色均可发现可绑 server；
    # HR 招聘提示调用方不受影响，hr 亦持 MCP_BIND）。
    "list_available_mcp": frozenset({Capability.MCP_BIND}),
    # MCP 自助绑定（09-08）：绑定的前提是 server 已由用户在设置里配置，
    # agent 只是把自己/下属挂上去 —— 不给注册新 server 的能力（stdio
    # 注册=将来会 spawn 任意进程，仍属用户专属操作）。
    "bind_mcp": frozenset({Capability.MCP_BIND}),
    "unbind_mcp": frozenset({Capability.MCP_BIND}),
    # ── 45 轮批次6 收编：只读观测类（五族均有 SOURCE_READ，映射零行为
    # 变化；原 EXEMPT 豁免收回，启动断言从此对它们有真实覆盖）──
    "grep": frozenset({Capability.SOURCE_READ}),
    "list_files": frozenset({Capability.SOURCE_READ}),
    "read_file": frozenset({Capability.SOURCE_READ}),
    "search_files": frozenset({Capability.SOURCE_READ}),
    "read_charter": frozenset({Capability.SOURCE_READ}),
    "read_goals": frozenset({Capability.SOURCE_READ}),
    "read_memory": frozenset({Capability.SOURCE_READ}),
    "read_roster": frozenset({Capability.SOURCE_READ}),
    "read_skill": frozenset({Capability.SOURCE_READ}),
    "read_work_logs": frozenset({Capability.SOURCE_READ}),
    "list_available_skills": frozenset({Capability.SOURCE_READ}),
    "list_alarms": frozenset({Capability.SOURCE_READ}),
    "list_subordinates": frozenset({Capability.SOURCE_READ}),
    "check_agent_status": frozenset({Capability.SOURCE_READ}),
    "check_agent_progress": frozenset({Capability.SOURCE_READ}),
    "get_platform_state": frozenset({Capability.SOURCE_READ}),
    "get_tasks": frozenset({Capability.SOURCE_READ}),
    "view_org_chart": frozenset({Capability.SOURCE_READ}),
    "git_worktree_list": frozenset({Capability.SOURCE_READ}),
    "git_worktree_status": frozenset({Capability.SOURCE_READ}),
    # checkpoint 只写自己的 worktree——executor/qa/coordinator 均有
    # SOURCE_WRITE；CEO/HR 本就不在该工具的可见集（allowlist 外）。
    "git_worktree_checkpoint": frozenset({Capability.SOURCE_WRITE}),
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
    # 出口合同前置审计 — 只对可写码角色开放（executor/qa/builder coordinator）
    "request_code_audit": frozenset({Capability.SOURCE_WRITE}),
    # start_team_meeting：MANAGE_ORG 映射 + 家族特判（HR 有 MANAGE_ORG
    # 但不开会——见下方 tool_hard_deny）。
    "start_team_meeting": frozenset({Capability.MANAGE_ORG}),
    # write_file: capability depends on path scope (checked separately)
}

# Paths HR (no DOC_WRITE / SOURCE_WRITE) may write — legacy prefix scope.
#
# ⚠ 这张表**故意**比 agent 可访问的 .hiveweave 子目录集更窄（只含 doc 性质的
# shared/reports/drafts），不要"对齐"成全集：这里管的是**无源码写权角色能写哪**，
# 而 `tools/file.py::_check_hiveweave_dir` 与 `tools/bash.py::_ALLOWED_HW_SUBDIRS`
# 管的是**agent 工具通道能读写哪**（含 worktrees/handoffs/sandbox-temp，
# 以及只读的 merge-quarantine）。三者语义不同，不共用一个常量。
#
# 真正需要防的是「file.py 与 bash.py 两份清单悄悄漂移」（report TEST_DSH_54 #5
# 点名的既有债务：加目录要三处手工同步）。该漂移由**行为化交叉守卫**兜住 ——
# `tests/test_hiveweave_dir_protection.py::TestHiveweaveAllowlistConsistency`
# 逐子目录断言两层判定一致，只改一处即转红。
COORDINATOR_WRITE_PREFIXES = (
    "docs/",
    "doc/",
    "tests/",  # QA 主管（qa_lead）：契约驱动验收探针写在 tests/smoke/
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
    """Match QA / 测试工程师 roles (shared with prompts.executor).

    只认结构化/语义明确的测试角色：exact 键、test/qa engineer 短语、
    测试工程师/测试专员、浏览器测试（浏览器 QA 语义明确）。
    **不**匹配裸词 ``e2e`` —— role 名里带 e2e 的协调员/开发岗（如
    "S3 e2e 数据面"）不应被判成 qa（E2E 实测误判，2026-08）。
    """
    original = role or ""
    r = original.strip().lower()
    if r in {"test_engineer", "qa_engineer", "qa engineer", "qa"}:
        return True
    if "test engineer" in r or "qa engineer" in r:
        return True
    if "测试工程师" in original or "测试专员" in original:
        return True
    if "浏览器测试" in original:
        return True
    if "evidence collector" in r:
        return True
    if r.endswith(" qa"):
        return True
    return False


def infer_role_family(agent: dict[str, Any]) -> RoleFamily:
    """Derive role family from agent row.

    优先级（结构化角色 ID 优先，松散 role 名仅兜底）：
    1. 显式 role_family
    2. 精确机器可读角色键 hr/ceo（历史 permission_type 可能被存成 coordinator）
    3. 精确 QA 角色键（qa / qa_engineer / test_engineer）—— 角色名即角色 ID
    4. permission_type==coordinator —— 协调员不会被 role 名松散子串带偏成 qa
    5. 测试工程师松散 role 名兜底（Echo 事故：测试工程师常无 qa permission_type）
    6. 其余 permission_type 角色 ID；兜底 executor
    """
    explicit = (agent.get("role_family") or "").strip().lower()
    if explicit in FAMILY_CAPABILITIES:
        return explicit

    role = (agent.get("role") or "").strip()
    role_l = role.lower()
    perm = (agent.get("permission_type") or "").strip().lower()

    if role_l == "hr" or role == "人力资源" or "人力资源" in role:
        return "hr"
    # role==ceo 优先于 permission_type=coordinator —— CEO 是独立行政 family，
    # 不享受中层 builder 的写码权。
    if role_l == "ceo":
        return "ceo"
    # 精确 QA 角色键：role 名即角色 ID，优先于 permission_type
    if role_l in ("qa", "qa_engineer", "qa engineer", "test_engineer"):
        return "qa"
    # 结构化角色 ID：协调员不被 role 名松散子串（e2e 等）带偏成 qa
    if perm == "coordinator" or role_l == "coordinator":
        return "coordinator"
    if is_test_engineer_role(role):
        return "qa"
    if perm in FAMILY_CAPABILITIES:
        return perm
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


def has_visual_test_duty(agent: dict[str, Any]) -> bool:
    """True if this agent produces UI/test evidence (not look-only browse).

    CEO has BROWSE to look at the product but neither TEST_RUN nor
    BROWSER_ACCEPTANCE — screenshots are inspection, not a gate.
    """
    return has_capability(agent, Capability.TEST_RUN) or has_capability(
        agent, Capability.BROWSER_ACCEPTANCE
    )


# Stamp tools share TOOL_CAPABILITY OR {BROWSE, BROWSER_ACCEPTANCE}.
# Look-only BROWSE (CEO) must still hard-deny these.
_VISUAL_STAMP_TOOLS = frozenset({
    "assert_visual",
    "game_run_case",
    "game_run_case_main",
})

# ── 团队开会（docs/spec/team-meeting.md §权限与工具）──────────
# start_team_meeting：仅 ceo / coordinator。MANAGE_ORG 不够（HR 也有），
# 下方 tool_hard_deny 再加家族特判（同 hire_agent 的 HR-only 形状）。
# speak/continue/conclude 是 MeetingTurnRunner 执行期白名单工具：
# 不进任何角色 allowlist，普通路径一律硬拒（runner 回调本地拦截，不落
# executor —— 此 deny 是纵深防御 + 启动断言的「显式决策」落点）。
MEETING_RUNNER_TOOLS = frozenset({
    "speak_in_meeting",
    "continue_meeting_round",
    "conclude_topic",
})

_MEETING_FAMILIES = frozenset({"ceo", "coordinator"})


def tool_hard_deny(agent: dict[str, Any], tool_name: str) -> str | None:
    """Return deny reason if tool is blocked by hard capability, else None."""
    from hiveweave.services.eval_seal import sealed_tool_deny

    sealed = sealed_tool_deny(agent, tool_name)
    if sealed:
        return sealed
    # T3.2 纵深防御：Windows pwsh 宿主上 bash/bash_main 不暴露 —— 模型按
    # 陈旧提示直调时给出可操作错误（指向 pwsh / pwsh_main），而不是执行。
    try:
        from hiveweave.services.host_env import host_tool_deny_reason

        host_deny = host_tool_deny_reason(tool_name)
        if host_deny:
            return host_deny
    except Exception:
        pass  # 过滤层故障不改变能力判定结果（fail-open 到能力门）
    caps = capabilities_for(agent)
    required = TOOL_CAPABILITY.get(tool_name)
    # Meeting runner tools: whitelist-only inside MeetingTurnRunner; the
    # normal executor path can never reach them (runner intercepts locally).
    # 必须放在未映射早退之前 —— 它们不在 TOOL_CAPABILITY（EXEMPT 是显式
    # 决策：能力门即「全员显式硬拒」，见 tool_capability_check）。
    if tool_name in MEETING_RUNNER_TOOLS:
        return (
            f"Hard capability deny: '{tool_name}' is a meeting-internal "
            "tool (MeetingTurnRunner whitelist only) — normal turns cannot "
            "call it"
        )
    # start_team_meeting: MANAGE_ORG passes for HR too — meetings are
    # chaired by CEO / mid-level coordinators only (spec §权限与工具).
    # 放在能力不匹配文案之前：拒绝提示要写明「谁能开」（规格验收 A）。
    if tool_name == "start_team_meeting" and (
        infer_role_family(agent) not in _MEETING_FAMILIES
    ):
        return (
            f"Hard capability deny: 'start_team_meeting' may only be called "
            f"by ceo or coordinator; role_family="
            f"{infer_role_family(agent)} cannot open team meetings"
        )
    if required is None:
        # write_file 的能力判定走 hard_check → write_path_allowed 的路径
        # scope（TOOL_CAPABILITY 特意不映射它）；未映射工具已由启动断言
        # （tool_capability_check）保证有映射或显式豁免，这里统一放行。
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
    # BROWSE OR BROWSER_ACCEPTANCE lets CEO pass the cap check for stamp
    # tools. Look-only browse is not test duty — allowed_tools must not elevate.
    if tool_name in _VISUAL_STAMP_TOOLS and not has_visual_test_duty(agent):
        family = infer_role_family(agent)
        return (
            f"Hard capability deny: '{tool_name}' stamps test evidence; "
            f"role_family={family} may browse to look, not to attest."
        )
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
        if tool_name in (
            "bash", "bash_main", "pwsh", "pwsh_main", "run_command",
            "start_dev_server",
        ):
            from hiveweave.services.eval_seal import sealed_bash_deny

            cmd = ""
            if tool_args:
                cmd = str(
                    tool_args.get("command") or tool_args.get("cmd") or ""
                )
            bash_reason = sealed_bash_deny(agent, cmd)
            if bash_reason:
                return bash_reason
        # write_file + edit_file share path-kind / prefix scope
        if tool_name in ("write_file", "edit_file"):
            return write_path_allowed(agent, _extract_file_path(tool_args))
        return None


policy_service = PolicyService()
