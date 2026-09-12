"""M3 回归测试 — 孤儿 run_steps 清扫 + record_step_end 有界重试。

背景：record_step_end 的 SELECT+UPDATE 曾失败仅 log.warning 无重试，
slack-clone_03 实测 6 行孤儿 step（run 已完成但 step 永远 running，
跨 17 小时）。修复：create_activation 开头清扫孤儿步骤 + record_step_end
UPDATE 有界重试（2 次，50/100ms 退避）。
"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from hiveweave.services.run_ledger import RunLedger

_SCHEMA = [
    "CREATE TABLE agent_activations ("
    "id TEXT PRIMARY KEY, agent_id TEXT, trigger_type TEXT, trigger_source TEXT, "
    "trigger_detail TEXT, inbox_msg_ids TEXT, interrupted_run_id TEXT, "
    "checkpoint_summary TEXT, created_at INTEGER)",
    # agent_runs 带上事实位列（与 _FACT_COLUMNS 对齐）——这样 _ensure_fact_columns
    # 的 ALTER 走 "duplicate column" 幂等路径，测试不依赖 _fact_columns_ready
    # 的跨测试缓存（否则第二个测试会因缓存命中而 ALTER 被跳过 → 列缺失）。
    "CREATE TABLE agent_runs ("
    "id TEXT PRIMARY KEY, agent_id TEXT, activation_id TEXT, "
    "status TEXT NOT NULL DEFAULT 'running', started_at INTEGER, "
    # F7 补出口（TEST_DSH_50/51）：孤儿步骤的 timeout_kind 归属要看 run 的
    # error_reason 是否总超时，所以 fake schema 必须带上这一列。
    "error_reason TEXT, "
    "empty_stream INTEGER DEFAULT 0, cache_verdict TEXT, "
    "cache_drifts TEXT, orphan_steps INTEGER DEFAULT 0)",
    "CREATE TABLE run_steps ("
    "id TEXT PRIMARY KEY, run_id TEXT, step_index INTEGER, step_type TEXT, "
    "tool_name TEXT, tool_call_id TEXT, tool_args_hash TEXT, "
    "status TEXT NOT NULL DEFAULT 'pending', result_hash TEXT, result_size INTEGER, "
    "result_excerpt TEXT, error TEXT, started_at INTEGER, ended_at INTEGER, "
    "duration_ms INTEGER, "
    # F4/F7 事实位（2026-08-30 加列；本文件 2026-09-10 补进 schema）
    "runner_failed INTEGER DEFAULT 0, command_failed INTEGER DEFAULT 0, "
    "injection_applied INTEGER DEFAULT 0, timeout_kind TEXT, timeout_ms INTEGER, "
    # L5 第三/第四格（2026-09-11）：孤儿步骤的「结果未知」与「从未开始」
    "outcome_unknown INTEGER DEFAULT 0, not_started INTEGER DEFAULT 0, "
    # TEST_DSH_54 #2（2026-09-12）：第五格 —— 「已开始执行」事实位。
    # 区分「从未派发」（started=0）与「执行中被掐死」（started=1）。
    "started INTEGER DEFAULT 0)",
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

    async def query(self, agent_id: str, sql: str, params=None):
        """只读查询（run 级 orphan_steps 聚合走这里）。"""
        return self.conn.execute(sql, params or []).fetchall()

    async def schema_marker_key_for_agent(self, agent_id: str):
        return ("fake-ws", 1)

    def seed_run_step(self, run_id: str, step_id: str, run_status: str,
                      step_status: str = "running",
                      error_reason: str | None = None,
                      started: int | None = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO agent_runs (id, agent_id, status, started_at, "
            "error_reason) VALUES (?, 'a1', ?, 1000, ?)",
            [run_id, run_status, error_reason],
        )
        self.conn.execute(
            "INSERT INTO run_steps (id, run_id, step_index, step_type, status, "
            "started_at, started) VALUES (?, ?, 0, 'llm_request', ?, 1000, ?)",
            [step_id, run_id, step_status, started],
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

    def step_timeout_kind(self, step_id: str):
        return self.conn.execute(
            "SELECT timeout_kind FROM run_steps WHERE id = ?", [step_id]
        ).fetchone()[0]

    def step_flags(self, step_id: str) -> tuple[object, object]:
        """L5：孤儿步骤的 (outcome_unknown, not_started)。"""
        return self.conn.execute(
            "SELECT outcome_unknown, not_started FROM run_steps WHERE id = ?",
            [step_id],
        ).fetchone()

    def run_orphan_steps(self, run_id: str):
        """TEST_DSH_54 #2：run 级孤儿计数事实位。"""
        return self.conn.execute(
            "SELECT orphan_steps FROM agent_runs WHERE id = ?", [run_id]
        ).fetchone()[0]


def _patched_db(fake: _FakeDb):
    """把 run_ledger 用到的 project_db 面全部接到 fake 上（单一上下文管理器，
    保持所有既有调用点 `with _patched_db(fake):` 不变）。

    query / schema_marker_key_for_agent 必须一起接：run 级 orphan_steps 聚合
    与事实位列补列都走它们，漏接会让新逻辑静默落进 except 分支。
    """
    stack = ExitStack()
    stack.enter_context(
        patch("hiveweave.services.run_ledger.project_db.execute", new=fake.execute)
    )
    stack.enter_context(
        patch("hiveweave.services.run_ledger.project_db.query", new=fake.query)
    )
    stack.enter_context(
        patch(
            "hiveweave.services.run_ledger.project_db.schema_marker_key_for_agent",
            new=fake.schema_marker_key_for_agent,
        )
    )
    return stack


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


def test_swept_orphan_carries_outcome_unknown_and_retry_guide():
    """L5：清扫的孤儿步骤带第三格事实位 + DSH 三段式重试指引原文。

    分野照 DSH `repair.ts:14-18`：调用**已记录**但结果未持久化 ⇒
    outcome_unknown=1（不是 not_started —— 那个留给 startup_sweep）。
    指引必须含「明确的可执行判据」，且**逐字**保留 "Do not retry blindly."
    """
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "completed")
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(ledger.create_activation("a1", "wake"))

    outcome_unknown, not_started = fake.step_flags("s1")
    assert outcome_unknown == 1, "孤儿步骤必须声明 outcome_unknown"
    assert not_started == 0, "本清扫不是 not_started 语义（那是 startup_sweep）"

    _status, _ended, error = fake.step_status("s1")
    # 回溯锚点保留（既有 F7 timeout_kind 回填靠它匹配）
    assert error.startswith("orphan step swept")
    # 三段式指引逐字校验（照抄 DSH repair.ts:106）
    assert "Its outcome is unknown." in error
    assert "retry only if the operation is read-only or idempotent" in error
    assert "first verify external state or ask the user" in error
    assert "Do not retry blindly." in error


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


def test_swept_orphan_of_hard_timeout_run_gets_timeout_kind_turn():
    """F7 补出口（TEST_DSH_50/51）：整轮兜底超时的 run，其孤儿步骤要带
    timeout_kind='turn' —— 这是「超时不可分类」的唯一漏网出口。

    实测基线：4 个 600s 硬杀 run（50 ×1 / 51 ×3）的末步 timeout_kind 全 NULL。
    """
    fake = _FakeDb()
    fake.seed_run_step("r-timeout", "s-timeout", "error",
                       error_reason="ValueError: 请求总超时")
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(ledger.create_activation("a1", "wake"))

    assert fake.step_status("s-timeout")[0] == "error"
    assert fake.step_timeout_kind("s-timeout") == "turn"


def test_swept_orphan_of_non_timeout_run_keeps_timeout_kind_null():
    """反向：startup_sweep / cancel 造成的孤儿**不是**超时，不得误标。

    否则 timeout_kind 会变成「所有孤儿都算超时」的噪声位。
    """
    fake = _FakeDb()
    fake.seed_run_step("r-restart", "s-restart", "interrupted",
                       error_reason="startup_sweep: stale running run from prior process")
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(ledger.create_activation("a1", "wake"))

    assert fake.step_status("s-restart")[0] == "error"
    assert fake.step_timeout_kind("s-restart") is None


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


# ── TEST_DSH_54 #2/#9：started 事实位把「从未派发」与「结果未知」分开 ──
#
# 报告原文（Layer 7 STEP 1）：
#   「agents/streaming.py:242 的 record_step_start（INSERT，status='running'）
#     发生在 :258 execute() **之前** —— 所以一行 running 记录无法区分
#     "从没派发执行"与"执行中被整轮超时掐死"。」
# 实测后果：116 步（含 submit_task×7 / update_task_status×12）被要求
# "先核实外部状态"；not_started 全库为 0。


def test_started_orphan_is_outcome_unknown_not_not_started():
    """started=1（执行中被掐死）⇒ outcome_unknown，**不是** not_started。

    这是 v1 稿会误判的那一类：把它标成 not_started 并配"可安全重试"，
    对 submit_task / dispatch_task 就是副作用双发。
    """
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "completed", started=1)
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(ledger.create_activation("a1", "wake"))

    outcome_unknown, not_started = fake.step_flags("s1")
    assert outcome_unknown == 1
    assert not_started == 0
    _s, _e, error = fake.step_status("s1")
    assert error.startswith("orphan step swept")
    assert "Do not retry blindly." in error


def test_never_started_orphan_is_not_started():
    """started=0（从未派发）⇒ not_started + "没有副作用"文案。

    run 已结束、步骤仍 running、但执行从未开始 —— 这是"必然无副作用"的
    唯一一类，理应可以直接重试（不是"先核实外部状态"）。
    """
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "completed", started=0)
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(ledger.create_activation("a1", "wake"))

    outcome_unknown, not_started = fake.step_flags("s1")
    assert not_started == 1
    assert outcome_unknown == 0
    _s, _e, error = fake.step_status("s1")
    assert error.startswith("orphan step never started")
    assert "had no side effect" in error
    # 反向断言：不得把"别盲目重试"的警告浪费在从未发生的调用上
    assert "Do not retry blindly." not in error


def test_legacy_null_started_falls_back_to_conservative_bucket():
    """存量行 started IS NULL（无法判定）⇒ 保守归 outcome_unknown。

    宁可少给"可安全重试"，不可错给 —— 错给的代价是副作用双发。
    """
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "completed", started=None)
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(ledger.create_activation("a1", "wake"))

    outcome_unknown, not_started = fake.step_flags("s1")
    assert outcome_unknown == 1, "NULL 不可假定为'从未执行'"
    assert not_started == 0


def test_sweep_writes_run_level_orphan_steps_fact():
    """run 级事实位：不改 `completed` 终态枚举，但让孤儿计数可被机器读出。

    报告要求「run 不再报 completed 建议改用事实位（orphan_steps>0），
    不改终态枚举 —— 下游 UI 与回归脚本都按 completed 统计」。
    """
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "completed", started=1)
    fake.seed_run_step("r1", "s2", "completed", started=0)
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(ledger.create_activation("a1", "wake"))

    assert fake.run_orphan_steps("r1") == 2
    # run 终态保持不动（本清扫不该改 run 状态）
    status = fake.conn.execute(
        "SELECT status FROM agent_runs WHERE id = 'r1'"
    ).fetchone()[0]
    assert status == "completed"


def test_mark_step_started_sets_flag():
    """mark_step_started 是 execute() 前一刻的落点位（streaming 调用它）。"""
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "running", started=0)
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(ledger.mark_step_started("a1", "s1"))

    row = fake.conn.execute(
        "SELECT started FROM run_steps WHERE id = 's1'"
    ).fetchone()[0]
    assert row == 1


def test_mark_step_started_is_best_effort_on_failure():
    """落位失败不得炸运行时（漏置位只会让该行退回保守桶）。"""
    fake = _FakeDb()
    ledger = RunLedger()

    async def boom(*_a, **_k):
        raise sqlite3.OperationalError("locked")

    with patch("hiveweave.services.run_ledger.project_db.execute", new=boom):
        asyncio.run(ledger.mark_step_started("a1", "s1"))  # 不得抛

    # 失败即止：不得退到别的写入路径（唯一漏斗 = 不要偷偷补写）
    assert fake.executed == []


# ── TEST_DSH_54 #6：漂移明细落库（opaque 的 final 分类之外，要能答"哪一段"）──
#
# 报告原文：「真正仍缺的只有 drifts[] 那份明细（到底哪一段前缀漂了：
# compacted_drift？history_rewritten？），它只在日志；平台日志文件停在 09-08
# ⇒ 事后仍无法回答"漂移的是哪一段前缀"。」


def test_set_run_fact_persists_cache_drifts():
    """cache_drifts 必须真的落到 agent_runs —— 被 allowed 白名单漏掉会静默丢弃。"""
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "completed", started=1)
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(
            ledger.set_run_fact(
                "a1", "r1",
                cache_verdict="drift_zero_hit",
                cache_drifts='["compacted_drift"]',
            )
        )

    row = fake.conn.execute(
        "SELECT cache_verdict, cache_drifts FROM agent_runs WHERE id = 'r1'"
    ).fetchone()
    assert row[0] == "drift_zero_hit"
    assert row[1] == '["compacted_drift"]', (
        "cache_drifts 不在 set_run_fact 的 allowed 白名单里就会被**静默丢弃**"
    )


def test_set_run_fact_skips_none_drifts():
    """无漂移（None / 空）不写 —— 保持"未确定"语义，别把 NULL 写成 '[]'。"""
    fake = _FakeDb()
    fake.seed_run_step("r1", "s1", "completed", started=1)
    ledger = RunLedger()

    with _patched_db(fake):
        asyncio.run(
            ledger.set_run_fact("a1", "r1", cache_verdict="hit_ok", cache_drifts=None)
        )

    row = fake.conn.execute(
        "SELECT cache_verdict, cache_drifts FROM agent_runs WHERE id = 'r1'"
    ).fetchone()
    assert row[0] == "hit_ok"
    assert row[1] is None


# ── `started` 列的**迁移形态**（审计 2026-09-12 发现的反向危险）──────────
#
# 这条不是风格问题：SQLite 的 `ALTER TABLE ADD COLUMN … DEFAULT 0` 会给
# **存量行回填 0**（实测 legacy running 行读出 started=0，`IS NULL` 命中 0 行）。
# 于是升级前遗留的 running 行会落进 started=0 → 被判 "从未执行、无副作用、
# 可直接重试" —— 正是本次修复要消灭的那次**副作用双发邀请**。
# 因此该 ALTER **必须不带 DEFAULT**：存量行 → NULL → 保守归 outcome_unknown。
# 新行由 record_step_start 的 INSERT 显式写 0。


def test_started_migration_leaves_legacy_rows_null():
    """迁移 DDL 不得给 started 带 DEFAULT —— 否则存量行被回填成"从未执行"。"""
    from hiveweave.db import schema as schema_mod

    ddl = [
        s for s in schema_mod.PROJECT_DB_TABLES
        if isinstance(s, str) and "ADD COLUMN started" in s
    ]
    assert len(ddl) == 1, f"started 的 ALTER 应恰好一条，实得 {ddl}"
    assert "DEFAULT" not in ddl[0].upper(), (
        "`ALTER TABLE run_steps ADD COLUMN started … DEFAULT 0` 会把存量 running 行"
        "回填为 0 ⇒ 被误判为 not_started（'可直接重试'）⇒ 副作用双发风险。"
        f" 实测 DDL：{ddl[0]!r}"
    )


def test_sqlite_alter_with_default_would_backfill_zero():
    """把上一条的判据钉在 SQLite 的真实行为上（防止有人"简化"成 DEFAULT 0）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE legacy (id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO legacy VALUES ('s','running')")
    conn.execute("ALTER TABLE legacy ADD COLUMN started INTEGER")
    assert conn.execute(
        "SELECT started IS NULL FROM legacy"
    ).fetchone()[0] == 1, "不带 DEFAULT 时存量行必须是 NULL（保守桶入口）"

    conn.execute("ALTER TABLE legacy ADD COLUMN started2 INTEGER DEFAULT 0")
    assert conn.execute(
        "SELECT started2 FROM legacy"
    ).fetchone()[0] == 0, (
        "带 DEFAULT 0 会被回填成 0 —— 正是不可采用的写法"
    )



