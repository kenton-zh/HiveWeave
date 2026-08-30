"""Unit tests for TEST6 audit platform fixes (P0/P1 mechanism layer)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from hiveweave.services.attestation import (
    count_reported_test_failures,
    required_failure_acks,
)
from hiveweave.services.health_supervisor import (
    GAME_TIME_STALE_S,
    HealthSupervisor,
    _WAITING_DISPOSITIONS,
)
from hiveweave.services.inbox import (
    filter_actionable_pending,
    inbox_digest_content,
    is_fyi_task_event,
    unwrap_user_message_envelope,
)
from hiveweave.services.process_registry import (
    _inject_hiveweave_test_exclude,
    prepare_spawn_command,
)
from hiveweave.tools.browse_tools import _map_ab_argv


class TestPendingFilter:
    def test_filters_task_event(self):
        msgs = [
            {"id": "1", "message_type": "task"},
            {"id": "2", "message_type": "task_event"},
            {"id": "3", "message_type": "ask"},
            {"id": "4", "message_type": "TASK_EVENT"},
        ]
        out = filter_actionable_pending(msgs)
        assert [m["id"] for m in out] == ["1", "3"]

    def test_is_fyi(self):
        assert is_fyi_task_event({"message_type": "task_event"})
        assert not is_fyi_task_event({"message_type": "task"})
        assert not is_fyi_task_event(None)


class TestFailureCount:
    def test_vitest_style(self):
        assert count_reported_test_failures("Tests 179 passed, 30 failed") == 30

    def test_pytest_style(self):
        assert count_reported_test_failures("===== 3 failed, 10 passed =====") == 3

    def test_none_when_absent(self):
        assert count_reported_test_failures("all green") is None
        assert count_reported_test_failures("") is None
        assert count_reported_test_failures(None) is None

    def test_failed_eq(self):
        assert count_reported_test_failures("failed=2 suite done") == 2


class TestSpawnExclude:
    def test_vitest_gets_exclude(self):
        out, note = _inject_hiveweave_test_exclude("npx vitest run")
        assert "--exclude" in out
        assert ".hiveweave" in out
        assert note and "[platform injected]" in note

    def test_pytest_gets_ignore(self):
        out, note = _inject_hiveweave_test_exclude("pytest -q")
        assert "--ignore=.hiveweave" in out
        assert note and "[platform injected]" in note

    def test_prepare_spawn_rewrites(self):
        cmd, env, err, inj = prepare_spawn_command("vitest run", project_id="p1")
        assert err is None
        assert "--exclude" in cmd
        assert "HIVEWEAVE_RESERVED_PORTS" in env
        assert inj is not None and inj.get("injected")

    def test_idempotent_when_already_excluded(self):
        raw = "vitest run --exclude **/.hiveweave/**"
        assert _inject_hiveweave_test_exclude(raw) == (raw, None)

    # F2：含管道/分号且无法定位 runner token → 放弃注入并告警
    def test_pipe_runner_token_kept_without_pollution(self):
        # runner token 可定位 → flag 仍插在 runner 后，不落在管道下游
        out, note = _inject_hiveweave_test_exclude(
            "pytest tests/ | Select-Object -First 5"
        )
        assert out.index("--ignore=") < out.index("|") if "--ignore=" in out else True
        # pytest runner 在管道前，_insert_after_runner 能找到 → 注入成功
        assert "--ignore=.hiveweave" in out
        assert note

    def test_pipe_unknown_runner_skips_injection(self):
        # mock runner 无法定位（不匹配 pytest/vitest/jest）→ 原样返回 + 告警
        cmd, note = _inject_hiveweave_test_exclude("ptest ./foo | wc -l")
        assert cmd == "ptest ./foo | wc -l"
        # 不能定位 runner —— 上面的命令本身不含 pytest/vitest/jest，
        # 因此不进注入路径；此处验证「含管道但不匹配任何 runner」返回 (cmd, None)
        assert note is None

    def test_unknown_runner_with_pipe_no_pollution(self):
        # 直接调用内部语义：即使 flag 注入路径被触发，管道命令也不会被 tail 污染。
        # 该场景由 vitest/jest/pytest 之外的测试命令触发 —— 行为是原样返回。
        cmd, note = _inject_hiveweave_test_exclude("mocha test/ | cat")
        assert cmd == "mocha test/ | cat"


class TestBrowseInlineJs:
    def test_inline_passes_through_as_expression(self):
        # Regression (shoulabridge audit bug #3): inline JS must be passed as
        # an expression to agent-browser `eval`, NOT materialised to a
        # tempfile whose *path* the old CLI evaluated (evaluate 1+1 returned
        # the path string).
        argv, stdin = _map_ab_argv(
            ["js", "() => document.body.click()"],
            workspace="",
        )
        assert argv == ["eval", "() => document.body.click()"]
        assert stdin is None

    def test_existing_file_preserved_as_eval(self):
        # `eval <path>` to an existing file reads its content in — the file
        # must not be evaluated as a literal path string.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False, encoding="utf-8"
        ) as f:
            f.write("1+1")
            path = f.name
        try:
            argv, stdin = _map_ab_argv(["eval", path], workspace="")
            assert argv[0] == "eval"
            assert argv[1] == "1+1"
            assert stdin is None
        finally:
            os.unlink(path)


class TestUserMessageEnvelopeUnwrap:
    """Busy-queue must not double-encode user text in trigger digests."""

    def test_unwraps_legacy_envelope(self):
        raw = '{"from": "用户", "content": "全部都要修复  但是你只负责测试"}'
        assert (
            unwrap_user_message_envelope(
                raw, from_agent_id="用户", message_type="user_message"
            )
            == "全部都要修复  但是你只负责测试"
        )

    def test_plain_text_passthrough(self):
        assert (
            unwrap_user_message_envelope(
                "继续 playtest", from_agent_id="用户", message_type="user_message"
            )
            == "继续 playtest"
        )

    def test_peer_json_not_unwrapped(self):
        peer = '{"from": "川流", "content": "收到"}'
        assert (
            unwrap_user_message_envelope(
                peer, from_agent_id="fc2b588c", message_type="normal"
            )
            == peer
        )

    def test_digest_helper_on_msg(self):
        msg = {
            "from_agent_id": "用户",
            "message_type": "user_message",
            "message": '{"from": "用户", "content": "只负责测试"}',
        }
        assert inbox_digest_content(msg) == "只负责测试"

    def test_digest_line_single_layer(self):
        import json

        content = inbox_digest_content(
            {
                "from_agent_id": "用户",
                "message_type": "user_message",
                "message": '{"from": "用户", "content": "汇报领导吧"}',
            }
        )
        line = json.dumps(
            {"from": "用户", "content": content, "message_type": "user_message"},
            ensure_ascii=False,
        )
        parsed = json.loads(line)
        assert parsed["content"] == "汇报领导吧"
        assert not parsed["content"].startswith("{")


class TestFailureAckRequirement:
    def test_required_matches_fail_n_under_cap(self):
        assert required_failure_acks(3) == 3
        assert required_failure_acks(1) == 1

    def test_required_caps_at_20(self):
        assert required_failure_acks(30) == 20
        assert required_failure_acks(100) == 20

    def test_single_ack_insufficient_for_30(self):
        # Documents the audit fix: 1 ack must not unlock 30 failures
        fail_n = count_reported_test_failures("Tests 179 passed, 30 failed")
        assert fail_n == 30
        assert required_failure_acks(fail_n) > 1


class TestHealthSupervisorExemptions:
    def test_waiting_dispositions_aligned(self):
        assert "waiting_human" in _WAITING_DISPOSITIONS
        assert "blocked" in _WAITING_DISPOSITIONS

    def test_game_time_stale_threshold(self):
        assert GAME_TIME_STALE_S == 120

    @pytest.mark.asyncio
    async def test_skip_processing_agent(self, monkeypatch):
        hs = HealthSupervisor()

        class _Mgr:
            def list_processing(self):
                return [("a1", "p1")]

            def get_agent(self, _aid):
                return None

        monkeypatch.setattr(
            "hiveweave.agents.supervisor.agent_manager",
            _Mgr(),
            raising=False,
        )
        # Import path used inside method
        import hiveweave.agents.supervisor as sup

        monkeypatch.setattr(sup, "agent_manager", _Mgr())
        assert await hs._should_skip_agent("a1", "p1", int(__import__("time").time() * 1000)) is True


class TestFuseDoesNotAckActionable:
    def test_fuse_escalation_method_exists(self):
        from hiveweave.agents.agent import Agent

        assert hasattr(Agent, "_escalate_trigger_fuse")

    def test_watcher_source_no_mark_read_on_fuse(self):
        """Regression: fuse path must not mark_read actionable pending."""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hiveweave"
            / "agents"
            / "watcher.py"
        )
        text = src.read_text(encoding="utf-8")
        idx = text.find("inbox_watcher_trigger_fuse_escalated")
        assert idx > 0
        window = text[idx : idx + 900]
        assert "mark_read_by_ids" not in window
        # Escalation contract text lives on Agent._escalate_trigger_fuse
        agent_src = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hiveweave"
            / "agents"
            / "recovery.py"
        )
        agent_text = agent_src.read_text(encoding="utf-8")
        assert "Inbox was NOT auto-acked" in agent_text or (
            "Inbox was NOT auto-acked" in text
        )