"""I15(P2-5) 批 6 回归 — 激活来源值域治理（缺省值 / 空串 / 双机制留一）。

背景（PLATFORM-ISSUES §十五 I15）：
- ``trigger_type = (opts.get("source") or "chat")`` 把「漏传」永久伪装成
  「用户发的」—— 缺省值不许是业务值，缺失必须落 ``"unknown"`` + 告警；
- ``trigger_source`` 72 行（全量只读普查 83 库 / 11,991 行）100% 空串、
  零 NULL —— 根因不只是 ``or ""`` 兜底：``opts["trigger"]`` 是**布尔**，
  ``isinstance(trigger, dict)`` 恒 False、读 from_agent_id 的分支是死代码，
  来源实际在 ``opts["from_agent_id"]``；
- 空串/NULL 择一 ⇒ 统一 NULL（列 DDL 可空），「没填」不许被
  ``IS NOT NULL`` 读成「填了」；
- 三组「双机制并存只启用其一」留一裁决注释钉在
  ``agents/agent.py::_activation_trigger_fields``（门铃/验收/招聘）。
"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from structlog.testing import capture_logs

from hiveweave.agents.agent import _activation_trigger_fields
from hiveweave.services.run_ledger import RunLedger

_SRC = Path(__file__).resolve().parents[1] / "src" / "hiveweave"
_AGENT_PY = (_SRC / "agents" / "agent.py").read_text(encoding="utf-8")


# ── ① 缺省值不许是业务值 ─────────────────────────────────────


def test_missing_source_defaults_to_unknown_not_chat():
    """source 缺失 ⇒ "unknown"（绝不是旧缺省 "chat"）。"""
    trigger_type, trigger_source = _activation_trigger_fields({})
    assert trigger_type == "unknown"
    assert trigger_type != "chat"
    assert trigger_source is None


def test_missing_source_emits_warning():
    """source 缺失必须落告警日志（activation_source_missing）。"""
    with capture_logs() as cap:
        _activation_trigger_fields({}, agent_id="a-1", preview="hello")
    warns = [e for e in cap if e.get("event") == "activation_source_missing"]
    assert warns, "缺 source 必须告警"
    assert warns[0]["agent_id"] == "a-1"


def test_explicit_source_passes_through():
    """显式传的 source 原样成为 trigger_type（"chat" 只在用户入口显式传）。"""
    assert _activation_trigger_fields({"source": "chat"})[0] == "chat"
    assert _activation_trigger_fields({"source": "task"})[0] == "task"
    assert _activation_trigger_fields({"source": "wait_satisfied"})[0] == (
        "wait_satisfied"
    )


# ── ② trigger_source：根因修复 + 空串/NULL 择一 ──────────────


def test_trigger_source_reads_opts_from_agent_id():
    """根因修复：来源在 opts["from_agent_id"]（旧代码读布尔 trigger 的
    dict 分支恒死，11,991 行全空串即此根因）。"""
    trigger_type, trigger_source = _activation_trigger_fields(
        {"trigger": True, "source": "task", "from_agent_id": "agent-9"}
    )
    assert trigger_type == "task"
    assert trigger_source == "agent-9"


def test_trigger_source_never_empty_string():
    """「没填」= None（NULL 语义），绝不许是 ''（IS NOT NULL 判据不再判反）。"""
    assert _activation_trigger_fields({})[1] is None
    assert _activation_trigger_fields({"trigger": True})[1] is None
    assert _activation_trigger_fields({"from_agent_id": ""})[1] is None
    assert _activation_trigger_fields({"from_agent_id": "   "})[1] is None
    assert _activation_trigger_fields({"from_agent_id": None})[1] is None


def test_trigger_source_strips_whitespace():
    assert _activation_trigger_fields({"from_agent_id": " agent-7 "})[1] == (
        "agent-7"
    )


def test_opts_none_tolerated():
    """opts=None（chat() 旧调用面可能传 None）不炸、归 unknown。"""
    trigger_type, trigger_source = _activation_trigger_fields(None)  # type: ignore[arg-type]
    assert trigger_type == "unknown"
    assert trigger_source is None


def test_old_chat_default_gone_from_source():
    """旧缺省表达式必须从 agent.py 消失（防止回归复活）。"""
    assert '(opts.get("source") or "chat")' not in _AGENT_PY


# ── ② 落库面：create_activation(None) 落 NULL 而非 '' ────────

_SCHEMA = [
    "CREATE TABLE agent_activations ("
    "id TEXT PRIMARY KEY, agent_id TEXT, trigger_type TEXT, trigger_source TEXT, "
    "trigger_detail TEXT, inbox_msg_ids TEXT, interrupted_run_id TEXT, "
    "checkpoint_summary TEXT, created_at INTEGER)",
    "CREATE TABLE agent_runs ("
    "id TEXT PRIMARY KEY, agent_id TEXT, status TEXT NOT NULL DEFAULT 'running', "
    "started_at INTEGER, error_reason TEXT, orphan_steps INTEGER DEFAULT 0)",
    "CREATE TABLE run_steps ("
    "id TEXT PRIMARY KEY, run_id TEXT, step_index INTEGER, step_type TEXT, "
    "tool_name TEXT, tool_call_id TEXT, status TEXT NOT NULL DEFAULT 'pending', "
    "result_hash TEXT, result_size INTEGER, result_excerpt TEXT, error TEXT, "
    "started_at INTEGER, ended_at INTEGER, duration_ms INTEGER, "
    "runner_failed INTEGER DEFAULT 0, command_failed INTEGER DEFAULT 0, "
    "injection_applied INTEGER DEFAULT 0, timeout_kind TEXT, timeout_ms INTEGER, "
    "outcome_unknown INTEGER DEFAULT 0, not_started INTEGER DEFAULT 0, "
    "started INTEGER DEFAULT 0)",
]


class _FakeDb:
    """最小内存 project_db stand-in（同 test_run_ledger_orphan_sweep 契约）。"""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in _SCHEMA:
            self.conn.execute(sql)
        self.conn.commit()

    async def execute(self, agent_id: str, sql: str, params=None) -> None:
        self.conn.execute(sql, params or [])
        self.conn.commit()

    async def query(self, agent_id: str, sql: str, params=None):
        return self.conn.execute(sql, params or []).fetchall()

    async def schema_marker_key_for_agent(self, agent_id: str):
        return ("fake-ws", 1)


def _patched_db(fake: _FakeDb):
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


def test_create_activation_none_source_stores_null():
    """agent 侧契约：trigger_source=None 必须**原样落 NULL**（非 ''），
    「没填」从此可被 IS NULL / IS NOT NULL 正确读出。"""
    fake = _FakeDb()
    ledger = RunLedger()
    with _patched_db(fake):
        asyncio.run(
            ledger.create_activation(
                "a1", "unknown", trigger_source=None, trigger_detail="d"
            )
        )
        asyncio.run(
            ledger.create_activation(
                "a1", "task", trigger_source="agent-2", trigger_detail="d"
            )
        )
    rows = fake.conn.execute(
        "SELECT trigger_type, trigger_source FROM agent_activations"
    ).fetchall()
    assert ("unknown", None) in rows, "None 必须落 NULL，不得变空串"
    assert ("task", "agent-2") in rows
    # 整表不允许出现空串（值域纪律）
    empties = fake.conn.execute(
        "SELECT COUNT(*) FROM agent_activations WHERE trigger_source = ''"
    ).fetchone()[0]
    assert empties == 0


# ── ③ 用户入口显式报 "chat" + 留一裁决注释在场（防回归守卫）──


def test_user_entry_points_declare_chat_source():
    """用户直聊入口必须显式传 source="chat"：unknown 只留给真正的漏传。"""
    chat_api = (_SRC / "api" / "chat.py").read_text(encoding="utf-8")
    assert '"source": "chat"' in chat_api, "REST 用户直聊缺显式 source"

    channels = (_SRC / "realtime" / "channels.py").read_text(encoding="utf-8")
    assert channels.count('agent.chat(message, {"source": "chat"})') >= 2, (
        "两条 WS 用户直聊路径都必须显式报 source"
    )

    user_msg = (_SRC / "services" / "user_message.py").read_text(encoding="utf-8")
    assert '"source": "chat"' in user_msg, "三端共用投递入口缺显式 source"

    # steer（用户插话）缺省与残留回放都按用户消息报 "chat"
    assert '{"source": "chat", **(opts or {})}' in _AGENT_PY
    assert '(_rest, {"source": "chat"},' in _AGENT_PY


def test_dual_mechanism_verdicts_documented():
    """三组双机制「留一」裁决注释必须钉在 _activation_trigger_fields。"""
    start = _AGENT_PY.index("def _activation_trigger_fields")
    doc = _AGENT_PY[start : start + 4000]
    # 门铃：留 agent_waits(kind='user')，question 下沉（I9 批 8 落地）
    assert "agent_waits(kind='user')" in doc
    # 验收：回合作内二道审计唯一路径 = request_code_audit
    assert "request_code_audit" in doc
    # 招聘：唯一执行通道 = hire_agent，staffing_demands 收窄为观测信号
    assert "hire_agent" in doc
    # 裁决总判据（同 F6：双写点必然漏点）
    assert "留一裁决" in doc
