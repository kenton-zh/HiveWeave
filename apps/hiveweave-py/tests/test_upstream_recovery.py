"""TEST_DSH_63 批3 组4：上游死亡编排侧恢复。

面（DSH 参照哲学：死亡可接受、恢复靠 durable 触发、任务保持 claimed）：
1. 上游死亡 → durable 重醒已排（复用 agent_waits kind=timer + phase=
   upstream_recovery；60s/180s/600s 退避封顶 3 次，次数持久化）；
2. 非上游死亡 → 不排；
3. 通知去重窗：300s 内 2 死 → 给 CEO 一条 ORG_ESCALATION 汇总；窗内只发
   一条；重醒耗尽升级独立于此窗（每 agent 30min 一条）；
4. R11 占位行：declared>0 零账死亡 run → 全 0 占位 usage 行（可机检标注）；
5. dwell/义务时钟暂停：重醒等待未到期 → has_pending_upstream_recovery
   True（game_time live_wait_agents 据此跳过 stall/改派/自动提交），
   唤醒触发（clear_expired 清行）后自然恢复。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

PROJECT_ID = "ups-proj-0001"
AGENT_ID = "ups-exec-0001"
CEO_ID = "ups-ceo-0001"
WAKE_TEXT = "上游抖动后自动恢复,任务与上下文不变"


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()
    yield
    await meta_db.close_meta_db()


@pytest.fixture(autouse=True)
def _reset_notice_state():
    from hiveweave.services import health_notice as hn

    hn.reset_upstream_notice_state_for_tests()
    yield
    hn.reset_upstream_notice_state_for_tests()


@pytest.fixture
async def seeded_project(tmp_path):
    """Meta projects 行 + per-project DB（exec+CEO 两 agent 行）+ 内存路由。"""
    ws = str(tmp_path / "ws")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "ups-test", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    cur = await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "model_id, created_at) VALUES (?, 'UP', ?, 'UpsExec', 'executor', "
        "'active', 'gpt-x', ?)",
        [AGENT_ID, PROJECT_ID, now],
    )
    await cur.close()
    cur = await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "parent_id, created_at) VALUES (?, 'UC', ?, 'UpsCEO', 'ceo', "
        "'active', NULL, ?)",
        [CEO_ID, PROJECT_ID, now],
    )
    await cur.close()
    await conn.commit()
    from hiveweave.services.agent_router import AgentRoute, agent_router

    agent_router.reset_for_tests()
    for aid, sid, role, name in (
        (AGENT_ID, "UP", "executor", "UpsExec"),
        (CEO_ID, "UC", "ceo", "UpsCEO"),
    ):
        agent_router.register(
            AgentRoute(
                agent_id=aid,
                project_id=PROJECT_ID,
                workspace_path=ws,
                short_id=sid,
                name=name,
                role=role,
                status="active",
            )
        )
    yield conn
    agent_router.reset_for_tests()


async def _waits(conn, agent_id: str = AGENT_ID) -> list[dict]:
    cur = await conn.execute(
        "SELECT * FROM agent_waits WHERE agent_id = ? "
        "ORDER BY created_at ASC",
        [agent_id],
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


def _fake_agent(project_id: str = PROJECT_ID, run_id: str = "run-abc"):
    return SimpleNamespace(
        id=AGENT_ID, project_id=project_id, _current_run_id=run_id
    )


# ── ① 上游死亡 → durable 重醒已排 ────────────────────────────────────


async def test_upstream_death_schedules_durable_wake(seeded_project, monkeypatch):
    """is_upstream_death 判真 → agent_waits 出现 kind=timer/phase=
    upstream_recovery 等待行，attempt=1、延迟 ≈60s、唤醒文案含钦定句子。"""
    import hiveweave.agents.agent as agent_mod

    monkeypatch.setattr(agent_mod, "is_upstream_death", lambda err: True)
    await agent_mod.Agent._maybe_schedule_upstream_recovery(
        _fake_agent(), RuntimeError("stream died")
    )
    waits = await _waits(seeded_project)
    assert len(waits) == 1
    w = waits[0]
    assert w["kind"] == "timer"
    assert w["phase"] == "upstream_recovery"
    assert w["cleared_at"] is None
    assert "attempt=1" in (w["note"] or "")
    assert WAKE_TEXT in (w["ref"] or "")
    now = int(time.time() * 1000)
    assert 50_000 <= int(w["expires_at"]) - now <= 70_000


async def test_non_upstream_death_schedules_nothing(seeded_project, monkeypatch):
    """非上游死亡（is_upstream_death 判假）→ 不排任何重醒等待。"""
    import hiveweave.agents.agent as agent_mod

    monkeypatch.setattr(agent_mod, "is_upstream_death", lambda err: False)
    await agent_mod.Agent._maybe_schedule_upstream_recovery(
        _fake_agent(), RuntimeError("some tool bug")
    )
    assert await _waits(seeded_project) == []


async def test_real_contract_true_for_region_and_5xx_exhaustion():
    """真实跨组契约（llm/retry.is_upstream_death，组3 已落地）冒烟：
    403 地域 / RetryableError 5xx 耗尽 / 熔断标记 → True；429 / 402 → False。"""
    from hiveweave.llm.retry import (
        PermanentError,
        RetryableError,
        is_upstream_death,
    )

    assert is_upstream_death(
        PermanentError("This model is not available in your country.", status=403)
    )
    assert is_upstream_death(
        RetryableError("HTTP 503: Service Unavailable", status=503, headers={})
    )
    assert is_upstream_death(Exception("upstream_breaker_open (retry in 42s)"))
    assert not is_upstream_death(
        RetryableError("rate limited", status=429, headers={})
    )
    assert not is_upstream_death(PermanentError("balance", status=402))


async def test_backoff_ladder_persisted_and_exhausts_after_three(seeded_project):
    """60s/180s/600s 退避、次数持久化（含已清除行）、第 4 次耗尽不排。"""
    from hiveweave.services.wait_contract import (
        schedule_upstream_recovery_wait,
        wait_contract_service,
    )

    expected = [(1, 60_000), (2, 180_000), (3, 600_000)]
    for attempt, delay in expected:
        # 排在过去（created + delay < now）→ 立即可被 clear_expired 触发
        past = int(time.time() * 1000) - delay - 10_000
        out = await schedule_upstream_recovery_wait(
            PROJECT_ID, AGENT_ID, run_id="r1", now_ms=past
        )
        assert out["scheduled"] is True
        assert out["attempt"] == attempt
        assert out["delay_ms"] == delay
        cleared = await wait_contract_service.clear_expired(PROJECT_ID, AGENT_ID)
        assert [w["id"] for w in cleared] == [out["wait_id"]]
    out4 = await schedule_upstream_recovery_wait(
        PROJECT_ID, AGENT_ID, run_id="r1", now_ms=past
    )
    assert out4 == {"scheduled": False, "exhausted": True, "attempt": 3}


async def test_no_stacked_wake_while_pending(seeded_project):
    """已有未触发的重醒等待 → 不叠排（保留最早一次）。"""
    from hiveweave.services.wait_contract import schedule_upstream_recovery_wait

    first = await schedule_upstream_recovery_wait(PROJECT_ID, AGENT_ID)
    assert first["scheduled"] is True
    second = await schedule_upstream_recovery_wait(PROJECT_ID, AGENT_ID)
    assert second == {"scheduled": False, "reason": "pending_exists"}
    assert len(await _waits(seeded_project)) == 1


# ── ③ 通知去重窗 + 耗尽升级 ─────────────────────────────────────────


async def test_two_deaths_in_window_send_one_summary(monkeypatch):
    """窗口内第 1 次只记数；≥2 次 → 一条 ORG_ESCALATION；窗口内只发一条。"""
    from hiveweave.services import health_notice as hn

    sent: list[tuple[str, str, str]] = []

    async def _fake_deliver(agent_id, text, *, kind, **kw):
        sent.append((agent_id, kind, text))
        return True

    async def _fake_ceo(pid):
        return CEO_ID

    monkeypatch.setattr(hn, "deliver_notice", _fake_deliver)
    monkeypatch.setattr(hn, "_project_ceo_id", _fake_ceo)

    assert await hn.notify_upstream_deaths(PROJECT_ID, AGENT_ID, "run-1") is False
    assert await hn.notify_upstream_deaths(PROJECT_ID, "peer-2", "run-2") is True
    # 窗口已发 → 后续死亡只记数，不再发
    assert await hn.notify_upstream_deaths(PROJECT_ID, AGENT_ID, "run-3") is False
    assert len(sent) == 1
    aid, kind, text = sent[0]
    assert aid == CEO_ID
    assert kind == hn.KIND_ORG_ESCALATION
    assert "2 次" in text
    assert AGENT_ID[:12] in text or "peer-2"[:12] in text


async def test_exhaustion_notice_independent_of_death_window(monkeypatch):
    """重醒耗尽升级不受死亡窗限制、单独发；每 agent 30min 去重。"""
    from hiveweave.services import health_notice as hn

    sent: list[tuple[str, str, str]] = []

    async def _fake_deliver(agent_id, text, *, kind, **kw):
        sent.append((agent_id, kind, text))
        return True

    async def _fake_ceo(pid):
        return CEO_ID

    monkeypatch.setattr(hn, "deliver_notice", _fake_deliver)
    monkeypatch.setattr(hn, "_project_ceo_id", _fake_ceo)

    assert await hn.notify_upstream_recovery_exhausted(
        PROJECT_ID, AGENT_ID, attempts=3
    ) is True
    # 30min 内同 agent 的重复耗尽不再发
    assert await hn.notify_upstream_recovery_exhausted(
        PROJECT_ID, AGENT_ID, attempts=3
    ) is False
    assert len(sent) == 1
    aid, kind, text = sent[0]
    assert aid == CEO_ID
    assert kind == hn.KIND_ORG_ESCALATION
    assert "60s/180s/600s" in text


# ── ④ R11 占位行 ────────────────────────────────────────────────────


async def test_dead_run_with_declared_calls_gets_placeholder_usage(seeded_project):
    """declared>0 零账死亡 run → 全 0 占位 usage 行 + 可机检 run 侧标注。"""
    conn = seeded_project
    now = int(time.time() * 1000)
    cur = await conn.execute(
        "INSERT INTO agent_runs (id, agent_id, status, actual_llm_calls, "
        "started_at) VALUES (?, ?, 'running', 1, ?)",
        ["run-dead-1", AGENT_ID, now],
    )
    await cur.close()
    await conn.commit()

    from hiveweave.services.run_ledger import RunLedger

    err = "PermanentError: This model is not available in your country. RegionError"
    await RunLedger().error_run(AGENT_ID, "run-dead-1", err)

    cur = await conn.execute("SELECT * FROM llm_usage WHERE run_id = ?", ["run-dead-1"])
    usage = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    assert len(usage) == 1
    u = usage[0]
    assert u["input_tokens"] == 0
    assert u["output_tokens"] == 0
    assert u["cache_read_tokens"] == 0
    assert u["cache_creation_tokens"] == 0
    assert u["total_tokens"] == 0
    assert u["duration_ms"] == 0
    # 占位行可机检：无真实请求 ⇒ request_type / provider 保持 NULL
    assert u["request_type"] is None
    assert u["provider"] is None
    # model 用该 run 实际模型（agents.model_id）
    assert u["model_id"] == "gpt-x"

    cur = await conn.execute(
        "SELECT status, ended_at, result_summary FROM agent_runs WHERE id = ?",
        ["run-dead-1"],
    )
    row = await cur.fetchone()
    await cur.close()
    run = dict(row)
    assert run["status"] == "error"
    assert u["created_at"] == run["ended_at"]  # 占位时刻 = 死亡时刻
    assert "[llm_usage_placeholder:upstream]" in run["result_summary"]

    # 幂等：重复 error_run 不重复补账
    await RunLedger().error_run(AGENT_ID, "run-dead-1", err)
    cur = await conn.execute(
        "SELECT COUNT(*) AS c FROM llm_usage WHERE run_id = ?", ["run-dead-1"]
    )
    cnt = (await cur.fetchone())[0]
    await cur.close()
    assert cnt == 1


async def test_zero_declared_run_gets_no_placeholder(seeded_project):
    """declared=0（无 LLM 调用）的死亡 run ⇒ 无账可补，不插占位行。"""
    conn = seeded_project
    now = int(time.time() * 1000)
    cur = await conn.execute(
        "INSERT INTO agent_runs (id, agent_id, status, actual_llm_calls, "
        "started_at) VALUES (?, ?, 'running', 0, ?)",
        ["run-idle-1", AGENT_ID, now],
    )
    await cur.close()
    await conn.commit()

    from hiveweave.services.run_ledger import RunLedger

    await RunLedger().error_run(AGENT_ID, "run-idle-1", "idle timeout")
    cur = await conn.execute(
        "SELECT COUNT(*) AS c FROM llm_usage WHERE run_id = ?", ["run-idle-1"]
    )
    cnt = (await cur.fetchone())[0]
    await cur.close()
    assert cnt == 0


# ── ⑤ dwell / 义务时钟暂停 ──────────────────────────────────────────


async def test_dwell_paused_while_wake_pending_and_resumes_after_fire(seeded_project):
    """重醒等待未到期 → dwell 暂停判据 True；触发（clear_expired 清行）后
    自然恢复 False。game_time 的 live_wait_agents 消费同一 list_all_active。"""
    from hiveweave.services.tasks.obligations import ObligationsMixin
    from hiveweave.services.wait_contract import (
        has_active_upstream_recovery,
        schedule_upstream_recovery_wait,
        wait_contract_service,
    )

    svc = ObligationsMixin()
    out = await schedule_upstream_recovery_wait(PROJECT_ID, AGENT_ID, run_id="r1")
    assert out["scheduled"] is True

    assert await has_active_upstream_recovery(PROJECT_ID, AGENT_ID) is True
    assert await svc.has_pending_upstream_recovery(PROJECT_ID, AGENT_ID) is True
    # game_time._nudge_stale_ledger 的 live_wait_agents 正是这条查询 ——
    # 该 agent 名下任务的 stall/auto-submit/VERIFY 改派因此全部跳过。
    live_ids = {
        w["agentId"] for w in await wait_contract_service.list_all_active(PROJECT_ID)
    }
    assert AGENT_ID in live_ids

    # 时间流逝到唤醒点：expires_at 落入过去 → tick 的 clear_expired 清行
    # → 暂停解除（无需任何人工动作）。
    conn = seeded_project
    cur = await conn.execute(
        "UPDATE agent_waits SET expires_at = ? WHERE id = ?",
        [int(time.time() * 1000) - 1, out["wait_id"]],
    )
    await cur.close()
    await conn.commit()
    cleared = await wait_contract_service.clear_expired(PROJECT_ID, AGENT_ID)
    assert [w["id"] for w in cleared] == [out["wait_id"]]
    assert await has_active_upstream_recovery(PROJECT_ID, AGENT_ID) is False
    assert await svc.has_pending_upstream_recovery(PROJECT_ID, AGENT_ID) is False


# ── 唤醒链路两处识别（source 分类 + latch 穿透）─────────────────────


async def test_wake_source_for_pending_detects_upstream_recovery():
    """重醒唤醒信（[WAIT_TIMEOUT] + wakeup_reason=upstream_recovery）被
    wake_source_for_pending 分类为专属 source='upstream_recovery'。"""
    from hiveweave.agents.trigger import wake_source_for_pending

    digest = (
        "[WAIT_TIMEOUT] Your wait (timer:" + WAKE_TEXT + "(attempt=1/3)) expired. "
        "ask_outstanding=False. Resume work or re-establish a wait. "
        "details={'wait_ref': '...', 'wait_kind': 'timer', "
        "'wakeup_reason': 'upstream_recovery', 'merged_wait_ids': ['x']}"
    )
    src = await wake_source_for_pending(
        [{"id": "m1", "message_type": "system", "message": digest}]
    )
    assert src == "upstream_recovery"
    # 普通系统消息不误判
    plain = await wake_source_for_pending(
        [{"id": "m2", "message_type": "system", "message": "[WAIT_TIMEOUT] hello"}]
    )
    assert plain != "upstream_recovery"


def test_resume_latch_cleared_by_upstream_recovery_source():
    """give-up latch 被重醒 source 穿透（否则 3 连败后重醒触发在最后一米
    被吞）；普通 trigger source 仍被闩住。"""
    import hiveweave.agents.agent as agent_mod

    fake = SimpleNamespace(
        _resume_suppressed=True,
        _resume_suppressed_at=time.monotonic(),
        _clear_resume_suppressed=lambda *, reason="ok": None,
    )
    assert (
        agent_mod.Agent.try_clear_resume_suppressed(
            fake, {"trigger": True, "source": "upstream_recovery"}
        )
        is False
    )
    fake2 = SimpleNamespace(
        _resume_suppressed=True,
        _resume_suppressed_at=time.monotonic(),
        _clear_resume_suppressed=lambda *, reason="ok": None,
    )
    assert (
        agent_mod.Agent.try_clear_resume_suppressed(
            fake2, {"trigger": True, "source": "trigger"}
        )
        is True
    )
