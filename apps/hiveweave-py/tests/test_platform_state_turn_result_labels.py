"""批23（5-7）：Platform State 的旧工具名标签改 turn_result 语义命名。

``services/platform_state.py`` 的 ``pending_turn.summary`` /
``pending_turn.waiting_on`` 条目此前 ``source="commit_turn"``（旧工具名），
``gates.pending_turn`` 的空态 note 写 "No pending commit_turn this wake."
—— 数据源实际是 ``services/turn_session`` 的 pending **turn_result**
（set/get_pending_turn_result）。改名后消费方不破（全仓无读该 source 值
的消费方，本测试同时锁新标签与新文案）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services.org import OrgService
from hiveweave.services.platform_state import build_platform_state
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)

PID = "turn-result-labels"


@pytest.fixture
async def ps_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = str(Path(tmpdir).resolve())

        async def fake_ws(p: str):
            return ws if p == PID else None

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PID, "workspace": ws}

        clear_pending_turn_result("ps-labels-agent")
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _make_agent(pid: str) -> dict:
    org = OrgService()
    return await org.create_agent(
        {
            "id": "ps-labels-agent",
            "project_id": pid,
            "name": "墨白",
            "role": "标签核查员",
            "permission_type": "executor",
            "status": "active",
        },
        bootstrap=True,
    )


def _fake_live():
    fake_live = MagicMock()
    fake_live.status = MagicMock(value="idle")
    fake_live.disposition = "waiting_human"
    fake_live._no_progress_streak = 0
    return fake_live


def _entry(epi: dict, key: str) -> dict:
    for section in ("verified", "claimed", "unknown"):
        for row in epi[section]:
            if row.get("key") == key:
                return row
    raise AssertionError(f"entry {key} not found")


@pytest.mark.asyncio
async def test_pending_turn_entries_use_turn_result_source(ps_env):
    pid = ps_env["project_id"]
    agent = await _make_agent(pid)

    set_pending_turn_result(
        agent["id"],
        {
            "phase": "waiting",
            "summary": "blocking on review",
            "gates": [],
            "waiting_on": [{"kind": "human", "ref": "user"}],
            "end_turn": True,
        },
    )
    with patch(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        return_value=_fake_live(),
    ):
        snap = await build_platform_state(agent_id=agent["id"], project_id=pid)

    epi = snap["epistemology"]
    summary_row = _entry(epi, "pending_turn.summary")
    waiting_row = _entry(epi, "pending_turn.waiting_on")
    assert summary_row["source"] == "turn_result", (
        "pending_turn.summary 的 source 必须是 turn_result 语义命名"
        "（旧值 commit_turn 是旧工具名标签）"
    )
    assert waiting_row["source"] == "turn_result"
    # 原有语义字段不回退。
    assert summary_row["epistemic"] == "claimed"
    assert waiting_row["epistemic"] == "claimed"


@pytest.mark.asyncio
async def test_empty_pending_note_uses_turn_result_naming(ps_env):
    pid = ps_env["project_id"]
    agent = await _make_agent(pid)

    clear_pending_turn_result(agent["id"])
    with patch(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        return_value=_fake_live(),
    ):
        snap = await build_platform_state(agent_id=agent["id"], project_id=pid)

    row = _entry(snap["epistemology"], "gates.pending_turn")
    assert row["note"] == "No pending turn_result this wake."
    assert "commit_turn" not in str(row.get("note"))
