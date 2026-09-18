"""会议行状态机 — 议题/轮次/唯一约束（docs/spec/team-meeting.md §议题串行）。

状态机：assembling → collecting ⇄ facilitating → concluded；异常 → aborted。
- ``continue_round`` 仅 ``r < MAX_ROUNDS``（工具表 + 本服务双重硬门）。
- ``conclude_topic`` 随时可（不必用满 3 轮），仅主席。
- 最后一题 conclude：**一笔事务**写 topic_result + concluded +
  delivery_state=pending（泵按 meeting id 幂等重试投递）。
- 同项目进行中一场：partial UNIQUE index（DB 约束，非 check-then-insert）。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

#: 进行中状态（占唯一锁；无唤醒路径必须 abort 释放）
ACTIVE_STATUSES = frozenset({"assembling", "collecting", "facilitating"})
TERMINAL_STATUSES = frozenset({"concluded", "aborted"})

MAX_ROUNDS = 3
DELIVERY_NONE = "none"
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"

UTTERANCE_ROLES = frozenset({"speech", "direction", "topic_result", "abstain"})

# ── abstain 的结构化原因（2026-09-18）──────────────────────────────
# 为什么存在：``abstain`` 本身是**合法终态**（空发言不得被当成正式表态）。
# 但「**主动**弃权」与「**被掐断**而未完成」在库里长得一模一样 ⇒ 主持人会
# 拿「没来得及说话」当「没有意见」推进决策（PLATFORM-ISSUES §8.5 形态①
# 同族：用正常状态掩盖异常状态）。
# ⇒ 落成 ``meeting_utterances.abstain_reason`` 列，消费者按**状态列**分流，
#   **不得**读 ``content`` 文案推断（文案自由、换语言即失效）。
# 放在 service 是因为 prompts（渲染简报）与 runner（产生终局）都要引用它，
# 而两者之间没有依赖边 —— 常量落在共同依赖上可避免循环导入。
ABSTAIN_NO_SPEECH = "no_speech"          # 模型没调 speak_in_meeting（真弃权）
ABSTAIN_BUDGET_EXHAUSTED = "budget_exhausted"  # 轮次预算切断（未跑完）
ABSTAIN_TIMEOUT = "timeout"              # 180s 墙钟超时
ABSTAIN_ERROR = "error"                  # LLM/工具异常
#: 平台**代写**的弃权行 —— 当事人从未获得发言机会（缺席/被移出/泵重启）。
#: ⚠ 与 ``NO_SPEECH``（本人听了、没意见）**必须分开**：把它算作「无异议」
#: 就是本修复要根除的错。「代写」类不计入 `INCOMPLETE_REASONS`，因为它们
#: **不是**「跑了但没跑完」——加轮并不能让缺席者发言，提示主席再加一轮是误导。
ABSTAIN_DISMISSED = "dismissed"          # 会中被 dismiss（平台代写，非本人表态）
ABSTAIN_RECOVERED = "recovered"          # 泵重启补写，本人从未表态
ABSTAIN_UNAVAILABLE = "agent_unavailable"  # 无活体 agent 实例（进程内缺实例）

#: 「未完成」类原因 —— 这些人**表达过但要被切断**，加一轮可能救回来。
ABSTAIN_INCOMPLETE_REASONS = frozenset({
    ABSTAIN_BUDGET_EXHAUSTED,
    ABSTAIN_TIMEOUT,
    ABSTAIN_ERROR,
})

#: 「平台代写」类原因 —— 这些人**从未表态**，且加轮也救不回来（人不在场）。
#: 渲染时同样不得与真弃权混同，但**不应**提示主席加轮。
ABSTAIN_WRITTEN_BY_PLATFORM = frozenset({
    ABSTAIN_DISMISSED,
    ABSTAIN_RECOVERED,
    ABSTAIN_UNAVAILABLE,
})


class MeetingError(Exception):
    """会议状态/权限类拒绝（工具层转为可操作错误文本）。"""


class MeetingConflict(MeetingError):
    """同项目已有一场进行中的会议（DB 唯一约束命中）。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row_to_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    for key in ("topics", "participants", "topic_results"):
        d.pop(key, None)
    try:
        d["topics"] = json.loads(d.pop("topics_json", None) or "[]")
    except Exception:
        d["topics"] = []
    try:
        d["participants"] = json.loads(
            d.pop("participant_ids_json", None) or "[]"
        )
    except Exception:
        d["participants"] = []
    try:
        d["topic_results"] = json.loads(
            d.pop("topic_results_json", None) or "[]"
        )
    except Exception:
        d["topic_results"] = []
    return d


_MEETING_COLS = (
    "id, project_id, chair_id, title, topics_json, participant_ids_json, "
    "status, topic_index, round_index, topic_results_json, delivery_state, "
    "hold_started_at, created_at, concluded_at"
)


class MeetingService:
    """meetings / meeting_utterances 表的领域服务。"""

    # ── 行级 CRUD ────────────────────────────────────────────

    async def create_meeting(
        self,
        project_id: str,
        chair_id: str,
        title: str,
        topics: list[str],
        participant_ids: list[str],
    ) -> dict[str, Any]:
        """INSERT 一场 assembling 会议。同项目进行中唯一（DB 约束）。"""
        if not (topics or []):
            raise MeetingError("meeting requires at least 1 topic")
        # 主席默认参会
        roster: list[str] = []
        for aid in [chair_id, *(participant_ids or [])]:
            aid = str(aid or "").strip()
            if aid and aid not in roster:
                roster.append(aid)
        if len(roster) < 2:
            raise MeetingError(
                "meeting requires at least 2 participants (chair included)"
            )
        now = _now_ms()
        meeting_id = str(uuid.uuid4())
        try:
            await project_db.execute_by_project(
                project_id,
                "INSERT INTO meetings (id, project_id, chair_id, title, "
                "topics_json, participant_ids_json, status, topic_index, "
                "round_index, topic_results_json, delivery_state, "
                "hold_started_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'assembling', 0, 0, '[]', ?, ?, ?)",
                [
                    meeting_id,
                    project_id,
                    chair_id,
                    title or "",
                    json.dumps(list(topics), ensure_ascii=False),
                    json.dumps(roster, ensure_ascii=False),
                    DELIVERY_NONE,
                    now,
                    now,
                ],
            )
        except Exception as e:
            if "idx_meetings_active_per_project" in str(e) or "UNIQUE" in str(
                e
            ).upper():
                raise MeetingConflict(
                    "another meeting is already in progress for this project"
                ) from e
            raise
        row = await self.get_meeting(project_id, meeting_id)
        assert row is not None
        return row

    async def get_meeting(
        self, project_id: str, meeting_id: str
    ) -> dict[str, Any] | None:
        cur = await project_db.query_by_project(
            project_id,
            f"SELECT {_MEETING_COLS} FROM meetings WHERE id = ? AND project_id = ?",
            [meeting_id, project_id],
        )
        return _row_to_dict(cur[0]) if cur else None

    async def get_active_meeting(self, project_id: str) -> dict[str, Any] | None:
        rows = await project_db.query_by_project(
            project_id,
            f"SELECT {_MEETING_COLS} FROM meetings WHERE project_id = ? "
            f"AND status IN ('assembling','collecting','facilitating') "
            f"ORDER BY created_at DESC LIMIT 1",
            [project_id],
        )
        return _row_to_dict(rows[0]) if rows else None

    async def list_meetings(
        self, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = await project_db.query_by_project(
            project_id,
            f"SELECT {_MEETING_COLS} FROM meetings WHERE project_id = ? "
            f"ORDER BY created_at DESC LIMIT ?",
            [project_id, max(1, min(int(limit), 200))],
        )
        return [_row_to_dict(r) for r in rows]

    # ── 状态迁移（带守卫） ───────────────────────────────────

    async def _update(
        self,
        project_id: str,
        meeting_id: str,
        *,
        status: str | None = None,
        topic_index: int | None = None,
        round_index: int | None = None,
        delivery_state: str | None = None,
    ) -> dict[str, Any]:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if topic_index is not None:
            sets.append("topic_index = ?")
            params.append(int(topic_index))
        if round_index is not None:
            sets.append("round_index = ?")
            params.append(int(round_index))
        if delivery_state is not None:
            sets.append("delivery_state = ?")
            params.append(delivery_state)
        if not sets:
            row = await self.get_meeting(project_id, meeting_id)
            if row is None:
                raise MeetingError(f"meeting not found: {meeting_id[:12]}")
            return row
        params.extend([meeting_id, project_id])
        await project_db.execute_by_project(
            project_id,
            f"UPDATE meetings SET {', '.join(sets)} WHERE id = ? AND project_id = ?",
            params,
        )
        row = await self.get_meeting(project_id, meeting_id)
        if row is None:
            raise MeetingError(f"meeting not found: {meeting_id[:12]}")
        return row

    async def set_status(
        self, project_id: str, meeting_id: str, status: str
    ) -> dict[str, Any]:
        """守卫式状态迁移（非法迁移 raise MeetingError）。

        合法边：assembling→collecting；collecting⇄facilitating；
        facilitating→concluded；{assembling,collecting,facilitating}→aborted。
        """
        row = await self.get_meeting(project_id, meeting_id)
        if row is None:
            raise MeetingError(f"meeting not found: {meeting_id[:12]}")
        cur = row["status"]
        allowed = {
            ("assembling", "collecting"),
            ("collecting", "facilitating"),
            ("facilitating", "collecting"),
            ("facilitating", "concluded"),
            ("assembling", "aborted"),
            ("collecting", "aborted"),
            ("facilitating", "aborted"),
        }
        if (cur, status) not in allowed:
            raise MeetingError(
                f"illegal meeting transition {cur} → {status} "
                f"(meeting {meeting_id[:12]})"
            )
        extra: dict[str, Any] = {}
        if status == "concluded":
            extra["delivery_state"] = DELIVERY_PENDING
        return await self._update(
            project_id, meeting_id, status=status, **extra
        )

    async def begin_collecting(
        self, project_id: str, meeting_id: str
    ) -> dict[str, Any]:
        """assembling → collecting：首个议题从第 1 轮开始。"""
        await self.set_status(project_id, meeting_id, "collecting")
        return await self._update(project_id, meeting_id, round_index=1)

    async def continue_round(
        self, project_id: str, meeting_id: str, actor_id: str, direction: str
    ) -> dict[str, Any]:
        """主席注入下一轮方向：facilitating → collecting（r+1）。

        ``r >= MAX_ROUNDS`` 时拒 continue（直调 service 也拒 —— 规格硬门）。
        """
        row = await self.get_meeting(project_id, meeting_id)
        if row is None:
            raise MeetingError(f"meeting not found: {meeting_id[:12]}")
        if row["status"] != "facilitating":
            raise MeetingError(
                f"continue_meeting_round requires status=facilitating "
                f"(got {row['status']})"
            )
        if str(actor_id) != str(row["chair_id"]):
            raise MeetingError("only the chair may continue_meeting_round")
        r = int(row["round_index"])
        if r >= MAX_ROUNDS:
            raise MeetingError(
                f"round {r}/{MAX_ROUNDS} is the last — must conclude_topic "
                "(continue is unavailable at r=3)"
            )
        if not (direction or "").strip():
            raise MeetingError("continue_meeting_round requires a direction")
        await self.record_utterance(
            project_id,
            meeting_id,
            topic_index=int(row["topic_index"]),
            round_index=r,
            agent_id=actor_id,
            role="direction",
            content=direction,
            _force=True,
        )
        return await self._update(
            project_id,
            meeting_id,
            status="collecting",
            round_index=r + 1,
        )

    async def conclude_topic(
        self,
        project_id: str,
        meeting_id: str,
        actor_id: str,
        result: str,
    ) -> dict[str, Any]:
        """主席收口当前议题；最后一题走一笔事务 → concluded + pending。

        Returns the updated meeting row (``concluded_now`` extra key tells
        the orchestrator whether the whole meeting finished).
        """
        row = await self.get_meeting(project_id, meeting_id)
        if row is None:
            raise MeetingError(f"meeting not found: {meeting_id[:12]}")
        if str(actor_id) != str(row["chair_id"]):
            raise MeetingError("only the chair may conclude_topic")
        if row["status"] not in ("facilitating", "collecting"):
            raise MeetingError(
                f"conclude_topic requires an in-discussion status "
                f"(got {row['status']})"
            )
        if not (result or "").strip():
            raise MeetingError("conclude_topic requires a non-empty result")
        topic_index = int(row["topic_index"])
        topics = list(row["topics"] or [])
        results = list(row["topic_results"] or [])
        results.append(
            {"title": str(topics[topic_index] or ""), "result": result.strip()}
        )
        is_last = topic_index + 1 >= len(topics)
        now = _now_ms()
        if is_last:
            # 一笔事务：topic_results + concluded + delivery_state=pending
            await project_db.execute_transaction_by_project(
                project_id,
                [
                    (
                        "UPDATE meetings SET topic_results_json = ?, "
                        "status = 'concluded', concluded_at = ?, "
                        "delivery_state = ? WHERE id = ? AND project_id = ?",
                        [
                            json.dumps(results, ensure_ascii=False),
                            now,
                            DELIVERY_PENDING,
                            meeting_id,
                            project_id,
                        ],
                    )
                ],
            )
            final = await self.get_meeting(project_id, meeting_id)
            assert final is not None
            final["concluded_now"] = True
            return final
        await project_db.execute_transaction_by_project(
            project_id,
            [
                (
                    "UPDATE meetings SET topic_results_json = ?, "
                    "topic_index = ?, round_index = 1, status = 'collecting' "
                    "WHERE id = ? AND project_id = ?",
                    [
                        json.dumps(results, ensure_ascii=False),
                        topic_index + 1,
                        meeting_id,
                        project_id,
                    ],
                )
            ],
        )
        final = await self.get_meeting(project_id, meeting_id)
        assert final is not None
        final["concluded_now"] = False
        return final

    async def mark_delivered(self, project_id: str, meeting_id: str) -> None:
        await project_db.execute_by_project(
            project_id,
            "UPDATE meetings SET delivery_state = ? "
            "WHERE id = ? AND project_id = ?",
            [DELIVERY_DELIVERED, meeting_id, project_id],
        )

    async def remove_participant(
        self, project_id: str, meeting_id: str, agent_id: str
    ) -> dict[str, Any] | None:
        """dismiss 钩子：把该人移出名册（含主席 → 调用方 abort）。"""
        row = await self.get_meeting(project_id, meeting_id)
        if row is None:
            return None
        roster = [a for a in (row["participants"] or []) if a != agent_id]
        await project_db.execute_by_project(
            project_id,
            "UPDATE meetings SET participant_ids_json = ? "
            "WHERE id = ? AND project_id = ?",
            [json.dumps(roster, ensure_ascii=False), meeting_id, project_id],
        )
        updated = await self.get_meeting(project_id, meeting_id)
        return updated

    # ── 发言记录 ─────────────────────────────────────────────

    async def record_utterance(
        self,
        project_id: str,
        meeting_id: str,
        *,
        topic_index: int,
        round_index: int,
        agent_id: str,
        role: str,
        content: str,
        abstain_reason: str = "",
        _force: bool = False,
    ) -> str:
        """写一条 meeting_utterances（平台侧记录，不是 agent 记忆）。

        ``abstain_reason`` 是**结构化事实位**（2026-09-18）：它把「主动弃权」
        与「被轮次预算掐断/超时/异常」分开。消费者按该列分流，**不得**去读
        ``content`` 文案推断原因（文案自由、随语言变）。

        守卫（``_force`` 供 direction/topic_result 内部写绕开 collecting 检查）：
        - speech 只能在 collecting 阶段、且议题/轮次与行状态一致；
        - 同一参会者同一轮只能发言一次。
        """
        if role not in UTTERANCE_ROLES:
            raise MeetingError(f"unknown utterance role: {role!r}")
        row = await self.get_meeting(project_id, meeting_id)
        if row is None:
            raise MeetingError(f"meeting not found: {meeting_id[:12]}")
        if not _force:
            if role == "speech":
                if row["status"] != "collecting":
                    raise MeetingError(
                        f"speech requires status=collecting "
                        f"(got {row['status']}) — late speak is rejected"
                    )
                if (
                    int(topic_index) != int(row["topic_index"])
                    or int(round_index) != int(row["round_index"])
                ):
                    raise MeetingError(
                        "speech topic/round does not match the meeting cursor"
                    )
                if await self.has_utterance(
                    project_id,
                    meeting_id,
                    topic_index=topic_index,
                    round_index=round_index,
                    agent_id=agent_id,
                    roles=("speech", "abstain"),
                ):
                    raise MeetingError(
                        "duplicate speak in the same round is rejected"
                    )
        utterance_id = str(uuid.uuid4())
        await project_db.execute_by_project(
            project_id,
            "INSERT INTO meeting_utterances (id, meeting_id, project_id, "
            "topic_index, round_index, agent_id, role, content, created_at, "
            "abstain_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                utterance_id,
                meeting_id,
                project_id,
                int(topic_index),
                int(round_index),
                agent_id,
                role,
                content or "",
                _now_ms(),
                abstain_reason or "",
            ],
        )
        return utterance_id

    async def get_utterances(
        self,
        project_id: str,
        meeting_id: str,
        *,
        topic_index: int | None = None,
        round_index: int | None = None,
        roles: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, meeting_id, topic_index, round_index, agent_id, role, "
            "content, created_at, abstain_reason FROM meeting_utterances "
            "WHERE meeting_id = ?"
        )
        params: list[Any] = [meeting_id]
        if topic_index is not None:
            sql += " AND topic_index = ?"
            params.append(int(topic_index))
        if round_index is not None:
            sql += " AND round_index = ?"
            params.append(int(round_index))
        if roles:
            placeholders = ", ".join("?" for _ in roles)
            sql += f" AND role IN ({placeholders})"
            params.extend(roles)
        sql += " ORDER BY created_at ASC, id ASC"
        rows = await project_db.query_by_project(project_id, sql, params)
        return [dict(r) for r in rows]

    async def has_utterance(
        self,
        project_id: str,
        meeting_id: str,
        *,
        topic_index: int,
        round_index: int,
        agent_id: str,
        roles: tuple[str, ...] = ("speech",),
    ) -> bool:
        rows = await self.get_utterances(
            project_id,
            meeting_id,
            topic_index=topic_index,
            round_index=round_index,
            roles=roles,
        )
        return any(str(r.get("agent_id")) == str(agent_id) for r in rows)

    # ── 泵用查询 ─────────────────────────────────────────────

    async def recover_candidates(self, project_id: str) -> list[dict[str, Any]]:
        """进行中 + concluded 但投递未完成（崩溃恢复按 meeting id 幂等）。"""
        rows = await project_db.query_by_project(
            project_id,
            f"SELECT {_MEETING_COLS} FROM meetings WHERE project_id = ? AND ("
            f"status IN ('assembling','collecting','facilitating') OR "
            f"(status = 'concluded' AND delivery_state = ?)) "
            f"ORDER BY created_at ASC",
            [project_id, DELIVERY_PENDING],
        )
        return [_row_to_dict(r) for r in rows]


meeting_service = MeetingService()
