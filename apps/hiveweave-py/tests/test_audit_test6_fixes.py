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
from hiveweave.tools.browse_tools import _materialize_inline_js


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
        out = _inject_hiveweave_test_exclude("npx vitest run")
        assert "--exclude" in out
        assert ".hiveweave" in out

    def test_pytest_gets_ignore(self):
        out = _inject_hiveweave_test_exclude("pytest -q")
        assert "--ignore=.hiveweave" in out

    def test_prepare_spawn_rewrites(self):
        cmd, env, err = prepare_spawn_command("vitest run", project_id="p1")
        assert err is None
        assert "--exclude" in cmd
        assert "HIVEWEAVE_RESERVED_PORTS" in env

    def test_idempotent_when_already_excluded(self):
        raw = "vitest run --exclude **/.hiveweave/**"
        assert _inject_hiveweave_test_exclude(raw) == raw


class TestBrowseInlineJs:
    def test_inline_passes_through_as_expression(self):
        # Regression (shoulabridge audit bug #3): inline JS must be passed to
        # `js` as an expression, NOT materialised to a tempfile whose *path*
        # gstack then evaluated (evaluate 1+1 returned the path string).
        argv = _materialize_inline_js(
            ["js", "() => document.body.click()"],
            workspace="",
        )
        assert argv == ["js", "() => document.body.click()"]

    def test_existing_file_preserved_as_eval(self):
        # `eval` reads a file path — a real file must stay `eval <path>`, not
        # be rewritten to `js` (old behavior evaluated the path string).
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False, encoding="utf-8"
        ) as f:
            f.write("1+1")
            path = f.name
        try:
            argv = _materialize_inline_js(["eval", path], workspace="")
            assert argv[0] == "eval"
            assert Path(argv[1]).resolve() == Path(path).resolve()
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