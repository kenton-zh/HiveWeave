"""Chat message service — UI message persistence.

契约 17: ChatMessage（UI 消息持久化）
- 区别于 conversation_turns（契约 03 LLM 历史）：chat_messages 是 UI 展示层消息
- 支持流式状态管理（is_streaming）、僵尸消息清理（clear_stuck_streaming）
- 未读背景消息检测（get_unread_background）、未回复用户消息检测（has_unanswered_user_messages）
- 布尔字段（is_streaming/is_background/is_read/is_context）以 0/1 整数存储

chat_messages 表 schema 已完整（含 images/metadata/tool_call_id），无需迁移。
"""

import json
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.db.project import ProjectDbError, execute_by_project

log = structlog.get_logger(__name__)


async def _execute_rowcount_by_agent(
    agent_id: str, sql: str, params: list | None = None
) -> int:
    """同纪律单语句写（agent 键控）+ 返回 rowcount。

    与 db/project.execute 同纪律：per-workspace 写锁 + 异常 rollback/re-raise。
    锁解析镜像 _get_write_lock（get_project_db_for_agent 填充映射；
    miss 时回退 agent 级锁，仅异常/测试路径）。
    """
    conn = await project_db.get_project_db_for_agent(agent_id)
    ws = project_db._agent_cache.get(agent_id) or f"agent:{agent_id}"
    lock = await project_db.get_workspace_write_lock(ws)
    async with lock:
        conn = await project_db.get_project_db_for_agent(agent_id)
        try:
            cursor = await conn.execute(sql, params or [])
            n = cursor.rowcount or 0
            await conn.commit()
            await cursor.close()
            return int(n)
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


async def _execute_rowcount(
    project_id: str, sql: str, params: list | None = None
) -> int:
    """同纪律单语句写（project 键控）+ 返回 rowcount（同 execute_by_project）。"""
    conn = await project_db.get_project_db_by_project_id(project_id)
    # 写者必持 per-workspace 写锁（硬不变量，无降级路径）——workspace 解析
    # 失败直接抛（与 wait_contract/roster/attestation/handoff 同款 helper 一致）。
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"No workspace_path for project_id={project_id}")
    lock = await project_db.get_workspace_write_lock(workspace)
    async with lock:
        try:
            cursor = await conn.execute(sql, params or [])
            n = cursor.rowcount or 0
            await conn.commit()
            await cursor.close()
            return int(n)
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


class ChatMessageService:
    """UI chat message persistence — distinct from conversation_turns.

    所有 agent 级操作路由到 per-project DB（通过 agent_id）。
    clear_stuck_streaming 遍历所有 project DB。

    R12: 构造函数接受可选 project_id，供 main.py lifespan 等场景按项目实例化。
    """

    def __init__(self, project_id: str | None = None) -> None:
        self._project_id = project_id

    async def save_message(self, attrs: dict) -> dict:
        """Save a UI message. Returns {id, role, content, created_at}.

        契约 17: save_message
        - id 缺省 → UUID; role 缺省 → 'assistant'; content 缺省 → ''
        - tool_calls 缺省 → '[]'
        - is_read 默认 1 (True); is_background/is_streaming/is_context 默认 0 (False)
        - bool → int (True→1, False→0)
        - images/metadata: dict|list → JSON 序列化（修复 E8: 补全 images 保存）
        """
        agent_id = attrs["agent_id"]
        msg_id = attrs.get("id") or str(uuid.uuid4())
        role = attrs.get("role", "assistant")
        content = attrs.get("content", "")
        thinking = attrs.get("thinking")
        tool_calls = attrs.get("tool_calls", "[]")
        tool_call_id = attrs.get("tool_call_id")
        # Defend against dict/list values — SQLite only accepts scalars
        if isinstance(thinking, (dict, list)):
            thinking = json.dumps(thinking, ensure_ascii=False)
        if isinstance(tool_calls, (dict, list)):
            tool_calls = json.dumps(tool_calls, ensure_ascii=False)
        is_streaming = 1 if attrs.get("is_streaming", False) else 0
        is_background = 1 if attrs.get("is_background", False) else 0
        is_read = 1 if attrs.get("is_read", True) else 0
        is_context = 1 if attrs.get("is_context", False) else 0
        team_from = attrs.get("team_from_agent_id")
        team_to = attrs.get("team_to_agent_id")

        # images/metadata: accept dict|list (JSON serialize) or string (as-is)
        images = attrs.get("images")
        if images is not None and isinstance(images, (dict, list)):
            images = json.dumps(images, ensure_ascii=False)
        metadata = attrs.get("metadata")
        if metadata is not None and isinstance(metadata, dict):
            metadata = json.dumps(metadata, ensure_ascii=False)

        now_ms = attrs.get("created_at") or int(time.time() * 1000)

        await project_db.execute(
            agent_id,
            "INSERT INTO chat_messages (id, agent_id, role, content, thinking, "
            "tool_calls, tool_call_id, is_streaming, is_background, is_read, "
            "is_context, team_from_agent_id, team_to_agent_id, images, metadata, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [msg_id, agent_id, role, content, thinking, tool_calls, tool_call_id,
             is_streaming, is_background, is_read, is_context, team_from, team_to,
             images, metadata, now_ms])
        log.info("chat_message_saved", agent_id=agent_id, role=role,
                 msg_id=msg_id, preview=content[:80] if content else "")
        return {"id": msg_id, "role": role, "content": content, "created_at": now_ms}

    async def update_message(self, agent_id: str, msg_id: str, attrs: dict) -> bool:
        """Update an existing message's content/is_read/is_streaming/tool_calls/thinking.

        契约 17: update_message — 仅更新非 None 字段。Returns True if a row was affected.
        """
        fields: list[str] = []
        params: list = []
        for key in ("content", "thinking", "tool_calls", "tool_call_id",
                    "is_read", "is_streaming", "is_context", "is_background",
                    "metadata"):
            if key in attrs and attrs[key] is not None:
                val = attrs[key]
                if key in ("is_read", "is_streaming", "is_context", "is_background"):
                    val = 1 if val else 0
                # Defend against dict/list values — SQLite only accepts scalars.
                # These arrive when upstream code passes raw objects instead of
                # JSON strings (e.g. thinking as dict, tool_calls as list).
                if isinstance(val, (dict, list)):
                    val = json.dumps(val, ensure_ascii=False)
                elif not isinstance(val, (str, int, float, bool, type(None))):
                    val = str(val)
                fields.append(f"{key} = ?")
                params.append(val)
        if not fields:
            return False
        params.extend([agent_id, msg_id])

        ok = await _execute_rowcount_by_agent(
            agent_id,
            f"UPDATE chat_messages SET {', '.join(fields)} "
            f"WHERE agent_id = ? AND id = ?",
            params,
        ) > 0
        if not ok:
            log.warning(
                "update_message_no_row",
                agent_id=agent_id,
                msg_id=msg_id,
                fields=list(attrs.keys()),
            )
        return ok

    async def finalize_streaming_message(
        self,
        agent_id: str,
        msg_id: str | None,
        attrs: dict | None = None,
        *,
        allow_agent_wide_fallback: bool = True,
    ) -> bool:
        """Guarantee ``is_streaming=0`` for a turn's placeholder.

        Root cause of orphan zombies: callers treated ``update_message`` as
        success even when it returned False (no DB / no row), cleared the
        in-memory msg id, and left the DB row streaming forever.

        Strategy:
        1. UPDATE the specific row with ``is_streaming=0`` (+ optional fields).
        2. If that fails and ``allow_agent_wide_fallback``, clear ALL streaming
           rows for this agent (safe when the turn is ending and no newer
           placeholder should exist yet).
        3. Returns True if the flag is known cleared; False only if both
           attempts failed (caller should log; runtime sweep is last resort).
        """
        payload = dict(attrs or {})
        payload["is_streaming"] = False

        ok = False
        if msg_id:
            try:
                ok = await self.update_message(agent_id, msg_id, payload)
            except Exception as e:
                log.warning(
                    "finalize_streaming_update_failed",
                    agent_id=agent_id,
                    msg_id=msg_id,
                    error=str(e),
                )
                ok = False

        if ok:
            return True

        if not allow_agent_wide_fallback:
            return False

        try:
            await self.update_streaming_messages_done(agent_id)
            log.warning(
                "finalize_streaming_used_agent_wide_fallback",
                agent_id=agent_id,
                msg_id=msg_id,
            )
            return True
        except Exception as e:
            log.error(
                "finalize_streaming_failed",
                agent_id=agent_id,
                msg_id=msg_id,
                error=str(e),
            )
            return False

    _MSG_SELECT = (
        "SELECT id, agent_id, role, content, thinking, tool_calls, "
        "tool_call_id, is_streaming, is_background, is_read, is_context, "
        "team_from_agent_id, team_to_agent_id, images, metadata, created_at "
        "FROM chat_messages WHERE agent_id = ? "
    )
    # Chat 主栏：全部 user/assistant（含 background trigger 消息与后台
    # assistant 回复）+ 上下文边界标记（role=system + context_marker）。
    # 来源区分由 metadata.source / team_from_agent_id
    # 供前端渲染徽章（用户 / AGENT / 系统 / 看门狗）。
    # 边界标记必须进主栏：它标出模型记忆起点，滤掉则 UI 显示模型早已
    # 压缩掉的历史（「上下文与实际不一致」根因）。其他 system 行不进主栏。
    # json_extract 按键名精确锚定 + kind 白名单与前端 normalizeContextMarker
    # 对齐：LIKE '%"context_marker"%' 会放行未知 kind，那种行前端判 false
    # 丢弃，却已占掉 direct_limit 配额 = 静默吃掉一条真实历史。
    _PANEL_DIRECT_WHERE = (
        "AND (role IN ('user', 'assistant') "
        "OR (role = 'system' AND json_valid(metadata) "
        "AND json_extract(metadata, '$.context_marker') "
        "IN ('compaction', 'prune'))) "
    )
    # 「团队沟通」信件栏：role=team 或 background user（trigger digest）。
    # 不含 background assistant / 工具芯片。
    _PANEL_OTHER_WHERE = (
        "AND (role = 'team' OR (IFNULL(is_background, 0) = 1 AND role = 'user')) "
    )

    async def get_messages(
        self, agent_id: str, limit: int = 200, offset: int = 0
    ) -> list[dict]:
        """Get recent messages for an agent (chronological order). Default limit 200.

        契约 17: DESC + reverse → 正序返回。异常返回 []（fail-empty）。
        R7 fix: 支持 offset 分页（DESC 结果上跳过 offset 条再取 limit 条）。

        Mixed recency across all roles. Agent internals / debug pagination use
        this. UI panes must use ``get_panel_messages`` so foreground chat is
        not squeezed out by team/background traffic.
        """
        try:
            return await self._fetch_recent(agent_id, extra_where="", limit=limit, offset=offset)
        except Exception as e:
            log.warning("get_messages_failed", agent_id=agent_id, error=str(e))
            return []

    async def get_panel_messages(
        self,
        agent_id: str,
        *,
        direct_limit: int = 100,
        other_limit: int = 100,
    ) -> list[dict]:
        """Union of two capped windows for the Chat UI (chronological).

        Mixed ``LIMIT 100`` on the whole table drops old foreground user/assistant
        rows once team + background traffic fills the window — main pane empty,
        「团队沟通」 still populated. Fetch each pane's recency independently,
        then merge by created_at. Predicates match frontend filters
        (displayMessages vs isTeamChannelMessage).
        """
        direct_limit = max(1, min(int(direct_limit), 500))
        other_limit = max(1, min(int(other_limit), 500))
        direct: list[dict] = []
        other: list[dict] = []
        try:
            direct = await self._fetch_recent(
                agent_id, extra_where=self._PANEL_DIRECT_WHERE, limit=direct_limit
            )
        except Exception as e:
            # Direct fail must not 200 a team-only body — UI would replace
            # the transcript with an empty main pane.
            log.warning("get_panel_messages_direct_failed", agent_id=agent_id, error=str(e))
            raise
        try:
            other = await self._fetch_recent(
                agent_id, extra_where=self._PANEL_OTHER_WHERE, limit=other_limit
            )
        except Exception as e:
            log.warning("get_panel_messages_other_failed", agent_id=agent_id, error=str(e))
        return self._merge_panel_windows(direct, other)

    async def get_direct_window_messages(
        self, agent_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """Main-pane window pagination (same predicate as panel direct window).

        前端「加载更早」用：与 get_panel_messages 的 direct 窗同谓词
        （role IN user/assistant），offset 向后翻页无缺口；team 不混入。
        """
        try:
            return await self._fetch_recent(
                agent_id,
                extra_where=self._PANEL_DIRECT_WHERE,
                limit=limit,
                offset=offset,
            )
        except Exception as e:
            log.warning(
                "get_direct_window_messages_failed",
                agent_id=agent_id,
                error=str(e),
            )
            return []

    async def get_history(self, agent_id: str, limit: int = 200) -> list[dict]:
        """Alias for get_messages."""
        return await self.get_messages(agent_id, limit)

    async def _fetch_recent(
        self,
        agent_id: str,
        *,
        extra_where: str,
        limit: int,
        offset: int = 0,
    ) -> list[dict]:
        rows = await project_db.query(
            agent_id,
            f"{self._MSG_SELECT}{extra_where}"
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [agent_id, limit, offset],
        )
        return [self._row_to_msg(r) for r in reversed(rows)]

    @staticmethod
    def _merge_panel_windows(*windows: list[dict]) -> list[dict]:
        by_id: dict[str, dict] = {}
        for window in windows:
            for msg in window:
                mid = msg.get("id")
                if mid is None:
                    continue
                by_id[str(mid)] = msg
        return sorted(
            by_id.values(),
            key=lambda m: (int(m.get("created_at") or 0), str(m.get("id") or "")),
        )

    async def update_streaming_messages_done(self, agent_id: str) -> None:
        """Mark all streaming messages for an agent as done (is_streaming=0).

        契约 17: 用于 safety_timeout / :DOWN handler，防止崩溃后僵尸流式消息。
        Empty content is stamped so the UI does not show a blank bubble forever.
        """
        await project_db.execute(
            agent_id,
            "UPDATE chat_messages SET is_streaming = 0, "
            "content = CASE "
            "  WHEN content IS NULL OR TRIM(content) = '' "
            "  THEN '[对话被中断]' ELSE content END "
            "WHERE agent_id = ? AND is_streaming = 1",
            [agent_id],
        )

    async def clear_orphan_streaming(
        self,
        project_id: str,
        *,
        protect_agent_ids: set[str] | frozenset[str] | None = None,
        soft_age_ms: int = 600_000,
        hard_age_ms: int = 660_000,
    ) -> int:
        """Auto-heal stuck streaming rows for one project (runtime, not only boot).

        A message is an orphan when ``is_streaming=1`` and its agent is
        **not** currently PROCESSING (idle / dead / never started).

        PROCESSING agents are left alone here — stuck live streams are
        handled by ``_streaming_stuck_ms`` / quiet cap, not by a 10-minute
        age cutoff (that cutoff was the old SAFETY wall clock on the row).
        ``soft_age_ms`` / ``hard_age_ms`` remain on the signature for call-site
        compat and are ignored when any agent is protected.

        Returns number of rows cleared.
        """
        protect = set(protect_agent_ids or ())
        try:
            if protect:
                placeholders = ", ".join("?" * len(protect))
                sql = (
                    "UPDATE chat_messages SET is_streaming = 0, "
                    "content = CASE "
                    "  WHEN content IS NULL OR TRIM(content) = '' "
                    "  THEN '[对话被中断]' ELSE content END "
                    f"WHERE is_streaming = 1 AND "
                    f"agent_id NOT IN ({placeholders})"
                )
                params: list = [*protect]
            else:
                # No agent processing — every streaming row is a zombie
                sql = (
                    "UPDATE chat_messages SET is_streaming = 0, "
                    "content = CASE "
                    "  WHEN content IS NULL OR TRIM(content) = '' "
                    "  THEN '[对话被中断]' ELSE content END "
                    "WHERE is_streaming = 1"
                )
                params = []

            cleared = await _execute_rowcount(project_id, sql, params)
            if cleared:
                log.info(
                    "orphan_streaming_cleared",
                    project_id=project_id,
                    cleared=cleared,
                    protected=len(protect),
                )
            return cleared
        except Exception as e:
            log.warning(
                "clear_orphan_streaming_failed",
                project_id=project_id,
                error=str(e),
            )
            return 0

    async def mark_as_read(self, agent_id: str, msg_ids: list[str]) -> int:
        """Mark messages as read by ID list. Returns count marked.

        契约 17: 空列表返回 0 不发 SQL。异常返回 0。
        """
        if not msg_ids:
            return 0
        try:
            placeholders = ", ".join(["?"] * len(msg_ids))
            await project_db.execute(
                agent_id,
                f"UPDATE chat_messages SET is_read = 1 "
                f"WHERE agent_id = ? AND id IN ({placeholders})",
                [agent_id] + msg_ids)
            return len(msg_ids)
        except Exception as e:
            log.warning("mark_as_read_failed", agent_id=agent_id, error=str(e))
            return 0

    async def get_unread_background(self, agent_id: str) -> list[dict]:
        """Get unread background messages (oldest first).

        契约 17: is_background=1 AND is_read=0, ORDER BY created_at ASC。
        异常返回 []（fail-empty）。
        """
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT id, agent_id, role, content, thinking, tool_calls, "
                "tool_call_id, is_streaming, is_background, is_read, is_context, "
                "team_from_agent_id, team_to_agent_id, images, metadata, created_at "
                "FROM chat_messages WHERE agent_id = ? AND is_background = 1 "
                "AND is_read = 0 ORDER BY created_at ASC",
                [agent_id])
            return [self._row_to_msg(r) for r in rows]
        except Exception as e:
            log.warning("get_unread_background_failed", agent_id=agent_id,
                        error=str(e))
            return []

    async def has_unanswered_user_messages(self, agent_id: str) -> bool:
        """Check if there are unanswered user messages.

        契约 17: 存在前台 user 消息，其后（含同时刻 created_at >=）无前台 assistant 消息响应。
        忽略 is_background=1 的消息。异常返回 False（fail-safe，不误触发）。
        """
        try:
            row = await project_db.query_one(
                agent_id,
                "SELECT EXISTS("
                "  SELECT 1 FROM chat_messages m1"
                "  WHERE m1.agent_id = ? AND m1.role = 'user'"
                "    AND m1.is_background = 0"
                "    AND NOT EXISTS("
                "      SELECT 1 FROM chat_messages m2"
                "      WHERE m2.agent_id = m1.agent_id"
                "        AND m2.role = 'assistant'"
                "        AND m2.is_background = 0"
                "        AND m2.created_at >= m1.created_at"
                "    )"
                ") AS has_unanswered",
                [agent_id])
            return bool(row and row["has_unanswered"])
        except Exception as e:
            log.warning("has_unanswered_check_failed", agent_id=agent_id,
                        error=str(e))
            return False

    async def clear_stuck_streaming(self) -> None:
        """Clear all stuck streaming messages across all projects.

        契约 17: 启动时遍历所有 project DB，清除 is_streaming=1 的僵尸消息。
        单个 project 失败仅 warning，不中断整体。整体异常 rescue 返回。
        """
        try:
            rows = await meta_db.query("SELECT id, workspace_path FROM projects")
            for row in rows:
                workspace = row["workspace_path"]
                if not workspace:
                    continue
                try:
                    await execute_by_project(
                        row["id"],
                        "UPDATE chat_messages SET is_streaming = 0, "
                        "content = CASE "
                        "  WHEN content IS NULL OR TRIM(content) = '' "
                        "  THEN '[对话被中断]' ELSE content END "
                        "WHERE is_streaming = 1",
                    )
                except Exception as e:
                    log.warning("clear_stuck_streaming_project_failed",
                                project_id=row["id"], error=str(e))
            log.info("clear_stuck_streaming_done", project_count=len(rows))
        except Exception as e:
            log.warning("clear_stuck_streaming_failed", error=str(e))

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _row_to_msg(row) -> dict:
        d = dict(row)
        d["is_streaming"] = bool(d.get("is_streaming"))
        d["is_background"] = bool(d.get("is_background"))
        d["is_read"] = bool(d.get("is_read"))
        d["is_context"] = bool(d.get("is_context"))
        # BUG-034 fix: add camelCase aliases — frontend mapDbToChatMessages()
        # reads camelCase but DB columns are snake_case. Without these aliases,
        # isTeamChannelMessage() always returns false and "团队沟通" never shows.
        # toolCalls 别名（BUG-034 遗漏）：历史消息无 metadata.segments 补偿，
        # 前端工具行渲染依赖该字段。
        d["isBackground"] = d["is_background"]
        d["isStreaming"] = d["is_streaming"]
        d["isRead"] = d["is_read"]
        d["isContext"] = d["is_context"]
        d["teamFromAgentId"] = d.get("team_from_agent_id")
        d["teamToAgentId"] = d.get("team_to_agent_id")
        d["toolCalls"] = d.get("tool_calls")
        return d
