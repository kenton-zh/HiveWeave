"""T9（2026-09-29）：同参重复升级处置 — 回归守卫。

病灶（S15 朝露，101 run 长项目实测）：`[SELF_REPEAT]` inbox 提醒发了多轮
毫无效果——上下文被裁剪线洗掉新鲜工具输出的结构病面前，软提醒的处置强度
不够。修法（重复侧升级，挂在同参重复检测点 = ToolExecutor.execute 入口的
单一漏斗）：
- 连续同参 ≥3 次 → 回执文本从提醒升级为**强处方**；
- 第 N 次起（默认 5，HIVEWEAVE_SELF_REPEAT_REFUSE_THRESHOLD 可调）→
  直接拒绝执行该调用（回执 = 强处方，算一次平台拒绝，不消耗上游）；
- 换参数立即恢复（连击清零）。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.tools import executor as exec_mod
from hiveweave.tools.executor import ToolExecutor


_ARGS = {"filePath": "src/a.py", "offset": 100}

_ERR_RESULT = {"success": False, "output": "", "error": "Error: boom"}
_OK_RESULT = {"success": True, "output": "file body here", "error": None}


@pytest.fixture(autouse=True)
def _clean_streaks():
    exec_mod.reset_self_repeat_streaks_for_tests()
    yield
    exec_mod.reset_self_repeat_streaks_for_tests()


# ── 连击计数 ──────────────────────────────────────────────────────────


class TestStreakCounting:
    def test_consecutive_same_args_accumulate(self) -> None:
        counts = [
            exec_mod._note_same_identity_call("agent-A", "read_file", _ARGS)
            for _ in range(4)
        ]
        assert counts == [1, 2, 3, 4]

    def test_param_change_resets(self) -> None:
        exec_mod._note_same_identity_call("agent-A", "read_file", _ARGS)
        exec_mod._note_same_identity_call("agent-A", "read_file", _ARGS)
        # 换参数立即恢复（计 1，不从旧连击延续）
        c = exec_mod._note_same_identity_call(
            "agent-A", "read_file", {"filePath": "src/b.py", "offset": 1}
        )
        assert c == 1

    def test_tool_change_resets(self) -> None:
        exec_mod._note_same_identity_call("agent-A", "read_file", _ARGS)
        c = exec_mod._note_same_identity_call("agent-A", "grep", {"pattern": "x"})
        assert c == 1

    def test_agents_independent(self) -> None:
        exec_mod._note_same_identity_call("agent-A", "read_file", _ARGS)
        exec_mod._note_same_identity_call("agent-A", "read_file", _ARGS)
        c = exec_mod._note_same_identity_call("agent-B", "read_file", _ARGS)
        assert c == 1

    def test_key_order_is_same_identity(self) -> None:
        """call_identity 规范化：键序不同 = 同一身份，不洗白也不误分。"""
        exec_mod._note_same_identity_call(
            "agent-A", "read_file", {"filePath": "x", "offset": 2}
        )
        c = exec_mod._note_same_identity_call(
            "agent-A", "read_file", {"offset": 2, "filePath": "x"}
        )
        assert c == 2

    def test_commit_turn_exempt(self) -> None:
        """commit_turn 是等待契约的合法连发，不计数。"""
        for _ in range(6):
            assert exec_mod._note_same_identity_call(
                "agent-A", "commit_turn", {"phase": "waiting"}
            ) == 0


class TestThresholds:
    def test_default_thresholds(self) -> None:
        assert exec_mod.self_repeat_action(1) == "allow"
        assert exec_mod.self_repeat_action(2) == "allow"
        assert exec_mod.self_repeat_action(3) == "escalate"
        assert exec_mod.self_repeat_action(4) == "escalate"
        assert exec_mod.self_repeat_action(5) == "refuse"

    def test_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("HIVEWEAVE_SELF_REPEAT_REFUSE_THRESHOLD", "7")
        assert exec_mod.self_repeat_action(6) == "escalate"
        assert exec_mod.self_repeat_action(7) == "refuse"

    def test_env_below_escalate_is_clamped(self, monkeypatch) -> None:
        """env < 3 被 clamp 到 4——处方带（3）永远存在。"""
        monkeypatch.setenv("HIVEWEAVE_SELF_REPEAT_REFUSE_THRESHOLD", "2")
        assert exec_mod._self_repeat_refuse_threshold() == 4
        assert exec_mod.self_repeat_action(3) == "escalate"
        assert exec_mod.self_repeat_action(4) == "refuse"

    def test_env_junk_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("HIVEWEAVE_SELF_REPEAT_REFUSE_THRESHOLD", "junk")
        assert exec_mod._self_repeat_refuse_threshold() == 5


# ── 处方文案 ──────────────────────────────────────────────────────────


class TestPrescription:
    def test_mentions_count_tool_and_next_steps(self) -> None:
        text = exec_mod.self_repeat_prescription(3, "read_file")
        assert "3" in text and "read_file" in text
        assert "换方法" in text
        assert "send_message" in text
        assert "禁止" in text

    def test_refusal_variant_states_not_executed(self) -> None:
        text = exec_mod.self_repeat_prescription(5, "read_file", refused=True)
        assert "拒绝执行" in text
        assert "未运行" in text


# ── execute() 集成：第 5 次拒绝执行 ───────────────────────────────────


class _Allow:
    async def evaluate_detailed(self, agent_id, tool_name, args):
        return ("allow", None)


async def _read_note(ex: ToolExecutor, ws: str) -> dict:
    """对真实注册工具 read_file 发起一次同参调用（S15 病灶原型）。"""
    return await ex.execute(
        "a1", "read_file", {"filePath": "note.txt"}, ws
    )


@pytest.mark.asyncio
async def test_fifth_identical_call_is_refused(tmp_path):
    """1-4 次正常执行；第 5 次同参被拒、工具不运行。"""
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    ex = ToolExecutor(_Allow(), object())
    ws = str(tmp_path)
    receipts = [await _read_note(ex, ws) for _ in range(5)]
    for r in receipts[:4]:
        assert r["success"] is True, r
        assert "拒绝执行" not in (r.get("output") or "")
    refused = receipts[4]
    assert refused["success"] is False
    assert refused["blocked"] is True
    assert refused["fact"] == "runner_failed"
    assert "SELF_REPEAT ESCALATED" in (refused.get("error") or "")
    assert "拒绝执行" in (refused.get("error") or "")


@pytest.mark.asyncio
async def test_param_change_immediately_recovers(tmp_path):
    """换参数立即恢复：不再拒绝，回执回到正常执行。"""
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "other.txt").write_text("world", encoding="utf-8")
    ex = ToolExecutor(_Allow(), object())
    ws = str(tmp_path)
    for _ in range(5):
        await _read_note(ex, ws)
    r = await ex.execute(
        "a1", "read_file", {"filePath": "other.txt"}, ws
    )
    assert r["success"] is True
    assert "world" in (r.get("output") or "")
    assert "SELF_REPEAT" not in (r.get("output") or "")


@pytest.mark.asyncio
async def test_third_and_fourth_calls_escalate_receipt(tmp_path):
    """3/4 次：仍执行，但回执附加强处方；1/2 次回执干净。"""
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    ex = ToolExecutor(_Allow(), object())
    ws = str(tmp_path)
    outputs = [(await _read_note(ex, ws)).get("output") or "" for _ in range(4)]
    assert all("SELF_REPEAT" not in s for s in outputs[:2]), "1/2 次不应有处方"
    assert "SELF_REPEAT ESCALATED" in outputs[2]
    assert "SELF_REPEAT ESCALATED" in outputs[3]
    assert "hello" in outputs[3], "处方追加，原文保留"


# ── _f10_result_hooks 升级带（成功路径也要能升级——S15 正是成功读重复）──


@pytest.mark.asyncio
async def test_hooks_append_prescription_on_success_at_count_3():
    out = await exec_mod._f10_result_hooks(
        dict(_OK_RESULT), "read_file", _ARGS, "agent-A", repeat_count=3
    )
    assert out["success"] is True
    assert "SELF_REPEAT ESCALATED" in out["output"]
    assert "file body here" in out["output"], "原文必须保留"


@pytest.mark.asyncio
async def test_hooks_no_prescription_below_escalate():
    out = await exec_mod._f10_result_hooks(
        dict(_OK_RESULT), "read_file", _ARGS, "agent-A", repeat_count=2
    )
    assert out["output"] == "file body here"


@pytest.mark.asyncio
async def test_hooks_no_prescription_when_count_not_passed():
    """缺省 repeat_count=None ⇒ 旧行为一字不变（既有调用方零感知）。"""
    out = await exec_mod._f10_result_hooks(
        dict(_OK_RESULT), "read_file", _ARGS, "agent-A"
    )
    assert out["output"] == "file body here"


@pytest.mark.asyncio
async def test_hooks_prescription_goes_to_output_error_untouched():
    """处方只进 output；error 逐字不动（回执纪律 + AST 守卫钉死）。"""
    result = {"success": False, "output": "", "error": "Error: boom"}
    out = await exec_mod._f10_result_hooks(
        result, "read_file", _ARGS, "agent-A", repeat_count=3
    )
    assert out["error"] == "Error: boom"
    assert "SELF_REPEAT ESCALATED" in out["output"]


# ── 失败路径：SELF_REPEAT inbox 提醒文案升级 ─────────────────────────


@pytest.mark.asyncio
async def test_failure_notice_text_upgrades_to_prescription():
    """连击 ≥3 时，失败复撞的 inbox 提醒正文升级为强处方。"""
    from hiveweave.services import failure_signature as fsig

    delivered: list[dict] = []

    async def _fake_deliver(agent_id, text, *, kind, **kw):
        delivered.append({"text": text, "kind": kind})
        return True

    async def _fake_record(**kw):
        return {
            "written": True, "preexisting": True,
            "sig": "SIG|STABLE|SIGNATURE|TEXT|THAT|IS|LONG|ENOUGH|XX",
            "module_id": "mid-1",
        }

    async def _fake_notice(*a, **kw):
        return ("[shared fix] sol text", fsig.NOTICE_TIER_SOLUTION)

    async def _fake_org(**kw):
        return ""

    with (
        patch("hiveweave.tools.executor.deliver_notice", _fake_deliver),
        patch("hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj-1")),
        patch.object(fsig, "record_failure_signature", _fake_record),
        patch.object(fsig, "known_signature_notice", _fake_notice),
        patch.object(fsig, "note_distinct_hitter", _fake_org),
        patch.object(exec_mod, "_LAST_SEEN_SIG_WINDOW_S", 10 ** 9),
    ):
        exec_mod.reset_self_repeat_hits_for_tests()
        # 模拟 execute() 的登记口径：连击 3 次后撞同一失败
        for _ in range(3):
            exec_mod._note_same_identity_call("agent-A", "bash", {"a": 1})
        # 首撞登记（不发 SELF REPEAT，TEST_DSH_47 #6 语义），复撞才发
        await exec_mod._f10_result_hooks(
            dict(_ERR_RESULT), "bash", {"a": 1}, "agent-A", repeat_count=3
        )
        await exec_mod._f10_result_hooks(
            dict(_ERR_RESULT), "bash", {"a": 1}, "agent-A", repeat_count=3
        )

    selves = [d for d in delivered if d["kind"] == "SELF_REPEAT"]
    assert selves, "复撞必须仍有 SELF_REPEAT 提醒"
    assert "SELF_REPEAT ESCALATED" in selves[-1]["text"]
    assert "该路径无效" in selves[-1]["text"]


@pytest.mark.asyncio
async def test_failure_notice_keeps_legacy_wording_below_escalate():
    """repeat_count=None（未传）⇒ 提醒文案维持旧措辞（既有断言不破）。"""
    sig = "SIG|STABLE|SIGNATURE|TEXT|THAT|IS|LONG|ENOUGH|XX"
    exec_mod.reset_self_repeat_hits_for_tests()
    first = exec_mod._note_self_repeat_hit("agent-A", "bash", sig)
    assert first == ""
    second = exec_mod._note_self_repeat_hit("agent-A", "bash", sig)
    assert "刚撞过同一失败" in second
    assert "ESCALATED" not in second


# ── 拒绝不算工具失败（不进共享签名池 / 不跑 F10 失败钩子）─────────────


@pytest.mark.asyncio
async def test_refusal_does_not_touch_failure_signature_pool(tmp_path):
    """拒绝路径在 hooks 之前返回：record_failure_signature 不被调用。"""
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    ex = ToolExecutor(_Allow(), object())
    ws = str(tmp_path)
    for _ in range(4):
        assert (await _read_note(ex, ws))["success"] is True
    with patch(
        "hiveweave.services.failure_signature.record_failure_signature",
        new=AsyncMock(),
    ) as mock_rec:
        r = await _read_note(ex, ws)
        assert r.get("blocked") is True
        # 拒绝是平台拒绝，不是工具失败签名（不污染共享池）
        mock_rec.assert_not_awaited()


@pytest.mark.asyncio
async def test_refusal_receipt_is_wire_clean(tmp_path):
    """拒绝回执可安全序列化进 tool 消息（streaming 桥接层消费形态）。"""
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    ex = ToolExecutor(_Allow(), object())
    ws = str(tmp_path)
    r: dict = {}
    for _ in range(5):
        r = await _read_note(ex, ws)
    assert isinstance(r["error"], str)
    json.dumps(r, ensure_ascii=False, default=str)  # 不可序列化 = 桥接层会炸
