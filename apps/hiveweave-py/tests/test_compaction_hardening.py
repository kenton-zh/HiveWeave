"""TEST18 巡检修复回归：压缩加固（P0/P1）。

覆盖：
1. compactor length-escalation：reasoning 模型吃光 max_tokens（finish_reason=length
   + content 空）→ 8000 预算重试一次；HTTP 错误不重试。
2. 专用压缩模型路由（HIVEWEAVE_COMPACTOR_MODEL_ID）：配置优先，缺失回退 agent 模型。
3. noop 压缩跳过持久化：LLM 失败 + 未超预算 + 无新消息 → 不再无条件
   DELETE+INSERT 重写整段历史（16 次回退 = 16 次非原子窗口实证）。
4. 触发去重 + 失败冷却：连续两次 append 只入队一次；冷却期内不重触发。
5. 原子持久化：DELETE+INSERT 单事务，INSERT 失败整体回滚（历史不丢）。
6. 并发事务串行化：per-workspace 写锁 + BEGIN IMMEDIATE 击穿回归。
7. _persist_turn 失败上抛 + telemetry 计数（静默失败修复）。
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.conversation.compaction import (
    SUMMARY_MAX_TOKENS_ESCALATED,
    _call_compactor_llm,
    resolve_compactor_callback,
)
from hiveweave.conversation.store import ConversationStore
from hiveweave.services.telemetry import telemetry

from tests.test_idle_architecture_p0 import EXEC, task_env  # noqa: F401


# ── Fake httpx 客户端 ───────────────────────────────────────


class FakeResponse:
    def __init__(self, status: int, json_data: dict | None = None):
        self.status_code = status
        self._data = json_data or {}

    def json(self) -> dict:
        return self._data


class FakeClient:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.posts: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def _compactor_model(model_id: str = "m", **extra) -> dict:
    row = {
        "base_url": "https://gw.example/v1",
        "api_key": "k",
        "model_id": model_id,
        "provider_type": "openai-compatible",
        "context_window": 128_000,
        "max_output_tokens": 8_192,
    }
    row.update(extra)
    return row


def _length_response() -> FakeResponse:
    """reasoning 模型吃光预算：finish_reason=length + content 空。"""
    return FakeResponse(
        200,
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "thinking...",
                    },
                }
            ]
        },
    )


def _ok_response(content: str = "### Goal\nsummary") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
            ]
        },
    )


# ── 1. length-escalation 重试 ───────────────────────────────


@pytest.mark.asyncio
async def test_uses_model_max_output_tokens_and_retries_once():
    """预算 = 模型输出上限；length+空 content → 相同预算幂等重试一次。"""
    client = FakeClient([_length_response(), _ok_response("final summary")])
    with patch("httpx.AsyncClient", return_value=client):
        result = await _call_compactor_llm(
            _compactor_model("reasoning-model"), "prompt",
            max_tokens=16_000,
        )
    assert result == "final summary"
    assert len(client.posts) == 2
    # 两次都用模型输出上限（不再 2000 首试）
    assert client.posts[0]["json"]["max_tokens"] == 16_000
    assert client.posts[1]["json"]["max_tokens"] == 16_000
    assert client.posts[1]["url"] == "https://gw.example/v1/chat/completions"


@pytest.mark.asyncio
async def test_responses_protocol_posts_to_responses_not_chat():
    client = FakeClient([_ok_response("ok summary")])
    with patch("httpx.AsyncClient", return_value=client):
        result = await _call_compactor_llm(
            _compactor_model(
                "muse-spark-1.2",
                provider_type="openai-responses",
                base_url="https://opencode.ai/zen/go/v1",
            ),
            "prompt",
        )
    assert result == "ok summary"
    assert client.posts[0]["url"] == "https://opencode.ai/zen/go/v1/responses"
    assert "messages" not in client.posts[0]["json"]
    assert "input" in client.posts[0]["json"]


@pytest.mark.asyncio
async def test_no_max_tokens_falls_back_to_default_budget():
    """未传 max_tokens（模型行无该列）→ 回退 SUMMARY_MAX_TOKENS_ESCALATED。"""
    client = FakeClient([_ok_response("ok summary")])
    with patch("httpx.AsyncClient", return_value=client):
        result = await _call_compactor_llm(_compactor_model(), "prompt")
    assert result == "ok summary"
    assert len(client.posts) == 1
    assert client.posts[0]["json"]["max_tokens"] == SUMMARY_MAX_TOKENS_ESCALATED


@pytest.mark.asyncio
async def test_no_retry_when_content_present():
    """content 非空 → 单次调用，不重试。"""
    client = FakeClient([_ok_response("ok summary")])
    with patch("httpx.AsyncClient", return_value=client):
        result = await _call_compactor_llm(
            _compactor_model(), "prompt", max_tokens=16_000
        )
    assert result == "ok summary"
    assert len(client.posts) == 1
    assert client.posts[0]["json"]["max_tokens"] == 16_000


@pytest.mark.asyncio
async def test_no_retry_on_http_error():
    """HTTP 非 200 → 不重试，返回 None。"""
    client = FakeClient([FakeResponse(429)])
    with patch("httpx.AsyncClient", return_value=client):
        result = await _call_compactor_llm(_compactor_model(), "prompt")
    assert result is None
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_content_present_with_length_finish_no_escalate():
    """finish_reason=length 但 content 非空（部分摘要）→ 不升级，直接返回。"""
    client = FakeClient(
        [
            FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"role": "assistant", "content": "partial"},
                        }
                    ]
                },
            )
        ]
    )
    with patch("httpx.AsyncClient", return_value=client):
        result = await _call_compactor_llm(_compactor_model(), "prompt")
    assert result == "partial"
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_bad_json_no_escalate():
    """非 JSON 响应 → 返回 None，不重试。"""

    class BadResp:
        status_code = 200

        def json(self):
            raise ValueError("no json")

    client = FakeClient([BadResp()])
    with patch("httpx.AsyncClient", return_value=client):
        result = await _call_compactor_llm(_compactor_model(), "prompt")
    assert result is None
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_double_failure_returns_none():
    """两次都 length+空 → None（两次调用都发出）。"""
    client = FakeClient([_length_response(), _length_response()])
    with patch("httpx.AsyncClient", return_value=client):
        result = await _call_compactor_llm(_compactor_model(), "prompt")
    assert result is None
    assert len(client.posts) == 2


# ── 2. 专用压缩模型路由 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_dedicated_compactor_model_preferred():
    """配置 COMPACTOR_MODEL_ID → 用专用模型，不查 agent 模型。"""
    from hiveweave.config import settings

    async def fake_meta(sql: str, params: list | None = None):
        assert "WHERE id = ?" in sql
        assert params == ["dedicated-1"]
        return {
            "model_id": "ded-model",
            "base_url": "https://ded.example/v1",
            "api_key": "k1",
            "max_output_tokens": 12_000,
        }

    with patch.object(settings, "compactor_model_id", "dedicated-1"):
        with patch("hiveweave.db.meta.query_one", fake_meta):
            cb = await resolve_compactor_callback(EXEC)
    assert cb is not None

    client = FakeClient([_ok_response("dedicated summary")])
    with patch("httpx.AsyncClient", return_value=client):
        result = await cb("prompt")
    assert result == "dedicated summary"
    assert client.posts[0]["url"] == "https://ded.example/v1/chat/completions"
    assert client.posts[0]["json"]["model"] == "ded-model"
    assert client.posts[0]["headers"]["Authorization"] == "Bearer k1"
    # max_tokens 取模型配置的输出上限
    assert client.posts[0]["json"]["max_tokens"] == 12_000


@pytest.mark.asyncio
async def test_dedicated_missing_falls_back_to_agent_model():
    """专用模型 id 不存在 → 回退 agent 自己的模型。"""
    from hiveweave.config import settings

    async def fake_meta(sql: str, params: list | None = None):
        if "WHERE id = ?" in sql and params == ["dedicated-x"]:
            return None  # 专用模型不存在
        if "WHERE id = ?" in sql and params == ["agent-model-1"]:
            return {
                "model_id": "agent-model",
                "base_url": "https://agent.example/v1",
                "api_key": "k2",
                "max_output_tokens": 16_000,
            }
        return None

    async def fake_project(agent_id: str, sql: str, params: list | None = None):
        assert "model_id" in sql
        return {"model_id": "agent-model-1"}

    with patch.object(settings, "compactor_model_id", "dedicated-x"):
        with patch("hiveweave.db.meta.query_one", fake_meta):
            with patch("hiveweave.db.project.query_one", fake_project):
                cb = await resolve_compactor_callback(EXEC)
    assert cb is not None

    client = FakeClient([_ok_response("agent summary")])
    with patch("httpx.AsyncClient", return_value=client):
        result = await cb("prompt")
    assert result == "agent summary"
    assert client.posts[0]["url"] == "https://agent.example/v1/chat/completions"
    assert client.posts[0]["headers"]["Authorization"] == "Bearer k2"
    assert client.posts[0]["json"]["max_tokens"] == 16_000


@pytest.mark.asyncio
async def test_dedicated_without_base_url_falls_back():
    """专用模型行存在但 base_url 为空 → 跳过，回退 agent 模型。"""
    from hiveweave.config import settings

    async def fake_meta(sql: str, params: list | None = None):
        if "WHERE id = ?" in sql and params == ["dedicated-x"]:
            return {"model_id": "ded", "base_url": "", "api_key": "k"}
        if "WHERE id = ?" in sql and params == ["agent-model-1"]:
            return {
                "model_id": "agent-model",
                "base_url": "https://agent.example/v1",
                "api_key": "k2",
                "max_output_tokens": 8192,
            }
        return None

    async def fake_project(agent_id: str, sql: str, params: list | None = None):
        return {"model_id": "agent-model-1"}

    with patch.object(settings, "compactor_model_id", "dedicated-x"):
        with patch("hiveweave.db.meta.query_one", fake_meta):
            with patch("hiveweave.db.project.query_one", fake_project):
                cb = await resolve_compactor_callback(EXEC)
    assert cb is not None

    client = FakeClient([_ok_response("agent summary")])
    with patch("httpx.AsyncClient", return_value=client):
        result = await cb("prompt")
    assert result == "agent summary"
    assert client.posts[0]["url"] == "https://agent.example/v1/chat/completions"
    assert client.posts[0]["json"]["max_tokens"] == 8192


# ── 对话历史构造 ────────────────────────────────────────────


def _make_history(n: int = 6) -> list[dict]:
    msgs: list[dict] = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"question {i}"})
        msgs.append(
            {
                "role": "assistant",
                "content": f"answer {i}",
                "tool_calls": [
                    {"id": f"tc-{i}", "type": "function",
                     "function": {"name": "read_file", "arguments": "{}"}}
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"tc-{i}", "content": "tool out"})
    return msgs


async def _seed_agent_row(workspace: str, pid: str) -> None:
    from hiveweave.db.project import ensure_project_db

    now_ms = int(time.time() * 1000)
    conn = await ensure_project_db(workspace)
    await conn.execute(
        "INSERT OR REPLACE INTO agents (id, project_id, name, role, "
        "permission_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [EXEC, pid, "exec-1", "executor", "executor", now_ms, now_ms],
    )
    await conn.commit()
    return conn


# ── 3. noop 压缩跳过持久化 ──────────────────────────────────


@pytest.mark.asyncio
async def test_noop_compaction_skips_persist(task_env):
    """LLM 失败 + 未超预算 → 不重写 DB（无 DELETE/INSERT），缓存与前缀不变。"""
    pid = task_env["project_id"]
    conn = await _seed_agent_row(task_env["workspace"], pid)
    store = ConversationStore()
    history = _make_history()
    store._cache[(pid, EXEC)] = list(history)

    db_agent_patch = patch(
        "hiveweave.db.project.get_project_db_for_agent",
        AsyncMock(return_value=conn),
    )
    db_agent_patch.start()
    try:
        with patch(
            "hiveweave.conversation.store.resolve_compactor_callback",
            AsyncMock(return_value=None),
        ):
            with patch(
                "hiveweave.db.project.execute_transaction",
                AsyncMock(),
            ) as mock_tx:
                await store._do_compaction(
                    EXEC, pid, (pid, EXEC), list(history), 100_000
                )
                # 空回退：绝不再无条件重写整段历史
                mock_tx.assert_not_called()
    finally:
        db_agent_patch.stop()

    assert store._cache[(pid, EXEC)] == history  # 缓存原样
    assert store.get_compacted_prefix(pid, EXEC) is None  # 无前缀


@pytest.mark.asyncio
async def test_successful_compaction_still_persists(task_env):
    """LLM 摘要成功 → 仍走单事务持久化（回归护栏）。"""
    pid = task_env["project_id"]
    conn = await _seed_agent_row(task_env["workspace"], pid)
    store = ConversationStore()
    store._cache[(pid, EXEC)] = _make_history()

    db_agent_patch = patch(
        "hiveweave.db.project.get_project_db_for_agent",
        AsyncMock(return_value=conn),
    )
    db_agent_patch.start()
    try:
        async def fake_compactor(prompt: str) -> str:
            return "real summary"

        with patch(
            "hiveweave.conversation.store.resolve_compactor_callback",
            AsyncMock(return_value=fake_compactor),
        ):
            with patch(
                "hiveweave.db.project.execute_transaction",
                AsyncMock(),
            ) as mock_tx:
                await store._do_compaction(
                    EXEC, pid, (pid, EXEC), _make_history(), 100_000
                )
                mock_tx.assert_awaited_once()
    finally:
        db_agent_patch.stop()

    prefix = store.get_compacted_prefix(pid, EXEC)
    assert prefix is not None
    assert "real summary" in prefix
    assert (pid, EXEC) not in store._compaction_pending


# ── 4. 触发去重 + 失败冷却 ──────────────────────────────────


@pytest.mark.asyncio
async def test_double_trigger_dedup_and_cooldown(task_env):
    """连续两次触发只入队一次；冷却期内不重触发；冷却后恢复。"""
    pid = task_env["project_id"]
    store = ConversationStore()
    # 128K 窗口：budget=108000，70% 阈值≈75600 → 需 >75.6K tokens 才触发。
    big = [{"role": "user", "content": "a" * 700_000}]  # ~175k tokens > 75.6k 阈值

    async def fake_ctx(_agent_id: str) -> int:
        return 128_000

    store._get_agent_context_window = fake_ctx
    store._enqueue_write = AsyncMock()

    key = (pid, EXEC)
    await store._maybe_trigger_compaction(EXEC, pid, key, big)
    await store._maybe_trigger_compaction(EXEC, pid, key, big)
    assert store._enqueue_write.call_count == 1  # 双入队去重

    # 冷却期内（pending 未清）不重触发
    await store._maybe_trigger_compaction(EXEC, pid, key, big)
    assert store._enqueue_write.call_count == 1

    # 模拟压缩完成（清 pending）+ 冷却期已过 → 可再次触发
    store._compaction_pending.discard(key)
    store._last_compaction_ts[key] = time.time() - 600
    await store._maybe_trigger_compaction(EXEC, pid, key, big)
    assert store._enqueue_write.call_count == 2

    # 模拟刚实际执行过（_do_compaction 写入时间戳）→ 冷却期内不触发
    store._compaction_pending.discard(key)
    store._last_compaction_ts[key] = time.time()
    await store._maybe_trigger_compaction(EXEC, pid, key, big)
    assert store._enqueue_write.call_count == 2


# ── 5. 原子持久化 ───────────────────────────────────────────


class FailingConn:
    """execute 代理：INSERT conversation_turns 时抛错（模拟磁盘/连接故障）。"""

    def __init__(self, conn, fail_on_insert: bool = True):
        self._conn = conn
        self.fail_on_insert = fail_on_insert

    async def execute(self, sql, params=None):
        if self.fail_on_insert and "INSERT INTO conversation_turns" in sql:
            raise RuntimeError("disk full")
        return await self._conn.execute(sql, params or [])

    async def commit(self):
        return await self._conn.commit()

    async def rollback(self):
        return await self._conn.rollback()


@pytest.mark.asyncio
async def test_persist_compaction_atomic_rollback(task_env):
    """INSERT 失败 → 整体回滚，旧 turn 仍在（DELETE 未提交）。"""
    pid = task_env["project_id"]
    conn = await _seed_agent_row(task_env["workspace"], pid)

    # 先落一条旧 turn（模拟压缩前的历史）
    store = ConversationStore()
    with patch(
        "hiveweave.db.project.get_project_db_for_agent",
        AsyncMock(return_value=conn),
    ):
        await store._persist_turn(EXEC, [{"role": "user", "content": "old turn"}])

    before = telemetry._counters.get("compaction_persist_failed", 0)
    failing = FailingConn(conn)
    with patch(
        "hiveweave.db.project.get_project_db_for_agent",
        AsyncMock(return_value=failing),
    ):
        with pytest.raises(RuntimeError):
            await store._persist_compaction(
                EXEC, [{"role": "user", "content": "new"}], "summary"
            )

    # DELETE 已回滚：旧 turn 仍在
    rows = await conn.execute(
        "SELECT COUNT(*) FROM conversation_turns WHERE agent_id = ?", (EXEC,)
    )
    n = (await rows.fetchone())[0]
    assert n == 1
    # 失败计数可见（静默失败修复）
    assert telemetry._counters.get("compaction_persist_failed", 0) == before + 1


# ── 6. _persist_turn 失败上抛 + 计数 ────────────────────────


@pytest.mark.asyncio
async def test_persist_turn_raises_and_counts(task_env):
    """_persist_turn 失败上抛 + persist_turn_failed 计数（不再静默吞）。"""
    pid = task_env["project_id"]
    conn = await _seed_agent_row(task_env["workspace"], pid)
    store = ConversationStore()

    before = telemetry._counters.get("persist_turn_failed", 0)
    failing = FailingConn(conn)
    with patch(
        "hiveweave.db.project.get_project_db_for_agent",
        AsyncMock(return_value=failing),
    ):
        with pytest.raises(RuntimeError):
            await store._persist_turn(EXEC, [{"role": "user", "content": "x"}])

    assert telemetry._counters.get("persist_turn_failed", 0) == before + 1


# ── 7. 并发事务串行化（S1 回归）─────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_execute_transaction_serialized(task_env):
    """S1：同 ws 两 agent 并发 execute_transaction → 写锁串行化，两个都成功。"""
    import asyncio
    import uuid as uuid_mod

    from hiveweave.db import project as project_db
    from hiveweave.db.project import execute_transaction

    pid = task_env["project_id"]
    conn = await _seed_agent_row(task_env["workspace"], pid)
    ws = task_env["workspace"]

    # 两个 agent 映射到同一 workspace（真实运行 get_project_db_for_agent 会填充）
    project_db._agent_cache["agent-a"] = ws
    project_db._agent_cache["agent-b"] = ws

    async def write(aid: str, label: str) -> None:
        await execute_transaction(
            aid,
            [
                ("DELETE FROM conversation_turns WHERE agent_id = ?", [aid]),
                (
                    "INSERT INTO conversation_turns "
                    "(id, agent_id, turn_index, raw_messages, approx_tokens, "
                    "created_at) VALUES (?, ?, 0, ?, 1, 1)",
                    [str(uuid_mod.uuid4()), aid, label],
                ),
            ],
        )

    db_patch = patch(
        "hiveweave.db.project.get_project_db_for_agent",
        AsyncMock(return_value=conn),
    )
    db_patch.start()
    try:
        # 无写锁时并发 BEGIN IMMEDIATE 会抛
        # "cannot start a transaction within a transaction"（审计实证）
        await asyncio.gather(write("agent-a", "A"), write("agent-b", "B"))
    finally:
        db_patch.stop()

    rows = await conn.execute(
        "SELECT agent_id, raw_messages FROM conversation_turns ORDER BY agent_id"
    )
    data = await rows.fetchall()
    assert [(r[0], r[1]) for r in data] == [("agent-a", "A"), ("agent-b", "B")]

    # 清理（避免污染后续测试的 workspace 锁）
    project_db._agent_cache.pop("agent-a", None)
    project_db._agent_cache.pop("agent-b", None)
    project_db._write_locks.pop(ws, None)


# ── COMPACTION_TRIGGER_RATIO：默认 70%（可经 env 覆盖） ───────────────


def test_compaction_trigger_ratio_default_070():
    """默认压缩阈值 = 70%，量纲 = **有效**窗口（min(声明, 256K 封顶)）。

    512K 声明 → 有效 256K → budget=236000，70%≈165.2K。
    150K（<70%）不触发；180K（>70%）触发。回归 50%→70% 的调高，
    同时锁住 P1-⑤：声明值不再直接决定压缩线。
    """
    from hiveweave.conversation import compaction as comp
    from hiveweave.conversation.token_utils import (
        EFFECTIVE_CONTEXT_CAP,
        resolve_effective_context_window,
    )

    assert comp.COMPACTION_TRIGGER_RATIO == 0.70
    assert resolve_effective_context_window(524_288) == EFFECTIVE_CONTEXT_CAP
    c = comp.Compaction()
    effective_budget = EFFECTIVE_CONTEXT_CAP - comp.COMPACTION_BUFFER
    # <70% → 不压缩
    assert c.check_overflow(150_000, 524_288) is None
    assert c.should_compact(150_000, 524_288) is False
    # >70% → 压缩，返回按有效窗口算的 budget
    budget = c.check_overflow(180_000, 524_288)
    assert budget == effective_budget
    assert c.should_compact(180_000, 524_288) is True


def test_compaction_trigger_ratio_env_override():
    """HIVEWEAVE_COMPACTION_TRIGGER_RATIO 覆盖默认值。"""
    import importlib

    with patch.dict(
        "os.environ", {"HIVEWEAVE_COMPACTION_TRIGGER_RATIO": "0.85"}
    ):
        comp = importlib.import_module("hiveweave.conversation.compaction")
        importlib.reload(comp)
        c = comp.Compaction()
        # 有效窗口 256K → budget=236000, 85%≈200.6K
        assert c.check_overflow(190_000, 524_288) is None
        assert c.should_compact(210_000, 524_288) is True
    # 恢复默认（重载）
    comp = importlib.import_module("hiveweave.conversation.compaction")
    importlib.reload(comp)
    assert comp.COMPACTION_TRIGGER_RATIO == 0.70


# ── P1-⑤：有效上下文封顶（1M 声明不得让压缩永不触发） ─────────────


def test_effective_context_cap_clamps_million_window():
    """1M 声明 → 有效 256K；实测 409K 峰值必须触发压缩。

    修复前：压缩线 = (1M − 20K) × 0.70 ≈ 686K > 409K，全项目零压缩。
    修复后：压缩线 = (256K − 20K) × 0.70 = 165.2K，409K 远超线。
    """
    from hiveweave.conversation.compaction import Compaction
    from hiveweave.conversation.token_utils import (
        EFFECTIVE_CONTEXT_CAP,
        calculate_history_budget,
        resolve_effective_context_window,
    )

    assert resolve_effective_context_window(1_000_000) == EFFECTIVE_CONTEXT_CAP
    # 声明值小于封顶时原样透传（不抬高小窗口）
    assert resolve_effective_context_window(128_000) == 128_000
    assert calculate_history_budget([], 1_000_000) == EFFECTIVE_CONTEXT_CAP - 20_000

    c = Compaction()
    assert c.should_compact(409_299, 1_000_000) is True
    # 修复前的旧压缩线之下、新压缩线之上的区间同样触发
    assert c.should_compact(257_000, 1_000_000) is True
    # 低于新压缩线仍不触发（未把压缩变成常态）
    assert c.should_compact(100_000, 1_000_000) is False


def test_effective_context_cap_env_override_and_disable():
    """HIVEWEAVE_EFFECTIVE_CONTEXT_WINDOW 可调；显式 0 = 关闭封顶。"""
    from hiveweave.conversation.token_utils import (
        EFFECTIVE_CONTEXT_CAP,
        effective_context_cap,
        resolve_effective_context_window,
    )

    with patch.dict("os.environ", {"HIVEWEAVE_EFFECTIVE_CONTEXT_WINDOW": "200000"}):
        assert effective_context_cap() == 200_000
        assert resolve_effective_context_window(1_000_000) == 200_000
    with patch.dict("os.environ", {"HIVEWEAVE_EFFECTIVE_CONTEXT_WINDOW": "0"}):
        assert resolve_effective_context_window(1_000_000) == 1_000_000
    # 非法值回退默认，不静默变成 0（那等于关闭封顶）
    with patch.dict("os.environ", {"HIVEWEAVE_EFFECTIVE_CONTEXT_WINDOW": "abc"}):
        assert effective_context_cap() == EFFECTIVE_CONTEXT_CAP
    with patch.dict("os.environ", {"HIVEWEAVE_EFFECTIVE_CONTEXT_WINDOW": "-5"}):
        assert effective_context_cap() == EFFECTIVE_CONTEXT_CAP


def test_effective_cap_preserves_illegal_window_semantics():
    """context_window <= 0 原样透传 —— 非法配置的硬失败语义不被封顶吞掉。"""
    from hiveweave.conversation.compaction import Compaction
    from hiveweave.conversation.token_utils import resolve_effective_context_window

    assert resolve_effective_context_window(0) == 0
    assert resolve_effective_context_window(-1) == -1
    assert Compaction().check_overflow(999_999, 0) is None


def test_compaction_trigger_ratio_env_clamped():
    """非法 env 值被 clamp 到 (0,1)：>=1 → 0.99，<=0 → 0.50。"""
    import importlib

    with patch.dict(
        "os.environ", {"HIVEWEAVE_COMPACTION_TRIGGER_RATIO": "5"}
    ):
        comp = importlib.import_module("hiveweave.conversation.compaction")
        importlib.reload(comp)
        assert comp._compaction_ratio() == 0.99
    with patch.dict(
        "os.environ", {"HIVEWEAVE_COMPACTION_TRIGGER_RATIO": "-1"}
    ):
        comp = importlib.import_module("hiveweave.conversation.compaction")
        importlib.reload(comp)
        assert comp._compaction_ratio() == 0.50
    # 恢复默认
    comp = importlib.import_module("hiveweave.conversation.compaction")
    importlib.reload(comp)
    assert comp.COMPACTION_TRIGGER_RATIO == 0.70
