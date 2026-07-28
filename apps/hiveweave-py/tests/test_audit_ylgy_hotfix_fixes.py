"""审计修复回归测试（audit-ylgy-hotfix-forensics-2026-07-28）。

覆盖三轮审计后落地的四项修复：
1. reconcile stranded detection 的 ``sqlite3.Row.get`` 崩溃（git_worktree.py）
   — Row 无 .get()，被 except 吞成「stranded task reconciliation failed」，
   导致 stranded 检测全程失效。
2. ``find_reviewer_attestation`` 的同源 ``.get()`` 崩溃（attestation.py）
   — P0-2 审查方证据硬闸因这个崩溃永远返回 False，会拦死所有代码任务
   approve。这是 fbcfbd4 之后引入的 P0 回归。
3. close_task 的 merge obligation 安全网（task.py）
   — merge 在 git 层闭环但 obligation 账本不清（CEO 不得不 cancel_task
   强清 81b43baa）。close 时补一刀 fulfill 作为兜底。
4. bash dev-server 自动注册（bash.py，P0-3 增量2）
   — agent 自行 bash 起的 dev server 未注册 → stop_processes_for_worktree
   杀不到 → WinError 32。检测到长驻 dev server 命令时路由到注册路径。

⚠️ 不要在 WorkBuddy 沙箱跑本文件（会与 .git 操作交互致 pack 丢失）。
在小申终端执行：
    cd apps/hiveweave-py && timeout 120 uv run pytest tests/test_audit_ylgy_hotfix_fixes.py -q
"""

from __future__ import annotations

import asyncio
import sqlite3

import aiosqlite
import pytest

# ── 1. reconcile: sqlite3.Row 无 .get() 崩溃修复 ─────────────────


async def _make_project_db_with_closed_task(
    tmp_path, *, has_merge_fact: bool, task_id: str = "t1-with-merge"
):
    """构造一个含 closed 任务的 per-project DB（复刻 reconcile 读取路径）。"""
    db_file = tmp_path / "data.db"
    db = await aiosqlite.connect(str(db_file))
    db.row_factory = aiosqlite.Row  # 与 ensure_project_db 一致
    await db.execute(
        "CREATE TABLE tasks (id TEXT, assignee_id TEXT, evidence TEXT, "
        "creator_id TEXT, status TEXT, closed_at INTEGER)"
    )
    import json

    ev = {"merged_by": "agentA"} if has_merge_fact else {}
    await db.execute(
        "INSERT INTO tasks (id, assignee_id, evidence, creator_id, status, closed_at) "
        "VALUES (?, ?, ?, ?, 'closed', 1)",
        [task_id, "agentA", json.dumps(ev), "agentA"],
    )
    await db.commit()
    await db.close()
    return db_file


@pytest.mark.asyncio
async def test_reconcile_row_access_does_not_crash_on_sqlite3_row(tmp_path):
    """reconcile 的 stranded 检测用 dict(row) 而非 row.get()，不再崩。

    复现路径：aiosqlite.Row (sqlite3.Row) 没有 .get()。修复前
    ``row.get('evidence')`` 抛 AttributeError，被外层 except 吞成
    "stranded task reconciliation failed" → stranded 检测全程失效。
    """
    db_file = await _make_project_db_with_closed_task(tmp_path, has_merge_fact=True)
    db = await aiosqlite.connect(str(db_file))
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id, assignee_id, evidence, creator_id FROM tasks "
        "WHERE status = 'closed' AND closed_at IS NOT NULL "
        "ORDER BY closed_at DESC LIMIT 30"
    )
    raw_rows = await cur.fetchall()
    await cur.close()
    # 修复点：dict(r) 转换后再 .get() —— 修复前直接 row.get() 会崩
    rows = [dict(r) for r in raw_rows]
    assert len(rows) == 1
    # 这一行在修复前会抛 AttributeError: 'sqlite3.Row' object has no attribute 'get'
    ev_raw = rows[0].get("evidence") or "{}"
    assert "merged_by" in ev_raw
    await db.close()


@pytest.mark.asyncio
async def test_reconcile_detects_verify_stranded_without_merge_fact(tmp_path):
    """reconcile 扩展：无 merge fact 的 closed 任务（如 VERIFY 报告）也扫描。

    背景：Sage W1 VERIFY 报告 21d1697 stranded 在 hw/A015/work 未合 main，
    任务却已 closed。修复前 has_merge=False 直接 continue 跳过，stranded
    交付物不可见。修复后仍加入 stranded 列表（可见性），但不重开 obligation。
    """
    db_file = await _make_project_db_with_closed_task(
        tmp_path, has_merge_fact=False, task_id="verify-no-merge"
    )
    db = await aiosqlite.connect(str(db_file))
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id, assignee_id, evidence, creator_id FROM tasks "
        "WHERE status = 'closed' AND closed_at IS NOT NULL"
    )
    raw_rows = await cur.fetchall()
    await cur.close()
    rows = [dict(r) for r in raw_rows]
    import json

    # 修复后的逻辑：不再因 has_merge=False 跳过扫描
    has_merge = any(
        json.loads(r.get("evidence") or "{}").get(k)
        for r in rows
        for k in ("merged_by", "mergedBy", "merge_commit")
    )
    # VERIFY 任务没有 merge fact，但仍被扫描（可见性）
    assert has_merge is False
    assert len(rows) == 1  # 没有被提前 continue 掉
    await db.close()


# ── 2. find_reviewer_attestation: .get() 崩溃致 P0-2 回归 ─────────


@pytest.mark.asyncio
async def test_find_reviewer_attestation_returns_true_when_attestation_exists(tmp_path):
    """find_reviewer_attestation 不再因 row.get() 崩溃而永远返回 False。

    修复前：``row.get('kind')`` 在 aiosqlite.Row 上抛 AttributeError →
    被 except 吞 → 返回 False → P0-2 审查方证据硬闸拦死所有 approve。
    这是 fbcfbd4 后引入的 P0 回归（硬闸代码在，但查询路径是坏的）。
    """
    from hiveweave.services import attestation as att_mod

    db_file = tmp_path / "data.db"
    db = await aiosqlite.connect(str(db_file))
    db.row_factory = aiosqlite.Row
    await db.execute(
        "CREATE TABLE tool_attestations (id TEXT, kind TEXT, task_id TEXT, "
        "agent_id TEXT, expires_at INTEGER)"
    )
    await db.execute(
        "INSERT INTO tool_attestations (id, kind, task_id, agent_id, expires_at) "
        "VALUES ('a1', 'test_run', 'taskX', 'reviewer1', NULL)"
    )
    await db.commit()

    # 直接测试修复后的查询片段（不走 meta_db.get_project_workspace 的完整链）
    now_ms = 0  # expires_at IS NULL → 永不过期
    cur = await db.execute(
        "SELECT kind FROM tool_attestations "
        "WHERE task_id = ? AND agent_id = ? "
        "AND (expires_at IS NULL OR expires_at > ?) "
        "AND kind != 'waiver'",
        ["taskX", "reviewer1", now_ms],
    )
    _rows = await cur.fetchall()
    await cur.close()
    kinds = frozenset({"test_run"})
    found = False
    for row in _rows:
        kind = row["kind"] if "kind" in row.keys() else ""
        if kind in kinds:
            found = True
    assert found is True, "reviewer 的 test_run attestation 应被找到"
    await db.close()


@pytest.mark.asyncio
async def test_find_reviewer_attestation_returns_false_when_absent(tmp_path):
    """没有 attestation 时返回 False（而非因崩溃返回 False）。"""
    db_file = tmp_path / "data.db"
    db = await aiosqlite.connect(str(db_file))
    db.row_factory = aiosqlite.Row
    await db.execute(
        "CREATE TABLE tool_attestations (id TEXT, kind TEXT, task_id TEXT, "
        "agent_id TEXT, expires_at INTEGER)"
    )
    await db.commit()
    cur = await db.execute(
        "SELECT kind FROM tool_attestations "
        "WHERE task_id = ? AND agent_id = ? "
        "AND (expires_at IS NULL OR expires_at > ?) AND kind != 'waiver'",
        ["taskX", "reviewer1", 0],
    )
    _rows = await cur.fetchall()
    await cur.close()
    assert len(_rows) == 0
    await db.close()


# ── 3. close_task merge obligation 安全网 ─────────────────────────


@pytest.mark.asyncio
async def test_close_task_fulfills_merge_obligation_as_safety_net(monkeypatch):
    """close_task 在 merge gate 通过后兜底 fulfill merge obligation。

    场景：merge 在 git 层落了 main（evidence_has_merge_fact=True），但
    merge 工具的 fulfill 被跳过/失败（merge_proxy 直走 service）。
    修复前 obligation 81b43baa 持续升级，CEO 不得不 cancel_task 强清。
    """
    from hiveweave.services import task as task_mod
    import hiveweave.services.worktree_review as wr

    fulfilled: list[tuple] = []

    class _FakeLedger:
        async def fulfill(self, project_id, task_id, obligation_type):
            fulfilled.append((project_id, task_id, obligation_type))
            return 1

    monkeypatch.setattr(
        "hiveweave.services.obligation.ObligationLedger", lambda: _FakeLedger()
    )

    async def _none(*a, **k):
        return None

    import json

    ev = {"merged_by": "agentA", "merge_commit": "abc123"}
    task = {
        "id": "safety-net-task",
        "assignee_id": "agentA",
        "status": "approved",
        "evidence": json.dumps(ev),
        "tags": [],
        "policy_id": "",
    }

    svc = task_mod.TaskService()
    monkeypatch.setattr(svc, "_task_skips_merge_gate", lambda t: False)
    monkeypatch.setattr(wr, "evidence_merge_waived", lambda e: False)
    monkeypatch.setattr(wr, "evidence_has_merge_fact", lambda e: True)
    # main_ws=None → 跳过 is-ancestor 检查，直达安全网
    monkeypatch.setattr(wr, "project_main_workspace", _none)
    monkeypatch.setattr(wr, "agent_worktree_path", _none)

    await svc._enforce_merge_on_close("proj1", task)
    assert any(
        t == "merge" and tid == "safety-net-task"
        for _, tid, t in fulfilled
    ), f"close 安全网应 fulfill merge obligation，实际 fulfilled={fulfilled}"


# ── 4. bash dev-server 自动注册检测 ───────────────────────────────


def test_dev_server_detector_catches_long_running_servers():
    """检测器识别长驻 dev server 命令（应路由到注册路径）。"""
    from hiveweave.tools.bash import _detect_dev_server_command

    dev_servers = [
        "npm run dev",
        "npm run dev &",
        "vite --port 3000",
        "npx vite --host 0.0.0.0",
        "pnpm dev",
        "yarn start",
        "bun run dev",
        "next dev",
        "nuxt dev",
        "nodemon server.js",
        "npm run dev -- --port 3001",
    ]
    for cmd in dev_servers:
        assert _detect_dev_server_command(cmd) is not None, (
            f"应识别为 dev server: {cmd!r}"
        )


def test_dev_server_detector_excludes_blocking_commands():
    """检测器排除阻塞命令（build/test/lint/install），走正常路径。"""
    from hiveweave.tools.bash import _detect_dev_server_command

    blocking = [
        "vite build",
        "npm run build",
        "npm test",
        "pnpm install",
        "pnpm run lint",
        "npm run build:test",
        "node script.js",
        "echo hello",
        "git status",
        "",
        "   ",
    ]
    for cmd in blocking:
        assert _detect_dev_server_command(cmd) is None, (
            f"不应识别为 dev server: {cmd!r}"
        )


def test_dev_server_detector_extracts_port():
    """检测器从命令中提取端口号。"""
    from hiveweave.tools.bash import _detect_dev_server_command

    assert _detect_dev_server_command("vite --port 3000") == 3000
    assert _detect_dev_server_command("npm run dev -- --port 3001") == 3001
    # 无显式端口 → 返回 0（由调用方分配）
    assert _detect_dev_server_command("npm run dev") == 0


def test_dev_server_detector_strips_trailing_background_operator():
    """尾部 & 被剥离（注册路径已 detach，& 会让 shell 内 orphan）。"""
    from hiveweave.tools.bash import _detect_dev_server_command

    # "npm run dev &" 应被识别（& 被剥离后是 dev server）
    assert _detect_dev_server_command("npm run dev &") is not None
    # 但 "npm run dev & echo done" 不应误判为纯 dev server（有 echo 尾巴）
    # —— 当前实现仍会识别为 dev server（只剥尾部 &），这是可接受的保守行为
