"""I9 (批8) 回归 — question 无人值守门铃（同生 wait + 双触发 + 事实位 + 诚实语义）。

病灶（s3-clone_13 实测）：CEO 09:33:02 发 3 选 1 提问，09:34:57 项目下班
（115 秒）——``questions`` 无 expires_at、不落 ``agent_waits``、超时返
``success=True``（fake success），这道题跨两次下班仍是 ``status='pending'``。

修复 = 门铃两套并一套 + 双触发：
  ① question 落库同时落一条 ``agent_waits(kind='user')``（ref=question_id）；
  ② 触发 A（时间）：≥QUESTION_UNATTENDED_TIMEOUT_S（600s，锚 = 上游 DSH
     tool-jobs「硬顶」档）无人应答 ⇒ 按 options[0]（推荐项）裁决；
  ③ 触发 B（生命周期）：下班/停止 ⇒ 立即按默认项裁决并写进交接摘要
     （接线批 7 的 stop_project_cleanly 收尾链）；
  ④ 事实位 timed_out_at / resolved_by='timeout'|'lifecycle_stop' 落库；
  ⑤ 轮内超时/被掐改**如实失败**（question 保持 pending，answer 仍可写）；
  ⑥ 同生共死：question 收口 ⇒ 同生 agent_waits 一并解除。

验收判据（fixplan §四 I9，可机检）：
  ① pending 超阈值出现可见告警且 answer 可写；
  ② 停止时 pending 立即按默认项裁决并写进交接摘要；
  ③ 两事实位落库；
  ④ agent_waits(kind='user') 与 question 同生共死。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.db.schema import PROJECT_DB_COLUMN_CHECKS
from hiveweave.tools import question as qmod

PROJECT_ID = "i9-proj-0001"
AGENT_A = "i9-ceo-0001"

_OPTIONS = [{"label": "A 方案（推荐）", "risk": "低"}, {"label": "B 方案"}]


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    """meta DB 钉到本用例临时路径（照 test_i8_run_lifecycle_closeout 模式）。"""
    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()
    yield
    await meta_db.close_meta_db()


@pytest.fixture
async def seeded_project(tmp_path):
    """Meta projects 行 + per-project DB（1 个 active agent）+ 内存路由。"""
    ws = str(tmp_path / "ws")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "i9-test", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    from hiveweave.services.agent_router import AgentRoute, agent_router

    agent_router.reset_for_tests()
    cur = await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "model_id, created_at) VALUES (?, ?, ?, ?, ?, 'active', 'gpt-x', ?)",
        [AGENT_A, "I9A", PROJECT_ID, "Agent-I9A", "ceo", now],
    )
    await cur.close()
    await conn.commit()
    agent_router.register(
        AgentRoute(
            agent_id=AGENT_A,
            project_id=PROJECT_ID,
            workspace_path=ws,
            short_id="I9A",
            name="Agent-I9A",
            role="ceo",
            status="active",
        )
    )
    yield conn
    agent_router.reset_for_tests()


async def _seed_question(
    conn,
    qid: str,
    *,
    status: str = "pending",
    options: list | None = None,
    expires_at: int | None = None,
    agent_id: str = AGENT_A,
) -> None:
    now = int(time.time() * 1000)
    cur = await conn.execute(
        "INSERT INTO questions (id, agent_id, project_id, question, options, "
        "status, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            qid,
            agent_id,
            PROJECT_ID,
            "shell 全项目已死，Halyard 怎么走？",
            json.dumps(options, ensure_ascii=False) if options else None,
            status,
            now,
            expires_at,
        ],
    )
    await cur.close()
    await conn.commit()


async def _question_row(conn, qid: str) -> dict:
    cur = await conn.execute("SELECT * FROM questions WHERE id = ?", [qid])
    row = await cur.fetchone()
    await cur.close()
    assert row is not None, f"question {qid} not found"
    return dict(row)


async def _wait_rows(conn, qid: str) -> list[dict]:
    cur = await conn.execute(
        "SELECT * FROM agent_waits WHERE ref = ? AND kind = 'user'", [qid]
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _poll(assert_fn, *, timeout: float = 6.0, interval: float = 0.05):
    """轮询直到 assert_fn() 不抛（提问路径含多个 aiosqlite 往返 + 徽章
    查询，固定 sleep 会与后台任务竞态 —— 必须按条件等）。支持同步/异步断言。"""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = assert_fn()
            if inspect.iscoroutine(result):
                result = await result
            return result
        except AssertionError as exc:  # noqa: PERF203
            last_exc = exc
            await asyncio.sleep(interval)
    raise AssertionError(f"poll timeout: {last_exc}")


# ── ② 阈值自检（上游锚点：默认 ≤ 硬顶，DSH tool-jobs :202 照抄）────────


def test_thresholds_self_check_rejects_default_over_cap():
    from hiveweave.tools.question import validate_question_thresholds

    validate_question_thresholds(600, 3600)  # 合法：不抛
    with pytest.raises(ValueError):
        validate_question_thresholds(7200, 3600)  # 默认 > 硬顶 ⇒ 抛
    with pytest.raises(ValueError):
        validate_question_thresholds(0, 3600)
    with pytest.raises(ValueError):
        validate_question_thresholds(600, 0)


def test_threshold_defaults_come_from_config_settings():
    assert qmod._unattended_timeout_s() == 600  # DSH 硬顶档，非 30s 默认
    assert qmod._wait_cap_s() == 3600  # 平台既有 user-wait 档


# ── ③ schema：门铃三列 + COLUMN_CHECKS fail-loud 登记 ─────────────────


async def test_schema_registers_question_doorbell_columns(tmp_path):
    conn = await project_db.ensure_project_db(str(tmp_path / "ws-fresh"))
    cur = await conn.execute("PRAGMA table_info(questions)")
    cols = {row[1] for row in await cur.fetchall()}
    await cur.close()
    assert {"expires_at", "timed_out_at", "resolved_by"} <= cols
    assert {"expires_at", "timed_out_at", "resolved_by"} <= (
        PROJECT_DB_COLUMN_CHECKS["questions"]
    )


# ── ①④ 提问落库：expires_at + 同生 kind='user' wait（同 expires_at）────


@pytest.mark.asyncio
async def test_ask_records_expires_at_and_user_wait_same_expiry(
    seeded_project, monkeypatch
):
    monkeypatch.setattr(qmod, "QUESTION_TIMEOUT_S", 5.0)
    task = asyncio.create_task(
        qmod.execute_question(
            AGENT_A, "shell 全项目已死，Halyard 怎么走？", options=_OPTIONS
        )
    )

    def _live_questions():
        live = [
            q
            for q, (pid, _aid) in qmod._question_projects.items()
            if pid == PROJECT_ID
        ]
        assert live, "提问后必须有未收口 question 落点账"
        return live

    live = await _poll(_live_questions)
    qid = live[0]

    async def _born():
        qrow = await _question_row(seeded_project, qid)
        assert qrow["status"] == "pending"
        assert qrow["expires_at"] is not None
        assert qrow["expires_at"] >= qrow["created_at"] + 599_000  # ≈ created+600s
        waits = await _wait_rows(seeded_project, qid)
        assert len(waits) == 1, "I9 ①：落库必须同生一条 agent_waits(kind='user')"
        return qrow, waits

    qrow, waits = await _poll(_born)
    wait = waits[0]
    assert wait["agent_id"] == AGENT_A
    assert wait["phase"] == "question"
    assert wait["cleared_at"] is None
    assert wait["expires_at"] == qrow["expires_at"], "同生 wait 与 question 同到期"

    # 答案先到 ⇒ 同生共死（验收 ④）
    assert qmod.resolve_question(qid, "B 方案") is True
    result = await asyncio.wait_for(task, timeout=5)
    assert result["success"] is True
    qrow = await _question_row(seeded_project, qid)
    assert qrow["status"] == "answered"

    async def _wait_cleared():
        waits = await _wait_rows(seeded_project, qid)
        assert waits and all(w["cleared_at"] is not None for w in waits)

    await _poll(_wait_cleared)


# ── ①③ 触发 A：无人值守 ⇒ 按推荐项裁决 + 可见告警 + 事实位 ────────────


@pytest.mark.asyncio
async def test_unattended_timeout_adjudicates_recommended_option(
    seeded_project, monkeypatch
):
    # 轮内钟 0.05s（如实失败返回）；无人值守钟 1s（触发 A 裁决）
    monkeypatch.setattr(qmod, "QUESTION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(qmod.settings, "question_unattended_timeout_s", 1)

    from hiveweave.realtime.event_bus import status_event_bus

    events: list[tuple[str, dict]] = []

    async def fake_publish(channel, event):
        events.append((channel, event))

    monkeypatch.setattr(status_event_bus, "publish", fake_publish)

    async def fake_trigger(agent_id):
        return None

    monkeypatch.setattr(
        "hiveweave.agents.trigger.trigger_subordinate", fake_trigger
    )

    result = await qmod.execute_question(
        AGENT_A, "shell 全项目已死，Halyard 怎么走？", options=_OPTIONS
    )
    # execute_question 直接 await ⇒ 落点账已落（仅未收口者入账）
    qid = next(
        q
        for q, (pid, _a) in qmod._question_projects.items()
        if pid == PROJECT_ID
    )
    # fake success 已死：轮内超时必须如实失败，question 保持 pending
    assert result["success"] is False
    assert "STAYS PENDING" in (result.get("error") or "")
    qrow = await _question_row(seeded_project, qid)
    assert qrow["status"] == "pending", "轮内超时不得把 question 标成终态"

    # 触发 A 到点：按 options[0]（推荐项）裁决
    async def _wait_adjudicated():
        row = await _question_row(seeded_project, qid)
        assert row["status"] == "timed_out", (
            f"触发 A 未到点（status={row['status']}）"
        )
        return row

    qrow = await _poll(_wait_adjudicated, timeout=10)
    assert qrow["resolved_by"] == "timeout"
    assert qrow["timed_out_at"] is not None
    assert qrow["answer"] == "A 方案（推荐）", "必须按 options[0] 推荐项裁决"

    # 同生 wait 解除（验收 ④）—— 裁决 UPDATE 与 wait 解除之间有窗口，按条件等
    async def _sibling_cleared():
        waits = await _wait_rows(seeded_project, qid)
        assert waits and all(w["cleared_at"] is not None for w in waits)

    await _poll(_sibling_cleared)

    # 可见告警：chat 留痕 + 实时广播（不许静默）
    # ⚠ 留痕在裁决 UPDATE **之后**异步完成（best-effort），poll 检测到
    # status 翻转即返回时留痕可能尚未落库 ⇒ 按条件等（修 flake：原版
    # 立即查询偶发抢在写入前，实测 1/3 失败率）。
    async def _chat_trace_visible():
        cur = await seeded_project.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE metadata LIKE ?",
            ['%"question_adjudicated"%'],
        )
        assert (await cur.fetchone())[0] >= 1

    await _poll(_chat_trace_visible)
    # 广播事件与 inbox 同理：都在裁决 UPDATE 之后的可见出口序列里，按条件等
    async def _events_and_inbox_visible():
        adjudicated_events = [
            e for _ch, e in events if e.get("type") == "question_adjudicated"
        ]
        assert adjudicated_events, "触发 A 必须广播可见事件"
        assert adjudicated_events[0]["resolvedBy"] == "timeout"
        cur = await seeded_project.execute(
            "SELECT COUNT(*) FROM inbox WHERE to_agent_id = ? AND wake = 1 "
            "AND message LIKE '%[QUESTION AUTO-RESOLVED]%'",
            [AGENT_A],
        )
        assert (await cur.fetchone())[0] >= 1
        return adjudicated_events

    adjudicated_events = await _poll(_events_and_inbox_visible)


@pytest.mark.asyncio
async def test_late_answer_after_in_turn_timeout_clears_doorbell(
    seeded_project, monkeypatch
):
    monkeypatch.setattr(qmod, "QUESTION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        qmod.settings, "question_unattended_timeout_s", 3600
    )  # 本测内不触发 A

    result = await qmod.execute_question(AGENT_A, "要继续吗？", options=_OPTIONS)
    assert result["success"] is False
    qid = next(
        q
        for q, (pid, _a) in qmod._question_projects.items()
        if pid == PROJECT_ID
    )
    # 轮内钟已到、future 已收 → 晚到答案走 orphan 清扫分支
    assert qmod.resolve_question(qid, "晚到答案") is False
    assert qid not in qmod._adjudicators, "晚到答案必须撤销未决裁决钟"

    async def _wait_cleared():
        waits = await _wait_rows(seeded_project, qid)
        assert waits and all(w["cleared_at"] is not None for w in waits)

    await _poll(_wait_cleared)

    # 验收 ① 后半：answer 仍可写（API 端 UPDATE 无 status 过滤）——在
    # pending 行上直接落答案成功
    cur = await seeded_project.execute(
        "UPDATE questions SET answer = ?, status = 'answered', answered_at = ? "
        "WHERE id = ?",
        ["晚到答案", int(time.time() * 1000), qid],
    )
    await seeded_project.commit()
    assert cur.rowcount == 1
    qrow = await _question_row(seeded_project, qid)
    assert qrow["status"] == "answered"
    assert qrow["answer"] == "晚到答案"


# ── ②③ 触发 B：下班/停止 ⇒ 立即按默认项裁决 + 交接摘要 + 事实位 ────────


@pytest.mark.asyncio
async def test_lifecycle_stop_adjudicates_pending_and_cancelled(
    seeded_project,
):
    from hiveweave.services.wait_contract import record_question_user_wait
    from hiveweave.tools.question import adjudicate_project_questions

    await _seed_question(seeded_project, "i9-q-pending", options=_OPTIONS)
    await _seed_question(
        seeded_project, "i9-q-cancelled", status="cancelled", options=_OPTIONS
    )
    await _seed_question(seeded_project, "i9-q-answered", status="answered")
    now = int(time.time() * 1000)
    for qid in ("i9-q-pending", "i9-q-cancelled"):
        await record_question_user_wait(
            PROJECT_ID, AGENT_A, ref=qid, expires_at_ms=now + 600_000
        )

    summary = await adjudicate_project_questions(PROJECT_ID)
    assert {s["questionId"] for s in summary} == {
        "i9-q-pending",
        "i9-q-cancelled",
    }
    for qid in ("i9-q-pending", "i9-q-cancelled"):
        qrow = await _question_row(seeded_project, qid)
        assert qrow["status"] == "timed_out"
        assert qrow["resolved_by"] == "lifecycle_stop"
        assert qrow["timed_out_at"] is not None
        assert qrow["answer"] == "A 方案（推荐）"
        waits = await _wait_rows(seeded_project, qid)
        assert waits and all(w["cleared_at"] is not None for w in waits)
    # 已答的不动
    qrow = await _question_row(seeded_project, "i9-q-answered")
    assert qrow["status"] == "answered"

    # 交接摘要：裁决写进 wake=1 inbox（activate pre-park 并进复工 briefing）
    cur = await seeded_project.execute(
        "SELECT message FROM inbox WHERE to_agent_id = ? AND wake = 1 "
        "AND message_type = 'question_timeout'",
        [AGENT_A],
    )
    msgs = [r[0] for r in await cur.fetchall()]
    await cur.close()
    assert msgs, "触发 B 必须产出交接摘要 inbox（wake=1）"
    assert any("lifecycle_stop" in m for m in msgs)

    # 幂等：再扫一遍无新裁决
    assert await adjudicate_project_questions(PROJECT_ID) == []


@pytest.mark.asyncio
async def test_stop_project_cleanly_hooks_question_adjudication(seeded_project):
    from hiveweave.services.project_lifecycle import stop_project_cleanly

    # 接线点 = 批 7 收尾链（stop_project_cleanly），不另起清扫
    src = inspect.getsource(stop_project_cleanly)
    assert "adjudicate_project_questions" in src

    await _seed_question(seeded_project, "i9-q-stop", options=_OPTIONS)
    result = await stop_project_cleanly(PROJECT_ID)
    assert result.get("questions_adjudicated") == 1
    qrow = await _question_row(seeded_project, "i9-q-stop")
    assert qrow["status"] == "timed_out"
    assert qrow["resolved_by"] == "lifecycle_stop"


# ── 触发 A 补票：恢复时按 expires_at 补裁决（重启丢钟兜底）─────────────


@pytest.mark.asyncio
async def test_resume_catchup_adjudicates_only_expired(seeded_project):
    from hiveweave.tools.question import adjudicate_expired_questions

    now = int(time.time() * 1000)
    await _seed_question(
        seeded_project, "i9-q-old", options=_OPTIONS, expires_at=now - 1_000
    )
    await _seed_question(
        seeded_project, "i9-q-fresh", options=_OPTIONS, expires_at=now + 600_000
    )

    summary = await adjudicate_expired_questions(PROJECT_ID)
    assert [s["questionId"] for s in summary] == ["i9-q-old"]
    qrow = await _question_row(seeded_project, "i9-q-old")
    assert qrow["status"] == "timed_out"
    assert qrow["resolved_by"] == "timeout"
    assert qrow["answer"] == "A 方案（推荐）"
    # 未到期的保持 pending（门铃仍在途）
    qrow = await _question_row(seeded_project, "i9-q-fresh")
    assert qrow["status"] == "pending"
