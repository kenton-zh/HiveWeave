"""批次 7 · 记忆下沉的**主动入口** + ``memories.module_id`` 回指真模块。

背景（团队交付面 C）：``archive`` / ``compacted_prefix`` 此前**只有对话压缩
被动触发**，没有任何主动/按需入口；且 M3 兜底让归档记忆的 ``module_id``
存的是**前任的 agent_id** 而非真实模块 id，使蓝图
``docs/AI工程组织_MVP蓝图.md:299`` 的「按模块取前任经验」实际退化成
「按前任 id 取」。

本文件钉住两件事：
1. ``MemoryService.consolidate_memories`` —— 主动压缩真的压缩（真落摘要条目）、
   真的守阈值、真返回诊断；
2. ``archive_agent_memories(module_id=...)`` —— 归档写**真实模块 id**，
   且继任者经 ``ModuleService.get_archived_memories_for_module`` 取得到。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db.project import close_all
from hiveweave.services.memory import (
    _COMPRESSED_SUMMARY_TYPE,
    MemoryService,
)
from hiveweave.services.modules import ModuleService


async def _make_project(tmp_path: Path) -> tuple[str, str]:
    project_id = f"memsink-{uuid.uuid4().hex[:12]}"
    ws = tmp_path / project_id
    ws.mkdir(parents=True, exist_ok=True)
    await meta_db.init_meta_db()
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [project_id, "Memory Sink Test", str(ws), int(time.time() * 1000)],
    )
    return project_id, str(ws)


@pytest.fixture
async def project(tmp_path):
    project_id, ws = await _make_project(tmp_path)
    yield project_id, ws
    await close_all()


async def _seed_memories(
    mem: MemoryService, project_id: str, agent_id: str, n: int
) -> None:
    for i in range(n):
        await mem.save_memory(
            agent_id=agent_id, project_id=project_id, scope="agent",
            content=f"fact {i}", type="fact",
        )


# ── 1 · 主动压缩入口 ───────────────────────────────────────


async def test_consolidate_force_runs_compaction_below_threshold(project):
    """``force=True`` 在未到阈值时**真的跑了压缩路径** —— 这是「主动」与
    「被动」的差别所在。

    注意断言的是「跑没跑」（诊断 + 未压缩窗口收缩），不是「一定写出摘要」：
    没有可用 LLM 时压缩走**硬裁剪回退**（删最老条目），只有在条目数超过
    注入窗口（``_FRESH_MEMORY_MAX=10``）时才会真的删 —— 见下一个用例。
    这里条目少（3 条 < 10），所以回退路径下「无操作」是正确行为；
    本用例用**打桩 LLM** 走摘要路径，验证摘要真的落库。
    """
    project_id, _ = project
    mem = MemoryService()
    await _seed_memories(mem, project_id, "a1", 3)

    fresh_before = await mem._count_fresh("a1", project_id)
    assert fresh_before == 3

    async def _fake_compactor(_prompt: str) -> str:
        return "- merged summary of fact 0..2"

    import hiveweave.services.memory as memory_mod

    orig = memory_mod._resolve_compactor_callback
    memory_mod._resolve_compactor_callback = lambda _aid: _async_ret(
        _fake_compactor
    )
    try:
        diag = await mem.consolidate_memories("a1", project_id, force=True)
    finally:
        memory_mod._resolve_compactor_callback = orig

    assert diag["compacted"] is True, f"force=True 竟然没压：{diag}"
    assert diag["fresh_before"] == 3
    assert diag["success"] is True, f"打了桩 LLM 却没走摘要路径：{diag}"

    # 真的产生了压缩摘要条目（不是「返回 True 但啥也没写」的假绿）
    summary = await mem.get_compressed_summary("a1", project_id)
    assert summary is not None, "报了压缩但库里没有 compressed_summary 条目"
    assert summary["type"] == _COMPRESSED_SUMMARY_TYPE
    assert "merged summary" in summary["content"]


async def _async_ret(value):
    return value


async def test_consolidate_without_force_respects_threshold(project):
    """``force=False``（默认）在未到阈值时不压 —— 主动不等于无条件。"""
    project_id, _ = project
    mem = MemoryService()
    await _seed_memories(mem, project_id, "a2", 3)

    diag = await mem.consolidate_memories("a2", project_id)
    assert diag["compacted"] is False
    assert diag["reason"] == "below_trigger"
    assert diag["fresh_before"] == 3
    # 没压就不该有摘要条目
    assert await mem.get_compressed_summary("a2", project_id) is None


async def test_consolidate_returns_diagnostics_not_just_bool(project):
    """诊断字段齐备 —— 「调了但无事发生」不能是静默点。"""
    project_id, _ = project
    mem = MemoryService()
    await _seed_memories(mem, project_id, "a3", 1)
    diag = await mem.consolidate_memories("a3", project_id)
    for key in ("compacted", "reason", "fresh_before", "success"):
        assert key in diag, f"诊断缺字段 {key}：{diag}"


async def test_consolidate_force_trims_when_llm_unavailable(project):
    """无 LLM 时的回退路径在条目超窗口时**真的收缩窗口**（有损但 token 有界）。

    这是主动入口的兜底价值：长活前主动压一次，即使没有 LLM 也能把窗口压回
    上限，不依赖对话压缩被动触发。
    """
    project_id, _ = project
    mem = MemoryService()
    # 灌到明显超过 _FRESH_MEMORY_MAX(10)，触发回退裁剪
    await _seed_memories(mem, project_id, "a4", 15)

    assert await mem._count_fresh("a4", project_id) == 15
    diag = await mem.consolidate_memories("a4", project_id, force=True)
    assert diag["compacted"] is True
    assert diag["success"] is False, "测试环境无 LLM，应走硬裁剪回退"
    # 窗口被压回上限（保最新 10 条）
    assert await mem._count_fresh("a4", project_id) == 10


# ── 2 · 归档写真实 module_id（蓝图 :299 的写侧接线）────────


async def test_archive_writes_real_module_id_when_given(project):
    """给了 module_id → 归档记忆挂在**真模块**上（不是 M3 的 agent_id 兜底）。"""
    project_id, ws = project
    mem = MemoryService()
    mods = ModuleService()
    mod = await mods.create_module(project_id, "auth")

    await _seed_memories(mem, project_id, "ex-dev", 2)
    archived = await mem.archive_agent_memories(
        "ex-dev", project_id, module_id=mod["id"]
    )
    assert archived == 2

    # 直连 DB 核验 module_id 列真的是模块 id（不经服务的读路径）
    from hiveweave.db.project import ensure_project_db

    conn = await ensure_project_db(ws)
    cursor = await conn.execute(
        "SELECT module_id FROM memories WHERE agent_id = ? AND scope = 'archive'",
        ["ex-dev"],
    )
    rows = await cursor.fetchall()
    await cursor.close()
    assert rows, "归档后查不到行"
    assert {r["module_id"] for r in rows} == {mod["id"]}, (
        "归档记忆的 module_id 不是真实模块 id（M3 兜底又生效了？）"
    )


async def test_archive_falls_back_to_agent_id_without_module(project):
    """不给 module_id → 沿用 M3 兜底（填 agent_id），不破坏既有行为。"""
    project_id, _ = project
    mem = MemoryService()
    await _seed_memories(mem, project_id, "ex-dev-2", 1)
    await mem.archive_agent_memories("ex-dev-2", project_id)

    # M3 语义：继任者可经 get_archived_memories(project_id, agent_id) 检索
    via_agent = await mem.get_archived_memories(project_id, "ex-dev-2")
    assert len(via_agent) == 1
    assert via_agent[0]["module_id"] == "ex-dev-2"


async def test_archive_preserves_existing_module_id(project):
    """写入时就挂了真模块的条目，归档不该被覆盖成兜底值。"""
    project_id, _ = project
    mem = MemoryService()
    mods = ModuleService()
    mod = await mods.create_module(project_id, "precise")

    # 一条挂了真模块、一条没挂
    await mem.save_memory(
        agent_id="ex-dev-3", project_id=project_id, scope="agent",
        content="tagged", module_id=mod["id"],
    )
    await mem.save_memory(
        agent_id="ex-dev-3", project_id=project_id, scope="agent",
        content="untagged",
    )

    await mem.archive_agent_memories(
        "ex-dev-3", project_id, module_id=mod["id"]
    )
    entries = await mem.get_archived_memories(project_id, mod["id"])
    by_content = {e["content"]: e["module_id"] for e in entries}
    assert by_content["tagged"] == mod["id"]
    assert by_content["untagged"] == mod["id"]


# ── 3 · 端到端：模块 → 归档 → 继任者按模块取回 ────────────


async def test_successor_retrieves_predecessor_by_module(project):
    """蓝图 :299 的完整链路：解散下属 → 归档挂真模块 → 继任者按模块取回。"""
    project_id, _ = project
    mem = MemoryService()
    mods = ModuleService()
    mod = await mods.create_module(project_id, "billing")

    await _seed_memories(mem, project_id, "predecessor", 2)
    await mem.archive_agent_memories(
        "predecessor", project_id, module_id=mod["id"]
    )

    # 继任者（新 agent）按模块取前任经验
    got = await mods.get_archived_memories_for_module(project_id, mod["id"])
    contents = {e["content"] for e in got}
    assert contents == {"fact 0", "fact 1"}
    # 归因：知道自己是从这个模块捞上来的
    assert {e["_via_module_id"] for e in got} == {mod["id"]}
