"""Doom loop 检测 — BUGFIX #1 commit_turn 阈值 + 豁免收窄。

回归场景（井字棋实测）：CEO 每轮必须 commit_turn 收工；出口闸门拒收后
LLM 以相同参数重试 → 默认阈值 3 触发 doom → 首条指令即 [ERROR]，
无任何正常输出。

修复：
1. commit_turn 专属阈值（89a030a 定 6；32597f3 调整为 8 —— 同参指纹
   才计数，强制出口工具需要更大容忍度，本测试以 8 为准）
2. 豁免收窄（修 #1）：同参数连续调用始终计数。合法重试路径是"失败后
   改参数再调"——不同参数走 else 分支重置 count=1。同参数重试不豁免。
"""

from __future__ import annotations

from hiveweave.llm.streamer import Streamer


def _tc(name: str, args: str = '{"phase":"done_slice"}') -> dict:
    return {"id": "call-1", "name": name, "arguments": args}


def _tracker() -> dict:
    return {"last_key": None, "count": 0}


class TestCommitTurnThreshold:
    def test_commit_turn_tolerates_retries_up_to_8(self):
        """commit_turn 连续 7 次同参数不触发（默认阈值 3 会误杀）。"""
        tracker = _tracker()
        for _ in range(7):
            hit = Streamer._detect_doom_loop([_tc("commit_turn")], tracker)
            assert hit is None

    def test_commit_turn_trips_at_8(self):
        """阈值 8 以 32597f3 为准（同参指纹计数，强制出口容忍度上调 6→8）。"""
        tracker = _tracker()
        hit = None
        for _ in range(8):
            hit = Streamer._detect_doom_loop([_tc("commit_turn")], tracker)
        assert hit == "commit_turn"

    def test_default_limit_still_3_for_unlisted_tool(self):
        tracker = _tracker()
        hit = None
        for _ in range(3):
            hit = Streamer._detect_doom_loop([_tc("some_tool")], tracker)
        assert hit == "some_tool"


class TestFailureRetryNarrowed:
    def test_retry_after_error_still_counts(self):
        """修 #1: 执行失败后的同参数重试仍计 doom —— 豁免已收窄。

        合法重试路径是"改参数再调"，同参数重试是 doom loop 的典型模式。
        """
        tracker = _tracker()
        # 第 1 次调用（count=1）→ 执行失败
        Streamer._detect_doom_loop([_tc("commit_turn")], tracker)
        # 失败后的同参数重试：count 照常 +1（不再豁免）
        Streamer._detect_doom_loop([_tc("commit_turn")], tracker)
        assert tracker["count"] == 2

    def test_different_args_resets_after_error(self):
        """失败后改参数重试 → count 重置为 1（合法重试路径保留）。"""
        tracker = _tracker()
        Streamer._detect_doom_loop([_tc("bash", '{"command":"ls"}')], tracker)
        # 改参数重试 → count 重置
        Streamer._detect_doom_loop([_tc("bash", '{"command":"pwd"}')], tracker)
        assert tracker["count"] == 1

    def test_different_args_resets(self):
        tracker = _tracker()
        Streamer._detect_doom_loop([_tc("read_file", '{"path":"a"}')], tracker)
        Streamer._detect_doom_loop([_tc("read_file", '{"path":"b"}')], tracker)
        assert tracker["count"] == 1
