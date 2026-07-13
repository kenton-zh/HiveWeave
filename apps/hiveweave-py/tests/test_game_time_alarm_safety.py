"""_fire_alarm 危险脚本拦截测试.

被测文件: src/hiveweave/services/game_time.py
被测方法: GameTimeService._fire_alarm(alarm)

覆盖范围:
  _fire_alarm 在执行 alarm.script_command 前调用 _validate_command_safety，
  对 `cat .env`（敏感路径）和 `rm -rf /`（自毁命令）必须跳过子进程执行，
  不调用 asyncio.create_subprocess_shell。

测试策略:
  - 用 unittest.mock.patch 替换 game_time._execute（避免真实 DB 写入）
  - 用 unittest.mock.patch 监控 asyncio.create_subprocess_shell 是否被调用
  - alarm.to_agent_id 置空以跳过 inbox 通知（避免依赖 InboxService）
  - 一个正向控制测试：安全脚本（echo hello）应调用 create_subprocess_shell，
    证明 mock 按预期工作，且危险脚本的"不调用"是真拦截而非测试缺陷
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


def _make_alarm(alarm_id: str, script_command: str,
                to_agent_id: str = "") -> dict:
    """构造一个带 script_command 的闹钟字典.

    to_agent_id 置空以跳过 inbox 通知，使测试聚焦于脚本安全检查。
    repeat_interval_seconds=0 表示 one-shot 闹钟（_fire_alarm 返回 None）。
    """
    return {
        "id": alarm_id,
        "project_id": "proj-test",
        "from_agent_id": "agent-from",
        "to_agent_id": to_agent_id,
        "purpose": "test alarm",
        "fire_at_game_seconds": 100,
        "repeat_interval_seconds": 0,  # one-shot
        "script_command": script_command,
        "fired": False,
        "run_count": 0,
    }


class TestFireAlarmScriptSafety:
    """_fire_alarm 必须对危险 script_command 跳过子进程执行."""

    async def test_cat_env_script_not_executed(self):
        """包含 `cat .env` 的脚本必须被拦截，不创建子进程."""
        alarm = _make_alarm("alarm-env", script_command="cat .env")
        game_time._alarm_project["alarm-env"] = "proj-test"

        mock_execute = AsyncMock()
        mock_subprocess = AsyncMock()
        svc = GameTimeService()
        with patch("hiveweave.services.game_time._execute", mock_execute), \
             patch("asyncio.create_subprocess_shell", mock_subprocess):
            result = await svc._fire_alarm(alarm)

        # 危险脚本不应创建子进程
        mock_subprocess.assert_not_called()
        # one-shot 闹钟返回 None
        assert result is None
        # DB 更新（标记 fired）仍执行 — 证明 _fire_alarm 正常跑完
        assert mock_execute.await_count == 1

    async def test_rm_rf_root_script_not_executed(self):
        """包含 `rm -rf /` 的脚本必须被拦截，不创建子进程."""
        alarm = _make_alarm("alarm-rm", script_command="rm -rf /")
        game_time._alarm_project["alarm-rm"] = "proj-test"

        mock_execute = AsyncMock()
        mock_subprocess = AsyncMock()
        svc = GameTimeService()
        with patch("hiveweave.services.game_time._execute", mock_execute), \
             patch("asyncio.create_subprocess_shell", mock_subprocess):
            result = await svc._fire_alarm(alarm)

        mock_subprocess.assert_not_called()
        assert result is None
        assert mock_execute.await_count == 1

    async def test_safe_script_is_executed(self):
        """正向控制：安全脚本（echo hello）应调用 create_subprocess_shell.

        证明 mock 按预期工作，且危险脚本的"不调用"是真拦截而非测试缺陷
        （如 _fire_alarm 提前崩溃、或 _validate_command_safety 误拦一切）。
        """
        alarm = _make_alarm("alarm-safe", script_command="echo hello")
        game_time._alarm_project["alarm-safe"] = "proj-test"

        # mock 子进程返回成功执行
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello\n", b"")
        mock_proc.returncode = 0
        mock_execute = AsyncMock()
        mock_subprocess = AsyncMock(return_value=mock_proc)
        svc = GameTimeService()
        with patch("hiveweave.services.game_time._execute", mock_execute), \
             patch("asyncio.create_subprocess_shell", mock_subprocess):
            result = await svc._fire_alarm(alarm)

        # 安全脚本应创建子进程
        mock_subprocess.assert_called_once()
        assert result is None
