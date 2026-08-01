"""记忆窗口压缩（token 预算）回归 — 压缩随对话压缩同步触发。

注入模型（2026-08-01 用户钦定）：
- 每轮只注入 project 共享层（build_project_context）；agent 私有记忆不每轮注入。
- 对话压缩后（store._do_compaction）把 agent 记忆快照追加到 compacted_prefix。
- 记忆压缩只在对话压缩时同步触发一次（maybe_compact_agent_memories），
  未压缩记忆 ≥20 条才压：合并最老 8 条 + 旧摘要 → LLM 新摘要，旧条目打标不删。

覆盖：
1. 注入窗口：get_agent_memories 只返回最新 10 条未压缩记忆（DESC）；
   全量查询 get_all_agent_memories 返回全部。
2. 压缩触发：19 条不压，≥20 条压缩（最老 8 条打标 + 摘要 upsert，窗口回落）。
3. LLM 失败/异常回退：硬裁剪保最新 10 条。
4. 历史可查：压缩后旧条目仍可经 read_memory 工具召回。
5. 二次压缩：新摘要合并旧摘要。
6. 分层注入：build_project_context 只含 project 层；build_agent_context 只含 agent 层。
7. 对话压缩集成：store._do_compaction 后 compacted_prefix 含记忆快照。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.conversation.compaction import SUMMARY_MARKER
from hiveweave.conversation.store import ConversationStore
from hiveweave.services.memory import MemoryService
from hiveweave.tools.orchestration_tools import (
    ReadMemoryParams,
    read_memory_tool,
)

from tests.test_idle_architecture_p0 import EXEC, task_env  # noqa: F401


@pytest.fixture(autouse=True)
def _clear_memory_state():
    from hiveweave.services import memory as memory_module

    memory_module._cache.clear()
    memory_module._compact_locks.clear()
    yield
    memory_module._cache.clear()
    memory_module._compact_locks.clear()


async def _write_many(pid: str, n: int, prefix: str = "m"):
    """写入 n 条 agent 记忆（module_id=None → INSERT 路径）。"""
    mem = MemoryService()
    for i in range(n):
        await mem.add_entry(EXEC, pid, f"{prefix}-{i}")


async def _fresh_count(pid: str) -> int:
    mem = MemoryService()
    allm = await mem.get_all_agent_memories(EXEC, pid)
    return sum(
        1
        for m in allm
        if not (m.get("metadata") or {}).get("compressed")
        and m.get("type") != "compressed_summary"
    )


# ── 1. 注入窗口 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_window_limits_to_latest_10_fresh_desc(task_env):
    """12 条记忆 → 窗口只显示最新 10 条（DESC），全量查询得 12 条。"""
    pid = task_env["project_id"]
    await _write_many(pid, 12)

    mem = MemoryService()
    window = await mem.get_agent_memories(EXEC, pid)
    assert len(window) == 10
    assert window[0]["content"] == "m-11"  # 最新在前
    assert window[-1]["content"] == "m-2"

    allm = await mem.get_all_agent_memories(EXEC, pid)
    assert len(allm) == 12


# ── 2. 压缩触发（随对话压缩同步，≥20 条）─────────────────


@pytest.mark.asyncio
async def test_compaction_not_triggered_below_20(task_env):
    """19 条未压缩 → maybe_compact_agent_memories 不压缩（<20）。"""
    pid = task_env["project_id"]
    calls: list[str] = []

    async def fake_compactor(prompt: str) -> str:
        calls.append(prompt)
        return "compressed"

    with patch(
        "hiveweave.services.memory._resolve_compactor_callback",
        return_value=fake_compactor,
    ):
        await _write_many(pid, 19)
        assert await _fresh_count(pid) == 19

        mem = MemoryService()
        assert await mem.maybe_compact_agent_memories(EXEC, pid) is False
        assert calls == []
        assert await _fresh_count(pid) == 19


@pytest.mark.asyncio
async def test_compaction_triggered_at_20_with_llm_summary(task_env):
    """20 条未压缩 → 压缩：最老 8 条打标，新摘要写入，窗口回落。"""
    pid = task_env["project_id"]
    calls: list[str] = []

    async def fake_compactor(prompt: str) -> str:
        calls.append(prompt)
        return f"compressed-{len(calls)}"

    with patch(
        "hiveweave.services.memory._resolve_compactor_callback",
        return_value=fake_compactor,
    ):
        await _write_many(pid, 20)
        mem = MemoryService()
        assert await mem.maybe_compact_agent_memories(EXEC, pid) is True
        assert len(calls) == 1

    allm = await mem.get_all_agent_memories(EXEC, pid)
    compressed = [
        m for m in allm if (m.get("metadata") or {}).get("compressed")
    ]
    assert len(compressed) == 8  # 最老 8 条打标，不删除

    summaries = [
        m for m in allm if m.get("type") == "compressed_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["content"] == "compressed-1"

    # 窗口回落：10 条未压缩（20 - 8 = 12 fresh，窗口只显示 10）
    window = await mem.get_agent_memories(EXEC, pid)
    assert len(window) == 10
    assert all(
        not (m.get("metadata") or {}).get("compressed") for m in window
    )
    assert await _fresh_count(pid) == 12

    # 快照文本含窗口 + 摘要两块
    ctx = await mem.build_agent_context(EXEC, pid)
    assert ctx is not None
    assert "## Your Private Working Memory" in ctx
    assert "## Older Memories (compressed summary" in ctx
    assert "compressed-1" in ctx


# ── 3. LLM 失败回退：硬裁剪 ───────────────────────────────


@pytest.mark.asyncio
async def test_compaction_llm_failure_hard_trims(task_env):
    """LLM 回调失败/返回空 → 硬裁剪保最新 10 条原文。"""
    pid = task_env["project_id"]

    async def broken_compactor(prompt: str) -> str | None:
        return None

    with patch(
        "hiveweave.services.memory._resolve_compactor_callback",
        return_value=broken_compactor,
    ):
        await _write_many(pid, 20)
        mem = MemoryService()
        await mem.maybe_compact_agent_memories(EXEC, pid)

    allm = await mem.get_all_agent_memories(EXEC, pid)
    assert len(allm) == 10  # 删掉最老 10 条
    assert not any((m.get("metadata") or {}).get("compressed") for m in allm)
    assert not any(m.get("type") == "compressed_summary" for m in allm)
    assert allm[0]["content"] == "m-19"  # 最新保留


@pytest.mark.asyncio
async def test_compaction_llm_exception_hard_trims(task_env):
    """LLM 回调抛异常 → 同样回退硬裁剪（不吞错导致崩溃）。"""
    pid = task_env["project_id"]

    async def exploding_compactor(prompt: str) -> str:
        raise RuntimeError("llm down")

    with patch(
        "hiveweave.services.memory._resolve_compactor_callback",
        return_value=exploding_compactor,
    ):
        await _write_many(pid, 20)
        mem = MemoryService()
        await mem.maybe_compact_agent_memories(EXEC, pid)

    allm = await mem.get_all_agent_memories(EXEC, pid)
    assert len(allm) == 10


# ── 4. 历史可查 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_old_entries_queryable_after_compaction(task_env):
    """压缩后旧条目仍可经 read_memory 工具全量召回（含被压条目）。"""
    pid = task_env["project_id"]

    async def fake_compactor(prompt: str) -> str:
        return "summary-of-old"

    with patch(
        "hiveweave.services.memory._resolve_compactor_callback",
        return_value=fake_compactor,
    ):
        await _write_many(pid, 20)
        mem = MemoryService()
        await mem.maybe_compact_agent_memories(EXEC, pid)

    with patch(
        "hiveweave.tools.orchestration_tools.get_project_id",
        return_value=pid,
    ):
        r = await read_memory_tool(
            ReadMemoryParams(), agent_id=EXEC,
            workspace=task_env["workspace"],
        )
        assert r.success
        # 被压条目（最早写入的 m-0）仍可召回
        assert "m-0" in r.output
        # 摘要条目也可见（全量查询含 compressed_summary 类型）
        assert "summary-of-old" in r.output


# ── 5. 二次压缩合并旧摘要 ─────────────────────────────────


@pytest.mark.asyncio
async def test_second_compaction_merges_previous_summary(task_env):
    """第二轮压缩的 LLM 输入包含旧摘要内容（合并而非覆盖）。"""
    pid = task_env["project_id"]
    calls: list[str] = []

    async def fake_compactor(prompt: str) -> str:
        calls.append(prompt)
        return f"compressed-{len(calls)}"

    with patch(
        "hiveweave.services.memory._resolve_compactor_callback",
        return_value=fake_compactor,
    ):
        await _write_many(pid, 20)  # 第一轮：压缩 8 条 → 摘要 compressed-1
        mem = MemoryService()
        await mem.maybe_compact_agent_memories(EXEC, pid)
        assert len(calls) == 1
        assert await _fresh_count(pid) == 12

        await _write_many(pid, 8, prefix="s2")  # 12+8=20 → 第二轮
        await mem.maybe_compact_agent_memories(EXEC, pid)
        assert len(calls) == 2

    # 第二轮输入含旧摘要
    assert "## Previous compressed summary" in calls[1]
    assert "compressed-1" in calls[1]

    allm = await mem.get_all_agent_memories(EXEC, pid)
    summaries = [
        m for m in allm if m.get("type") == "compressed_summary"
    ]
    assert len(summaries) == 1  # upsert 不新增
    assert summaries[0]["content"] == "compressed-2"

    compressed = [
        m for m in allm if (m.get("metadata") or {}).get("compressed")
    ]
    assert len(compressed) == 16  # 8 + 8
    assert await _fresh_count(pid) == 12


# ── 6. 分层注入 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_and_agent_layers_inject_separately(task_env):
    """build_project_context 只含 project 层；build_agent_context 只含 agent 层。"""
    pid = task_env["project_id"]
    mem = MemoryService()
    await mem.add_entry(EXEC, pid, "agent-secret")
    await mem.save_memory(
        EXEC, pid, scope="project", content="shared-constitution",
    )

    proj = await mem.build_project_context(pid)
    assert proj is not None
    assert "shared-constitution" in proj
    assert "agent-secret" not in proj

    agent = await mem.build_agent_context(EXEC, pid)
    assert agent is not None
    assert "agent-secret" in agent
    assert "shared-constitution" not in agent


# ── 7. 对话压缩集成：快照挂到 compacted_prefix ────────────


def _make_history(agent_id: str, n: int = 6) -> list[dict]:
    """构造会触发压缩的历史消息（assistant + tool 对）。"""
    msgs: list[dict] = []
    for i in range(n):
        msgs.append(
            {"role": "user", "content": f"question {i}"}
        )
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
        msgs.append(
            {"role": "tool", "tool_call_id": f"tc-{i}", "content": "tool out"}
        )
    return msgs


@pytest.mark.asyncio
async def test_conversation_compaction_appends_memory_snapshot(task_env):
    """store._do_compaction 后 compacted_prefix 含记忆快照（快照随摘要持久化）。"""
    pid = task_env["project_id"]

    async def fake_conv_compactor(prompt: str) -> str:
        return "conversation summary"

    from hiveweave.db.project import ensure_project_db, get_project_db_for_agent

    # agents 行：compacted_prefix 持久化到 agents 表，需先有该 agent。
    # task_env 的 agent 未注册 Meta DB，故经 workspace 连接直插；
    # 持久化路径（project_db.execute/query_one）同样 patch get_project_db_for_agent。
    now_ms = int(__import__("time").time() * 1000)
    conn = await ensure_project_db(task_env["workspace"])
    await conn.execute(
        "INSERT OR REPLACE INTO agents (id, project_id, name, role, "
        "permission_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [EXEC, pid, "exec-1", "executor", "executor", now_ms, now_ms],
    )
    await conn.commit()

    store = ConversationStore()
    await _write_many(pid, 3, prefix="mem")

    # 种子对话缓存（_do_compaction 持锁后从 _cache 重读，治根 audit T1#2）
    store._cache[(pid, EXEC)] = _make_history(EXEC)

    # 持久化路径（project_db.execute/query_one → get_project_db_for_agent）
    # 指向 workspace 连接，绕过 Meta DB 注册
    db_agent_patch = patch(
        "hiveweave.db.project.get_project_db_for_agent",
        AsyncMock(return_value=conn),
    )
    db_agent_patch.start()
    try:
        with patch(
            "hiveweave.conversation.store.resolve_compactor_callback",
            return_value=fake_conv_compactor,
        ):
            await store._do_compaction(
                EXEC, pid, (pid, EXEC), _make_history(EXEC), 200
            )
    finally:
        db_agent_patch.stop()

    prefix = store.get_compacted_prefix(pid, EXEC)
    assert prefix is not None
    assert SUMMARY_MARKER in prefix
    assert "conversation summary" in prefix
    assert "## Current Memory Snapshot" in prefix
    assert "mem-2" in prefix  # 记忆快照内容（最新条）

    # 持久化：重启后从 DB 加载的 prefix 也含快照（compacted_prefix 列）
    store2 = ConversationStore()
    with patch(
        "hiveweave.db.project.get_project_db_for_agent",
        AsyncMock(return_value=conn),
    ):
        await store2._load_compacted_prefix(EXEC, pid)
    prefix2 = store2.get_compacted_prefix(pid, EXEC)
    assert prefix2 is not None
    assert "## Current Memory Snapshot" in prefix2
    assert "mem-2" in prefix2
