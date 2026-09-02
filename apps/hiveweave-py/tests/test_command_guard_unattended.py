"""s3-clone_07 报告 P0-2：命令护栏的 ask 路径必须读 unattended_mode。

`resolve_ask_with_approval` 是 shell 命令护栏的 ask 出口，此前直连
`approval_service.request_permission`，绕过了 F5 在 executor/pipeline 注册路径
上的 unattended 检查——实测 18 次 `Remove-Item -Recurse` 审批全部等满 120s
（≈28 分钟墙钟），而项目开关明明是开着的。

契约：
- unattended=True → **不入队**，确定性 deny + 指路专用工具（疏导：堵外壳疏内核）
- unattended=False → 行为与旧版完全一致（照常走在线审批）
- 前置查询自身故障 → 回落原审批路径（best-effort，不引入新故障面）
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hiveweave.services.command_guard import GuardVerdict, resolve_ask_with_approval

PROJECT_ID = "proj-unattended-test"


def _ask_verdict() -> GuardVerdict:
    return GuardVerdict(False, "ask", "Remove-Item -Recurse 命中护栏", "destructive_recursive")


@pytest.fixture
def guard_env(monkeypatch: pytest.MonkeyPatch):
    """打桩：meta_db.get_agent_by_id / is_unattended_mode / approval_service。

    返回一个 recorder 字典：{"unattended": bool|Exception, "meta_error": bool,
    "approval_called": int}
    """
    rec: dict[str, Any] = {"unattended": False, "meta_error": False, "approval_called": 0}

    async def fake_get_agent(agent_id: str):
        if rec["meta_error"]:
            raise RuntimeError("meta down")
        return {"id": agent_id, "project_id": PROJECT_ID}

    async def fake_is_unattended(pid: str | None) -> bool:
        if rec["meta_error"]:
            raise RuntimeError("meta down")
        assert pid == PROJECT_ID
        return bool(rec["unattended"])

    async def fake_request_permission(**kwargs: Any) -> None:
        rec["approval_called"] += 1
        return None

    monkeypatch.setattr("hiveweave.db.meta.get_agent_by_id", fake_get_agent)
    monkeypatch.setattr(
        "hiveweave.services.approval.is_unattended_mode", fake_is_unattended
    )
    monkeypatch.setattr(
        "hiveweave.services.approval.approval_service.request_permission",
        fake_request_permission,
    )
    return rec


async def test_unattended_instant_deny_without_enqueue(guard_env):
    rec = guard_env
    rec["unattended"] = True
    v = await resolve_ask_with_approval(
        _ask_verdict(), agent_id="a1", tool_name="bash", tool_args={}
    )
    assert v.blocked is True
    assert v.action == "deny"
    assert "unattended mode" in v.reason
    assert "delete_directory" in v.reason  # 疏导内核：堵外壳必须带替代路径
    # 关键：不入队——审批服务一次都不该被碰
    assert rec["approval_called"] == 0


async def test_unattended_off_keeps_legacy_approval_path(guard_env):
    rec = guard_env
    rec["unattended"] = False
    v = await resolve_ask_with_approval(
        _ask_verdict(), agent_id="a1", tool_name="bash", tool_args={}
    )
    # 旧行为：照常走在线审批，批准后放行
    assert v.action == "allow"
    assert v.blocked is False
    assert rec["approval_called"] == 1


async def test_meta_failure_falls_back_to_approval_path(guard_env):
    rec = guard_env
    rec["meta_error"] = True
    rec["unattended"] = True  # 即使开关是开的，查询挂了也应回落旧行为
    v = await resolve_ask_with_approval(
        _ask_verdict(), agent_id="a1", tool_name="bash", tool_args={}
    )
    assert v.action == "allow"
    assert rec["approval_called"] == 1


async def test_non_ask_verdict_untouched(guard_env):
    rec = guard_env
    rec["unattended"] = True
    v = await resolve_ask_with_approval(
        GuardVerdict(False, "allow", "", "r"), agent_id="a1", tool_name="bash"
    )
    assert v.action == "allow"
    assert rec["approval_called"] == 0
