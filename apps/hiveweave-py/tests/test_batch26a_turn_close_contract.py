r"""批26-A（回合收尾契约设计收敛，第五轮诊断 C5 头号）回归。

## 病灶（s23 A667 芥末实锤，DB 站在 agent 一边）

1. **唤醒循环**：一条 10-05 11:20 的 safety_timeout 中断 run 之后又完成了
   52 个 run，但 ``find_interrupted_run`` 只取「最新一条 interrupted」且
   ``status='interrupted'`` 全仓无人清除 ⇒ 每次唤醒仍注入 ``[RUN RECOVERY]``
   （stub 计到「第 45 次」），平台每次都宣称「上一回合未结束、继续」——
   而其任务早已 closed、disposition=waiting_human、等待引用有效。模型控诉
   「反复唤醒来自运行时收口判定，不来自我的动作缺口」成立。
2. **发射循环**：停泊轮里哨兵文案断言「The platform treats your work as
   still open, so you are woken again to continue」—— 与 wait 契约有效/
   义务空的 DB 事实直接矛盾；模型既不能重登记（回执禁止重复 wait_on），
   又没被给出生路 ⇒ 在「如何结束回合」决策点打转（单响应烧满 128k 输出
   预算的 Emitting 循环）。

## 修法（本文件钉死的三条判据）

- **S1（run 侧）**：恢复简报加 ``is_latest_run`` freshness 守卫 —— 中断
  历史一旦被后续成功 run 覆盖就不再是续跑判据（``Agent.
  _load_recovery_context`` 返回 ``summary=None``，简报与 stub 计数链消失）。
- **S2（哨兵侧）**：``continue_sentinel_text()`` auto 分支改为形状真实的
  机械续跑陈述 —— 不再断言「工作未收口」，并给已停泊/无事可做的模型一条
  确定出口（输出收尾文本并停止，平台自动收口）。legacy 分支逐字节不动。
- **同源纪律**：http_stream 与 cache_warmer 必须同源调用
  ``continue_sentinel_text()``（P1-3 教训；cache_warmer 行为级断言见
  ``test_cache_warmer.py::test_warm_body_replicates_continue_sentinel``）。

wait 契约 / 义务结算本体不动（那是正确的）；出口侧对停泊事实的尊重由既有
``test_t16_turn_exit_auto.py`` 覆盖（平台合成 waiting phase ⇒ 门禁放行）。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.config import settings
from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.llm.streamer.constants import (
    CONTINUE_SENTINEL,
    continue_sentinel_text,
)
from hiveweave.services import task as task_mod
from hiveweave.services import wait_contract as wait_contract_module
from hiveweave.services.run_ledger import run_ledger

PROJECT_ID = "test-batch26a-project"
AGENT_ID = "test-batch26a-agent"


@pytest.fixture
async def env():
    """per-project DB 环境（t16 测试同款夹具形态）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        wait_contract_module._migrated.clear()
        task_mod._migrated.clear()
        project_db._agent_cache[AGENT_ID] = workspace_path
        await ensure_project_db(workspace_path)

        with patch(
            "hiveweave.db.meta.get_project_workspace",
            fake_get_project_workspace,
        ):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        project_db._agent_cache.pop(AGENT_ID, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _stub_agent():
    """只带 ``_load_recovery_context`` 所需属性的最小替身（真方法体）。"""
    from hiveweave.agents.agent import Agent

    agent = object.__new__(Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    agent._run_ledger = run_ledger
    return agent


async def _load_recovery_context():
    """真方法体入口（P1-5 测试同款：Agent 方法 + 最小替身 self）。"""
    from hiveweave.agents.agent import Agent

    return await Agent._load_recovery_context(_stub_agent())


async def _insert_run(run_id: str, status: str, *, started_offset_ms: int = 0,
                      error_reason: str | None = None) -> None:
    """直插一行 agent_runs（started_at 单调递增由 offset 控制）。"""
    base = int(time.time() * 1000)
    await project_db.execute(
        AGENT_ID,
        "INSERT INTO agent_runs (id, agent_id, status, started_at, ended_at, "
        "error_reason) VALUES (?, ?, ?, ?, ?, ?)",
        [
            run_id,
            AGENT_ID,
            status,
            base + started_offset_ms,
            base + started_offset_ms + 500,
            error_reason,
        ],
    )


async def _register_valid_wait() -> None:
    """登记一条有效 wait 契约（kind=user ⇒ waiting_human 形态）。"""
    from hiveweave.services.wait_contract import wait_contract_service

    await wait_contract_service.replace_waits(
        PROJECT_ID,
        AGENT_ID,
        [{"kind": "user", "ref": "user-1", "note": "等用户终验"}],
        phase="waiting",
        obligations=[],
    )


async def _insert_step(run_id: str) -> None:
    """给中断 run 插一条已完成 tool step（checkpoint 简报走完整形态）。"""
    now = int(time.time() * 1000)
    await project_db.execute(
        AGENT_ID,
        "INSERT INTO run_steps (id, run_id, step_index, step_type, tool_name, "
        "status, started_at) VALUES (?, ?, 0, 'tool_call', 'read_file', "
        "'completed', ?)",
        [f"step-{run_id}", run_id, now],
    )


# ── S1：恢复简报让位于收口事实（芥末形态回归）──────────────────


@pytest.mark.asyncio
async def test_mustard_form_stale_interrupted_run_yields_to_settled_state(env):
    """芥末形态：中断 run 之后已有成功 run + wait 有效 + 义务空
    ⇒ 不再产出 [RUN RECOVERY] 简报（「第 43 轮」的源头被钉死）。"""
    # DB 形态对齐 s23-A667：一条 safety_timeout 中断 run，其后 52 个
    # completed run（此处插 2 个足以判陈旧）；任务侧 closed + waiting_human
    # + 零义务（wait 契约行有效）。
    await _insert_run(
        "run-interrupted-stale",
        "interrupted",
        started_offset_ms=-60_000,
        error_reason="safety_timeout",
    )
    await _insert_run("run-completed-after-1", "completed", started_offset_ms=-1000)
    await _insert_run("run-completed-after-2", "completed", started_offset_ms=0)
    await _register_valid_wait()
    from hiveweave.services.wait_contract import wait_contract_service

    waits = await wait_contract_service.list_active(PROJECT_ID, AGENT_ID)
    assert len(waits) == 1 and waits[0]["kind"] == "user"

    run_id, summary = await _load_recovery_context()

    # run_id 照返回（create_activation 取证位保留），但 summary=None ⇒
    # chat() 的 `if interrupted_summary:` 不注入任何 [RUN RECOVERY] 文本。
    assert run_id == "run-interrupted-stale"
    assert summary is None


@pytest.mark.asyncio
async def test_fresh_interrupted_run_still_generates_brief(env):
    """阳性对照（真中断不受影响）：中断 run 是本 agent 最新一行 ⇒ 照常
    生成 checkpoint 简报（正在续跑，行为与修前一致）。"""
    await _insert_run(
        "run-interrupted-fresh",
        "interrupted",
        started_offset_ms=0,
        error_reason="safety_timeout",
    )
    await _insert_step("run-interrupted-fresh")
    run_id, summary = await _load_recovery_context()
    assert run_id == "run-interrupted-fresh"
    # checkpoint 简报完整形态（非 None ⇒ chat() 会注入 [RUN RECOVERY] 全文）
    assert summary is not None and "Interrupted run had" in summary


@pytest.mark.asyncio
async def test_no_interrupted_run_returns_empty_context(env):
    """无中断 run ⇒ (None, None)（常规唤醒，零附加行为）。"""
    await _insert_run("run-completed-only", "completed", started_offset_ms=0)
    run_id, summary = await _load_recovery_context()
    assert run_id is None and summary is None


@pytest.mark.asyncio
async def test_reinterrupted_run_supersedes_stale_one(env):
    """中断后又中断 ⇒ find_interrupted_run 取最新那条且视为 fresh（P1-5
    docstring 场景 C：续跑那轮又中断，新的那条才是最新 interrupted）。"""
    await _insert_run(
        "run-interrupted-old", "interrupted", started_offset_ms=-60_000
    )
    await _insert_run(
        "run-interrupted-new",
        "interrupted",
        started_offset_ms=0,
        error_reason="safety_timeout",
    )
    await _insert_step("run-interrupted-new")
    run_id, summary = await _load_recovery_context()
    assert run_id == "run-interrupted-new"
    assert summary is not None


# ── S2：哨兵文案与停泊事实对齐 ────────────────────────────────


def test_sentinel_auto_text_truthful_at_park(monkeypatch):
    """auto 哨兵：不再断言「工作未收口」；给出已停泊/无事情形的确定出口。"""
    monkeypatch.setattr(settings, "turn_exit_mode", "auto")
    text = continue_sentinel_text()

    # ① 旧病灶措辞必须消失：状态盲的「工作未收口」断言 +「被唤醒」话术
    assert "still open" not in text
    assert "woken again" not in text
    # ② 机械事实与「非人类指令」保留（防模型把续跑误读成用户 wake）
    assert "not a new human instruction" in text
    assert "did not end with a user message" in text
    # ③ 明确否认「未收口」含义（不变式：停泊事实优先于形状判定）
    assert "does NOT mean your work is unresolved" in text
    # ④ 已停泊（wait_on 已登记）/ 无事可做 ⇒ 合法出口 = 收尾文本 + 停止
    assert "already parked" in text
    assert "auto-closes the turn when you stop" in text
    # ⑤ 真要挂起时 wait_on 配方仍在（正常工作形态不受影响）
    assert 'wait_on(refs=[{"kind": "task|agent|fact|user", "ref": "<id>"}])' in text
    # ⑥ commit_turn 已下架的教学保留
    assert "commit_turn is delisted" in text


def test_sentinel_legacy_text_byte_identical_to_constant(monkeypatch):
    """legacy 兼容：legacy 分支文案与既有 ``CONTINUE_SENTINEL`` 常量逐字节
    相等（legacy wire 形态不变的硬锚）。"""
    monkeypatch.setattr(settings, "turn_exit_mode", "legacy")
    assert continue_sentinel_text() == CONTINUE_SENTINEL
    # 旧常量本身未被改动（兼容旧导入）
    assert "still open" in CONTINUE_SENTINEL
    assert "finish outstanding steps or commit_turn" in CONTINUE_SENTINEL


def test_sentinel_same_source_call_sites():
    """同源纪律（P1-3）：http_stream 与 cache_warmer 都必须调用
    ``continue_sentinel_text()``，不得各写一份文案。行为级断言见
    ``test_cache_warmer.py::test_warm_body_replicates_continue_sentinel``；
    此处钉住另一侧（http_stream 注入点）的调用存在性。"""
    import inspect

    import hiveweave.llm.streamer.http_stream as http_stream_mod
    import hiveweave.services.cache_warmer as cache_warmer_mod

    hs_src = inspect.getsource(http_stream_mod)
    cw_src = inspect.getsource(cache_warmer_mod)
    assert "continue_sentinel_text()" in hs_src, (
        "http_stream 必须调用 continue_sentinel_text()（同源纪律）"
    )
    assert "continue_sentinel_text()" in cw_src, (
        "cache_warmer 必须调用 continue_sentinel_text()（同源纪律）"
    )
    # 两侧都不得再内联哨兵文案本体（防漂移的静态判据）
    for name, src in (("http_stream", hs_src), ("cache_warmer", cw_src)):
        assert "Re-invoking this turn" not in src, (
            f"{name} 内联了哨兵文案 —— 违反同源纪律（P1-3 教训）"
        )


# ── 不变式的边界：正常「干一半被续跑」不受影响 ─────────────────


@pytest.mark.asyncio
async def test_working_agent_still_gets_sentinel_shape(monkeypatch):
    """真有义务时哨兵照发（注入条件只看请求尾形状，不看停泊状态）——
    S2 只改文案语义，不改注入判据。"""
    monkeypatch.setattr(settings, "turn_exit_mode", "auto")
    # 注入判据（http_stream: 尾条非 user ⇒ 追加哨兵）在文案改动前后不变；
    # 本测试把「auto 文案仍是一条可追加的静态 user 尾」钉成契约。
    text = continue_sentinel_text()
    assert isinstance(text, str) and text
    assert text.startswith("[HiveWeave runtime]")
