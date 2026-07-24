"""Organization service — agent CRUD and tree traversal (contract 04).

契约 04: 多 Agent 编制 (org 部分)
- agent 数据存在 per-project DB 的 agents 表 (按项目物理隔离)
- AgentRouter (内存) 存路由信息 (agent_id → project_id + 展示字段)
- CRUD: create_agent / get_agent / delete_agent
- 树遍历: get_subordinates / get_superior / get_full_tree
- resolve_agent: short_id (A007) → UUID exact → UUID prefix
- transfer_agent: 带环检测 (新父不能是后代)
- dismiss_agent: 软删除 (status=archived), 检查无下属
- generate_short_id: A001, A002, ... (TS 模式: 取 max+1)
- JSON 列 (skills / allowed_tools / ...) 自动编解码
"""

import asyncio
import json
import re
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.services.agent_router import AgentRoute, agent_router

log = structlog.get_logger(__name__)


def _fix_mojibake(s: str) -> str:
    """尝试修复双重编码的 UTF-8 字符串。

    BUG-009/012 修复：如果 str 看起来像 UTF-8 bytes 被当 latin-1/cp1252
    解码的结果（mojibake），尝试 encode + decode('utf-8') 还原。

    只在修复后包含 CJK 字符时才采用，避免误修合法的 latin-1 文本。
    """
    if not isinstance(s, str) or len(s) < 2:
        return s
    high_chars = sum(1 for c in s if ord(c) >= 0x80)
    if high_chars < 2 or high_chars < len(s) * 0.3:
        return s
    # 尝试 latin-1 和 cp1252(Windows-1252) 两种编码
    for encoding in ("latin-1", "cp1252"):
        try:
            fixed = s.encode(encoding).decode("utf-8")
            if any("\u4e00" <= c <= "\u9fff" for c in fixed):
                return fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return s


# Columns stored as JSON arrays in the agents table
_JSON_COLS = frozenset({
    "skills",
    "allowed_tools",
    "denied_tools",
    "ask_tools",
    "mcp_servers",
    "bound_skills",
})

# Scalar columns allowed in INSERT/UPDATE (excludes id/short_id/created_at
# which are handled explicitly)
_SCALAR_COLS = (
    "project_id",
    "name",
    "role",
    "parent_id",
    "module_id",
    "status",
    "goal",
    "backstory",
    "model_id",
    "permission_type",
    "permission_mode",
    "reasoning_effort",
    "workspace_path",
    "worktree_error",
    "language",
    "last_active_at",
)

_SHORT_ID_RE = re.compile(r"^[aA]\d{1,4}$")
_SHORT_ID_NUM_RE = re.compile(r"^A(\d+)$")

# 权限类型的可读标签 — 事故修复: LLM 经常误读 [coordinator]/[executor],
# 把代码任务派给只读的协调者。标签保留原始类型便于程序解析。
PERMISSION_TYPE_LABELS: dict[str, str] = {
    "coordinator": "只读协调·coordinator",
    "executor": "可写代码·executor",
}

# 通讯录/花名册共用的单行图例
PERMISSION_LABEL_LEGEND: str = (
    "图例: [只读协调] = 只能规划/审批/派单，不能修改代码文件; "
    "[可写代码] = 可在自己的 worktree 修改代码"
)


class OrgService:
    """Organization CRUD + tree traversal — agents live in per-project DB.

    AgentRouter (in-memory) provides routing (agent_id → project_id + display
    fields). Full agent data (name, role, skills, etc.) is in per-project
    DB.agents.
    """

    # ── Org chart dirty-flag sync (仿照 goals_dirty) ──────────
    # 组织架构变更时 bump version，agent 下次对话注入一次精简 org chart 后清除标记。
    # 避免 agent 不知道同事花名、误用 role 找人（如 send_message(recipients=["HR"])）。

    _VERSION_UNSET: int = -1

    # Class-level lock — protects short_id generation + agent creation
    # across concurrent hire_agent calls (HR can hire multiple agents in
    # one tool round, all calling create_agent in parallel).
    _create_lock: asyncio.Lock = asyncio.Lock()

    def __init__(self) -> None:
        self._org_version: dict[str, int] = {}
        self._agent_org_version: dict[tuple[str, str], int] = {}

    # ── CREATE ───────────────────────────────────────────────

    async def create_agent(self, attrs: dict, *, bootstrap: bool = False) -> dict:
        """Create a new agent. Returns the created agent dict.

        Auto-generates ``id`` (UUID) and ``short_id`` (A001-style) if not
        provided. JSON list columns are encoded automatically.

        Writes to per-project DB (agents table) + registers in AgentRouter
        (in-memory) for routing.

        bound_skills is initialized as a copy of skills — these are the
        纪律技能 (discipline skills) that get injected into the agent's
        system prompt at runtime.

        ``bootstrap=True`` skips hire invariants (CEO/HR seed only).
        """
        from hiveweave.services.org_invariants import validate_hire

        agent_id = attrs.get("id") or str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        project_id = attrs.get("project_id", "")

        # Initialize bound_skills from skills if not explicitly set
        if "bound_skills" not in attrs and "skills" in attrs:
            attrs = {**attrs, "bound_skills": attrs["skills"]}

        # Hard org gates (tools + REST share this path)
        if not bootstrap and project_id:
            existing = await self.list_agents(project_id)
            err = validate_hire(
                agents=existing,
                name=str(attrs.get("name") or ""),
                role=str(attrs.get("role") or ""),
                permission_type=str(attrs.get("permission_type") or "executor"),
                parent_id=str(attrs.get("parent_id") or ""),
                bootstrap=False,
            )
            if err:
                raise ValueError(err)

        # DESIGN-3: same-role dismiss→hire cooldown
        role = str(attrs.get("role") or "")
        if not bootstrap and project_id and role:
            try:
                from hiveweave.services.org_guardrails import (
                    check_same_role_rehire,
                )

                rehire_err = await check_same_role_rehire(project_id, role)
                if rehire_err:
                    raise ValueError(rehire_err)
            except ValueError:
                raise
            except Exception as e:
                log.warning(
                    "org.same_role_rehire_check_failed",
                    project_id=project_id,
                    role=role,
                    error=str(e),
                )

        cols: list[str] = ["id", "short_id", "created_at", "updated_at"]
        vals: list = [agent_id, None, now_ms, now_ms]  # short_id filled under lock

        for col in _SCALAR_COLS:
            if col in attrs:
                cols.append(col)
                vals.append(attrs[col])

        for col in _JSON_COLS:
            if col in attrs:
                cols.append(col)
                v = attrs[col]
                vals.append(json.dumps(v) if isinstance(v, list) else v)

        placeholders = ", ".join(["?"] * len(cols))
        col_list = ", ".join(cols)

        # Lock: protect short_id generation → DB insert → agent_router registration
        # Prevents race condition when HR hires multiple agents in one tool round
        # (parallel create_agent calls would all read the same max short_id).
        async with self._create_lock:
            short_id = attrs.get("short_id") or await self.generate_short_id()
            vals[1] = short_id  # Fill short_id into vals list

            # Write to per-project DB
            conn = await project_db.get_project_db_by_project_id(project_id)
            await conn.execute(
                f"INSERT INTO agents ({col_list}) VALUES ({placeholders})", vals
            )
            await conn.commit()

            # Register in agent_router for routing
            workspace_path = attrs.get("workspace_path", "")
            if not workspace_path:
                workspace_path = await meta_db.get_project_workspace(project_id) or ""
            agent_router.register(AgentRoute(
                agent_id=agent_id,
                project_id=project_id,
                workspace_path=workspace_path,
                short_id=short_id,
                name=attrs.get("name", ""),
                role=attrs.get("role", ""),
                status=attrs.get("status", "active"),
            ))

        log.info("org.create_agent", agent_id=agent_id, short_id=short_id,
                 role=attrs.get("role"), name=attrs.get("name"))

        self.touch_org_version(project_id)

        agent = await self.get_agent(agent_id)
        return agent or {"id": agent_id, "short_id": short_id}

    # ── READ ─────────────────────────────────────────────────

    async def get_agent(self, agent_id: str) -> dict | None:
        """Get agent by full UUID.

        Routes through meta_db.get_agent_by_id() which transparently
        resolves agent_id → AgentRouter → per-project DB.
        """
        agent = await meta_db.get_agent_by_id(agent_id)
        return self._row(agent) if agent else None

    async def get_agent_by_role(self, project_id: str, role: str) -> dict | None:
        """Get first active agent by role within a project.

        project 不存在（ProjectDbError）时返回 None。
        """
        try:
            conn = await project_db.get_project_db_by_project_id(project_id)
        except project_db.ProjectDbError:
            return None
        cursor = await conn.execute(
            "SELECT * FROM agents WHERE project_id = ? AND role = ? "
            "AND status != 'archived' LIMIT 1",
            [project_id, role],
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row(row) if row else None

    async def list_agents(self, project_id: str | None = None) -> list[dict]:
        """List all agents for a project (or all if project_id is None).

        Ordered by ``created_at`` ASC (oldest first).
        """
        if project_id is None:
            # List across all projects — use agent_router to find all agent_ids,
            # then load each from its per-project DB
            routes = agent_router.list_active_routes()
            result: list[dict] = []
            for route in routes:
                agent = await self.get_agent(route.agent_id)
                if agent:
                    result.append(agent)
            # Sort by created_at
            result.sort(key=lambda a: a.get("created_at", 0))
            return result

        try:
            conn = await project_db.get_project_db_by_project_id(project_id)
        except project_db.ProjectDbError:
            return []
        cursor = await conn.execute(
            "SELECT * FROM agents WHERE project_id = ? "
            "ORDER BY created_at ASC",
            [project_id],
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [d for r in rows if (d := self._row(r)) is not None]

    async def resolve_agent(self, agent_id_or_short_id: str) -> dict | None:
        """Resolve by short_id (A007), full UUID, or UUID prefix.

        Priority: shortId exact → UUID exact → UUID prefix (6-35 chars).
        For 花名 / display-name resolution within a project, use
        ``resolve_agent_ref(project_id, ref)``.
        """
        inp = agent_id_or_short_id.strip()

        # 1. short_id exact match (case-insensitive)
        if _SHORT_ID_RE.match(inp):
            norm = "A" + inp[1:].upper()
            route = agent_router.find_by_short_id(norm)
            if route:
                return await self.get_agent(route.agent_id)

        # 2. UUID exact match
        agent = await self.get_agent(inp)
        if agent:
            return agent

        # 3. UUID prefix match (ambiguous → first match)
        if 6 <= len(inp) < 36:
            matches = agent_router.find_by_uuid_prefix(inp, limit=2)
            if matches:
                return await self.get_agent(matches[0].agent_id)

        return None

    async def resolve_agent_ref(
        self, project_id: str, ref: str
    ) -> dict | None:
        """Resolve agent within a project: id → short_id → 花名 exact → unique prefix.

        Agents learn 花名 from org chart / check_agent_status; tools must
        accept the same refs (TEST11 evening review P2-3).
        """
        inp = (ref or "").strip()
        if not inp or not project_id:
            return None

        # 1. id / short_id / uuid prefix (scoped to project when found)
        agent = await self.resolve_agent(inp)
        if agent and agent.get("project_id") == project_id:
            return agent

        all_agents = await self.list_agents(project_id)
        active = [
            a for a in all_agents
            if (a.get("status") or "").lower() != "archived"
        ]

        # 2. 花名 / name exact (case-insensitive)
        name_hits = [
            a for a in active
            if (a.get("name") or "").strip().lower() == inp.lower()
        ]
        if len(name_hits) == 1:
            return name_hits[0]
        if len(name_hits) > 1:
            return None

        # 3. Unique name prefix (≥2 chars to avoid noise)
        if len(inp) >= 2:
            prefix_hits = [
                a for a in active
                if (a.get("name") or "").strip().lower().startswith(inp.lower())
            ]
            if len(prefix_hits) == 1:
                return prefix_hits[0]

        # 4. Role exact (legacy helpers.resolve_agent_id compat)
        role_hits = [
            a for a in active
            if (a.get("role") or "").strip().lower() == inp.lower()
        ]
        if len(role_hits) == 1:
            return role_hits[0]

        return None

    # ── UPDATE ───────────────────────────────────────────────

    async def update_agent(self, agent_id: str, attrs: dict) -> dict | None:
        """Update agent fields. Returns updated agent or None if not found.

        JSON list columns are encoded automatically. ``id`` and ``created_at``
        are immutable. Writes to per-project DB + syncs AgentRouter for
        display fields (name, role, status).
        """
        agent = await self.get_agent(agent_id)
        if not agent:
            return None

        sets: list[str] = []
        vals: list = []
        for k, v in attrs.items():
            if k in ("id", "created_at", "short_id"):
                continue
            if k in _JSON_COLS and isinstance(v, list):
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            vals.append(v)

        if not sets:
            return agent

        sets.append("updated_at = ?")
        vals.append(int(time.time() * 1000))
        vals.append(agent_id)

        await project_db.execute(
            agent_id,
            f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", vals
        )
        log.info("org.update_agent", agent_id=agent_id,
                 fields=list(attrs.keys()))

        # Sync agent_router for display fields
        router_kwargs: dict = {}
        for k in ("name", "role", "status"):
            if k in attrs:
                router_kwargs[k] = attrs[k]
        if router_kwargs:
            agent_router.update(agent_id, **router_kwargs)

        # org chart 变更时触发 dirty（仅 name/role 变更影响通讯录）
        if any(k in attrs for k in ("name", "role", "parent_id")):
            project_id = agent.get("project_id", "")
            if project_id:
                self.touch_org_version(project_id)

        return await self.get_agent(agent_id)

    async def update_status(self, agent_id: str, status: str) -> None:
        """Update only the status field (lightweight)."""
        await project_db.execute(
            agent_id,
            "UPDATE agents SET status = ?, updated_at = ? WHERE id = ?",
            [status, int(time.time() * 1000), agent_id],
        )
        agent_router.update(agent_id, status=status)

    async def update_parent(self, agent_id: str,
                            new_parent_id: str | None) -> None:
        """Update only the parent_id field (re-parenting)."""
        await project_db.execute(
            agent_id,
            "UPDATE agents SET parent_id = ?, updated_at = ? WHERE id = ?",
            [new_parent_id, int(time.time() * 1000), agent_id],
        )

    # ── DELETE ───────────────────────────────────────────────

    async def delete_agent(self, agent_id: str) -> dict:
        """Hard-delete an agent. Refuses if agent has subordinates.

        Returns ``{success}`` or ``{success: False, message}``.
        """
        agent = await self.get_agent(agent_id)
        if not agent:
            return {"success": False, "message": "Agent not found"}

        children = await self.get_subordinates(agent_id)
        if children:
            return {"success": False,
                    "message": f"Agent has {len(children)} subordinate(s). "
                               "Transfer or dismiss them first."}

        await project_db.execute(
            agent_id, "DELETE FROM agents WHERE id = ?", [agent_id]
        )
        agent_router.unregister(agent_id)
        log.info("org.delete_agent", agent_id=agent_id)
        return {"success": True}

    async def dismiss_agent(
        self,
        project_id: str,
        agent_id: str,
        *,
        dismissed_by: str | None = None,
    ) -> dict:
        """Soft-delete (archive) an agent. Verifies no subordinates.

        Closes the lifecycle: open tasks reassigned/archived, inbox ACKed,
        alarms cancelled, worktree cleaned. Returns ``{success, agent}`` or
        ``{success: False, message}``.

        DESIGN-3: enforces per-project per-game-day dismiss quota before
        archiving; records the event for same-role rehire cooldown.
        """
        children = await self.get_subordinates(agent_id)
        if children:
            return {"success": False,
                    "message": f"Cannot dismiss agent with {len(children)} "
                               "subordinate(s). Transfer or dismiss them first."}

        agent_before = await self.get_agent(agent_id)
        if not agent_before:
            return {"success": False, "message": "Agent not found"}

        # DESIGN-3: dismiss quota (blast-radius controller)
        try:
            from hiveweave.services.org_guardrails import check_dismiss_quota

            quota_err = await check_dismiss_quota(project_id)
            if quota_err:
                return {"success": False, "message": quota_err}
        except Exception as e:
            log.warning(
                "dismiss_quota_check_failed",
                agent_id=agent_id,
                error=str(e),
            )

        updated = await self.update_agent(agent_id, {"status": "archived"})
        if not updated:
            return {"success": False, "message": "Agent not found"}

        try:
            from hiveweave.services.org_guardrails import record_dismiss

            await record_dismiss(
                project_id,
                agent_id=agent_id,
                role=str(agent_before.get("role") or ""),
                dismissed_by=dismissed_by,
                short_id=agent_before.get("short_id"),
                name=agent_before.get("name"),
            )
        except Exception as e:
            log.warning(
                "dismiss_record_failed",
                agent_id=agent_id,
                error=str(e),
            )

        parent_id = agent_before.get("parent_id") or ""
        # Resolve grandparent once — used when reassign would make
        # assignee==reviewer (common: parent was already pinned reviewer).
        grandparent_id = ""
        if parent_id:
            parent_row = await self.get_agent(parent_id)
            if parent_row:
                grandparent_id = parent_row.get("parent_id") or ""
        now_ms = int(time.time() * 1000)
        # BUG-2: if dismissed agent still has submitted/reviewing deliverables,
        # delay worktree delete (quarantine) so reviewers can read evidence.
        defer_worktree_delete = False
        reassigned_summary: list[dict] = []

        # Close Task Ledger obligations — reassign open work to parent, else archive
        # BUG-1: do NOT flatten submitted/reviewing/verifying → claimed.
        try:
            conn = await project_db.get_project_db_by_project_id(project_id)
            cur = await conn.execute(
                "SELECT id, status, reviewer_id, title FROM tasks "
                "WHERE assignee_id = ? AND is_archived = 0 "
                "AND status NOT IN ('closed', 'cancelled')",
                [agent_id],
            )
            open_tasks = [dict(r) for r in await cur.fetchall()]
            await cur.close()

            if any(
                (t.get("status") or "") in ("submitted", "reviewing")
                for t in open_tasks
            ):
                defer_worktree_delete = True

            if parent_id:
                for t in open_tasks:
                    tid = t["id"]
                    status = (t.get("status") or "").lower()
                    old_reviewer = t.get("reviewer_id") or ""
                    if status in ("claimed", "running", "rework", "created", "blocked"):
                        # In-progress work: reassign + reset to claimed
                        await conn.execute(
                            "UPDATE tasks SET assignee_id = ?, status = 'claimed', "
                            "claimed_at = ?, updated_at = ? WHERE id = ?",
                            [parent_id, now_ms, now_ms, tid],
                        )
                        reassigned_summary.append({
                            "task_id": tid,
                            "from_status": status,
                            "to_status": "claimed",
                            "action": "reassign_reset",
                        })
                    elif status in ("submitted", "reviewing"):
                        # Keep review pipeline. Parent inherits assignee; if
                        # that would equal reviewer (common: parent already
                        # pinned at submit), escalate reviewer to grandparent.
                        # No grandparent → keep dismissed as assignee so the
                        # active parent can still approve (self-review gate).
                        new_assignee = parent_id
                        new_reviewer = old_reviewer
                        if not new_reviewer or new_reviewer == agent_id:
                            new_reviewer = parent_id
                        if new_reviewer == new_assignee:
                            if (
                                grandparent_id
                                and grandparent_id not in (new_assignee, agent_id)
                            ):
                                new_reviewer = grandparent_id
                            else:
                                new_assignee = agent_id
                        await conn.execute(
                            "UPDATE tasks SET assignee_id = ?, reviewer_id = ?, "
                            "updated_at = ? WHERE id = ?",
                            [new_assignee, new_reviewer, now_ms, tid],
                        )
                        reassigned_summary.append({
                            "task_id": tid,
                            "from_status": status,
                            "to_status": status,
                            "action": "reassign_keep_status",
                            "assignee_id": new_assignee,
                            "reviewer_id": new_reviewer,
                        })
                    elif status == "verifying":
                        # VERIFY in flight — leave status alone; only move
                        # assignee so the ledger stays owned by an active agent.
                        await conn.execute(
                            "UPDATE tasks SET assignee_id = ?, updated_at = ? "
                            "WHERE id = ?",
                            [parent_id, now_ms, tid],
                        )
                        reassigned_summary.append({
                            "task_id": tid,
                            "from_status": status,
                            "to_status": status,
                            "action": "reassign_verifying",
                        })
                    elif status == "approved":
                        # CREATOR_MUST_MERGE stays on creator; if dismissed was
                        # assignee only, leave approved with parent as assignee.
                        await conn.execute(
                            "UPDATE tasks SET assignee_id = ?, updated_at = ? "
                            "WHERE id = ?",
                            [parent_id, now_ms, tid],
                        )
                        reassigned_summary.append({
                            "task_id": tid,
                            "from_status": status,
                            "to_status": status,
                            "action": "reassign_approved",
                        })
                    else:
                        # Unknown / terminal-ish — reassign without status rewrite
                        await conn.execute(
                            "UPDATE tasks SET assignee_id = ?, updated_at = ? "
                            "WHERE id = ?",
                            [parent_id, now_ms, tid],
                        )
                        reassigned_summary.append({
                            "task_id": tid,
                            "from_status": status,
                            "to_status": status,
                            "action": "reassign_passthrough",
                        })
            else:
                # No parent: archive unfinished work as cancelled (not closed).
                await conn.execute(
                    "UPDATE tasks SET is_archived = 1, status = 'cancelled', "
                    "archived_by = 'system', archived_reason = 'agent dismissed', "
                    "archived_at = ?, updated_at = ? "
                    "WHERE assignee_id = ? AND is_archived = 0 "
                    "AND status NOT IN ('closed', 'cancelled')",
                    [now_ms, now_ms, agent_id],
                )
            await conn.commit()
            log.info(
                "org.dismiss_agent.tasks_closed",
                agent_id=agent_id,
                reassigned_to=parent_id or None,
                deferred_worktree=defer_worktree_delete,
                reassigned=len(reassigned_summary),
            )
        except Exception as e:
            log.warning(
                "dismiss_close_tasks_failed",
                agent_id=agent_id,
                error=str(e),
            )

        # Notify parent about inherited obligations (structured, language-neutral)
        if parent_id and reassigned_summary:
            try:
                from hiveweave.services.inbox import InboxService

                lines = [
                    "[DISMISS REASSIGN] "
                    f"Agent {agent_before.get('short_id') or agent_id[:8]} "
                    f"({agent_before.get('name') or ''}) dismissed; "
                    f"{len(reassigned_summary)} task(s) transferred to you."
                ]
                for item in reassigned_summary[:12]:
                    lines.append(
                        f"- task={str(item['task_id'])[:12]} "
                        f"{item['from_status']}→{item['to_status']} "
                        f"({item['action']})"
                    )
                await InboxService().send_message(
                    from_agent_id="system",
                    to_agent_id=parent_id,
                    message="\n".join(lines),
                    message_type="system",
                    priority="urgent",
                )
            except Exception as e:
                log.warning(
                    "dismiss_notify_parent_failed",
                    agent_id=agent_id,
                    error=str(e),
                )

        # BUG-5: archive personnel_records so position uniqueness stays real
        try:
            from hiveweave.services.roster import RosterService

            await RosterService().update(
                project_id,
                agent_id,
                {"status": "archived", "updated_by": "system"},
            )
        except Exception as e:
            log.warning(
                "dismiss_archive_personnel_failed",
                agent_id=agent_id,
                error=str(e),
            )

        # ACK all unread inbox — archived agents are never woken
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().mark_all_read(agent_id)
        except Exception as e:
            log.warning(
                "dismiss_mark_inbox_read_failed",
                agent_id=agent_id,
                error=str(e),
            )

        # A4 修复：清理该 agent 的所有 pending 闹钟，防止触发到已 archived agent
        try:
            from hiveweave.services.game_time import GameTimeService
            gt = GameTimeService()
            await gt.cancel_alarms_for_agent(project_id, agent_id)
        except Exception as e:
            log.warning("dismiss_cancel_alarms_failed",
                        agent_id=agent_id, error=str(e))

        # 清理该 agent 的隔离 worktree（executor 才有）
        # BUG-2: submitted/reviewing → quarantine instead of immediate delete
        try:
            short_id = updated.get("short_id", "")
            ws_path = updated.get("workspace_path", "")
            if short_id and ws_path:
                from hiveweave.services.git_worktree import GitWorktreeService
                gwt = GitWorktreeService()
                project_ws = await meta_db.get_project_workspace(project_id)
                if project_ws:
                    if defer_worktree_delete:
                        q = await gwt.quarantine_for_review(
                            project_ws, short_id
                        )
                        log.info(
                            "org.dismiss_agent.worktree_quarantined",
                            agent_id=agent_id,
                            short_id=short_id,
                            quarantine=q,
                        )
                    else:
                        await gwt.delete(project_ws, short_id)
                        log.info(
                            "org.dismiss_agent.worktree_cleaned",
                            agent_id=agent_id,
                            short_id=short_id,
                        )
        except Exception as e:
            log.warning("dismiss_clean_worktree_failed",
                        agent_id=agent_id, error=str(e))

        log.info("org.dismiss_agent", agent_id=agent_id,
                 project_id=project_id)
        self.touch_org_version(project_id)
        return {"success": True, "agent": updated}

    # ── Org chart dirty-flag sync ────────────────────────────

    def touch_org_version(self, project_id: str) -> None:
        """Bump the org version for a project (monotonic ns)."""
        self._org_version[project_id] = time.monotonic_ns()

    def get_org_version(self, project_id: str) -> int:
        """Get the current org version (0 if never set)."""
        return self._org_version.get(project_id, 0)

    async def get_agent_org_version(self, agent_id: str) -> int:
        """Get the org version an agent last read. _VERSION_UNSET if never."""
        project_id = await meta_db.get_agent_project_id(agent_id)
        if project_id is None:
            return self._VERSION_UNSET
        return self._agent_org_version.get(
            (project_id, agent_id), self._VERSION_UNSET
        )

    async def set_agent_org_version(self, agent_id: str, version: int) -> None:
        """Mark that an agent has read the org chart at the given version."""
        project_id = await meta_db.get_agent_project_id(agent_id)
        if project_id is None:
            return
        self._agent_org_version[(project_id, agent_id)] = version

    def org_dirty(self, agent_id: str, project_id: str) -> bool:
        """Check if an agent needs to re-read the org chart."""
        v_cur = self._org_version.get(project_id, 0)
        v_read = self._agent_org_version.get(
            (project_id, agent_id), self._VERSION_UNSET
        )
        if v_read == self._VERSION_UNSET:
            return True
        return v_cur != v_read

    async def build_org_directory(self, project_id: str) -> str:
        """构建精简组织通讯录——只含花名、short_id、role、权限标签、层级关系。

        供 context prompt 注入。org chart 变更后每个 agent 首次对话注入一次，
        之后跳过直到下次变更（仿照 goals_dirty 机制）。
        """
        tree = await self.get_full_tree(project_id)
        if not tree:
            return ""

        lines: list[str] = []

        def walk(node: dict, depth: int) -> None:
            prefix = "  " * depth
            name = node.get("name", "?")
            sid = node.get("short_id", "?")
            role = node.get("role", "?")
            # 可读权限标签: 避免 LLM 误读原始类型而把代码任务派给只读协调者
            perm = str(node.get("permission_type") or "executor")
            label = PERMISSION_TYPE_LABELS.get(perm.lower(), perm)
            lines.append(f"{prefix}- {name} ({sid}) role={role} [{label}]")
            for child in (node.get("children") or []):
                walk(child, depth + 1)

        for root in tree:
            walk(root, 0)

        header = (
            "## Team Directory（组织通讯录 — 用花名或 short_id 找人, 勿用 role）\n"
            + PERMISSION_LABEL_LEGEND
        )
        return header + "\n" + "\n".join(lines)

    # ── TREE TRAVERSAL ───────────────────────────────────────

    async def get_subordinates(self, agent_id: str) -> list[dict]:
        """Get direct children of an agent (excludes archived)."""
        rows = await project_db.query(
            agent_id,
            "SELECT * FROM agents WHERE parent_id = ? AND status != 'archived'",
            [agent_id],
        )
        return [d for r in rows if (d := self._row(r)) is not None]

    async def get_superior(self, agent_id: str) -> dict | None:
        """Get the parent (superior) of an agent, or None if root."""
        agent = await self.get_agent(agent_id)
        if not agent or not agent.get("parent_id"):
            return None
        return await self.get_agent(agent["parent_id"])

    async def get_all_descendants(self, agent_id: str) -> list[dict]:
        """Get all descendants of an agent (recursive, excludes archived)."""
        children = await self.get_subordinates(agent_id)
        result = list(children)
        for child in children:
            result.extend(await self.get_all_descendants(child["id"]))
        return result

    async def get_full_tree(self, project_id: str) -> list[dict]:
        """Build the org tree for a project.

        Roots = active agents with no parent. Archived agents excluded.
        Each node has ``children`` (list or None if leaf).
        """
        agents = await self.list_agents(project_id)
        active = [a for a in agents if a.get("status") != "archived"]

        children_map: dict[str | None, list[dict]] = {}
        for a in active:
            pid = a.get("parent_id")
            children_map.setdefault(pid, []).append(a)

        def build(parent_id: str | None) -> list[dict]:
            kids = children_map.get(parent_id, [])
            nodes: list[dict] = []
            for kid in kids:
                sub = build(kid["id"])
                nodes.append({
                    "id": kid["id"],
                    "short_id": kid.get("short_id"),
                    "name": kid["name"],
                    "role": kid["role"],
                    "status": kid.get("status", "active"),
                    "permission_type": kid.get("permission_type", "executor"),
                    "goal": kid.get("goal", ""),
                    "model_id": kid.get("model_id"),
                    "config": {
                        "model_id": kid.get("model_id"),
                        "system_prompt": kid.get("system_prompt"),
                        "permission_type": kid.get("permission_type", "executor"),
                    },
                    "children": sub if sub else None,
                })
            return nodes

        return build(None)

    # ── TRANSFER ─────────────────────────────────────────────

    async def transfer_agent(self, project_id: str, agent_id: str,
                             new_parent_id: str | None) -> dict | None:
        """Transfer agent to a new parent. Verifies no cycle + org invariants.

        Returns the updated agent dict, or ``{success: False, message}`` on
        cycle / invariant failure. ``new_parent_id=None`` makes the agent a root
        (forbidden for executors).
        """
        if agent_id == new_parent_id:
            return {"success": False,
                    "message": "Cannot transfer an agent to be its own parent"}

        if new_parent_id is not None:
            descendants = await self.get_all_descendants(agent_id)
            if any(d["id"] == new_parent_id for d in descendants):
                return {"success": False,
                        "message": "Cannot transfer: new parent is a descendant "
                                   "(would create a cycle)"}

        from hiveweave.services.org_invariants import validate_transfer

        agents = await self.list_agents(project_id)
        inv_err = validate_transfer(
            agents=agents,
            agent_id=agent_id,
            new_parent_id=new_parent_id,
        )
        if inv_err:
            return {"success": False, "message": inv_err}

        updated = await self.update_agent(agent_id, {"parent_id": new_parent_id})
        if updated:
            self.touch_org_version(project_id)
        return updated

    # ── SHORT ID ─────────────────────────────────────────────

    async def generate_short_id(self) -> str:
        """Generate next short ID (A001, A002, ...).

        Finds the current maximum A-number from agent_router and increments.
        short_id is globally unique across all projects.
        """
        short_ids = agent_router.list_all_short_ids()
        max_num = 0
        for sid in short_ids:
            if not sid:
                continue
            m = _SHORT_ID_NUM_RE.match(sid)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num
        return f"A{str(max_num + 1).zfill(3)}"

    async def touch_last_active(self, agent_id: str) -> None:
        """Persist activity timestamp (stall/silence must not use lifecycle status).

        Also sets activated_at on first activation (修 #4): COALESCE 幂等写入，
        NULL → now；已有值不变。
        """
        now_ms = int(time.time() * 1000)
        try:
            await self.update_agent(agent_id, {"last_active_at": now_ms})
            # 修 #4: 首次激活时写入 activated_at
            await project_db.execute(
                agent_id,
                "UPDATE agents SET activated_at = COALESCE(activated_at, ?) "
                "WHERE id = ?",
                [now_ms, agent_id],
            )
        except Exception as e:
            log.warning(
                "touch_last_active_failed",
                agent_id=agent_id,
                error=str(e),
            )

    # ── HELPERS ──────────────────────────────────────────────

    @staticmethod
    def _row(row) -> dict | None:
        """Convert a DB row to a dict, decoding JSON columns to lists."""
        if row is None:
            return None
        d = dict(row)
        for col in _JSON_COLS:
            v = d.get(col)
            if isinstance(v, str):
                try:
                    d[col] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    d[col] = []
            elif v is None:
                d[col] = []
        # BUG-009/012 修复：防御性 mojibake 修复。
        # 如果 str 字段看起来像 UTF-8 bytes 被当 latin-1 解码的结果
        # （双重编码），尝试 encode('latin-1').decode('utf-8') 还原。
        # 只在修复后包含 CJK 字符时才采用，避免误修合法的 latin-1 文本。
        for k, v in d.items():
            if isinstance(v, str) and len(v) >= 2:
                d[k] = _fix_mojibake(v)
        return d
