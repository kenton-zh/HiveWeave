"""批29 N4（2026-10-09）：SELF_REPEAT 跨 run 累计拒绝击杀 — 回归守卫。

病灶（第六轮 s24 磐石实测）：12:56-14:34 以完全相同参数轮询
halyard/core/store.py 70 次（25 放行 + 45 拒绝）仍不终止 —— 模型学会了
「等 10 分钟窗口衰减 → 原样重试」：T9 连击是 per-agent 单槽 + 600s 窗，
窗口一滑清零重计；run 级 doom-loop 击杀只覆盖单 run ×3，轮询分散在多 run
永不触发。

修法（与既有 ≥3/≥5 语义兼容）：
- per-(agent_id, args_hash) 累计拒绝计数，**跨窗口保留**（窗口只控制
  streak 的连续性判定/升级节奏，不控制累计总量）；
- 累计 ≥ HIVEWEAVE_SELF_REPEAT_CUMULATIVE_KILL（默认 12）⇒ 击杀：回执
  升级为「此参数组合已死——禁止再用」并带 self_repeat_kill 标记；
- 已死后同参调用**即视即杀**（新 run 首撞即死，不必再等 streak 攒到 5）；
- 击杀标记经 streaming 桥接 → _execute_tools（搭 tool_results 顺风车）→
  tool_loop 复用 doom-loop 击杀路径收口整个 run（status=error）。

边界（如实登记）：累计计数纯内存（进程重启失忆），容量有界。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hiveweave.llm.streamer.core import Streamer
from hiveweave.tools import executor as exec_mod
from hiveweave.tools.executor import ToolExecutor

_ARGS = {"filePath": "note.txt"}


class _Allow:
    async def evaluate_detailed(self, agent_id, tool_name, args):
        return ("allow", None)


@pytest.fixture(autouse=True)
def _clean_repeat_memory():
    exec_mod.reset_self_repeat_streaks_for_tests()
    exec_mod.reset_self_repeat_cumulative_for_tests()
    yield
    exec_mod.reset_self_repeat_streaks_for_tests()
    exec_mod.reset_self_repeat_cumulative_for_tests()


# ── 累计计数（跨 run / 跨窗口保留的单元面）────────────────────────────


class TestCumulativeCounting:
    def test_refusals_accumulate_and_survive_streak_reset(self) -> None:
        """等衰减绕过的核心断言：streak 清零（窗口滑动）不清累计。"""
        counts = [
            exec_mod._note_self_repeat_refusal("a1", "read_file", _ARGS)
            for _ in range(3)
        ]
        assert counts == [1, 2, 3]
        # 窗口滑动 / 新 run：streak 清零，累计保留
        exec_mod.reset_self_repeat_streaks_for_tests()
        assert exec_mod._peek_self_repeat_cumulative("a1", "read_file", _ARGS) == 3
        assert exec_mod._note_self_repeat_refusal("a1", "read_file", _ARGS) == 4

    def test_peek_does_not_increment(self) -> None:
        for _ in range(2):
            exec_mod._note_self_repeat_refusal("a1", "read_file", _ARGS)
        for _ in range(3):
            assert exec_mod._peek_self_repeat_cumulative("a1", "read_file", _ARGS) == 2

    def test_exempt_tool_not_counted(self) -> None:
        for _ in range(3):
            assert (
                exec_mod._note_self_repeat_refusal(
                    "a1", "commit_turn", {"phase": "waiting"}
                )
                == 0
            )

    def test_different_args_and_agents_isolated(self) -> None:
        exec_mod._note_self_repeat_refusal("a1", "read_file", _ARGS)
        assert (
            exec_mod._peek_self_repeat_cumulative(
                "a1", "read_file", {"filePath": "other.txt"}
            )
            == 0
        )
        assert exec_mod._peek_self_repeat_cumulative("a2", "read_file", _ARGS) == 0

    def test_key_order_is_same_combo(self) -> None:
        exec_mod._note_self_repeat_refusal(
            "a1", "read_file", {"filePath": "x", "offset": 2}
        )
        assert (
            exec_mod._peek_self_repeat_cumulative(
                "a1", "read_file", {"offset": 2, "filePath": "x"}
            )
            == 1
        )


# ── 阈值 env ④ ────────────────────────────────────────────────────────


class TestKillThresholdEnv:
    def test_default_is_twelve(self) -> None:
        assert exec_mod._self_repeat_cumulative_kill_threshold() == 12

    def test_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("HIVEWEAVE_SELF_REPEAT_CUMULATIVE_KILL", "3")
        assert exec_mod._self_repeat_cumulative_kill_threshold() == 3

    def test_env_junk_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("HIVEWEAVE_SELF_REPEAT_CUMULATIVE_KILL", "junk")
        assert exec_mod._self_repeat_cumulative_kill_threshold() == 12

    def test_env_nonpositive_clamped_to_one(self, monkeypatch) -> None:
        monkeypatch.setenv("HIVEWEAVE_SELF_REPEAT_CUMULATIVE_KILL", "0")
        assert exec_mod._self_repeat_cumulative_kill_threshold() == 1

    def test_kill_prescription_text(self) -> None:
        text = exec_mod.self_repeat_kill_prescription("read_file", 12)
        assert "SELF_REPEAT KILL" in text
        assert "禁止再用" in text
        assert "12" in text and "read_file" in text
        assert "send_message" in text


# ── execute() 集成 ①②③④ ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_twelve_refusals_across_three_runs_kill_on_twelfth(tmp_path):
    """① 同参跨 3 个 run 累计 12 次拒绝 ⇒ 第 12 次击杀。

    每 run 8 次同参调用（1-4 放行 / 5-8 拒绝）⇒ 每 run 4 次拒绝；
    run 间清 streak（模拟窗口衰减 + 新 run），累计不清。
    """
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    ex = ToolExecutor(_Allow(), object())
    ws = str(tmp_path)

    plain_refusals = 0
    kill_receipts: list[dict] = []
    for _run in range(3):
        for _ in range(8):
            r = await ex.execute("a1", "read_file", dict(_ARGS), ws)
            if r.get("blocked"):
                if r.get("self_repeat_kill"):
                    kill_receipts.append(r)
                else:
                    plain_refusals += 1
        # 新 run + 窗口衰减：streak 清零（累计保留——被杀不了正是本修法）
        exec_mod.reset_self_repeat_streaks_for_tests()

    assert plain_refusals == 11, "前 11 次拒绝不应击杀"
    assert len(kill_receipts) == 1, "第 12 次拒绝必须击杀"
    kr = kill_receipts[0]
    assert kr["success"] is False
    assert "SELF_REPEAT KILL" in (kr.get("error") or "")
    assert "禁止再用" in (kr.get("error") or "")
    assert kr["fact"] == "runner_failed"
    assert kr.get("output") == "", "击杀回执不得携带执行输出（工具未运行）"


@pytest.mark.asyncio
async def test_window_decay_preserves_cumulative_and_keeps_incrementing(
    tmp_path, monkeypatch
):
    """② 等 600s+ 窗口衰减后重试 ⇒ 累计计数保留、仍递增（非清零重计）。

    窗口缩到 50ms + 真实间隔 200ms ⇒ 必然滑动；衰减后连击从 1 重来
    （1-4 放行），第 5 次再拒 ⇒ 累计 2→3，而不是从 1 重数。
    """
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    ex = ToolExecutor(_Allow(), object())
    ws = str(tmp_path)

    # 第一窗口：6 次连击 → 第 5/6 次拒绝（累计 2）
    for _ in range(6):
        await ex.execute("a1", "read_file", dict(_ARGS), ws)
    assert exec_mod._peek_self_repeat_cumulative("a1", "read_file", _ARGS) == 2

    # 「等 600s+」：窗口压缩 + 真实时间流逝
    monkeypatch.setattr(exec_mod, "_SELF_REPEAT_STREAK_WINDOW_S", 0.05)
    await asyncio.sleep(0.2)

    # 衰减不清累计
    assert exec_mod._peek_self_repeat_cumulative("a1", "read_file", _ARGS) == 2

    # 衰减后重试：第 4 次连击仍放行（streak 已清零），第 5 次再拒
    receipts = [
        await ex.execute("a1", "read_file", dict(_ARGS), ws) for _ in range(5)
    ]
    assert receipts[3]["success"] is True, "衰减后 streak 从 1 重来，第 4 次应放行"
    assert receipts[4].get("blocked") is True
    assert not receipts[4].get("self_repeat_kill")
    assert exec_mod._peek_self_repeat_cumulative("a1", "read_file", _ARGS) == 3, (
        "累计必须跨窗口保留且仍递增（2→3），不得清零重计"
    )


@pytest.mark.asyncio
async def test_dead_combo_killed_on_first_call_of_new_run(tmp_path):
    """已死组合即视即杀：累计过阈后，新 run 首次同参调用（streak=1）直接击杀。"""
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    ex = ToolExecutor(_Allow(), object())
    ws = str(tmp_path)

    # 组合已被累计拒绝 12 次（跨 run 的平台拒绝历史）
    for _ in range(12):
        exec_mod._note_self_repeat_refusal("a1", "read_file", dict(_ARGS))
    exec_mod.reset_self_repeat_streaks_for_tests()  # 新 run，streak 从 1 起

    r = await ex.execute("a1", "read_file", dict(_ARGS), ws)
    assert r["success"] is False
    assert r.get("self_repeat_kill") is True
    assert "SELF_REPEAT KILL" in (r.get("error") or "")
    assert r.get("output") == "", "已死组合不得再执行工具"


@pytest.mark.asyncio
async def test_other_args_and_other_agent_not_implicated(tmp_path):
    """③ 不同参数 / 不同 agent 不受牵连。"""
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "other.txt").write_text("world", encoding="utf-8")
    ex = ToolExecutor(_Allow(), object())
    ws = str(tmp_path)

    for _ in range(12):
        exec_mod._note_self_repeat_refusal("a1", "read_file", dict(_ARGS))

    ok = await ex.execute("a1", "read_file", {"filePath": "other.txt"}, ws)
    assert ok["success"] is True
    assert "world" in (ok.get("output") or "")

    other_agent = await ex.execute("a2", "read_file", dict(_ARGS), ws)
    assert other_agent["success"] is True
    assert "hello" in (other_agent.get("output") or "")


@pytest.mark.asyncio
async def test_env_lowered_threshold_kills_earlier(tmp_path, monkeypatch):
    """④ env 阈值可调：击杀阈值 3 ⇒ 第 3 次累计拒绝（第 7 次调用）即杀。"""
    monkeypatch.setenv("HIVEWEAVE_SELF_REPEAT_CUMULATIVE_KILL", "3")
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    ex = ToolExecutor(_Allow(), object())
    ws = str(tmp_path)
    receipts = [
        await ex.execute("a1", "read_file", dict(_ARGS), ws) for _ in range(7)
    ]
    # 拒绝自第 5 次调用起：第 5/6 次拒绝为普通拒绝，第 7 次（累计 3）击杀
    assert receipts[4].get("blocked") is True
    assert not receipts[4].get("self_repeat_kill")
    assert receipts[5].get("blocked") is True
    assert not receipts[5].get("self_repeat_kill")
    assert receipts[6].get("self_repeat_kill") is True


# ── 击杀信号管道：_execute_tools 搭 tool_results 顺风车 ───────────────


class _FakeProvider:
    provider_type = "fake"
    model_name = "fake-model"
    fallback = None
    max_output_tokens = 4096
    supports_thinking = False
    context_window = 128_000


def _make_streamer() -> Streamer:
    provider_factory = MagicMock()
    provider_factory.create.return_value = _FakeProvider()
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(return_value=MagicMock(allowed=True, fallback=None))
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()
    streamer = Streamer(
        provider_factory_inst=provider_factory,
        circuit_breaker_inst=breaker,
        retry_handler=MagicMock(),
    )
    streamer._trim_context_if_needed = lambda messages, provider: messages  # type: ignore[method-assign]

    async def _ident_pressure(messages, provider, **kwargs):
        return messages

    streamer._pressure_compact_if_needed = _ident_pressure  # type: ignore[method-assign]
    streamer._maybe_inject_mid_round_reminder = (  # type: ignore[method-assign]
        lambda messages, round_num, cap: messages
    )
    return streamer


def _tool_round(tools: list[dict]) -> dict:
    return {
        "status": "ok",
        "text": "",
        "thinking": "",
        "tool_calls": tools,
        "finish_reason": "tool_calls",
        "usage": None,
    }


def _text_round(text: str) -> dict:
    return {
        "status": "ok",
        "text": text,
        "thinking": "",
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": None,
    }


async def test_execute_tools_attaches_kill_reason_to_its_own_msg():
    """击杀信号以 self_repeat_kill_reason 键搭 tool_results，返回元数不变。"""
    streamer = _make_streamer()
    calls = [
        {"id": "k1", "name": "bash", "arguments": "{}"},
        {"id": "ok1", "name": "read_file", "arguments": "{}"},
    ]

    async def on_tool_call(name, args, tid):
        # 模拟 streaming 桥接的输出形态（content = Error: + 处方）
        if tid == "k1":
            return {
                "role": "tool", "content": "Error: [SELF_REPEAT KILL] 已死",
                "tool_call_id": tid, "success": False, "blocked": True,
                "self_repeat_kill": True,
            }
        return {
            "role": "tool", "content": "fine",
            "tool_call_id": tid, "success": True,
        }

    tool_results, error_ids, blocked_ids, duplicate_ids, end_turn = (
        await streamer._execute_tools(
            agent_id="a1", tool_calls=calls,
            on_tool_call=on_tool_call, on_delta=None,
        )
    )

    assert end_turn is False  # 5 元返回契约不变（既有调用方/测试零感知）
    kill_msgs = [
        m for m in tool_results if m.get("self_repeat_kill_reason")
    ]
    assert len(kill_msgs) == 1
    assert kill_msgs[0]["tool_call_id"] == "k1"
    assert "SELF_REPEAT KILL" in kill_msgs[0]["self_repeat_kill_reason"]
    assert "t1" not in blocked_ids and "k1" in blocked_ids
    assert error_ids == {"k1"}
    assert duplicate_ids == set()


async def test_tool_loop_kills_run_on_self_repeat_kill_receipt():
    """tool_loop 读到击杀信号 ⇒ 复用 doom-loop 击杀路径 run 级收口（不再发下一轮）。"""
    streamer = _make_streamer()
    stream_calls = 0

    async def fake_stream(*, messages, **kwargs):
        nonlocal stream_calls
        stream_calls += 1
        if stream_calls == 1:
            return _tool_round(
                [{"id": "t1", "name": "bash", "arguments": "{}"}]
            )
        return _text_round("should not reach")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    kill_text = "Error: [SELF_REPEAT KILL] 此参数组合已死——禁止再用"
    streamer._execute_tools = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [{
                "role": "tool", "content": kill_text,
                "tool_call_id": "t1",
                "self_repeat_kill_reason": kill_text,
            }],
            {"t1"},
            {"t1"},
            set(),
            False,
        )
    )

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )

    assert stream_calls == 1, "击杀后不得再发起下一轮 LLM 请求"
    assert result["status"] == "error"
    assert "SELF_REPEAT KILL" in (result.get("error") or "")
