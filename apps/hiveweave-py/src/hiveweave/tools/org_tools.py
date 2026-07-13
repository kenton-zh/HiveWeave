"""Organization management + skill tools.

Migrated from executor.py ``_tool_*`` methods and inline dispatch code to
``@tool``-registered standalone functions.  Uses
:class:`~hiveweave.tools.pipeline.ToolContext` for service access
(``ctx.org``, ``ctx.skills``, ``ctx.templates``, ``ctx.roster``).

Tools:
    Org management: hire_agent, dismiss_agent, transfer_agent,
                    list_subordinates, view_org_chart, read_roster,
                    update_roster, list_agent_templates
    Skills:         list_available_skills, read_skill, bind_skill,
                    unbind_skill
"""

from __future__ import annotations

from typing import Any

import structlog

from pydantic import BaseModel, Field, ConfigDict, field_validator

from .base import tool
from .result import ToolResult
from .helpers import coerce_to_list, get_project_id, resolve_agent_id

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Section 1: Organization management tools
# ═══════════════════════════════════════════════════════════════════════


# ── hire_agent ───────────────────────────────────────────


class HireAgentParams(BaseModel):
    """Parameters for hire_agent tool."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(
        description="Agent codename (e.g. a Chinese flower name).",
    )
    role: str = Field(
        description=(
            "Chinese job title. Display label only -- does NOT determine "
            "permission. Use permissionType to set authority."
        ),
    )
    system_prompt: str | None = Field(
        default=None,
        alias="systemPrompt",
        description="2-4 sentence character narrative / backstory for the agent.",
        json_schema_extra={
            "aliases": ["systemPrompt", "system_prompt", "backstory"]
        },
    )
    permission_type: str | None = Field(
        default=None,
        alias="permissionType",
        description=(
            "MANDATORY. coordinator = manages subordinates; "
            "executor = hands-on work."
        ),
        json_schema_extra={"aliases": ["permissionType", "permission_type"]},
    )
    parent_agent_id: str | None = Field(
        default=None,
        alias="parentAgentId",
        description="Parent agent ID (default: CEO).",
        json_schema_extra={
            "aliases": ["parentAgentId", "parent_agent_id", "parentId", "parent_id", "parent"]
        },
    )
    skills: list[str] | None = Field(
        default=None,
        description=(
            'Skills to bind. Tool skills: use "#N" to reference skills '
            "from list_available_skills by number. Discipline skills: "
            "use full slug."
        ),
    )

    @field_validator("skills", mode="before")
    @classmethod
    def _coerce_skills(cls, v: Any) -> Any:
        return coerce_to_list(v)

    goal: str | None = Field(
        default=None,
        description="Agent's goal. Defaults to a role-based generic goal.",
    )
    template_id: str | None = Field(
        default=None,
        alias="templateId",
        description="Optional template ID to pre-fill role/goal/skills.",
        json_schema_extra={"aliases": ["templateId", "template_id"]},
    )


@tool(
    "hire_agent",
    "Creates and deploys a new agent with a specified name, role, goal, "
    "and backstory. Use it to bring new team members into the "
    "organization. Returns the new agent ID.",
    requires_workspace=False,
    security_level="standard",
)
async def hire_agent_tool(
    params: HireAgentParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Hire a new agent via OrgService.create_agent."""
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )
    if not ctx or not getattr(ctx, "skills", None):
        return ToolResult.err(
            "SkillRegistryService not available (ctx.skills is missing)"
        )

    name = params.name
    role = params.role
    backstory = params.system_prompt or ""
    skills = params.skills or []
    parent_id = params.parent_agent_id or ""
    goal = params.goal or ""
    template_id = params.template_id
    perm_type_arg = (params.permission_type or "").strip().lower()

    if not name:
        return ToolResult.err("hire_agent requires 'name' (agent codename)")
    if not role:
        return ToolResult.err("hire_agent requires 'role' (job title)")

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    # Resolve parent_id: LLM may pass short_id instead of UUID.
    all_agents = await ctx.org.list_agents(project_id)
    if parent_id:
        resolved_parent = None
        for a in all_agents:
            if a["id"] == parent_id:
                resolved_parent = parent_id
                break
        if not resolved_parent:
            for a in all_agents:
                if a.get("short_id", "").upper() == parent_id.upper():
                    resolved_parent = a["id"]
                    log.info(
                        "tool.hire_agent.parent_resolved",
                        short_id=parent_id,
                        uuid=a["id"][:12],
                    )
                    break
        if not resolved_parent:
            for a in all_agents:
                if a.get("name", "").lower() == parent_id.lower():
                    resolved_parent = a["id"]
                    break
        parent_id = resolved_parent or ""

    # If no parentId specified or resolved, default to CEO
    if not parent_id:
        ceo = await ctx.org.get_agent_by_role(project_id, "ceo")
        if ceo:
            parent_id = ceo["id"]

    # Determine permission_type
    if perm_type_arg in ("coordinator", "executor"):
        perm_type = perm_type_arg
    else:
        coordinator_roles = {
            "ceo", "hr", "qa", "cto", "architect", "manager", "pm",
        }
        perm_type = (
            "coordinator" if role.lower() in coordinator_roles else "executor"
        )
        log.warning(
            "tool.hire_agent.permission_type_inferred",
            role=role,
            inferred=perm_type,
            hint="HR should pass explicit permissionType; role-string "
            "inference is unreliable for non-English/unknown roles",
        )
    perm_mode = "readonly" if perm_type == "coordinator" else "readwrite"

    # Get default model_id
    existing_agents = await ctx.org.list_agents(project_id)
    model_id = None
    if existing_agents:
        for a in existing_agents:
            if a.get("model_id"):
                model_id = a["model_id"]
                break
    if not model_id:
        try:
            from hiveweave.services.model import ModelService

            ms = ModelService()
            active = await ms.list_active()
            if active:
                chosen = active[-1]
                model_id = chosen.get("model_id") or chosen.get("id")
                log.info(
                    "tool.hire_agent.model_from_service", model_id=model_id
                )
        except Exception as e:
            log.warning("tool.hire_agent.model_service_failed", error=str(e))
    if not model_id:
        model_id = "step-3.7-flash"

    # Get language from project
    from hiveweave.db import meta as meta_db

    project_row = await meta_db.query_one(
        "SELECT language FROM projects WHERE id = ?", [project_id]
    )
    language = project_row["language"] if project_row else "zh"

    # Validate skill slug validity
    if skills and isinstance(skills, list):
        resolved_skills: list[str] = []
        unresolved: list[str] = []
        for sk in skills:
            sk = sk.strip() if isinstance(sk, str) else str(sk).strip()
            resolved = ctx.skills.resolve_skill_ref(agent_id, sk)
            if resolved is None:
                unresolved.append(sk)
            else:
                resolved_skills.append(resolved)
        if unresolved:
            return ToolResult.err(
                f"Unresolved skill references: {unresolved}. "
                'Use list_available_skills first, then reference by "#N" '
                "or use full slug."
            )

        valid_skills: list[str] = []
        invalid_skills: list[str] = []
        for sk in resolved_skills:
            if ctx.skills._get_builtin_skill(sk) is not None:
                valid_skills.append(sk)
            else:
                detail = await ctx.skills._fetch_skills_sh_detail(sk)
                if detail is not None:
                    valid_skills.append(sk)
                else:
                    invalid_skills.append(sk)
        if invalid_skills:
            return ToolResult.err(
                f"Invalid skill slugs: {invalid_skills}. "
                "Use list_available_skills to find valid slugs. "
                "Raw tech names like 'React 18' are NOT valid slugs."
            )
        skills = valid_skills

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
    }

    try:
        new_agent = await ctx.org.create_agent(attrs)
        new_id = new_agent.get("id", "?")
        new_short = new_agent.get("short_id", "?")

        # Create isolated worktree for executor agents
        worktree_path = ""
        worktree_error = ""
        if perm_type == "executor":
            try:
                from hiveweave.services.git_worktree import GitWorktreeService

                gwt = GitWorktreeService()
                project_ws = await meta_db.get_project_workspace(project_id)
                if project_ws:
                    wt_result = await gwt.create(
                        workspace_path=project_ws,
                        short_id=new_short,
                        task_name=role,
                    )
                    if wt_result.get("success") and wt_result.get("path"):
                        worktree_path = wt_result["path"]
                        await ctx.org.update_agent(new_id, {
                            "workspace_path": worktree_path,
                        })
                        log.info(
                            "tool.hire_agent.worktree_created",
                            agent_id=new_id,
                            short_id=new_short,
                            worktree=worktree_path,
                        )
            except Exception as wt_err:
                log.warning(
                    "tool.hire_agent.worktree_failed",
                    agent_id=new_id,
                    error=str(wt_err),
                )
                worktree_error = str(wt_err)

        # Start the agent so it can process inbox messages
        try:
            from hiveweave.agents.supervisor import agent_manager
            from hiveweave.realtime.event_bus import create_agent_callbacks

            on_status, on_stream = create_agent_callbacks(new_id, project_id)
            started = await agent_manager.start_agent(
                new_id, project_id, new_agent,
                on_stream_event=on_stream, on_status_change=on_status,
            )
            log.info(
                "tool.hire_agent.started",
                agent_id=agent_id,
                new_agent_id=new_id,
                new_short_id=new_short,
                name=name,
                role=role,
                status=started.status.value if started else "none",
            )
        except Exception as start_err:
            log.warning(
                "tool.hire_agent.start_failed",
                new_agent_id=new_id,
                error=str(start_err),
            )

        log.info(
            "tool.hire_agent",
            agent_id=agent_id,
            new_agent_id=new_id,
            new_short_id=new_short,
            name=name,
            role=role,
        )

        # Push realtime event so frontend org tree updates immediately
        try:
            from hiveweave.realtime.event_bus import status_event_bus

            await status_event_bus.publish_agent_created(new_id, role, name)
            await status_event_bus.publish_org_changed()
        except Exception as evt_err:
            log.debug("hire_agent_event_push_failed", error=str(evt_err))

        if worktree_path:
            wt_info = f"  Worktree: {worktree_path}\n"
        elif worktree_error:
            wt_info = (
                f"  Worktree: creation failed ({worktree_error})\n"
                f"  Agent will use project root until next restart\n"
                f"  (worktree auto-recovers on backend restart)\n"
            )
        else:
            wt_info = "  Worktree: (shared project root)\n"

        return ToolResult.ok(
            f"Agent hired successfully.\n"
            f"  Name: {name}\n"
            f"  Role: {role}\n"
            f"  Short ID: {new_short}  <-- use this to reference this agent\n"
            f"  Internal ID: {new_id}\n"
            f"  Parent: {parent_id}\n"
            f"  Permission: {perm_type}\n"
            f"  Model: {model_id}\n"
            f"{wt_info}"
            f"  Skills: {skills}\n"
            f"  Backstory: {backstory[:100] if backstory else '(none)'}"
        )
    except Exception as e:
        return ToolResult.err(f"Failed to hire agent: {e}")


# ── dismiss_agent ────────────────────────────────────────


class DismissAgentParams(BaseModel):
    """Parameters for dismiss_agent tool."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(
        alias="agentId",
        description="Agent ID, short_id, or name to dismiss.",
        json_schema_extra={"aliases": ["agentId", "agent_id", "id", "target"]},
    )
    reason: str | None = Field(
        default=None,
        description="Optional reason for dismissal.",
        json_schema_extra={"aliases": ["feedback", "comment"]},
    )


@tool(
    "dismiss_agent",
    "Permanently remove/fire an agent from the organization. "
    "Cannot be undone.",
    requires_workspace=False,
    security_level="standard",
)
async def dismiss_agent_tool(
    params: DismissAgentParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Dismiss an agent from the organization."""
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    if not params.agent_id:
        return ToolResult.err("dismiss_agent requires 'agentId'")

    target_agent = await ctx.org.resolve_agent(params.agent_id)
    if not target_agent:
        return ToolResult.err(f"Agent not found: {params.agent_id}")

    result = await ctx.org.dismiss_agent(project_id, target_agent["id"])
    if result.get("success"):
        return ToolResult.ok(
            f"Agent {target_agent['name']} "
            f"({target_agent.get('short_id', '?')}) has been dismissed."
        )
    return ToolResult.err(result.get("message", "Unknown error"))


# ── transfer_agent ───────────────────────────────────────


class TransferAgentParams(BaseModel):
    """Parameters for transfer_agent tool."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(
        alias="agentId",
        description="Agent ID, short_id, or name to transfer.",
        json_schema_extra={"aliases": ["agentId", "agent_id", "id"]},
    )
    new_parent_id: str = Field(
        alias="newParentId",
        description="New parent/supervisor agent ID, short_id, or name.",
        json_schema_extra={
            "aliases": ["newParentId", "new_parent_id", "parentId", "parent_id", "target"]
        },
    )


@tool(
    "transfer_agent",
    "Reassign an agent to a new parent/supervisor in the hierarchy.",
    requires_workspace=False,
    security_level="standard",
)
async def transfer_agent_tool(
    params: TransferAgentParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Transfer an agent to a new parent."""
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    if not params.agent_id:
        return ToolResult.err("transfer_agent requires 'agentId'")

    target_agent = await ctx.org.resolve_agent(params.agent_id)
    if not target_agent:
        return ToolResult.err(f"Agent not found: {params.agent_id}")

    resolved_parent = None
    if params.new_parent_id:
        parent_agent = await ctx.org.resolve_agent(params.new_parent_id)
        if not parent_agent:
            return ToolResult.err(
                f"New parent agent not found: {params.new_parent_id}"
            )
        resolved_parent = parent_agent["id"]

    result = await ctx.org.transfer_agent(
        project_id, target_agent["id"], resolved_parent
    )
    if result is None:
        return ToolResult.err("Agent not found")
    if isinstance(result, dict) and result.get("success") is False:
        return ToolResult.err(result.get("message", "Unknown error"))

    return ToolResult.ok(
        f"Agent {target_agent['name']} transferred to new parent."
    )


# ── list_subordinates ────────────────────────────────────


class ListSubordinatesParams(BaseModel):
    """Parameters for list_subordinates tool."""

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "list_subordinates",
    "List your direct reports (subordinates).",
    requires_workspace=False,
    security_level="standard",
)
async def list_subordinates_tool(
    params: ListSubordinatesParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """List direct children of the calling agent."""
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    subs = await ctx.org.get_subordinates(agent_id)
    if not subs:
        return ToolResult.ok("You have no direct subordinates.")

    lines = []
    for s in subs:
        lines.append(
            f"- {s['name']} ({s.get('short_id', '?')}) | "
            f"role={s.get('role', '?')} | "
            f"status={s.get('status', '?')} | "
            f"goal={s.get('goal', '')[:80]}"
        )
    return ToolResult.ok(
        f"Direct subordinates ({len(subs)}):\n" + "\n".join(lines)
    )


# ── view_org_chart ───────────────────────────────────────


class ViewOrgChartParams(BaseModel):
    """Parameters for view_org_chart tool."""

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "view_org_chart",
    "View the full organizational hierarchy tree showing reporting lines.",
    requires_workspace=False,
    security_level="standard",
)
async def view_org_chart_tool(
    params: ViewOrgChartParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Show the full organization tree."""
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    tree = await ctx.org.get_full_tree(project_id)
    if not tree:
        return ToolResult.ok("Org chart is empty.")

    def format_node(node, indent=0):
        prefix = "  " * indent
        line = (
            f"{prefix}- {node['name']} "
            f"({node.get('short_id', '?')}) "
            f"role={node.get('role', '?')}"
        )
        if node.get("goal"):
            line += f" goal={node['goal'][:60]}"
        lines = [line]
        for child in (node.get("children") or []):
            lines.extend(format_node(child, indent + 1))
        return lines

    all_lines = []
    for root in tree:
        all_lines.extend(format_node(root))

    return ToolResult.ok(
        "=== Org Chart ===\n" + "\n".join(all_lines)
    )


# ── read_roster ──────────────────────────────────────────


class ReadRosterParams(BaseModel):
    """Parameters for read_roster tool."""

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "read_roster",
    "Read the team roster listing all agents and their "
    "roles/departments.",
    requires_workspace=False,
    security_level="standard",
)
async def read_roster_tool(
    params: ReadRosterParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Read the team roster."""
    if not ctx or not getattr(ctx, "roster", None):
        return ToolResult.err(
            "RosterService not available (ctx.roster is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    roster_text = await ctx.roster.get_roster(project_id)
    return ToolResult.ok(roster_text)


# ── update_roster ────────────────────────────────────────


class UpdateRosterParams(BaseModel):
    """Parameters for update_roster tool."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(
        alias="agentId",
        description="Agent ID, short_id, or name to update roster for.",
        json_schema_extra={"aliases": ["agentId", "agent_id", "target", "id", "roster"]},
    )
    position: str | None = Field(
        default=None,
        description="Position/title in the roster.",
    )
    department: str | None = Field(
        default=None,
        description="Department name.",
    )
    responsibilities: str | None = Field(
        default=None,
        description="Responsibilities description.",
    )
    status: str | None = Field(
        default=None,
        description="Employment status (e.g. active, on_leave).",
    )
    hire_date: str | None = Field(
        default=None,
        alias="hireDate",
        description="Hire date string.",
        json_schema_extra={"aliases": ["hireDate", "hire_date"]},
    )


@tool(
    "update_roster",
    "Update an agent's position, department, responsibilities, or "
    "status in the roster.",
    requires_workspace=False,
    security_level="standard",
)
async def update_roster_tool(
    params: UpdateRosterParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Update an agent's roster entry."""
    if not ctx or not getattr(ctx, "roster", None):
        return ToolResult.err(
            "RosterService not available (ctx.roster is missing)"
        )
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    if not params.agent_id:
        return ToolResult.err("update_roster requires 'agentId'")

    target_agent = await ctx.org.resolve_agent(params.agent_id)
    if not target_agent:
        return ToolResult.err(f"Agent not found: {params.agent_id}")

    roster_attrs: dict[str, Any] = {}
    if params.position is not None:
        roster_attrs["position"] = params.position
    if params.department is not None:
        roster_attrs["department"] = params.department
    if params.responsibilities is not None:
        roster_attrs["responsibilities"] = params.responsibilities
    if params.status is not None:
        roster_attrs["status"] = params.status
    if params.hire_date is not None:
        roster_attrs["hire_date"] = params.hire_date

    result = await ctx.roster.update_roster(
        project_id, target_agent["id"], roster_attrs
    )
    return ToolResult.ok(result)


# ── list_agent_templates ─────────────────────────────────


class ListAgentTemplatesParams(BaseModel):
    """Parameters for list_agent_templates tool."""

    model_config = ConfigDict(populate_by_name=True)

    search: str | None = Field(
        default=None,
        description="Optional keyword to filter templates by name or description.",
    )
    division: str | None = Field(
        default=None,
        description="Optional division filter.",
    )


@tool(
    "list_agent_templates",
    "List available agent templates for hiring.",
    requires_workspace=False,
    security_level="standard",
)
async def list_agent_templates_tool(
    params: ListAgentTemplatesParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """List available agent templates (HR only)."""
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )
    if not ctx or not getattr(ctx, "templates", None):
        return ToolResult.err(
            "TemplateService not available (ctx.templates is missing)"
        )

    # Runtime role check -- only HR can browse templates
    caller = await ctx.org.get_agent(agent_id)
    if not caller or caller.get("role", "").lower() != "hr":
        return ToolResult.err(
            "Permission denied: only HR can browse agent templates"
        )

    opts: dict[str, Any] = {}
    if params.search:
        opts["search"] = params.search
    if params.division:
        opts["division"] = params.division

    templates = await ctx.templates.list_all(opts)
    if not templates:
        return ToolResult.ok(
            "No templates found. Try a different search keyword or division."
        )

    lines = []
    for t in templates:
        lines.append(
            f"- {t['name']} (role: {t.get('role', '?')}) -- "
            f"ID: {t['id']} -- {t.get('description', 'no description')}"
        )
    output = (
        f"Available agent templates ({len(templates)} found):\n"
        + "\n".join(lines)
        + "\n\nPass templateId in hire_agent to pre-fill "
        "role/goal/skills."
    )
    return ToolResult.ok(output)


# ═══════════════════════════════════════════════════════════════════════
# Section 2: Skill tools
# ═══════════════════════════════════════════════════════════════════════


# ── list_available_skills ────────────────────────────────


class ListAvailableSkillsParams(BaseModel):
    """Parameters for list_available_skills tool."""

    model_config = ConfigDict(populate_by_name=True)

    search: str | None = Field(
        default=None,
        description=(
            "Optional keyword to filter skills (e.g. 'react', 'testing', "
            "'planning'). Case-insensitive."
        ),
    )


@tool(
    "list_available_skills",
    "Lists all skills available in the marketplace (built-in + external + "
    'skills.sh). Pass \'search\' to filter by keyword. Returns numbered '
    'skills (e.g. #1, #2). Use "#N" in hire_agent\'s skills parameter to '
    "reference by number, or use full slug.",
    requires_workspace=False,
    security_level="standard",
)
async def list_available_skills_tool(
    params: ListAvailableSkillsParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """List all available skills."""
    if not ctx or not getattr(ctx, "skills", None):
        return ToolResult.err(
            "SkillRegistryService not available (ctx.skills is missing)"
        )

    result = await ctx.skills.list_available_skills(
        params.search, agent_id=agent_id
    )
    return ToolResult.ok(result)


# ── read_skill ───────────────────────────────────────────


class ReadSkillParams(BaseModel):
    """Parameters for read_skill tool."""

    model_config = ConfigDict(populate_by_name=True)

    skill_slug: str = Field(
        alias="skillSlug",
        description="Skill name or slug to read.",
        json_schema_extra={
            "aliases": ["skillSlug", "skill_slug", "slug", "skillName", "skill", "name", "id"]
        },
    )


@tool(
    "read_skill",
    "Reads the documentation and definition of a specific skill by name "
    "or slug. Use it to understand what a skill does, how to use it, and "
    "how to invoke it.",
    requires_workspace=False,
    security_level="standard",
)
async def read_skill_tool(
    params: ReadSkillParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Read skill documentation by slug."""
    if not ctx or not getattr(ctx, "skills", None):
        return ToolResult.err(
            "SkillRegistryService not available (ctx.skills is missing)"
        )

    slug = params.skill_slug
    if not slug:
        return ToolResult.err("read_skill requires 'skillSlug' (skill name)")

    bound = await ctx.skills.get_bound_skills(agent_id)
    result = await ctx.skills.read_skill(slug, bound)
    return ToolResult.ok(result)


# ── bind_skill ───────────────────────────────────────────


class BindSkillParams(BaseModel):
    """Parameters for bind_skill tool."""

    model_config = ConfigDict(populate_by_name=True)

    target: str = Field(
        description="Agent to bind the skill to (name, short_id, or UUID).",
        json_schema_extra={
            "aliases": ["target", "agentId", "agent_id", "id"]
        },
    )
    skill_name: str = Field(
        alias="skillName",
        description="Skill slug to bind.",
        json_schema_extra={
            "aliases": ["skillName", "skill_name", "skill", "slug", "skillSlug"]
        },
    )


@tool(
    "bind_skill",
    "Attach a skill to an agent, granting them that capability.",
    requires_workspace=False,
    security_level="standard",
)
async def bind_skill_tool(
    params: BindSkillParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Bind a skill to an agent."""
    if not ctx or not getattr(ctx, "skills", None):
        return ToolResult.err(
            "SkillRegistryService not available (ctx.skills is missing)"
        )
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    skill_name = params.skill_name
    if not skill_name:
        return ToolResult.err("bind_skill requires 'skillName' (skill slug)")

    target_id = params.target or agent_id
    if target_id != agent_id:
        target_agent = await ctx.org.resolve_agent(target_id)
        if not target_agent:
            return ToolResult.err(f"Agent not found: {target_id}")
        target_id = target_agent["id"]

    result = await ctx.skills.bind_skill(target_id, skill_name)
    if result.get("ok"):
        return ToolResult.ok(
            f"Skill '{skill_name}' bound to agent {target_id}."
        )
    return ToolResult.err(result.get("error", "Unknown error"))


# ── unbind_skill ─────────────────────────────────────────


class UnbindSkillParams(BaseModel):
    """Parameters for unbind_skill tool."""

    model_config = ConfigDict(populate_by_name=True)

    target: str = Field(
        description="Agent to unbind the skill from (name, short_id, or UUID).",
        json_schema_extra={
            "aliases": ["target", "agentId", "agent_id", "id"]
        },
    )
    skill_name: str = Field(
        alias="skillName",
        description="Skill slug to unbind.",
        json_schema_extra={
            "aliases": ["skillName", "skill_name", "skill", "slug", "skillSlug"]
        },
    )


@tool(
    "unbind_skill",
    "Remove a skill from an agent.",
    requires_workspace=False,
    security_level="standard",
)
async def unbind_skill_tool(
    params: UnbindSkillParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Unbind a skill from an agent."""
    if not ctx or not getattr(ctx, "skills", None):
        return ToolResult.err(
            "SkillRegistryService not available (ctx.skills is missing)"
        )
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    skill_name = params.skill_name
    if not skill_name:
        return ToolResult.err("unbind_skill requires 'skillName' (skill slug)")

    target_id = params.target or agent_id
    if target_id != agent_id:
        target_agent = await ctx.org.resolve_agent(target_id)
        if not target_agent:
            return ToolResult.err(f"Agent not found: {target_id}")
        target_id = target_agent["id"]

    result = await ctx.skills.unbind_skill(target_id, skill_name)
    if result.get("ok"):
        return ToolResult.ok(
            f"Skill '{skill_name}' unbound from agent {target_id}."
        )
    return ToolResult.err(result.get("error", "Unknown error"))
