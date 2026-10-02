"""T27: 复读型卡死断路器（P0 — S16 牧云 / D79 木棉复盘）.

被测文件: src/hiveweave/services/game_time.py
被测方法: GameTimeService._check_rumination_agents / _detect_rumination_streak

病灶：连续多个 run 零工具调用、回执逐字节相同、status=completed、
work_logs 全是 completion ⇒ 既有 distress 判据（只认 work_logs.type='error'
同签名 ≥6 次）对 completed 型空转全盲 —— 上级不知情，token 烧数小时。

测试策略（对齐 test_silence_watchdog.py）:
  - tempfile 创建真实 per-project DB；patch meta_db.get_project_workspace 路由
  - patch meta_db.query_one 控制 projects.is_started（上班豁免）
  - InboxService.send_message 以 AsyncMock 捕获（不发真实 LLM 调用）
  - 每个测试清空 game_time._states，避免 tracker 跨用例污染

四场景：
  1. 复读形态命中 + 双 notice（本人强处方 + 上级知情）+ 同 streak 幂等；
  2. 换行为重置（streak 断裂 gen+1），同签名复发可再投；
  3. 正常长文档作者（run 间摘要互异）不命中 + 单 run 不命中；
  4. error 型 run / 平台尾注摘要 / 有工具调用 / 窗口外旧账 / 新 interrupted
     run 均不命中（error 型 distress 归旧路径，不回归）。
"""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services import game_time
from hiveweave.services import wait_contract as wait_contract_module
from hiveweave.services.game_time import GameTimeService

PROJECT_ID = "test-t27-project"
CEO_ID = "t27-ceo"
EXECUTOR_ID = "t27-executor"

RUMINATED_SUMMARY = "已分析当前情况，将继续按原计划推进。"
DOC_SUMMARIES = [
    "设计文档：完成了数据流一节的初稿。",
    "设计文档：吸收评审意见，重写了数据流一节。",
    "设计文档：补齐了失败路径与回滚章节。",
]
PLATFORM_NOTE_SUMMARY = "[TURN BUDGET] 预算耗尽，已保留全部产出并收口。"


@pytest.fixture(autouse=True)
def clean_states():
    """每个测试前后清空 game_time 内存态，防止 tracker 跨用例污染."""
    game_time._states.clear()
    yield
    game_time._states.clear()


@pytest.fixture
async def env():
    """真实 per-project DB（temp workspace）+ meta_db 路由 patch.

    清理时先弹出并关闭缓存连接再删临时目录（Windows 文件占用）。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        wait_contract_module._migrated.clear()
        project_db._agent_cache[CEO_ID] = workspace_path
        project_db._agent_cache[EXECUTOR_ID] = workspace_path

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
            project_db._agent_cache.pop(CEO_ID, None)
            project_db._agent_cache.pop(EXECUTOR_ID, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


# ── Helpers ─────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _insert_agent(env, agent_id, name, parent_id=None,
                        created_at=None):
    conn = await ensure_project_db(env["workspace_path"])
    ts = created_at if created_at is not None else _now_ms()
    await conn.execute(
        "INSERT INTO agents (id, project_id, name, role, parent_id, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        [agent_id, PROJECT_ID, name, "executor", parent_id, ts, ts])
    await conn.commit()


async def _insert_run(env, agent_id, *, status="completed", tool_calls=0,
                      summary=RUMINATED_SUMMARY, age_ms=60_000):
    """插一条 agent_runs（只读消费的既有数据源，此处是测试造数）."""
    conn = await ensure_project_db(env["workspace_path"])
    ts = _now_ms() - int(age_ms)
    await conn.execute(
        "INSERT INTO agent_runs (id, agent_id, status, actual_tool_calls, "
        "result_summary, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), agent_id, status, tool_calls, summary, ts,
         ts + 30_000])
    await conn.commit()


async def _insert_rumination_streak(env, agent_id, *, summary=RUMINATED_SUMMARY,
                                    n=3, status="completed", tool_calls=0,
                                    newest_age_ms=60_000):
    """自新到旧插 n 条相同摘要的 run（默认 60s/120s/180s 前）."""
    for i in range(n):
        await _insert_run(env, agent_id, status=status, tool_calls=tool_calls,
                          summary=summary,
                          age_ms=newest_age_ms + i * 120_000)


def _seed_state():
    """_check_rumination_agents 需要 _states 里有该项目条目（生产由 tick 保证）."""
    game_time._states[PROJECT_ID] = {"project_id": PROJECT_ID}


def _trackers():
    return game_time._states[PROJECT_ID].get("rumination_trackers", {})


def _started_mock(is_started=1):
    return patch("hiveweave.db.meta.query_one",
                 new=AsyncMock(return_value={"is_started": is_started}))


def _run_check(env):
    mock_inbox = AsyncMock()
    svc = GameTimeService()

    async def _invoke():
        with _started_mock(1), \
                patch("hiveweave.services.inbox.InboxService.send_message",
                      mock_inbox):
            await svc._check_rumination_agents(PROJECT_ID)

    return _invoke, mock_inbox


# ── 场景 1: 复读形态命中 + 双 notice + 幂等 ──────────────────


async def test_rumination_hits_sends_dual_notices_and_is_idempotent(env):
    """连续 3 个 completed+零工具+同摘要 run：本人强处方 + 上级知情，同 streak 只投一轮."""
    await _insert_agent(env, EXECUTOR_ID, "牧云", parent_id=CEO_ID)
    await _insert_rumination_streak(env, EXECUTOR_ID, n=3)
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()

    assert mock_inbox.await_count == 2
    recipients = {c.kwargs["to_agent_id"] for c in mock_inbox.await_args_list}
    assert recipients == {EXECUTOR_ID, CEO_ID}
    for call in mock_inbox.await_args_list:
        assert "复读卡断路器" in call.kwargs["message"]
        assert call.kwargs["priority"] == "urgent"
        assert call.kwargs["wake"] is True
        assert call.kwargs["message_type"] == "platform_notice"
        assert call.kwargs.get("trusted_platform") is True
        assert call.kwargs.get("idempotency_key")
    self_calls = [c for c in mock_inbox.await_args_list
                  if c.kwargs["to_agent_id"] == EXECUTOR_ID]
    esc_calls = [c for c in mock_inbox.await_args_list
                 if c.kwargs["to_agent_id"] == CEO_ID]
    # 本人处方：换方法 / 向上级求助；上级知情：含 agent 名 + 摘要
    assert "换方法" in self_calls[0].kwargs["message"]
    assert "send_message" in self_calls[0].kwargs["message"]
    assert "牧云" in esc_calls[0].kwargs["message"]
    assert RUMINATED_SUMMARY[:20] in esc_calls[0].kwargs["message"]
    # 双 notice 幂等键同 streak 同代际（同 sig 同 gen：键尾两段一致）
    assert self_calls[0].kwargs["idempotency_key"].split(":")[-2:] == \
        esc_calls[0].kwargs["idempotency_key"].split(":")[-2:]

    tracker = _trackers()[EXECUTOR_ID]
    assert tracker["notified"] is True
    assert tracker["sig"]

    # 同 streak 二次扫描：不重复投递（幂等）
    await invoke()
    assert mock_inbox.await_count == 2


async def test_rumination_below_streak_threshold_does_not_hit(env):
    """只有 2 个重复 run（< N=3）：不命中，不投递."""
    await _insert_agent(env, EXECUTOR_ID, "牧云", parent_id=CEO_ID)
    await _insert_rumination_streak(env, EXECUTOR_ID, n=2)
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()

    assert mock_inbox.await_count == 0
    assert _trackers() == {}


# ── 场景 2: 换行为重置；同签名复发可再投 ─────────────────────


async def test_behavior_change_resets_and_recurrence_renotifies(env):
    """复读→换行为（摘要变化）→ 重置；再复发同签名 → 新代际幂等键再投一轮."""
    await _insert_agent(env, EXECUTOR_ID, "牧云", parent_id=CEO_ID)
    await _insert_rumination_streak(env, EXECUTOR_ID, n=3,
                                    newest_age_ms=600_000)
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()
    assert mock_inbox.await_count == 2
    first_key = [c for c in mock_inbox.await_args_list
                 if c.kwargs["to_agent_id"] == EXECUTOR_ID][0] \
        .kwargs["idempotency_key"]

    # 换行为：最新 run 摘要变了 → streak 断裂 → tracker 重置（gen+1）
    await _insert_run(env, EXECUTOR_ID, summary="改用另一条路径重试，先读配置。",
                      age_ms=30_000)
    await invoke()
    tracker = _trackers()[EXECUTOR_ID]
    assert tracker["notified"] is False
    assert tracker["gen"] == 1
    assert mock_inbox.await_count == 2  # 重置不投递

    # 复发：3 条相同摘要 run 重新占据最近三席（比换行为 run 更新），
    # 代际 +1 后同签名可再投（新幂等键）
    for age in (10_000, 15_000, 20_000):
        await _insert_run(env, EXECUTOR_ID, summary=RUMINATED_SUMMARY,
                          age_ms=age)
    await invoke()
    assert mock_inbox.await_count == 4
    self_keys = [c.kwargs["idempotency_key"]
                 for c in mock_inbox.await_args_list
                 if c.kwargs["to_agent_id"] == EXECUTOR_ID]
    assert len(self_keys) == 2 and self_keys[1] != first_key
    assert _trackers()[EXECUTOR_ID]["notified"] is True


# ── 场景 3: 正常长文档作者不命中（防误报）────────────────────


async def test_normal_doc_author_with_varying_summaries_does_not_hit(env):
    """写文档类纯文本产出：零工具调用合法，run 间摘要互异 ⇒ 不命中."""
    await _insert_agent(env, EXECUTOR_ID, "写手", parent_id=CEO_ID)
    for i, summary in enumerate(DOC_SUMMARIES):
        await _insert_run(env, EXECUTOR_ID, summary=summary,
                          age_ms=60_000 + i * 120_000)
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()

    assert mock_inbox.await_count == 0
    assert _trackers() == {}


async def test_single_long_run_does_not_hit(env):
    """单次长文本 run（< N=3）：不命中 —— 单次长输出是合法形态."""
    await _insert_agent(env, EXECUTOR_ID, "写手", parent_id=CEO_ID)
    await _insert_rumination_streak(env, EXECUTOR_ID, n=1)
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()

    assert mock_inbox.await_count == 0


# ── 场景 4: 非复读形态 / error 型不命中（旧路径不回归）────────


async def test_error_runs_do_not_trigger_rumination_notices(env):
    """error 型 run 归既有 distress 旧路径（work_logs error 签名），断路器不抢."""
    await _insert_agent(env, EXECUTOR_ID, "报错者", parent_id=CEO_ID)
    await _insert_rumination_streak(env, EXECUTOR_ID, n=3, status="error")
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()

    assert mock_inbox.await_count == 0


async def test_platform_note_summaries_do_not_accumulate_streak(env):
    """平台尾注摘要（[TAG] 形状，收口机制自产）不作为复读证据."""
    await _insert_agent(env, EXECUTOR_ID, "预算 cuts", parent_id=CEO_ID)
    await _insert_rumination_streak(env, EXECUTOR_ID, n=3,
                                    summary=PLATFORM_NOTE_SUMMARY)
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()

    assert mock_inbox.await_count == 0


async def test_runs_with_tool_calls_do_not_hit(env):
    """有工具调用的 run（哪怕摘要相同）不是复读型空转."""
    await _insert_agent(env, EXECUTOR_ID, "实干者", parent_id=CEO_ID)
    await _insert_rumination_streak(env, EXECUTOR_ID, n=3, tool_calls=2)
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()

    assert mock_inbox.await_count == 0


async def test_stale_runs_outside_lookback_do_not_hit(env):
    """窗口外旧账（最新 run 超过 lookback 30min）：归沉默看门狗，不重复报警."""
    await _insert_agent(env, EXECUTOR_ID, "旧账", parent_id=CEO_ID)
    await _insert_rumination_streak(env, EXECUTOR_ID, n=3,
                                    newest_age_ms=45 * 60_000)
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()

    assert mock_inbox.await_count == 0


async def test_new_interrupted_run_breaks_streak(env):
    """最新 run 是 interrupted（非 completed）：断 streak，不命中."""
    await _insert_agent(env, EXECUTOR_ID, "中断者", parent_id=CEO_ID)
    await _insert_run(env, EXECUTOR_ID, status="interrupted", age_ms=60_000)
    await _insert_rumination_streak(env, EXECUTOR_ID, n=3,
                                    newest_age_ms=180_000)
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()

    assert mock_inbox.await_count == 0


async def test_live_wait_contract_holder_is_skipped(env):
    """挂未过期 wait contract 的合法停泊：不投（没有新 run 在烧）."""
    from hiveweave.services.wait_contract import wait_contract_service

    await _insert_agent(env, EXECUTOR_ID, "停车者", parent_id=CEO_ID)
    await _insert_rumination_streak(env, EXECUTOR_ID, n=3)
    # 先走一次 list_all_active 确保 agent_waits schema 已建，再落一条 wait
    await wait_contract_service.list_all_active(PROJECT_ID)
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO agent_waits (id, agent_id, project_id, kind, ref, "
        "wake_on, expires_at, created_at) VALUES (?, ?, ?, ?, ?, '[]', ?, ?)",
        [str(uuid.uuid4()), EXECUTOR_ID, PROJECT_ID, "user", "user",
         _now_ms() + 10 * 60_000, _now_ms()])
    await conn.commit()
    _seed_state()

    invoke, mock_inbox = _run_check(env)
    await invoke()

    assert mock_inbox.await_count == 0


# ── 判据纯函数 ───────────────────────────────────────────────


def test_summaries_similar_identical_and_near():
    assert game_time._summaries_similar("a b c", "a  b c") is True  # 压空白全等
    assert game_time._summaries_similar("", "x") is False           # 空不参与
    assert game_time._summaries_similar("abc", "xyz") is False      # 无重合


def test_platform_note_shape_detected():
    assert game_time._is_platform_note_summary(PLATFORM_NOTE_SUMMARY) is True
    assert game_time._is_platform_note_summary(RUMINATED_SUMMARY) is False
