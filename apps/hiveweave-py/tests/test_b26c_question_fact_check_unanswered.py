"""批26 修复2：QUESTION 事实确认题超时**不代答**（假裁决根治）。

病灶（第五轮两次假裁决）：无人值守超时自动裁决在「事实确认型问题」上
替用户断言假事实（「已重启，继续」—— backend 根本没重启，agent 实测
戳穿）。

修法：``adjudicate_question``（触发 A/B/恢复补票唯一出口）对事实确认
形态（问题文本以「是否/有没有/已经/已重启/已完成」开头、含「是否已」，
或选项是「是/否/已/未」二元确认形态）**不代答**：落既有终态 timed_out
但 resolved_by='unanswered_fact_check'、answer=NULL；提问者与 wait_on
等待者收 ``[QUESTION UNANSWERED]``（wake=1），等待按超时正常释放。
非事实型问题照旧默认裁决。

验收（可机检）：
  ① 「是否已重启 backend」超时 ⇒ 不产出「已重启」裁决，answer 为 NULL，
     agent 收到 UNANSWERED 通知（不是 AUTO-RESOLVED）；
  ② 普通选择题超时 ⇒ 默认裁决照旧（answer=推荐项、resolved_by=timeout）；
  ③ 无人值守流不因新形态卡死：终态行再裁决返回 None（扫描面不再拾起），
     wait_on 等待者同轮释放。
"""

from __future__ import annotations

import json
import time

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.tools import question as qmod

PROJECT_ID = "b26c-factcheck-proj"
AGENT_A = "b26c-factcheck-ceo-1"
AGENT_B = "b26c-factcheck-waiter-2"

FACT_QTEXT = "是否已重启 backend？请确认后我再继续。"
FACT_OPTIONS = [
    {"label": "是，已重启，继续"},
    {"label": "否，还没重启"},
]
NORMAL_QTEXT = "shell 全项目已死，Halyard 怎么走？"
NORMAL_OPTIONS = [
    {"label": "A 方案（推荐）", "risk": "低"},
    {"label": "B 方案"},
]


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    """meta DB 钉到本用例临时路径（照 test_batch22 模式）。"""
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
    """Meta projects 行 + per-project DB（提问者 + 等待者）+ 内存路由。"""
    ws = str(tmp_path / "ws")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "b26c-factcheck", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    from hiveweave.services.agent_router import AgentRoute, agent_router

    agent_router.reset_for_tests()
    for aid, sid in ((AGENT_A, "B26A"), (AGENT_B, "B26B")):
        cur = await conn.execute(
            "INSERT INTO agents (id, short_id, project_id, name, role, "
            "status, model_id, created_at) "
            "VALUES (?, ?, ?, ?, 'ceo', 'active', 'gpt-x', ?)",
            [aid, sid, PROJECT_ID, f"Agent-{sid}", now],
        )
        await cur.close()
        agent_router.register(
            AgentRoute(
                agent_id=aid,
                project_id=PROJECT_ID,
                workspace_path=ws,
                short_id=sid,
                name=f"Agent-{sid}",
                role="ceo",
                status="active",
            )
        )
    await conn.commit()
    yield conn
    agent_router.reset_for_tests()


async def _seed_question(
    conn,
    qid: str,
    *,
    question: str,
    options: list | None,
    status: str = "pending",
) -> None:
    now = int(time.time() * 1000)
    cur = await conn.execute(
        "INSERT INTO questions (id, agent_id, project_id, question, options, "
        "status, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            qid,
            AGENT_A,
            PROJECT_ID,
            question,
            json.dumps(options, ensure_ascii=False) if options else None,
            status,
            now,
            now + 600_000,
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


async def _inbox_messages(conn, agent_id: str) -> list[dict]:
    cur = await conn.execute(
        "SELECT * FROM inbox WHERE to_agent_id = ? ORDER BY created_at",
        [agent_id],
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


def _adjudicate_kwargs(**overrides):
    kw = {
        "send_inbox": True,
        "trigger_agent": False,
    }
    kw.update(overrides)
    return kw


# ── 判据单元测试 ─────────────────────────────────────────────────────


def test_is_fact_check_question_leads():
    assert qmod.is_fact_check_question("是否已重启 backend？")
    assert qmod.is_fact_check_question("有没有收到我的提交？")
    assert qmod.is_fact_check_question("已经完成了可以合并吗")
    assert qmod.is_fact_check_question("已重启了吗")
    assert qmod.is_fact_check_question("已完成的部分先合并？")
    # 「是否已」子串（不在开头）
    assert qmod.is_fact_check_question("服务 A 是否已经重启")


def test_is_fact_check_question_binary_options():
    # 选项精确二元确认形态 ⇒ 命中（问题文本不命中也一样算）
    assert qmod.is_fact_check_question("backend 处理完了吗", ["是", "否"])
    assert qmod.is_fact_check_question(
        "继续吗", [{"label": "已"}, {"label": "未"}]
    )


def test_is_fact_check_question_negatives():
    # 普通选择题不误伤
    assert not qmod.is_fact_check_question(NORMAL_QTEXT, NORMAL_OPTIONS)
    assert not qmod.is_fact_check_question("要不要重试", ["是，立即重试", "否，稍后"])
    assert not qmod.is_fact_check_question("选哪个方案", ["A", "B"])
    assert not qmod.is_fact_check_question("", None)


# ── ① 事实确认题超时 ⇒ 不代答 + UNANSWERED 通知 ─────────────────────


async def test_fact_check_timeout_does_not_answer(seeded_project):
    qid = "b26c-factcheck-1"
    await _seed_question(
        seeded_project, qid, question=FACT_QTEXT, options=FACT_OPTIONS
    )
    result = await qmod.adjudicate_question(
        qid, PROJECT_ID, reason=qmod.RESOLVED_BY_TIMEOUT, **_adjudicate_kwargs()
    )
    assert result is not None
    assert result["resolved_by"] == qmod.RESOLVED_BY_UNANSWERED_FACT_CHECK
    assert result["chosen"] is None

    row = await _question_row(seeded_project, qid)
    assert row["status"] == "timed_out"
    assert row["resolved_by"] == qmod.RESOLVED_BY_UNANSWERED_FACT_CHECK
    # 不代答：answer 必须为 NULL —— 不得产出「已重启，继续」类假事实
    # （question 列本来就是问题文本，只查 answer/裁决面）
    assert row["answer"] is None

    inbox = await _inbox_messages(seeded_project, AGENT_A)
    assert inbox, "提问者必须收到 wake=1 通知"
    bodies = " ".join(m["message"] for m in inbox)
    assert "[QUESTION UNANSWERED]" in bodies
    assert "平台不代答" in bodies
    assert "勿将本问题视为已确认" in bodies
    # 不得出现默认裁决话术
    assert "已按推荐项裁决" not in bodies
    assert "[QUESTION AUTO-RESOLVED]" not in bodies


async def test_fact_check_timeout_releases_waiters(seeded_project):
    """wait_on(kind=user, ref=question) 的等待者同轮收到 UNANSWERED 并释放。"""
    qid = "b26c-factcheck-2"
    await _seed_question(
        seeded_project, qid, question=FACT_QTEXT, options=FACT_OPTIONS
    )
    from hiveweave.services.wait_contract import (
        record_question_user_wait,
        wait_contract_service,
    )

    await record_question_user_wait(
        PROJECT_ID,
        AGENT_B,
        ref=qid,
        expires_at_ms=int(time.time() * 1000) + 600_000,
        note="waiting on fact answer",
    )
    active_before = await wait_contract_service.list_all_active(PROJECT_ID)
    assert any(
        str(w.get("ref") or "") == qid for w in active_before
    ), "前置：等待行必须已挂上"

    await qmod.adjudicate_question(
        qid, PROJECT_ID, reason=qmod.RESOLVED_BY_TIMEOUT, **_adjudicate_kwargs()
    )
    active_after = await wait_contract_service.list_all_active(PROJECT_ID)
    assert not any(
        str(w.get("ref") or "") == qid for w in active_after
    ), "相关等待必须已按超时释放（不卡死）"
    waiter_inbox = await _inbox_messages(seeded_project, AGENT_B)
    assert any("[QUESTION UNANSWERED]" in str(m["message"]) for m in waiter_inbox)


# ── ② 普通选择题超时 ⇒ 默认裁决照旧 ─────────────────────────────────


async def test_normal_question_timeout_still_defaults(seeded_project):
    qid = "b26c-normal-1"
    await _seed_question(
        seeded_project, qid, question=NORMAL_QTEXT, options=NORMAL_OPTIONS
    )
    result = await qmod.adjudicate_question(
        qid, PROJECT_ID, reason=qmod.RESOLVED_BY_TIMEOUT, **_adjudicate_kwargs()
    )
    assert result is not None
    assert result["resolved_by"] == qmod.RESOLVED_BY_TIMEOUT
    assert result["chosen"] == "A 方案（推荐）"

    row = await _question_row(seeded_project, qid)
    assert row["status"] == "timed_out"
    assert row["resolved_by"] == qmod.RESOLVED_BY_TIMEOUT
    assert row["answer"] == "A 方案（推荐）"

    inbox = await _inbox_messages(seeded_project, AGENT_A)
    bodies = " ".join(str(m["message"]) for m in inbox)
    assert "[QUESTION AUTO-RESOLVED]" in bodies
    assert "[QUESTION UNANSWERED]" not in bodies


# ── ③ 无人值守流不因新终态卡死 ───────────────────────────────────────


async def test_fact_check_terminal_state_not_repicked(seeded_project):
    """终态行再裁决返回 None（触发 A 恢复补票 / 触发 B 扫描不再拾起）。"""
    qid = "b26c-factcheck-3"
    await _seed_question(
        seeded_project, qid, question=FACT_QTEXT, options=FACT_OPTIONS
    )
    first = await qmod.adjudicate_question(
        qid, PROJECT_ID, reason=qmod.RESOLVED_BY_TIMEOUT,
        send_inbox=False, trigger_agent=False,
    )
    assert first is not None
    # 触发 A 补票（expires_at 已过的 pending 扫描）：事实题已终态 ⇒ 不重复
    assert await qmod.adjudicate_expired_questions(PROJECT_ID) == []
    # 触发 B（下班收尾）：同样不重复裁决
    assert await qmod.adjudicate_project_questions(PROJECT_ID) == []
    again = await qmod.adjudicate_question(
        qid, PROJECT_ID, reason=qmod.RESOLVED_BY_LIFECYCLE_STOP,
        send_inbox=False, trigger_agent=False,
    )
    assert again is None
