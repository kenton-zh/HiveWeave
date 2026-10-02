"""Charter service — project charter and enterprise goals management.

契约 14: 项目章程与企业目标
- agent_charters table in per-project DB (save: DELETE + INSERT in single transaction)
- goals workbook in project_meta.goals_json (JSON: objective/focus/keyResults/userInvolvement)
- Goals dirty-flag sync via in-memory version dict (replaces Elixir ETS)
- userInvolvement default: "宏观决策+技术选型" (medium level)
- key_results normalization: string → {text, status:"doing", owner:nil}
"""

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import (
    execute_by_project,
    execute_transaction_by_project,
    get_project_db_by_project_id,
)
from hiveweave.services.agent_router import agent_router

logger = structlog.get_logger()

DEFAULT_USER_INVOLVEMENT = "宏观决策+技术选型"  # medium level (契约 14 RECONCILE)

# ── B3 ③④：charter 编制结构声明（零 schema 变更）────────────────
# dict 形态新增可选 paradigm / faces / pod_count 字段（B3 修法③，范式枚举
# 与最低中层数换算在 org_invariants.CHARTER_PARADIGMS /
# min_business_coordinators）。存储不加列：把声明编码成一行人类可读
# 「[org-shape] paradigm=… faces=… pod=…」标记追加进既有 project_rules 列
# （agent_charters 无 DDL）；read 侧 parse_org_shape 解析回结构化字段。
# 用单行文本而非裸 JSON：project_rules 会被 agents/agent.py 原文注入各
# agent 的 Project Rules 段，标记行对人可读、对 parse_org_shape 可逆。
# 旧 charter 无标记 → 解析为 {} → 调用方跳过校验（不拦旧数据）。
ORG_SHAPE_MARKER_PREFIX = "[org-shape]"


def _org_shape_marker(paradigm: Any, faces: Any, pod_count: Any) -> str:
    """dict 字段 → 单行 [org-shape] 标记；无可声明字段时返回空串。"""
    parts: list[str] = []
    if paradigm:
        p = str(paradigm).strip().lower()
        if p:
            parts.append(f"paradigm={p}")
    for label, val in (("faces", faces), ("pod", pod_count)):
        if val is not None:
            try:
                parts.append(f"{label}={int(val)}")
            except (TypeError, ValueError):
                continue
    if not parts:
        return ""
    return f"{ORG_SHAPE_MARKER_PREFIX} " + " ".join(parts)


def parse_org_shape(*texts: str | None) -> dict[str, Any]:
    """从 charter 文本（project_rules / content）解析 [org-shape] 标记行。

    多个文本/多行标记时取**最后一个**有效声明（重存的新声明覆盖旧声明）。
    键值不合法的 token 忽略；无任何标记返回 {}（旧 charter → 跳过校验）。
    传多个文本时后面的参数优先（content 覆盖 project_rules）。
    """
    result: dict[str, Any] = {}
    for text in texts:
        if not text:
            continue
        for line in str(text).splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith(ORG_SHAPE_MARKER_PREFIX):
                continue
            fields: dict[str, Any] = {}
            for token in stripped[len(ORG_SHAPE_MARKER_PREFIX):].split():
                key, _, raw = token.partition("=")
                key = key.strip().lower()
                if not raw:
                    continue
                if key == "paradigm":
                    fields["paradigm"] = raw.strip().lower()
                elif key == "faces":
                    try:
                        fields["faces"] = int(raw)
                    except ValueError:
                        continue
                elif key == "pod":
                    try:
                        fields["pod_count"] = int(raw)
                    except ValueError:
                        continue
            if fields:
                result.update(fields)
    return result


def _next_charter_version(prev: Any) -> str:
    """B4：charter 重存 = 新版本（minor 自增；不可解析回 1.0）。"""
    try:
        s = str(prev or "").strip()
        if not s:
            return "1.0"
        return f"{round(float(s) + 0.1, 1):.1f}"
    except (TypeError, ValueError):
        return "1.0"


class CharterService:
    """Manages project charters and enterprise goals with dirty-flag sync."""

    def __init__(self) -> None:
        # In-memory goals version (replaces Elixir ETS :hiveweave_goals_sync)
        self._goals_version: dict[str, int] = {}
        self._agent_goals_version: dict[tuple[str, str], int] = {}

    async def save_charter(
        self, project_id: str, agent_id: str, content: str | dict
    ) -> str:
        """Save or update the project charter (DELETE + INSERT).

        content: str (charter body) or dict {title, content, status,
        project_rules} plus optional org-shape declaration fields
        ``paradigm`` / ``faces`` / ``pod_count``（B3 修法③；零 schema
        变更 —— 编码成 [org-shape] 标记行追加进 project_rules 列）。
        Re-saving bumps ``version`` (B4：charter 新版本，minor 自增).
        Returns the new charter ID.

        Writes to the per-project DB (agent_charters table). The project_id
        is resolved via AgentRouter from agent_id to ensure correct routing.
        """
        if isinstance(content, dict):
            title = content.get("title", "Project Charter")
            body = content.get("content", "")
            status = content.get("status", "active")
            project_rules = content.get("project_rules", "")
            paradigm = content.get("paradigm")
            faces = content.get("faces")
            pod_count = content.get("pod_count", content.get("pod"))
        else:
            title = "Project Charter"
            body = str(content)
            status = "active"
            project_rules = ""
            paradigm = None
            faces = None
            pod_count = None

        # B3 ③：org-shape 声明 → 单行标记追加进 project_rules（不拒存：
        # 字段非法时 _org_shape_marker 自动丢弃该键，charter 照常保存）。
        shape_marker = _org_shape_marker(paradigm, faces, pod_count)
        if shape_marker:
            project_rules = (
                f"{project_rules}\n{shape_marker}"
                if project_rules
                else shape_marker
            )
            logger.info(
                "charter.org_shape_declared",
                project_id=project_id,
                marker=shape_marker,
            )

        charter_id = str(uuid.uuid4())
        now = int(time.time() * 1000)

        # Route to per-project DB via AgentRouter
        routed_pid = agent_router.get_project_id(agent_id)
        if routed_pid is None:
            raise ValueError(
                f"Agent {agent_id} not found in AgentRouter"
            )
        workspace = await meta_db.get_project_workspace(routed_pid)
        if workspace is None:
            raise ValueError(
                f"Workspace not found for project {routed_pid}"
            )

        # B4：重存 = charter 新版本（minor 自增；首存为 1.0）。查询失败
        # 不拦存（fail-open，回退 1.0）。
        prev_version: Any = None
        try:
            conn = await get_project_db_by_project_id(routed_pid)
            cursor = await conn.execute(
                """SELECT version FROM agent_charters WHERE project_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                [routed_pid],
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is not None:
                prev_version = row["version"]
        except Exception as e:
            logger.debug("charter.prev_version_lookup_failed", error=str(e))
        version = _next_charter_version(prev_version)

        # DELETE + INSERT 单事务原子写（公共 tx helper 自带写锁 + 异常回滚）
        try:
            await execute_transaction_by_project(
                routed_pid,
                [
                    (
                        "DELETE FROM agent_charters WHERE project_id = ?",
                        [routed_pid],
                    ),
                    (
                        """INSERT INTO agent_charters
                           (id, project_id, agent_id, title, content, status,
                            project_rules, version, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [charter_id, routed_pid, agent_id, title, body,
                         status, project_rules, version, now, now],
                    ),
                ],
            )
        except Exception as e:
            logger.error("charter.save_failed", project_id=routed_pid,
                         error=str(e))
            raise

        logger.info("charter.saved", project_id=routed_pid, title=title[:80])
        return charter_id

    async def read_charter(self, project_id: str) -> dict | None:
        """Read the current project charter from per-project DB.

        Returns dict with 'formatted' plus（若 charter 声明了编制结构）
        B3 ③④ 解析出的 ``paradigm`` / ``faces`` / ``pod_count`` 字段；
        旧 charter 无标记 → 不含这些键（调用方跳过校验）。
        底层 get_project_db_by_project_id 失败时 raise ProjectDbError，由调用方处理。
        """
        conn = await get_project_db_by_project_id(project_id)
        try:
            cursor = await conn.execute(
                """SELECT id, project_id, agent_id, title, content, status,
                          project_rules, version, created_at, updated_at
                   FROM agent_charters WHERE project_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                [project_id],
            )
            row = await cursor.fetchone()
            await cursor.close()
        except Exception:
            return None
        if row is None:
            return None
        d = dict(row)
        # B3 ③④：解析 [org-shape] 声明。content 优先（agent 在正文里
        # 手写的声明覆盖 project_rules 里 dict 字段落盘的标记）。
        d.update(parse_org_shape(d.get("project_rules"), d.get("content")))
        d["formatted"] = self._format_charter(d)
        return d

    async def read_goals(self, project_id: str) -> dict:
        """Read enterprise goals from project_meta.goals_json (agent-facing).

        Uses the dedicated goals_json column to avoid overwriting the human-facing
        charter_json. Falls back to charter_json.goals for old data.

        Returns dict {objective, focus, keyResults, userInvolvement} or {}.
        底层 get_project_db_by_project_id 失败时 raise ProjectDbError，由调用方处理。
        """
        conn = await get_project_db_by_project_id(project_id)
        try:
            cursor = await conn.execute(
                "SELECT goals_json, charter_json FROM project_meta WHERE project_id = ?",
                [project_id],
            )
            row = await cursor.fetchone()
            await cursor.close()
        except Exception:
            return {}
        if row is None:
            return {}
        # Prefer goals_json (agent-facing column)
        raw = row["goals_json"]
        if raw:
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    return decoded
            except json.JSONDecodeError:
                pass
        # Fallback: extract goals from charter_json (human-facing, nested format)
        charter_raw = row["charter_json"]
        if charter_raw:
            try:
                charter = json.loads(charter_raw)
                if isinstance(charter, dict) and "goals" in charter:
                    return charter["goals"]
            except json.JSONDecodeError:
                pass
        return {}

    async def update_goals(self, project_id: str, goals: dict) -> None:
        """Update enterprise goals (merge with existing, bump version).

        Writes to project_meta.goals_json in the per-project DB.
        """
        existing = await self.read_goals(project_id)

        objective = goals.get("objective") or existing.get("objective", "")
        focus = goals.get("focus") or existing.get("focus", "")

        # Normalize key_results: accept string or object arrays
        kr_raw = goals.get("key_results") or goals.get("keyResults") or []
        if kr_raw:
            key_results = [self._normalize_kr(kr) for kr in kr_raw]
        else:
            key_results = existing.get("keyResults", [])

        user_involvement = (
            goals.get("user_involvement")
            or goals.get("userInvolvement")
            or existing.get("userInvolvement", DEFAULT_USER_INVOLVEMENT)
        )

        goals_json = json.dumps({
            "objective": objective,
            "focus": focus,
            "keyResults": key_results,
            "userInvolvement": user_involvement,
        })

        # Write to project_meta.goals_json in per-project DB (agent-facing).
        # Uses UPSERT to handle the case where the project_meta row doesn't exist yet.
        now = int(time.time() * 1000)
        await execute_by_project(
            project_id,
            """INSERT INTO project_meta (project_id, goals_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                   goals_json = excluded.goals_json,
                   updated_at = excluded.updated_at""",
            [project_id, goals_json, now],
        )
        self.touch_goals_version(project_id)
        logger.info("charter.goals_updated", project_id=project_id)

        # 推送 WebSocket 事件 — 前端 GoalsPanel 监听后重新拉取
        try:
            from hiveweave.realtime.event_bus import status_event_bus
            await status_event_bus.publish_goals_updated(project_id)
        except Exception as e:
            logger.warning("goals_updated_push_failed", project_id=project_id, error=str(e))

    # ── Goals dirty-flag sync ─────────────────────────────────

    # Sentinel for "never initialized" — distinct from "synced to empty state".
    # Prevents 0==0 bug where agent synced to version 0 is always dirty.
    _VERSION_UNSET: int = -1

    def touch_goals_version(self, project_id: str) -> None:
        """Bump the goals version for a project (monotonic ns)."""
        self._goals_version[project_id] = time.monotonic_ns()

    def get_goals_version(self, project_id: str) -> int:
        """Get the current goals version (0 if never set)."""
        return self._goals_version.get(project_id, 0)

    async def get_agent_goals_version(self, agent_id: str) -> int:
        """Get the version an agent last read. Returns _VERSION_UNSET if never read."""
        project_id = await meta_db.get_agent_project_id(agent_id)
        if project_id is None:
            return self._VERSION_UNSET
        return self._agent_goals_version.get(
            (project_id, agent_id), self._VERSION_UNSET
        )

    async def set_agent_goals_version(self, agent_id: str, version: int) -> None:
        """Mark that an agent has read the goals at the given version."""
        project_id = await meta_db.get_agent_project_id(agent_id)
        if project_id is None:
            return
        self._agent_goals_version[(project_id, agent_id)] = version

    def goals_dirty(self, agent_id: str, project_id: str) -> bool:
        """Check if an agent needs to re-read the goals.

        Uses _VERSION_UNSET (-1) as sentinel for "never read" to avoid
        the 0==0 infinite-dirty bug after backend restart.
        """
        v_cur = self._goals_version.get(project_id, 0)
        v_read = self._agent_goals_version.get(
            (project_id, agent_id), self._VERSION_UNSET
        )
        # Never read at all → dirty
        if v_read == self._VERSION_UNSET:
            return True
        # Already read but version changed → dirty
        return v_cur != v_read

    # ── Helpers ───────────────────────────────────────────────

    def _normalize_kr(self, kr: Any) -> dict:
        """Normalize a key_result to {text, status, owner, taskIds?} object."""
        if isinstance(kr, str):
            return {"text": kr, "status": "doing", "owner": None}
        if isinstance(kr, dict):
            result = {
                "text": kr.get("text", ""),
                "status": kr.get("status", "doing"),
                "owner": kr.get("owner"),
            }
            tids = kr.get("taskIds") or kr.get("task_ids")
            if isinstance(tids, list) and tids:
                result["taskIds"] = [str(t) for t in tids if t]
            return result
        return {"text": str(kr), "status": "doing", "owner": None}

    def _format_charter(self, d: dict) -> str:
        """Build Markdown formatted string for the charter."""
        title = d.get("title", "Untitled")
        content = d.get("content", "")
        status = d.get("status", "unknown")
        created = d.get("created_at")
        time_str = "unknown"
        if created:
            try:
                dt = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
                time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                time_str = "unknown"
        return (
            f"## Project Charter: {title}\n\n"
            f"{content}\n\n"
            f"_Status: {status}, Created: {time_str}_"
        )


charter_service = CharterService()
