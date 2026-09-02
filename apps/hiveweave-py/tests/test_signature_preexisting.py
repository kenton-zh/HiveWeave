"""批次 39-处置2 单元测试：P1-3 签名自指抑制（preexisting 门控）。

39 审计 P1-3：`[shared fix]` 提示 8 连发，条条指向自己 2 秒前写的错误原文——
首撞者不该收到"先读它"提示。修复：record_failure_signature 返回
preexisting/preexisting_source（签名是否早已存在及其首撞者），executor 门控
hint 只发给"别人的坑"。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services import failure_signature as fs


def _mem(sig: str, source: str | None, tail: str = "") -> dict:
    content = (
        f"[失败签名] tool=pwsh | {sig}\n"
        f"根因提示: 平台护栏拒绝\n"
        f"原文尾: {tail}\n"
        f"首个撞到的 Agent: {source}"
    )
    return {
        "type": "failure_signature",
        "content": content,
        "metadata": {"source_agent_id": source, "signature": sig},
    }


@pytest.fixture
def fake_memory():
    """MemoryService 打桩：get_project_memories 返回可配置的条目列表。"""
    svc = MagicMock()
    svc.get_project_memories = AsyncMock(return_value=[])
    svc.save_memory = AsyncMock(return_value="mem-1")
    with patch("hiveweave.services.memory.MemoryService", return_value=svc):
        yield svc


@pytest.mark.asyncio
async def test_fresh_signature_reports_not_preexisting(fake_memory):
    """首撞：共享空间没有该签名 → preexisting=False（executor 不发 hint）。"""
    fake_memory.get_project_memories = AsyncMock(return_value=[])
    rec = await fs.record_failure_signature(
        project_id="proj",
        agent_id="agent-A",
        tool_name="pwsh",
        error="Error: Command blocked: [unattended mode] something",
        attribution="平台护栏拒绝",
    )
    assert rec["written"] is True
    assert rec["preexisting"] is False


@pytest.mark.asyncio
async def test_preexisting_signature_detected_with_source(fake_memory):
    """签名早已存在（别人首撞）→ preexisting=True 且带首撞者。"""
    sig_val = None

    # 先拿到 signature_of 的真实输出，再喂给内存桩
    real_sig = fs.signature_of("Error: Command blocked: [unattended mode] X")

    async def seeded_get(pid):
        return [_mem(real_sig, "agent-A")]

    fake_memory.get_project_memories = seeded_get
    rec = await fs.record_failure_signature(
        project_id="proj",
        agent_id="agent-B",  # 别的 agent 撞了同一个坑
        tool_name="pwsh",
        error="Error: Command blocked: [unattended mode] X",
        attribution="平台护栏拒绝",
    )
    assert rec["written"] is True
    assert rec["preexisting"] is True
    assert rec["preexisting_source"] == "agent-A"
    assert sig_val is None  # placeholder，防误用


@pytest.mark.asyncio
async def test_same_source_rehit_reports_preexisting_self(fake_memory):
    """同一 agent 再撞自己的坑 → preexisting=True 且 source=自己（executor 据此抑制）。"""
    real_sig = fs.signature_of("Error: Command blocked: [unattended mode] X")

    async def seeded_get(pid):
        return [_mem(real_sig, "agent-B")]

    fake_memory.get_project_memories = seeded_get
    rec = await fs.record_failure_signature(
        project_id="proj",
        agent_id="agent-B",
        tool_name="pwsh",
        error="Error: Command blocked: [unattended mode] X",
        attribution="平台护栏拒绝",
    )
    assert rec["preexisting"] is True
    assert rec["preexisting_source"] == "agent-B"


def test_signature_of_none_for_empty_error():
    assert fs.signature_of("") is None
    assert fs.signature_of(None) is None
