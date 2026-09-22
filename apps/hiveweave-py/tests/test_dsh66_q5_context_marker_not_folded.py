"""TEST_DSH_66 Q5 —— **「27 条分界行是重复」这一命题被证伪**的守卫。

## 结论（2026-09-22，现场 TEST_DSH_66 库全量实测）

开工单 Q5 的描述是「8 小时里 27 条同款 `role='system'` 分界行（观感：
机械式多余提醒）」，并给了两个修法选项。**实测数据不支持"重复"这个前提**，
故本批**不修**（`store.py` 保持 HEAD 原样），只落本文件把结论钉住，
免得下一轮又有人照"观感"去写折叠。

### 判定依据（三条，全部可复算）

1. **相邻同 agent 同 kind 的间隔最小 = 113.088s**。现场 31 行（prune 27 +
   compaction 4）⇒ 相邻同 (agent, kind) 间隔共 **28 个**，全部落在
   `[113.088s, 4567.305s]`。**没有任何一对在 5s 内**（更别说 1s / 60s）。
   ⇒ 任何"窗口折叠"（60s 桶 / 绝对 ±1s / 滚动锚 1s）在这份数据上
   **收益恒为 0**：27 → 27。

2. **27 个 marker 一一对应 27 个互不相同的 run 收口时刻**（零重复）。
   每个 marker 距其最近的 run-step `ended_at` 的 |Δ| ∈ [0.034s, 1.006s]
   （26/27 条；剩 1 条 22.5s）。⇒ 每个 prune 是**一次独立 run**收口时
   真落地的一次裁剪，**不是同一次操作的重复播报**。

3. **UI 语义否决折叠**：`ContextMarkerRow`（`apps/web/src/chat/
   MessageBubble.tsx:340`）是**带位置的**分界线元素 —— 它在消息流里占
   一格，语义是「分界线**以上**的输出模型已看不见」。删掉中间任一条
   ⇒ 该边界整体下移，**对用户撒谎**（把模型其实还记得的区段说成已丢）。
   开工单本身也写了：「UI 若依赖**每条**标记做定位 ⇒ 只能选 ②」。

### 为什么两个候选修法都不选

- **选项 ① 窗口折叠**：前提（有重复）已被第 1、2 条否掉；即便强行做，
  也会因第 3 条**篡改边界语义**。**不做。**
- **选项 ② 读时派生**（不落行、UI 按占位符画）：语义上安全，但它是
  **前端+存储契约改动**，收益只是"少几行 `chat_messages`"——
  27 行 / 8 小时不是任何性能或可读性瓶颈（真正的可读性问题是**行本身
  长得一样**，那是文案问题，不是行数问题）。**收益不足，本批不做。**

⇒ 若后续仍要动 Q5，**先过本文件的第 1、2 条**（用现场库数一遍），
   确认"重复"真的出现了再动手。

## 本文件的两类用例

- **A 类：钉住"不该折叠"的机制现状** —— 连续两次 emit（含容差内抖动）
  必须落 **2** 行。这是**反向守卫**：谁要是加了折叠，这里转红。
- **B 类：钉住判据的现场可复算性** —— 用真实库复算第 1 条（最小间隔
  ≥ 真实阈值）。库不在时 skip（不假装通过）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.conversation.store import (
    CONTEXT_MARKER_KIND_COMPACTION,
    CONTEXT_MARKER_KIND_PRUNE,
    ConversationStore,
)

AGENT = "11111111-1111-1111-1111-111111111111"
OTHER_AGENT = "22222222-2222-2222-2222-222222222222"

#: 现场库（开工单所指项目）。缺失时 B 类用例 skip，**不**假装通过。
FIELD_DB = Path("D:/PC_AI/Project/HiveTestProject/TEST_DSH_66/.hiveweave/data.db")

#: 第 1 条实测：相邻同 (agent, kind) 的最小间隔。
#: 用它当反向守卫的**下界** —— 只要现场真有重复，这个数会掉到秒级，
#: 那时本断言转红，提示"前提可能变了，去复核 Q5"。
FIELD_MIN_ADJACENT_GAP_S = 100.0


async def _rows(project_id: str, agent_id: str) -> list[dict]:
    """读该 agent 的全部 context_marker 行（按时间正序）。"""
    from hiveweave.db import project as project_db

    conn = await project_db.get_project_db_by_project_id(project_id)
    cur = await conn.execute(
        "SELECT id, created_at, json_extract(metadata, '$.context_marker') AS kind "
        "FROM chat_messages WHERE agent_id = ? AND role = 'system' "
        "AND json_extract(metadata, '$.context_marker') IS NOT NULL "
        "ORDER BY created_at ASC",
        [agent_id],
    )
    out = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    return out


@pytest.fixture
async def env():
    """临时 workspace + 项目库。

    路由链（两条入口都只依赖 `get_project_workspace`）：
    - `save_message` → `get_project_db_for_agent` → `meta.get_agent_project_id`
    - `_rows` → `get_project_db_by_project_id` → `meta.get_project_workspace`
    ⇒ 只 patch `get_project_workspace`（+ agent→project 映射）即可全覆盖，
    不 mock `ensure_project_db`（要跑真 schema 迁移）。
    """
    from hiveweave.db import project as project_db

    project_id = "aaaaaaaa-0000-0000-0000-000000000001"
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace if pid == project_id else None

        async def fake_agent_project(aid: str):
            return project_id if aid in (AGENT, OTHER_AGENT) else None

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws), patch(
            "hiveweave.db.meta.get_agent_project_id", fake_agent_project
        ):
            await project_db.ensure_project_db(workspace)
            try:
                yield {"project_id": project_id, "workspace": workspace}
            finally:
                project_db._agent_cache.pop(AGENT, None)
                project_db._agent_cache.pop(OTHER_AGENT, None)
                async with project_db._ensure_lock:
                    conn = project_db._cache.pop(workspace, None)
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass


# ── A 类：反向守卫 —— 不该折叠 ─────────────────────────────


@pytest.mark.asyncio
async def test_two_close_emits_both_persist(env):
    """⚠ 反向守卫：紧邻两次 emit（0.05s，任何折叠窗口都会命中）必须 **2** 行。

    这是本文件的核心断言，方向与常规修复相反 —— 它保护的是
    「每个 marker 都是一次真实操作的位置标记」这个语义。
    **谁加了折叠，这里转红。**
    """
    pid = env["project_id"]
    store = ConversationStore()

    await store._emit_context_marker(
        AGENT, CONTEXT_MARKER_KIND_PRUNE, pruned_count=1, pruned_tokens=10,
    )
    await asyncio.sleep(0.05)
    await store._emit_context_marker(
        AGENT, CONTEXT_MARKER_KIND_PRUNE, pruned_count=2, pruned_tokens=20,
    )

    rows = await _rows(pid, AGENT)
    assert len(rows) == 2, (
        f"两次独立 prune 必须各留一条边界行，实际 {len(rows)} 行 —— "
        f"若为 1 说明有人加了折叠。⚠ 折叠会**篡改边界语义**："
        f"`ContextMarkerRow` 是带位置的分界线，"
        f"语义是「以上模型已看不见」，删中间任一条会让该边界下移、"
        f"把模型其实还记得的区段说成已丢（对用户撒谎）。详见本文件 docstring。"
    )


@pytest.mark.asyncio
async def test_compaction_and_prune_markers_coexist(env):
    """两种 kind 是**不同事实**，各自独立落行（互不影响）。"""
    pid = env["project_id"]
    store = ConversationStore()

    await store._emit_context_marker(
        AGENT, CONTEXT_MARKER_KIND_PRUNE, pruned_count=3, pruned_tokens=30,
    )
    await store._emit_context_marker(
        AGENT, CONTEXT_MARKER_KIND_COMPACTION, kept=5, has_summary=True,
    )

    rows = await _rows(pid, AGENT)
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["compaction", "prune"], f"实际 {kinds}"


@pytest.mark.asyncio
async def test_markers_of_different_agents_are_independent(env):
    """不同 agent 的标记互不干扰（各自计数）。"""
    pid = env["project_id"]
    store = ConversationStore()

    await store._emit_context_marker(
        AGENT, CONTEXT_MARKER_KIND_PRUNE, pruned_count=1, pruned_tokens=10,
    )
    await store._emit_context_marker(
        OTHER_AGENT, CONTEXT_MARKER_KIND_PRUNE, pruned_count=1, pruned_tokens=10,
    )

    assert len(await _rows(pid, AGENT)) == 1
    assert len(await _rows(pid, OTHER_AGENT)) == 1


# ── B 类：现场可复算（库缺失则 skip）─────────────────────


def _field_rows() -> list[tuple[str, int, str]] | None:
    """从现场库读 (agent_id, created_at, kind)；库不在返回 None。"""
    if not FIELD_DB.exists():
        return None
    c = sqlite3.connect(str(FIELD_DB))
    try:
        return [
            (r[0], r[1], r[2])
            for r in c.execute(
                "SELECT agent_id, created_at, "
                "json_extract(metadata, '$.context_marker') AS k "
                "FROM chat_messages WHERE role = 'system' "
                "AND json_extract(metadata, '$.context_marker') IS NOT NULL "
                "ORDER BY created_at ASC"
            )
        ]
    finally:
        c.close()


def test_field_adjacent_gaps_have_no_duplicates():
    """第 1 条复算：现场相邻同 (agent, kind) 间隔**全部** ≥ 下界。

    若此断言转红，说明现场真的出现了秒级相邻对（"重复播报"），
    那时才应重新评估 Q5（并改本文件的第 2 条）。
    """
    rows = _field_rows()
    if rows is None:
        pytest.skip(f"现场库不存在，跳过复算：{FIELD_DB}")

    gaps: list[float] = []
    prev_key: tuple[str, str] | None = None
    prev_ts: int | None = None
    for agent_id, ts, kind in rows:
        key = (agent_id, kind)
        if key == prev_key and prev_ts is not None:
            gaps.append((ts - prev_ts) / 1000.0)
        prev_key, prev_ts = key, ts

    assert gaps, "应至少有一对相邻同 kind 标记 —— 若为空说明查询口径变了"
    smallest = min(gaps)
    assert smallest >= FIELD_MIN_ADJACENT_GAP_S, (
        f"现场相邻同 kind 最小间隔 = {smallest:.2f}s，"
        f"低于下界 {FIELD_MIN_ADJACENT_GAP_S}s ⇒ **现场出现了近邻重复**，"
        f"Q5 的『重复』前提这次可能成立，请重新取数评估（别直接照本文件的结论）。"
    )


def test_field_markers_map_to_distinct_runs():
    """第 2 条复算：每个 marker 距最近的 run-step 收口 ≤ 1.1s，且**不共享**。

    共享（两个以上 marker 落在同一个 `ended_at`）才叫"同一次操作重复播报"。
    实测：27 个 marker → 27 个互不相同的收口时刻（零共享）。
    """
    rows = _field_rows()
    if rows is None:
        pytest.skip(f"现场库不存在，跳过复算：{FIELD_DB}")

    c = sqlite3.connect(str(FIELD_DB))
    try:
        ends = sorted(
            r[0]
            for r in c.execute(
                "SELECT DISTINCT ended_at FROM run_steps "
                "WHERE ended_at IS NOT NULL ORDER BY ended_at"
            )
        )
    finally:
        c.close()
    if not ends:
        pytest.skip("run_steps 无收口时刻，无法复算")

    import bisect

    markers = [ts for _aid, ts, kind in rows if kind == "prune"]
    assert markers, "现场应有 prune 标记"

    nearest: list[int] = []
    for ts in markers:
        i = bisect.bisect_left(ends, ts)
        cands = [ends[j] for j in (i - 1, i) if 0 <= j < len(ends)]
        if cands:
            nearest.append(min(cands, key=lambda x: abs(ts - x)))

    # ① 每个 marker 都钉在某个 run 收口附近（1.1s 内）
    pinned = sum(1 for m, e in zip(markers, nearest) if abs(m - e) <= 1100)
    assert pinned >= len(markers) - 1, (
        f"{len(markers)} 个 prune 中只有 {pinned} 个落在 run 收口 1.1s 内 —— "
        f"若显著变少说明 marker 的产生时机改了，本结论需重估"
    )
    # ② 不共享：收口时刻互不相同 ⇒ 每个 marker = 一次独立 run
    assert len(set(nearest)) == len(nearest), (
        f"出现共享收口时刻 —— 即多个 marker 锚在同一次 run 收口上，"
        f"那才是『同一次操作重复播报』：{len(nearest)} 个 marker → "
        f"{len(set(nearest))} 个不同收口"
    )


# ── C 类：文案与时机契约（与折叠无关，防顺手改坏）──────────


@pytest.mark.asyncio
async def test_marker_records_counts_in_metadata(env):
    """prune 的 pruned_count/pruned_tokens 必须进 metadata（前端/取证要用）。"""
    pid = env["project_id"]
    store = ConversationStore()

    await store._emit_context_marker(
        AGENT, CONTEXT_MARKER_KIND_PRUNE, pruned_count=7, pruned_tokens=1234,
    )

    from hiveweave.db import project as project_db

    conn = await project_db.get_project_db_by_project_id(pid)
    cur = await conn.execute(
        "SELECT metadata FROM chat_messages WHERE agent_id = ? "
        "AND json_extract(metadata, '$.context_marker') = 'prune'",
        [AGENT],
    )
    row = await cur.fetchone()
    await cur.close()

    import json

    meta = json.loads(row[0])
    assert meta["pruned_count"] == 7
    assert meta["pruned_tokens"] == 1234


@pytest.mark.asyncio
async def test_emit_failure_does_not_raise(env, monkeypatch):
    """落标记失败**绝不影响**已完成的压缩/裁剪（标记是展示层补充）。

    契约（docstring）：「标记只在操作实际落地后发出，且失败绝不影响已完成
    的压缩/裁剪」。故 save_message 抛错时 `_emit_context_marker` 必须吞掉。
    """
    from hiveweave.services import chat_message as cms

    async def boom(self, payload):
        raise RuntimeError("simulated save failure")

    monkeypatch.setattr(cms.ChatMessageService, "save_message", boom)

    store = ConversationStore()
    # 不抛 = 通过
    await store._emit_context_marker(
        AGENT, CONTEXT_MARKER_KIND_PRUNE, pruned_count=1, pruned_tokens=1,
    )
    assert True, "落标记失败必须被吞掉（不影响主流程）"
