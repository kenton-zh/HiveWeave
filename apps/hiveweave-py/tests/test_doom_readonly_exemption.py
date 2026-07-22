"""Doom loop 只读轮询豁免 — get_tasks 等只读工具不按 3 次阈值误杀。

实锤事故（生产日志）：归零/拾光/潮汐各被杀过一次 ——
「Doom loop detected: tool get_tasks called 3+ times with same args (after warning)」。
agent 没有订阅机制，轮询 get_tasks/read_file 是它获取状态的唯一手段，
只读工具同参重复调用应豁免 doom 计数，仅受 15 次保险丝约束（防 token 烧钱）。

覆盖：
1. 只读工具 3-10 次同参连续调用不触发 doom
2. 写类/副作用工具 3 次同参仍触发（阈值不变）
3. 只读工具超过保险丝（15 次）仍熔断
"""

from __future__ import annotations

from hiveweave.llm.streamer import (
    DOOM_LOOP_READONLY_FUSE,
    DOOM_LOOP_READONLY_TOOLS,
    Streamer,
    doom_loop_limit,
)


def _tc(name: str, args: str = "{}") -> dict:
    return {"id": "call-1", "name": name, "arguments": args}


def _tracker() -> dict:
    return {"last_key": None, "count": 0, "last_errored": False}


class TestReadonlyExemption:
    def test_get_tasks_10_same_args_no_doom(self):
        """get_tasks 同参连续 10 次不触发 —— 事故核心回归。

        TEST3: get_tasks 专属阈值降为 6，故 5 次仍安全。
        """
        tracker = _tracker()
        for i in range(5):
            hit = Streamer._detect_doom_loop(
                [_tc("get_tasks", '{"status":"open"}')], tracker
            )
            assert hit is None, f"第 {i + 1} 次调用误触发 doom"

    def test_readonly_set_tools_no_doom_within_10(self):
        """集合内只读工具（非专属低阈值）3-10 次同参均不触发。"""
        low = {"check_agent_status", "get_tasks"}
        for name in sorted(DOOM_LOOP_READONLY_TOOLS - low):
            tracker = _tracker()
            for i in range(10):
                hit = Streamer._detect_doom_loop([_tc(name)], tracker)
                assert hit is None, f"{name} 第 {i + 1} 次调用误触发 doom"

    def test_status_poll_fuse_trips_early(self):
        """check_agent_status / get_tasks 专属低保险丝（TEST3）。"""
        assert doom_loop_limit("check_agent_status") == 5
        assert doom_loop_limit("get_tasks") == 6
        tracker = _tracker()
        hit = None
        for _ in range(5):
            hit = Streamer._detect_doom_loop([_tc("check_agent_status")], tracker)
        assert hit == "check_agent_status"

    def test_readonly_fuse_trips_at_15(self):
        """一般只读工具同参连续 15 次仍熔断 —— 保险丝兜底防 token 烧钱。"""
        tracker = _tracker()
        hit = None
        for _ in range(DOOM_LOOP_READONLY_FUSE):
            hit = Streamer._detect_doom_loop([_tc("read_file", '{"path":"a"}')], tracker)
        assert hit == "read_file"

    def test_readonly_below_fuse_no_doom(self):
        """只读工具同参连续 14 次（保险丝 -1）不触发。"""
        tracker = _tracker()
        for i in range(DOOM_LOOP_READONLY_FUSE - 1):
            hit = Streamer._detect_doom_loop([_tc("read_file", '{"path":"a"}')], tracker)
            assert hit is None, f"第 {i + 1} 次调用误触发 doom"

    def test_readonly_polling_interleaved_with_writes_resets(self):
        """轮询穿插其他工具调用时计数重置，更不会误杀。"""
        tracker = _tracker()
        for _ in range(5):
            assert Streamer._detect_doom_loop([_tc("get_tasks")], tracker) is None
            # 穿插一次不同调用 → 计数重置
            assert Streamer._detect_doom_loop(
                [_tc("write_file", '{"path":"x","content":"y"}')], tracker
            ) is None


class TestWriteToolsUnchanged:
    def test_bash_still_trips_at_3(self):
        """副作用工具 bash 同参 3 次仍触发 —— 阈值不变。"""
        tracker = _tracker()
        hit = None
        for _ in range(3):
            hit = Streamer._detect_doom_loop(
                [_tc("bash", '{"command":"ls"}')], tracker
            )
        assert hit == "bash"

    def test_apply_patch_still_trips_at_3(self):
        tracker = _tracker()
        hit = None
        for _ in range(3):
            hit = Streamer._detect_doom_loop([_tc("apply_patch", '{"patches":[]}')], tracker)
        assert hit == "apply_patch"

    def test_unlisted_write_tool_default_3(self):
        """未列名工具（如 dispatch_task）仍按默认阈值 3 触发。"""
        tracker = _tracker()
        hit = None
        for _ in range(3):
            hit = Streamer._detect_doom_loop(
                [_tc("dispatch_task", '{"title":"t"}')], tracker
            )
        assert hit == "dispatch_task"

    def test_send_message_threshold_5_unchanged(self):
        """send_message 保持 5 次阈值：4 次不触发，第 5 次触发。"""
        tracker = _tracker()
        for _ in range(4):
            assert Streamer._detect_doom_loop(
                [_tc("send_message", '{"to":"x","content":"y"}')], tracker
            ) is None
        hit = Streamer._detect_doom_loop(
            [_tc("send_message", '{"to":"x","content":"y"}')], tracker
        )
        assert hit == "send_message"


class TestDoomLoopLimitMapping:
    def test_readonly_tools_map_to_fuse(self):
        from hiveweave.llm.streamer import DOOM_LOOP_TOOL_LIMITS

        for name in DOOM_LOOP_READONLY_TOOLS:
            if name in DOOM_LOOP_TOOL_LIMITS:
                assert doom_loop_limit(name) == DOOM_LOOP_TOOL_LIMITS[name]
            else:
                assert doom_loop_limit(name) == DOOM_LOOP_READONLY_FUSE

    def test_write_tools_keep_table_limits(self):
        assert doom_loop_limit("bash") == 3
        assert doom_loop_limit("apply_patch") == 3
        assert doom_loop_limit("write_file") == 8
        assert doom_loop_limit("send_message") == 5
        assert doom_loop_limit("commit_turn") == 8

    def test_unlisted_tool_default_3(self):
        assert doom_loop_limit("some_unknown_tool") == 3
