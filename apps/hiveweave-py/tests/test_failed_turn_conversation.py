"""Failed LLM turns persist the user instruction into conversation history."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hiveweave.agents.recovery import (
    FAILED_TURN_MARKER,
    FAILED_TURN_NEXT_WAKE_HINT,
    attach_failed_turn_hint,
    history_ends_with_failed_turn,
    persist_failed_turn,
)

USER_TEXT = "做个互动网页 帮我学习 PYTHON 的基本知识 我有C和C++经验"
ENVELOPE = json.dumps({"from": "用户", "content": USER_TEXT}, ensure_ascii=False)


class _FakeConv:
    def __init__(self) -> None:
        self.turns: list[dict] = []

    async def append_turn(self, agent_id, project_id, messages):
        self.turns.extend(list(messages))

    async def get_history(self, agent_id, project_id, token_budget=None):
        return list(self.turns)


def test_history_ends_with_error_marker():
    assert history_ends_with_failed_turn(
        [
            {"role": "user", "content": USER_TEXT},
            {"role": "assistant", "content": f"{FAILED_TURN_MARKER} [ERROR] HTTP 403:"},
        ]
    )
    assert history_ends_with_failed_turn(
        [{"role": "assistant", "content": f"{FAILED_TURN_MARKER} [TIMEOUT] Run interrupted"}]
    )
    assert history_ends_with_failed_turn(
        [{"role": "assistant", "content": f"{FAILED_TURN_MARKER} [空响应超限，已升级上级处理]"}]
    )
    assert not history_ends_with_failed_turn(
        [
            {"role": "user", "content": USER_TEXT},
            {"role": "assistant", "content": "[ERROR] that is a successful reply about errors"},
        ]
    )
    assert not history_ends_with_failed_turn(
        [{"role": "user", "content": "继续"}]
    )


def test_continue_nudge_gets_failed_turn_hint():
    history = [
        {"role": "user", "content": ENVELOPE},
        {"role": "assistant", "content": f"{FAILED_TURN_MARKER} [ERROR] HTTP 403:"},
    ]
    out = attach_failed_turn_hint(history, "继续")
    assert USER_TEXT not in out  # original lives in history, not duplicated here
    assert FAILED_TURN_NEXT_WAKE_HINT in out
    assert out.startswith("继续")
    # Idempotent if already attached
    assert attach_failed_turn_hint(history, out) == out
    assert attach_failed_turn_hint([], "继续") == "继续"


@pytest.mark.asyncio
async def test_persist_failed_turn_writes_user_and_error():
    conv = _FakeConv()
    agent = SimpleNamespace(
        id="ceo-1",
        project_id="proj-1",
        current_job={"message": ENVELOPE},
        _conversation=conv,
        _pending_resume_hint=None,
    )
    await persist_failed_turn(agent, "[ERROR] HTTP 403:")
    roles = [m["role"] for m in conv.turns]
    assert roles == ["user", "assistant"]
    assert USER_TEXT in conv.turns[0]["content"]
    assert conv.turns[1]["content"].startswith(FAILED_TURN_MARKER)
    assert history_ends_with_failed_turn(conv.turns)
    nudged = attach_failed_turn_hint(conv.turns, "继续")
    assert FAILED_TURN_NEXT_WAKE_HINT in nudged

    await persist_failed_turn(agent, "[ERROR] HTTP 403:")
    assert len(conv.turns) == 2


@pytest.mark.asyncio
async def test_checkpoint_does_not_clobber_failed_turn_fallback():
    class _Boom:
        async def get_history(self, *args, **kwargs):
            return []

        async def append_turn(self, *args, **kwargs):
            raise RuntimeError("db down")

    class _Log:
        async def write_work_log(self, *args, **kwargs):
            return None

    agent = SimpleNamespace(
        id="ceo-1",
        project_id="proj-1",
        current_job={"message": ENVELOPE},
        _conversation=_Boom(),
        _pending_resume_hint=None,
        _work_log=_Log(),
    )
    await persist_failed_turn(agent, "[ERROR] HTTP 403:")
    assert USER_TEXT in (agent._pending_resume_hint or "")

    from hiveweave.agents.agent import Agent

    await Agent._write_resume_checkpoint(
        agent, reason="safety_timeout", inbox_ids=["inbox-1"]
    )
    hint = agent._pending_resume_hint or ""
    assert USER_TEXT in hint
    assert "[RESUME CHECKPOINT]" in hint


@pytest.mark.asyncio
async def test_persist_failed_turn_fallback_hint_on_append_failure():
    class _Boom:
        async def append_turn(self, *args, **kwargs):
            raise RuntimeError("db down")

    agent = SimpleNamespace(
        id="ceo-1",
        project_id="proj-1",
        current_job={"message": ENVELOPE},
        _conversation=_Boom(),
        _pending_resume_hint=None,
    )
    await persist_failed_turn(agent, "[ERROR] HTTP 403:")
    hint = agent._pending_resume_hint or ""
    assert FAILED_TURN_NEXT_WAKE_HINT in hint
    assert USER_TEXT in hint
