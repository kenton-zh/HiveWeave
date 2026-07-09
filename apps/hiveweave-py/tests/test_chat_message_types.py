"""Regression tests for chat_message type-safety bugs.

BUG: save_message/update_message crashed when dict/list values were passed
     as thinking or tool_calls fields (SQLite only accepts scalars).
BUG: _streaming_msg_id was assigned the entire save_message() return dict
     instead of saved["id"], causing "Error binding parameter 6" everywhere.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.chat_message import ChatMessageService


class TestSaveMessageDictDefense:
    """save_message() converts dict/list values to JSON before SQLite binding."""

    @pytest.fixture
    def svc(self):
        return ChatMessageService()

    async def test_dict_thinking_is_json_encoded(self, svc: ChatMessageService):
        """thinking field as dict should be JSON-serialized, not passed raw to SQLite."""
        attrs = {
            "agent_id": "test-agent",
            "role": "assistant",
            "content": "hello",
            "thinking": {"type": "thought", "content": "hmm"},
            "tool_calls": "[]",
        }
        # Patch get_project_db_for_agent (called by save_message) to return a mock
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.lastrowid = 1
        mock_conn.execute.return_value = mock_cursor

        with patch(
            "hiveweave.services.chat_message.project_db.get_project_db_for_agent",
            return_value=mock_conn,
        ):
            result = await svc.save_message(attrs)

        # Should succeed — no ProgrammingError
        assert result is not None
        assert "id" in result

    async def test_list_tool_calls_is_json_encoded(self, svc: ChatMessageService):
        """tool_calls as list should be JSON-serialized, not passed raw to SQLite."""
        attrs = {
            "agent_id": "test-agent-2",
            "role": "assistant",
            "content": "Using tools",
            "thinking": None,
            "tool_calls": [{"function": {"name": "bash", "arguments": '{"cmd":"ls"}'}}],
        }
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.lastrowid = 1
        mock_conn.execute.return_value = mock_cursor

        with patch(
            "hiveweave.services.chat_message.project_db.get_project_db_for_agent",
            return_value=mock_conn,
        ):
            result = await svc.save_message(attrs)

        assert result is not None
        assert "id" in result


class TestUpdateMessageDictDefense:
    """update_message() converts dict/list values to JSON before SQLite binding."""

    @pytest.fixture
    def svc(self):
        return ChatMessageService()

    async def test_dict_value_is_json_encoded(self, svc: ChatMessageService):
        """Any dict/list field value should be JSON-serialized."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.rowcount = 1
        mock_conn.execute.return_value = mock_cursor

        # Simulate the bug: _streaming_msg_id was a dict, not a string
        with patch(
            "hiveweave.services.chat_message.project_db.get_project_db_for_agent",
            return_value=mock_conn,
        ):
            result = await svc.update_message(
                "test-agent",
                "msg-001",  # This was the bug — passing dict as msg_id
                {
                    "content": "test",
                    "thinking": {"nested": {"deep": True}},
                    "is_streaming": False,
                },
            )

        assert result is True  # Update succeeded
        # Verify the SQL params don't contain raw dicts
        call_args = mock_conn.execute.call_args
        params = call_args[0][1]  # params list
        for p in params:
            assert not isinstance(p, (dict, list)), f"Raw dict/list leaked into SQL: {p}"
