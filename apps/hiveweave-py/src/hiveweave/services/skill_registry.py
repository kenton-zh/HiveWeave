"""Skill registry service — SKILL.md-style instruction binding.

契约 10: MCP 与技能（技能部分）
- 技能来源三层：外部文件系统（agent-skills）→ 内置注册表 → skills.sh 远程市场
- 技能定义包含：slug / name / description / instructions / category
- bind_skill / unbind_skill 修改 agents.bound_skills（Meta DB）
- skills 字段为不可变入职快照（hire 时写入，bind/unbind 不动）
- bound_skills 为运行时可变集合（初始化为 skills 副本）
- build_active_skills_section 注入 system prompt 摘要段（仅摘要，read_skill 按需加载全文）
- skills.sh best-effort（8s 超时，失败静默降级到 外部 + 内置）

权限门禁（resolve_and_update_agent，由 tool_executor 层强制）：
- 自身 / 直属下属 / CEO+HR 可操作项目内任意 agent；跨项目拒绝
- 本服务只做数据层操作，权限校验由上游 tool_executor 负责

移植自 Elixir skill_registry.ex + TS clawhub-service.ts。已迁移到 skills.sh。
"""

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from hiveweave.config import settings
from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

# ── 常量 ────────────────────────────────────────────────────
# 外部技能目录（best-effort；不存在则返回空）
# 通过环境变量 HIVEWEAVE_EXTERNAL_SKILLS_DIR 配置；未设则无外部技能
EXTERNAL_SKILLS_DIR = Path(settings.external_skills_dir) if settings.external_skills_dir else None

CLAWHUB_BASE_URL = "https://clawhub.ai/api/v1/skills"  # legacy, unused
CLAWHUB_TIMEOUT = 5.0  # 契约 10: 5s 超时，失败静默降级

# skills.sh — Open Agent Skills Ecosystem (https://www.skills.sh)
# 技能格式: owner/repo/skill-name (e.g. anthropics/skills/frontend-design)
# SKILL.md 内容在详情页服务端渲染，可直接 httpx 抓取。
SKILLS_SH_BASE_URL = "https://www.skills.sh"
SKILLS_SH_TIMEOUT = 8.0  # skills.sh 页面较大，给 8s


# ── Built-in skill registry（8 个内置技能）──────────────────
# 移植自 Elixir skill_registry.ex @builtin_skills

BUILTIN_SKILLS: list[dict[str, Any]] = [
    {
        "slug": "code-review",
        "name": "code-review",
        "description": "Review code for quality, patterns, and potential bugs.",
        "category": "quality",
        "instructions": (
            "# Code Review Skill\n\n"
            "When reviewing code:\n"
            "1. Check for correctness — does the code do what it claims?\n"
            "2. Check for readability — is it clear and maintainable?\n"
            "3. Check for performance — any obvious bottlenecks or N+1 queries?\n"
            "4. Check for security — input validation, auth checks, injection risks.\n"
            "5. Check for consistency — naming conventions, error handling patterns.\n"
            "6. Provide actionable feedback — not just \"this is wrong\" but \"here's how to fix it\".\n"
            "7. Distinguish between blockers (must fix) and suggestions (nice to have).\n"
        ),
    },
    {
        "slug": "testing",
        "name": "testing",
        "description": "Write and run unit tests and integration tests.",
        "category": "quality",
        "instructions": (
            "# Testing Skill\n\n"
            "When writing tests:\n"
            "1. Start with the happy path — verify core functionality works.\n"
            "2. Add edge cases — empty input, boundary values, large input.\n"
            "3. Add error cases — invalid input, missing dependencies, timeout.\n"
            "4. Use descriptive test names that explain the expected behavior.\n"
            "5. Follow AAA pattern: Arrange, Act, Assert.\n"
            "6. Mock external dependencies — don't make real API calls in unit tests.\n"
            "7. Aim for high coverage on business logic, not on boilerplate.\n"
        ),
    },
    {
        "slug": "documentation",
        "name": "documentation",
        "description": "Generate and maintain project documentation.",
        "category": "engineering",
        "instructions": (
            "# Documentation Skill\n\n"
            "When writing documentation:\n"
            "1. Start with a clear summary — what does this module/function do?\n"
            "2. Document parameters, return values, and exceptions.\n"
            "3. Include usage examples that actually work.\n"
            "4. Explain WHY, not just WHAT — the reasoning behind design decisions.\n"
            "5. Keep docs near the code they describe.\n"
            "6. Update docs when code changes — stale docs are worse than no docs.\n"
        ),
    },
    {
        "slug": "debugging",
        "name": "debugging",
        "description": "Diagnose and fix runtime errors and performance issues.",
        "category": "engineering",
        "instructions": (
            "# Debugging Skill\n\n"
            "When debugging:\n"
            "1. Reproduce the issue reliably before attempting fixes.\n"
            "2. Read the error message carefully — it usually tells you what's wrong.\n"
            "3. Isolate the problem — binary search by commenting out code.\n"
            "4. Check recent changes — git log/diff to see what changed.\n"
            "5. Add logging to trace execution flow.\n"
            "6. Fix the root cause, not the symptom.\n"
            "7. Verify the fix doesn't introduce new issues.\n"
        ),
    },
    {
        "slug": "refactoring",
        "name": "refactoring",
        "description": "Restructure code for better maintainability without changing behavior.",
        "category": "engineering",
        "instructions": (
            "# Refactoring Skill\n\n"
            "When refactoring:\n"
            "1. Ensure tests exist before refactoring — they verify behavior is unchanged.\n"
            "2. Make small, incremental changes — one refactor at a time.\n"
            "3. Rename variables/functions to express intent clearly.\n"
            "4. Extract complex logic into named functions.\n"
            "5. Reduce duplication — DRY, but don't over-abstract.\n"
            "6. Keep the public API stable — internal changes only.\n"
            "7. Run tests after each change.\n"
        ),
    },
    {
        "slug": "security-audit",
        "name": "security-audit",
        "description": "Scan code for security vulnerabilities and best practice violations.",
        "category": "security",
        "instructions": (
            "# Security Audit Skill\n\n"
            "When auditing security:\n"
            "1. Check authentication — are all sensitive endpoints protected?\n"
            "2. Check authorization — can users access resources they shouldn't?\n"
            "3. Check input validation — SQL injection, XSS, path traversal.\n"
            "4. Check secrets management — no hardcoded passwords/API keys.\n"
            "5. Check dependencies — known CVEs in packages.\n"
            "6. Check error handling — don't leak stack traces to users.\n"
            "7. Check rate limiting — protect against brute force.\n"
        ),
    },
    {
        "slug": "deployment",
        "name": "deployment",
        "description": "Manage CI/CD pipelines and deployment workflows.",
        "category": "ops",
        "instructions": (
            "# Deployment Skill\n\n"
            "When managing deployments:\n"
            "1. Use infrastructure-as-code where appropriate.\n"
            "2. Separate build and run stages.\n"
            "3. Use environment variables for configuration — never hardcode.\n"
            "4. Run tests in CI before deploying.\n"
            "5. Use blue-green or canary deployments for zero-downtime.\n"
            "6. Monitor after deployment — logs, metrics, alerts.\n"
            "7. Have a rollback plan — know how to revert quickly.\n"
        ),
    },
    {
        "slug": "data-analysis",
        "name": "data-analysis",
        "description": "Analyze data sets, generate reports and visualizations.",
        "category": "analytics",
        "instructions": (
            "# Data Analysis Skill\n\n"
            "When analyzing data:\n"
            "1. Understand the question — what decision will this analysis inform?\n"
            "2. Clean the data — handle missing values, outliers, duplicates.\n"
            "3. Explore with summary statistics — mean, median, distribution.\n"
            "4. Visualize patterns — use appropriate chart types.\n"
            "5. Test hypotheses — statistical significance matters.\n"
            "6. Communicate findings clearly — actionable insights, not just numbers.\n"
            "7. Document your methodology — others should be able to reproduce.\n"
        ),
    },
    # ── gstack Discipline Skills ─────────────────────────────
    {
        "slug": "self-review",
        "name": "self-review",
        "description": "Five-axis self-review before submitting code: correctness, readability, architecture, security, performance.",
        "category": "discipline",
        "instructions": (
            "# Self-Review Discipline\n\n"
            "Before submitting ANY code to QA or your superior, run a five-axis self-review:\n\n"
            "1. **Correctness**: Does the code do what it claims? Edge cases handled? Null/empty states?\n"
            "2. **Readability**: Can someone else understand this without asking? Clear naming? No magic numbers?\n"
            "3. **Architecture**: Is this in the right place? No duplicated logic? Clean separation of concerns?\n"
            "4. **Security**: Any injection risks? Hardcoded secrets? Unsanitized user input? Path traversal?\n"
            "5. **Performance**: N+1 queries? Unbounded loops? Missing indexes? Memory leaks?\n\n"
            "Fix everything you find BEFORE submitting. QA finding a basic issue = you didn't self-review seriously.\n"
            "Output: a brief self-review report (what you checked, what you fixed, what you're unsure about).\n"
        ),
    },
    {
        "slug": "code-review-and-quality",
        "name": "code-review-and-quality",
        "description": "Structured five-axis code review with severity tagging and actionable feedback. Equivalent to gstack /review discipline.",
        "category": "discipline",
        "instructions": (
            "# Code Review & Quality Discipline\n\n"
            "When reviewing code, run a structured five-axis review:\n\n"
            "1. **Correctness**: Logic errors, edge conditions, race conditions, error handling gaps.\n"
            "2. **Readability**: Naming, structure, comments, complexity — would a new team member understand this?\n"
            "3. **Architecture**: Layering, coupling, abstraction levels, Hyrum's Law, dependency direction.\n"
            "4. **Security**: Input validation, auth/authz, data leaks, injection, OWASP Top 10.\n"
            "5. **Performance**: Algorithm complexity, N+1 queries, memory leaks, async patterns.\n\n"
            "Output format (MANDATORY):\n"
            "- Verdict: APPROVE / CHANGES REQUESTED / REJECT\n"
            "- Critical Issues: must-fix before merge (tagged CRITICAL)\n"
            "- Warnings: should-fix (tagged WARNING)\n"
            "- Nits: nice-to-fix (tagged NIT)\n"
            "- What's Done Well: positive reinforcement\n"
            "Each finding: path:line: [SEVERITY] problem. fix suggestion.\n"
            "Standard: \"Would a staff engineer approve this?\"\n"
        ),
    },
    {
        "slug": "security-and-hardening",
        "name": "security-and-hardening",
        "description": "OWASP Top 10 + STRIDE threat modeling + exploit scenario construction. Equivalent to gstack /cso discipline.",
        "category": "discipline",
        "instructions": (
            "# Security & Hardening Discipline\n\n"
            "When auditing security, use exploit-driven analysis:\n\n"
            "1. **OWASP Top 10**: Injection, XSS, CSRF, SSRF, broken auth, sensitive data exposure, XXE, broken access control, security misconfig, insufficient logging.\n"
            "2. **STRIDE Threat Modeling**: Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege.\n"
            "3. **Secret Detection**: Hardcoded keys, tokens, passwords, API keys — in code AND config files.\n"
            "4. **Dependency Supply Chain**: Known CVEs in dependencies.\n"
            "5. **LLM/AI Security**: Prompt injection, excessive agency, unbounded consumption (OWASP LLM Top 10).\n\n"
            "Every finding MUST include: CWE ID + CVSS estimate + concrete exploit scenario + specific fix.\n"
            "Confidence threshold: 8/10 minimum to report. Can't construct an exploit? Don't report it.\n"
            "Output: CLEAR / ISSUES FOUND / CRITICAL VULNERABILITY. Critical findings escalate immediately.\n"
        ),
    },
    {
        "slug": "debugging-and-error-recovery",
        "name": "debugging-and-error-recovery",
        "description": "Systematic root-cause investigation — reproduce, isolate, fix, verify. Equivalent to gstack /investigate discipline.",
        "category": "discipline",
        "instructions": (
            "# Debugging & Error Recovery Discipline\n\n"
            "When debugging, never guess — investigate:\n\n"
            "1. **Reproduce**: Can you trigger the bug reliably? Write a reproduction script.\n"
            "2. **Isolate**: Binary-search by commenting out code, checking git bisect, narrowing input.\n"
            "3. **Read the Error**: The error message usually tells you exactly what's wrong. Don't skip it.\n"
            "4. **Check Recent Changes**: git log/diff — what changed? When did it break?\n"
            "5. **Add Logging**: Trace execution flow at the key decision points.\n"
            "6. **Fix Root Cause**: Don't fix the symptom. Understand WHY it happened.\n"
            "7. **Verify**: The reproduction script now passes. Add it as a regression test.\n"
            "8. **Escalate if stalled**: After 3 failed repair attempts, escalate to superior with: what was tried, what was observed each time, what you suspect but can't confirm.\n"
            "Never: comment out a failing test, apply a workaround without understanding, claim 'fixed' without verifying.\n"
        ),
    },
    {
        "slug": "design-consultation",
        "name": "design-consultation",
        "description": "Research competitors, extract design language, propose complete design system. Equivalent to gstack /design-consultation.",
        "category": "discipline",
        "instructions": (
            "# Design Consultation Discipline\n\n"
            "When designing UI/UX for a new feature or product:\n\n"
            "1. **Understand the Product**: What do users do here? What's the core interaction?\n"
            "2. **Research Competitors**: Study 2-3 comparable products — what patterns work? What's missing?\n"
            "3. **Extract Design Language**: Colors, typography, spacing, component patterns, interaction models.\n"
            "4. **Propose Design System**: Complete system covering: aesthetic direction, typography scale, color palette, spacing system, motion guidelines, component library.\n"
            "5. **Apply Beautiful Defaults**: Composition-first, brand-first, cardless, poster-not-document.\n\n"
            "Output: a design brief with mood, direction, and concrete specifications (font names, hex colors, spacing values).\n"
            "Reference styles: poe.ninja, maxroll for dark data-dense UIs; Linear, Vercel for clean functional UIs.\n"
        ),
    },
    {
        "slug": "design-review",
        "name": "design-review",
        "description": "Pixel-level visual quality audit: spacing, hierarchy, AI slop patterns, interaction states. Equivalent to gstack /design-review.",
        "category": "discipline",
        "instructions": (
            "# Design Review Discipline\n\n"
            "When reviewing UI implementation against design specs:\n\n"
            "1. **Spacing & Alignment**: Consistent padding/margins? Grid adherence? Baseline alignment?\n"
            "2. **Typography**: Correct font, size, weight, line-height? Hierarchy clear?\n"
            "3. **Color**: Exact hex values? Contrast ratios pass WCAG AA?\n"
            "4. **Interaction States**: Hover, active, focus, disabled, loading, empty, error — all accounted for?\n"
            "5. **AI Slop Patterns**: Generic card layouts, excessive rounded corners, same-size-everything, placeholder-looking content.\n"
            "6. **Edge Cases**: Long names (47 chars), zero results, network error, first-time user, returning user.\n"
            "7. **Information Hierarchy**: What does the user see first? second? third? Does the visual weight match the importance?\n\n"
            "Output: PASS or numbered issues with screenshots where possible. Each issue: location, problem, fix.\n"
        ),
    },
    {
        "slug": "planning-and-task-breakdown",
        "name": "planning-and-task-breakdown",
        "description": "Decompose specs into atomic verifiable tasks with dependency ordering. Equivalent to gstack /plan-eng-review.",
        "category": "discipline",
        "instructions": (
            "# Planning & Task Breakdown Discipline\n\n"
            "When breaking down a spec into executable tasks:\n\n"
            "1. **Atomic Tasks**: Each task is independently verifiable — has a clear pass/fail criterion.\n"
            "2. **Dependency Order**: Order tasks by dependency. What must be done first?\n"
            "3. **Acceptance Criteria**: Every task has concrete, testable acceptance criteria — not \"implement X\" but \"X works when Y input produces Z output\".\n"
            "4. **Edge Cases Identified**: What breaks? What's the error path? What if the input is empty?\n"
            "5. **Estimation**: Rough effort estimate per task (S/M/L). Don't over-precision.\n"
            "6. **Interface Contracts**: Between-task interfaces must be explicit — what format, what fields, what guarantees?\n\n"
            "Output: ordered task list with dependencies, acceptance criteria, and interface contracts.\n"
            "Principle: No task without verification. No verification without evidence.\n"
        ),
    },
    {
        "slug": "shipping-and-launch",
        "name": "shipping-and-launch",
        "description": "Pre-launch checklist: tests, changelog, version bump, regression check, deployment verification. Equivalent to gstack /ship.",
        "category": "discipline",
        "instructions": (
            "# Shipping & Launch Discipline\n\n"
            "Before shipping any code:\n\n"
            "1. **Tests Pass**: All tests green. No skipped tests. Coverage not decreased.\n"
            "2. **No Regressions**: Run existing test suite — nothing broke.\n"
            "3. **Changelog Updated**: What changed, what's new, what's fixed, what's deprecated.\n"
            "4. **Version Bumped**: Semantic versioning (MAJOR.MINOR.PATCH) — breaking changes get MAJOR bump.\n"
            "5. **Docs Updated**: API docs, README, deployment guides — all current.\n"
            "6. **Deployment Verified**: Health check passes, smoke test passes, canary monitoring active.\n"
            "7. **Rollback Plan**: If this deploy fails, how do we revert? Document the rollback steps.\n\n"
            "Output: a ship report (version, changelog, test results, deployment status, rollback instructions).\n"
            "Gate: ALL items must pass. Partial ship = no ship.\n"
        ),
    },
    {
        "slug": "spec-driven-development",
        "name": "spec-driven-development",
        "description": "Turn vague intent into precise executable specification before coding. Equivalent to gstack /spec.",
        "category": "discipline",
        "instructions": (
            "# Spec-Driven Development Discipline\n\n"
            "Before writing any code, produce a spec that answers:\n\n"
            "1. **What**: What are we building? One-sentence problem statement.\n"
            "2. **Who**: Who is this for? One specific user persona.\n"
            "3. **Why**: Why now? What changes if we build this? What changes if we don't?\n"
            "4. **Success Criteria**: How do we know it's done? Concrete, measurable outcomes.\n"
            "5. **Constraints**: Technical, time, resource, compliance constraints.\n"
            "6. **Edge Cases**: Empty states, error states, boundary conditions, concurrent access.\n"
            "7. **Dependencies**: What must exist first? What blocks this?\n"
            "8. **Out of Scope**: What are we explicitly NOT building? (Prevents scope creep)\n\n"
            "Get explicit user sign-off before proceeding to PLAN.\n"
            "Spec must be complete enough that an engineer who has never seen this project can implement it.\n"
        ),
    },
    {
        "slug": "context-engineering",
        "name": "context-engineering",
        "description": "Save and restore working context across sessions. Equivalent to gstack /context-save and /context-restore.",
        "category": "discipline",
        "instructions": (
            "# Context Engineering Discipline\n\n"
            "When working on long-running tasks across multiple sessions:\n\n"
            "1. **Save Context**: After completing a logical unit, save: what was done, what decisions were made, what's next, what's blocked.\n"
            "2. **Restore Context**: Before starting work, load the last saved context — don't rediscover what was already known.\n"
            "3. **Handoff Notes**: When passing work between agents, include: what was done, what's pending, what problems were encountered, what the next agent needs to know.\n\n"
            "Context persistence goes to: project memory (write_memory) for decisions, charter for project-level state, handoffs for agent-to-agent transfer.\n"
            "Principle: Never make the next agent (or your future self) rediscover what you already know.\n"
        ),
    },
    {
        "slug": "incremental-implementation",
        "name": "incremental-implementation",
        "description": "Deliver changes incrementally. Use when implementing any feature or change that touches more than one file.",
        "category": "discipline",
        "instructions": (
            "# Incremental Implementation Discipline\n\n"
            "When implementing a feature or change:\n\n"
            "1. **Break down**: Split the work into small, independently verifiable steps.\n"
            "2. **Implement step by step**: Complete one step fully before starting the next.\n"
            "3. **Verify each step**: After each step, confirm it works before moving on.\n"
            "4. **Commit early and often**: Each completed step should be a checkpoint.\n"
            "5. **Never batch unrelated changes**: One logical change per step.\n\n"
            "Principle: Small, verified steps are faster than one big batch that breaks in mysterious ways.\n"
        ),
    },
    {
        "slug": "test-driven-development",
        "name": "test-driven-development",
        "description": "Drive development with tests. Use when implementing any logic, fixing any bug, or changing any behavior.",
        "category": "discipline",
        "instructions": (
            "# Test-Driven Development Discipline\n\n"
            "When implementing or changing behavior:\n\n"
            "1. **Write a failing test first**: Define the expected behavior before writing code.\n"
            "2. **Write minimal code to pass**: Implement just enough to make the test green.\n"
            "3. **Refactor**: Clean up the code while keeping tests green.\n"
            "4. **Edge cases**: Add tests for boundary conditions, error paths, and empty inputs.\n\n"
            "Principle: If it's not tested, it doesn't work. Tests are the specification.\n"
        ),
    },
    {
        "slug": "frontend-ui-engineering",
        "name": "frontend-ui-engineering",
        "description": "Build production-quality UIs. Use when creating or modifying user-facing interfaces, components, or layouts.",
        "category": "tool",
        "instructions": (
            "# Frontend UI Engineering Skill\n\n"
            "When building user interfaces:\n\n"
            "1. **Component-first**: Break UI into reusable components with clear props and responsibilities.\n"
            "2. **State management**: Choose the right state approach (local, context, global) — don't over-engineer.\n"
            "3. **Accessibility**: Semantic HTML, keyboard navigation, ARIA labels where needed.\n"
            "4. **Responsive**: Design for mobile-first, then scale up.\n"
            "5. **Performance**: Lazy-load heavy components, memoize expensive renders, avoid unnecessary re-renders.\n"
            "6. **Error boundaries**: Wrap components that might fail; show fallback UI.\n\n"
            "Principle: The UI must look and feel production-quality, not AI-generated.\n"
        ),
    },
    {
        "slug": "interview-me",
        "name": "interview-me",
        "description": "Extract what the user actually wants through one-question-at-a-time interviews. Use when requirements are underspecified.",
        "category": "discipline",
        "instructions": (
            "# Interview Discipline\n\n"
            "When requirements are vague or underspecified:\n\n"
            "1. **One question at a time**: Never overwhelm with a list of questions.\n"
            "2. **Understand intent**: Ask 'why' before 'what' — the goal matters more than the feature.\n"
            "3. **Confirm understanding**: Restate what you heard before proceeding.\n"
            "4. **Stop when confident**: End the interview when you have ~95% confidence.\n\n"
            "Principle: Don't silently fill in ambiguous requirements — ask first.\n"
        ),
    },
    {
        "slug": "documentation-and-adrs",
        "name": "documentation-and-adrs",
        "description": "Record decisions and documentation. Use when making architectural decisions or changing public APIs.",
        "category": "discipline",
        "instructions": (
            "# Documentation & ADR Discipline\n\n"
            "When making significant decisions:\n\n"
            "1. **Record the decision**: What was decided, when, by whom, and why.\n"
            "2. **Capture alternatives**: What other options were considered? Why were they rejected?\n"
            "3. **Document consequences**: What impact does this decision have on the system?\n"
            "4. **Update docs**: Keep API docs, READMEs, and architecture diagrams in sync.\n\n"
            "Principle: Future engineers (and agents) need to understand WHY, not just WHAT.\n"
        ),
    },
]


# ── Helpers ─────────────────────────────────────────────────


def _parse_json_list(json_str: str | None) -> list[str]:
    """解析 JSON 字符串为字符串列表；非列表/异常返回 []。"""
    if not json_str:
        return []
    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            return [str(x) for x in data]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _filter_skills(skills: list[dict], search: str | None) -> list[dict]:
    """按 slug / description 模糊过滤（大小写不敏感）。"""
    if not search:
        return skills
    k = search.lower()
    return [
        s for s in skills
        if k in (s.get("slug") or "").lower()
        or k in (s.get("description") or "").lower()
    ]


def _parse_frontmatter(path: Path) -> dict[str, str] | None:
    """解析 SKILL.md 的 YAML frontmatter（name, description）。

    不依赖 python-frontmatter 库（与 Elixir 实现一致，用正则提取字段）。
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not content.startswith("---"):
        return None
    # 按 --- 分隔：[前导, frontmatter, body]
    parts = re.split(r"^---\s*$", content, maxsplit=2, flags=re.MULTILINE)
    if len(parts) < 3:
        return None
    frontmatter = parts[1]
    return {
        "name": _extract_yaml_field(frontmatter, "name"),
        "description": _extract_yaml_field(frontmatter, "description"),
    }


def _extract_yaml_field(yaml_text: str, field: str) -> str:
    """从 YAML 文本中提取单行字段值（去引号/去 > 前缀）。"""
    m = re.search(rf"^{field}:\s*(.+)$", yaml_text, re.MULTILINE)
    if not m:
        return ""
    val = m.group(1).strip()
    val = val.strip('"').strip("'")
    if val.startswith(">"):
        val = val[1:].strip()
    return val


class SkillRegistryService:
    """Skill registry — list, bind, unbind, read SKILL.md instructions.

    三层来源优先级：外部文件系统 → 内置注册表 → skills.sh 远程市场。
    """

    # ── 外部技能（文件系统，同步）──────────────────────────────

    @staticmethod
    def _list_external_skills(search: str | None = None) -> list[dict]:
        """扫描外部技能目录，解析每个子目录的 SKILL.md frontmatter。"""
        if not EXTERNAL_SKILLS_DIR or not EXTERNAL_SKILLS_DIR.exists():
            return []
        results: list[dict] = []
        try:
            for entry in EXTERNAL_SKILLS_DIR.iterdir():
                if not entry.is_dir():
                    continue
                skill_md = entry / "SKILL.md"
                if not skill_md.exists():
                    continue
                meta = _parse_frontmatter(skill_md)
                if meta is None:
                    continue
                results.append({
                    "slug": entry.name,
                    "name": meta.get("name") or entry.name,
                    "description": meta.get("description") or "",
                    "instructions": "",  # 全文在 read_skill 时按需加载
                    "source": "external",
                    "category": "external",
                })
        except OSError:
            pass
        return _filter_skills(results, search)

    @staticmethod
    def _get_external_skill(slug: str) -> dict | None:
        """取单个外部技能的元信息（不含全文）。"""
        if not EXTERNAL_SKILLS_DIR:
            return None
        skill_md = EXTERNAL_SKILLS_DIR / slug / "SKILL.md"
        if not skill_md.exists():
            return None
        meta = _parse_frontmatter(skill_md)
        if meta is None:
            return None
        return {
            "slug": slug,
            "name": meta.get("name") or slug,
            "description": meta.get("description") or "",
            "instructions": "",
            "source": "external",
            "category": "external",
        }

    @staticmethod
    def _read_external_skill_file(slug: str) -> str | None:
        """读取外部技能全文（.compressed.md 优先于 .md）。"""
        if not EXTERNAL_SKILLS_DIR:
            return None
        compressed = EXTERNAL_SKILLS_DIR / slug / "SKILL.compressed.md"
        original = EXTERNAL_SKILLS_DIR / slug / "SKILL.md"
        if compressed.exists():
            try:
                return compressed.read_text(encoding="utf-8")
            except OSError:
                return None
        if original.exists():
            try:
                return original.read_text(encoding="utf-8")
            except OSError:
                return None
        return None

    # ── 内置 + 外部（同步，无 skills.sh）────────────────────────

    @staticmethod
    def _get_builtin_skill(slug: str) -> dict | None:
        """按 slug 取技能：外部优先，其次内置。不含 skills.sh。"""
        ext = SkillRegistryService._get_external_skill(slug)
        if ext is not None:
            return ext
        for s in BUILTIN_SKILLS:
            if s["slug"] == slug:
                return s
        return None

    @staticmethod
    def _list_builtin_skills(search: str | None = None) -> list[dict]:
        """列出 外部 + 内置 技能（不含 skills.sh）。"""
        external = SkillRegistryService._list_external_skills(search)
        all_skills = external + BUILTIN_SKILLS
        return _filter_skills(all_skills, search)

    # ── skills.sh（异步，best-effort）──────────────────────────
    # skills.sh 是 Open Agent Skills Ecosystem，技能格式 owner/repo/skill-name。
    # 搜索：抓取 leaderboard 页面 HTML，用正则提取技能链接和描述。
    # 详情：抓取技能详情页，提取 SKILL.md 段落内容。
    # 超时/失败静默降级到内置技能。

    # 简单内存缓存：避免同一 skill 重复抓取（slug → detail dict）
    _skills_sh_cache: dict[str, dict] = {}

    # Per-agent 搜索结果缓存：agent_id → [slug1, slug2, ...]
    # HR 调 list_available_skills 后，结果按序号存入此缓存。
    # hire_agent 的 skills 参数接受 "#1" 格式，从此缓存解析为真实 slug。
    _skill_search_cache: dict[str, list[str]] = {}

    async def _search_skills_sh(self, search: str | None = None) -> list[dict]:
        """搜索 skills.sh marketplace（8s 超时，失败返回 []）。

        抓取 leaderboard 页面，正则提取技能 slug（owner/repo/skill-name）。
        为每个候选并发抓取详情页 summary，让 HR 有描述可看。
        如果有 search 参数，在客户端做关键词过滤。
        """
        try:
            async with httpx.AsyncClient(timeout=SKILLS_SH_TIMEOUT) as client:
                resp = await client.get(SKILLS_SH_BASE_URL)
                if resp.status_code != 200:
                    return []
                html = resp.text

            # 正则提取技能链接：href="/owner/repo/skill-name"
            # 排除 /trending, /hot, /topic/ 等非技能路径
            pattern = r'href="/([^/]+/[^/]+/[^"]+)"'
            raw_matches = re.findall(pattern, html)

            # 去重 + 过滤非技能路径，按热度排序（leaderboard 本身按安装量降序）
            # 只取前 3 个匹配的，给 HR 选择空间但不过多占用上下文
            seen: set[str] = set()
            candidates: list[str] = []
            for slug in raw_matches:
                # 排除导航路径
                if slug in ("trending", "hot"):
                    continue
                if slug.startswith(("topic/", "site/")):
                    continue
                if slug in seen:
                    continue
                seen.add(slug)

                # 从 slug 提取 skill-name 作为显示名
                parts = slug.split("/")
                skill_name = parts[-1] if parts else slug

                # 如果有 search，做客户端过滤
                if search:
                    if search.lower() not in slug.lower() and search.lower() not in skill_name.lower():
                        continue

                candidates.append(slug)
                if len(candidates) >= 3:
                    break

            if not candidates:
                return []

            # 并发抓取每个候选的详情页 summary（让 HR 有描述可看）
            details = await asyncio.gather(
                *[self._fetch_skills_sh_detail(s) for s in candidates],
                return_exceptions=True,
            )

            skills: list[dict] = []
            for slug, detail in zip(candidates, details):
                parts = slug.split("/")
                skill_name = parts[-1] if parts else slug
                if isinstance(detail, dict) and detail:
                    summary = detail.get("summary") or detail.get("description") or ""
                    skills.append({
                        "slug": slug,
                        "summary": f"{skill_name}: {summary}" if summary else f"{skill_name} — from {parts[0]}/{parts[1]}",
                        "description": summary or "",
                        "displayName": skill_name,
                    })
                else:
                    skills.append({
                        "slug": slug,
                        "summary": f"{skill_name} — from {parts[0]}/{parts[1]}",
                        "description": "",
                        "displayName": skill_name,
                    })

            return skills
        except Exception as e:
            log.debug("skills_sh_search_failed", error=str(e))
            return []

    async def _fetch_skills_sh_detail(self, slug: str) -> dict | None:
        """取 skills.sh 单个技能详情（8s 超时，失败返回 None）。

        抓取 https://www.skills.sh/{slug} 页面，提取 SKILL.md 段落内容。
        结果缓存在内存中避免重复抓取。
        """
        # 检查缓存
        if slug in self._skills_sh_cache:
            return self._skills_sh_cache[slug]

        try:
            url = f"{SKILLS_SH_BASE_URL}/{slug}"
            async with httpx.AsyncClient(timeout=SKILLS_SH_TIMEOUT) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return None
                html = resp.text

            # 提取 SKILL.md 内容：页面中 "SKILL.md" 标题之后的内容
            # skills.sh 详情页将 SKILL.md 内容渲染在页面中，用正则提取
            # 策略：找 SKILL.md 之后的文本，直到下一个 section（Installs / Repository / Related）
            skill_md = self._extract_skill_md(html, slug)

            # 提取 summary（页面上 Summary 段落的描述）
            summary = self._extract_summary(html)

            result = {
                "slug": slug,
                "summary": summary or slug,
                "description": summary or "",
                "skill_md": skill_md or f"# {slug.split('/')[-1]}\n\nNo SKILL.md content available.",
            }

            # 缓存
            self._skills_sh_cache[slug] = result
            return result
        except Exception as e:
            log.debug("skills_sh_detail_failed", slug=slug, error=str(e))
            return None

    @staticmethod
    def _extract_skill_md(html: str, slug: str) -> str | None:
        """从 skills.sh 详情页 HTML 中提取 SKILL.md 内容。

        skills.sh 页面结构：SKILL.md 标题后跟着技能指令内容，
        然后是 Installs / Repository 等元数据 section。
        我们提取 SKILL.md 标题之后、下一个 section 之前的内容。
        """
        # 尝试多种分隔符模式
        # 1. 找 "SKILL.md" 标记后的内容
        md_start_patterns = [
            r"SKILL\.md\s*</(?:h\d|div|section)[^>]*>\s*<[^>]*>",  # 标题后跟内容
            r"SKILL\.md\s*\n",
        ]

        # 简化策略：用正则提取所有 <p> 和 <pre> 标签内容，组合成文本
        # skills.sh 将 SKILL.md 渲染为 HTML 段落
        # 找到 SKILL.md 标题位置
        md_idx = html.find("SKILL.md")
        if md_idx == -1:
            return None

        # 从 SKILL.md 标题后开始，截取到 "Installs" 或 "Repository" 或 "Related skills" 之前
        after_md = html[md_idx:]

        # 找结束位置
        end_markers = ["Installs", "Repository", "Related skills", "Security Audits"]
        end_idx = len(after_md)
        for marker in end_markers:
            mi = after_md.find(marker, 20)  # 跳过标题本身
            if mi != -1 and mi < end_idx:
                end_idx = mi

        content_html = after_md[:end_idx]

        # 从 HTML 提取纯文本：移除标签
        text = re.sub(r"<[^>]+>", "\n", content_html)
        # 清理多余空行
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        # 移除开头的 "SKILL.md"
        if text.startswith("SKILL.md"):
            text = text[8:].strip()

        return text if text else None

    @staticmethod
    def _extract_summary(html: str) -> str | None:
        """从 skills.sh 详情页 HTML 中提取 Summary 段落描述。"""
        idx = html.find("Summary")
        if idx == -1:
            return None
        after = html[idx:]
        # 找下一个 section 标记
        end_markers = ["Installation", "SKILL.md", "Installs", "Repository"]
        end_idx = len(after)
        for marker in end_markers:
            mi = after.find(marker, 10)
            if mi != -1 and mi < end_idx:
                end_idx = mi
        content = after[:end_idx]
        text = re.sub(r"<[^>]+>", "\n", content)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text.startswith("Summary"):
            text = text[7:].strip()
        return text if text else None

    # ── 公共 API：技能发现 ───────────────────────────────────

    async def list_available_skills(
        self, search: str | None = None, agent_id: str | None = None
    ) -> str:
        """列出所有可用技能（外部 + 内置 + skills.sh），返回带序号的格式化字符串。

        skills.sh 不可用时静默降级到 外部 + 内置。
        如果传入 agent_id，搜索结果会按序号存入 per-agent 缓存，
        之后 hire_agent 的 skills 参数可用 "#1" 格式引用。
        """
        builtin = self._list_builtin_skills(search)
        skills_sh = await self._search_skills_sh(search)

        if not builtin and not skills_sh:
            return (
                f'Available Skills'
                f'{f" (search: {chr(34)}{search}{chr(34)})" if search else ""}:\n\n'
                "No skills found. Try a different search term.\n\n"
                "To bind a skill, use `bind_skill` with the slug as skillName."
            )

        # 所有结果统一编号，存入 per-agent 缓存
        # 序号从缓存已有数量 +1 开始，确保多次搜索序号连续且唯一
        existing_cache = self._skill_search_cache.get(agent_id, []) if agent_id else []
        all_slugs: list[str] = []
        lines: list[str] = []
        idx = len(existing_cache)  # 续编号

        if builtin:
            lines.append("## Built-in Skills")
            for s in builtin:
                slug = s["slug"]
                if slug in existing_cache:
                    # 已在缓存中，用已有的序号
                    existing_idx = existing_cache.index(slug) + 1
                    lines.append(f"- **#{existing_idx}** {slug}: {s.get('description', '')} [built-in]")
                else:
                    idx += 1
                    all_slugs.append(slug)
                    lines.append(f"- **#{idx}** {slug}: {s.get('description', '')} [built-in]")

        if skills_sh:
            lines.append("")
            lines.append("## skills.sh Marketplace")
            for s in skills_sh:
                slug = s["slug"]
                desc = s.get("summary") or s.get("description") or "No description"
                if slug in existing_cache:
                    existing_idx = existing_cache.index(slug) + 1
                    lines.append(f"- **#{existing_idx}** {slug}: {desc}")
                else:
                    idx += 1
                    all_slugs.append(slug)
                    lines.append(f"- **#{idx}** {slug}: {desc}")

        # 存缓存：追加新发现的 slug（去重）
        if agent_id and all_slugs:
            existing_cache.extend(all_slugs)
            self._skill_search_cache[agent_id] = existing_cache

        q = chr(34)
        header = f'Available Skills{f" (search: {q}{search}{q})" if search else ""}:\n\n'
        return (
            header
            + "\n".join(lines)
            + "\n\nTo bind a skill in hire_agent, use \"#N\" (e.g. \"#1\") to reference by number, "
            "or use the full slug. Discipline skills from the matching table use full slug."
        )

    def resolve_skill_ref(self, agent_id: str, ref: str) -> str | None:
        """将 "#N" 格式的技能引用解析为真实 slug。

        如果 ref 不以 "#" 开头，直接返回 ref（视为完整 slug）。
        如果 ref 是 "#N" 但缓存中不存在或序号越界，返回 None。
        """
        ref = ref.strip()
        if not ref.startswith("#"):
            return ref  # 完整 slug，直接返回
        try:
            n = int(ref[1:])
        except ValueError:
            return None
        cache = self._skill_search_cache.get(agent_id, [])
        if 1 <= n <= len(cache):
            return cache[n - 1]
        return None

    async def get_skill_detail(self, slug: str) -> str:
        """取技能详情（外部 → 内置 → skills.sh），返回格式化字符串。"""
        slug = slug.strip()

        # 1. 外部技能
        ext = self._get_external_skill(slug)
        if ext is not None:
            content = self._read_external_skill_file(slug) or "Instructions not available."
            return (
                f"## Skill: {slug}\n\n"
                f"**Description:** {ext.get('description', '')}\n\n"
                "**Source:** agent-skills\n\n---\n\n"
                f"{content}"
            )

        # 2. 内置技能
        for s in BUILTIN_SKILLS:
            if s["slug"] == slug:
                return (
                    f"## Skill: {slug}\n\n"
                    f"**Description:** {s['description']}\n\n"
                    "**Source:** Built-in\n\n---\n\n"
                    f"{s['instructions']}"
                )

        # 3. skills.sh
        detail = await self._fetch_skills_sh_detail(slug)
        if detail is not None:
            desc = detail.get("summary") or detail.get("description") or "No description"
            return (
                f"## Skill: {slug}\n\n"
                f"**Description:** {desc}\n\n"
                "**Source:** skills.sh Marketplace\n\n---\n\n"
                f"{detail.get('skill_md') or 'No instructions available.'}"
            )

        return (
            f'Skill "{slug}" not found in built-in registry or skills.sh. '
            "Use `list_available_skills` to search for available skills."
        )

    async def read_skill(
        self, slug: str, bound_skills: list[str] | None = None
    ) -> str:
        """读取技能全文（外部 → 内置 → skills.sh）。

        agent 运行时按需调用以加载完整指令。bound_skills 非空且 slug 在其中时
        加 "(Bound skill) " 前缀。
        """
        slug = slug.strip()
        bound = bound_skills or []
        prefix = "(Bound skill) " if slug in bound else ""

        # 1. 外部技能文件
        content = self._read_external_skill_file(slug)
        if content is not None:
            return f"{prefix}{content}"

        # 2. 内置技能
        skill = self._get_builtin_skill(slug)
        if skill is not None and skill.get("instructions"):
            return f"{prefix}{skill['instructions']}"

        # 3. skills.sh
        detail = await self._fetch_skills_sh_detail(slug)
        if detail is not None:
            return f"{prefix}{detail.get('skill_md') or detail.get('summary') or 'No instructions available.'}"

        return (
            f'Skill "{slug}" not found. '
            "Use `list_available_skills` to discover available skills."
        )

    # ── 公共 API：技能绑定（Meta DB agents.bound_skills）──────

    async def get_bound_skills(self, agent_id: str) -> list[str]:
        """获取 agent 当前已绑定的技能 slug 列表。"""
        row = await project_db.query_one(
            agent_id, "SELECT bound_skills FROM agents WHERE id = ? LIMIT 1", [agent_id]
        )
        if row is None:
            return []
        return _parse_json_list(row["bound_skills"])

    async def bind_skill(self, agent_id: str, skill_name: str) -> dict:
        """绑定技能到 agent（修改 bound_skills，skills 字段不动）。

        返回 {"ok": bool, ...}：
        - agent 不存在 → ok=False, error
        - 技能不存在 → ok=False, error
        - 已绑定 → ok=False, error（去重）
        - 成功 → ok=True, skill
        """
        skill_name = skill_name.strip()

        agent = await meta_db.get_agent_by_id(agent_id)
        if agent is None:
            return {"ok": False, "error": f"Agent '{agent_id}' not found"}

        # 1. 检查技能存在（外部 → 内置 → skills.sh best-effort）
        if self._get_builtin_skill(skill_name) is None:
            detail = await self._fetch_skills_sh_detail(skill_name)
            if detail is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}

        # 2. 去重
        bound = await self.get_bound_skills(agent_id)
        if skill_name in bound:
            return {"ok": False, "error": f"Skill '{skill_name}' is already bound"}

        # 3. UPDATE bound_skills（skills 字段为不可变入职快照，不动）
        bound.append(skill_name)
        now_ms = int(time.time() * 1000)
        await project_db.execute(
            agent_id,
            "UPDATE agents SET bound_skills = ?, updated_at = ? WHERE id = ?",
            [json.dumps(bound), now_ms, agent_id],
        )
        log.info("skill_bound", agent_id=agent_id, skill=skill_name)
        return {"ok": True, "skill": skill_name}

    async def unbind_skill(self, agent_id: str, skill_name: str) -> dict:
        """解绑技能（校验存在性后从 bound_skills 移除）。"""
        skill_name = skill_name.strip()
        bound = await self.get_bound_skills(agent_id)
        if skill_name not in bound:
            return {"ok": False, "error": f"Skill '{skill_name}' is not bound"}
        bound.remove(skill_name)
        now_ms = int(time.time() * 1000)
        await project_db.execute(
            agent_id,
            "UPDATE agents SET bound_skills = ?, updated_at = ? WHERE id = ?",
            [json.dumps(bound), now_ms, agent_id],
        )
        log.info("skill_unbound", agent_id=agent_id, skill=skill_name)
        return {"ok": True, "skill": skill_name}

    # ── 公共 API：prompt 注入 ─────────────────────────────────

    @staticmethod
    def build_active_skills_section(bound_skills_json: str | None) -> str:
        """构建注入 system prompt 的 "Active Skills" 摘要段。

        仅显示摘要（节省上下文）；agent 通过 read_skill 按需加载全文。
        使用 _get_builtin_skill（外部 + 内置，同步，不查 skills.sh）—— 避免
        prompt 构建时阻塞网络。
        """
        slugs = _parse_json_list(bound_skills_json)
        if not slugs:
            return ""

        lines: list[str] = []
        for slug in slugs:
            skill = SkillRegistryService._get_builtin_skill(slug)
            if skill is not None:
                lines.append(f"- **{slug}**: {skill.get('description', '')}")
            else:
                lines.append(
                    f"- **{slug}**: (custom skill — use read_skill to load instructions)"
                )

        first_slug = slugs[0]
        return (
            "## Active Skills\n"
            "The following skills are bound to you. Each shows only a summary here.\n"
            f'When a task matches a skill, use `read_skill("{first_slug}")` '
            "(or the relevant slug) to load its full instructions before proceeding.\n\n"
            + "\n".join(lines)
            + "\n"
        )
