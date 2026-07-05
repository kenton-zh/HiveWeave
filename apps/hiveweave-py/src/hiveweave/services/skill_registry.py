"""Skill registry service — SKILL.md-style instruction binding.

契约 10: MCP 与技能（技能部分）
- 技能来源三层：外部文件系统（agent-skills）→ 内置注册表 → ClawHub 远程市场
- 技能定义包含：slug / name / description / instructions / category
- bind_skill / unbind_skill 修改 agents.bound_skills（Meta DB）
- skills 字段为不可变入职快照（hire 时写入，bind/unbind 不动）
- bound_skills 为运行时可变集合（初始化为 skills 副本）
- build_active_skills_section 注入 system prompt 摘要段（仅摘要，read_skill 按需加载全文）
- ClawHub best-effort（5s 超时，失败静默降级到 外部 + 内置）

权限门禁（resolve_and_update_agent，由 tool_executor 层强制）：
- 自身 / 直属下属 / CEO+HR 可操作项目内任意 agent；跨项目拒绝
- 本服务只做数据层操作，权限校验由上游 tool_executor 负责

移植自 Elixir skill_registry.ex + TS clawhub-service.ts。
"""

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from hiveweave.db import meta as meta_db

log = structlog.get_logger(__name__)

# ── 常量 ────────────────────────────────────────────────────
# 外部技能目录（best-effort；不存在则返回空）
EXTERNAL_SKILLS_DIR = Path("d:/PC_AI/Project/agent-skills/skills")

CLAWHUB_BASE_URL = "https://clawhub.ai/api/v1/skills"
CLAWHUB_TIMEOUT = 5.0  # 契约 10: ClawHub 5s 超时，失败静默降级


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
            "1. Use infrastructure-as-code — Dockerfile, docker-compose, IaC.\n"
            "2. Separate build and run stages in Dockerfiles.\n"
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

    三层来源优先级：外部文件系统 → 内置注册表 → ClawHub 远程市场。
    """

    # ── 外部技能（文件系统，同步）──────────────────────────────

    @staticmethod
    def _list_external_skills(search: str | None = None) -> list[dict]:
        """扫描外部技能目录，解析每个子目录的 SKILL.md frontmatter。"""
        if not EXTERNAL_SKILLS_DIR.exists():
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

    # ── 内置 + 外部（同步，无 ClawHub）────────────────────────

    @staticmethod
    def _get_builtin_skill(slug: str) -> dict | None:
        """按 slug 取技能：外部优先，其次内置。不含 ClawHub。"""
        ext = SkillRegistryService._get_external_skill(slug)
        if ext is not None:
            return ext
        for s in BUILTIN_SKILLS:
            if s["slug"] == slug:
                return s
        return None

    @staticmethod
    def _list_builtin_skills(search: str | None = None) -> list[dict]:
        """列出 外部 + 内置 技能（不含 ClawHub）。"""
        external = SkillRegistryService._list_external_skills(search)
        all_skills = external + BUILTIN_SKILLS
        return _filter_skills(all_skills, search)

    # ── ClawHub（异步，best-effort）──────────────────────────

    async def _search_clawhub(self, search: str | None = None) -> list[dict]:
        """搜索 ClawHub 市场（5s 超时，失败返回 []）。"""
        params: dict[str, Any] = {"limit": 10}
        if search:
            params["search"] = search
        try:
            async with httpx.AsyncClient(timeout=CLAWHUB_TIMEOUT) as client:
                resp = await client.get(CLAWHUB_BASE_URL, params=params)
                if resp.status_code != 200:
                    return []
                body = resp.json()
                if not isinstance(body, dict):
                    return []
                items = body.get("items", []) or []
                return [
                    {
                        "slug": s.get("slug"),
                        "summary": s.get("summary"),
                        "description": s.get("description"),
                        "displayName": s.get("displayName"),
                    }
                    for s in items
                    if isinstance(s, dict)
                ]
        except Exception as e:
            log.debug("clawhub_search_failed", error=str(e))
            return []

    async def _fetch_clawhub_detail(self, slug: str) -> dict | None:
        """取 ClawHub 单个技能详情（5s 超时，失败返回 None）。"""
        try:
            async with httpx.AsyncClient(timeout=CLAWHUB_TIMEOUT) as client:
                resp = await client.get(f"{CLAWHUB_BASE_URL}/{slug}")
                if resp.status_code != 200:
                    return None
                body = resp.json()
                if not isinstance(body, dict):
                    return None
                return {
                    "slug": body.get("slug"),
                    "summary": body.get("summary"),
                    "description": body.get("description"),
                    "skill_md": body.get("skillMd") or body.get("skill_md"),
                }
        except Exception as e:
            log.debug("clawhub_detail_failed", slug=slug, error=str(e))
            return None

    # ── 公共 API：技能发现 ───────────────────────────────────

    async def list_available_skills(self, search: str | None = None) -> str:
        """列出所有可用技能（外部 + 内置 + ClawHub），返回格式化字符串。

        ClawHub 不可用时静默降级到 外部 + 内置。
        """
        builtin = self._list_builtin_skills(search)
        clawhub_skills = await self._search_clawhub(search)

        if not builtin and not clawhub_skills:
            return (
                f'Available Skills{f" (search: \"{search}\")" if search else ""}:\n\n'
                "No skills found. Try a different search term.\n\n"
                "To bind a skill, use `bind_skill` with the slug as skillName."
            )

        lines: list[str] = []
        if builtin:
            lines.append("## Built-in Skills")
            for s in builtin:
                lines.append(f"- **{s['slug']}**: {s.get('description', '')} [built-in]")

        if clawhub_skills:
            lines.append("")
            lines.append("## ClawHub Marketplace")
            for s in clawhub_skills:
                desc = s.get("summary") or s.get("description") or "No description"
                lines.append(f"- **{s.get('slug')}**: {desc}")

        header = f'Available Skills{f" (search: \"{search}\")" if search else ""}:\n\n'
        return (
            header
            + "\n".join(lines)
            + "\n\nTo bind a skill, use `bind_skill` with the slug as skillName."
        )

    async def get_skill_detail(self, slug: str) -> str:
        """取技能详情（外部 → 内置 → ClawHub），返回格式化字符串。"""
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

        # 3. ClawHub
        detail = await self._fetch_clawhub_detail(slug)
        if detail is not None:
            desc = detail.get("summary") or detail.get("description") or "No description"
            return (
                f"## Skill: {slug}\n\n"
                f"**Description:** {desc}\n\n"
                "**Source:** ClawHub Marketplace\n\n---\n\n"
                f"{detail.get('skill_md') or 'No instructions available.'}"
            )

        return (
            f'Skill "{slug}" not found in built-in registry or ClawHub. '
            "Use `list_available_skills` to search for available skills."
        )

    async def read_skill(
        self, slug: str, bound_skills: list[str] | None = None
    ) -> str:
        """读取技能全文（外部 → 内置 → ClawHub）。

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

        # 3. ClawHub
        detail = await self._fetch_clawhub_detail(slug)
        if detail is not None:
            return f"{prefix}{detail.get('skill_md') or detail.get('summary') or 'No instructions available.'}"

        return (
            f'Skill "{slug}" not found. '
            "Use `list_available_skills` to discover available skills."
        )

    # ── 公共 API：技能绑定（Meta DB agents.bound_skills）──────

    async def get_bound_skills(self, agent_id: str) -> list[str]:
        """获取 agent 当前已绑定的技能 slug 列表。"""
        row = await meta_db.query_one(
            "SELECT bound_skills FROM agents WHERE id = ? LIMIT 1", [agent_id]
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

        # 1. 检查技能存在（外部 → 内置 → ClawHub best-effort）
        if self._get_builtin_skill(skill_name) is None:
            detail = await self._fetch_clawhub_detail(skill_name)
            if detail is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}

        # 2. 去重
        bound = await self.get_bound_skills(agent_id)
        if skill_name in bound:
            return {"ok": False, "error": f"Skill '{skill_name}' is already bound"}

        # 3. UPDATE bound_skills（skills 字段为不可变入职快照，不动）
        bound.append(skill_name)
        now_ms = int(time.time() * 1000)
        await meta_db.execute(
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
        await meta_db.execute(
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
        使用 _get_builtin_skill（外部 + 内置，同步，不查 ClawHub）—— 避免
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
