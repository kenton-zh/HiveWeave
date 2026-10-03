"""批22 · P1-12：user_pings 写方立项 + 无人值守超时裁决的迟到改判回归。

病灶：``user_pings`` 表全仓零 INSERT（读方 GET /api/user-pings 消费一张
永远为空的表）；无人值守 question 21/23 超时自动裁决后，用户**无法改判**
（R6 真缺口）。批8 的 agent_waits(kind='user') 门铃是活的（等待机制）——
本修复补的是**人类侧可见的 ping 台账 + 迟到裁决通道**，不重复门铃。

验收（可机检）：
  ① question 创建 ⇒ user_pings 有行（pending）；
  ② 收口（answered / 超时裁决）⇒ 状态同步；
  ③ timed_out 补选 ⇒ [QUESTION RE-ADJUDICATED] 通知送达 agent 且
     user_pings 置 readjudicated，questions 行 resolved_by='user_readjudicated'；
  ④ answered（非 timeout）仍拒绝重判（409；语义收紧范围只开 timed_out）；
  ⑤ 幂等：同答案重复提交不重投。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from fastapi import HTTPException

from hiveweave.api.chat import QuestionAnswerBody, answer_question
from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.tools import question as qmod

PROJECT_ID = "b22-proj-pings"
AGENT_A = "b22-ping-ceo-1"

_OPTIONS = [{"label": "A 方案（推荐）", "risk": "低"}, {"label": "B 方案"}]
_QTEXT = "shell 全项目已死，Halyard 怎么走？"


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    """meta DB 钉到本用例临时路径（照 test_i9_question_doorbell 模式）。"""
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
        [PROJECT_ID, "b22-pings-test", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    from hiveweave.services.agent_router import AgentRoute, agent_router

    agent_router.reset_for_tests()
    cur = await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "model_id, created_at) VALUES (?, ?, ?, ?, 'ceo', 'active', 'gpt-x', ?)",
        [AGENT_A, "B22P", PROJECT_ID, "Agent-B22P", now],
    )
    await cur.close()
    await conn.commit()
    agent_router.register(
        AgentRoute(
            agent_id=AGENT_A,
            project_id=PROJECT_ID,
            workspace_path=ws,
            short_id="B22P",
            name="Agent-B22P",
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
    answer: str | None = None,
    resolved_by: str | None = None,
) -> None:
    now = int(time.time() * 1000)
    cur = await conn.execute(
        "INSERT INTO questions (id, agent_id, project_id, question, options, "
        "answer, status, created_at, expires_at, timed_out_at, resolved_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            qid,
            AGENT_A,
            PROJECT_ID,
            _QTEXT,
            json.dumps(_OPTIONS, ensure_ascii=False),
            answer,
            status,
            now,
            now + 600_000,
            now if status == "timed_out" else None,
            resolved_by,
        ],
    )
    await cur.close()
    await conn.commit()


async def _seed_ping(conn, qid: str) -> None:
    cur = await conn.execute(
        "INSERT INTO user_pings (id, project_id, from_agent_id, message, "
        "is_read, created_at, question_id, status) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, 'pending')",
        [
            str(uuid.uuid4()),
            PROJECT_ID,
            AGENT_A,
            f"{_QTEXT} 选项: A 方案（推荐） / B 方案",
            int(time.time() * 1000),
            qid,
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


async def _ping_rows(conn, qid: str) -> list[dict]:
    cur = await conn.execute(
        "SELECT * FROM user_pings WHERE question_id = ?", [qid]
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _inbox_messages(conn, agent_id: str, message_type: str) -> list[dict]:
    cur = await conn.execute(
        "SELECT * FROM inbox WHERE to_agent_id = ? AND message_type = ? "
        "ORDER BY created_at",
        [agent_id, message_type],
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _wait_for_pending_question(conn, timeout_s: float = 5.0) -> str:
    """轮询 pending question 行，并等到其 answer Future 已注册（resolve 可用）。

    execute_question 的 INSERT 在建 Future **之前**（中间隔着 ping 台账 /
    wait 登记 / chat 留痕多个 await）—— 只看行会撞上「行在了、future 还没
    在」的窗口，resolve_question 会误判为晚到答案。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        cur = await conn.execute(
            "SELECT id FROM questions WHERE agent_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1",
            [AGENT_A],
        )
        row = await cur.fetchone()
        await cur.close()
        if row:
            qid = str(row["id"])
            if qid in qmod._pending:
                return qid
        await asyncio.sleep(0.02)
    raise AssertionError("pending question 未出现（execute_question 未落库）")


# ── ① question 创建 ⇒ user_pings 有行（pending，含选项摘要）────────


async def test_question_creation_inserts_user_ping(seeded_project):
    import contextlib

    task = asyncio.create_task(
        qmod.execute_question(
            AGENT_A, _QTEXT, options=list(_OPTIONS)
        )
    )
    try:
        qid = await _wait_for_pending_question(seeded_project)
        pings = await _ping_rows(seeded_project, qid)
        assert len(pings) == 1, "question 创建必须写一行 ping 台账"
        ping = pings[0]
        assert ping["status"] == "pending"
        assert ping["from_agent_id"] == AGENT_A
        assert ping["project_id"] == PROJECT_ID
        assert _QTEXT in ping["message"]
        assert "A 方案（推荐）" in ping["message"] and "B 方案" in ping["message"]
        assert not ping["is_read"]
        # 收口（用户答复）→ 台账同步 answered（验收 ② answered 支）
        assert qmod.resolve_question(qid, "B 方案") is True
        result = await task
    finally:
        if not task.done():  # 断言失败时防 180s 挂起（teardown 双保险）
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert result["success"] is True
    ping = (await _ping_rows(seeded_project, qid))[0]
    assert ping["status"] == "answered"


# ── ② 超时裁决 ⇒ 台账 timed_out ─────────────────────────────────


async def test_adjudication_marks_ping_timed_out(seeded_project):
    qid = "b22-ping-adj-1"
    await _seed_question(seeded_project, qid, status="pending")
    await _seed_ping(seeded_project, qid)
    result = await qmod.adjudicate_question(
        qid,
        PROJECT_ID,
        reason=qmod.RESOLVED_BY_TIMEOUT,
        send_inbox=False,
        trigger_agent=False,
    )
    assert result is not None
    qrow = await _question_row(seeded_project, qid)
    assert qrow["status"] == "timed_out"
    assert qrow["resolved_by"] == "timeout"
    ping = (await _ping_rows(seeded_project, qid))[0]
    assert ping["status"] == "timed_out"


# ── ③ timed_out 补选 ⇒ 重判通知送达 + 台账 readjudicated ─────────


async def test_timed_out_readjudication_delivers_notice_and_updates_ledger(
    seeded_project,
):
    qid = "b22-ping-readj-1"
    await _seed_question(
        seeded_project, qid, status="timed_out",
        answer="A 方案（推荐）", resolved_by="timeout",
    )
    await _seed_ping(seeded_project, qid)

    resp = await answer_question(
        qid, QuestionAnswerBody(answer="B 方案", agentId=AGENT_A)
    )
    assert resp["ok"] is True and resp["readjudicated"] is True

    # questions 行：用户选择生效 + 改判事实位
    qrow = await _question_row(seeded_project, qid)
    assert qrow["status"] == "answered"
    assert qrow["answer"] == "B 方案"
    assert qrow["resolved_by"] == "user_readjudicated"
    # 台账：readjudicated
    ping = (await _ping_rows(seeded_project, qid))[0]
    assert ping["status"] == "readjudicated"
    # agent 收到 [QUESTION RE-ADJUDICATED] 平台通知（wake=1）
    msgs = await _inbox_messages(seeded_project, AGENT_A, "question_readjudicated")
    assert len(msgs) == 1
    assert "[QUESTION RE-ADJUDICATED]" in msgs[0]["message"]
    assert "B 方案" in msgs[0]["message"]


# ── ⑤ 幂等：同答案重复提交不重投 ────────────────────────────────


async def test_readjudication_idempotent_on_same_answer(seeded_project):
    qid = "b22-ping-readj-2"
    await _seed_question(
        seeded_project, qid, status="timed_out",
        answer="A 方案（推荐）", resolved_by="timeout",
    )
    await _seed_ping(seeded_project, qid)

    # 首次迟到裁决但答案与原自动裁决相同 ⇒ 落账不投递
    resp1 = await answer_question(
        qid, QuestionAnswerBody(answer="A 方案（推荐）", agentId=AGENT_A)
    )
    assert resp1["ok"] is True and resp1["readjudicated"] is True
    assert (
        await _inbox_messages(seeded_project, AGENT_A, "question_readjudicated")
        == []
    )
    ping = (await _ping_rows(seeded_project, qid))[0]
    assert ping["status"] == "readjudicated"

    # 重复提交同答案 ⇒ 幂等 no-op（行已 answered + 同答案）
    resp2 = await answer_question(
        qid, QuestionAnswerBody(answer="A 方案（推荐）", agentId=AGENT_A)
    )
    assert resp2.get("idempotent") is True and resp2["readjudicated"] is False
    assert (
        await _inbox_messages(seeded_project, AGENT_A, "question_readjudicated")
        == []
    )
    # 改判后重复同答案同样幂等（不重投）
    qid2 = "b22-ping-readj-3"
    await _seed_question(
        seeded_project, qid2, status="timed_out",
        answer="A 方案（推荐）", resolved_by="timeout",
    )
    await _seed_ping(seeded_project, qid2)
    await answer_question(
        qid2, QuestionAnswerBody(answer="B 方案", agentId=AGENT_A)
    )
    assert len(
        await _inbox_messages(seeded_project, AGENT_A, "question_readjudicated")
    ) == 1
    resp3 = await answer_question(
        qid2, QuestionAnswerBody(answer="B 方案", agentId=AGENT_A)
    )
    assert resp3.get("idempotent") is True
    assert len(
        await _inbox_messages(seeded_project, AGENT_A, "question_readjudicated")
    ) == 1, "重复提交同答案不得重投通知"


# ── ④ answered（非 timeout）仍拒绝重判 ──────────────────────────


async def test_answered_question_refuses_new_answer_but_idempotent_on_same(
    seeded_project,
):
    qid = "b22-ping-answered-1"
    await _seed_question(
        seeded_project, qid, status="answered", answer="X 方案"
    )
    await _seed_ping(seeded_project, qid)

    # 不同答案 → 409（重判语义只开 timed_out）
    with pytest.raises(HTTPException) as ei:
        await answer_question(
            qid, QuestionAnswerBody(answer="Y 方案", agentId=AGENT_A)
        )
    assert ei.value.status_code == 409
    qrow = await _question_row(seeded_project, qid)
    assert qrow["answer"] == "X 方案", "拒绝改判时不得改写已收口问题"
    assert qrow["resolved_by"] is None

    # 同答案重复提交 → 幂等放行、不投递
    resp = await answer_question(
        qid, QuestionAnswerBody(answer="X 方案", agentId=AGENT_A)
    )
    assert resp.get("idempotent") is True
    assert (
        await _inbox_messages(seeded_project, AGENT_A, "question_readjudicated")
        == []
    )


# ── 补：晚到答案（future 已不在）也同步台账 answered ────────────


async def test_late_answer_via_endpoint_updates_ping_to_answered(seeded_project):
    qid = "b22-ping-late-1"
    await _seed_question(seeded_project, qid, status="pending")
    await _seed_ping(seeded_project, qid)
    resp = await answer_question(
        qid, QuestionAnswerBody(answer="A 方案（推荐）", agentId=AGENT_A)
    )
    assert resp["ok"] is True and resp["readjudicated"] is False
    qrow = await _question_row(seeded_project, qid)
    assert qrow["status"] == "answered"
    ping = (await _ping_rows(seeded_project, qid))[0]
    assert ping["status"] == "answered"
