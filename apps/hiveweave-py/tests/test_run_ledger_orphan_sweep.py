"""M3 回归测试 — 孤儿 run_steps 清扫 + record_step_end 有界重试。

背景：record_step_end 的 SELECT+UPDATE 曾失败仅 log.warning 无重试，
slack-clone_03 实测 6 行孤儿 step（run 已完成但 step 永远 running，
跨 17 小时）。修复：create_activation 开头清扫孤儿步骤 + record_step_end
UPDATE 有界重试（2 次，50/100ms 退避）。
"""
from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

from hiveweave.services.run_ledger import RunLedger

_SCHEMA = [
    "CREATE TABLE agent_activations ("
    "id TEXT PRIMARY KEY, agent_id TEXT, trigger_type TEXT, trigger_source TEXT, "
    "trigger_detail TEXT, inbox_msg_ids TEXT, interrupted_run_id TEXT, "
    "checkpoint_summary TEXT, created_at INTEGER)",
    "CREATE TABLE agent_runs ("
    "id TEXT PRIMARY KEY, agent_id TEXT, activation_id TEXT, "
    "status TEXT NOT NULL DEFAULT 'running', started_at INTEGER)",
    "CREATE TABLE run_steps ("
    "id TEXT PRIMARY KEY, run_id TEXT, step_index INTEGER, step_type TEXT, "
    "tool_name TEXT, tool_call_id TEXT, tool_args_hash TEXT, "
    "status TEXT NOT NULL DEFAULT 'pending', result_hash TEXT, result_size INTEGER, "
    "result_excerpt TEXT, error TEXT, started_at INTEGER, ended_at INTEGER, "
    "duration_ms INTEGER)",
]


class _FakeDb:
    """最小内存 stand-in for project_db：执行 run_ledger 的 SQL 并记账。

    所有调用发生在同一线程（asyncio.run 的循环线程），plain sqlite3 安全。
    """

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in _SCHEMA:
            self.conn.execute(sql)
        self.conn.commit()
        self.executed: list[tuple[str, list]] = []
        self.fail_first_update = False

    async def execute(self, agent_id: str, sql: str, params=None) -> None:
        params = params or []
        self.executed.append((sql, params))
        if self.fail_first_update and sql.strip().startswith("UPDATE run_steps"):
            self.fail_first_update = False
            # 锁竞争在真实链路是 sqlite3.OperationalError（busy_timeout 耗尽）；
            # run_ledger 只对该类型重试，RuntimeError 属非瞬断直接放弃（复审收窄）。
            raise sqlite3.OperationalError("simulated sqlite lock")
        self.conn.execute(sql, params)
        self.conn.commit()

    def seed_run_step(self, run_id: str, step_id: str, run_status: str,
                      step_status: str = "running") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO agent_runs (id, agent_id, status, started_at) "
            "VALUES (?, 'a1', ?, 1000)",
            [run_id, run_status],
        )
        self.conn.execute(
            "INSERT INTO run_steps (id, run_id, step_index, step_type, status, "
            "started_at) VALUES (?, ?, 0, 'llm_request', ?, 1000)",
            [step_id, run_id, step_status],
        )
        self.conn.commit()

    def step_status(self, step_id: str) -> tuple[str, object, object]:
        row = self.conn.execute(
            "SELECT status, ended_at, error FROM run_steps WHERE id = ?",
            [step_id],
        ).fetchone()
        assert row is not None, f"step {step_id} not found"
        return row

    def activation_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM agent_activations"
        ).fetchone()[0]


def _patched_db(fake: _FakeDb):
    return patch("hiveweave.services.run_ledger.project_db.execute",
                 new=fake.execute)


def test_create_activation_sweeps_orphan_steps_of_ended_run():
    """① 已完成 run + running step → create_activation 后 step 被清扫为 error。"""
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "completed")
    fake.seed_run_step("r1", "s2", "completed")
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(ledger.create_activation("a1", "wake"))

    for sid in ("s1", "s2"):
        status, ended_at, error = fake.step_status(sid)
        assert status == "error"
        assert ended_at is not None
        assert "orphan" in error


def test_create_activation_keeps_steps_of_running_run():
    """② running 中的 run 的 step 不被清扫；仅结束 run 的被清扫。"""
    fake = _FakeDb()
    fake.seed_run_step("r-running", "s-running", "running")
    fake.seed_run_step("r-done", "s-done", "completed")
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(ledger.create_activation("a1", "wake"))

    assert fake.step_status("s-running")[0] == "running"
    assert fake.step_status("s-done")[0] == "error"
    assert fake.activation_count() == 1


def test_record_step_end_retries_after_update_failure():
    """③ UPDATE 首次失败（sqlite 锁竞争）→ 有界重试成功，status 落库 completed。"""
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "completed")
    fake.fail_first_update = True
    ledger = RunLedger()

    with _patched_db(fake), patch(
        "hiveweave.services.run_ledger.project_db.query",
        AsyncMock(return_value=[{"started_at": 1000}]),
    ):
        asyncio.run(ledger.record_step_end("a1", "s1", "completed",
                                           result_hash="abc123"))

    updates = [sql for sql, _ in fake.executed
               if sql.strip().startswith("UPDATE run_steps")]
    assert len(updates) == 2, f"expected 2 attempts, got {len(updates)}"
    status, ended_at, _ = fake.step_status("s1")
    assert status == "completed"
    assert ended_at is not None
    row = fake.conn.execute(
        "SELECT result_hash, duration_ms FROM run_steps WHERE id = 's1'"
    ).fetchone()
    assert row[0] == "abc123"
    assert row[1] is not None


def test_record_step_end_exhausts_retries_without_raising():
    """重试耗尽后仍不抛异常（best-effort 语义），仅记 warning。"""
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "completed")
    ledger = RunLedger()

    async def always_fail(*_a, **_k):
        raise RuntimeError("persistent lock")

    with patch("hiveweave.services.run_ledger.project_db.execute",
               new=always_fail), patch(
        "hiveweave.services.run_ledger.project_db.query",
        AsyncMock(return_value=[{"started_at": 1000}]),
    ), patch("hiveweave.services.run_ledger.log") as log:
        asyncio.run(ledger.record_step_end("a1", "s1", "completed"))

    assert log.warning.called
    assert len(fake.executed) == 0  # 未触达真实执行器（全被 mock 拦截）
