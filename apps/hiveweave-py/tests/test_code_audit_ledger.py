"""request_code_audit ledger contract tests.

Covers (contract with services/code_audit.py, developed in parallel):
- count_change_lines per-branch: write_file content lines; edit_file
  max(old,new); apply_patch mixed ops (add/update/delete/unknown);
  unknown tool -> 0; missing params -> 0
- per-agent ledger isolation
- reset_ledger zeroes
- ledger_snapshot copy semantics
- append_code_audit_notice idempotency (append once, second call no-op)

Ledger is global (no project_id in the API) — tests run against a temp
Meta DB. Imports are lazy so the file collects even before the parallel
service module lands.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    """Point the Meta DB at a temp path, (re)initialize it, and empty the
    in-memory change ledger (global dict — must not leak across tests)."""
    from hiveweave.db import meta as meta_db
    from hiveweave.services import code_audit

    for agent_id in code_audit.ledger_snapshot():
        code_audit.reset_ledger(agent_id)

    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    asyncio.run(meta_db.close_meta_db())
    asyncio.run(meta_db.init_meta_db())
    return meta_db


# ── count_change_lines 分支 ──────────────────────────────────


def test_count_write_file_content_lines():
    from hiveweave.services.code_audit import count_change_lines

    assert count_change_lines("write_file", {"content": "a\nb\nc"}) == 3
    assert count_change_lines("write_file", {"content": "single"}) == 1
    assert count_change_lines("write_file", {"content": ""}) == 0
    assert count_change_lines("write_file", {}) == 0


def test_count_edit_file_max_old_new():
    from hiveweave.services.code_audit import count_change_lines

    assert (
        count_change_lines(
            "edit_file",
            {"old_string": "a\nb", "new_string": "a\nb\nc\nd"},
        )
        == 4
    )
    assert count_change_lines("edit_file", {"old_string": "a\nb"}) == 2
    assert count_change_lines("edit_file", {"new_string": "a\nb\nc"}) == 3
    assert count_change_lines("edit_file", {}) == 0


def test_count_apply_patch_mixed_ops():
    from hiveweave.services.code_audit import count_change_lines

    params = {
        "patches": [
            {"op": "add", "content": "a\nb"},
            {"op": "update", "old_string": "x", "new_string": "y\nz"},
            {"op": "delete", "old_string": "d\n"},
            {"op": "bogus", "old_string": "q"},
        ]
    }
    assert count_change_lines("apply_patch", params) == 4


def test_count_unknown_tool_and_missing_params():
    from hiveweave.services.code_audit import count_change_lines

    assert count_change_lines("something_else", {"content": "a\nb"}) == 0
    assert count_change_lines("write_file", None) == 0


# ── 台账（全局，按 agent 隔离）───────────────────────────────


def test_ledger_isolation_per_agent(isolated_ledger):
    from hiveweave.services.code_audit import (
        get_unaudited_lines,
        record_change,
    )

    record_change("agent-a", 5)
    record_change("agent-b", 3)
    assert get_unaudited_lines("agent-a") == 5
    assert get_unaudited_lines("agent-b") == 3
    assert get_unaudited_lines("agent-c") == 0


def test_reset_ledger_zeroes(isolated_ledger):
    from hiveweave.services.code_audit import (
        get_unaudited_lines,
        record_change,
        reset_ledger,
    )

    record_change("agent-a", 7)
    reset_ledger("agent-a")
    assert get_unaudited_lines("agent-a") == 0
    reset_ledger("agent-unknown")  # 无该 agent 行也不抛


def test_last_change_ts_tracks_edits(isolated_ledger):
    from hiveweave.services.code_audit import (
        get_last_change_ts,
        record_change,
        reset_ledger,
    )

    assert get_last_change_ts("agent-a") == 0.0  # 缺席 → 0
    record_change("agent-a", 3)
    ts = get_last_change_ts("agent-a")
    assert ts > 0  # 有编辑 → ts 被记录
    reset_ledger("agent-a")
    assert get_last_change_ts("agent-a") == 0.0  # 重置 → 0


def test_last_change_ts_ignores_nonpositive_lines(isolated_ledger):
    from hiveweave.services.code_audit import get_last_change_ts, record_change

    record_change("agent-a", 0)
    record_change("agent-a", -5)
    assert get_last_change_ts("agent-a") == 0.0  # lines<=0 不更新 ts


def test_ledger_snapshot_copy_semantics(isolated_ledger):
    from hiveweave.services.code_audit import (
        ledger_snapshot,
        record_change,
    )

    record_change("agent-a", 4)
    snap = ledger_snapshot()
    assert snap.get("agent-a") == 4
    snap["agent-a"] = 999
    snap["ghost"] = 1
    fresh = ledger_snapshot()
    assert fresh.get("agent-a") == 4
    assert "ghost" not in fresh


def test_append_code_audit_notice_idempotent(isolated_ledger):
    from hiveweave.services.code_audit import append_code_audit_notice

    first = append_code_audit_notice("impl done, needs audit")
    second = append_code_audit_notice("impl done, needs audit")
    assert isinstance(first, str) and first
    assert "CODE AUDIT" in first
    assert second == first  # marker detection → 第二次调用 no-op，不重复追加
