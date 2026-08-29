"""P1-3 Phase 2 · build_messages 前缀不变式（验收标准 2）。

同一 agent 相邻两 run 的首请求 messages 必须满足前缀包含不变式：
1. System1（identity）字节级一致 —— 真实 build_identity_prompt 链路；
2. compacted 段字节级一致（本测试无摘要 → 双方都缺席）；
3. 对话主体（非 system 消息序列）：上一 run 首请求序列是本次序列的前缀
   —— 上一 run 的 user/assistant 对话 append 进本次 history；
4. 真实探针 compare_and_record 对两次输出判定 prefix_stable。

布局契约：agents/agent.py:_build_messages =
[System1][compacted?][history...][System2 动态][user]。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.agents.agent import Agent, AgentState
from hiveweave.llm.streamer.probe import compare_and_record, reset_probe

PROJECT_ID = "prefix-stability-project"
AGENT_ID = "prefix-stability-exec"


@pytest.fixture(autouse=True)
def _mock_charter_and_skills(monkeypatch):
    """隔离 _build_context_prompt 的模块级服务依赖（charter 走真实 DB）。"""
    fake_charter = SimpleNamespace(
        read_goals=AsyncMock(return_value=None),
        goals_dirty=lambda *a, **k: False,
        get_goals_version=lambda *a, **k: 0,
        set_agent_goals_version=AsyncMock(),
        read_charter=AsyncMock(return_value=None),
    )
    monkeypatch.setattr("hiveweave.agents.agent.charter_service", fake_charter)
    monkeypatch.setattr(
        "hiveweave.agents.agent.SkillRegistryService.build_active_skills_section",
        staticmethod(lambda *_a, **_k: ""),
    )
    # 函数内 import 的服务 → patch 模块属性
    fake_handoff = SimpleNamespace(
        get_accepted_handoffs=AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "hiveweave.services.handoff.HandoffService",
        lambda *a, **k: fake_handoff,
    )
    monkeypatch.setattr(
        "hiveweave.services.turn_exit.build_exit_contract_hint",
        AsyncMock(return_value=None),
    )
    yield


def _make_agent() -> Agent:
    """轻量构造（同 test_agent_interruption_counting 模式），
    但保留真实 _build_messages / _get_identity_prompt / _build_context_prompt，
    仅 mock 其外部依赖（DB / 服务层）。"""
    agent = Agent.__new__(Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    agent.config = {
        "name": "小舟",
        "role": "developer",
        "role_type": "executor",
        "model_id": "test-model",
    }
    agent.status = AgentState.PROCESSING
    agent._identity_prompt = None  # 真实 identity 缓存（首调时构建）
    agent._pending_resume_hint = None
    agent._workspace_path = None
    agent._memory = AsyncMock()
    agent._memory.build_project_context = AsyncMock(return_value="")
    agent._org = AsyncMock()
    agent._org.build_org_directory = AsyncMock(return_value="")
    agent._org.org_dirty = lambda *_a, **_k: False
    agent._conversation = AsyncMock()
    agent._conversation.get_compacted_prefix = lambda *_a, **_k: None
    agent._conversation.get_history = AsyncMock(return_value=[])
    agent._get_workspace_path = AsyncMock(return_value="")
    return agent


async def test_adjacent_runs_satisfy_prefix_invariant():
    reset_probe()
    try:
        agent = _make_agent()

        # ── run 1：首次请求 ──
        msgs1 = await agent._build_messages("q1", {})

        # ── 模拟 run 1 对话落库（append_turn 过滤 system 后追加）──
        agent._conversation.get_history = AsyncMock(
            return_value=[
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ]
        )

        # ── run 2：相邻第二 run 首请求 ──
        msgs2 = await agent._build_messages("q2", {})

        # 1. System1 字节级一致（真实 identity 链路）
        assert msgs1[0]["role"] == "system"
        assert msgs1[0] == msgs2[0]

        # 注：msgs1[1]/msgs2[1] 位置语义不同（history 空 → [1] 是 System2；
        # history 非空 → [1] 是 history 首条），位置级断言不可比。前缀
        # 不变式由下述 dialog 前缀包含 + 探针 verdict 完整覆盖。

        # 3. 对话主体前缀包含：msgs1 的非 system 序列 ⊑ msgs2 的非 system 序列
        d1 = [m for m in msgs1 if m.get("role") != "system"]
        d2 = [m for m in msgs2 if m.get("role") != "system"]
        assert d1 == d2[: len(d1)], (
            f"dialog prefix broken:\nprev={d1}\ncur_head={d2[: len(d1)]}"
        )

        # 4. 真实探针判定
        model_key = "test-model@https://example.invalid/v1"
        v1 = compare_and_record(AGENT_ID, msgs1, model_key=model_key)
        assert v1["verdict"] == "no_baseline"
        v2 = compare_and_record(AGENT_ID, msgs2, model_key=model_key)
        assert v2["verdict"] == "prefix_stable", v2
    finally:
        reset_probe()


async def test_missing_history_append_detected_as_rewrite():
    """真实链路中上一 run 的对话必然 append 进下一 run history；若
    get_history 返回空（对话蒸发类故障），探针必须报 history_rewritten
    —— 这正是探针的观测价值（前缀不变式被破坏 = 上下文丢失信号）。"""
    reset_probe()
    try:
        agent = _make_agent()
        msgs1 = await agent._build_messages("first", {})
        # 反常：上一 run 对话未落库，history 仍为空
        msgs2 = await agent._build_messages("second", {})
        assert msgs1[0] == msgs2[0]  # identity 仍稳定

        model_key = "test-model@https://example.invalid/v1"
        compare_and_record(AGENT_ID, msgs1, model_key=model_key)
        v2 = compare_and_record(AGENT_ID, msgs2, model_key=model_key)
        assert v2["verdict"] == "history_rewritten", v2
    finally:
        reset_probe()
