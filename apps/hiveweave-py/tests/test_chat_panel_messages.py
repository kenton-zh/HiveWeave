"""Chat UI panel fetch — foreground must not be squeezed by mixed LIMIT.

Bug: GET /chat/messages default limit=100 was mixed recency across all roles.
After a long run, the last 100 rows were team + background; the two opening
foreground user/assistant rows sat outside the window. Main pane empty,
「团队沟通」 still showed a count. Messages were not deleted.

Fix: get_panel_messages unions two capped windows that match the frontend
filters (displayMessages vs isTeamChannelMessage). Mixed get_messages stays
for agent internals.

2026-08 契约更新：direct 窗含 background user/assistant（全量时间线，
来源由 metadata.source 区分）；offset>0 分页走 direct 窗同谓词
（get_direct_window_messages），翻页无缺口且不混入 team。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.api import chat as chat_api
from hiveweave.services.chat_message import ChatMessageService


AGENT = "ceo-panel-1"


def _row(
    msg_id: str,
    role: str,
    created_at: int,
    *,
    bg: int = 0,
    content: str = "x",
) -> dict:
    return {
        "id": msg_id,
        "agent_id": AGENT,
        "role": role,
        "content": content,
        "thinking": None,
        "tool_calls": None,
        "tool_call_id": None,
        "is_streaming": 0,
        "is_background": bg,
        "is_read": 1,
        "is_context": 0,
        "team_from_agent_id": None,
        "team_to_agent_id": None,
        "images": None,
        "metadata": None,
        "created_at": created_at,
    }


def _s3_clone_squeeze_corpus() -> list[dict]:
    """Opening human chat + 120 newer team/background rows (归零-shaped)."""
    rows = [
        _row("fg-user", "user", 1, content="GARGANTUA brief"),
        _row("fg-asst", "assistant", 2, content="组织为空…"),
    ]
    for i in range(120):
        t = 1000 + i
        if i % 2 == 0:
            rows.append(_row(f"team-{i}", "team", t, content=f"letter {i}"))
        else:
            rows.append(_row(f"bg-user-{i}", "user", t, bg=1, content=f"digest {i}"))
    rows.append(_row("bg-asst", "assistant", 2000, bg=1, content="hidden tool live"))
    return rows


def _sql_matches(sql: str, fragment: str) -> bool:
    return " ".join(sql.split()).find(fragment) >= 0


def _fake_query(corpus: list[dict]):
    async def query(agent_id: str, sql: str, params: list | None = None):
        params = params or []
        limit = int(params[1]) if len(params) > 1 else 200
        offset = int(params[2]) if len(params) > 2 else 0
        rows = [r for r in corpus if r["agent_id"] == agent_id]
        if _sql_matches(sql, "role IN ('user', 'assistant')"):
            rows = [r for r in rows if r["role"] in ("user", "assistant")]
        elif "role = 'team'" in sql:
            rows = [
                r
                for r in rows
                if r["role"] == "team" or (r["is_background"] and r["role"] == "user")
            ]
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return rows[offset : offset + limit]

    return query


async def test_panel_keeps_old_foreground_when_mixed_window_is_full():
    corpus = _s3_clone_squeeze_corpus()
    svc = ChatMessageService()
    sqls: list[str] = []

    async def query(agent_id: str, sql: str, params: list | None = None):
        sqls.append(sql)
        return await _fake_query(corpus)(agent_id, sql, params)

    with patch("hiveweave.services.chat_message.project_db.query", side_effect=query):
        mixed = await svc.get_messages(AGENT, limit=100)
        panel = await svc.get_panel_messages(AGENT, direct_limit=100, other_limit=100)

    mixed_ids = {m["id"] for m in mixed}
    panel_ids = {m["id"] for m in panel}
    assert "fg-user" not in mixed_ids
    assert "fg-asst" not in mixed_ids
    assert "fg-user" in panel_ids
    assert "fg-asst" in panel_ids
    # 新契约：direct 窗含 background user/assistant（全量时间线）
    assert "bg-asst" in panel_ids
    assert "bg-user-1" in panel_ids
    # other 窗（team+bg-user 共 120 行）limit=100 截断：丢最旧 20 → team 50
    assert sum(1 for m in panel if m["role"] == "team") == 50
    blob = "\n".join(sqls)
    assert ChatMessageService._PANEL_DIRECT_WHERE in blob
    assert ChatMessageService._PANEL_OTHER_WHERE in blob
    ids = [m["id"] for m in panel]
    assert ids == sorted(
        ids, key=lambda i: next(r["created_at"] for r in corpus if r["id"] == i)
    )


async def test_panel_direct_window_includes_background_digest():
    """digest（background user）在主栏可见且 background 标志保留（前端折叠/徽章用）。"""
    corpus = [
        _row("fg", "user", 1, content="human"),
        _row("digest", "user", 2, bg=1, content="trigger"),
        _row("letter", "team", 3, content="hi"),
    ]
    svc = ChatMessageService()
    with patch(
        "hiveweave.services.chat_message.project_db.query",
        side_effect=_fake_query(corpus),
    ):
        panel = await svc.get_panel_messages(AGENT, direct_limit=10, other_limit=10)
    by_id = {m["id"]: m for m in panel}
    assert by_id["fg"]["is_background"] is False
    assert by_id["digest"]["is_background"] is True
    assert by_id["letter"]["role"] == "team"


async def test_ui_endpoint_offset_zero_uses_panel_not_mixed():
    with (
        patch.object(chat_api._chat_msg, "get_panel_messages", new_callable=AsyncMock) as panel,
        patch.object(chat_api._chat_msg, "get_messages", new_callable=AsyncMock) as mixed,
    ):
        panel.return_value = [{"id": "fg"}]
        out = await chat_api.chat_messages("a1", limit=100, offset=0)
        assert out == [{"id": "fg"}]
        panel.assert_awaited_once_with("a1", direct_limit=100, other_limit=100)
        mixed.assert_not_called()


async def test_ui_endpoint_offset_uses_direct_window_pagination():
    """offset>0 与 direct 窗同谓词翻页（加载更早）——不混 team、无缺口。"""
    with (
        patch.object(chat_api._chat_msg, "get_panel_messages", new_callable=AsyncMock) as panel,
        patch.object(chat_api._chat_msg, "get_direct_window_messages", new_callable= AsyncMock) as direct,
        patch.object(chat_api._chat_msg, "get_messages", new_callable=AsyncMock) as mixed,
    ):
        direct.return_value = [{"id": "page2"}]
        out = await chat_api.chat_messages("a1", limit=100, offset=100)
        assert out == [{"id": "page2"}]
        direct.assert_awaited_once_with("a1", limit=100, offset=100)
        panel.assert_not_called()
        mixed.assert_not_called()


async def test_panel_direct_failure_does_not_return_team_only():
    """Direct-window DB failure must raise, not 200 a team-only list."""
    svc = ChatMessageService()

    async def boom(agent_id: str, sql: str, params: list | None = None):
        if ChatMessageService._PANEL_DIRECT_WHERE in sql:
            raise RuntimeError("direct db down")
        return await _fake_query(_s3_clone_squeeze_corpus())(agent_id, sql, params)

    with patch("hiveweave.services.chat_message.project_db.query", side_effect=boom):
        with pytest.raises(RuntimeError, match="direct db down"):
            await svc.get_panel_messages(AGENT, direct_limit=100, other_limit=100)

