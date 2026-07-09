"""A4 修复回归测试 — cancel_alarms_for_agent 防止 dismiss agent 后闹钟残留.

被测文件: src/hiveweave/services/game_time.py
被测方法: GameTimeService.cancel_alarms_for_agent(project_id, agent_id) -> int

测试策略:
  - game_time 的 _execute / _query 是模块级函数，内部走 per-project DB
  - 用 unittest.mock.patch 替换 _execute，用内存列表追踪调用
  - 直接操控模块级 _states 字典模拟内存中的闹钟状态
  - 每个测试后清理 _states 避免污染其他测试
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services import game_time
from hiveweave.services.game_time import GameTimeService


@pytest.fixture(autouse=True)
def clean_states():
    """每个测试前后清空 _states 和 _alarm_project，防止测试间污染."""
    game_time._states.clear()
    game_time._alarm_project.clear()
    yield
    game_time._states.clear()
    game_time._alarm_project.clear()


def _make_alarm(alarm_id: str, project_id: str,
                from_agent_id: str, to_agent_id: str,
                purpose: str = "test alarm",
                fire_at: int = 100) -> dict:
    """构造一个内存态闹钟字典（与 _load_state / schedule_alarm 的格式一致）."""
    return {
        "id": alarm_id,
        "project_id": project_id,
        "from_agent_id": from_agent_id,
        "to_agent_id": to_agent_id,
        "purpose": purpose,
        "fire_at_game_seconds": fire_at,
        "repeat_interval_seconds": 0,
        "script_command": "",
        "fired": False,
        "run_count": 0,
    }


class TestCancelAlarmsForAgent:
    """cancel_alarms_for_agent — dismiss agent 时清理其相关 pending 闹钟 (A4)."""

    async def test_cancel_alarms_by_to_agent_id(self):
        """匹配 to_agent_id 的闹钟被清理，_execute 被调用."""
        project_id = "proj-001"
        agent_id = "agent-to"
        other_id = "agent-other"

        # 在 _states 中预置 3 个闹钟：2 个 to_agent_id 匹配，1 个不匹配
        game_time._states[project_id] = {
            "project_id": project_id,
            "current_game_seconds": 0,
            "real_started_at": 0,
            "alarms": [
                _make_alarm("a1", project_id, from_agent_id=other_id,
                            to_agent_id=agent_id),  # 匹配 to
                _make_alarm("a2", project_id, from_agent_id=other_id,
                            to_agent_id=agent_id),  # 匹配 to
                _make_alarm("a3", project_id, from_agent_id=other_id,
                            to_agent_id=other_id),  # 不匹配
            ],
            "tick_count": 0,
            "task": None,
            "stall_cooldowns": {},
        }

        mock_execute = AsyncMock()
        svc = GameTimeService()
        with patch("hiveweave.services.game_time._execute", mock_execute):
            count = await svc.cancel_alarms_for_agent(project_id, agent_id)

        # 返回清理数量
        assert count == 2
        # _execute 被调用一次（UPDATE SQL）
        assert mock_execute.await_count == 1
        call_args = mock_execute.await_args
        assert call_args.args[0] == project_id
        sql = call_args.args[1]
        assert "status = 'cancelled'" in sql
        # 参数是 [agent_id, agent_id]
        assert call_args.args[2] == [agent_id, agent_id]
        # _states 中匹配的闹钟已被移除，只剩 a3
        remaining = game_time._states[project_id]["alarms"]
        assert len(remaining) == 1
        assert remaining[0]["id"] == "a3"

    async def test_cancel_alarms_by_from_agent_id(self):
        """匹配 from_agent_id 的闹钟被清理."""
        project_id = "proj-002"
        agent_id = "agent-from"
        other_id = "agent-other"

        game_time._states[project_id] = {
            "project_id": project_id,
            "current_game_seconds": 0,
            "real_started_at": 0,
            "alarms": [
                _make_alarm("b1", project_id, from_agent_id=agent_id,
                            to_agent_id=other_id),  # 匹配 from
                _make_alarm("b2", project_id, from_agent_id=other_id,
                            to_agent_id=other_id),  # 不匹配
            ],
            "tick_count": 0,
            "task": None,
            "stall_cooldowns": {},
        }

        mock_execute = AsyncMock()
        svc = GameTimeService()
        with patch("hiveweave.services.game_time._execute", mock_execute):
            count = await svc.cancel_alarms_for_agent(project_id, agent_id)

        assert count == 1
        assert mock_execute.await_count == 1
        remaining = game_time._states[project_id]["alarms"]
        assert len(remaining) == 1
        assert remaining[0]["id"] == "b2"

    async def test_cancel_returns_count(self):
        """返回的 count 等于被清理的闹钟数（混合 to/from 匹配）."""
        project_id = "proj-003"
        agent_id = "agent-mix"
        other_id = "agent-other"

        # 4 个闹钟：2 个匹配 to，1 个匹配 from，1 个不匹配 → 清理 3 个
        game_time._states[project_id] = {
            "project_id": project_id,
            "current_game_seconds": 0,
            "real_started_at": 0,
            "alarms": [
                _make_alarm("c1", project_id, from_agent_id=other_id,
                            to_agent_id=agent_id),       # 匹配 to
                _make_alarm("c2", project_id, from_agent_id=agent_id,
                            to_agent_id=other_id),       # 匹配 from
                _make_alarm("c3", project_id, from_agent_id=agent_id,
                            to_agent_id=agent_id),       # 匹配 to+from（只算 1 次）
                _make_alarm("c4", project_id, from_agent_id=other_id,
                            to_agent_id=other_id),       # 不匹配
            ],
            "tick_count": 0,
            "task": None,
            "stall_cooldowns": {},
        }

        mock_execute = AsyncMock()
        svc = GameTimeService()
        with patch("hiveweave.services.game_time._execute", mock_execute):
            count = await svc.cancel_alarms_for_agent(project_id, agent_id)

        # c1, c2, c3 被清理，c4 保留
        assert count == 3
        remaining = game_time._states[project_id]["alarms"]
        assert len(remaining) == 1
        assert remaining[0]["id"] == "c4"

    async def test_cancel_no_alarms(self):
        """无匹配闹钟时返回 0，不报错，_execute 仍被调用（DB 层 UPDATE）."""
        project_id = "proj-004"
        agent_id = "agent-none"
        other_id = "agent-other"

        game_time._states[project_id] = {
            "project_id": project_id,
            "current_game_seconds": 0,
            "real_started_at": 0,
            "alarms": [
                _make_alarm("d1", project_id, from_agent_id=other_id,
                            to_agent_id=other_id),  # 不匹配
            ],
            "tick_count": 0,
            "task": None,
            "stall_cooldowns": {},
        }

        mock_execute = AsyncMock()
        svc = GameTimeService()
        with patch("hiveweave.services.game_time._execute", mock_execute):
            count = await svc.cancel_alarms_for_agent(project_id, agent_id)

        assert count == 0
        # DB 层 UPDATE 仍执行（即使内存里没匹配，DB 可能有未加载的闹钟）
        assert mock_execute.await_count == 1
        # 闹钟列表不变
        remaining = game_time._states[project_id]["alarms"]
        assert len(remaining) == 1

    async def test_cancel_no_state(self):
        """_states 中无该 project 时不报错，DB 仍更新，返回 0.

        场景：项目刚启动、tick_loop 未运行，或进程重启后内存状态丢失，
        但 DB 中仍有 pending 闹钟。cancel 仍应通过 _execute 更新 DB，
        内存层 cancelled 计为 0（无状态可算）。
        """
        project_id = "proj-005"
        agent_id = "agent-nostate"

        # 故意不在 _states 中放该 project
        assert project_id not in game_time._states

        mock_execute = AsyncMock()
        svc = GameTimeService()
        with patch("hiveweave.services.game_time._execute", mock_execute):
            count = await svc.cancel_alarms_for_agent(project_id, agent_id)

        # 无内存状态 → cancelled = 0
        assert count == 0
        # DB 层 UPDATE 仍执行
        assert mock_execute.await_count == 1
        call_args = mock_execute.await_args
        assert call_args.args[0] == project_id
        # _states 仍未被创建（cancel 不负责创建状态）
        assert project_id not in game_time._states
